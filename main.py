"""astrbot_plugin_moe_music —— 连接自建 lx_music_api 后端的点歌插件。

命令：
- ``点歌 <歌名> [序号]`` 及平台别名（网易点歌 / QQ点歌 / 酷狗点歌 …）
- ``查歌词 <歌名>``
- ``点歌自检``

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

from .core.api_client import ApiError, MusicApiClient
from .core.commands import MoeMusicService
from .core.config import COMMAND_SOURCE_ALIAS, LLM_SOURCE_ALIAS, PluginConfig
from .core.lyrics_render import LyricsRenderer
from .core.sender import SongSender
from .core.songlist_render import SonglistRenderer

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

USAGE_HINT = (
    "用法：点歌 <歌名> [序号]\n"
    "也可以用：网易点歌 / QQ点歌 / 酷狗点歌 / 酷我点歌 / 咪咕点歌 <歌名>\n"
    "查歌词：<歌名> 前加「查歌词」哦～"
)


class MoeMusicPlugin(Star):
    """萌音点歌插件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig.from_astrbot_config(config)
        self.api = MusicApiClient(
            base_url=self.cfg.api_base_url,
            api_key=self.cfg.api_key,
            request_timeout=self.cfg.request_timeout,
            proxy=self.cfg.proxy,
        )
        # 临时下载目录：优先 AstrBot 临时目录，失败退回系统临时目录
        self.download_dir = Path(self._resolve_temp_dir()) / "moe_music" / uuid.uuid4().hex[:8]
        self.sender = SongSender(self.cfg, self.api, self.download_dir)
        font_path = Path(__file__).parent / "fonts" / "simhei.ttf"
        self.lyrics_renderer = LyricsRenderer(font_path)
        self.songlist_renderer = SonglistRenderer(font_path)
        self.service = MoeMusicService(
            self.cfg, self.api, self.sender, self.lyrics_renderer, self.songlist_renderer
        )

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
            f"默认音源 {self.cfg.default_source}，默认音质 {self.cfg.default_quality}"
        )
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
        """插件卸载：释放 HTTP 会话并清理临时目录。"""
        await self.api.close()
        self.sender.cleanup_download_dir()

    # ============ 命令 ============

    @filter.command("点歌", alias=SONG_COMMAND_ALIASES)
    async def song_command(self, event: AstrMessageEvent):
        """点歌 / 网易点歌 / QQ点歌 / 腾讯点歌 / 酷狗点歌 / 酷我点歌 / 咪咕点歌 / 网易 <歌名> [序号]

        点歌支持一次到位语法（歌名后跟序号直接发送）。
        """

        if not self._check_ready(event):
            return

        cmd, _, arg = event.message_str.strip().partition(" ")
        source = COMMAND_SOURCE_ALIAS.get(cmd.lower(), "")
        arg = arg.strip()

        # 解析尾部序号（“点歌 晴天 1” → index=1）
        index_hint = 0
        tokens = arg.split()
        if len(tokens) >= 2 and tokens[-1].isdigit():
            index_hint = int(tokens[-1])
            arg = " ".join(tokens[:-1])

        if not arg:
            await event.send(event.plain_result(USAGE_HINT))
            event.stop_event()
            return

        try:
            await self.service.handle_song_request(event, arg, source=source, index_hint=index_hint)
        except Exception:
            logger.error(f"[萌音点歌] 点歌处理异常：\n{traceback.format_exc()}")
            await event.send(event.plain_result("点歌出了点小问题，请稍后再试～"))
        finally:
            event.stop_event()

    @filter.command("查歌词", alias={"查看歌词"})
    async def lyrics_command(self, event: AstrMessageEvent):
        """查歌词 / 查看歌词 <歌名>，返回歌词图片"""

        if not self._check_ready(event):
            return

        cmd, _, arg = event.message_str.strip().partition(" ")
        if not arg.strip():
            await event.send(event.plain_result("用法：查歌词 <歌名>"))
            event.stop_event()
            return

        try:
            await self.service.handle_lyrics_request(event, arg.strip())
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
