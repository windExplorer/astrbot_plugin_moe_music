"""点歌业务逻辑：点歌 / 查歌词 / 自检 / LLM Tool 的共用实现。

命令 handler（main.py）只做参数解析与转发；本模块负责访问控制、与后端交互、
候选列表展示、session_waiter 选歌等待、歌词渲染、记录持久化与异常兜底。
用户侧只输出温馨提示，技术细节全部进日志。
"""

import asyncio
import base64
import re
import time
import traceback

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)

from .access import AccessController
from .api_client import ApiError, MusicApiClient
from .config import COMMAND_SOURCE_ALIAS, PluginConfig
from .info_cache import InfoCache, fresh
from .lyrics_render import LyricsRenderer
from .model import Track
from .onebot import call_action_of, message_id_payload, send_message_via_onebot
from .sender import FILE_ONLY_MODES, DeliveryOptions, SongSender, send_lyrics_image
from .share import ShareInfo
from .song_card_render import CardInfo
from .songlist_render import SonglistRenderer
from .storage import RecordStore

# 归一化歌名/歌手时的噪声字符（空格、标点、括号说明等，含全角）
_MATCH_NOISE_RE = re.compile(r"[\s\-_/·、,，.。!！?？'\"“”‘’()（）\[\]【】]+")


def _norm_text(text: str) -> str:
    """歌词/歌曲名归一化：小写 + 去空格与标点，用于分享卡片与搜索结果的相似度比较。"""
    return _MATCH_NOISE_RE.sub("", (text or "").lower())


def pick_best_track(
    tracks: list[Track], title: str, singer: str = "", platform: str = ""
) -> Track | None:
    """从搜索结果里挑出最像分享卡片的那一首（歌名 > 歌手 > 同平台）。

    分享卡片自带歌名与歌手，聚合搜索下直接取第 1 条容易选到翻唱/现场版/合集；
    这里按文本重合度打分，最高分为 0（完全不像）时退回第 1 条——搜索关键词本身
    就是「歌名 歌手」，搜出来的第 1 条即搜索引擎的相关度判断。
    """
    if not tracks:
        return None
    want_title, want_singer = _norm_text(title), _norm_text(singer)
    best: Track | None = None
    best_score = -1
    for track in tracks:
        name, artist = _norm_text(track.name), _norm_text(track.singer)
        score = 0
        if want_title:
            if name == want_title:
                score += 100
            elif want_title in name or name in want_title:
                score += 60
        if want_singer and artist:
            if want_singer == artist:
                score += 40
            elif want_singer in artist or artist in want_singer:
                score += 20
        if platform and track.source == platform:
            score += 10
        if score > best_score:
            best, best_score = track, score
    if best_score <= 0:
        logger.info("[萌音点歌] 分享搜索结果与卡片信息无明显重合，取搜索第 1 条")
    return best


class _UserSessionFilter(SessionFilter):
    """会话维度隔离：统一消息来源 + 发送者 ID。"""

    def filter(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


def _is_song_command_text(text: str) -> bool:
    """判断一条消息是否是新的点歌命令（用于等待选号时的重复点歌提示）。"""
    cmd = text.strip().partition(" ")[0].strip().lower()
    return cmd in COMMAND_SOURCE_ALIAS


def _bytes_to_b64(data: bytes) -> str:
    """图片消息段的 file 字段值。

    必须带 ``base64://`` 前缀（AstrBot 官方实现同此）：裸 base64 会被协议端当成
    文件路径去查找，直接报 retcode=1200「未知文件类型或路径不存在」——这正是
    图片菜单模式下候选列表拿不到 message_id、进而无法自动撤回的根因。
    """
    return "base64://" + base64.b64encode(data).decode()


class MoeMusicService:
    """点歌业务服务。"""

    QUEUE_BUSY_HINT = "点歌的人有点多啦，稍等一下再试吧～"

    def __init__(
        self,
        config: PluginConfig,
        api: MusicApiClient,
        sender: SongSender,
        lyrics_renderer: LyricsRenderer,
        songlist_renderer: SonglistRenderer,
        store: RecordStore | None = None,
        access: AccessController | None = None,
        queue=None,
        card_renderer=None,
        info_cache: InfoCache | None = None,
        enricher=None,
    ):
        self.cfg = config
        self.api = api
        self.sender = sender
        self.lyrics_renderer = lyrics_renderer
        self.songlist_renderer = songlist_renderer
        self.store = store
        self.access = access
        self.queue = queue  # SongTaskQueue；None 表示直通执行（测试/降级）
        # 歌曲信息卡片：渲染器 / 信息缓存 / 增强信息抓取器（任一缺失即降级为不发卡片）
        self.card_renderer = card_renderer
        self.info_cache = info_cache
        self.enricher = enricher
        # 会话 -> 最近一次候选列表「能否被撤回」的结论（撤回失败时输出，免翻旧日志）
        self._candidate_diag: dict[str, str] = {}
        # (会话, 曲目) -> 上次发卡片时间：同会话短期重发同一首时跳过卡片，防刷屏
        self._card_sent_at: dict[tuple[str, str], float] = {}
        # 后台信息补齐任务（terminate 时统一取消，避免卸载后还在打外部接口）
        self._bg_tasks: set[asyncio.Task] = set()
        # 会话 -> (最近一次点歌的曲目, 时间)：引用我们发的卡片/语音时还原曲目用
        self._recent_tracks: dict[str, tuple[Track, float]] = {}

    def _note_candidate_diag(self, event: AstrMessageEvent, reason: str) -> None:
        """记下本次候选列表的撤回可行性结论（按会话维度）。"""
        try:
            self._candidate_diag[event.unified_msg_origin] = reason
        except Exception:
            pass

    def _candidate_diag_of(self, event: AstrMessageEvent) -> str:
        try:
            return self._candidate_diag.get(event.unified_msg_origin, "未记录")
        except Exception:
            return "未记录"

    async def _run_task(self, fn, *args, **kwargs):
        """任务入队执行（排队耗时由 worker 注入 fn 的 queue_wait_ms 参数）；队满抛 QueueFull。"""
        if self.queue is not None:
            return await self.queue.submit(fn, *args, **kwargs)
        return await fn(*args, queue_wait_ms=0, **kwargs)

    # ============ 上下文 / 访问控制 / 记录 ============

    @staticmethod
    async def _collect_context(event: AstrMessageEvent) -> dict:
        """采集用户与会话上下文（记录用），尽力而为不抛错。"""
        is_private = event.is_private_chat()
        ctx = {
            "user_id": str(event.get_sender_id() or ""),
            "user_name": str(event.get_sender_name() or ""),
            "platform": str(event.get_platform_name() or ""),
            "message_type": "private" if is_private else "group",
            "group_id": "" if is_private else str(event.get_group_id() or ""),
            "group_name": "",
            "unified_msg_origin": str(event.unified_msg_origin or ""),
            "is_admin": 0,
        }
        try:
            ctx["is_admin"] = int(bool(event.is_admin()))
        except Exception:
            pass
        if ctx["group_id"]:
            getter = getattr(event, "get_group", None)
            if getter is not None:
                try:
                    group = await getter()
                    ctx["group_name"] = str(getattr(group, "group_name", "") or "")
                except Exception:
                    pass  # 群名获取失败留空即可
        return ctx

    async def _check_access(self, event: AstrMessageEvent, ctx: dict) -> bool:
        """白/黑名单检查；拒绝时发送提示并返回 False。"""
        if not self.access or not self.access.enabled:
            return True
        allowed, deny_kind = self.access.check(event)
        if allowed:
            return True
        logger.info(
            f"[萌音点歌] 已拦截点歌请求：{deny_kind}（user={ctx.get('user_id')} "
            f"group={ctx.get('group_id')} platform={ctx.get('platform')}）"
        )
        await event.send(event.plain_result(self.access.deny_hint(deny_kind)))
        return False

    async def _search_with_record(
        self,
        event: AstrMessageEvent,
        keyword: str,
        limit: int,
        source: str,
        ctx: dict,
        trigger_type: str,
        queue_wait_ms: int = 0,
        search_quality: str | None = None,
        send_hint: bool = True,
    ) -> tuple[list[Track] | None, int]:
        """搜索 + 写搜索记录（成败均记）；失败时发送提示并返回 ``(None, 耗时)``。

        后端音源被连续请求短暂冷却时会返回空结果（实测存在），因此空结果
        间隔 3 秒自动重试一次，仍为空才判定无结果。

        Args:
            search_quality: 显式传给搜索接口的音质（None = 普通点歌音质）。会先按本
                Key 上限收敛——后端对超出上限的显式音质直接 422，不收敛会导致搜索失败。
            send_hint: False 表示失败时不发通用提示（分享识别用自己的文案，避免
                「换个关键词试试吧」这种与分享场景对不上的提示）。
        """
        started = time.monotonic()
        tracks = None
        for attempt in (1, 2):
            try:
                tracks = await self.api.search(
                    keyword=keyword,
                    limit=limit,
                    source=source or None,
                    quality=self.api.clamp_quality(search_quality or self.cfg.default_quality),
                )
            except ApiError as e:
                duration_ms = int((time.monotonic() - started) * 1000)
                logger.error(f"[萌音点歌] 搜索失败：code={e.code} {e.message}（关键词：{keyword}）")
                if self.store:
                    await self.store.add_search_record(
                        trigger_type=trigger_type,
                        keyword=keyword,
                        source=source or "",
                        result_count=0,
                        success=0,
                        error_code=e.code,
                        duration_ms=duration_ms,
                        queue_wait_ms=queue_wait_ms,
                        **ctx,
                    )
                if send_hint:
                    await event.send(event.plain_result(e.user_hint))
                return None, duration_ms
            if tracks or attempt == 2:
                break
            # 空结果：可能命中后端音源冷却窗口，间隔后重试一次
            logger.info(f"[萌音点歌] 搜索「{keyword}」结果为空，3 秒后重试一次（可能为音源瞬时冷却）")
            await asyncio.sleep(3)

        duration_ms = int((time.monotonic() - started) * 1000)
        if self.store:
            await self.store.add_search_record(
                trigger_type=trigger_type,
                keyword=keyword,
                source=source or "",
                result_count=len(tracks),
                success=1,
                duration_ms=duration_ms,
                queue_wait_ms=queue_wait_ms,
                **ctx,
            )
        return tracks, duration_ms

    # ============ 点歌 ============

    def _file_delivery(self) -> DeliveryOptions:
        """「点歌文件」指令的发送策略：只下载文件，音质与嵌入开关独立配置。

        不做链接降级——「点歌文件」的语义就是拿到文件本体，失败时明确告知而非糊弄。
        """
        return DeliveryOptions(
            quality=self.cfg.file_quality,
            modes=list(FILE_ONLY_MODES),
            embed_metadata=self.cfg.file_embed_metadata,
            fail_hint="文件下载失败了，换一首或稍后再试试吧～",
        )

    # ============ 分享识别（QQ音乐 / 网易云 / 酷狗 / 酷我） ============

    SHARE_NOT_FOUND_HINT = "识别到分享的{label}，但没能在音源里找到，稍后再试试吧～"

    def _share_delivery(self) -> DeliveryOptions:
        """分享自动点歌的发送策略：默认语音，音质与发送方式独立配置。"""
        return DeliveryOptions(
            quality=self.cfg.share_quality,
            modes=list(self.cfg.share_send_modes),
            embed_metadata=self.cfg.embed_metadata,
            fail_hint="识别到分享，但这首歌暂时发不出来，稍后再试试吧～",
        )

    async def handle_share_request(
        self,
        event: AstrMessageEvent,
        share: ShareInfo,
        *,
        file_mode: bool = False,
        command: str = "分享识别",
    ) -> bool:
        """把识别出的分享变成歌曲：解析曲目 → 发送（默认语音 / 可选文件）。

        与点歌指令的差别：失败不展示候选列表让人选——分享是「自动说话」，
        弹一堆候选项比直接说没找到更打扰人。

        Args:
            file_mode: True（引用分享 + 「点歌文件」）时下载文件本体，音质与嵌入
                开关走 ``file_*`` 配置，与指令路径完全一致。
            command: 记录用的触发词（分享识别 / 具体的点歌指令名）。

        Returns:
            bool: 是否已成功发送。
        """
        ctx = await self._collect_context(event)
        if not await self._check_access(event, ctx):
            return False

        delivery = self._file_delivery() if file_mode else self._share_delivery()
        flow_start = time.monotonic()
        logger.info(
            f"[萌音点歌] 识别到分享：{share.platform_name}《{share.display}》"
            f"（id={share.track_id or '<无>'} 来源={share.origin} 发送={delivery.modes}）"
        )

        async def _resolve_job(queue_wait_ms=0):
            return await self._resolve_share_track(event, share, ctx, queue_wait_ms=queue_wait_ms)

        try:
            track = await self._run_task(_resolve_job)
        except asyncio.QueueFull:
            await event.send(event.plain_result(self.QUEUE_BUSY_HINT))
            return False

        if track is None:
            await event.send(
                event.plain_result(self.SHARE_NOT_FOUND_HINT.format(label=share.label))
            )
            return False

        return await self._send_via_queue(
            event,
            track,
            record_ctx={
                "trigger_type": "share",
                "command": command,
                "keyword": share.keyword or share.title or share.url,
                "selected_index": 1,
                "selection_type": "share",
                **ctx,
            },
            flow_start=flow_start,
            delivery=delivery,
        )

    async def handle_share_lyrics(self, event: AstrMessageEvent, share: ShareInfo) -> bool:
        """「引用分享 + 歌词」：解析分享曲目并发送歌词（与查歌词同款渲染）。

        Returns:
            bool: 是否成功发送歌词。
        """
        ctx = await self._collect_context(event)
        if not await self._check_access(event, ctx):
            return False
        logger.info(
            f"[萌音点歌] 识别到分享（歌词）：{share.platform_name}《{share.display}》"
            f"（id={share.track_id or '<无>'}）"
        )

        async def _resolve_job(queue_wait_ms=0):
            return await self._resolve_share_track(event, share, ctx, queue_wait_ms=queue_wait_ms)

        try:
            track = await self._run_task(_resolve_job)
        except asyncio.QueueFull:
            await event.send(event.plain_result(self.QUEUE_BUSY_HINT))
            return False
        if track is None:
            await event.send(
                event.plain_result(self.SHARE_NOT_FOUND_HINT.format(label=share.label))
            )
            return False
        return await self.send_lyrics_for_track(event, track)

    async def _resolve_share_track(
        self, event: AstrMessageEvent, share: ShareInfo, ctx: dict, queue_wait_ms: int = 0
    ) -> Track | None:
        """分享 → 曲目：先用链接里的平台原生 id 精确取详情，失败再按「歌名 歌手」搜索。"""
        if share.platform and share.track_id:
            track = await self._track_by_native_id(share)
            if track is not None:
                return track
        if not share.keyword:
            return None
        tracks, _search_ms = await self._search_with_record(
            event,
            share.keyword,
            max(3, min(10, self.cfg.song_limit)),
            share.platform,
            ctx,
            "share",
            queue_wait_ms=queue_wait_ms,
            send_hint=False,
        )
        if not tracks:
            return None
        return pick_best_track(tracks, share.title, share.singer, share.platform)

    async def _track_by_native_id(self, share: ShareInfo) -> Track | None:
        """按分享链接里的原生 id 取曲目详情（取不到返回 None，由调用方退化搜索）。"""
        music_id = f"{share.platform}:{share.track_id}"
        try:
            data = await self.api.music_info(music_id)
        except ApiError as e:
            logger.warning(
                f"[萌音点歌] 分享按 id 取详情失败，退化为搜索：code={e.code} {e.message}（{music_id}）"
            )
            return None
        except Exception:
            logger.warning(
                f"[萌音点歌] 分享按 id 取详情异常，退化为搜索：\n{traceback.format_exc()}"
            )
            return None
        if not data or not str(data.get("name") or "").strip():
            logger.info(f"[萌音点歌] 分享按 id 取不到曲目，退化为搜索：{music_id}")
            return None
        track = Track.from_api(data)
        logger.info(f"[萌音点歌] 分享已按原生 id 精确定位：{music_id}《{track.display}》")
        return track

    async def handle_song_request(
        self,
        event: AstrMessageEvent,
        keyword: str,
        source: str = "",
        index_hint: int = 0,
        command: str = "点歌",
        file_mode: bool = False,
    ) -> None:
        """处理一次点歌请求（访问检查 → 入队搜索 → 直发 / 候选列表 → 等待选号 → 入队发送）。

        Args:
            file_mode: True 表示「点歌文件」指令——流程与点歌完全一致，只是发送阶段
                       走独立的文件策略（``file_local`` + ``cfg.file_quality`` +
                       ``cfg.file_embed_metadata``），且不追加歌词图片。
        """
        delivery = self._file_delivery() if file_mode else None

        ctx = await self._collect_context(event)
        if not await self._check_access(event, ctx):
            return

        flow_start = time.monotonic()  # 端到端计时起点（含等待用户选号）
        record_base = {"trigger_type": "command", "command": command, "keyword": keyword, **ctx}

        # 搜索任务入队（排队耗时记入 search_records.queue_wait_ms）
        async def _search_job(queue_wait_ms=0):
            return await self._search_with_record(
                event,
                keyword,
                self.cfg.song_limit,
                source,
                ctx,
                "command",
                queue_wait_ms=queue_wait_ms,
                search_quality=self.cfg.file_quality if file_mode else None,
            )

        try:
            tracks, search_ms = await self._run_task(_search_job)
        except asyncio.QueueFull:
            await event.send(event.plain_result(self.QUEUE_BUSY_HINT))
            return
        record_base["search_ms"] = search_ms  # 搜索接口实际耗时（不计等待用户）

        if not tracks:
            if tracks is not None:
                logger.info(f"[萌音点歌] 搜索无结果：{keyword}")
                await event.send(event.plain_result("没有找到相关歌曲，换个关键词试试吧～"))
            return

        # 一次到位：带序号且合法，直接发送
        if 0 < index_hint <= len(tracks):
            await self._send_via_queue(
                event,
                tracks[index_hint - 1],
                record_ctx={**record_base, "selected_index": index_hint, "selection_type": "direct_index"},
                flow_start=flow_start,
                delivery=delivery,
            )
            return

        # 序号明显超出候选范围：提示一次后仍展示列表
        warned = False
        if index_hint > len(tracks):
            warned = True
            await event.send(event.plain_result(f"序号 {index_hint} 超出范围啦，请从下面的列表中重新选择～"))

        # 单曲直发
        if len(tracks) == 1:
            await self._send_via_queue(
                event,
                tracks[0],
                record_ctx={**record_base, "selected_index": 1, "selection_type": "single"},
                flow_start=flow_start,
                delivery=delivery,
            )
            return

        candidate_id = await self._send_candidate_list(event, keyword, tracks, delivery=delivery)
        if warned:
            logger.debug(f"[萌音点歌] 命令序号超范围，已引导重新选择：{index_hint}")
        await self._wait_for_selection(
            event, tracks, record_base, candidate_id, flow_start, delivery=delivery
        )

    async def _send_via_queue(
        self,
        event,
        track: Track,
        record_ctx: dict,
        flow_start: float | None = None,
        delivery: DeliveryOptions | None = None,
    ) -> bool:
        """发送任务入队（排队耗时记入 play_records.queue_wait_ms）；队满时提示繁忙。"""
        timings: dict = {}
        if flow_start is not None:
            timings["_flow_start"] = flow_start  # 端到端计时（含等待用户选号）

        async def _send_job(queue_wait_ms=0):
            timings["queue_wait_ms"] = queue_wait_ms
            return await self._send_track(
                event, track, record_ctx=record_ctx, timings=timings, delivery=delivery
            )

        try:
            return await self._run_task(_send_job)
        except asyncio.QueueFull:
            logger.warning(f"[萌音点歌] 发送任务被队列拒绝（队满）：《{track.display}》")
            await event.send(event.plain_result(self.QUEUE_BUSY_HINT))
            return False

    async def _send_candidate_list(
        self,
        event: AstrMessageEvent,
        keyword: str,
        tracks: list[Track],
        delivery: DeliveryOptions | None = None,
    ) -> int | str | None:
        """发送候选列表：图片菜单（带封面）或文本列表，图片失败自动回退文本。

        aiocqhttp 平台经协议端 call_action 发送以拿到 message_id（供选歌结束后撤回）；
        其他平台走通用发送，返回 None（无法撤回）。
        """
        diag: dict = {}
        recallable = self.cfg.recall_candidate and self._is_aiocqhttp(event)
        if not recallable:
            diag["reason"] = (
                "配置中 recall_candidate 已关闭"
                if not self.cfg.recall_candidate
                else "当前平台不支持协议端 call_action（非 OneBot）"
            )
            logger.info(f"[萌音点歌] 候选列表本次不自动撤回：{diag['reason']}")
        try:
            if self.cfg.selection_display == "image":
                try:
                    image_bytes = await self._render_candidate_image(keyword, tracks)
                except Exception:
                    logger.warning(
                        f"[萌音点歌] 候选列表图片渲染失败，回退文本：\n{traceback.format_exc()}"
                    )
                else:
                    if recallable:
                        sent, message_id = await send_message_via_onebot(
                            event,
                            [{"type": "image", "data": {"file": _bytes_to_b64(image_bytes)}}],
                            diag,
                        )
                        if sent:
                            return message_id  # 已发出：即使没拿到 id 也不能重发一遍
                    await event.send(event.chain_result([Image.fromBytes(image_bytes)]))
                    return None

            action_text = "下载文件" if delivery else "点歌"
            lines = [
                f"为【{keyword}】找到 {len(tracks)} 首，回复序号{action_text}（回复“取消”退出）："
            ]
            for i, track in enumerate(tracks, 1):
                duration = f" [{track.duration_text()}]" if track.duration_text() else ""
                lines.append(f"{i}. {track.display}（{track.source_name}）{duration}")
            text = "\n".join(lines)
            if recallable:
                sent, message_id = await send_message_via_onebot(
                    event, [{"type": "text", "data": {"text": text}}], diag
                )
                if sent:
                    return message_id
            await event.send(event.plain_result(text))
            return None
        finally:
            # 无论走哪条分支都留下结论，撤回时可直接说明「为什么撤不了」
            self._note_candidate_diag(event, diag.get("reason", "正常（可撤回）"))

    @staticmethod
    def _is_aiocqhttp(event: AstrMessageEvent) -> bool:
        """是否具备 OneBot 协议端调用能力（能 call_action，撤回/卡片都依赖它）。

        两件事都不能做：
        - 不能用 ``get_platform_name() == "aiocqhttp"``：那返回的是用户在平台配置里
          自定义的名称（可能是 napcat / qq 等），与适配器类型无关；
        - 也不能只靠 ``isinstance(event, AiocqhttpMessageEvent)``：插件以包形式加载时，
          插件 import 的 astrbot 模块与框架运行时未必是同一个模块对象，isinstance 可能
          恒为 False，且完全静默——撤回与音乐卡片会一起失效、日志里什么都看不到。

        直接探测 bot 是否提供 call_action 最可靠（astrbot-comfyui-anima 同做法）。
        """
        return call_action_of(event) is not None

    async def _recall_message(self, event: AstrMessageEvent, message_id) -> None:
        """撤回候选列表消息（仅 aiocqhttp）；失败记 warning（可能权限不足或消息已删）。

        QQ 群机器人撤回自身消息有时间窗限制（约 2 分钟），超时后撤回必然失败，属预期。
        """
        if not self.cfg.recall_candidate:
            return
        if not message_id:
            logger.warning(
                "[萌音点歌] 候选列表没有可用的 message_id，无法自动撤回"
                f"（原因：{self._candidate_diag_of(event)}）"
            )
            return
        ca = call_action_of(event)
        if ca is None:
            logger.warning("[萌音点歌] 当前平台没有 call_action（非 OneBot），无法撤回候选列表")
            return
        mid = str(message_id)
        try:
            await ca("delete_msg", message_id=message_id_payload(mid))
            logger.info(f"[萌音点歌] 已撤回候选列表消息：{mid}")
        except Exception as e:
            logger.warning(
                f"[萌音点歌] 撤回候选列表失败（消息可能已被删除/超时或协议端不支持）：{type(e).__name__}: {e}"
            )

    async def _render_candidate_image(self, keyword: str, tracks: list[Track]) -> bytes:
        """并行下载候选封面后渲染图片菜单。"""
        cover_urls = {t.id: t.cover_url for t in tracks if t.cover_url}

        async def _fetch(cover_id: str, url: str) -> tuple[str, bytes | None]:
            try:
                return cover_id, await asyncio.wait_for(self.api.download_bytes(url), timeout=6)
            except Exception:
                return cover_id, None

        results = await asyncio.gather(*(_fetch(cid, url) for cid, url in cover_urls.items()))
        covers = {cid: data for cid, data in results if data}
        logger.debug(f"[萌音点歌] 候选封面下载完成：{len(covers)}/{len(cover_urls)}")
        return await self.songlist_renderer.render_async(keyword, tracks, covers, timeout=self.cfg.timeout)

    async def _wait_for_selection(
        self,
        event: AstrMessageEvent,
        tracks: list[Track],
        record_base: dict,
        candidate_id: int | str | None = None,
        flow_start: float | None = None,
        delivery: DeliveryOptions | None = None,
    ) -> None:
        """session_waiter 等待用户回复序号；结束后撤回候选列表（选中/取消/超范围/超时）。"""

        @session_waiter(timeout=self.cfg.timeout, record_history_chains=False)
        async def song_picker(controller: SessionController, ev: AstrMessageEvent):
            text = ev.message_str.strip()
            if text in ("取消", "算了", "退出", "q", "Q"):
                controller.stop()
                ev.stop_event()
                await self._recall_message(ev, candidate_id)
                await ev.send(ev.plain_result("好的，已取消点歌～"))
                return
            if _is_song_command_text(text):
                # 等待选号期间又发起点歌：明确提示，不进任务队列，继续等待
                await ev.send(
                    ev.plain_result("您还有一单点歌在进行中，请回复序号选择，或回复「取消」后再点新的哦～")
                )
                return
            if not text.isdigit():
                # 非序号消息不响应，继续等待（不打断会话）
                return
            n = int(text)
            if not (1 <= n <= len(tracks)):
                controller.stop()
                ev.stop_event()
                await self._recall_message(ev, candidate_id)
                await ev.send(ev.plain_result("序号超出范围啦，本次点歌已结束～"))
                return
            controller.stop()
            ev.stop_event()
            await self._send_via_queue(
                ev,
                tracks[n - 1],
                record_ctx={**record_base, "selected_index": n, "selection_type": "picked"},
                flow_start=flow_start,
                delivery=delivery,
            )
            await self._recall_message(ev, candidate_id)

        try:
            await song_picker(event, _UserSessionFilter())
        except TimeoutError:
            logger.info(f"[萌音点歌] 选歌超时（{self.cfg.timeout}s）")
            await self._recall_message(event, candidate_id)
            await event.send(event.plain_result("点歌超时啦，请重新点歌～"))
        except Exception:
            logger.error(f"[萌音点歌] 选歌等待异常：\n{traceback.format_exc()}")
            await self._recall_message(event, candidate_id)
            await event.send(event.plain_result("点歌出了点小问题，请重新试试吧～"))

    # ============ 查歌词 ============

    async def handle_lyrics_request(
        self, event: AstrMessageEvent, keyword: str, command: str = "查歌词"
    ) -> bool:
        """查歌词：搜索第一首 → 取歌词 → 渲染图片发送。

        Returns:
            bool: 是否成功发送。
        """
        ctx = await self._collect_context(event)
        if not await self._check_access(event, ctx):
            return False

        async def _search_job(queue_wait_ms=0):
            tracks, _ms = await self._search_with_record(
                event, keyword, 1, "", ctx, "command", queue_wait_ms=queue_wait_ms
            )
            return tracks

        try:
            tracks = await self._run_task(_search_job)
        except asyncio.QueueFull:
            await event.send(event.plain_result(self.QUEUE_BUSY_HINT))
            return False
        if not tracks:
            if tracks is not None:
                await event.send(event.plain_result("没有找到相关歌曲，换个关键词试试吧～"))
            return False
        return await self.send_lyrics_for_track(event, tracks[0])

    async def _send_track(
        self,
        event: AstrMessageEvent,
        track: Track,
        record_ctx: dict | None = None,
        timings: dict | None = None,
        delivery: DeliveryOptions | None = None,
    ) -> bool:
        """发送歌曲的统一入口；开卡片时取链后先发信息卡片，开 enable_lyrics 时成功后追加歌词图。

        delivery 非空（「点歌文件」指令）时不再追加歌词图片——歌词已写进文件标签。
        """
        card = await self._prepare_song_card(event, track, delivery)
        sent = await self.sender.send_track(
            event, track, record_ctx=record_ctx, timings=timings, options=delivery, song_card=card
        )
        if sent:
            # 记住会话最近一次点歌：引用我们发的卡片/语音/文件时用来还原曲目
            self._remember_track(event, track)
            # 增强信息（年份/简介/热评/歌手简介）在后台补齐并只写缓存：不拖慢发歌，
            # 下次这首歌 / 这位歌手就能用上完整卡片
            self._schedule_enrich(track, getattr(event, "unified_msg_origin", None))
        if sent and delivery is None and self.cfg.enable_lyrics:
            try:
                await self.send_lyrics_for_track(event, track, quiet=True)
            except Exception:
                logger.warning(f"[萌音点歌] 附加歌词失败（不影响点歌）：\n{traceback.format_exc()}")
        return sent

    # ============ 歌曲信息卡片 ============

    def _card_allowed(self, event: AstrMessageEvent, track: Track) -> bool:
        """同一会话短期内重复点同一首歌时跳过卡片（歌照发，只防卡片刷屏）。"""
        window = int(self.cfg.song_card_repeat_sec or 0)
        if window <= 0:
            return True
        key = (str(getattr(event, "unified_msg_origin", "") or ""), track.id)
        now = time.monotonic()
        last = self._card_sent_at.get(key)
        if last is not None and now - last < window:
            logger.info(
                f"[萌音点歌] 同会话 {int(now - last)}s 内已发过该曲卡片，跳过：《{track.display}》"
            )
            return False
        # 顺手清掉过期键，避免插件长期运行时字典无限增长
        self._card_sent_at = {
            k: ts for k, ts in self._card_sent_at.items() if now - ts < window
        }
        self._card_sent_at[key] = now
        return True

    async def _prepare_song_card(
        self, event: AstrMessageEvent, track: Track, delivery: DeliveryOptions | None
    ) -> bytes | None:
        """渲染歌曲信息卡片；**只读缓存**，不联网也不调 LLM（那些在后台补齐任务里）。"""
        if not self.cfg.song_card_enable or self.card_renderer is None:
            return None
        if not self._card_allowed(event, track):
            return None
        info = await self._card_info(track)
        cover = await self._card_cover(track)
        try:
            return await self.card_renderer.render_async(
                track,
                info=info,
                cover=cover,
                quality=delivery.quality if delivery else self.cfg.default_quality,
                requester=self._card_requester(event),
                timestamp=time.strftime("%Y-%m-%d %H:%M"),
            )
        except Exception:
            logger.warning(f"[萌音点歌] 信息卡片渲染失败（跳过卡片）：\n{traceback.format_exc()}")
            return None

    async def _card_info(self, track: Track) -> CardInfo:
        """从缓存取卡片增强信息；缓存不可用/为空时返回空信息（卡片自动少画几块）。"""
        if self.info_cache is None:
            return CardInfo()
        try:
            row = await self.info_cache.get_song(track.id)
            artist = await self.info_cache.get_artist(track.singer) if track.singer else {}
        except Exception:
            logger.warning(f"[萌音点歌] 读取信息缓存失败（卡片降级为基础信息）：\n{traceback.format_exc()}")
            return CardInfo()

        def _int(value) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        return CardInfo(
            year=_int(row.get("year")),
            intro=str(row.get("intro") or ""),
            artist_bio=str(artist.get("bio") or ""),
            hot_comment=str(row.get("hot_comment") or ""),
            hot_comment_user=str(row.get("hot_comment_user") or ""),
            hot_comment_likes=_int(row.get("hot_comment_likes")),
            hot_comment_source=str(row.get("hot_comment_source") or ""),
        )

    async def _card_cover(self, track: Track) -> bytes | None:
        """取封面字节；逐级降级，全失败返回 None（卡片画占位图）。

        顺序：搜索/详情自带的 ``coverUrl`` → 缓存（补齐任务存的网易云封面）→
        后端 ``/music/:id/pic``（kw/kg/tx 有实现）→ 网易云详情实时补（wy 音源
        没有 pic 实现、详情也不带 picUrl，卡片封面只能来自这里）。
        """
        url = track.cover_url
        if not url and self.info_cache is not None:
            try:
                url = str((await self.info_cache.get_song(track.id)).get("cover_url") or "")
            except Exception:
                url = ""
        if not url:
            try:
                url = await self.api.pic(track.id) or ""
            except Exception as e:
                logger.debug(f"[萌音点歌] 后端封面接口失败：{type(e).__name__}: {e}")
                url = ""
        if not url and track.source == "wy" and self.enricher is not None:
            tried_at = None
            if self.info_cache is not None:
                tried_at = (await self.info_cache.get_song(track.id)).get("cover_at")
            if not fresh(None, tried_at):  # 近期试过仍无封面 → 该歌确实没有，7 天内不再试
                wy_id = track.id.split(":", 1)[-1]
                detail = await self.enricher.fetch_wy_detail(wy_id) or {}
                url = str(detail.get("cover") or "")
                if self.info_cache is not None:
                    # 详情顺带拿到了年份/简介就一并入库，背景补齐任务无需再拉一次
                    fields = {"cover_at": time.time()}
                    if url:
                        fields["cover_url"] = url
                    if detail.get("year"):
                        fields["year"] = detail["year"]
                    if detail.get("intro"):
                        fields["intro"] = detail["intro"]
                    await self.info_cache.upsert_song(track.id, **fields)
        if not url:
            return None
        try:
            return await self.api.download_bytes(url)
        except Exception as e:
            logger.debug(f"[萌音点歌] 封面下载失败（{url}）：{type(e).__name__}: {e}")
            return None

    @staticmethod
    def _card_requester(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_name() or "")
        except Exception:
            return ""

    # ============ 增强信息后台补齐 ============

    def _schedule_enrich(self, track: Track, umo: str | None) -> None:
        """把「补年份 / 补热评 / 补歌手简介」丢到后台任务，不阻塞发歌。"""
        if self.enricher is None or self.info_cache is None:
            return
        if not self.cfg.song_card_enable:
            return
        if not (self.cfg.song_card_year or self.cfg.song_card_comment or self.cfg.song_card_artist_bio):
            return
        try:
            task = asyncio.create_task(self._enrich_track(track, umo))
        except RuntimeError:  # 事件循环不可用（插件卸载中）
            logger.debug("[萌音点歌] 事件循环不可用，跳过信息补齐")
            return
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _enrich_track(self, track: Track, umo: str | None) -> None:
        """补齐一首歌的增强信息（任何失败都只记日志，负缓存避免反复打接口）。"""
        try:
            await self._enrich_song(track, umo)
            await self._enrich_artist(track, umo)
            await self._log_enrich_result(track)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(f"[萌音点歌] 信息补齐任务异常（已忽略）：\n{traceback.format_exc()}")

    async def _log_enrich_result(self, track: Track) -> None:
        """补齐结果一行说清（排查「卡片上怎么没有热评/简介」看这里就够）。"""
        if self.info_cache is None:
            return
        row = await self.info_cache.get_song(track.id)
        artist = await self.info_cache.get_artist(track.singer) if track.singer else {}
        comment = "无"
        if row.get("hot_comment"):
            comment = f"有({row.get('hot_comment_source') or 'wy'})"
        logger.info(
            f"[萌音点歌] 信息补齐完成：《{track.display}》"
            f" 年份={row.get('year') or '无'}"
            f" 简介={'有' if row.get('intro') else '无'}"
            f" 热评={comment}"
            f" 歌手简介={'有' if artist.get('bio') else '无'}"
        )

    async def _enrich_song(self, track: Track, umo: str | None = None) -> None:
        """补网易云映射 id / 发行年份 / 歌曲简介 / 热评。"""
        need_year = self.cfg.song_card_year
        need_intro = self.cfg.song_card_intro
        need_comment = self.cfg.song_card_comment
        if not (need_year or need_intro or need_comment):
            return
        row = await self.info_cache.get_song(track.id)
        fields: dict = {"info_at": time.time()}

        # 年份 + 歌曲简介 + 封面：一次网易云详情调用同时拿（简介缺 album 文案时 LLM 兜底）
        # 注意：详情调用只由年份/简介的新鲜度驱动——封面缺失本身不允许绕过负缓存
        # 反复拉接口（封面有独立的 cover_at 负缓存，见 _card_cover）。
        wy_detail: dict = {}
        if need_year or need_intro:
            wy_id = str(row.get("wy_id") or "")
            if not fresh(wy_id, row.get("mapped_at")):
                wy_id = await self.enricher.resolve_wy_id(track) or ""
                await self.info_cache.upsert_song(track.id, wy_id=wy_id, mapped_at=time.time())
            detail_needed = not fresh(
                row.get("year"), row.get("info_at")
            ) or not fresh(row.get("intro"), row.get("info_at"))
            if wy_id and detail_needed:
                wy_detail = await self.enricher.fetch_wy_detail(wy_id) or {}
            if wy_detail.get("cover"):
                fields["cover_url"] = wy_detail["cover"]
            if need_year and not fresh(row.get("year"), row.get("info_at")) and wy_detail.get("year"):
                fields["year"] = wy_detail["year"]
            if need_intro and not fresh(row.get("intro"), row.get("info_at")):
                intro = wy_detail.get("intro")
                if not intro:
                    intro = await self.enricher.fetch_song_intro(track, umo)
                if intro:
                    fields["intro"] = intro

        # 热评按曲目所在平台直取（wy/kw/kg），其余平台回退网易云同曲映射
        if need_comment and not fresh(row.get("hot_comment"), row.get("info_at")):
            comment, source = await self.enricher.fetch_hot_comment_for_track(track)
            if comment:
                fields["hot_comment"] = comment["text"]
                fields["hot_comment_user"] = comment["user"]
                fields["hot_comment_likes"] = comment["likes"]
                fields["hot_comment_source"] = source
        await self.info_cache.upsert_song(track.id, **fields)

    # ============ 会话最近点歌记忆（引用卡片/语音还原曲目） ============

    CARD_QUOTE_TTL = 600
    """引用我们发的卡片 / 语音 / 文件时，能用会话最近一次点歌还原曲目的时间窗（秒）。"""

    def _remember_track(self, event: AstrMessageEvent, track: Track) -> None:
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            return
        if not umo:
            return
        now = time.monotonic()
        self._recent_tracks[umo] = (track, now)
        expired = [k for k, (_t, ts) in self._recent_tracks.items() if now - ts > self.CARD_QUOTE_TTL * 4]
        for key in expired:
            self._recent_tracks.pop(key, None)

    def last_track(self, event: AstrMessageEvent) -> Track | None:
        """会话内最近一次点歌的曲目；超过 ``CARD_QUOTE_TTL`` 或没有记录返回 None。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            return None
        entry = self._recent_tracks.get(umo)
        if not entry:
            return None
        track, ts = entry
        if time.monotonic() - ts > self.CARD_QUOTE_TTL:
            return None
        return track

    async def handle_track_request(
        self,
        event: AstrMessageEvent,
        track: Track,
        *,
        file_mode: bool = False,
        lyrics: bool = False,
        command: str = "",
    ) -> bool:
        """对已解析出的曲目执行发送 / 下载 / 查歌词（引用我们发的卡片等场景）。"""
        ctx = await self._collect_context(event)
        if not await self._check_access(event, ctx):
            return False
        if lyrics:
            return await self.send_lyrics_for_track(event, track)
        delivery = self._file_delivery() if file_mode else self._share_delivery()
        return await self._send_via_queue(
            event,
            track,
            record_ctx={
                "trigger_type": "share",
                "command": command,
                "keyword": track.display,
                "selected_index": 1,
                "selection_type": "share",
                **ctx,
            },
            delivery=delivery,
        )

    async def _enrich_artist(self, track: Track, umo: str | None) -> None:
        """补歌手简介（LLM，歌手维度复用：同一位歌手只生成一次）。"""
        if not self.cfg.song_card_artist_bio or not track.singer:
            return
        row = await self.info_cache.get_artist(track.singer)
        if fresh(row.get("bio"), row.get("attempt_at")):
            return
        bio = await self.enricher.fetch_artist_bio(track.singer, umo)
        fields: dict = {"attempt_at": time.time()}
        if bio:
            fields["bio"] = bio
            fields["source"] = "llm"
        await self.info_cache.upsert_artist(track.singer, **fields)
        if bio:
            logger.info(f"[萌音点歌] 已缓存歌手简介：{track.singer}")

    async def shutdown_background(self) -> None:
        """取消后台信息补齐任务（插件卸载时调用）。"""
        tasks = list(self._bg_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_tasks.clear()

    async def send_lyrics_for_track(self, event: AstrMessageEvent, track: Track, quiet: bool = False) -> bool:
        """取歌词并渲染发送；渲染失败回退纯文本。

        Args:
            quiet: 静默模式（点歌后附加歌词场景）：取词失败 / 歌词为空 / 发送失败
                   只记日志不发提示，避免打扰用户。

        Returns:
            bool: 是否成功发送。
        """
        try:
            lyric_data = await self.api.lyric(track.id)
        except ApiError as e:
            logger.error(f"[萌音点歌] 获取歌词失败：code={e.code} {e.message}（{track.id}）")
            if not quiet:
                await event.send(event.plain_result(e.user_hint))
            return False

        lyric_text = (lyric_data or {}).get("lyric", "") or ""
        if not lyric_text.strip():
            logger.info(f"[萌音点歌] 歌词为空：{track.id}")
            if not quiet:
                await event.send(event.plain_result("这首歌暂无歌词～"))
            return False

        try:
            image_bytes = await self.lyrics_renderer.render_async(
                lyric_text, title=track.name, subtitle=track.display
            )
        except Exception:
            logger.error(f"[萌音点歌] 歌词渲染失败，回退文本：\n{traceback.format_exc()}")
            await event.send(
                event.plain_result(f"《{track.name}》 {track.singer}\n\n{self._plain_lyrics(lyric_text)}")
            )
            return True

        sent = await send_lyrics_image(event, image_bytes)
        if not sent and not quiet:
            await event.send(
                event.plain_result(f"《{track.name}》 {track.singer}\n\n{self._plain_lyrics(lyric_text)}")
            )
            return True
        return sent

    @staticmethod
    def _plain_lyrics(lyrics: str) -> str:
        """歌词纯文本兜底（去时间轴、去元信息）。"""
        from .lyrics_render import LyricsRenderer

        return "\n".join(LyricsRenderer.clean_lrc(lyrics)).strip()

    # ============ 自检 ============

    async def handle_self_test(self, event: AstrMessageEvent) -> None:
        """点歌自检：调 /me 回显 Key 限额信息（诊断命令，不受名单限制）。"""
        try:
            data = await self.api.me()
        except ApiError as e:
            logger.error(f"[萌音点歌] 自检失败：code={e.code} {e.message}")
            await event.send(event.plain_result(e.user_hint))
            return

        key_info = data.get("apiKey") or {}
        if key_info.get("status") and key_info.get("status") != "active":
            await event.send(event.plain_result("音乐服务配置有误，请联系管理员～"))
            return

        lines = ["🎵 音乐服务连接正常"]
        if key_info.get("name"):
            lines.append(f"Key 名称：{key_info['name']}")
        if self.api.key_max_quality:
            lines.append(f"最高音质：{self.api.key_max_quality}")
        if self.api.key_qps_limit:
            lines.append(f"频率上限：{self.api.key_qps_limit}/s")
        sources = key_info.get("allowedSources")
        if sources:
            lines.append(f"允许音源：{'、'.join(sources)}")
        elif key_info.get("defaultSource"):
            lines.append(f"默认音源：{key_info['defaultSource']}")
        if self.store:
            try:
                search_n, play_n = await self.store.counts()
                lines.append(f"已累计记录：搜索 {search_n} 次 / 点歌 {play_n} 首")
                lines.append(f"记录库：{self.store.db_path}")
            except Exception:
                pass
        if self.info_cache:
            try:
                song_n, artist_n = await self.info_cache.stats()
                lines.append(f"信息卡片：{'开启' if self.cfg.song_card_enable else '关闭'}")
                lines.append(f"信息缓存：曲目 {song_n} 条 / 歌手 {artist_n} 条")
            except Exception:
                pass
        if self.queue:
            lines.append(
                f"任务队列：并发 {self.queue._concurrency}，等待中 {self.queue.pending}，"
                f"累计提交 {self.queue.submitted}，累计拒绝 {self.queue.rejected}"
            )
        lines.append("服务地址可达 ✓")
        await event.send(event.plain_result("\n".join(lines)))
        logger.info(f"[萌音点歌] 自检成功：{key_info.get('name', '<unnamed>')}")

    # ============ LLM Tool 共用 ============

    async def _llm_search(
        self, event, keyword: str, source: str, ctx: dict
    ) -> tuple[list[Track] | None, int] | str:
        """LLM 搜索任务入队；返回 (tracks, search_ms) / None（失败已提示）/ 'busy'（队满）。"""

        async def _job(queue_wait_ms=0):
            return await self._search_with_record(
                event, keyword, 1, source, ctx, "llm_tool", queue_wait_ms=queue_wait_ms
            )

        try:
            return await self._run_task(_job)
        except asyncio.QueueFull:
            return "busy"

    async def llm_play_song(self, event: AstrMessageEvent, song_name: str, source: str = "") -> str:
        """LLM Tool：按歌名点歌并播放（发送第一首）。返回给 LLM 的结果文本。"""
        ctx = await self._collect_context(event)
        if not await self._check_access(event, ctx):
            return "用户没有点歌权限或当前会话未开放点歌"

        flow_start = time.monotonic()
        search_result = await self._llm_search(event, song_name, source, ctx)
        if search_result == "busy":
            return "当前点歌人数较多，请稍后再试"
        tracks, search_ms = search_result
        if tracks is None:
            return "点歌失败：音乐服务暂时不可用"
        if not tracks:
            return f"没有找到《{song_name}》相关的歌曲"
        track = tracks[0]
        timings: dict = {"_flow_start": flow_start}

        async def _send_job(queue_wait_ms=0):
            timings["queue_wait_ms"] = queue_wait_ms
            return await self._send_track(
                event,
                track,
                record_ctx={
                    "trigger_type": "llm_tool",
                    "command": "play_song_by_name",
                    "keyword": song_name,
                    "selected_index": 1,
                    "selection_type": "llm",
                    "search_ms": search_ms,
                    **ctx,
                },
                timings=timings,
            )

        try:
            sent = await self._run_task(_send_job)
        except asyncio.QueueFull:
            return "当前点歌人数较多，请稍后再试"
        if not sent:
            return "歌曲发送失败，请稍后再试"
        return f"已为用户播放《{track.name}》- {track.singer}（{track.source_name}）"

    async def llm_query_lyrics(self, event: AstrMessageEvent, song_name: str) -> str:
        """LLM Tool：按歌名查询并发送歌词。返回给 LLM 的结果文本。"""
        ctx = await self._collect_context(event)
        if not await self._check_access(event, ctx):
            return "用户没有点歌权限或当前会话未开放点歌"

        search_result = await self._llm_search(event, song_name, "", ctx)
        if search_result == "busy":
            return "当前点歌人数较多，请稍后再试"
        tracks, _search_ms = search_result
        if tracks is None:
            return "歌词查询失败：音乐服务暂时不可用"
        if not tracks:
            return f"没有找到《{song_name}》相关的歌曲"
        sent = await self.send_lyrics_for_track(event, tracks[0])
        if not sent:
            return "歌词获取或发送失败"
        return f"已为用户展示《{tracks[0].name}》- {tracks[0].singer} 的歌词"

    @staticmethod
    def resolve_command_source(cmd: str) -> str | None:
        """命令别名 → 平台码；非点歌命令返回 None。"""
        return COMMAND_SOURCE_ALIAS.get(cmd.strip().lower())
