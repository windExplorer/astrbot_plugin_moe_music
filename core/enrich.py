"""信息卡片增强数据抓取：发行年份、网易云热评、歌手简介。

三条来源，全部 **best-effort**：短超时、失败静默返回 None（只记日志），绝不阻断发歌。

1. **发行年份**：网易云旧版公开接口 ``/api/song/detail`` 的 ``album.publishTime``；
2. **热门评论**：按曲目所在平台直取——网易云（``R_SO_4_<id>`` 旧版未加密端点）、
   酷我（``ncomment.kuwo.cn`` 老客户端接口，免签名）、酷狗（``rank/topliked``，
   参数 md5 签名，盐值与洛雪桌面端一致）；QQ音乐评论需要额外的 songId 映射与签名，
   不移植——这些平台回退到「网易云同曲映射」再取；
3. **歌手简介**：插件侧没有联网搜索能力（AstrBot 只暴露 LLM），因此用当前对话模型
   生成一句简介并标注「AI 简介」；LLM 不可用/说不认识就跳过。

前两条都需要**网易云曲目 id**：曲目本身就是 wy 的直接用；其他平台先让我们自己的
后端按「歌名 歌手」搜一首 wy 同曲，且要求**歌名归一化后完全一致、歌手也吻合**才采用
——映射错了会把别人的年份和评论挂到这首歌上。

外部请求的域名挂在模块常量上（``WY_API_BASE``），测试里替换为本地假服务。
"""

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import aiohttp
from astrbot.api import logger

from .model import Track

# 网易云公开接口地址（测试可替换为本地假服务）
WY_API_BASE = "https://music.163.com"

# 酷我热评：老客户端接口，无需签名（酷我曲目 id 就是参数里的 sid）
KW_COMMENT_URL = "http://ncomment.kuwo.cn/com.s"
_KW_HEADERS = {"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9;)"}

# 酷狗热评：参数需 md5 签名（盐值与洛雪桌面端一致）；酷狗曲目 id（hash）即 schash/extdata
KG_COMMENT_BASE = "http://m.comment.service.kugou.com"
_KG_SIGN_KEY = "OIlwieks28dk2k092lksi2UIkp"
_KG_MID = "16249512204336365674023395779019"
_KG_CODE = "fc4be23b4e972707f36b8a828a93ba8a"

# 外部抓取超时（秒）：卡片增强绝不拖慢发歌，超时就当没有
FETCH_TIMEOUT = 3.0

# LLM 生成歌手简介的超时（后台任务，宽一点）
LLM_TIMEOUT = 20.0

_WY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
}

# 网易云评论里的表情占位符（如 [大笑]），画在卡片上是纯噪声，直接去掉
_EMOJI_TAG_RE = re.compile(r"\[[^\[\]]{1,8}\]")

# 秒级时间戳的合理下限（1980-01-01）：更小的值基本是把别的字段误当时间戳
_SECONDS_MIN = 315532800.0

# 歌手简介：卡片最多显示这么多字，LLM 也按这个上限要求
BIO_MAX_CHARS = 120

_BIO_SYSTEM_PROMPT = (
    "你是音乐资料助手。只输出一句中文简介，不超过 60 个汉字，"
    "不要 Markdown、不要引号、不要客套话、不要换行。"
)

_BIO_NO_DATA = ("暂无资料", "无法确定", "不确定", "没有资料", "无法提供", "抱歉")


def _clean_text(text: str, limit: int = 0) -> str:
    """去掉表情占位符 / 多余空白；limit > 0 时按字数截断加省略号。"""
    flat = _EMOJI_TAG_RE.sub("", (text or "").replace("\r", " ").replace("\n", " "))
    flat = " ".join(flat.split())
    if limit and len(flat) > limit:
        return flat[:limit] + "…"
    return flat


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """剥离简介里的 HTML 标签（网易云 description 常带 <br> 等）并解码常见实体。"""
    flat = _HTML_TAG_RE.sub(" ", text or "")
    entities = {"<br/>": " ", "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"'}
    for entity, char in entities.items():
        flat = flat.replace(entity, char)
    return flat


class Enricher:
    """信息卡片增强数据抓取器。"""

    def __init__(
        self,
        api,
        *,
        proxy: str = "",
        provider_getter: Callable[[str | None], Awaitable[Any]] | None = None,
        llm_ask: Callable[..., Awaitable[str | None]] | None = None,
        fetch_timeout: float = FETCH_TIMEOUT,
        llm_timeout: float = LLM_TIMEOUT,
    ):
        """
        Args:
            api: :class:`MusicApiClient`（非网易云曲目要靠它做同曲映射）。
            proxy: 外部请求代理（与音乐后端共用配置，留空直连）。
            provider_getter: ``async (umo) -> LLM Provider | None``；不传则跳过 AI 简介。
            llm_ask: ``async (event, umo, prompt, system_prompt) -> str | None``；
                由插件注入（会话开启联网搜索时走 agent 循环，LLM 可调用搜索工具）。
                注入后优先于 provider_getter 使用。
            fetch_timeout: 外部接口超时（秒）。
            llm_timeout: LLM 超时（秒）。
        """
        self.api = api
        self._proxy = proxy.strip() or None
        self._provider_getter = provider_getter
        self._llm_ask = llm_ask
        self._fetch_timeout = max(1.0, fetch_timeout)
        self._llm_timeout = max(3.0, llm_timeout)
        self._session: aiohttp.ClientSession | None = None

    # ============ HTTP ============

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=_WY_HEADERS,
                timeout=aiohttp.ClientTimeout(total=self._fetch_timeout),
                trust_env=True,
            )
        return self._session

    async def _get_json(self, path: str) -> dict | None:
        """GET 网易云公开接口并解析 JSON；任何异常都返回 None。"""
        return await self._get_json_url(f"{WY_API_BASE}{path}")

    async def _get_json_url(self, url: str, headers: dict | None = None) -> dict | None:
        """GET 任意外部 JSON 接口；任何异常都返回 None（增强信息绝不影响发歌）。"""
        try:
            session = await self._ensure_session()
            async with session.get(url, proxy=self._proxy, headers=headers) as resp:
                if resp.status != 200:
                    logger.info(f"[萌音点歌] 外部接口返回 HTTP {resp.status}：{url}")
                    return None
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            logger.info(f"[萌音点歌] 外部接口请求失败：{type(e).__name__}: {e}")
            return None
        except asyncio.TimeoutError:
            logger.info(f"[萌音点歌] 外部接口超时（{self._fetch_timeout}s）：{url}")
            return None
        except Exception:
            logger.info(f"[萌音点歌] 外部接口解析异常（响应非 JSON？）：{url}")
            return None
        return data if isinstance(data, dict) else None

    def set_proxy(self, proxy: str) -> None:
        """更新外部请求代理（WebUI 改配置后热应用）。"""
        self._proxy = (proxy or "").strip() or None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # ============ 1. 网易云 id 映射 ============

    async def resolve_wy_id(self, track: Track) -> str | None:
        """取曲目在网易云上的 id：本平台直接取；跨平台按「歌名 歌手」找同曲。"""
        if track.source == "wy":
            return track.id.split(":", 1)[-1] or None
        keyword = " ".join(x for x in (track.name, track.singer) if x).strip()
        if not keyword:
            return None
        try:
            candidates = await self.api.search(keyword, limit=5, source="wy")
        except Exception as e:  # ApiError / 网络异常都只是「没有映射」
            logger.debug(f"[萌音点歌] 同曲映射搜索失败（{track.display}）：{type(e).__name__}: {e}")
            return None

        want_name = _norm(track.name)
        want_singer = _norm(track.singer)
        for cand in candidates:
            if _norm(cand.name) != want_name:
                continue  # 歌名必须完全一致（归一化后），避免挂错歌的评论
            cand_singer = _norm(cand.singer)
            if want_singer and cand_singer:
                # 歌手也得吻合（互相包含即可，兼容「A / B」这类多歌手写法）
                if want_singer not in cand_singer and cand_singer not in want_singer:
                    continue
            wy_id = cand.id.split(":", 1)[-1]
            logger.debug(f"[萌音点歌] 同曲映射命中：《{track.display}》→ wy:{wy_id}")
            return wy_id or None
        logger.debug(f"[萌音点歌] 同曲映射未命中（歌名/歌手不吻合）：《{track.display}》")
        return None

    # ============ 2. 网易云歌曲详情（发行年份 + 专辑简介 + 封面） ============

    async def fetch_wy_detail(self, wy_id: str) -> dict:
        """取网易云歌曲详情：``{"year", "cover", "artist_id"}``（缺哪项就没有哪个键）。

        - ``year``：``album.publishTime``（毫秒时间戳）——事实数据，优先于 LLM；
        - ``cover``：``album.picUrl``——后端 wy 音源没有 pic 实现且详情不带 picUrl，
          这是网易云歌曲封面的主要来源；
        - ``artist_id``：主唱歌手的网易云 id，用于直取歌手简介（``fetch_artist_intro_wy``）。
        """
        if not wy_id:
            return {}
        data = await self._get_json(f"/api/song/detail/?ids=[{wy_id}]&id={wy_id}")
        if not data:
            return {}
        songs = data.get("songs")
        if not isinstance(songs, list) or not songs:
            return {}
        song = songs[0] if isinstance(songs[0], dict) else {}
        album = song.get("album") if isinstance(song.get("album"), dict) else {}
        short_album = song.get("al") if isinstance(song.get("al"), dict) else {}
        detail: dict = {}
        for raw in (
            album.get("publishTime"),
            short_album.get("publishTime"),
            song.get("publishTime"),
        ):
            year = _year_of(raw)
            if year:
                detail["year"] = year
                break
        cover = str(
            album.get("picUrl") or short_album.get("picUrl") or song.get("picUrl") or ""
        ).strip()
        if cover.startswith("http"):
            detail["cover"] = cover
        # 歌手 id：公开接口用完整字段 artists，weapi 缩写格式用 ar
        artists = song.get("artists") or song.get("ar") or []
        if isinstance(artists, list) and artists and isinstance(artists[0], dict):
            artist_id = str(artists[0].get("id") or "")
            if artist_id.isdigit():
                detail["artist_id"] = artist_id
        return detail

    async def fetch_artist_intro_wy(self, artist_id: str) -> str | None:
        """网易云歌手简介（公开接口，事实数据）：``artist.briefDesc`` 一段。

        比 LLM 快且不会编造；简介常是长文，截断到卡片能展示的长度。
        取不到返回 None（交给 LLM 兜底）。
        """
        if not artist_id:
            return None
        data = await self._get_json(f"/api/artist/{artist_id}")
        if not data:
            return None
        artist = data.get("artist") if isinstance(data.get("artist"), dict) else {}
        desc = _clean_text(_strip_html(str(artist.get("briefDesc") or "")), BIO_MAX_CHARS)
        return desc or None

    async def fetch_year(self, wy_id: str) -> int | None:
        """取发行年份（``fetch_wy_detail`` 的便捷封装，兼容旧调用）。"""
        return (await self.fetch_wy_detail(wy_id)).get("year")

    # ============ 3. 热门评论（wy 直取；kw/kg 本平台直取；其余回退 wy 映射） ============

    async def fetch_hot_comment(self, wy_id: str) -> dict | None:
        """网易云热评第一条：``{text, user, likes}``；取不到返回 None。"""
        if not wy_id:
            return None
        data = await self._get_json(
            f"/api/v1/resource/comments/R_SO_4_{wy_id}?limit=1&offset=0"
        )
        if not data:
            return None
        scope = data.get("data") if isinstance(data.get("data"), dict) else data
        for key in ("hotComments", "comments"):
            items = scope.get(key)
            if isinstance(items, list) and items:
                comment = items[0] if isinstance(items[0], dict) else {}
                text = _clean_text(str(comment.get("content") or ""))
                if not text:
                    continue
                user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
                likes = comment.get("likedCount")
                return {
                    "text": text,
                    "user": _clean_text(str(user.get("nickname") or "")),
                    "likes": int(likes) if isinstance(likes, (int, float)) else 0,
                }
        return None

    async def fetch_hot_comment_kw(self, kw_id: str) -> dict | None:
        """酷我热评第一条（老客户端接口，无需签名；酷我曲目 id 即 sid）。"""
        if not kw_id:
            return None
        query = (
            "f=web&type=get_rec_comment&aapiver=1&prod=kwplayer_ar_10.5.2.0&digest=15"
            f"&sid={kw_id}&start=0&msgflag=1&count=1&newver=3&uid=0"
        )
        data = await self._get_json_url(f"{KW_COMMENT_URL}?{query}", _KW_HEADERS)
        if not data or str(data.get("code")) != "200":
            return None
        items = data.get("hot_comments")
        item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
        text = _clean_text(str(item.get("msg") or ""))
        if not text:
            return None
        likes = item.get("like_num")
        return {
            "text": text,
            "user": _clean_text(str(item.get("u_name") or "")),
            "likes": int(likes) if isinstance(likes, (int, float)) else 0,
        }

    async def fetch_hot_comment_kg(self, kg_hash: str) -> dict | None:
        """酷狗热评第一条（参数需 md5 签名；曲目 hash 即 schash / extdata）。"""
        if not kg_hash:
            return None
        params = (
            "dfid=0"
            f"&mid={_KG_MID}"
            f"&clienttime={int(time.time() * 1000)}"
            "&uuid=0"
            f"&extdata={kg_hash}"
            f"&appid=1005&code={_KG_CODE}"
            f"&schash={kg_hash}"
            "&clientver=11409&p=1&clienttoken=&pagesize=1&ver=10&kugouid=0"
        )
        url = f"{KG_COMMENT_BASE}/r/v1/rank/topliked?{params}&signature={_kg_signature(params)}"
        data = await self._get_json_url(url)
        if not data or data.get("err_code") != 0:
            return None
        items = data.get("list")
        item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
        text = _clean_text(str(item.get("content") or ""))
        if not text:
            return None
        like = item.get("like") if isinstance(item.get("like"), dict) else {}
        likes = like.get("likenum")
        return {
            "text": text,
            "user": _clean_text(str(item.get("user_name") or "")),
            "likes": int(likes) if isinstance(likes, (int, float)) else 0,
        }

    async def fetch_hot_comment_for_track(self, track: Track) -> tuple[dict | None, str]:
        """按曲目所在平台取热评：wy / kw / kg 直取本平台，其余回退网易云同曲映射。

        Returns:
            ``(热评 dict 或 None, 来源平台码)``。
        """
        native = {
            "wy": lambda: self.fetch_hot_comment(track.id.split(":", 1)[-1]),
            "kw": lambda: self.fetch_hot_comment_kw(track.id.split(":", 1)[-1]),
            "kg": lambda: self.fetch_hot_comment_kg(track.id.split(":", 1)[-1]),
        }.get(track.source)
        if native is not None:
            comment = await native()
            if comment:
                return comment, track.source
            if track.source == "wy":
                return None, ""  # 同一接口刚失败过，别再走映射重复请求
        # QQ音乐 / 咪咕 / 未知平台，以及本平台没抓到：回退网易云同曲映射
        wy_id = await self.resolve_wy_id(track)
        if wy_id:
            comment = await self.fetch_hot_comment(wy_id)
            if comment:
                return comment, "wy"
        return None, ""

    # ============ 4. 卡片信息（LLM 一次调用：歌曲简介 + 歌手简介 + 年份兜底） ============

    async def fetch_card_info(
        self,
        track: Track,
        *,
        need_year: bool = False,
        need_intro: bool = False,
        need_bio: bool = False,
        event=None,
        umo: str | None = None,
    ) -> dict:
        """一次 LLM 调用同时获取歌曲简介 / 歌手简介 / 年份（按需）。

        返回 dict，只含成功拿到的键（``year`` / ``intro`` / ``artist_bio``）。
        会话开启联网搜索时（插件注入的 ``llm_ask`` 走 agent 循环）LLM 会自行
        搜索核实；确实查不到的字段返回时缺省，**绝不编造**。
        """
        if not (need_year or need_intro or need_bio):
            return {}
        title = (track.name or "").strip()
        if not title:
            return {}
        fields: list[str] = []
        if need_year:
            fields.append('"year": 发行年份（整数）或 null')
        if need_intro:
            fields.append('"intro": 一句话歌曲简介（不超过 60 字，侧重创作背景/年代/影响）或 null')
        if need_bio:
            fields.append('"artist_bio": 一句话歌手简介（不超过 60 字，风格/代表作/成就）或 null')
        who = f" 演唱：{track.singer}" if track.singer else ""
        album = f" 专辑：{track.album}" if track.album else ""
        prompt = (
            "请只输出一个 JSON 对象（不要任何多余文字），格式：{ " + ", ".join(fields) + " }。\n"
            f"歌曲：「{title}」{who}{album}。\n"
            "所有信息必须真实；不确定就先联网搜索核实，确实查不到才用 null，不要编造。"
        )
        text = await self._ask_llm(prompt, event=event, umo=umo)
        if not text:
            return {}
        data = _parse_llm_json(text)
        out: dict = {}
        if need_year:
            try:
                year = int(data.get("year"))
            except (TypeError, ValueError):
                year = 0
            if 1900 <= year <= 2100:
                out["year"] = year
        for key in ("intro", "artist_bio"):
            if not data.get(key):
                continue
            value = _clean_text(str(data[key]), BIO_MAX_CHARS).strip("\"'“”‘’`* ")
            if value and not any(mark in value for mark in _BIO_NO_DATA):
                out[key] = value
        return out

    async def _ask_llm(self, prompt: str, event=None, umo: str | None = None) -> str | None:
        """调当前对话模型拿原始文本；失败返回 None。

        优先走插件注入的 ``llm_ask``（会话开启联网搜索时挂搜索工具、由 agent
        循环执行工具调用）；没注入则直调 provider（无工具）。
        """
        if self._llm_ask is not None:
            try:
                return await self._llm_ask(event, umo, prompt, _BIO_SYSTEM_PROMPT)
            except asyncio.TimeoutError:
                logger.info(f"[萌音点歌] LLM 生成超时（{self._llm_timeout}s）")
                return None
            except Exception as e:
                logger.info(f"[萌音点歌] LLM 生成失败：{type(e).__name__}: {e}")
                return None
        if self._provider_getter is None:
            return None
        try:
            provider = await self._provider_getter(umo)
        except Exception as e:
            logger.debug(f"[萌音点歌] 获取 LLM Provider 失败：{type(e).__name__}: {e}")
            return None
        if provider is None:
            logger.info(
                "[萌音点歌] 没有可用的对话模型，跳过信息获取"
                "（请在 AstrBot 配置一个 Chat LLM 提供商，歌曲简介/歌手简介依赖它）"
            )
            return None
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=_BIO_SYSTEM_PROMPT),
                timeout=self._llm_timeout,
            )
        except asyncio.TimeoutError:
            logger.info(f"[萌音点歌] LLM 生成超时（{self._llm_timeout}s）")
            return None
        except Exception as e:
            logger.info(f"[萌音点歌] LLM 生成失败：{type(e).__name__}: {e}")
            return None
        return str(getattr(resp, "completion_text", "") or "").strip() or None


def _parse_llm_json(text: str) -> dict:
    """从 LLM 输出里抠出 JSON 对象（容忍 ```json 围栏 / 前后废话）；失败返回空 dict。"""
    text = (text or "").strip()
    if not text:
        return {}
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _norm(text: str) -> str:
    """歌名/歌手归一化：小写 + 去空格标点（与 commands.pick_best_track 同一套规则）。"""
    return re.sub(r"[\s\-_/·、,，.。!！?？'\"“”‘’()（）\[\]【】]+", "", (text or "").lower())


def _kg_signature(params: str) -> str:
    """酷狗接口参数签名（与洛雪桌面端一致：排序拼接后 md5(盐+参数+盐)）。"""
    parts = sorted(params.split("&"))
    joined = "".join(parts)
    return hashlib.md5(f"{_KG_SIGN_KEY}{joined}{_KG_SIGN_KEY}".encode()).hexdigest()


def _year_of(raw) -> int | None:
    """把 publishTime（毫秒或秒时间戳 / 字符串）解析成合理年份。

    网易云给的是**毫秒**时间戳；万一是秒级的，还要求它至少晚于 1980 年——否则
    出现的 1970 年基本可以断定是把别的数字字段误当成了时间戳（年份宁可不显示，
    也不能画一个错的）。
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 1e11:  # 毫秒时间戳
        value /= 1000.0
    elif value < _SECONDS_MIN:  # 秒级却早于 1980：视为非时间戳
        return None
    try:
        year = datetime.fromtimestamp(value).year
    except (OverflowError, OSError, ValueError):
        return None
    # 合理性校验：唱片工业起点 ~ 明年
    if 1900 <= year <= datetime.now().year + 1:
        return year
    return None
