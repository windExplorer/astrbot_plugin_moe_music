"""音频元数据嵌入：把标题 / 歌手 / 专辑 / 封面 / 歌词写进音频文件。

- 支持 MP3（ID3v2）、FLAC、MP4/M4A 三类主流格式，其余格式跳过（仅记日志）；
- 纯同步实现（mutagen），由调用方放线程池执行；
- 任何失败由调用方兜底：只记日志，绝不阻断文件发送。
"""

import traceback
from pathlib import Path

from astrbot.api import logger

try:
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, USLT, ID3NoHeaderError
    from mutagen.mp4 import MP4, MP4Cover

    MUTAGEN_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖缺失时功能降级
    MUTAGEN_AVAILABLE = False


def sniff_image_mime(data: bytes) -> str:
    """按文件头识别封面图片 MIME 类型。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"  # 默认按 jpeg（后端封面绝大多数为此格式）


def _embed_id3(
    path: Path, title: str, artist: str, album: str, cover: bytes | None, lyrics: str | None
) -> None:
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    tags.setall("TIT2", [TIT2(encoding=3, text=title)])
    tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
    tags.setall("TALB", [TALB(encoding=3, text=album)])
    if cover:
        tags.setall(
            "APIC",
            [APIC(encoding=3, mime=sniff_image_mime(cover), type=3, desc="Cover", data=cover)],
        )
    if lyrics:
        tags.setall("USLT", [USLT(encoding=3, lang="chi", desc="", text=lyrics)])
    tags.save(str(path), v2_version=3)  # v2.3 兼容老车机/播放器


def _embed_flac(
    path: Path, title: str, artist: str, album: str, cover: bytes | None, lyrics: str | None
) -> None:
    audio = FLAC(str(path))
    audio["title"] = title
    audio["artist"] = artist
    audio["album"] = album
    if lyrics:
        # 键名用大写 LYRICS：Vorbis Comment 规范推荐全大写，部分播放器/工具只按大写查找
        # （后端下载链路同样写 LYRICS，保持两边一致）。
        audio["LYRICS"] = lyrics
    if cover:
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = sniff_image_mime(cover)
        pic.desc = "Cover"
        pic.data = cover
        audio.clear_pictures()
        audio.add_picture(pic)
    audio.save()


def _embed_mp4(
    path: Path, title: str, artist: str, album: str, cover: bytes | None, lyrics: str | None
) -> None:
    audio = MP4(str(path))
    audio["\xa9nam"] = [title]
    audio["\xa9ART"] = [artist]
    audio["\xa9alb"] = [album]
    if lyrics:
        audio["\xa9lyr"] = [lyrics]
    if cover:
        image_format = MP4Cover.FORMAT_PNG if sniff_image_mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover, imageformat=image_format)]
    audio.save()


# 扩展名 -> 嵌入函数
_EMBEDDERS = {
    ".mp3": _embed_id3,
    ".flac": _embed_flac,
    ".m4a": _embed_mp4,
    ".mp4": _embed_mp4,  # 音频型 m4a 常被误命名为 mp4
}


def embed_metadata(
    path: Path,
    *,
    title: str,
    artist: str,
    album: str = "",
    cover_bytes: bytes | None = None,
    lyrics: str | None = None,
) -> bool:
    """向音频文件写入标签，返回是否成功。

    Args:
        path: 音频文件路径（按扩展名分派写入器）。
        title / artist / album: 基本标签。
        cover_bytes: 封面图片字节（None 跳过封面）。
        lyrics: 非同步歌词文本（None 跳过歌词）。
    """
    if not MUTAGEN_AVAILABLE:
        logger.warning("[萌音点歌] mutagen 未安装，跳过元数据嵌入（请在插件环境安装 mutagen）")
        return False

    embedder = _EMBEDDERS.get(path.suffix.lower())
    if embedder is None:
        logger.debug(f"[萌音点歌] 格式 {path.suffix} 暂不支持元数据嵌入，跳过")
        return False

    try:
        embedder(path, title, artist, album, cover_bytes, lyrics)
        logger.debug(f"[萌音点歌] 元数据嵌入完成：{path.name}")
        return True
    except Exception:
        logger.warning(f"[萌音点歌] 元数据嵌入失败（文件照常发送）：{path.name}\n{traceback.format_exc()}")
        return False
