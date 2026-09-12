"""发送策略测试：音质收敛、多模式降级。"""

import sys
from pathlib import Path

from aiohttp import web
from astrbot_plugin_moe_music.core.api_client import ApiError, MusicApiClient
from astrbot_plugin_moe_music.core.config import PluginConfig
from astrbot_plugin_moe_music.core.model import Track
from astrbot_plugin_moe_music.core.sender import FILE_ONLY_MODES, DeliveryOptions, SongSender

# 复用 conftest stub 的消息组件与 aiocqhttp 事件类
_comp = sys.modules["astrbot.api.message_components"]
Record = _comp.Record
AiocqhttpMessageEvent = sys.modules[
    "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
].AiocqhttpMessageEvent


class MockEvent:
    """非 aiocqhttp 平台的 mock 事件。"""

    def __init__(self):
        self.sent = []
        self.stopped = False

    async def send(self, result):
        self.sent.append(result)

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    def stop_event(self):
        self.stopped = True


class MockAiocqEvent(AiocqhttpMessageEvent, MockEvent):
    """aiocqhttp 平台的 mock 事件（含可编程 bot.api）。"""

    def __init__(self, card_ok=True):
        MockEvent.__init__(self)
        self.is_private = False
        self.card_ok = card_ok

        class Api:
            def __init__(self, outer):
                self.outer = outer

            async def call_action(self, action, **payload):
                if not self.outer.card_ok:
                    raise RuntimeError("client not support")
                self.outer.sent.append(("card", action, payload))

        self.bot = type("Bot", (), {})()
        self.bot.api = Api(self)

    def is_private_chat(self):
        return self.is_private

    def get_sender_id(self):
        return "10001"

    def get_group_id(self):
        return "20001"


def make_track(**kwargs):
    data = {"id": "wy:1", "name": "晴天", "singer": "周杰伦", "source": "wy", "qualitys": ["128k", "320k"]}
    data.update(kwargs)
    return Track.from_api(data)


def ok_url(quality):
    return (200, {"code": 0, "message": "ok", "data": {"url": "http://h/api/temp/t", "quality": quality}})


TOO_HIGH = (422, {"code": 4220, "message": "quality too high", "data": None})


class FakeUrlBackend:
    """只提供 /music/:id/url 的假后端，记录每次请求的 quality。"""

    def __init__(self, script, audio_bytes=None):
        self.script = list(script)
        self.calls = []
        self.audio_bytes = audio_bytes  # 提供时可下载 /api/temp/t（file/record_local 测试用）
        app = web.Application()

        async def url_handler(request):
            self.calls.append(request.query.get("quality"))
            status, payload = self.script.pop(0)
            return web.json_response(payload, status=status)

        app.router.add_get("/api/v1/music/wy:1/url", url_handler)

        if self.audio_bytes is not None:
            body = self.audio_bytes

            async def audio_handler(request):
                return web.Response(body=body, content_type="audio/mpeg")

            app.router.add_get("/api/temp/t", audio_handler)
        self.runner = web.AppRunner(app)

    async def __aenter__(self):
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        if self.audio_bytes is not None:
            # 把响应中的占位临时链接改写为本地可达地址
            base = f"http://127.0.0.1:{port}"

            def _rewrite(item):
                status, payload = item
                if (
                    isinstance(payload, dict)
                    and isinstance(payload.get("data"), dict)
                    and payload["data"].get("url")
                ):
                    payload = {**payload, "data": {**payload["data"], "url": f"{base}/api/temp/t"}}
                return status, payload

            self.script = [_rewrite(i) for i in self.script]
        client = MusicApiClient(f"http://127.0.0.1:{port}", "sk-test", request_timeout=5)
        client.calls = self.calls  # 共享请求记录，测试可直接访问
        return client

    async def __aexit__(self, *args):
        await self.runner.cleanup()


def make_sender(api, modes=None, overrides=None):
    cfg = PluginConfig.from_astrbot_config(
        {"send_modes": modes or ["record_link", "text"], **(overrides or {})}
    )
    return SongSender(cfg, api, Path(__file__).parent / "_tmp_downloads")


class TestResolvePlayUrl:
    async def test_direct_success(self):
        async with FakeUrlBackend([ok_url("320k")]) as api:
            sender = make_sender(api)
            audio = await sender.resolve_play_url(make_track())
            assert audio["url"].endswith("/api/temp/t")
            assert sender.api.calls == ["320k"]

    async def test_quality_descend_on_422(self):
        async with FakeUrlBackend([TOO_HIGH, ok_url("128k")]) as api:
            sender = make_sender(api)
            sender.cfg.default_quality = "flac"  # 期望 flac，被 422 后收敛
            audio = await sender.resolve_play_url(make_track())
            assert audio["quality"] == "128k"
            assert len(sender.api.calls) == 2

    async def test_uses_key_max_quality_first(self):
        async with FakeUrlBackend([ok_url("128k")]) as api:
            api.key_max_quality = "128k"
            sender = make_sender(api)
            sender.cfg.default_quality = "flac"
            audio = await sender.resolve_play_url(make_track())
            assert audio["quality"] == "128k"
            assert sender.api.calls == ["128k"]  # 第一次请求就收敛，不浪费


class TestSendTrackFallback:
    async def test_record_link_success(self):
        async with FakeUrlBackend([ok_url("320k")]) as api:
            sender = make_sender(api, modes=["record_link"])
            event = MockEvent()
            ok = await sender.send_track(event, make_track())
            assert ok
            kind, chain = event.sent[0]
            assert kind == "chain"
            assert isinstance(chain[0], Record)

    async def test_card_fails_then_text(self):
        async with FakeUrlBackend([ok_url("320k")]) as api:
            sender = make_sender(api, modes=["card", "text"])
            event = MockAiocqEvent(card_ok=False)
            ok = await sender.send_track(event, make_track())
            assert ok
            # 卡片失败后降级到文本（text 模式走 chain_result）
            assert event.sent[0][0] == "chain"

    async def test_card_success_on_aiocqhttp(self):
        async with FakeUrlBackend([ok_url("320k")]) as api:
            # 封面接口失败不影响卡片发送
            async def _pic_fail(music_id):
                raise ApiError(4040, "no pic")

            api.pic = _pic_fail
            sender = make_sender(api, modes=["card"])
            event = MockAiocqEvent(card_ok=True)
            ok = await sender.send_track(event, make_track())
            assert ok
            assert event.sent[0][0] == "card"
            action, payload = event.sent[0][1], event.sent[0][2]
            assert action == "send_group_msg"
            music_seg = payload["message"][0]
            assert music_seg["type"] == "music"
            assert music_seg["data"]["type"] == "custom"
            assert music_seg["data"]["title"] == "晴天"
            assert music_seg["data"]["audio"].endswith("/api/temp/t")

    async def test_all_modes_fail_sends_hint(self):
        async with FakeUrlBackend([ok_url("320k")]) as api:
            sender = make_sender(api, modes=["record_link"])

            async def _dead_url(music_id, quality=None):
                raise ApiError(-1, "boom", network=True)

            api.play_url = _dead_url
            event = MockEvent()
            ok = await sender.send_track(event, make_track())
            assert not ok
            assert event.sent[-1] == ("plain", "网络开小差了，请稍后重试～")


class TestLocalFilename:
    """本地文件命名：歌名 - 歌手.扩展名，无随机后缀。"""

    async def test_clean_filename_no_hash(self, tmp_path):
        async with FakeUrlBackend([ok_url("flac")], audio_bytes=b"ID3fake-flac-audio") as api:
            sender = make_sender(api, modes=["file_local"])
            sender.download_dir = tmp_path
            event = MockEvent()
            ok = await sender.send_track(event, make_track(), record_ctx={"user_id": "1"})
            assert ok
            kind, chain = event.sent[0]
            seg = chain[0]
            assert seg.name == "晴天 - 周杰伦.flac"


class TestPublicUrlRouting:
    """发送给用户的链接用 public_base_url，插件下载用后端原链接。"""

    async def test_text_sends_public_url(self):
        async with FakeUrlBackend([ok_url("320k")]) as api:
            api._public_base_url = "https://music.example.com"  # 模拟 main.py 按 cfg 构造的客户端
            sender = make_sender(api, modes=["text"])
            event = MockEvent()
            ok = await sender.send_track(event, make_track())
            assert ok
            kind, chain = event.sent[0]
            text = str(getattr(chain[0], "args", "") or getattr(chain[0], "text", ""))
            assert "https://music.example.com/api/temp/t" in text
            assert "127.0.0.1" not in text

    async def test_download_uses_raw_url(self, tmp_path):
        # file_local 需要下载：raw url 指向测试服务器，改写后的公网地址不可达
        # 若下载误用公网地址会失败——以此证明下载走的是原链接
        async with FakeUrlBackend([ok_url("320k")], audio_bytes=b"ID3audio") as api:
            api._public_base_url = "https://music.example.com"
            sender = make_sender(api, modes=["file_local"])
            sender.download_dir = tmp_path
            event = MockEvent()
            ok = await sender.send_track(event, make_track())
            assert ok  # 下载成功 => 用的是 raw url
            seg = event.sent[0][1][0]
            assert seg.name == "晴天 - 周杰伦.mp3"

    async def test_card_audio_uses_public_url(self):
        async with FakeUrlBackend([ok_url("320k")]) as api:
            api._public_base_url = "https://music.example.com"

            async def _pic_ok(music_id):
                return "http://cdn.example.com/cover.jpg"

            api.pic = _pic_ok
            sender = make_sender(api, modes=["card"])
            event = MockAiocqEvent(card_ok=True)
            ok = await sender.send_track(event, make_track())
            assert ok
            payload = event.sent[0][2]
            data = payload["message"][0]["data"]
            assert data["audio"] == "https://music.example.com/api/temp/t"
            assert data["url"] == "https://music.example.com/api/temp/t"


class TestDeliveryOptions:
    """「点歌文件」指令的独立发送策略（v0.11.0）。"""

    async def test_quality_comes_from_options(self, tmp_path):
        """音质取 options.quality（file_quality），不受 default_quality 影响。"""
        async with FakeUrlBackend([ok_url("flac")], audio_bytes=b"ID3audio") as api:
            sender = make_sender(api, modes=["record_link"], overrides={"default_quality": "320k"})
            sender.download_dir = tmp_path
            ok = await sender.send_track(
                MockEvent(),
                make_track(),
                options=DeliveryOptions(quality="flac", modes=list(FILE_ONLY_MODES)),
            )
            assert ok
            assert api.calls == ["flac"]  # 请求的是文件音质，而非配置里的 320k

    async def test_sends_file_component(self, tmp_path):
        """固定走 file_local：发出的是文件本体（File 组件），不是卡片/语音。"""
        async with FakeUrlBackend([ok_url("flac")], audio_bytes=b"ID3audio") as api:
            sender = make_sender(api, modes=["record_link", "text"])
            sender.download_dir = tmp_path
            event = MockEvent()
            ok = await sender.send_track(
                event,
                make_track(),
                options=DeliveryOptions(quality="flac", modes=list(FILE_ONLY_MODES)),
            )
            assert ok
            assert event.sent[0][0] == "chain"
            assert type(event.sent[0][1][0]).__name__ == "File"

    async def test_embed_metadata_toggle(self, tmp_path):
        """embed_metadata=False 时不写标签；True 时才嵌入封面/歌词。"""
        async with FakeUrlBackend([ok_url("flac"), ok_url("flac")], audio_bytes=b"ID3audio") as api:
            sender = make_sender(api, modes=["file_local"])
            sender.download_dir = tmp_path
            embedded: list = []

            async def _spy(path, track, cover_url, timings):
                embedded.append(path)

            sender._embed_track_metadata = _spy

            off = await sender.send_track(
                MockEvent(),
                make_track(),
                options=DeliveryOptions(quality="flac", modes=["file_local"], embed_metadata=False),
            )
            assert off and embedded == []

            on = await sender.send_track(
                MockEvent(),
                make_track(),
                options=DeliveryOptions(quality="flac", modes=["file_local"], embed_metadata=True),
            )
            assert on and len(embedded) == 1

    async def test_no_link_fallback_and_custom_hint(self):
        """file_local 失败即失败：不降级为链接，并给出文件专用提示。"""
        async with FakeUrlBackend([ok_url("flac")]) as api:  # 无 audio_bytes → 下载失败
            sender = make_sender(api, modes=["text"])
            event = MockEvent()
            ok = await sender.send_track(
                event,
                make_track(),
                options=DeliveryOptions(
                    quality="flac",
                    modes=list(FILE_ONLY_MODES),
                    fail_hint="文件下载失败了，换一首或稍后再试试吧～",
                ),
            )
            assert not ok
            assert event.sent == [("plain", "文件下载失败了，换一首或稍后再试试吧～")]

    async def test_defaults_unchanged_without_options(self, tmp_path):
        """不传 options 时保持普通点歌行为：用配置的音质与发送方式。"""
        async with FakeUrlBackend([ok_url("320k")], audio_bytes=b"ID3audio") as api:
            sender = make_sender(api, modes=["file_local"], overrides={"default_quality": "320k"})
            sender.download_dir = tmp_path
            event = MockEvent()
            ok = await sender.send_track(event, make_track())
            assert ok
            assert api.calls == ["320k"]
            assert type(event.sent[0][1][0]).__name__ == "File"
