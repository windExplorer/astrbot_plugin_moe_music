"""astrbot_plugin_moe_music —— 连接自建 lx_music_api 后端的点歌插件。

命令：
- ``点歌 <歌名> [序号]`` 及平台别名（网易点歌 / QQ点歌 / 酷狗点歌 …）
- ``点歌文件 <歌名> [序号]``（下载音乐文件并内嵌封面与歌词）
- ``查歌词 <歌名>``
- ``点歌自检``

分享识别（v0.11.5）：
- 引用别人的分享再发「点歌」/「点歌文件」→ 直接播放 / 下载那首歌（不受开关影响）；
- 开启 share_auto_play（默认关闭）后：群里有人分享 QQ音乐 / 网易云 / 酷狗 / 酷我
  的歌曲卡片或链接 → 自动识别并发语音。

LLM Tool：
- ``play_song_by_name`` / ``query_lyrics_by_name``
"""

import asyncio
import traceback
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core.access import AccessController
from .core.api_client import ApiError, MusicApiClient
from .core.commands import MoeMusicService
from .core.config import LLM_SOURCE_ALIAS, PluginConfig, resolve_command_source
from .core.enrich import Enricher
from .core.info_cache import InfoCache
from .core.lyrics_render import LyricsRenderer
from .core.queue import SongTaskQueue
from .core.sender import SongSender
from .core.share import extract_quoted_share, extract_share
from .core.song_card_render import SongCardRenderer
from .core.songlist_render import SonglistRenderer
from .core.storage import RecordStore

# 点歌命令全部别名（含大小写变体，CommandFilter 匹配为大小写敏感，故全部显式列出）
SONG_COMMAND_ALIASES = {
    "网易点歌",
    "网易",
    "QQ点歌",
    "qq点歌",
    "Qq点歌",
    "qQ点歌",
    "腾讯点歌",
    "酷狗点歌",
    "酷我点歌",
    "咪咕点歌",
}

# 「点歌文件」指令全部别名：与点歌别名同构（点歌名 + “文件”），含大小写变体；
# 「下载」是它的短别名（主要配合「引用分享 + 下载」使用）
FILE_COMMAND_ALIASES = {f"{name}文件" for name in SONG_COMMAND_ALIASES} | {"下载"}

USAGE_HINT = (
    "用法：点歌 <歌名> [序号]\n"
    "也可以用：网易点歌 / QQ点歌 / 酷狗点歌 / 酷我点歌 / 咪咕点歌 <歌名>\n"
    "查歌词 / 下载：<歌名> 前加「歌词」或「下载」哦～\n"
    "引用歌曲分享（或刚发的卡片）再发「点歌 / 下载 / 歌词」，可直接点播 / 下载 / 查歌词～"
)

FILE_USAGE_HINT = (
    "用法：点歌文件 <歌名> [序号]（也可直接说「下载 <歌名>」）\n"
    "也可以用：网易点歌文件 / QQ点歌文件 / 酷狗点歌文件 / 酷我点歌文件 / 咪咕点歌文件 <歌名>\n"
    "引用歌曲分享（或刚发的卡片）再发「下载 / 点歌文件」，可直接下载那首歌的文件～\n"
    "会下载音乐文件并写入封面与歌词（音质可在配置里单独调整）～"
)


class MoeMusicPlugin(Star):
    """萌音点歌插件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # AstrBotConfig 原始配置（WebUI 配置页读写用）
        self.cfg = PluginConfig.from_astrbot_config(config)
        self.api = MusicApiClient(
            base_url=self.cfg.api_base_url,
            api_key=self.cfg.api_key,
            request_timeout=self.cfg.request_timeout,
            proxy=self.cfg.proxy,
            public_base_url=self.cfg.public_base_url,
        )
        # 临时下载目录：优先 AstrBot 临时目录，失败退回系统临时目录
        self.download_dir = Path(self._resolve_temp_dir()) / "moe_music" / uuid.uuid4().hex[:8]
        # 记录库：存 AstrBot 数据目录（不随插件卸载删除，供后续统计）
        data_db = Path(self._resolve_data_dir()) / "moe_music" / "records.db"
        self.store = RecordStore(data_db)
        # 信息缓存库（歌曲/歌手增强信息，歌曲信息卡片用）：与记录库同文件、独立表
        self.info_cache = InfoCache(data_db)
        self.access = AccessController(self.cfg)
        # 点歌任务队列：限制并发、队满拒绝，排队耗时入统计
        self.queue = SongTaskQueue(self.cfg.queue_concurrency, self.cfg.queue_max_pending)
        self.sender = SongSender(self.cfg, self.api, self.download_dir, store=self.store)
        font_path = Path(__file__).parent / "fonts" / "simhei.ttf"
        self.lyrics_renderer = LyricsRenderer(font_path)
        self.songlist_renderer = SonglistRenderer(font_path)
        self.song_card_renderer = SongCardRenderer(font_path)
        self.enricher = Enricher(
            self.api,
            proxy=self.cfg.proxy,
            provider_getter=self._llm_provider_for,
            llm_ask=self._llm_ask,
        )
        self.service = MoeMusicService(
            self.cfg,
            self.api,
            self.sender,
            self.lyrics_renderer,
            self.songlist_renderer,
            store=self.store,
            access=self.access,
            queue=self.queue,
            card_renderer=self.song_card_renderer,
            info_cache=self.info_cache,
            enricher=self.enricher,
            out_of_band_sender=self._send_to_session,
        )

    async def _send_to_session(self, umo: str, chain: list) -> bool:
        """按 unified_msg_origin 会话外主动发消息（信息卡片补发用）。

        补发发生在消息流水线结束之后，事件对象可能已不可用，走框架的
        ``context.send_message`` 路由到对应平台。返回是否发送成功。
        """
        if self.context is None:
            return False
        try:
            from astrbot.core.message.message_event_result import MessageChain

            return bool(await self.context.send_message(umo, MessageChain(chain=chain)))
        except Exception as e:
            logger.warning(f"[萌音点歌] 会话外消息发送失败：{type(e).__name__}: {e}")
            return False

    async def _llm_provider_for(self, umo: str | None = None):
        """取获取卡片信息用的对话模型；没有可用模型时返回 None。

        配置了 ``llm_provider_id`` 时固定用该模型（推荐挂便宜快速的，或开了
        联网搜索的）；ID 无效时记警告并退回系统默认。留空则跟随系统：
        优先当前会话使用的模型，会话没有指定时退回全局任意一个 Chat Provider。
        """
        if self.context is None:
            return None
        provider_id = self.cfg.llm_provider_id
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception as e:
                logger.debug(f"[萌音点歌] 按 ID 获取模型失败：{type(e).__name__}: {e}")
                provider = None
            if provider is not None:
                return provider
            logger.warning(
                f"[萌音点歌] 找不到模型 ID「{provider_id}」（WebUI → 对话模型 页查看 ID），"
                "已回退为系统当前对话模型"
            )
        try:
            provider = await self.context.get_using_provider_async(umo)
        except Exception as e:
            logger.debug(f"[萌音点歌] 获取会话对话模型失败：{type(e).__name__}: {e}")
            provider = None
        if provider is not None:
            return provider
        try:
            providers = self.context.get_all_providers()
            return providers[0] if providers else None
        except Exception as e:
            logger.debug(f"[萌音点歌] 获取可用对话模型列表失败：{type(e).__name__}: {e}")
            return None

    async def _llm_ask(self, event, umo: str | None, prompt: str, system_prompt: str) -> str | None:
        """调当前对话模型生成文本（卡片信息获取用）。

        会话开启联网搜索时（provider_settings.web_search）走框架的 agent 循环，
        LLM 可以调用搜索工具核实资料——查不到的才允许返回 null，不编造。
        没开联网搜索、或 agent 循环失败时退回直调模型。
        """
        if self.context is None:
            return None
        prov = await self._llm_provider_for(umo)
        if prov is None:
            return None
        toolset = None
        if self.cfg.llm_web_search:
            toolset = await self._web_search_toolset(event, umo)
        if toolset is not None and umo:
            try:
                # agent 循环按 ID 取模型：配置了专用模型时用它，否则用会话当前模型
                chat_provider_id = self.cfg.llm_provider_id or (
                    await self.context.get_current_chat_provider_id(umo)
                )
                resp = await self.context.tool_loop_agent(
                    event=event,
                    chat_provider_id=chat_provider_id,
                    tools=toolset,
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
                text = str(getattr(resp, "completion_text", "") or "").strip()
                if text:
                    return text
            except Exception as e:
                logger.info(f"[萌音点歌] LLM（联网模式）生成失败，回退直调：{type(e).__name__}: {e}")
        try:
            resp = await prov.text_chat(prompt=prompt, system_prompt=system_prompt)
            return str(getattr(resp, "completion_text", "") or "").strip() or None
        except Exception as e:
            logger.info(f"[萌音点歌] LLM 生成失败：{type(e).__name__}: {e}")
            return None

    async def _web_search_toolset(self, event, umo: str | None):
        """按会话的 provider_settings 构建联网搜索工具集；未开启返回 None。

        复用框架 ``_apply_web_search_tools``（与主对话流程同一套 Web Searcher
        配置：tavily / bocha / brave / …），版本不兼容时静默降级为无工具直调。
        """
        if event is None or not umo or self.context is None:
            return None
        try:
            from astrbot.core.astr_main_agent import _apply_web_search_tools
            from astrbot.core.provider.entities import ProviderRequest

            req = ProviderRequest(prompt="")
            await _apply_web_search_tools(event, req, self.context)
            return getattr(req, "func_tool", None)
        except Exception as e:
            logger.debug(f"[萌音点歌] 构建联网搜索工具失败：{type(e).__name__}: {e}")
            return None

    @staticmethod
    def _resolve_data_dir() -> str:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return get_astrbot_data_path()
        except Exception:
            import tempfile

            return tempfile.gettempdir()

    @staticmethod
    def _resolve_temp_dir() -> str:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            return get_astrbot_temp_path()
        except Exception:
            import tempfile

            return tempfile.gettempdir()

    async def initialize(self):
        """插件加载：启动时后台自检，仅记录日志不发言。"""
        if not self.cfg.api_base_url or not self.cfg.api_key:
            logger.warning("[萌音点歌] 尚未配置后端地址或 API Key，点歌功能不可用，请在插件配置中填写")
            return
        logger.info(
            f"[萌音点歌] 初始化完成：服务 {self.cfg.api_base_url}，Key {self.cfg.key_masked}，"
            f"默认音源 {self.cfg.default_source}，默认音质 {self.cfg.default_quality}，"
            f"队列并发 {self.cfg.queue_concurrency}"
        )
        await self.queue.start()
        try:
            from .webui_api import register_web_api

            register_web_api(self)
        except Exception:
            logger.warning("[萌音点歌] WebUI 路由注册失败（AstrBot 版本可能不支持插件 Pages），控制台不可用")
        asyncio.create_task(self._startup_probe())

    async def apply_webui_config(self, clean: dict) -> None:
        """WebUI 配置页保存：写回 AstrBotConfig 并热更新运行时组件。"""
        self.config.update(clean)
        await self.config.save_config_async()

        old_api = self.api
        self.cfg = PluginConfig.from_astrbot_config(self.config)
        new_api = MusicApiClient(
            base_url=self.cfg.api_base_url,
            api_key=self.cfg.api_key,
            request_timeout=self.cfg.request_timeout,
            proxy=self.cfg.proxy,
            public_base_url=self.cfg.public_base_url,
        )
        # 迁移 Key 限额缓存，避免刚保存完就重新自检
        new_api.key_max_quality = old_api.key_max_quality
        new_api.key_default_quality = old_api.key_default_quality
        new_api.key_qps_limit = old_api.key_qps_limit
        self.api = new_api
        await old_api.close()

        self.sender.cfg = self.cfg
        self.sender.api = new_api
        self.service.cfg = self.cfg
        self.service.api = new_api
        self.enricher.api = new_api
        self.enricher.set_proxy(self.cfg.proxy)
        self.access = AccessController(self.cfg)
        self.service.access = self.access
        logger.info(f"[萌音点歌] 已通过 WebUI 更新配置：{sorted(clean)}（队列并发数等结构性配置重启后生效）")

        # Key 限额缓存（音质上限 / QPS）会因换 Key 或后端调整而失效——不刷新就会按过期
        # 上限错误收敛音质（表现为「配置的音质不生效」）。保存后后台重新自检一次。
        asyncio.create_task(self._startup_probe())

    async def _startup_probe(self):
        """启动自检：成功则更新 Key 限额缓存；失败仅记日志。"""
        try:
            await self.api.me()
        except ApiError as e:
            logger.warning(f"[萌音点歌] 启动自检未通过：code={e.code}（Key {self.cfg.key_masked}）")
        except Exception:
            logger.warning("[萌音点歌] 启动自检异常（网络不可达？）")

    async def terminate(self):
        """插件卸载：停后台补齐任务与队列、释放 HTTP 会话、关库并清理临时目录。"""
        await self.service.shutdown_background()
        await self.queue.stop()
        await self.api.close()
        await self.enricher.close()
        await self.store.close()
        await self.info_cache.close()
        self.sender.cleanup_download_dir()

    # ============ 分享识别 ============

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def share_message(self, event: AstrMessageEvent):
        """群里分享 QQ音乐 / 网易云 / 酷狗 / 酷我 的歌曲卡片或链接 → 自动发语音。

        认不出分享内容时立刻返回，不打断本条消息的其他处理（命令 / AI 照常）。
        识别成功则吞掉事件：已经发过语音，不该再让 AI 复读一句。
        """
        if not self.cfg.share_auto_play:
            return
        # 未配置后端时静默放弃——这里是「每条消息都会走」的钩子，
        # 不能像 _check_ready 那样每条都提示「音乐服务配置有误」。
        if not self.cfg.api_base_url or not self.cfg.api_key:
            return
        share = self._extract_share_safely(event)
        if share is None:
            return
        try:
            await self.service.handle_share_request(event, share)
        except Exception:
            logger.error(f"[萌音点歌] 分享处理异常：\n{traceback.format_exc()}")
            await event.send(event.plain_result("这首歌暂时发不出来，稍后再试试吧～"))
        finally:
            event.stop_event()

    @staticmethod
    def _extract_share_safely(event: AstrMessageEvent):
        """提取本条消息里的分享（异常一律当成「没有分享」，绝不影响消息的其他处理）。"""
        try:
            return extract_share(event.get_messages())
        except Exception:
            logger.warning(f"[萌音点歌] 分享识别异常：\n{traceback.format_exc()}")
            return None

    async def _handle_quoted_share(
        self, event: AstrMessageEvent, *, kind: str = "play", command: str
    ) -> bool:
        """「引用 + 指令（不带歌名）」：优先认音乐分享卡片/链接，其次认我们刚发的卡片。

        引用**音乐分享**（json 卡片 / 分享链接）→ 按分享解析那首歌；
        引用**我们发的信息卡片 / 语音 / 文件**（无法解析出分享信息的媒体消息）
        → 用会话最近一次点歌的曲目还原（``service.last_track``，10 分钟内有效）。

        Args:
            kind: ``play`` 按分享策略发送（默认语音）/ ``file`` 下载文件 / ``lyrics`` 发歌词。

        Returns:
            bool: 引用里是否识别到目标（True 表示本次请求已接管）。
        """
        try:
            share = extract_quoted_share(event.get_messages())
        except Exception:
            logger.warning(f"[萌音点歌] 引用分享识别异常：\n{traceback.format_exc()}")
            share = None

        track = None
        if share is not None:
            try:
                if kind == "lyrics":
                    await self.service.handle_share_lyrics(event, share)
                else:
                    await self.service.handle_share_request(
                        event, share, file_mode=(kind == "file"), command=command
                    )
            except Exception:
                logger.error(f"[萌音点歌] 引用分享处理异常：\n{traceback.format_exc()}")
                await event.send(event.plain_result("这首歌暂时发不出来，稍后再试试吧～"))
            return True

        # 引用我们发的卡片 / 语音 / 文件：内容里解析不出分享信息，用最近点歌记忆还原
        if self._quoted_has_media(event):
            track = self.service.last_track(event)
            if track is not None:
                try:
                    await self.service.handle_track_request(
                        event, track, file_mode=(kind == "file"), lyrics=(kind == "lyrics"), command=command
                    )
                except Exception:
                    logger.error(f"[萌音点歌] 引用卡片处理异常：\n{traceback.format_exc()}")
                    await event.send(event.plain_result("这首歌暂时发不出来，稍后再试试吧～"))
                return True
            logger.info(f"[萌音点歌] 引用了媒体消息但会话内没有可用的最近点歌记录（{command}）")
        return False

    @staticmethod
    def _quoted_has_media(event: AstrMessageEvent) -> bool:
        """被引用的消息里是否含图片 / 语音 / 文件（我们发的卡片、语音、下载文件都是）。"""
        try:
            chain = []
            for comp in event.get_messages() or []:
                if type(comp).__name__ == "Reply":
                    chain.extend(getattr(comp, "chain", None) or [])
            return any(type(seg).__name__ in {"Image", "Record", "File"} for seg in chain)
        except Exception:
            return False

    # ============ 命令 ============

    @filter.command("点歌", alias=SONG_COMMAND_ALIASES)
    async def song_command(self, event: AstrMessageEvent):
        """点歌 / 网易点歌 / QQ点歌 / 腾讯点歌 / 酷狗点歌 / 酷我点歌 / 咪咕点歌 / 网易 <歌名> [序号]

        点歌支持一次到位语法（歌名后跟序号直接发送）；
        不带歌名、但引用了别人的歌曲分享时，直接点那首。
        """

        if not self._check_ready(event):
            return

        cmd, _, arg = event.message_str.strip().partition(" ")
        source = resolve_command_source(cmd)
        arg = arg.strip()

        # 解析尾部序号（“点歌 晴天 1” → index=1）
        index_hint = 0
        tokens = arg.split()
        if len(tokens) >= 2 and tokens[-1].isdigit():
            index_hint = int(tokens[-1])
            arg = " ".join(tokens[:-1])

        if not arg:
            # 「引用分享 + 点歌」：把引用里的歌当成歌名
            if await self._handle_quoted_share(event, kind="play", command=cmd):
                event.stop_event()
                return
            await event.send(event.plain_result(USAGE_HINT))
            event.stop_event()
            return

        try:
            await self.service.handle_song_request(
                event, arg, source=source, index_hint=index_hint, command=cmd
            )
        except Exception:
            logger.error(f"[萌音点歌] 点歌处理异常：\n{traceback.format_exc()}")
            await event.send(event.plain_result("点歌出了点小问题，请稍后再试～"))
        finally:
            event.stop_event()

    @filter.command("点歌文件", alias=FILE_COMMAND_ALIASES)
    async def song_file_command(self, event: AstrMessageEvent):
        """点歌文件 / 网易点歌文件 / QQ点歌文件 / 酷狗点歌文件 <歌名> [序号]

        平台前缀别名与点歌一致（网易 / QQ / 腾讯 / 酷狗 / 酷我 / 咪咕）+「点歌文件」。
        下载音乐文件（内嵌封面与歌词），音质与普通点歌分开配置；流程同点歌。
        不带歌名、但引用了别人的歌曲分享时，直接下载那首歌的文件。
        """

        if not self._check_ready(event):
            return

        cmd, _, arg = event.message_str.strip().partition(" ")
        # 平台识别：命令去掉「文件」后缀后与点歌别名同构，复用同一张映射表
        source = resolve_command_source(cmd, file_mode=True)
        arg = arg.strip()

        # 解析尾部序号（“点歌文件 晴天 1” → index=1）
        index_hint = 0
        tokens = arg.split()
        if len(tokens) >= 2 and tokens[-1].isdigit():
            index_hint = int(tokens[-1])
            arg = " ".join(tokens[:-1])

        if not arg:
            # 「引用分享 + 点歌文件 / 下载」：下载引用里那首歌的文件本体
            if await self._handle_quoted_share(event, kind="file", command=cmd):
                event.stop_event()
                return
            await event.send(event.plain_result(FILE_USAGE_HINT))
            event.stop_event()
            return

        try:
            await self.service.handle_song_request(
                event, arg, source=source, index_hint=index_hint, command=cmd, file_mode=True
            )
        except Exception:
            logger.error(f"[萌音点歌] 点歌文件处理异常：\n{traceback.format_exc()}")
            await event.send(event.plain_result("下载文件出了点小问题，请稍后再试～"))
        finally:
            event.stop_event()

    @filter.command("查歌词", alias={"查看歌词", "歌词"})
    async def lyrics_command(self, event: AstrMessageEvent):
        """查歌词 / 查看歌词 / 歌词 <歌名>，返回歌词图片；引用歌曲分享可直接查那首"""

        if not self._check_ready(event):
            return

        cmd, _, arg = event.message_str.strip().partition(" ")
        if not arg.strip():
            # 「引用分享 + 歌词」：查引用里那首歌的歌词
            if await self._handle_quoted_share(event, kind="lyrics", command=cmd):
                event.stop_event()
                return
            await event.send(
                event.plain_result("用法：查歌词 <歌名>（引用歌曲分享或刚发的卡片可直接查那首）")
            )
            event.stop_event()
            return

        try:
            await self.service.handle_lyrics_request(event, arg.strip(), command=cmd)
        except Exception:
            logger.error(f"[萌音点歌] 查歌词处理异常：\n{traceback.format_exc()}")
            await event.send(event.plain_result("歌词查询出了点小问题，请稍后再试～"))
        finally:
            event.stop_event()

    @filter.command("点歌自检")
    async def self_test_command(self, event: AstrMessageEvent):
        """点歌自检：检测与音乐后端的连接及 API Key 限额"""

        if not self.cfg.enable_self_test:
            return
        try:
            await self.service.handle_self_test(event)
        except Exception:
            logger.error(f"[萌音点歌] 自检异常：\n{traceback.format_exc()}")
            await event.send(event.plain_result("音乐服务开小差了，请稍后再试～"))
        finally:
            event.stop_event()

    # ============ LLM Tool ============

    @filter.llm_tool()
    async def play_song_by_name(self, event: AstrMessageEvent, song_name: str, source: str = ""):
        """当用户想听歌、点歌、播放音乐时，根据歌名搜索并发送歌曲。用户提到具体音源平台时传入 source。

        Args:
            song_name(string): 歌曲名称，可包含歌手名，如 "晴天 周杰伦"
            source(string): 音乐平台，可选。允许值：网易、QQ音乐、腾讯、酷狗、酷我、咪咕。留空则聚合搜索
        """
        if not self.cfg.api_key:
            return "音乐服务尚未配置，无法点歌"
        platform = LLM_SOURCE_ALIAS.get(source.strip().lower(), "") if source else ""
        try:
            return await self.service.llm_play_song(event, song_name.strip(), source=platform)
        except Exception:
            logger.error(f"[萌音点歌] AI 点歌异常：\n{traceback.format_exc()}")
            return "点歌失败，请稍后再试"

    @filter.llm_tool()
    async def query_lyrics_by_name(self, event: AstrMessageEvent, song_name: str):
        """当用户想看歌词、查歌词时，根据歌名搜索并发送歌词图片。

        Args:
            song_name(string): 歌曲名称，可包含歌手名，如 "晴天 周杰伦"
        """
        if not self.cfg.api_key:
            return "音乐服务尚未配置，无法查询歌词"
        try:
            return await self.service.llm_query_lyrics(event, song_name.strip())
        except Exception:
            logger.error(f"[萌音点歌] AI 查歌词异常：\n{traceback.format_exc()}")
            return "歌词查询失败，请稍后再试"

    # ============ 内部 ============

    def _check_ready(self, event: AstrMessageEvent) -> bool:
        """检查配置是否就绪；未就绪时发送提示。"""
        if not self.cfg.api_base_url or not self.cfg.api_key:
            logger.warning("[萌音点歌] 后端地址或 API Key 未配置，已拒绝本次点歌请求")
            event.stop_event()
            asyncio.create_task(self._send_ready_hint(event))
            return False
        return True

    @staticmethod
    async def _send_ready_hint(event: AstrMessageEvent):
        try:
            await event.send(event.plain_result("音乐服务配置有误，请联系管理员～"))
        except Exception:
            logger.warning("[萌音点歌] 未配置提示发送失败")
