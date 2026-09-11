"""lx_music_api 开放 API（/api/v1）HTTP 客户端。

职责：
- Bearer API Key 鉴权注入；
- 统一响应 ``{code, message, data}`` 解析，非 0 code 抛 :class:`ApiError`；
- 429 限流按 ``Retry-After`` 退避重试一次；
- 按 Key 的 QPS 限额推导最小请求间隔做节流排队；
- 封面 / 音频流式下载。

技术细节只进日志，调用方通过 :class:`ApiError` 的 ``user_hint`` 拿到用户侧温馨提示。
"""

import asyncio
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import aiofiles
import aiohttp
from astrbot.api import logger

from .model import Track

# 业务错误码 -> 用户侧温馨提示（不含任何技术细节）
USER_HINTS: dict[int, str] = {
    4000: "点歌请求好像有点问题，换个关键词试试吧～",
    4010: "音乐服务配置有误，请联系管理员～",
    4011: "音乐服务配置有误，请联系管理员～",
    4040: "没有找到相关歌曲，换个关键词试试吧～",
    4090: "音乐服务暂时处理不了这个请求，请稍后再试～",
    4220: "这首歌暂时不能以该音质获取，请稍后再试～",
    4280: "音乐服务尚未就绪，请稍后再试～",
    4290: "音乐服务有点忙，请稍后再试～",
    5000: "音乐服务开小差了，请稍后再试～",
}

DEFAULT_USER_HINT = "音乐服务开小差了，请稍后再试～"
NETWORK_HINT = "网络开小差了，请稍后重试～"


class ApiError(Exception):
    """后端业务异常（非 0 code 或网络层失败转义）。"""

    def __init__(self, code: int, message: str, *, network: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.network = network  # True 表示网络层异常（超时/连接失败），code 仅为占位

    @property
    def user_hint(self) -> str:
        """面向用户的温馨提示。"""
        if self.network:
            return NETWORK_HINT
        return USER_HINTS.get(self.code, DEFAULT_USER_HINT)


def guess_audio_ext(quality: str, content_type: str | None = None) -> str:
    """按音质档位 / 响应 Content-Type 推断音频扩展名。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if "flac" in ct:
        return ".flac"
    if "wav" in ct:
        return ".wav"
    if "ogg" in ct:
        return ".ogg"
    if "mp4" in ct or "m4a" in ct or "aac" in ct:
        return ".m4a"
    if "ape" in ct:
        return ".ape"
    if quality in ("flac", "flac24bit"):
        return ".flac"
    if quality == "wav":
        return ".wav"
    if quality == "ape":
        return ".ape"
    return ".mp3"


class MusicApiClient:
    """lx_music_api 开放 API 客户端（asyncio / aiohttp）。

    Args:
        base_url: 后端对外地址（不含 ``/api/v1``）。
        api_key: ``sk-`` 前缀的 API Key。
        request_timeout: 单次 HTTP 请求超时（秒）。
        proxy: 可选 HTTP 代理地址，空字符串表示直连。
        public_base_url: 临时链接的对外可达地址（如 https://music.example.com）。
            后端按请求 Host 拼临时链接，若本机经内网/localhost 访问后端，
            签发的链接外部用户不可达；配置此项后发送给用户的链接会改写为该地址。
            留空表示后端地址本身对外可达，链接原样使用。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        request_timeout: int = 10,
        proxy: str = "",
        public_base_url: str = "",
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=max(5, request_timeout))
        self._proxy = proxy.strip() or None
        self._public_base_url = public_base_url.strip().rstrip("/")
        self._session: aiohttp.ClientSession | None = None

        # 节流：按 Key 的 qpsLimit 推导最小请求间隔，避免自触发限流
        self._throttle_lock = asyncio.Lock()
        self._last_request_ts = 0.0
        self._min_interval = 0.34  # 默认按 QPS=3 估算，自检成功后按 Key 实际限额更新

        # 自检结果缓存
        self.key_max_quality: str | None = None
        self.key_default_quality: str | None = None
        self.key_qps_limit: int | None = None

    @property
    def api_root(self) -> str:
        return f"{self._base_url}/api/v1"

    def publicize(self, url: str) -> str:
        """把本站临时链接改写为对外可达地址；未配置 public_base_url 时原样返回。

        仅替换 scheme/netloc（及可选的路径前缀），保留后端签发的路径与查询串，
        因此后端即使部署在反向代理子路径下也能正确改写。
        """
        if not url or not self._public_base_url:
            return url
        pub = urlparse(self._public_base_url)
        orig = urlparse(url)
        path = (pub.path.rstrip("/") if pub.path else "") + (orig.path or "")
        return urlunparse((pub.scheme or orig.scheme, pub.netloc, path, "", orig.query, ""))

    def _key_masked(self) -> str:
        if not self._api_key:
            return "<未配置>"
        return f"{self._api_key[:5]}****{self._api_key[-3:]}"

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": "astrbot_plugin_moe_music/0.1.0",
            }
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=self._timeout,
                trust_env=True,
            )
        return self._session

    async def _throttle(self) -> None:
        """连续请求之间按最小间隔排队，避免打爆自己 Key 的 QPS 限额。"""
        async with self._throttle_lock:
            now = time.monotonic()
            wait = self._last_request_ts + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_ts = time.monotonic()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        """发起请求并解析统一响应，返回 ``data`` 字段。

        - HTTP 429 / code=4290：按 ``Retry-After`` 退避后重试一次；
        - 其余非 0 code：抛 :class:`ApiError`；
        - 网络异常：抛 ``ApiError(network=True)``。
        """
        if not self._base_url:
            raise ApiError(-1, "后端地址未配置")
        if not self._api_key:
            raise ApiError(4010, "API Key 未配置")

        url = f"{self.api_root}{path}"
        retried = False
        last_error: ApiError | None = None

        for _ in range(2):  # 首次 + 429 重试一次
            await self._throttle()
            session = await self._ensure_session()
            try:
                logger.debug(f"[萌音点歌] HTTP {method} {url} params={params or {}}")
                async with session.request(
                    method, url, params=params, json=json_body, proxy=self._proxy
                ) as resp:
                    # 优先尝试解析 JSON body（后端失败响应也返回 {code,message,data}）
                    payload: dict | None = None
                    try:
                        payload = await resp.json(content_type=None)
                    except Exception:
                        payload = None

                    if resp.status == 429:
                        retry_after = 1.0
                        try:
                            retry_after = max(0.2, float(resp.headers.get("Retry-After", 1)))
                        except (TypeError, ValueError):
                            pass
                        last_error = ApiError(4290, f"触发限流（Retry-After={retry_after}s）")
                        if not retried:
                            retried = True
                            logger.warning(f"[萌音点歌] 请求触发限流，{retry_after}s 后重试一次：{path}")
                            await asyncio.sleep(retry_after)
                            continue
                        raise last_error

                    if payload is None:
                        logger.error(f"[萌音点歌] 后端响应非 JSON：HTTP {resp.status} {path}")
                        raise ApiError(5000, f"HTTP {resp.status}，响应非 JSON")

                    code = payload.get("code", -1)
                    message = str(payload.get("message", "") or "")
                    data = payload.get("data")

                    if code == 0:
                        return data if isinstance(data, dict) else ({} if data is None else data)

                    # 业务失败：部分网关可能不按约定返回 429 头，仅以 code=4290 表示限流
                    if code == 4290 and not retried:
                        retried = True
                        logger.warning(f"[萌音点歌] 后端返回限流（code=4290），1s 后重试一次：{path}")
                        await asyncio.sleep(1.0)
                        continue

                    logger.error(
                        f"[萌音点歌] 后端业务错误：HTTP {resp.status} code={code} {message} ({path})"
                    )
                    raise ApiError(int(code), message)

            except aiohttp.ClientError as e:
                logger.error(f"[萌音点歌] 网络请求失败：{type(e).__name__}: {e} ({path})")
                raise ApiError(-1, f"{type(e).__name__}: {e}", network=True) from e
            except asyncio.TimeoutError as e:
                logger.error(f"[萌音点歌] 请求超时（{self._timeout.total}s）：{path}")
                raise ApiError(-1, "request timeout", network=True) from e

        raise last_error or ApiError(5000, "unreachable")

    # ============ 开放 API 封装 ============

    async def me(self) -> dict:
        """自检：回显本 Key 的限额信息，并更新节流间隔与音质上限缓存。"""
        data = await self._request("GET", "/me")
        key_info = data.get("apiKey") or {}
        qps = key_info.get("qpsLimit")
        if isinstance(qps, (int, float)) and qps > 0:
            self.key_qps_limit = int(qps)
            self._min_interval = max(0.05, 1.0 / qps)
        self.key_max_quality = key_info.get("maxQuality") or None
        self.key_default_quality = key_info.get("defaultQuality") or None
        logger.debug(
            f"[萌音点歌] 自检成功：key={self._key_masked()} qps={self.key_qps_limit} "
            f"maxQuality={self.key_max_quality} status={key_info.get('status')}"
        )
        return data

    async def search(
        self,
        keyword: str,
        limit: int = 5,
        source: str | None = None,
        quality: str | None = None,
    ) -> list[Track]:
        """聚合 / 指定平台搜索，返回 Track 列表（source 为空表示使用默认音源）。"""
        params: dict[str, str] = {"keyword": keyword, "limit": str(max(1, min(20, limit)))}
        if source and source != "all":
            params["source"] = source
        if quality:
            params["quality"] = quality
        data = await self._request("GET", "/search", params=params)
        items = data.get("list", []) if isinstance(data, dict) else []
        return [Track.from_api(item) for item in items if isinstance(item, dict)]

    async def lyric(self, music_id: str) -> dict | None:
        """取歌词（lyric/tlyric/rlyric/lxlyric），不存在返回 None。"""
        try:
            return await self._request("GET", f"/music/{music_id}/lyric")
        except ApiError as e:
            if e.code == 4040:
                return None
            raise

    async def pic(self, music_id: str) -> str | None:
        """取封面 URL，不存在返回 None。"""
        try:
            data = await self._request("GET", f"/music/{music_id}/pic")
        except ApiError as e:
            if e.code == 4040:
                return None
            raise
        return data.get("url") or None

    async def play_url(self, music_id: str, quality: str | None = None) -> dict:
        """取播放链接（本站临时链接）。4220（音质超限）由调用方收敛重取。

        Returns:
            dict: ``{url, quality, expiresAt}``
        """
        params = {"quality": quality} if quality else {}
        return await self._request("GET", f"/music/{music_id}/url", params=params)

    async def download(self, url: str, dest: Path) -> Path:
        """流式下载文件（临时链接 / 封面）到指定路径。

        Args:
            url: 下载地址（后端临时链接或封面 URL）。
            dest: 目标路径（含扩展名，调用方用 :func:`guess_audio_ext` 预先确定）。
        """
        if urlparse(url).scheme not in ("http", "https"):
            raise ApiError(4040, f"非法下载地址 scheme: {urlparse(url).scheme}", network=True)
        session = await self._ensure_session()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest.with_suffix(dest.suffix + ".part")
        try:
            async with session.get(url, proxy=self._proxy) as resp:
                if resp.status != 200:
                    logger.error(f"[萌音点歌] 下载失败：HTTP {resp.status}（文件 {dest.name}）")
                    raise ApiError(4040, f"下载失败：HTTP {resp.status}", network=True)
                async with aiofiles.open(tmp_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        await f.write(chunk)
        except aiohttp.ClientError as e:
            logger.error(f"[萌音点歌] 下载网络异常：{type(e).__name__}: {e}")
            raise ApiError(-1, f"{type(e).__name__}: {e}", network=True) from e
        except asyncio.TimeoutError as e:
            logger.error(f"[萌音点歌] 下载超时：{dest.name}")
            raise ApiError(-1, "download timeout", network=True) from e

        tmp_path.replace(dest)
        logger.debug(f"[萌音点歌] 下载完成：{dest.name}（{dest.stat().st_size} 字节）")
        return dest

    async def download_bytes(self, url: str, max_bytes: int = 5 * 1024 * 1024) -> bytes:
        """流式下载小文件到内存（如封面图），超过 max_bytes 抛 ApiError。"""
        if urlparse(url).scheme not in ("http", "https"):
            raise ApiError(4040, f"非法下载地址 scheme: {urlparse(url).scheme}", network=True)
        session = await self._ensure_session()
        buf = bytearray()
        try:
            async with session.get(url, proxy=self._proxy) as resp:
                if resp.status != 200:
                    raise ApiError(4040, f"下载失败：HTTP {resp.status}", network=True)
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise ApiError(4040, f"文件超过 {max_bytes} 字节上限", network=True)
        except aiohttp.ClientError as e:
            raise ApiError(-1, f"{type(e).__name__}: {e}", network=True) from e
        except asyncio.TimeoutError as e:
            raise ApiError(-1, "download timeout", network=True) from e
        return bytes(buf)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
