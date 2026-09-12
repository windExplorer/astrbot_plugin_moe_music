"""插件配置封装。"""

import re
from dataclasses import dataclass, field

from astrbot.api import logger

# 发送模式全集（校验用）
SEND_MODES = {"card", "record_link", "record_local", "file_link", "file_local", "text"}

# 合法的音源平台码（all 表示聚合搜索）
SOURCE_CODES = {"all", "kw", "kg", "tx", "wy", "mg", "xm", "bd"}

# 命令别名 -> 平台码（键统一小写；大小写变体在 main.py 的 alias 集合中展开）
COMMAND_SOURCE_ALIAS: dict[str, str] = {
    "点歌": "",
    "网易点歌": "wy",
    "网易": "wy",
    "qq点歌": "tx",
    "腾讯点歌": "tx",
    "酷狗点歌": "kg",
    "酷我点歌": "kw",
    "咪咕点歌": "mg",
}

# LLM Tool 的 source 参数 -> 平台码
LLM_SOURCE_ALIAS: dict[str, str] = {
    "": "",
    "all": "",
    "网易": "wy",
    "网易云": "wy",
    "网易云音乐": "wy",
    "wy": "wy",
    "qq": "tx",
    "qq音乐": "tx",
    "腾讯": "tx",
    "tx": "tx",
    "酷狗": "kg",
    "kg": "kg",
    "酷我": "kw",
    "kw": "kw",
    "咪咕": "mg",
    "mg": "mg",
}


def _strip_option_label(option: str) -> str:
    """归一化配置面板选项：去掉括号说明，如 "all(聚合)" -> "all"、"card(音乐卡片)" -> "card"。"""
    return option.strip().split("(", 1)[0].strip().lower()


@dataclass(slots=True)
class PluginConfig:
    """AstrBotConfig 的强类型视图。"""

    api_base_url: str = "http://127.0.0.1:3000"
    api_key: str = ""
    public_base_url: str = ""  # 临时链接对外可达地址（空 = 直接用后端返回的链接）
    default_source: str = "all"
    default_quality: str = "320k"
    song_limit: int = 5
    selection_display: str = "text"  # 候选列表显示方式：text(文本列表) / image(图片菜单)
    send_modes: list[str] = field(default_factory=lambda: ["card", "record_link", "file_local", "text"])
    timeout: int = 15
    request_timeout: int = 10
    queue_concurrency: int = 2  # 点歌任务并发度（worker 数）
    queue_max_pending: int = 20  # 等待队列上限，队满直接拒绝
    enable_lyrics: bool = False
    embed_metadata: bool = True  # 文件模式嵌入封面/歌词/标题等元数据
    proxy: str = ""
    enable_self_test: bool = True
    # 访问控制：白名单优先于黑名单；两者都为空时不限制
    whitelist_groups: list[str] = field(default_factory=list)
    whitelist_users: list[str] = field(default_factory=list)
    blacklist_groups: list[str] = field(default_factory=list)
    blacklist_users: list[str] = field(default_factory=list)

    @classmethod
    def from_astrbot_config(cls, config) -> "PluginConfig":
        """从 AstrBotConfig（dict 子类）读取配置并做基础校验。"""
        base_url = str(config.get("api_base_url", "") or "").strip().rstrip("/")
        api_key = str(config.get("api_key", "") or "").strip()
        public_base_url = str(config.get("public_base_url", "") or "").strip().rstrip("/")

        default_source = _strip_option_label(str(config.get("default_source", "all") or "all"))
        if default_source not in SOURCE_CODES:
            logger.warning(f"[萌音点歌] 配置 default_source 无效：{default_source}，回退为 all")
            default_source = "all"

        default_quality = _strip_option_label(str(config.get("default_quality", "320k") or "320k"))

        try:
            song_limit = int(config.get("song_limit", 5))
        except (TypeError, ValueError):
            song_limit = 5
        song_limit = max(1, min(20, song_limit))

        # send_modes：面板保存的选项可能带 "(说明)" 后缀，逐项归一化并去重保序
        raw_modes = config.get("send_modes", []) or []
        send_modes: list[str] = []
        for item in raw_modes:
            mode = _strip_option_label(str(item))
            if mode in SEND_MODES and mode not in send_modes:
                send_modes.append(mode)
        if not send_modes:
            logger.warning("[萌音点歌] send_modes 配置为空，使用默认降级顺序")
            send_modes = ["card", "record_link", "file_local", "text"]

        def _int_opt(key: str, default: int, lo: int, hi: int) -> int:
            try:
                val = int(config.get(key, default))
            except (TypeError, ValueError):
                val = default
            return max(lo, min(hi, val))

        selection_display = _strip_option_label(str(config.get("selection_display", "text") or "text"))
        if selection_display not in ("text", "image"):
            logger.warning(f"[萌音点歌] 配置 selection_display 无效：{selection_display}，回退为 text")
            selection_display = "text"

        def _str_list(key: str) -> list[str]:
            values = config.get(key, []) or []
            return [str(v).strip() for v in values if str(v).strip()]

        return cls(
            api_base_url=base_url,
            api_key=api_key,
            public_base_url=public_base_url,
            default_source=default_source,
            default_quality=default_quality,
            song_limit=song_limit,
            selection_display=selection_display,
            send_modes=send_modes,
            timeout=_int_opt("timeout", 15, 5, 60),
            request_timeout=_int_opt("request_timeout", 10, 5, 30),
            queue_concurrency=_int_opt("queue_concurrency", 2, 1, 5),
            queue_max_pending=_int_opt("queue_max_pending", 20, 5, 50),
            enable_lyrics=bool(config.get("enable_lyrics", False)),
            embed_metadata=bool(config.get("embed_metadata", True)),
            proxy=str(config.get("proxy", "") or "").strip(),
            enable_self_test=bool(config.get("enable_self_test", True)),
            whitelist_groups=_str_list("whitelist_groups"),
            whitelist_users=_str_list("whitelist_users"),
            blacklist_groups=_str_list("blacklist_groups"),
            blacklist_users=_str_list("blacklist_users"),
        )

    @property
    def key_masked(self) -> str:
        """脱敏后的 API Key，仅用于日志。"""
        if not self.api_key:
            return "<未配置>"
        if re.fullmatch(r"sk-.{4,}", self.api_key):
            return f"{self.api_key[:5]}****{self.api_key[-3:]}"
        return "<已配置>"
