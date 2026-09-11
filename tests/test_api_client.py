"""MusicApiClient 测试：用 aiohttp 内置测试服务器模拟 lx_music_api 后端。"""

import asyncio

import aiohttp
import pytest
from aiohttp import web
from astrbot_plugin_moe_music.core.api_client import ApiError, MusicApiClient, guess_audio_ext


class FakeBackend:
    """模拟后端：记录请求，按脚本返回响应。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.handlers: dict[str, list] = {}  # path -> [callable(req) -> (status, payload, headers)]

    def route(self, path, handler):
        self.handlers.setdefault(path, []).append(handler)

    async def serve(self, handler_map_extra=None):
        app = web.Application()

        async def _dispatch(request: web.Request) -> web.Response:
            self.requests.append(
                {
                    "method": request.method,
                    "path": request.path,
                    "query": dict(request.query),
                    "headers": dict(request.headers),
                    "json": await request.json() if request.can_read_body else None,
                }
            )
            queue = self.handlers.get(request.path)
            if not queue:
                return web.json_response({"code": 0, "message": "ok", "data": {}})
            result = queue.pop(0)
            status, payload, headers = result
            return web.json_response(payload, status=status, headers=headers)

        app.router.add_route("*", "/{tail:.*}", _dispatch)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, f"http://127.0.0.1:{port}"


@pytest.fixture()
async def backend():
    fb = FakeBackend()
    runner, base_url = await fb.serve()
    yield fb, base_url
    await runner.cleanup()


def make_client(base_url: str) -> MusicApiClient:
    return MusicApiClient(base_url=base_url, api_key="sk-test1234567890", request_timeout=5)


class TestAuthAndParsing:
    async def test_bearer_token_and_ok_response(self, backend):
        fb, base_url = backend
        fb.route(
            "/api/v1/me",
            (200, {"code": 0, "message": "ok", "data": {"apiKey": {"name": "bot", "qpsLimit": 10}}}, {}),
        )
        client = make_client(base_url)
        data = await client.me()
        assert data["apiKey"]["name"] == "bot"
        req = fb.requests[0]
        assert req["headers"]["Authorization"] == "Bearer sk-test1234567890"

    async def test_qps_updates_throttle_interval(self, backend):
        fb, base_url = backend
        fb.route("/api/v1/me", (200, {"code": 0, "message": "ok", "data": {"apiKey": {"qpsLimit": 10}}}, {}))
        client = make_client(base_url)
        await client.me()
        assert client.key_qps_limit == 10
        assert client._min_interval == pytest.approx(0.1)

    async def test_business_error_raises_api_error(self, backend):
        fb, base_url = backend
        fb.route("/api/v1/search", (400, {"code": 4010, "message": "key invalid", "data": None}, {}))
        client = make_client(base_url)
        with pytest.raises(ApiError) as exc_info:
            await client.search("晴天")
        assert exc_info.value.code == 4010
        assert exc_info.value.user_hint == "音乐服务配置有误，请联系管理员～"

    async def test_http_error_body_parsed(self, backend):
        fb, base_url = backend
        fb.route("/api/v1/search", (422, {"code": 4220, "message": "quality too high", "data": None}, {}))
        client = make_client(base_url)
        with pytest.raises(ApiError) as exc_info:
            await client.search("晴天")
        assert exc_info.value.code == 4220


class TestRateLimit:
    async def test_429_retry_once_with_retry_after(self, backend):
        fb, base_url = backend
        fb.route(
            "/api/v1/search",
            (429, {"code": 4290, "message": "rate limited", "data": None}, {"Retry-After": "0.2"}),
        )
        fb.route(
            "/api/v1/search",
            (
                200,
                {
                    "code": 0,
                    "message": "ok",
                    "data": {"total": 1, "list": [{"id": "wy:1", "name": "晴天", "source": "wy"}]},
                },
                {},
            ),
        )
        client = make_client(base_url)
        tracks = await client.search("晴天")
        assert len(tracks) == 1
        assert tracks[0].name == "晴天"
        assert len(fb.requests) == 2  # 重试一次

    async def test_429_twice_fails(self, backend):
        fb, base_url = backend
        payload = (429, {"code": 4290, "message": "rate limited", "data": None}, {"Retry-After": "0.1"})
        fb.route("/api/v1/search", payload)
        fb.route("/api/v1/search", payload)
        client = make_client(base_url)
        with pytest.raises(ApiError) as exc_info:
            await client.search("晴天")
        assert exc_info.value.code == 4290
        assert len(fb.requests) == 2  # 只重试一次


class TestNetworkErrors:
    async def test_connection_error_becomes_network_api_error(self):
        # 指向一个几乎不可能有服务的端口
        client = MusicApiClient("http://127.0.0.1:1", "sk-test", request_timeout=3)
        with pytest.raises(ApiError) as exc_info:
            await client.search("晴天")
        assert exc_info.value.network is True
        assert "网络开小差" in exc_info.value.user_hint

    async def test_timeout(self, backend):
        fb, base_url = backend

        async def slow_handler(request):
            await asyncio.sleep(1.0)
            return web.json_response({"code": 0, "message": "ok", "data": {}})

        # 直接注册慢响应（绕过 FakeBackend 队列机制）
        from aiohttp import web as _web

        app = _web.Application()
        app.router.add_get("/api/v1/search", slow_handler)
        runner = _web.AppRunner(app)
        await runner.setup()
        site = _web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            client = MusicApiClient(f"http://127.0.0.1:{port}", "sk-test", request_timeout=3)
            # aiohttp total timeout 3s，慢响应 1s 内不会超时 —— 用更短超时验证
            client._timeout = aiohttp.ClientTimeout(total=0.2)
            with pytest.raises(ApiError) as exc_info:
                await client.search("晴天")
            assert exc_info.value.network is True
        finally:
            await runner.cleanup()


class TestEndpoints:
    async def test_search_params(self, backend):
        fb, base_url = backend
        fb.route("/api/v1/search", (200, {"code": 0, "message": "ok", "data": {"total": 0, "list": []}}, {}))
        client = make_client(base_url)
        await client.search("晴天", limit=5, source="wy", quality="320k")
        query = fb.requests[0]["query"]
        assert query["keyword"] == "晴天"
        assert query["limit"] == "5"
        assert query["source"] == "wy"
        assert query["quality"] == "320k"

    async def test_search_all_omits_source(self, backend):
        fb, base_url = backend
        fb.route("/api/v1/search", (200, {"code": 0, "message": "ok", "data": {"total": 0, "list": []}}, {}))
        client = make_client(base_url)
        await client.search("晴天", source="all")
        assert "source" not in fb.requests[0]["query"]

    async def test_lyric_404_returns_none(self, backend):
        fb, base_url = backend
        fb.route("/api/v1/music/wy:1/lyric", (404, {"code": 4040, "message": "not found", "data": None}, {}))
        client = make_client(base_url)
        assert await client.lyric("wy:1") is None

    async def test_pic_returns_url(self, backend):
        fb, base_url = backend
        fb.route(
            "/api/v1/music/wy:1/pic",
            (200, {"code": 0, "message": "ok", "data": {"url": "http://x/cover.jpg"}}, {}),
        )
        client = make_client(base_url)
        assert await client.pic("wy:1") == "http://x/cover.jpg"

    async def test_play_url(self, backend):
        fb, base_url = backend
        fb.route(
            "/api/v1/music/wy:1/url",
            (
                200,
                {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "url": "http://127.0.0.1:3000/api/temp/tok",
                        "quality": "320k",
                        "expiresAt": "2026-01-01",
                    },
                },
                {},
            ),
        )
        client = make_client(base_url)
        data = await client.play_url("wy:1", "320k")
        assert data["url"].endswith("/api/temp/tok")
        assert fb.requests[0]["query"]["quality"] == "320k"

    async def test_download_streams_to_file(self, backend):
        fb, base_url = backend

        # FakeBackend 的 dispatch 返回 JSON，这里直接放个二进制路由
        from aiohttp import web as _web

        async def binary(request):
            return _web.Response(body=b"ID3audio-bytes", content_type="audio/mpeg")

        app = _web.Application()
        app.router.add_get("/api/temp/tok", binary)
        runner = _web.AppRunner(app)
        await runner.setup()
        site = _web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            client = make_client(f"http://127.0.0.1:{port}")
            import tempfile
            from pathlib import Path

            dest = Path(tempfile.mkdtemp()) / "song.mp3"
            got = await client.download(f"http://127.0.0.1:{port}/api/temp/tok", dest)
            assert got.read_bytes() == b"ID3audio-bytes"
        finally:
            await runner.cleanup()


class TestGuessExt:
    def test_by_quality(self):
        assert guess_audio_ext("flac") == ".flac"
        assert guess_audio_ext("flac24bit") == ".flac"
        assert guess_audio_ext("wav") == ".wav"
        assert guess_audio_ext("320k") == ".mp3"

    def test_by_content_type(self):
        assert guess_audio_ext("320k", "audio/flac; charset=utf-8") == ".flac"
        assert guess_audio_ext("320k", "audio/mpeg") == ".mp3"
