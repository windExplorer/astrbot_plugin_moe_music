#!/usr/bin/env python3
"""检查音频文件是否真的嵌入了封面与歌词（不依赖播放器）。

系统 / 手机自带播放器看不到歌词时，无法判断是「播放器不读内嵌歌词」还是
「文件压根没嵌进去」。本脚本直接读容器里的原始元数据帧，给出客观结论：

    mp3   必须是 ID3v2 的 USLT 帧；写成 TXXX:USLT 是非法的，多数播放器读不到
    flac  必须是 Vorbis Comment 的 LYRICS
    m4a   必须是 ©lyr
    ogg   必须是 LYRICS

用法：
    uv run python scripts/check_tags.py "D:\\Downloads\\晴天.mp3"
    uv run python scripts/check_tags.py "D:\\Downloads"          # 递归扫目录

退出码：0 = 全部正确嵌入；1 = 存在未正确嵌入的文件；2 = 用法 / 路径错误。
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".ape", ".wv", ".aac"}

MARKS = {
    "ok": "[ OK ]",
    "wrong": "[FAIL]",
    "empty": "[WARN]",
    "missing": "[FAIL]",
    "error": "[ERR ]",
}


@dataclass
class Lyric:
    """一处歌词发现。standard=False 表示放在播放器普遍不读的非标准位置。"""

    frame: str
    standard: bool
    text: str


@dataclass
class Report:
    path: Path
    container: str = "?"
    title: str = ""
    artist: str = ""
    cover: str = "无"
    lyrics: list[Lyric] = field(default_factory=list)
    error: str = ""

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        if any(x.standard and x.text.strip() for x in self.lyrics):
            return "ok"
        if any(x.text.strip() for x in self.lyrics):
            return "wrong"
        if self.lyrics:
            return "empty"
        return "missing"

    @property
    def reason(self) -> str:
        return {
            "ok": "已正确嵌入（标准位置，通用播放器可读）",
            "wrong": "有歌词数据但放在非标准位置，多数播放器读不到",
            "empty": "歌词帧存在但内容为空",
            "missing": "没有歌词帧",
            "error": self.error,
        }[self.status]


def _text_of(value) -> str:
    """把 mutagen 的字段值（str / bytes / list）统一成字符串。"""
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(_text_of(v) for v in value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _preview(text: str, limit: int = 70) -> str:
    flat = " ".join(text.replace("\r", "").split())
    return (flat[:limit] + "…") if len(flat) > limit else (flat or "(空)")


def _fill_id3(report: Report, tags: ID3) -> None:
    for frame in tags.values():
        fid = frame.FrameID
        if fid == "TIT2":
            report.title = str(frame)
        elif fid == "TPE1":
            report.artist = str(frame)
        elif fid == "APIC":
            report.cover = f"APIC（{getattr(frame, 'mime', '?')}，{len(frame.data)} 字节）"
        elif fid == "USLT":
            report.lyrics.append(Lyric("USLT", True, _text_of(frame.text)))
        elif fid == "SYLT":
            report.lyrics.append(Lyric("SYLT（逐行同步）", True, _text_of(frame.text)))
        elif fid == "TXXX":
            desc = str(getattr(frame, "desc", "") or "")
            key = desc.upper().replace(" ", "").replace("_", "")
            if key in {"USLT", "LYRIC", "LYRICS", "UNSYNCEDLYRICS"}:
                report.lyrics.append(Lyric(f"TXXX:{desc}", False, _text_of(frame.text)))


def _fill_mp4(report: Report, tags) -> None:
    report.title = _text_of(tags.get("©nam"))
    report.artist = _text_of(tags.get("©ART"))
    if tags.get("covr"):
        report.cover = f"covr（{len(tags['covr'][0])} 字节）"
    if tags.get("©lyr"):
        report.lyrics.append(Lyric("©lyr", True, _text_of(tags["©lyr"])))
    for key in tags.keys():
        if isinstance(key, str) and key.startswith("----") and "LYRIC" in key.upper():
            report.lyrics.append(Lyric(key, True, _text_of(tags.get(key))))


def _fill_vorbis(report: Report, tags) -> None:
    """FLAC / Ogg 的 Vorbis Comment，以及 APEv2 等通用兜底。"""
    for key, value in tags.items():
        normalized = str(key).upper().replace(" ", "").replace("_", "")
        text = _text_of(value)
        if normalized == "TITLE":
            report.title = text
        elif normalized in {"ARTIST", "ALBUMARTIST"}:
            report.artist = report.artist or text
        elif normalized in {"LYRIC", "LYRICS", "UNSYNCEDLYRICS"}:
            report.lyrics.append(Lyric(str(key), True, text))
        elif normalized == "METADATABLOCKPICTURE":
            report.cover = "METADATA_BLOCK_PICTURE（内嵌封面）"


def inspect(path: Path) -> Report:
    report = Report(path=path)
    try:
        audio = MutagenFile(path)
    except Exception as e:
        report.error = f"解析失败：{type(e).__name__}: {e}"
        return report
    if audio is None:
        report.error = "mutagen 无法识别的格式（可能不是音频）"
        return report
    report.container = type(audio).__name__

    tags = audio.tags
    if tags is None:
        return report
    if isinstance(tags, ID3):
        _fill_id3(report, tags)
    elif isinstance(audio, MP4):
        _fill_mp4(report, tags)
    else:
        _fill_vorbis(report, tags)

    if isinstance(audio, FLAC) and audio.pictures:
        pic = audio.pictures[0]
        report.cover = f"FLAC picture（{pic.mime}，{len(pic.data)} 字节）"
    return report


def print_report(report: Report) -> None:
    print(f"{MARKS[report.status]} {report.path.name}")
    title = report.title or "(无)"
    artist = report.artist or "(无)"
    print(f"       容器: {report.container} | 标题: {title} | 歌手: {artist}")
    print(f"       封面: {report.cover}")
    if not report.lyrics:
        print(f"       歌词: 无（{report.reason}）")
    for lyric in report.lyrics:
        kind = "标准" if lyric.standard else "非标准"
        print(f"       歌词: {lyric.frame}（{kind}，{len(lyric.text)} 字）")
        print(f"             {_preview(lyric.text)}")
    if report.error:
        print(f"       错误: {report.error}")
    print(f"       路径: {report.path}")


def collect(raw_paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for raw in raw_paths:
        p = Path(raw)
        if p.is_dir():
            found.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in AUDIO_EXTS))
        elif p.is_file():
            found.append(p)
        else:
            print(f"[ERR ] 路径不存在：{p}")
    return found


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    targets = collect(argv)
    if not targets:
        print("没有找到音频文件（支持：" + " ".join(sorted(AUDIO_EXTS)) + "）")
        return 2

    reports = [inspect(p) for p in targets]
    for report in reports:
        print_report(report)

    counts = Counter(r.status for r in reports)
    print("-" * 78)
    print(
        f"共 {len(reports)} 个文件：正确嵌入 {counts['ok']}，非标准位置 {counts['wrong']}，"
        f"空歌词 {counts['empty']}，未嵌入 {counts['missing']}，无法解析 {counts['error']}"
    )
    bad = [r for r in reports if r.status != "ok"]
    if bad:
        print("\n需要处理的文件：")
        for report in bad:
            print(f"  - {report.path}  →  {report.reason}")
        return 1

    print("\n全部文件均已正确嵌入标准歌词帧，播放器看不到只能是播放器不支持读取。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
