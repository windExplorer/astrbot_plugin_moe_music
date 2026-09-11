"""数据模型：曲目 Track。"""

from dataclasses import dataclass

# 平台码 -> 中文名（用于候选列表展示与 LLM Tool 的音源参数解析）
SOURCE_NAMES: dict[str, str] = {
    "kw": "酷我",
    "kg": "酷狗",
    "tx": "QQ音乐",
    "wy": "网易云",
    "mg": "咪咕",
    "xm": "虾米",
    "bd": "百度",
}

# 音质档位，等级递增（与后端 qualityRank 一致）
QUALITY_ORDER: list[str] = [
    "128k",
    "192k",
    "320k",
    "flac",
    "flac24bit",
    "wav",
    "ape",
    "atmos",
    "atmos_plus",
    "dolby",
    "master",
]


def quality_rank(quality: str) -> int:
    """返回音质档位等级，未知档位返回 -1。"""
    try:
        return QUALITY_ORDER.index(quality)
    except ValueError:
        return -1


@dataclass(slots=True)
class Track:
    """后端开放 API 的曲目 DTO（PublicTrack）。"""

    id: str  # 后端全局唯一 id，如 "wy:123456"
    name: str
    singer: str
    album: str
    duration: int  # 秒
    source: str  # 平台码：kw/kg/tx/wy/mg/xm/bd
    qualitys: list[str]
    cover_url: str | None = None

    @classmethod
    def from_api(cls, data: dict) -> "Track":
        """将后端 PublicTrack JSON 反序列化为 Track，容错缺字段。"""
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "") or "未知歌曲"),
            singer=str(data.get("singer", "") or ""),
            album=str(data.get("album", "") or ""),
            duration=int(data.get("duration", 0) or 0),
            source=str(data.get("source", "") or ""),
            qualitys=list(data.get("qualitys", []) or []),
            cover_url=data.get("coverUrl") or None,
        )

    @property
    def source_name(self) -> str:
        """平台码对应的中文名，未知平台原样返回。"""
        return SOURCE_NAMES.get(self.source, self.source)

    @property
    def display(self) -> str:
        """「歌名 - 歌手」展示形式。"""
        return f"{self.name} - {self.singer}" if self.singer else self.name

    def duration_text(self) -> str:
        """时长 mm:ss 文本。"""
        if self.duration <= 0:
            return ""
        m, s = divmod(int(self.duration), 60)
        return f"{m:02d}:{s:02d}"
