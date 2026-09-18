"""分享识别测试：卡片/链接解析 + 分享自动点歌与「引用分享下载文件」。"""

import sys

from aiohttp import web
from astrbot_plugin_moe_music.core.share import (
    ShareInfo,
    extract_quoted_share,
    extract_share,
    extract_share_from_text,
    parse_share_payload,
)
from test_commands import FakeBackend, MockEvent, make_service, track_json

COMPONENTS = sys.modules["astrbot.api.message_components"]


def plain(text: str):
    return COMPONENTS.Plain(text=text)


def json_card(payload):
    return COMPONENTS.Json(data=payload)


def reply(chain):
    return COMPONENTS.Reply(id=1, chain=chain)


# ---- 各家分享卡片的真实形态（meta 有的是 dict，有的是「字符串套 JSON」）----

QQ_MUSIC_CARD = {
    "app": "com.tencent.music.lua",
    "config": {"autosize": True, "ctime": 1700000000, "forward": True},
    "desc": "",
    "extra": {"app_type": 1},
    "meta": (
        '{"musicUrl":"https://i.y.qq.com/v8/playsong.html?songmid=0039MnYb0qxYhV&songid=1001",'
        '"jumpUrl":"https://i.y.qq.com/v8/playsong.html?songmid=0039MnYb0qxYhV",'
        '"preview":"http://isure.stream.qqmusic.qq.com/xx.m4a",'
        '"appid":"100497308","title":"晴天","desc":"周杰伦","tag":"QQ音乐",'
        '"image":"https://y.gtimg.cn/music/photo_new/T002R300x300M000xxx.jpg"}'
    ),
    "prompt": "[分享]晴天",
    "ver": "0.0.0.1",
    "view": "music",
}

NETEASE_CARD = {
    "app": "com.tencent.structmsg",
    "desc": "音乐",
    "meta": {
        "music": {
            "appid": 100495085,
            "desc": "隔壁老樊",
            "hostType": 0,
            "jumpUrl": "https://music.163.com/song?id=1330348068",
            "musicUrl": "https://music.163.com/song?id=1330348068",
            "preview": "https://p1.music.126.net/cover.jpg",
            "tag": "网易云音乐",
            "title": "我曾",
            "type": 0,
        }
    },
    "prompt": "[分享]我曾",
    "view": "music",
}

KUGOU_CARD = {
    "app": "com.tencent.structmsg",
    "meta": {
        "music": {
            "desc": "梦然",
            "jumpUrl": "https://www.kugou.com/mixsong/2h2sc246.html",
            "musicUrl": "https://www.kugou.com/mixsong/2h2sc246.html",
            "tag": "酷狗音乐",
            "title": "少年",
        }
    },
    "prompt": "[分享]少年",
    "view": "music",
}

KUWO_CARD = {
    "app": "com.tencent.structmsg",
    "meta": {
        "music": {
            "desc": "周杰伦",
            "jumpUrl": "https://www.kuwo.cn/play_detail/228908",
            "musicUrl": "https://www.kuwo.cn/play_detail/228908",
            "tag": "酷我音乐",
            "title": "七里香",
        }
    },
    "prompt": "[分享]七里香",
    "view": "music",
}

MINIAPP_CARD = {
    "app": "com.tencent.miniapp",
    "meta": {"detail_1": {"title": "某个小程序", "qqdocurl": "https://example.com/x"}},
    "prompt": "[小程序]某个小程序",
    "view": "miniapp",
}


class TestShareParsing:
    def test_qq_music_card(self):
        share = parse_share_payload(QQ_MUSIC_CARD)
        assert share is not None
        assert share.platform == "tx"
        assert share.track_id == "0039MnYb0qxYhV"  # 原生 songmid：可精确取详情
        assert share.title == "晴天"
        assert share.singer == "周杰伦"
        assert share.platform_name == "QQ音乐"
        assert share.keyword == "晴天 周杰伦"

    def test_netease_card(self):
        share = parse_share_payload(NETEASE_CARD)
        assert share is not None
        assert share.platform == "wy"
        assert share.track_id == "1330348068"
        assert share.title == "我曾"
        assert share.singer == "隔壁老樊"  # meta.music.desc 是歌手，不是外层「音乐」

    def test_kugou_card_uses_hash(self):
        share = parse_share_payload(KUGOU_CARD)
        assert share is not None
        assert share.platform == "kg"
        assert share.track_id == "2h2sc246"
        assert (share.title, share.singer) == ("少年", "梦然")

    def test_kuwo_card_falls_back_to_search(self):
        share = parse_share_payload(KUWO_CARD)
        assert share is not None
        assert share.platform == "kw"
        assert share.track_id == ""  # 酷我 rid 无法被后端内置详情解析，交给搜索
        assert share.usable
        assert (share.title, share.singer) == ("七里香", "周杰伦")

    def test_unknown_card_ignored(self):
        """小程序 / 图文等非音乐卡片：带 title 也不能当成点歌（否则每个分享都要搜一遍歌）。"""
        assert parse_share_payload(MINIAPP_CARD) is None

    def test_str_payload(self):
        import json

        share = parse_share_payload(json.dumps(QQ_MUSIC_CARD, ensure_ascii=False))
        assert share is not None and share.platform == "tx"

    def test_extract_from_components(self):
        share = extract_share([plain("好听"), json_card(QQ_MUSIC_CARD)])
        assert share is not None and share.platform == "tx"

    def test_extract_ignores_reply(self):
        """消息里只有「引用一张分享卡片」时不算本条消息是分享。"""
        assert extract_share([reply([json_card(QQ_MUSIC_CARD)])]) is None
        quoted = extract_quoted_share([reply([json_card(QQ_MUSIC_CARD)])])
        assert quoted is not None and quoted.platform == "tx"

    def test_extract_prefers_richer_component(self):
        share = extract_share([json_card(KUWO_CARD), json_card(QQ_MUSIC_CARD)])
        assert share is not None and share.track_id == "0039MnYb0qxYhV"

    def test_plain_text_link(self):
        share = extract_share_from_text("这首好听 https://music.163.com/song?id=1330348068 你听听")
        assert share is not None
        assert (share.platform, share.track_id, share.origin) == ("wy", "1330348068", "link")

    def test_plain_text_hashtag_route(self):
        share = extract_share_from_text("https://music.163.com/#/song?id=186016")
        assert share is not None and share.track_id == "186016"

    def test_plain_text_qq_play_song(self):
        share = extract_share_from_text(
            "https://i.y.qq.com/v8/playsong.html?songmid=001Qu4I30eVFYb&songid=1001"
        )
        assert share is not None and (share.platform, share.track_id) == ("tx", "001Qu4I30eVFYb")

    def test_plain_text_kuwo_link_is_unusable(self):
        """酷我链接只有 rid，既不能取详情也拿不到歌名 → 老老实实不响应。"""
        assert extract_share_from_text("https://www.kuwo.cn/play_detail/228908") is None

    def test_plain_text_irrelevant_link(self):
        assert extract_share_from_text("看这个 https://www.bilibili.com/video/BV1xx") is None
        assert extract_share_from_text("今天天气不错") is None

    def test_share_info_label(self):
        assert ShareInfo(title="晴天").label == "《晴天》"
        assert ShareInfo().label == "歌曲"

    def test_foreign_component_class_detected(self):
        """插件与框架的 astrbot 模块不是同一对象时 isinstance 恒为 False：

        这里用「同名但不同类」的 Json 模拟该场景，识别必须照样生效。
        """

        class Json:  # noqa: N801 - 故意与框架组件同名
            def __init__(self, data):
                self.data = data

        class Plain:  # noqa: N801
            def __init__(self, text):
                self.text = text

        share = extract_share([Plain("看看"), Json(QQ_MUSIC_CARD)])
        assert share is not None and share.track_id == "0039MnYb0qxYhV"

    def test_music_segment_fallback(self):
        """少数客户端用 OneBot music 段下发分享：163 的 id 可直接定位，QQ 只能搜索。"""
        from types import SimpleNamespace

        wy = SimpleNamespace(type="music", _type="163", id="1330348068", title="我曾", content="隔壁老樊")
        info = extract_share([wy])
        assert info is not None
        assert (info.platform, info.track_id, info.singer) == ("wy", "1330348068", "隔壁老樊")

        qq = SimpleNamespace(type="music", _type="qq", id="1001", title="晴天", content="周杰伦")
        info2 = extract_share([qq])
        assert info2 is not None
        assert (info2.platform, info2.track_id, info2.keyword) == ("tx", "", "晴天 周杰伦")

        # 自定义卡片（我们自己发出去的那种）不参与识别
        assert extract_share([SimpleNamespace(type="music", _type="custom", id="1")]) is None


def _text_of(seg) -> str:
    """取消息段文本（stub 的 Plain 把位置参数放在 args 里，真实组件在 text 上）。"""
    text = getattr(seg, "text", "") or ""
    if text:
        return str(text)
    return " ".join(str(a) for a in (getattr(seg, "args", ()) or ()))


def _sent_text(event) -> str:
    """取出已发送消息里的纯文本（text 模式下歌曲信息在 Plain 里）。"""
    parts: list[str] = []
    for kind, payload in event.sent:
        if kind == "plain":
            parts.append(str(payload))
        elif kind == "chain":
            parts.extend(_text_of(seg) for seg in payload)
    return "\n".join(parts)


def _spy_sender(service):
    """拦截真实发送，记录本次使用的 DeliveryOptions（避免测试里真去下载文件）。"""
    captured: dict = {}

    async def _spy(event, track, record_ctx=None, timings=None, options=None):
        captured["options"] = options
        captured["track"] = track
        return True

    service.sender.send_track = _spy
    return captured


class TestShareRequest:
    async def test_native_id_wins_over_search(self):
        """分享链接带原生 id：直接取详情，不该再去搜索。"""
        info = track_json(9) | {"id": "tx:0039MnYb0qxYhV", "name": "晴天", "singer": "周杰伦"}

        async def info_handler(request):
            return web.json_response({"code": 0, "message": "ok", "data": info})

        async def url_handler(request):
            return web.json_response(
                {"code": 0, "message": "ok", "data": {"url": "http://h/api/temp/t", "quality": "320k"}}
            )

        async with FakeBackend(
            search_result=[track_json(1)],
            extra_routes={
                "/api/v1/music/tx:{mid}/info": info_handler,
                "/api/v1/music/tx:{mid}/url": url_handler,
            },
        ) as api:
            service = make_service(api, {"share_send_modes": ["text(文本链接)"]})
            event = MockEvent()
            sent = await service.handle_share_request(event, parse_share_payload(QQ_MUSIC_CARD))
            assert sent
            assert api.calls == []  # 精确命中：没有走搜索/歌词/封面
            assert "晴天 - 周杰伦" in _sent_text(event)

    async def test_falls_back_to_search_by_keyword(self):
        """取不到原生 id（酷我）或详情失败：按「歌名 歌手」搜索并挑最像的一首。"""
        async with FakeBackend(
            search_result=[
                track_json(1) | {"name": "七里香 (翻唱)", "singer": "别人", "source": "wy"},
                track_json(2) | {"name": "七里香", "singer": "周杰伦", "source": "kw"},
            ]
        ) as api:
            service = make_service(api, {"share_send_modes": ["text(文本链接)"]})
            event = MockEvent()
            sent = await service.handle_share_request(event, parse_share_payload(KUWO_CARD))
            assert sent
            searches = [c for c in api.calls if c[0] == "search"]
            assert searches and searches[0][1]["keyword"] == "七里香 周杰伦"
            assert searches[0][1]["source"] == "kw"
            text = _sent_text(event)
            assert "七里香 - 周杰伦" in text  # 选中歌名/歌手都吻合的那首，而非第 1 条
            assert "翻唱" not in text

    async def test_id_failure_falls_back_to_search(self):
        """原生 id 取详情 404：自动退化为搜索，而不是直接报没找到。"""

        async def not_found(request):
            return web.json_response({"code": 4040, "message": "歌曲不存在", "data": None}, status=404)

        async with FakeBackend(
            search_result=[track_json(1) | {"source": "tx", "name": "晴天", "singer": "周杰伦"}],
            extra_routes={"/api/v1/music/tx:{mid}/info": not_found},
        ) as api:
            service = make_service(api)
            event = MockEvent()
            sent = await service.handle_share_request(event, parse_share_payload(QQ_MUSIC_CARD))
            assert sent
            assert [c[0] for c in api.calls].count("search") == 1

    async def test_no_result_hint(self):
        async with FakeBackend(search_result=[]) as api:
            service = make_service(api)
            event = MockEvent()
            sent = await service.handle_share_request(event, parse_share_payload(KUWO_CARD))
            assert not sent
            assert event.sent[-1] == (
                "plain",
                "识别到分享的《七里香》，但没能在音源里找到，稍后再试试吧～",
            )
            # 分享场景不发「换个关键词试试吧」这类对不上的通用提示
            assert not any("换个关键词" in item[1] for item in event.sent if item[0] == "plain")

    async def test_file_mode_uses_file_delivery(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service = make_service(api, {"file_quality": "master", "file_embed_metadata": False})
            captured = _spy_sender(service)
            event = MockEvent()
            sent = await service.handle_share_request(
                event, parse_share_payload(KUWO_CARD), file_mode=True, command="点歌文件"
            )
            assert sent
            options = captured["options"]
            assert options.modes == ["file_local"]  # 「引用分享 + 点歌文件」= 文件本体
            assert options.quality == "master"
            assert options.embed_metadata is False

    async def test_share_delivery_uses_share_settings(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service = make_service(api, {"share_quality": "192k", "share_send_modes": ["text(文本链接)"]})
            delivery = service._share_delivery()
            assert delivery.quality == "192k"
            assert delivery.modes == ["text"]

    async def test_access_control_blocks_share(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service = make_service(api, {"whitelist_groups": ["其他群号"]})
            event = MockEvent()
            sent = await service.handle_share_request(event, parse_share_payload(QQ_MUSIC_CARD))
            assert not sent
            assert event.sent[-1] == ("plain", "本群暂未开放点歌功能哦～")
            assert api.calls == []

    async def test_records_marked_as_share(self, tmp_path):
        from astrbot_plugin_moe_music.core.storage import RecordStore

        store = RecordStore(tmp_path / "records.db")
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service = make_service(api)
            service.store = store
            service.sender.store = store
            event = MockEvent()
            await service.handle_share_request(event, parse_share_payload(KUWO_CARD))
        row = store._conn.execute(
            "SELECT trigger_type, command, selection_type, keyword FROM play_records"
        ).fetchone()
        assert row == ("share", "分享识别", "share", "七里香 周杰伦")
        srow = store._conn.execute("SELECT trigger_type, keyword FROM search_records").fetchone()
        assert srow == ("share", "七里香 周杰伦")


class TestPluginShareHook:
    """插件入口：分享消息自动识别 + 「引用分享 + 点歌文件」下载文件。"""

    class _Event(MockEvent):
        def __init__(self, components=None, **kwargs):
            super().__init__(**kwargs)
            self._components = components or []

        def get_messages(self):
            return self._components

    def _plugin(self, api, config=None):
        from astrbot_plugin_moe_music.main import MoeMusicPlugin

        plugin = MoeMusicPlugin(
            context=None,
            config={"api_base_url": api._base_url, "api_key": "sk-test", **(config or {})},
        )
        plugin.api._base_url = api._base_url
        return plugin

    async def test_share_message_sends_and_stops(self):
        """开关打开后：分享卡片按语音发送（record 段），并吞掉事件不再交给 AI。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            plugin = self._plugin(api, {"share_auto_play": True})
            event = self._Event(components=[json_card(KUWO_CARD)])
            await plugin.share_message(event)
            assert event.stopped  # 已处理：不再交给 AI
            kinds = [item[0] for item in event.sent]
            assert kinds == ["chain"]
            segs = event.sent[0][1]
            assert getattr(segs[0], "file", "") == "http://h/api/temp/t"  # 语音段：链接

    async def test_share_message_text_mode(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            plugin = self._plugin(api, {"share_auto_play": True, "share_send_modes": ["text(文本链接)"]})
            event = self._Event(components=[json_card(KUWO_CARD)])
            await plugin.share_message(event)
            assert "歌曲1 - 歌手1" in _sent_text(event)

    async def test_non_share_message_not_stopped(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            plugin = self._plugin(api, {"share_auto_play": True})
            event = self._Event(message_str="今天天气不错", components=[plain("今天天气不错")])
            await plugin.share_message(event)
            assert not event.stopped
            assert event.sent == []

    async def test_auto_play_off_by_default(self):
        """默认关闭：不配置 share_auto_play 时，分享卡片完全不触发（一个请求都不发）。"""
        from astrbot_plugin_moe_music.core.config import PluginConfig

        assert PluginConfig.from_astrbot_config({"api_key": "sk-test"}).share_auto_play is False

        async with FakeBackend(search_result=[track_json(1)]) as api:
            plugin = self._plugin(api)  # 未显式打开
            event = self._Event(components=[json_card(KUWO_CARD)])
            await plugin.share_message(event)
            assert not event.stopped and event.sent == []
            assert api.calls == []

    async def test_disabled_by_config(self):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            plugin = self._plugin(api, {"share_auto_play": False})
            event = self._Event(components=[json_card(KUWO_CARD)])
            await plugin.share_message(event)
            assert not event.stopped and event.sent == []

    async def test_quoted_share_works_when_auto_off(self):
        """开关只管「自动」，引用分享 + 指令是用户明确操作，关着也要能用。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            plugin = self._plugin(api)  # 默认关闭
            captured = _spy_sender(plugin.service)
            event = self._Event(message_str="点歌", components=[reply([json_card(KUWO_CARD)])])
            await plugin.song_command(event)
            assert event.stopped
            assert captured["options"] is not None

    async def test_quoted_share_file_command(self):
        """引用分享 + 「点歌文件」（不带歌名）→ 下载引用里那首歌的文件。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            plugin = self._plugin(api)
            captured = _spy_sender(plugin.service)
            event = self._Event(
                message_str="点歌文件", components=[reply([json_card(KUWO_CARD)])]
            )
            await plugin.song_file_command(event)
            assert event.stopped
            searches = [c for c in api.calls if c[0] == "search"]
            assert searches and searches[0][1]["keyword"] == "七里香 周杰伦"
            assert captured["options"].modes == ["file_local"]

    async def test_quoted_share_song_command(self):
        """引用分享 + 「点歌」（不带歌名）→ 按分享策略发送（默认语音）。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            plugin = self._plugin(api)
            captured = _spy_sender(plugin.service)
            event = self._Event(message_str="点歌", components=[reply([json_card(KUWO_CARD)])])
            await plugin.song_command(event)
            assert event.stopped
            assert captured["options"].modes == ["record_link", "text"]

    async def test_quoted_share_without_share_shows_usage(self):
        async with FakeBackend(search_result=[]) as api:
            plugin = self._plugin(api)
            event = self._Event(message_str="点歌文件", components=[reply([plain("普通文本")])])
            await plugin.song_file_command(event)
            assert any("用法" in item[1] for item in event.sent if item[0] == "plain")
