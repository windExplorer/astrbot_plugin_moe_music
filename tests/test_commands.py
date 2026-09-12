"""点歌业务逻辑测试（MoeMusicService）与插件入口冒烟测试。"""

import sys
from pathlib import Path

from aiohttp import web
from astrbot_plugin_moe_music.core.api_client import MusicApiClient
from astrbot_plugin_moe_music.core.commands import MoeMusicService
from astrbot_plugin_moe_music.core.config import PluginConfig
from astrbot_plugin_moe_music.core.lyrics_render import LyricsRenderer
from astrbot_plugin_moe_music.core.model import Track
from astrbot_plugin_moe_music.core.sender import SongSender


class MockEvent:
    def __init__(
        self,
        message_str="",
        sender_id="10001",
        umo="qq:GroupMessage:20001",
        group_id="20001",
        group_name="测试群",
        is_admin=False,
        private=False,
    ):
        self.message_str = message_str
        self.sent = []
        self.stopped = False
        self.unified_msg_origin = umo
        self._sender_id = sender_id
        self._group_id = group_id
        self._group_name = group_name
        self._is_admin = is_admin
        self._private = private

    async def send(self, result):
        self.sent.append(result)

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    def stop_event(self):
        self.stopped = True

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "测试用户"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return self._group_id

    def is_private_chat(self):
        return self._private

    def is_admin(self):
        return self._is_admin

    async def get_group(self):
        from types import SimpleNamespace

        return SimpleNamespace(group_id=self._group_id, group_name=self._group_name)


def track_json(i, cover=None):
    return {
        "id": f"wy:{i}",
        "name": f"歌曲{i}",
        "singer": f"歌手{i}",
        "album": "专辑",
        "duration": 200,
        "source": "wy",
        "qualitys": ["128k", "320k"],
        "coverUrl": cover,
    }


class FakeBackend:
    def __init__(self, search_result, lyric_text="", extra_routes=None):
        self.search_result = search_result
        self.lyric_text = lyric_text
        self.requests = []
        app = web.Application()

        for path, handler in (extra_routes or {}).items():
            app.router.add_get(path, handler)

        async def search(request):
            self.requests.append(("search", dict(request.query)))
            return web.json_response(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {"total": len(self.search_result), "list": self.search_result},
                }
            )

        async def url(request):
            self.requests.append(("url", {}))
            return web.json_response(
                {"code": 0, "message": "ok", "data": {"url": "http://h/api/temp/t", "quality": "320k"}}
            )

        async def lyric(request):
            self.requests.append(("lyric", {}))
            if not self.lyric_text:
                return web.json_response({"code": 4040, "message": "none", "data": None}, status=404)
            return web.json_response({"code": 0, "message": "ok", "data": {"lyric": self.lyric_text}})

        async def me(request):
            self.requests.append(("me", {}))
            return web.json_response(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "authenticatedAs": "apikey",
                        "apiKey": {
                            "name": "测试Key",
                            "status": "active",
                            "qpsLimit": 5,
                            "maxQuality": "320k",
                            "allowedSources": ["wy", "tx"],
                        },
                    },
                }
            )

        app.router.add_get("/api/v1/search", search)
        app.router.add_get("/api/v1/music/wy:{i}/url", url)
        app.router.add_get("/api/v1/music/wy:{i}/lyric", lyric)
        app.router.add_get("/api/v1/me", me)
        self.runner = web.AppRunner(app)

    async def __aenter__(self):
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        client = MusicApiClient(f"http://127.0.0.1:{port}", "sk-test", request_timeout=5)
        client.calls = self.requests
        client.backend = self  # 暴露 FakeBackend 本体，测试可动态注册额外路由
        return client

    async def __aexit__(self, *args):
        await self.runner.cleanup()


def make_service(api, config_overrides=None):
    from astrbot_plugin_moe_music.core.access import AccessController
    from astrbot_plugin_moe_music.core.songlist_render import SonglistRenderer

    cfg = PluginConfig.from_astrbot_config(
        {"api_key": "sk-test", "send_modes": ["text"], "timeout": 5, **(config_overrides or {})}
    )
    sender = SongSender(cfg, api, Path(__file__).parent / "_tmp_downloads")
    font = Path(__file__).resolve().parent.parent / "fonts" / "simhei.ttf"
    renderer = LyricsRenderer(font)
    songlist = SonglistRenderer(font)
    return MoeMusicService(cfg, api, sender, renderer, songlist, access=AccessController(cfg))


class TestSongRequest:
    async def test_no_result_hint(self):
        async with FakeBackend(search_result=[]) as api:
            service = make_service(api)
            event = MockEvent()
            await service.handle_song_request(event, "不存在歌曲xyz")
            assert event.sent[-1] == ("plain", "没有找到相关歌曲，换个关键词试试吧～")

    async def test_single_result_direct_send(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service = make_service(api)
            event = MockEvent()
            await service.handle_song_request(event, "晴天")
            # 直接发送，不发候选列表
            assert len(event.sent) == 1
            kind, chain = event.sent[0]
            assert kind == "chain"  # text 模式发送的链接消息链
            assert any(
                "http" in getattr(seg, "text", "") or "http" in str(getattr(seg, "args", "")) for seg in chain
            )

    async def test_multi_result_shows_list(self):
        async with FakeBackend(search_result=[track_json(1), track_json(2), track_json(3)]) as api:
            service = make_service(api)
            event = MockEvent()
            await service.handle_song_request(event, "晴天")
            # conftest stub 的 session_waiter 立即返回；应发出候选列表
            list_msg = event.sent[0][1]
            assert "回复序号点歌" in list_msg
            assert "1. 歌曲1 - 歌手1" in list_msg
            assert "3. 歌曲3 - 歌手3" in list_msg

    async def test_index_hint_direct_send(self):
        async with FakeBackend(search_result=[track_json(1), track_json(2)]) as api:
            service = make_service(api)
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=2)
            assert len(event.sent) == 1  # 直接发送第 2 首，不展示列表


class TestLyrics:
    async def test_lyrics_image_sent(self):
        lrc = "[00:01.00]故事的小黄花\n[00:02.00]从出生那年就飘着"
        async with FakeBackend(search_result=[track_json(1)], lyric_text=lrc) as api:
            service = make_service(api)
            event = MockEvent()
            ok = await service.handle_lyrics_request(event, "晴天")
            assert ok
            assert event.sent[0][0] == "chain"  # 图片 + 说明

    async def test_lyrics_empty_hint(self):
        async with FakeBackend(search_result=[track_json(1)], lyric_text="") as api:
            service = make_service(api)
            event = MockEvent()
            ok = await service.handle_lyrics_request(event, "晴天")
            assert not ok
            assert event.sent[-1] == ("plain", "这首歌暂无歌词～")

    async def test_lyrics_fallback_text_on_render_error(self):
        lrc = "[00:01.00]正常歌词"
        async with FakeBackend(search_result=[track_json(1)], lyric_text=lrc) as api:
            service = make_service(api)

            async def _boom(*args, **kwargs):
                raise RuntimeError("render fail")

            service.lyrics_renderer.render_async = _boom
            event = MockEvent()
            ok = await service.handle_lyrics_request(event, "晴天")
            assert ok
            assert event.sent[-1][0] == "plain"
            assert "正常歌词" in event.sent[-1][1]


class TestSelfTest:
    async def test_self_test_ok(self):
        async with FakeBackend(search_result=[]) as api:
            service = make_service(api)
            event = MockEvent()
            await service.handle_self_test(event)
            text = event.sent[0][1]
            assert "连接正常" in text
            assert "测试Key" in text
            assert "320k" in text
            assert "wy、tx" in text


class TestLlmTools:
    async def test_play_song_returns_summary(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service = make_service(api)
            event = MockEvent()
            result = await service.llm_play_song(event, "晴天")
            assert "已为用户播放" in result
            assert "歌曲1" in result

    async def test_play_song_no_result(self):
        async with FakeBackend(search_result=[]) as api:
            service = make_service(api)
            event = MockEvent()
            result = await service.llm_play_song(event, "不存在的歌")
            assert "没有找到" in result

    async def test_query_lyrics_returns_summary(self):
        lrc = "[00:01.00]歌词内容"
        async with FakeBackend(search_result=[track_json(1)], lyric_text=lrc) as api:
            service = make_service(api)
            event = MockEvent()
            result = await service.llm_query_lyrics(event, "晴天")
            assert "已为用户展示" in result


class TestPluginSmoke:
    def test_plugin_instantiates(self):
        from astrbot_plugin_moe_music.main import MoeMusicPlugin

        cfg = {
            "api_base_url": "http://127.0.0.1:3000",
            "api_key": "sk-test1234567890",
            "send_modes": ["card(音乐卡片)", "text(文本链接)"],
        }
        plugin = MoeMusicPlugin(context=None, config=cfg)
        assert plugin.config["api_key"] == "sk-test1234567890"  # WebUI 配置页依赖
        assert plugin.cfg.api_key == "sk-test1234567890"
        assert plugin.cfg.send_modes == ["card", "text"]
        assert callable(plugin.song_command)
        assert callable(plugin.lyrics_command)
        assert callable(plugin.self_test_command)
        assert callable(plugin.play_song_by_name)
        assert callable(plugin.query_lyrics_by_name)

    async def test_song_command_usage_hint(self):
        from astrbot_plugin_moe_music.main import MoeMusicPlugin

        plugin = MoeMusicPlugin(context=None, config={"api_base_url": "http://x", "api_key": "sk-test"})
        event = MockEvent(message_str="点歌")
        await plugin.song_command(event)
        assert any("用法" in item[1] for item in event.sent if item[0] == "plain")
        assert event.stopped

    async def test_song_command_with_index(self):
        from astrbot_plugin_moe_music.main import MoeMusicPlugin

        async with FakeBackend(search_result=[track_json(1), track_json(2)]) as api:
            plugin = MoeMusicPlugin(context=None, config={"api_base_url": "", "api_key": "sk-test"})
            # 复用同一个 backend 的地址
            plugin.cfg.api_base_url = api._base_url
            plugin.api._base_url = api._base_url
            event = MockEvent(message_str="点歌 晴天 2")
            await plugin.song_command(event)
            assert event.stopped
            kinds = [item[0] for item in event.sent]
            assert "plain" in kinds or "chain" in kinds


def _tiny_jpeg_bytes() -> bytes:
    """生成一张 8x8 纯色 JPEG 作为假封面。"""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 60, 60)).save(buf, format="JPEG")
    return buf.getvalue()


class TestSelectionDisplay:
    """候选列表显示方式：文本 / 图片菜单。"""

    async def test_image_mode_sends_image(self):
        from aiohttp import web as _web

        cover = _tiny_jpeg_bytes()

        async def cover_handler(request):
            return _web.Response(body=cover, content_type="image/jpeg")

        async with FakeBackend(
            search_result=[track_json(1, "http://h/1.jpg"), track_json(2, "http://h/2.jpg")],
            extra_routes={"/1.jpg": cover_handler, "/2.jpg": cover_handler},
        ) as api:
            service = make_service(api, {"selection_display": "image(图片菜单)"})
            event = MockEvent()
            await service.handle_song_request(event, "晴天")
            # 应发送图片消息链（无纯文本候选列表）
            kinds = [item[0] for item in event.sent]
            assert "chain" in kinds
            img_comp = sys.modules["astrbot.api.message_components"].Image
            chain = next(item[1] for item in event.sent if item[0] == "chain")
            assert any(isinstance(seg, img_comp) for seg in chain)

    async def test_image_mode_falls_back_on_render_error(self):
        async with FakeBackend(search_result=[track_json(1), track_json(2)]) as api:
            service = make_service(api, {"selection_display": "image"})

            async def _boom(*args, **kwargs):
                raise RuntimeError("render fail")

            service.songlist_renderer.render_async = _boom
            event = MockEvent()
            await service.handle_song_request(event, "晴天")
            # 渲染失败回退文本列表
            kinds = [item[0] for item in event.sent]
            assert "plain" in kinds
            text = next(item[1] for item in event.sent if item[0] == "plain")
            assert "回复序号点歌" in text

    async def test_text_mode_default(self):
        async with FakeBackend(search_result=[track_json(1, "http://h/1.jpg"), track_json(2)]) as api:
            service = make_service(api)
            event = MockEvent()
            await service.handle_song_request(event, "晴天")
            kinds = [item[0] for item in event.sent]
            assert kinds == ["plain"]  # 默认文本，不发图片


class TestAccessAndRecords:
    """访问控制拦截与记录落库（v0.3.0）。"""

    async def test_whitelist_blocks_group(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service = make_service(api, {"whitelist_groups": ["其他群号"]})
            event = MockEvent()  # 默认群 20001 不在白名单
            await service.handle_song_request(event, "晴天")
            assert event.sent[-1] == ("plain", "本群暂未开放点歌功能哦～")
            # 未发生后端搜索
            assert api.calls == []

    async def test_whitelist_allows_listed_group(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service = make_service(api, {"whitelist_groups": ["20001"]})
            event = MockEvent()
            await service.handle_song_request(event, "晴天")
            kinds = [item[0] for item in event.sent]
            assert "chain" in kinds  # 正常发送

    async def test_admin_bypasses_whitelist(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service = make_service(api, {"whitelist_groups": ["其他群号"]})
            event = MockEvent(is_admin=True)
            await service.handle_song_request(event, "晴天")
            kinds = [item[0] for item in event.sent]
            assert "chain" in kinds  # 管理员不受限

    async def test_blacklist_blocks_user_but_not_others(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service = make_service(api, {"blacklist_users": ["10001"]})
            event = MockEvent()  # sender_id=10001
            await service.handle_song_request(event, "晴天")
            assert event.sent[-1] == ("plain", "您暂时没有点歌权限哦～")

            event2 = MockEvent(sender_id="10002")
            await service.handle_song_request(event2, "晴天")
            kinds = [item[0] for item in event2.sent]
            assert "chain" in kinds

    async def test_records_written_on_play(self, tmp_path):
        from astrbot_plugin_moe_music.core.storage import RecordStore

        store = RecordStore(tmp_path / "records.db")
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service = make_service(api)
            service.store = store
            service.sender.store = store
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1, command="点歌")

        _, play_n = await store.counts()
        assert play_n == 1
        row = store._conn.execute(
            "SELECT user_id, user_name, group_id, group_name, keyword, selection_type, "
            "trigger_type, command, track_id, track_name, quality, send_mode "
            "FROM play_records"
        ).fetchone()
        assert row[0] == "10001"
        assert row[1] == "测试用户"
        assert row[2] == "20001"
        assert row[3] == "测试群"
        assert row[4] == "晴天"
        assert row[5] == "direct_index"
        assert row[6] == "command"
        assert row[7] == "点歌"
        assert row[8] == "wy:1"
        assert row[9] == "歌曲1"
        assert row[10] == "320k"
        assert row[11] == "text"

    async def test_search_records_written(self, tmp_path):
        from astrbot_plugin_moe_music.core.storage import RecordStore

        store = RecordStore(tmp_path / "records.db")
        async with FakeBackend(search_result=[track_json(1), track_json(2)]) as api:
            service = make_service(api)
            service.store = store
            event = MockEvent()
            await service.handle_song_request(event, "晴天")

        search_n, _ = await store.counts()
        assert search_n == 1
        row = store._conn.execute(
            "SELECT keyword, success, result_count, group_name, user_id FROM search_records"
        ).fetchone()
        assert row[0] == "晴天"
        assert row[1] == 1
        assert row[2] == 2
        assert row[3] == "测试群"
        assert row[4] == "10001"

    async def test_search_record_error_logged(self, tmp_path):
        from astrbot_plugin_moe_music.core.storage import RecordStore

        store = RecordStore(tmp_path / "records.db")
        async with FakeBackend(search_result=[]) as api:
            service = make_service(api)
            service.store = store

            async def _dead(keyword, limit, source=None, quality=None):
                from astrbot_plugin_moe_music.core.api_client import ApiError

                raise ApiError(4290, "rate limited")

            service.api.search = _dead
            event = MockEvent()
            await service.handle_song_request(event, "晴天")

        row = store._conn.execute("SELECT success, error_code FROM search_records").fetchone()
        assert row[0] == 0
        assert row[1] == 4290


class TestLyricsAttachment:
    """enable_lyrics：点歌成功后附加歌词（v0.3.1 修复）。"""

    def _make_with_lyrics(self, api, lyric_text, store=None):
        service = make_service(api, {"enable_lyrics": True})
        if store:
            service.store = store
            service.sender.store = store
        # FakeBackend 的 lyric 路由由 lyric_text 参数控制
        return service

    async def test_append_lyrics_image_after_song(self):
        lrc = "[00:01.00]故事的小黄花\n[00:02.00]从出生那年就飘着"
        async with FakeBackend(search_result=[track_json(1)], lyric_text=lrc) as api:
            service = self._make_with_lyrics(api, lrc)
            event = MockEvent()
            ok = await service.handle_song_request(event, "晴天", index_hint=1)
            assert ok is None  # 命令路径无返回值
            kinds = [item[0] for item in event.sent]
            # 歌（chain: text 模式）+ 歌词图片（chain: Image）
            assert kinds.count("chain") == 2
            img_comp = sys.modules["astrbot.api.message_components"].Image
            lyric_chain = event.sent[-1][1]
            assert any(isinstance(seg, img_comp) for seg in lyric_chain)

    async def test_append_lyrics_silent_when_empty(self):
        async with FakeBackend(search_result=[track_json(1)], lyric_text="") as api:
            service = self._make_with_lyrics(api, "")
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            kinds = [item[0] for item in event.sent]
            # 只有歌本身；歌词为空时静默，不发"暂无歌词"提示
            assert kinds == ["chain"]

    async def test_no_lyrics_when_disabled(self):
        lrc = "[00:01.00]歌词"
        async with FakeBackend(search_result=[track_json(1)], lyric_text=lrc) as api:
            service = make_service(api, {"enable_lyrics": False})
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            kinds = [item[0] for item in event.sent]
            assert kinds == ["chain"]  # 只发歌，无歌词


class TestEmptyRetry:
    """搜索空结果自动重试（后端音源瞬时冷却缓解）。"""

    async def test_retry_on_empty_then_success(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service = make_service(api)
            calls = {"n": 0}

            async def flaky(keyword, limit, source=None, quality=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    return []  # 第一次空（音源冷却）
                return [Track.from_api(track_json(1))]

            service.api.search = flaky
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            assert calls["n"] == 2  # 重试过一次
            assert any("chain" in item[0] for item in event.sent)  # 最终发送成功

    async def test_no_infinite_retry(self):
        async with FakeBackend(search_result=[]) as api:
            service = make_service(api)
            calls = {"n": 0}

            async def always_empty(keyword, limit, source=None, quality=None):
                calls["n"] += 1
                return []

            service.api.search = always_empty
            event = MockEvent()
            await service.handle_song_request(event, "晴天")
            assert calls["n"] == 2  # 最多两次（首次 + 重试）
            assert event.sent[-1] == ("plain", "没有找到相关歌曲，换个关键词试试吧～")


def store_row(service):
    return service.store._conn.execute("SELECT result_count FROM search_records").fetchall()[-1]


class TestPickerRejection:
    """等待选号期间再次点歌：提示进行中，不进任务队列。"""

    async def test_new_song_command_during_wait_gets_hint(self):
        async with FakeBackend(search_result=[track_json(1), track_json(2)]) as api:
            service = make_service(api)
            event = MockEvent(message_str="点歌 别的歌")  # 等待中又发起点歌
            await service.handle_song_request(event, "晴天")
            # stub 的 session_waiter 会立即消费一条消息：应收到「进行中」提示
            hints = [item[1] for item in event.sent if item[0] == "plain" and "进行中" in item[1]]
            assert hints, "应提示还有一单点歌进行中"


class _AiocqMockEvent(MockEvent):
    """带 OneBot bot 的 mock 事件：记录 call_action 调用与返回 message_id。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.actions: list[tuple] = []

        class Api:
            def __init__(self, outer):
                self.outer = outer

            async def call_action(self, action, **payload):
                self.outer.actions.append((action, payload))
                if action in ("send_group_msg", "send_private_msg"):
                    return {"message_id": 4321}
                return {}

        self.bot = type("Bot", (), {})()
        self.bot.api = Api(self)


class TestRecallCandidate:
    """选歌结束后自动撤回候选列表（v0.8.0）。"""

    def _make(self, api, **cfg):
        from astrbot_plugin_moe_music.core.access import AccessController
        from astrbot_plugin_moe_music.core.songlist_render import SonglistRenderer

        c = PluginConfig.from_astrbot_config(
            {"api_key": "sk-test", "send_modes": ["text"], "timeout": 5, **cfg}
        )
        sender = SongSender(c, api, Path(__file__).parent / "_tmp_downloads")
        font = Path(__file__).resolve().parent.parent / "fonts" / "simhei.ttf"
        service = MoeMusicService(
            c, api, sender, LyricsRenderer(font), SonglistRenderer(font), access=AccessController(c)
        )
        return service

    async def test_recall_after_pick(self):
        async with FakeBackend(search_result=[track_json(1), track_json(2)]) as api:
            service = self._make(api)
            service.sender.store = None
            event = _AiocqMockEvent(message_str="2")  # 等待中直接回序号 2
            await service.handle_song_request(event, "晴天")
            actions = [a for a, _ in event.actions]
            assert actions.count("delete_msg") == 1  # 候选列表已撤回
            # 候选列表经 call_action 直发（拿 message_id）
            assert "send_group_msg" in actions

    async def test_recall_on_cancel(self):
        async with FakeBackend(search_result=[track_json(1), track_json(2)]) as api:
            service = self._make(api)
            event = _AiocqMockEvent(message_str="取消")
            await service.handle_song_request(event, "晴天")
            actions = [a for a, _ in event.actions]
            assert "delete_msg" in actions

    async def test_no_recall_when_disabled(self):
        async with FakeBackend(search_result=[track_json(1), track_json(2)]) as api:
            service = self._make(api, recall_candidate=False)
            event = _AiocqMockEvent(message_str="1")
            await service.handle_song_request(event, "晴天")
            actions = [a for a, _ in event.actions]
            assert "delete_msg" not in actions  # 开关关闭：不撤回
            # 候选列表也不必走 call_action（走通用发送）
            assert "send_group_msg" not in actions

    async def test_non_aiocqhttp_skips_recall(self):
        async with FakeBackend(search_result=[track_json(1), track_json(2)]) as api:
            service = self._make(api)
            event = MockEvent(message_str="1")  # 无 bot 的通用事件
            await service.handle_song_request(event, "晴天")
            # 不报错、正常发送即可（无撤回能力）
            assert any("chain" in item[0] or "plain" in item[0] for item in event.sent)
