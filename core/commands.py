"""点歌业务逻辑：点歌 / 查歌词 / 自检 / LLM Tool 的共用实现。

命令 handler（main.py）只做参数解析与转发；本模块负责访问控制、与后端交互、
候选列表展示、session_waiter 选歌等待、歌词渲染、记录持久化与异常兜底。
用户侧只输出温馨提示，技术细节全部进日志。
"""

import asyncio
import base64
import time
import traceback

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)

from .access import AccessController
from .api_client import ApiError, MusicApiClient
from .config import COMMAND_SOURCE_ALIAS, PluginConfig
from .lyrics_render import LyricsRenderer
from .model import Track
from .sender import SongSender, send_lyrics_image
from .songlist_render import SonglistRenderer
from .storage import RecordStore


class _UserSessionFilter(SessionFilter):
    """会话维度隔离：统一消息来源 + 发送者 ID。"""

    def filter(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


def _is_song_command_text(text: str) -> bool:
    """判断一条消息是否是新的点歌命令（用于等待选号时的重复点歌提示）。"""
    cmd = text.strip().partition(" ")[0].strip().lower()
    return cmd in COMMAND_SOURCE_ALIAS


def _bytes_to_b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


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
    ):
        self.cfg = config
        self.api = api
        self.sender = sender
        self.lyrics_renderer = lyrics_renderer
        self.songlist_renderer = songlist_renderer
        self.store = store
        self.access = access
        self.queue = queue  # SongTaskQueue；None 表示直通执行（测试/降级）

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
    ) -> list[Track] | None:
        """搜索 + 写搜索记录（成败均记）；失败时发送提示并返回 None。

        后端音源被连续请求短暂冷却时会返回空结果（实测存在），因此空结果
        间隔 3 秒自动重试一次，仍为空才判定无结果。
        """
        started = time.monotonic()
        tracks = None
        for attempt in (1, 2):
            try:
                tracks = await self.api.search(
                    keyword=keyword, limit=limit, source=source or None, quality=self.cfg.default_quality
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

    async def handle_song_request(
        self,
        event: AstrMessageEvent,
        keyword: str,
        source: str = "",
        index_hint: int = 0,
        command: str = "点歌",
    ) -> None:
        """处理一次点歌请求（访问检查 → 入队搜索 → 直发 / 候选列表 → 等待选号 → 入队发送）。"""
        ctx = await self._collect_context(event)
        if not await self._check_access(event, ctx):
            return

        flow_start = time.monotonic()  # 端到端计时起点（含等待用户选号）
        record_base = {"trigger_type": "command", "command": command, "keyword": keyword, **ctx}

        # 搜索任务入队（排队耗时记入 search_records.queue_wait_ms）
        async def _search_job(queue_wait_ms=0):
            return await self._search_with_record(
                event, keyword, self.cfg.song_limit, source, ctx, "command", queue_wait_ms=queue_wait_ms
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
            )
            return

        candidate_id = await self._send_candidate_list(event, keyword, tracks)
        if warned:
            logger.debug(f"[萌音点歌] 命令序号超范围，已引导重新选择：{index_hint}")
        await self._wait_for_selection(event, tracks, record_base, candidate_id, flow_start)

    async def _send_via_queue(
        self, event, track: Track, record_ctx: dict, flow_start: float | None = None
    ) -> bool:
        """发送任务入队（排队耗时记入 play_records.queue_wait_ms）；队满时提示繁忙。"""
        timings: dict = {}
        if flow_start is not None:
            timings["_flow_start"] = flow_start  # 端到端计时（含等待用户选号）

        async def _send_job(queue_wait_ms=0):
            timings["queue_wait_ms"] = queue_wait_ms
            return await self._send_track(event, track, record_ctx=record_ctx, timings=timings)

        try:
            return await self._run_task(_send_job)
        except asyncio.QueueFull:
            logger.warning(f"[萌音点歌] 发送任务被队列拒绝（队满）：《{track.display}》")
            await event.send(event.plain_result(self.QUEUE_BUSY_HINT))
            return False

    async def _send_candidate_list(
        self, event: AstrMessageEvent, keyword: str, tracks: list[Track]
    ) -> int | None:
        """发送候选列表：图片菜单（带封面）或文本列表，图片失败自动回退文本。

        aiocqhttp 平台经 call_action 发送以拿到 message_id（供选歌结束后撤回）；
        其他平台走通用发送，返回 None（无法撤回）。
        """
        recallable = self.cfg.recall_candidate and self._is_aiocqhttp(event)
        if self.cfg.selection_display == "image":
            try:
                image_bytes = await self._render_candidate_image(keyword, tracks)
            except Exception:
                logger.warning(f"[萌音点歌] 候选列表图片渲染失败，回退文本：\n{traceback.format_exc()}")
            else:
                if recallable:
                    message_id = await self._send_onebot_message(
                        event, [{"type": "image", "data": {"file": _bytes_to_b64(image_bytes)}}]
                    )
                    if message_id:
                        return int(message_id)
                await event.send(event.chain_result([Image.fromBytes(image_bytes)]))
                return None

        lines = [f"为【{keyword}】找到 {len(tracks)} 首，回复序号点歌（回复“取消”退出）："]
        for i, track in enumerate(tracks, 1):
            duration = f" [{track.duration_text()}]" if track.duration_text() else ""
            lines.append(f"{i}. {track.display}（{track.source_name}）{duration}")
        text = "\n".join(lines)
        if recallable:
            message_id = await self._send_onebot_message(event, [{"type": "text", "data": {"text": text}}])
            if message_id:
                return int(message_id)
        await event.send(event.plain_result(text))
        return None

    @staticmethod
    def _is_aiocqhttp(event: AstrMessageEvent) -> bool:
        """是否为 OneBot(aiocqhttp) 平台且具备 bot 客户端（可 call_action）。

        注意不能用 get_platform_name() == "aiocqhttp" 判断：那返回的是用户在
        平台配置里自定义的名称（可能是 napcat / qq 等），与适配器类型无关。
        """
        return isinstance(event, AiocqhttpMessageEvent) and getattr(event, "bot", None) is not None

    async def _send_onebot_message(self, event: AstrMessageEvent, message: list[dict]) -> int | None:
        """经 OneBot 直接发送消息段，返回 message_id；失败返回 None（调用方降级通用发送）。"""
        try:
            bot = event.bot
            if event.is_private_chat():
                result = await bot.api.call_action(
                    "send_private_msg", user_id=event.get_sender_id(), message=message
                )
            else:
                result = await bot.api.call_action(
                    "send_group_msg", group_id=event.get_group_id(), message=message
                )
            return result.get("message_id") if isinstance(result, dict) else None
        except Exception as e:
            logger.warning(f"[萌音点歌] OneBot 直发失败，降级通用发送：{type(e).__name__}: {e}")
            return None

    async def _recall_message(self, event: AstrMessageEvent, message_id: int | None) -> None:
        """撤回候选列表消息（仅 aiocqhttp）；失败记 warning（可能权限不足或消息已删）。"""
        if not message_id or not self.cfg.recall_candidate or not self._is_aiocqhttp(event):
            return
        try:
            await event.bot.api.call_action("delete_msg", message_id=message_id)
            logger.debug(f"[萌音点歌] 已撤回候选列表消息：{message_id}")
        except Exception as e:
            logger.warning(
                f"[萌音点歌] 撤回候选列表失败（消息可能已被删除或协议端不支持）：{type(e).__name__}: {e}"
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
        candidate_id: int | None = None,
        flow_start: float | None = None,
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
    ) -> bool:
        """发送歌曲的统一入口；开启 enable_lyrics 时成功后静默追加歌词图片。"""
        sent = await self.sender.send_track(event, track, record_ctx=record_ctx, timings=timings)
        if sent and self.cfg.enable_lyrics:
            try:
                await self.send_lyrics_for_track(event, track, quiet=True)
            except Exception:
                logger.warning(f"[萌音点歌] 附加歌词失败（不影响点歌）：\n{traceback.format_exc()}")
        return sent

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
