"""歌曲信息卡片发送链路测试：先卡片后音频、去重、后台补齐与负缓存。"""

import asyncio
import io
import sys
from pathlib import Path

from aiohttp import web
from astrbot_plugin_moe_music.core.access import AccessController
from astrbot_plugin_moe_music.core.commands import MoeMusicService
from astrbot_plugin_moe_music.core.config import PluginConfig
from astrbot_plugin_moe_music.core.info_cache import InfoCache
from astrbot_plugin_moe_music.core.lyrics_render import LyricsRenderer
from astrbot_plugin_moe_music.core.sender import SongSender
from astrbot_plugin_moe_music.core.song_card_render import CardInfo
from astrbot_plugin_moe_music.core.songlist_render import SonglistRenderer
from PIL import Image
from test_commands import FakeBackend, MockEvent, track_json

FONT = Path(__file__).resolve().parent.parent / "fonts" / "simhei.ttf"
COMPONENTS = sys.modules["astrbot.api.message_components"]


def jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 120, 220)).save(buf, format="JPEG")
    return buf.getvalue()


class FakeCardRenderer:
    """假卡片渲染器：记录调用参数，返回可辨识的字节。"""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    async def render_async(self, track, **kwargs):
        self.calls.append({"track": track, **kwargs})
        if self.fail:
            raise RuntimeError("render boom")
        return b"CARD-BYTES"


class FakeEnricher:
    """假增强抓取器：记录调用次数，便于断言负缓存是否生效。"""

    def __init__(
        self,
        wy_id="186016",
        year=2014,
        intro="测试歌曲简介",
        song_intro="测试歌曲简介",
        cover="http://h/wy-cover.jpg",
        comment=None,
        bio="测试歌手简介",
    ):
        self.wy_id = wy_id
        self.year = year
        self.intro = intro  # fetch_wy_detail 返回的专辑文案（占位）
        self.song_intro = song_intro  # LLM 生成的歌曲简介
        self.cover = cover
        self.comment = comment
        self.bio = bio
        self.wy_calls = 0
        self.detail_calls = 0
        self.intro_calls = 0
        self.comment_calls = 0
        self.bio_calls: list[str] = []

    async def resolve_wy_id(self, track):
        self.wy_calls += 1
        return self.wy_id

    async def fetch_wy_detail(self, wy_id):
        self.detail_calls += 1
        detail = {}
        if self.year:
            detail["year"] = self.year
        if self.intro:
            detail["intro"] = self.intro
        if self.cover:
            detail["cover"] = self.cover
        return detail

    async def fetch_year(self, wy_id):
        self.year_calls += 1
        return self.year

    async def fetch_song_intro(self, track, umo=None):
        self.intro_calls += 1
        return self.song_intro or None

    async def fetch_hot_comment(self, wy_id):
        self.comment_calls += 1
        return self.comment

    async def fetch_hot_comment_for_track(self, track):
        """与服务层的新入口保持一致：返回 (热评, 来源平台码)。"""
        self.comment_calls += 1
        if not self.comment:
            return None, ""
        return self.comment, "wy"

    async def fetch_artist_bio(self, singer, umo=None):
        self.bio_calls.append(singer)
        return self.bio


def make_service(api, tmp_path, **cfg) -> tuple[MoeMusicService, FakeCardRenderer, FakeEnricher]:
    config = PluginConfig.from_astrbot_config(
        {"api_key": "sk-test", "send_modes": ["text"], "timeout": 5, **cfg}
    )
    sender = SongSender(config, api, tmp_path / "dl")
    service = MoeMusicService(
        config,
        api,
        sender,
        LyricsRenderer(FONT),
        SonglistRenderer(FONT),
        access=AccessController(config),
        card_renderer=FakeCardRenderer(),
        info_cache=InfoCache(tmp_path / "records.db"),
        enricher=FakeEnricher(),
    )
    return service, service.card_renderer, service.enricher


async def drain_background(service: MoeMusicService) -> None:
    """等后台信息补齐任务跑完（测试里不能靠 sleep 猜）。"""
    for _ in range(20):
        tasks = list(service._bg_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def is_image(result) -> bool:
    if result[0] != "chain":
        return False
    return any(isinstance(seg, COMPONENTS.Image) for seg in result[1])


async def cover_routes() -> dict:
    """给 FakeBackend 用的封面路由（pic 返回同服务的封面 URL，该 URL 返回 JPEG 字节）。"""

    async def pic(request):
        # 指向本服务，插件才真的能下载到封面字节（外网假地址会解析失败）
        return web.json_response(
            {"code": 0, "message": "ok", "data": {"url": str(request.url.with_path("/c.jpg"))}}
        )

    async def cover(request):
        return web.Response(body=jpeg_bytes(), content_type="image/jpeg")

    return {"/api/v1/music/wy:{i}/pic": pic, "/c.jpg": cover}


class TestCardOrderAndSwitch:
    async def test_card_sent_before_song(self, tmp_path):
        """先卡片、后歌曲；卡片能拿到真实封面字节。"""
        async with FakeBackend(
            search_result=[track_json(1)], extra_routes=await cover_routes()
        ) as api:
            service, renderer, _ = make_service(api, tmp_path)
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)

            assert len(event.sent) == 2
            assert is_image(event.sent[0]), "第一张应是信息卡片"
            assert event.sent[1][0] == "chain" and not is_image(event.sent[1])
            call = renderer.calls[0]
            assert call["cover"] and call["cover"][:2] == b"\xff\xd8"  # 真封面
            assert call["requester"] == "测试用户"
            assert call["quality"] == "320k"

    async def test_card_disabled(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, _ = make_service(api, tmp_path, song_card_enable=False)
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            assert len(event.sent) == 1
            assert renderer.calls == []

    async def test_render_failure_does_not_block_song(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, _ = make_service(api, tmp_path)
            renderer.fail = True
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            assert len(event.sent) == 1  # 卡片渲染失败 → 只发歌
            assert not is_image(event.sent[0])

    async def test_share_path_also_sends_card(self, tmp_path):
        """分享识别走的也是同一条发送链路，同样先卡片后音频。"""
        from astrbot_plugin_moe_music.core.share import parse_share_payload
        from test_share import KUWO_CARD, json_card

        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, _ = make_service(api, tmp_path)
            event = MockEvent()
            await service.handle_share_request(event, parse_share_payload(KUWO_CARD))
            assert is_image(event.sent[0])
            assert json_card is not None  # 保持导入被使用（分享卡片样例）


class TestCardDedup:
    async def test_repeat_within_window_skips_card(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, _ = make_service(api, tmp_path, song_card_repeat_sec=300)
            first, second = MockEvent(), MockEvent()
            await service.handle_song_request(first, "晴天", index_hint=1)
            await service.handle_song_request(second, "晴天", index_hint=1)
            assert len(renderer.calls) == 1  # 窗口内不重发卡片
            assert len(second.sent) == 1  # 但歌照发
            assert not is_image(second.sent[0])

    async def test_zero_window_always_sends(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, _ = make_service(api, tmp_path, song_card_repeat_sec=0)
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            assert len(renderer.calls) == 2

    async def test_other_session_not_deduped(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, _ = make_service(api, tmp_path, song_card_repeat_sec=300)
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await service.handle_song_request(
                MockEvent(umo="qq:GroupMessage:99999"), "晴天", index_hint=1
            )
            assert len(renderer.calls) == 2  # 别群照样发


class TestBackgroundEnrich:
    async def test_enrich_writes_cache_then_card_gets_full_info(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, enricher = make_service(api, tmp_path)
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            await drain_background(service)

            assert enricher.detail_calls == 1
            assert enricher.bio_calls == ["歌手1"]
            row = await service.info_cache.get_song("wy:1")
            assert row["year"] == 2014
            assert row["intro"] == "测试歌曲简介"
            assert row["cover_url"] == "http://h/wy-cover.jpg"
            assert row["wy_id"] == "186016"
            artist = await service.info_cache.get_artist("歌手1")
            assert artist["bio"] == "测试歌手简介"

            # 第二次点同一首（换个会话避开卡片去重）→ 卡片直接用到缓存里的信息
            await service.handle_song_request(
                MockEvent(umo="qq:GroupMessage:88888"), "晴天", index_hint=1
            )
            info: CardInfo = renderer.calls[-1]["info"]
            assert info.year == 2014
            assert info.intro == "测试歌曲简介"
            assert info.artist_bio == "测试歌手简介"

    async def test_hot_comment_cached_when_enabled(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, enricher = make_service(api, tmp_path, song_card_comment=True)
            enricher.comment = {"text": "好听", "user": "网友", "likes": 123}
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            row = await service.info_cache.get_song("wy:1")
            assert row["hot_comment"] == "好听"
            assert row["hot_comment_likes"] == 123
            assert row["hot_comment_source"] == "wy"

    async def test_nothing_fetched_when_all_disabled(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, enricher = make_service(
                api,
                tmp_path,
                song_card_year=False,
                song_card_intro=False,
                song_card_comment=False,
                song_card_artist_bio=False,
            )
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            # 增强全关，但卡片封面仍需 wy 详情兜底（wy 音源无 pic 实现）
            assert (enricher.wy_calls, enricher.bio_calls) == (0, [])
            assert enricher.detail_calls == 1

    async def test_intro_prefers_llm_over_album_text(self, tmp_path):
        """简介以 LLM 生成的**歌曲**简介为主体：专辑文案（album）只是占位，会被升级替换。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, enricher = make_service(api, tmp_path)
            enricher.intro = "这是专辑的介绍"  # fetch_wy_detail 返回的 album.description
            enricher.song_intro = "这是歌曲的介绍"  # LLM 生成的歌曲简介
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            row = await service.info_cache.get_song("wy:1")
            assert row["intro"] == "这是歌曲的介绍"
            assert row["intro_source"] == "llm"

    async def test_intro_falls_back_to_album_text_without_llm(self, tmp_path):
        """LLM 不可用 / 不认识这首歌：保留专辑文案占位，不让简介栏空着。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, enricher = make_service(api, tmp_path)
            enricher.intro = "这是专辑的介绍"
            enricher.song_intro = None
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            row = await service.info_cache.get_song("wy:1")
            assert row["intro"] == "这是专辑的介绍"
            assert row["intro_source"] == "album"

    async def test_failed_fetch_not_retried_on_next_play(self, tmp_path):
        """抓不到也要记时间戳：否则每次点这首歌都会重打一遍外部接口。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, enricher = make_service(api, tmp_path)
            enricher.year = None
            enricher.intro = None
            enricher.cover = None
            enricher.bio = None
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            await service.handle_song_request(
                MockEvent(umo="qq:GroupMessage:88888"), "晴天", index_hint=1
            )
            await drain_background(service)
            # 第一次：卡片封面 1 次 + 补齐 1 次；第二次全部被负缓存拦下
            assert enricher.detail_calls == 2
            assert enricher.intro_calls == 1  # LLM 简介同样只试一次
            assert enricher.bio_calls == ["歌手1"]


class TestRecentTrackQuote:
    """「引用我们发的卡片 / 语音 / 文件 + 指令」：用会话最近一次点歌还原曲目。"""

    class _Event(MockEvent):
        def __init__(self, components=None, **kwargs):
            super().__init__(**kwargs)
            self._components = components or []

        def get_messages(self):
            return self._components

    def reply(self, chain):
        return COMPONENTS.Reply(id=1, chain=chain)

    def image_component(self):
        return COMPONENTS.Image(file=b"card-bytes")

    async def test_quote_card_download(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, _ = make_service(api, tmp_path)
            first = MockEvent()
            await service.handle_song_request(first, "晴天", index_hint=1)

            captured: dict = {}

            async def _spy(event, track, record_ctx=None, timings=None, options=None, song_card=None):
                captured["track"] = track
                captured["options"] = options
                return True

            service.sender.send_track = _spy
            event = self._Event(message_str="下载", components=[self.reply([self.image_component()])])
            await service.handle_track_request(
                event, service.last_track(event), file_mode=True, command="下载"
            )
            assert captured["track"].id == "wy:1"
            assert captured["options"].modes == ["file_local"]

    async def test_quote_card_lyrics_and_replay(self, tmp_path):
        lrc = "[00:01.00]故事的小黄花"
        async with FakeBackend(search_result=[track_json(1)], lyric_text=lrc) as api:
            service, _, _ = make_service(api, tmp_path)
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            track = service.last_track(MockEvent())
            assert track is not None and track.id == "wy:1"

            # 歌词：图片消息链
            lyrics_event = self._Event(components=[self.reply([self.image_component()])])
            assert await service.handle_track_request(
                lyrics_event, track, lyrics=True, command="歌词"
            )
            assert any(isinstance(seg, COMPONENTS.Image) for seg in lyrics_event.sent[-1][1])

            # 点歌：按分享策略重发一遍（默认语音）
            replay = self._Event(components=[self.reply([self.image_component()])])
            assert await service.handle_track_request(replay, track, command="点歌")
            seg = replay.sent[0][1][0]
            assert getattr(seg, "file", "") == "http://h/api/temp/t"

    async def test_recent_track_expires(self, tmp_path):

        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, _ = make_service(api, tmp_path)
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            umo = MockEvent().unified_msg_origin
            track, ts = service._recent_tracks[umo]
            service._recent_tracks[umo] = (track, ts - 601)  # 超过 TTL
            assert service.last_track(MockEvent()) is None

    async def test_wy_cover_fallback_via_live_detail(self, tmp_path):
        """wy 音源没有 pic 实现（后端 404）且曲目无 coverUrl：实时拉网易云详情补封面并写缓存。"""
        from aiohttp import web as _web

        async def cover_bytes(request):
            return _web.Response(body=jpeg_bytes(), content_type="image/jpeg")

        async with FakeBackend(
            search_result=[track_json(1) | {"coverUrl": None}],
            extra_routes={"/wy-cover.jpg": cover_bytes},
        ) as api:
            service, renderer, enricher = make_service(api, tmp_path)
            enricher.cover = f"{api._base_url}/wy-cover.jpg"  # 指向本地假服务，真实可下载
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            assert renderer.calls[0]["cover"][:2] == b"\xff\xd8"  # 占位图被换成真封面
            row = await service.info_cache.get_song("wy:1")
            assert row["cover_url"] == enricher.cover  # 已写缓存，下次不再拉详情
            assert enricher.detail_calls == 1  # 卡片路径拿详情时年份/简介一并入库

    async def test_no_cache_no_card_info(self, tmp_path):
        """缓存不可用（None）时卡片照样发，只是没有增强信息。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, _ = make_service(api, tmp_path)
            service.info_cache = None
            service.enricher = None
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            assert is_image(event.sent[0])
            assert renderer.calls[0]["info"].empty
