"""发送策略测试：音质收敛、多模式降级。"""

import sys
from pathlib import Path

from aiohttp import web
from astrbot_plugin_moe_music.core.api_client import ApiError, MusicApiClient
from astrbot_plugin_moe_music.core.config import PluginConfig
from astrbot_plugin_moe_music.core.model import Track
from astrbot_plugin_moe_music.core.sender import SongSender

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

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        app = web.Application()

        async def url_handler(request):
            self.calls.append(request.query.get("quality"))
            status, payload = self.script.pop(0)
            return web.json_response(payload, status=status)

        app.router.add_get("/api/v1/music/wy:1/url", url_handler)
        self.runner = web.AppRunner(app)

    async def __aenter__(self):
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        client = MusicApiClient(f"http://127.0.0.1:{port}", "sk-test", request_timeout=5)
        client.calls = self.calls  # 共享请求记录，测试可直接访问
        return client

    async def __aexit__(self, *args):
        await self.runner.cleanup()


def make_sender(api, modes=None):
    cfg = PluginConfig.from_astrbot_config({"send_modes": modes or ["record_link", "text"]})
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
