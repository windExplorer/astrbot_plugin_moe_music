"""聊天分享卡片 / 分享链接识别（QQ音乐、网易云音乐、酷狗音乐、酷我音乐）。

在 QQ 里分享歌曲时，协议端下发的是 OneBot 的 ``json`` 消息段（AstrBot 解析为
:class:`~astrbot.api.message_components.Json`），常见的两种形态：
- ``app=com.tencent.music.lua``：QQ音乐自己的音乐卡片，``meta`` 里有
  ``musicUrl``（``i.y.qq.com/v8/playsong.html?songmid=...``）、``title``、``desc``；
- ``app=com.tencent.structmsg``：网易云 / 酷狗 / 酷我 分享用的通用卡片，
  ``meta.music``（或 ``meta.detail_1``）里同样有 ``musicUrl`` / ``title`` / ``desc``。

两个必须处理的坑：
1. ``meta`` 常常是**字符串套 JSON**（``"meta": "{\\"title\\":...}"``），必须二次解析；
2. 分享链接里的曲目 id 各平台不一致——QQ音乐是 ``songmid``、网易云是数字 ``id``、
   酷狗是文件 ``hash``、酷我的 ``rid`` 后端没有内置详情能力，只能按关键词搜索。

因此本模块只负责「认出是哪个平台的哪首歌」，不碰网络与发送：

- :func:`extract_share`：从当前消息链里提取（不含被引用消息）；
- :func:`extract_quoted_share`：从**被引用**（引用回复）的消息链里提取；
- :func:`extract_share_from_text`：从纯文本里的分享链接提取。

认不出（平台未知、且既没有可用 id 也没有歌名）时一律返回 ``None``，绝不猜测。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from astrbot.api.message_components import Json, Plain, Reply

from .model import SOURCE_NAMES

# 平台码 -> (中文名, 可从其上下文中认出该平台的域名后缀)
SHARE_PLATFORMS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tx": ("QQ音乐", ("y.qq.com", "music.qq.com", "qqmusic.qq.com")),
    "wy": ("网易云音乐", ("music.163.com", "163cn.tv")),
    "kg": ("酷狗音乐", ("kugou.com", "kglink.cn")),
    "kw": ("酷我音乐", ("kuwo.cn", "kuwo.com")),
}

# 卡片里可能出现的平台标识（tag / app / 卡片文本），用于「有歌名但链接认不出」的兜底
_PLATFORM_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tx", ("qq音乐", "qqmusic", "com.tencent.music")),
    ("wy", ("网易云音乐", "网易云", "cloudmusic", "163.com")),
    ("kg", ("酷狗音乐", "酷狗", "kugou")),
    ("kw", ("酷我音乐", "酷我", "kuwo")),
)

# 歌名 / 歌手在各类卡片里的键名
_TITLE_KEYS = ("title", "name", "songname", "song_name", "musicname", "music_name", "songtitle")
_SINGER_KEYS = (
    "singer",
    "singers",
    "singername",
    "singer_name",
    "artist",
    "artistname",
    "artist_name",
    "authorname",
    "author",
)
_DESC_KEYS = ("desc", "description", "subtitle")

# 无意义的 desc（structmsg 外层 desc 是卡片类型，不是歌手）
_DESC_NOISE = {"音乐", "歌曲", "分享", "音乐分享", "单曲", "歌曲分享"}

# 文本里的链接（到空白 / 中文标点为止）
_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff<>\"'，。；、）】\]]+", re.IGNORECASE)
# 网易云 / 酷我的数字 id
_DIGITS_RE = re.compile(r"\d{3,}")
# 「[分享]歌名」「分享歌名」前缀
_SHARE_PREFIX_RE = re.compile(r"^[\[【(（]?\s*(分享|歌曲|音乐|单曲)\s*[\]】)）]?\s*")


@dataclass(slots=True)
class ShareInfo:
    """从聊天消息里识别出的分享信息。"""

    platform: str = ""
    """平台码：tx / wy / kg / kw；未知为空字符串。"""

    track_id: str = ""
    """可直接向后端定位曲目的 id（QQ音乐 songmid / 网易云数字 id / 酷狗 hash）；
    酷我链接里的 rid 后端没有内置详情能力，恒为空。"""

    title: str = ""
    """歌名（卡片 meta.title，或纯文本链接没有歌名时为空）。"""

    singer: str = ""
    """歌手（卡片 meta.desc）。"""

    url: str = ""
    """分享原始链接（排查用）。"""

    cover_url: str = ""
    """卡片自带的封面图（仅卡片可见；下载失败不影响识别）。"""

    origin: str = "card"
    """来源：card（分享卡片）/ link（文本链接）。"""

    @property
    def platform_name(self) -> str:
        """平台中文名（未知平台回退为「未知平台」）。"""
        return SHARE_PLATFORMS.get(self.platform, ("",))[0] or SOURCE_NAMES.get(
            self.platform, "未知平台"
        )

    @property
    def keyword(self) -> str:
        """搜索关键词：``歌名 歌手``。"""
        return " ".join(x for x in (self.title.strip(), self.singer.strip()) if x)

    @property
    def display(self) -> str:
        """日志/提示用的展示文本。"""
        if self.title and self.singer:
            return f"{self.title} - {self.singer}"
        return self.title or self.url or "未知歌曲"

    @property
    def label(self) -> str:
        """用户侧提示用的称呼：``《歌名》`` / ``歌曲``。"""
        return f"《{self.title}》" if self.title else "歌曲"

    @property
    def signature(self) -> str:
        """用于日志与去重的稳定标识。"""
        return f"{self.platform}:{self.track_id or self.title}"

    @property
    def usable(self) -> bool:
        """是否有足够信息去后端解析（有 id 或有关键词）。"""
        return bool(self.track_id or self.keyword)


# ============ 消息段识别 ============


def is_component(comp, cls, kind: str) -> bool:
    """判断消息段类型：isinstance 为主，类名 / ``type`` 字段兜底。

    插件以包形式加载时，插件 ``import`` 的 astrbot 模块与框架运行时未必是同一个
    模块对象，此时 ``isinstance`` 可能**恒为 False**（本仓库 v0.10.5 的自动撤回与
    音乐卡片就是这样一起静默失效的）。所以这里再比对类名与 ``type`` 字段值，
    保证「分享识别」不会因为一次模块重载就彻底不工作。
    """
    if isinstance(comp, cls):
        return True
    if type(comp).__name__ == cls.__name__:
        return True
    raw = getattr(comp, "type", None)
    value = getattr(raw, "value", raw)
    return str(value).strip().lower() == kind


def _component_text(comp) -> str:
    """取消息段的文本（真实组件在 ``text`` 上，测试 stub 可能放在位置参数里）。"""
    text = getattr(comp, "text", "") or ""
    if text:
        return str(text)
    return " ".join(str(a) for a in (getattr(comp, "args", ()) or ()))


# ============ 对外入口 ============


def extract_share(components: Iterable | None) -> ShareInfo | None:
    """从消息链里提取分享信息（**不**进入被引用的消息）。

    同一条消息里可能既有卡片又有文本链接，取信息最全的那个。
    """
    best: ShareInfo | None = None
    for comp in components or []:
        info: ShareInfo | None = None
        if is_component(comp, Json, "json"):
            payload = getattr(comp, "data", None)
            if isinstance(payload, (dict, list, str)):
                info = parse_share_payload(payload)
        elif is_component(comp, Plain, "plain"):
            info = extract_share_from_text(_component_text(comp))
        elif is_component(comp, Reply, "reply"):
            continue  # 引用消息不参与「本条消息是不是分享」的判断
        elif _component_kind(comp) == "music":
            info = parse_music_segment(comp)
        if info is None or not info.usable:
            continue
        if best is None or _richness(info) > _richness(best):
            best = info
    return best


def extract_quoted_share(components: Iterable | None) -> ShareInfo | None:
    """从被引用（引用回复）的消息链里提取分享信息。

    aiocqhttp 适配器会把引用消息的内容解析进 ``Reply.chain``（见框架
    ``_convert_handle_message_event``），因此「引用分享 + 发指令」能直接拿到卡片。
    """
    for comp in components or []:
        if not is_component(comp, Reply, "reply"):
            continue
        info = extract_share(getattr(comp, "chain", None) or [])
        if info is not None:
            return info
    return None


def parse_music_segment(comp) -> ShareInfo | None:
    """解析 OneBot ``music`` 消息段（少数客户端会用它下发分享，属兜底路径）。

    该段的字段是 ``_type``（qq / 163 / xm / custom）、``id``、``title``、``content``、
    ``url``。其中只有网易云（163）的 ``id`` 能直接对应后端曲目 id；QQ 的 ``id`` 是
    songid 而非后端使用的 songmid，只能退化到按歌名搜索。
    """
    kind = str(getattr(comp, "_type", "") or "").strip().lower()
    platform = {"163": "wy", "netease": "wy", "qq": "tx"}.get(kind, "")
    if not platform:
        return None
    raw_id = str(getattr(comp, "id", "") or "").strip()
    track_id = raw_id if platform == "wy" and _DIGITS_RE.fullmatch(raw_id) else ""
    title = str(getattr(comp, "title", "") or "").strip()
    singer = _clean_singer(str(getattr(comp, "content", "") or ""))
    info = ShareInfo(
        platform=platform,
        track_id=track_id,
        title=_clean_title(title),
        singer=singer,
        url=str(getattr(comp, "url", "") or ""),
        cover_url=str(getattr(comp, "image", "") or ""),
        origin="card",
    )
    return info if info.usable else None


def _component_kind(comp) -> str:
    """消息段类型名（小写）；取不到返回空串。"""
    raw = getattr(comp, "type", None)
    value = getattr(raw, "value", raw)
    return str(value).strip().lower() if value is not None else ""


def extract_share_from_text(text: str) -> ShareInfo | None:
    """从纯文本里提取分享链接（如用户直接把链接贴进聊天）。"""
    if not text:
        return None
    best: ShareInfo | None = None
    for url in _URL_RE.findall(text):
        parsed = _parse_share_url(url)
        if parsed is None:
            continue
        platform, track_id = parsed
        info = ShareInfo(platform=platform, track_id=track_id, url=url, origin="link")
        if info.usable and (best is None or _richness(info) > _richness(best)):
            best = info
    return best


def parse_share_payload(payload) -> ShareInfo | None:
    """解析一张分享卡片的 JSON 负载（``Json`` 组件的 ``data``）。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, (dict, list)):
        return None

    platform = ""
    track_id = ""
    url = ""
    for candidate in _collect_urls(payload):
        parsed = _parse_share_url(candidate)
        if parsed is None:
            continue
        p, tid = parsed
        # 优先保留带 id 的链接（能精确命中曲目）
        if not platform or (tid and not track_id) or p == platform:
            platform, track_id, url = p, tid, candidate
        if tid:
            break

    if not platform:
        platform = _infer_platform(payload)
    if not platform:
        # 认不出平台的卡片一律不管：QQ 里的小程序 / 视频 / 图文分享都带 title，
        # 若只看「有歌名」就动手，等于给每个分享卡片都搜一遍歌。
        return None
    title, singer = _extract_meta(payload)
    info = ShareInfo(
        platform=platform,
        track_id=track_id,
        title=title,
        singer=singer,
        url=url,
        cover_url=_extract_cover(payload),
        origin="card",
    )
    return info if info.usable else None


# ============ URL 解析 ============


def _platform_of_host(host: str) -> str:
    """按域名后缀识别平台；认不出返回空串。"""
    host = (host or "").lower()
    if not host:
        return ""
    for code, (_name, suffixes) in SHARE_PLATFORMS.items():
        for suffix in suffixes:
            if host == suffix or host.endswith("." + suffix):
                return code
    return ""


def _parse_share_url(url: str) -> tuple[str, str] | None:
    """分享链接 -> ``(平台码, 曲目 id)``；不是四大平台链接时返回 None。

    曲目 id 为「后端可直接定位」的形态：QQ音乐 songmid、网易云数字 id、酷狗文件 hash；
    酷我链接里的 rid 后端没有内置详情能力，只回平台码（由调用方退化为搜索）。
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    platform = _platform_of_host(parsed.hostname or "")
    if not platform:
        return None

    # 网易云常把查询串放在 hash 路由里（``/#/song?id=123``），两处都要看
    params = parse_qs(parsed.query)
    fragment = parsed.fragment or ""
    if "?" in fragment:
        for key, values in parse_qs(fragment.split("?", 1)[1]).items():
            params.setdefault(key, values)
    path = parsed.path or ""

    track_id = ""
    if platform == "tx":
        track_id = _first_param(params, "songmid", "song_mid")
        if not track_id:
            track_id = _path_after(path, "songDetail") or _song_mid_in_path(path)
    elif platform == "wy":
        track_id = _first_param(params, "id")
        if not track_id:
            track_id = _path_after(path, "song")
        if track_id and not _DIGITS_RE.fullmatch(track_id):
            track_id = ""
    elif platform == "kg":
        track_id = _first_param(params, "hash")
        if not track_id:
            hash_in_path = re.search(r"/mixsong/([0-9a-z]+)\.html", path, re.IGNORECASE)
            if hash_in_path:
                track_id = hash_in_path.group(1)
    # 酷我：rid 无法被后端内置详情解析（无 kw musicInfo），交由搜索
    return platform, track_id


def _first_param(params: dict[str, list[str]], *names: str) -> str:
    for name in names:
        values = params.get(name)
        if values:
            value = str(values[0]).strip()
            if value:
                return value
    return ""


def _path_after(path: str, marker: str) -> str:
    """取路径里 ``.../marker/<id>`` 形式的 id。"""
    parts = [p for p in path.split("/") if p]
    for index, part in enumerate(parts):
        if part.lower() == marker.lower() and index + 1 < len(parts):
            return parts[index + 1].strip()
    return ""


def _song_mid_in_path(path: str) -> str:
    """QQ音乐还有一种 ``/n/ryqq/songDetail/<songmid>`` 之外的短链：``/song/<mid>``。"""
    return _path_after(path, "song")


# ============ 卡片内容提取 ============


def _iter_dicts(node) -> Iterator[dict]:
    """深度优先遍历 JSON 结构，遇到「JSON 字符串」就地二次解析。

    分享卡片的 ``meta`` 常是字符串套 JSON，不解析就拿不到标题与链接。
    """
    stack: list = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple, set)):
            stack.extend(cur)
        elif isinstance(cur, str):
            text = cur.strip()
            if text[:1] in ("{", "[") and text[-1:] in ("}", "]"):
                try:
                    stack.append(json.loads(text))
                except (ValueError, TypeError):
                    continue


def _collect_urls(node) -> list[str]:
    """收集负载里所有字符串中的 http(s) 链接（保持出现顺序）。"""
    urls: list[str] = []
    seen: set[str] = set()
    for scope in _iter_dicts(node):
        for value in scope.values():
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                if not isinstance(item, str):
                    continue
                for url in _URL_RE.findall(item):
                    if url not in seen:
                        seen.add(url)
                        urls.append(url)
    return urls


def _first_str(scope: dict, keys: tuple[str, ...]) -> str:
    """按优先级从 dict 里取第一个非空字符串值（键名大小写不敏感）。"""
    lowered = {str(k).lower(): v for k, v in scope.items()}
    for key in keys:
        value = lowered.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _extract_meta(payload) -> tuple[str, str]:
    """提取 ``(歌名, 歌手)``：优先取「同一节点里同时有歌名」的那一层。"""
    scopes = list(_iter_dicts(payload))
    for scope in scopes:
        title = _first_str(scope, _TITLE_KEYS)
        if not title:
            continue
        singer = _first_str(scope, _SINGER_KEYS) or _first_str(scope, _DESC_KEYS)
        return _clean_title(title), _clean_singer(singer)
    # 没有 title 层：退化为卡片 prompt（形如「[分享]歌名」）
    for scope in scopes:
        prompt = _first_str(scope, ("prompt",))
        if prompt:
            return _clean_title(prompt), ""
    return "", ""


def _clean_title(text: str) -> str:
    return _SHARE_PREFIX_RE.sub("", (text or "").strip()).strip()


def _clean_singer(text: str) -> str:
    singer = (text or "").strip()
    if singer in _DESC_NOISE or singer.startswith("http"):
        return ""
    return _SHARE_PREFIX_RE.sub("", singer).strip()


def _extract_cover(payload) -> str:
    """卡片自带的封面/预览图（失败也无所谓，只用于日志兜底）。"""
    for scope in _iter_dicts(payload):
        for key in ("image", "preview", "picurl", "pic_url", "cover", "img"):
            value = scope.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value.strip()
    return ""


def _infer_platform(payload) -> str:
    """链接认不出平台时，靠 ``tag`` / ``app`` / 卡片文本猜一个。"""
    texts: list[str] = []
    for scope in _iter_dicts(payload):
        for key in ("tag", "app", "prompt", "title", "view", "source"):
            value = scope.get(key)
            if isinstance(value, str):
                texts.append(value.lower())
    joined = " ".join(texts)
    for code, hints in _PLATFORM_HINTS:
        if any(hint in joined for hint in hints):
            return code
    return ""


def _richness(info: ShareInfo) -> tuple[int, int, int]:
    """信息量排序键：平台 > 曲目 id > 歌名。"""
    return (
        1 if info.platform else 0,
        1 if info.track_id else 0,
        1 if info.keyword else 0,
    )
