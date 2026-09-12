"""歌曲发送策略与降级。

按配置的 ``send_modes`` 顺序尝试多种发送方式，任一成功即止：
- ``card``：OneBot(aiocqhttp) 音乐卡片（custom 类型），体验最佳；
- ``record_link`` / ``record_local``：语音链接 / 本地语音；
- ``file_link`` / ``file_local``：文件链接 / 本地文件；
- ``text``：纯文本临时链接兜底。

「点歌文件」指令走独立的 :class:`DeliveryOptions`（固定只用 ``file_local``、独立音质与
嵌入开关），与普通点歌的配置互不影响。
"""

import asyncio
import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import File, Image, Plain, Record

from .api_client import ApiError, MusicApiClient, guess_audio_ext
from .config import PluginConfig
from .metadata import embed_metadata
from .model import Track, quality_rank
from .onebot import call_action_of, to_onebot_id

# 非法文件名字符
_FILENAME_STRIP = re.compile(r'[\\/:*?"<>|\r\n\t]')

# 「点歌文件」指令固定只用本地文件：下载后发文件，失败即提示，不降级为链接
FILE_ONLY_MODES = ["file_local"]


def _safe_filename(name: str) -> str:
    return _FILENAME_STRIP.sub("_", name).strip("_ ") or "audio"


def _log_quality_result(track: Track, wanted: str, requested: str, audio: dict) -> None:
    """记录音质完整链路：期望 → 实际请求 → 后端实得（含来源）。

    排查「配了音质却没生效」时靠这一条日志即可区分两类原因：
    1. 期望超出本 Key 上限：wanted ≠ requested（被收敛后才请求）；
    2. 后端命中本地曲库：实得的是曲库里已有文件的音质，与请求可能不一致。
    """
    got = str(audio.get("quality") or "") or "未知"
    origin = "本地曲库" if audio.get("fromLocal") else "上游取链"
    if requested != wanted:
        logger.warning(
            f"[萌音点歌] 音质 {wanted} 超出本 Key 允许范围，已收敛为 {requested}：《{track.display}》"
        )
    if got != "未知" and got != requested:
        logger.warning(
            f"[萌音点歌] 后端实得音质 {got} 与请求 {requested} 不一致（{origin}）：《{track.display}》"
        )
    logger.info(
        f"[萌音点歌] 音质结果：期望 {wanted} → 请求 {requested} → 实得 {got}（{origin}）：《{track.display}》"
    )


@dataclass(slots=True)
class DeliveryOptions:
    """一次发送的策略：普通点歌与「点歌文件」指令的差异都收敛在这里。

    ``None``（调用方不传）表示走 PluginConfig 的默认策略，即普通点歌，行为与历史一致。
    """

    quality: str
    """期望音质（解析播放链接时使用；超过 Key 上限会自动收敛到允许的最高音质）。"""

    modes: list[str] = field(default_factory=list)
    """发送方式与降级顺序。"""

    embed_metadata: bool = True
    """本地文件发送时是否写入标题/歌手/专辑/封面/歌词标签。"""

    fail_hint: str = "这首歌暂时发不出来，换一首试试吧～"
    """所有发送方式都失败时给用户的提示（文件指令用自己的文案）。"""


class SongSender:
    """把一首 Track 以「卡片→语音→文件→文本」的顺序发送到聊天。"""

    def __init__(self, config: PluginConfig, api: MusicApiClient, download_dir: Path, store=None):
        self.cfg = config
        self.api = api
        self.download_dir = download_dir
        self.store = store

    # ============ 对外入口 ============

    async def send_track(
        self,
        event,
        track: Track,
        record_ctx: dict | None = None,
        timings: dict | None = None,
        options: DeliveryOptions | None = None,
    ) -> bool:
        """发送一首歌：解析播放链接（含音质收敛）→ 按发送方式降级发送。

        Args:
            record_ctx: 点歌记录上下文（用户/会话/选歌方式等），发送成功后
                        补齐歌曲与发送细节写入 play_records；None 表示不记录。
            timings: 可变字典，写入本次发送各阶段耗时（毫秒）与嵌入结果，
                     供 play_records 统计：resolve_ms / download_ms / embed_ms /
                     send_ms / queue_wait_ms / total_ms / metadata_embedded。
            options: 发送策略覆盖（音质 / 发送方式 / 是否嵌入元数据）；
                     None = 使用插件配置的默认策略（普通点歌）。

        Returns:
            bool: 是否发送成功。
        """
        timings = timings if timings is not None else {}
        # 端到端起点：调用方（service）在点歌命令进入时记录；未提供则以发送任务开始计
        t_start = float(timings.pop("_flow_start", 0.0)) or time.monotonic()

        wanted_quality = options.quality if options else self.cfg.default_quality
        modes = options.modes if options else self.cfg.send_modes
        embed = options.embed_metadata if options else self.cfg.embed_metadata

        try:
            t0 = time.monotonic()
            audio = await self.resolve_play_url(track, wanted=wanted_quality)
            timings["resolve_ms"] = int((time.monotonic() - t0) * 1000)
        except ApiError as e:
            logger.error(f"[萌音点歌] 获取播放链接失败：code={e.code} {e.message}（{track.id}）")
            await event.send(event.plain_result(e.user_hint))
            timings["total_ms"] = int((time.monotonic() - t_start) * 1000)
            return False

        cover_url = ""
        # 卡片（展示封面）/ 开启元数据嵌入的本地文件模式 才需要封面；失败不影响发送主流程
        need_cover = "card" in modes or (embed and "file_local" in modes)
        if need_cover:
            try:
                cover_url = await self.api.pic(track.id) or ""
            except ApiError as e:
                logger.warning(f"[萌音点歌] 获取封面失败：code={e.code}（{track.id}）")

        audio_url_raw = audio.get("url", "")
        quality = str(audio.get("quality", wanted_quality))
        # 发给用户的链接用对外可达地址（publicize）；插件自己下载仍走后端原地址（内网更快）
        audio_url = self.api.publicize(audio_url_raw)
        timings["quality_requested"] = wanted_quality
        timings["quality_fallback"] = int(quality != wanted_quality)
        timings["expires_at"] = str(audio.get("expiresAt") or "") or None

        for mode in modes:
            t0 = time.monotonic()
            try:
                sent = await self._dispatch(
                    event,
                    mode,
                    track,
                    audio_url,
                    audio_url_raw,
                    quality,
                    cover_url,
                    timings,
                    embed_metadata=embed,
                )
            except Exception:
                logger.error(f"[萌音点歌] 发送模式 {mode} 未捕获异常：\n{traceback.format_exc()}")
                sent = False
            timings["send_ms"] = int((time.monotonic() - t0) * 1000)
            if sent:
                logger.info(f"[萌音点歌] 已通过 {mode} 发送歌曲《{track.display}》")
                _finalize_timings(timings, t_start)
                if record_ctx is not None and self.store:
                    await self.store.add_play_record(
                        **timings,
                        **record_ctx,
                        track_id=track.id,
                        track_name=track.name,
                        singer=track.singer,
                        album=track.album,
                        source=track.source,
                        duration=track.duration,
                        quality=quality,
                        send_mode=mode,
                    )
                return True
            logger.debug(f"[萌音点歌] 发送模式 {mode} 失败或不可用，尝试下一模式")

        _finalize_timings(timings, t_start)
        logger.error(f"[萌音点歌] 所有发送模式均失败：《{track.display}》")
        await event.send(
            event.plain_result(
                options.fail_hint if options else "这首歌暂时发不出来，换一首试试吧～"
            )
        )
        return False

    # ============ 播放链接解析 ============

    async def resolve_play_url(self, track: Track, wanted: str | None = None) -> dict:
        """获取播放链接；音质超 Key 上限（4220）时自动收敛重取。

        收敛依据优先级：Key 的 maxQuality（自检缓存）→ 曲目可用音质列表 → 逐级降档。

        Args:
            wanted: 期望音质；None 表示用配置里的默认音质（普通点歌）。
                    「点歌文件」指令传入 ``file_quality``。
        """
        wanted = wanted or self.cfg.default_quality
        candidates: list[str] = []

        # 先按 Key 允许的上限收敛（后端对超出上限的显式音质是 422 拒绝，不是静默降级）
        allowed = self.api.clamp_quality(wanted)
        candidates.append(allowed)
        if allowed != wanted:
            candidates.append(wanted)  # 上限缓存可能过期：仍留一次原始期望
        # 曲目自身可用音质从高到低补充为候选
        for q in sorted(track.qualitys, key=quality_rank, reverse=True):
            if q not in candidates:
                candidates.append(q)
        # 兜底：整条音质阶梯逐级降档
        for q in reversed(["128k", "192k", "320k", "flac", "flac24bit"]):
            if q not in candidates and quality_rank(q) < quality_rank(wanted):
                candidates.append(q)

        last_error: ApiError | None = None
        for q in candidates:
            try:
                audio = await self.api.play_url(track.id, q)
                _log_quality_result(track, wanted, q, audio)
                return audio
            except ApiError as e:
                if e.code == 4220:
                    last_error = e
                    continue
                raise
        # 终极兜底：不传 quality，让后端按 Key 的默认音质签发
        # （个别后端配置/版本下可能拒绝所有显式音质，此时省略参数最稳妥）
        try:
            audio = await self.api.play_url(track.id)
            logger.warning(
                f"[萌音点歌] 显式音质均被拒，改由后端按 Key 默认音质签发："
                f"实得 {audio.get('quality') or '未知'}：《{track.display}》"
            )
            return audio
        except ApiError as e:
            if e.code != 4220:
                raise
            raise last_error or e

    # ============ 各发送模式 ============

    async def _dispatch(
        self,
        event,
        mode: str,
        track: Track,
        audio_url: str,
        audio_url_raw: str,
        quality: str,
        cover_url: str,
        timings: dict,
        embed_metadata: bool = True,
    ) -> bool:
        """audio_url 为对外可达链接（发给用户的）；audio_url_raw 为后端原链接（插件自己下载用）。"""
        if mode == "card":
            return await self._send_card(event, track, audio_url, cover_url)
        if mode == "record_link":
            return await self._send_record_link(event, audio_url)
        if mode == "record_local":
            return await self._send_record_local(event, track, audio_url_raw, quality, timings)
        if mode == "file_link":
            return await self._send_file_link(event, track, audio_url, quality)
        if mode == "file_local":
            return await self._send_file_local(
                event, track, audio_url_raw, quality, cover_url, timings, embed_metadata
            )
        if mode == "text":
            return await self._send_text(event, track, audio_url)
        logger.warning(f"[萌音点歌] 未知的发送模式：{mode}")
        return False

    async def _send_card(self, event, track: Track, audio_url: str, cover_url: str) -> bool:
        """OneBot 音乐卡片。

        平台判断不看 isinstance（插件与框架的 astrbot 模块未必同一对象，会恒 False），
        直接探测协议端 call_action 是否可用，与撤回链路同一套判断。
        """
        if not audio_url:
            return False
        payloads: dict = {
            "message": [
                {
                    "type": "music",
                    "data": {
                        "type": "custom",
                        "url": audio_url,
                        "audio": audio_url,
                        "title": track.name,
                        "image": cover_url or "",
                        "singer": track.singer or "",
                    },
                }
            ]
        }
        ca = call_action_of(event)
        if ca is None:
            logger.warning("[萌音点歌] 当前平台不支持协议端直发，跳过音乐卡片")
            return False
        try:
            if event.is_private_chat():
                await ca(
                    "send_private_msg",
                    user_id=to_onebot_id(event.get_sender_id()),
                    **payloads,
                )
            else:
                await ca(
                    "send_group_msg",
                    group_id=to_onebot_id(event.get_group_id()),
                    **payloads,
                )
            return True
        except Exception as e:
            logger.warning(f"[萌音点歌] 音乐卡片发送失败（客户端可能不支持）：{type(e).__name__}: {e}")
            return False

    async def _send_record_link(self, event, audio_url: str) -> bool:
        if not audio_url:
            return False
        seg = Record.fromURL(audio_url)
        await event.send(event.chain_result([seg]))
        return True

    async def _send_record_local(
        self, event, track: Track, audio_url: str, quality: str, timings: dict
    ) -> bool:
        if not audio_url:
            return False
        path = await self._download_audio(track, audio_url, quality, timings)
        if not path:
            return False
        seg = Record.fromFileSystem(str(path))
        await event.send(event.chain_result([seg]))
        return True

    async def _send_file_link(self, event, track: Track, audio_url: str, quality: str) -> bool:
        if not audio_url:
            return False
        ext = guess_audio_ext(quality)
        seg = File(name=f"{_safe_filename(track.display)}{ext}", url=audio_url)
        await event.send(event.chain_result([seg]))
        return True

    async def _send_file_local(
        self,
        event,
        track: Track,
        audio_url: str,
        quality: str,
        cover_url: str = "",
        timings: dict | None = None,
        embed_metadata: bool = True,
    ) -> bool:
        if not audio_url:
            return False
        timings = timings if timings is not None else {}
        path = await self._download_audio(track, audio_url, quality, timings)
        if not path:
            return False
        if embed_metadata:
            await self._embed_track_metadata(path, track, cover_url, timings)
        seg = File(name=path.name, file=str(path))
        await event.send(event.chain_result([seg]))
        return True

    async def _embed_track_metadata(self, path: Path, track: Track, cover_url: str, timings: dict) -> None:
        """为本地文件嵌入标题/歌手/专辑/封面/歌词；任一失败静默降级，不阻断发送。"""
        cover_bytes = None
        if cover_url:
            try:
                cover_bytes = await self.api.download_bytes(cover_url)
            except ApiError as e:
                logger.warning(f"[萌音点歌] 封面下载失败，跳过封面嵌入：code={e.code}")

        lyrics = None
        try:
            lyric_data = await self.api.lyric(track.id)
            lyrics = (lyric_data or {}).get("lyric", "") or None
        except ApiError as e:
            logger.debug(f"[萌音点歌] 歌词获取失败，跳过歌词嵌入：code={e.code}")

        t0 = time.monotonic()
        ok = await asyncio.to_thread(
            embed_metadata,
            path,
            title=track.name,
            artist=track.singer or "未知歌手",
            album=track.album or "",
            cover_bytes=cover_bytes,
            lyrics=lyrics,
        )
        timings["embed_ms"] = int((time.monotonic() - t0) * 1000)
        timings["metadata_embedded"] = int(bool(ok))

    async def _send_text(self, event, track: Track, audio_url: str) -> bool:
        if not audio_url:
            return False
        await event.send(
            event.chain_result([Plain(f"🎵 {track.display}\n{track.source_name} · {audio_url}")])
        )
        return True

    # ============ 下载辅助 ============

    async def _download_audio(
        self, track: Track, audio_url: str, quality: str, timings: dict | None = None
    ) -> Path | None:
        """下载音频到插件临时目录，返回本地路径。

        文件名用「歌名 - 歌手.扩展名」，干净可读；同目录隔离由 download_dir
        的每次启动随机子目录保证，无需在文件名上再加随机后缀。
        """
        ext = guess_audio_ext(quality)
        dest = self.download_dir / f"{_safe_filename(track.display)}{ext}"
        t0 = time.monotonic()
        try:
            got = await self.api.download(audio_url, dest)
            if timings is not None:
                timings["download_ms"] = int((time.monotonic() - t0) * 1000)
            return got
        except ApiError as e:
            logger.error(f"[萌音点歌] 音频下载失败：code={e.code} {e.message}（{track.id}）")
            return None

    def cleanup_download_dir(self) -> None:
        """清理临时下载目录（插件卸载/停用时调用）。"""
        try:
            if self.download_dir.exists():
                for f in self.download_dir.iterdir():
                    if f.is_file():
                        f.unlink(missing_ok=True)
                self.download_dir.rmdir()
        except OSError as e:
            logger.warning(f"[萌音点歌] 清理临时目录失败：{e}")


def _finalize_timings(timings: dict, t_start: float) -> None:
    """收尾耗时统计：process_ms 为接口纯处理耗时，total_ms 为端到端总耗时。"""
    now = time.monotonic()
    timings["total_ms"] = int((now - t_start) * 1000)
    timings["process_ms"] = sum(
        timings.get(k) or 0 for k in ("resolve_ms", "download_ms", "embed_ms", "send_ms")
    )


async def send_lyrics_image(event, image_bytes: bytes, caption: str = "") -> bool:
    """发送歌词图片，失败返回 False（由调用方回退文本）。"""
    try:
        seg = Image.fromBytes(image_bytes)
        chain = [seg]
        if caption:
            chain.append(Plain(caption))
        await event.send(event.chain_result(chain))
        return True
    except Exception:
        logger.error(f"[萌音点歌] 歌词图片发送失败：\n{traceback.format_exc()}")
        return False
