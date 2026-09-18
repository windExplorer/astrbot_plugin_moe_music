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

# 「点歌文件」指令的后缀：命令去掉它之后与普通点歌别名完全同构
FILE_COMMAND_SUFFIX = "文件"


def resolve_command_source(cmd: str, *, file_mode: bool = False) -> str:
    """命令名 → 平台码（找不到则空字符串 = 聚合搜索）。

    ``file_mode=True``（「点歌文件」指令）时先剥掉尾部「文件」后缀再查表，因此
    「酷狗点歌文件」「qq点歌文件」等大小写变体无需维护第二张映射表。
    """
    base = cmd.strip()
    if file_mode and base.endswith(FILE_COMMAND_SUFFIX):
        base = base[: -len(FILE_COMMAND_SUFFIX)]
    return COMMAND_SOURCE_ALIAS.get(base.lower(), "")


# 名单类配置的分隔符：换行 / 逗号（半角与全角）/ 顿号 / 分号 / 空白
_LIST_SEPARATORS = re.compile(r"[\s,，、;；]+")


def parse_str_list(value) -> list[str]:
    """名单类配置归一化为字符串列表（兼容列表与各种分隔符字符串）。

    AstrBot 配置页、旧版 WebUI 与手工编辑的 config 都可能把「列表」存成字符串，
    常见写法有 ``"123 456"``、``"123,456"``、``"123\\n456"``。直接 ``for v in value``
    会**逐字符**遍历字符串（``"123456"`` → ``["1","2","3",...]``），名单看着填了却
    一个都匹配不上——白/黑名单「填了不生效」多由此而来。
    """
    if value is None:
        return []
    if isinstance(value, str):
        chunks = [value]
    elif isinstance(value, (list, tuple, set)):
        chunks = [str(item) for item in value]
    else:
        chunks = [str(value)]
    result: list[str] = []
    for chunk in chunks:
        result.extend(p.strip() for p in _LIST_SEPARATORS.split(chunk) if p.strip())
    return result


def _strip_option_label(option: str) -> str:
    """归一化配置面板选项：去掉括号说明，如 "all(聚合)" -> "all"、"card(音乐卡片)" -> "card"。"""
    return option.strip().split("(", 1)[0].strip().lower()


def parse_send_modes(value, default: list[str], key: str = "send_modes") -> list[str]:
    """发送方式列表归一化：面板选项可能带 "(说明)" 后缀，逐项归一化并去重保序。

    配置为空（或全是非法项）时回退到 ``default``——发送方式为空会让「所有发送模式
    都失败」，点歌直接发不出去，必须兜底。
    """
    modes: list[str] = []
    for item in value or []:
        mode = _strip_option_label(str(item))
        if mode in SEND_MODES and mode not in modes:
            modes.append(mode)
    if not modes:
        logger.warning(f"[萌音点歌] {key} 配置为空或非法，使用默认降级顺序：{default}")
        return list(default)
    return modes


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
    record_via_onebot: bool = True  # 语音经协议端直发，绕开框架的 base64 + 强制转 wav
    timeout: int = 15
    request_timeout: int = 10
    queue_concurrency: int = 1  # 点歌任务并发度（默认串行：上一个任务完成再执行下一个）
    queue_max_pending: int = 20  # 等待队列上限，队满直接拒绝
    enable_lyrics: bool = False
    embed_metadata: bool = True  # 文件模式嵌入封面/歌词/标题等元数据
    # 「点歌文件」指令：独立音质与嵌入开关（与普通点歌的 default_quality / embed_metadata 分开）
    file_quality: str = "flac"
    file_embed_metadata: bool = True
    recall_candidate: bool = True  # 选歌结束后撤回候选列表（仅 aiocqhttp 可用）
    # 分享识别：群里有人分享 QQ音乐/网易云/酷狗/酷我的歌曲卡片或链接时自动点歌
    # 默认关闭（自动发消息属于「机器人主动说话」，由使用者显式开启）
    share_auto_play: bool = False
    share_send_modes: list[str] = field(default_factory=lambda: ["record_link", "text"])
    share_quality: str = "320k"  # 分享识别取链音质（语音会被 QQ 转码，320k 通常足够）
    # 歌曲信息卡片：点歌成功后先发卡片（图片）、再发音频
    song_card_enable: bool = True  # 是否发送卡片
    song_card_year: bool = True  # 渲染：卡片显示发行年份（数据由 LLM 总开关获取）
    song_card_intro: bool = True  # 渲染：卡片显示歌曲简介（数据由 LLM 总开关获取）
    song_card_llm_sync: bool = True  # 信息获取总开关：LLM 一次调用取简介/歌手简介/年份兜底
    llm_provider_id: str = ""  # 卡片信息用的模型 ID；空 = 跟随系统当前对话模型
    info_retry_days: int = 0  # 抓取失败后的重试间隔（天）；0 = 永不重试（成功数据永久保存）
    llm_web_search: bool = True  # LLM 获取信息时是否允许联网搜索（关 = 单轮直调，更快）
    song_intro_wiki: bool = True  # 歌曲简介优先用中文维基百科开放接口（快），LLM 兜底
    song_card_comment: bool = True  # 补热评第一条（wy/kw/kg 直取，失败静默跳过）
    song_card_artist_bio: bool = True  # 用 LLM 生成歌手简介（无可用模型时自动跳过）
    song_card_repeat_sec: int = 300  # 同会话同一首歌在此秒数内不重复发卡片（0 = 每次都发）
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
        file_quality = _strip_option_label(str(config.get("file_quality", "flac") or "flac"))
        share_quality = _strip_option_label(str(config.get("share_quality", "320k") or "320k"))

        try:
            song_limit = int(config.get("song_limit", 5))
        except (TypeError, ValueError):
            song_limit = 5
        song_limit = max(1, min(20, song_limit))

        # send_modes：面板保存的选项可能带 "(说明)" 后缀，逐项归一化并去重保序
        send_modes = parse_send_modes(
            config.get("send_modes", []), ["card", "record_link", "file_local", "text"]
        )
        # 分享识别是「自动说话」，默认走语音；发送方式独立配置，与普通点歌互不影响
        share_send_modes = parse_send_modes(
            config.get("share_send_modes", []), ["record_link", "text"], "share_send_modes"
        )

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
            # 兼容历史遗留的字符串配置（逗号/空格/换行分隔），不再逐字符拆分
            return parse_str_list(config.get(key))

        return cls(
            api_base_url=base_url,
            api_key=api_key,
            public_base_url=public_base_url,
            default_source=default_source,
            default_quality=default_quality,
            song_limit=song_limit,
            selection_display=selection_display,
            send_modes=send_modes,
            record_via_onebot=bool(config.get("record_via_onebot", True)),
            timeout=_int_opt("timeout", 15, 5, 60),
            request_timeout=_int_opt("request_timeout", 10, 5, 30),
            queue_concurrency=_int_opt("queue_concurrency", 1, 1, 5),
            queue_max_pending=_int_opt("queue_max_pending", 20, 5, 50),
            enable_lyrics=bool(config.get("enable_lyrics", False)),
            embed_metadata=bool(config.get("embed_metadata", True)),
            file_quality=file_quality,
            file_embed_metadata=bool(config.get("file_embed_metadata", True)),
            recall_candidate=bool(config.get("recall_candidate", True)),
            share_auto_play=bool(config.get("share_auto_play", False)),
            share_send_modes=share_send_modes,
            share_quality=share_quality,
            song_card_enable=bool(config.get("song_card_enable", True)),
            song_card_year=bool(config.get("song_card_year", True)),
            song_card_intro=bool(config.get("song_card_intro", True)),
            song_card_llm_sync=bool(config.get("song_card_llm_sync", True)),
            llm_provider_id=str(config.get("llm_provider_id", "") or "").strip(),
            info_retry_days=_int_opt("info_retry_days", 0, 0, 36500),
            llm_web_search=bool(config.get("llm_web_search", True)),
            song_intro_wiki=bool(config.get("song_intro_wiki", True)),
            song_card_comment=bool(config.get("song_card_comment", True)),
            song_card_artist_bio=bool(config.get("song_card_artist_bio", True)),
            song_card_repeat_sec=_int_opt("song_card_repeat_sec", 300, 0, 3600),
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
