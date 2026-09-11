"""点歌业务逻辑：点歌 / 查歌词 / 自检 / LLM Tool 的共用实现。

命令 handler（main.py）只做参数解析与转发；本模块负责访问控制、与后端交互、
候选列表展示、session_waiter 选歌等待、歌词渲染、记录持久化与异常兜底。
用户侧只输出温馨提示，技术细节全部进日志。
"""

import asyncio
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
from .lyrics_render import LyricsRenderer
from .model import Track
from .sender import SongSender, send_lyrics_image
from .songlist_render import SonglistRenderer
from .storage import RecordStore


class _UserSessionFilter(SessionFilter):
    """会话维度隔离：统一消息来源 + 发送者 ID。"""

    def filter(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


class MoeMusicService:
    """点歌业务服务。"""

    def __init__(
        self,
        config: PluginConfig,
        api: MusicApiClient,
        sender: SongSender,
        lyrics_renderer: LyricsRenderer,
        songlist_renderer: SonglistRenderer,
        store: RecordStore | None = None,
        access: AccessController | None = None,
    ):
        self.cfg = config
        self.api = api
        self.sender = sender
        self.lyrics_renderer = lyrics_renderer
        self.songlist_renderer = songlist_renderer
        self.store = store
        self.access = access

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
    ) -> list[Track] | None:
        """搜索 + 写搜索记录（成败均记）；失败时发送提示并返回 None。"""
        started = time.monotonic()
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
                    **ctx,
                )
            await event.send(event.plain_result(e.user_hint))
            return None

        if self.store:
            await self.store.add_search_record(
                trigger_type=trigger_type,
                keyword=keyword,
                source=source or "",
                result_count=len(tracks),
                success=1,
                duration_ms=int((time.monotonic() - started) * 1000),
                **ctx,
            )
        return tracks

    # ============ 点歌 ============

    async def handle_song_request(
        self,
        event: AstrMessageEvent,
        keyword: str,
        source: str = "",
        index_hint: int = 0,
        command: str = "点歌",
    ) -> None:
        """处理一次点歌请求（访问检查 → 搜索 → 直发 / 候选列表 → 等待选号 → 发送）。"""
        ctx = await self._collect_context(event)
        if not await self._check_access(event, ctx):
            return

        tracks = await self._search_with_record(event, keyword, self.cfg.song_limit, source, ctx, "command")
        if not tracks:
            if tracks is not None:
                logger.info(f"[萌音点歌] 搜索无结果：{keyword}")
                await event.send(event.plain_result("没有找到相关歌曲，换个关键词试试吧～"))
            return

        record_base = {"trigger_type": "command", "command": command, "keyword": keyword, **ctx}

        # 一次到位：带序号且合法，直接发送
        if 0 < index_hint <= len(tracks):
            await self.sender.send_track(
                event,
                tracks[index_hint - 1],
                record_ctx={**record_base, "selected_index": index_hint, "selection_type": "direct_index"},
            )
            return

        # 序号明显超出候选范围：提示一次后仍展示列表
        warned = False
        if index_hint > len(tracks):
            warned = True
            await event.send(event.plain_result(f"序号 {index_hint} 超出范围啦，请从下面的列表中重新选择～"))

        # 单曲直发
        if len(tracks) == 1:
            await self.sender.send_track(
                event, tracks[0], record_ctx={**record_base, "selected_index": 1, "selection_type": "single"}
            )
            return

        await self._send_candidate_list(event, keyword, tracks)
        if warned:
            logger.debug(f"[萌音点歌] 命令序号超范围，已引导重新选择：{index_hint}")
        await self._wait_for_selection(event, tracks, record_base)

    async def _send_candidate_list(self, event: AstrMessageEvent, keyword: str, tracks: list[Track]) -> None:
        """发送候选列表：图片菜单（带封面）或文本列表，图片失败自动回退文本。"""
        if self.cfg.selection_display == "image":
            try:
                image_bytes = await self._render_candidate_image(keyword, tracks)
                await event.send(event.chain_result([Image.fromBytes(image_bytes)]))
                return
            except Exception:
                logger.warning(f"[萌音点歌] 候选列表图片渲染失败，回退文本：\n{traceback.format_exc()}")

        lines = [f"为【{keyword}】找到 {len(tracks)} 首，回复序号点歌（回复“取消”退出）："]
        for i, track in enumerate(tracks, 1):
            duration = f" [{track.duration_text()}]" if track.duration_text() else ""
            lines.append(f"{i}. {track.display}（{track.source_name}）{duration}")
        await event.send(event.plain_result("\n".join(lines)))

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
        self, event: AstrMessageEvent, tracks: list[Track], record_base: dict
    ) -> None:
        """session_waiter 等待用户回复序号。"""

        @session_waiter(timeout=self.cfg.timeout, record_history_chains=False)
        async def song_picker(controller: SessionController, ev: AstrMessageEvent):
            text = ev.message_str.strip()
            if text in ("取消", "算了", "退出", "q", "Q"):
                controller.stop()
                ev.stop_event()
                await ev.send(ev.plain_result("好的，已取消点歌～"))
                return
            if not text.isdigit():
                # 非序号消息不响应，继续等待（不打断会话）
                return
            n = int(text)
            if not (1 <= n <= len(tracks)):
                controller.stop()
                ev.stop_event()
                await ev.send(ev.plain_result("序号超出范围啦，本次点歌已结束～"))
                return
            controller.stop()
            ev.stop_event()
            await self.sender.send_track(
                ev, tracks[n - 1], record_ctx={**record_base, "selected_index": n, "selection_type": "picked"}
            )

        try:
            await song_picker(event, _UserSessionFilter())
        except TimeoutError:
            logger.info(f"[萌音点歌] 选歌超时（{self.cfg.timeout}s）")
            await event.send(event.plain_result("点歌超时啦，请重新点歌～"))
        except Exception:
            logger.error(f"[萌音点歌] 选歌等待异常：\n{traceback.format_exc()}")
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

        tracks = await self._search_with_record(event, keyword, 1, "", ctx, "command")
        if not tracks:
            if tracks is not None:
                await event.send(event.plain_result("没有找到相关歌曲，换个关键词试试吧～"))
            return False
        return await self.send_lyrics_for_track(event, tracks[0])

    async def send_lyrics_for_track(self, event: AstrMessageEvent, track: Track) -> bool:
        """取歌词并渲染发送；渲染失败回退纯文本。"""
        try:
            lyric_data = await self.api.lyric(track.id)
        except ApiError as e:
            logger.error(f"[萌音点歌] 获取歌词失败：code={e.code} {e.message}（{track.id}）")
            await event.send(event.plain_result(e.user_hint))
            return False

        lyric_text = (lyric_data or {}).get("lyric", "") or ""
        if not lyric_text.strip():
            logger.info(f"[萌音点歌] 歌词为空：{track.id}")
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
        if not sent:
            await event.send(
                event.plain_result(f"《{track.name}》 {track.singer}\n\n{self._plain_lyrics(lyric_text)}")
            )
            return True
        return True

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
            except Exception:
                pass
        lines.append("服务地址可达 ✓")
        await event.send(event.plain_result("\n".join(lines)))
        logger.info(f"[萌音点歌] 自检成功：{key_info.get('name', '<unnamed>')}")

    # ============ LLM Tool 共用 ============

    async def llm_play_song(self, event: AstrMessageEvent, song_name: str, source: str = "") -> str:
        """LLM Tool：按歌名点歌并播放（发送第一首）。返回给 LLM 的结果文本。"""
        ctx = await self._collect_context(event)
        if not await self._check_access(event, ctx):
            return "用户没有点歌权限或当前会话未开放点歌"

        tracks = await self._search_with_record(event, song_name, 1, source, ctx, "llm_tool")
        if tracks is None:
            return "点歌失败：音乐服务暂时不可用"
        if not tracks:
            return f"没有找到《{song_name}》相关的歌曲"
        track = tracks[0]
        sent = await self.sender.send_track(
            event,
            track,
            record_ctx={
                "trigger_type": "llm_tool",
                "command": "play_song_by_name",
                "keyword": song_name,
                "selected_index": 1,
                "selection_type": "llm",
                **ctx,
            },
        )
        if not sent:
            return "歌曲发送失败，请稍后再试"
        return f"已为用户播放《{track.name}》- {track.singer}（{track.source_name}）"

    async def llm_query_lyrics(self, event: AstrMessageEvent, song_name: str) -> str:
        """LLM Tool：按歌名查询并发送歌词。返回给 LLM 的结果文本。"""
        ctx = await self._collect_context(event)
        if not await self._check_access(event, ctx):
            return "用户没有点歌权限或当前会话未开放点歌"

        tracks = await self._search_with_record(event, song_name, 1, "", ctx, "llm_tool")
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
