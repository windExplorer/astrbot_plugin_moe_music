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
        artist_id="887",
        song_intro="测试歌曲简介",
        cover="http://h/wy-cover.jpg",
        comment=None,
        bio="测试歌手简介",
        api_bio=None,
    ):
        self.wy_id = wy_id
        self.year = year  # 网易云详情里的发行年份
        self.artist_id = artist_id  # 详情里的主唱歌手网易云 id
        self.song_intro = song_intro  # LLM 生成的歌曲简介
        self.cover = cover
        self.comment = comment
        self.bio = bio  # LLM 生成的歌手简介
        self.api_bio = api_bio  # 网易云接口直取的歌手简介（默认无 → LLM 兜底）
        self.wy_calls = 0
        self.detail_calls = 0
        self.llm_calls = 0
        self.artist_api_calls = 0
        self.comment_calls = 0

    async def resolve_wy_id(self, track):
        self.wy_calls += 1
        return self.wy_id

    async def fetch_wy_detail(self, wy_id):
        self.detail_calls += 1
        detail = {}
        if self.year:
            detail["year"] = self.year
        if self.cover:
            detail["cover"] = self.cover
        if self.artist_id:
            detail["artist_id"] = self.artist_id
        return detail

    async def fetch_artist_intro_wy(self, artist_id):
        self.artist_api_calls += 1
        return self.api_bio

    async def fetch_card_info(
        self, track, need_year=False, need_intro=False, need_bio=False, event=None, umo=None
    ):
        self.llm_calls += 1
        out = {}
        if need_year and self.year:
            out["year"] = self.year
        if need_intro and self.song_intro:
            out["intro"] = self.song_intro
        if need_bio and self.bio:
            out["artist_bio"] = self.bio
        return out

    async def fetch_hot_comment(self, wy_id):
        self.comment_calls += 1
        return self.comment

    async def fetch_hot_comment_for_track(self, track):
        """与服务层的新入口保持一致：返回 (热评, 来源平台码)。"""
        self.comment_calls += 1
        if not self.comment:
            return None, ""
        return self.comment, "wy"


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
    async def test_card_races_song(self, tmp_path):
        """卡片与歌曲竞速：缓存冷 → 先歌后卡片（补齐后补发，信息齐全）；缓存热 → 先卡片后歌。"""
        routes = await cover_routes()

        async def wy_cover(request):
            return web.Response(body=jpeg_bytes(), content_type="image/jpeg")

        routes["/wy-cover.jpg"] = wy_cover
        async with FakeBackend(search_result=[track_json(1)], extra_routes=routes) as api:
            service, renderer, enricher = make_service(api, tmp_path, song_card_repeat_sec=0)
            # 补齐任务会把网易云封面写进缓存，测试里让它指向本地假服务才下载得到
            enricher.cover = f"{api._base_url}/wy-cover.jpg"
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            await drain_background(service)

            # 第一次：歌曲先走，卡片等补齐完成后补发（不再顶着空信息出去）
            assert event.sent[0][0] == "chain" and not is_image(event.sent[0]), "首次应先发歌"
            assert len(event.sent) == 2 and is_image(event.sent[1]), "补齐完成后应补发完整卡片"
            call = renderer.calls[0]
            assert call["cover"] and call["cover"][:2] == b"\xff\xd8"  # 真封面
            assert call["requester"] == "测试用户"
            assert call["quality"] == "320k"
            assert call["info"].year == 2014  # 补发卡片带增强信息
            assert call["info"].intro or call["info"].hot_comment

            # 第二次（缓存热）：先发卡片、后发歌，信息同样齐全
            second = MockEvent()
            await service.handle_song_request(second, "晴天", index_hint=1)
            await drain_background(service)
            assert is_image(second.sent[0]), "缓存热时应先发卡片"
            assert second.sent[1][0] == "chain" and not is_image(second.sent[1])
            assert renderer.calls[-1]["info"].year == 2014

    async def test_card_disabled(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, _ = make_service(api, tmp_path, song_card_enable=False)
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            await drain_background(service)
            assert len(event.sent) == 1
            assert renderer.calls == []

    async def test_render_failure_does_not_block_song(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, _ = make_service(api, tmp_path)
            renderer.fail = True
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            await drain_background(service)
            assert len(event.sent) == 1  # 卡片渲染失败 → 只发歌
            assert not is_image(event.sent[0])

    async def test_share_path_also_races_card(self, tmp_path):
        """分享识别走同一条发送链路：歌先走，卡片补齐后补发。"""
        from astrbot_plugin_moe_music.core.share import parse_share_payload
        from test_share import KUWO_CARD, json_card

        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, _ = make_service(api, tmp_path)
            event = MockEvent()
            await service.handle_share_request(event, parse_share_payload(KUWO_CARD))
            await drain_background(service)
            assert not is_image(event.sent[0]), "分享首次也应先发音频"
            assert is_image(event.sent[1]), "卡片随后补发"
            assert json_card is not None  # 保持导入被使用（分享卡片样例）


class TestCardDedup:
    async def test_repeat_within_window_skips_card(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, _ = make_service(api, tmp_path, song_card_repeat_sec=300)
            first, second = MockEvent(), MockEvent()
            await service.handle_song_request(first, "晴天", index_hint=1)
            await drain_background(service)  # 首次缓存冷：卡片在补齐后补发
            await service.handle_song_request(second, "晴天", index_hint=1)
            await drain_background(service)
            assert len(renderer.calls) == 1  # 窗口内不重发卡片
            assert len(second.sent) == 1  # 但歌照发
            assert not is_image(second.sent[0])

    async def test_zero_window_always_sends(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, _ = make_service(api, tmp_path, song_card_repeat_sec=0)
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            assert len(renderer.calls) == 2

    async def test_other_session_not_deduped(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, _ = make_service(api, tmp_path, song_card_repeat_sec=300)
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            await service.handle_song_request(
                MockEvent(umo="qq:GroupMessage:99999"), "晴天", index_hint=1
            )
            await drain_background(service)
            assert len(renderer.calls) == 2  # 别群照样发


class TestBackgroundEnrich:
    async def test_enrich_writes_cache_then_card_gets_full_info(self, tmp_path):
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, enricher = make_service(api, tmp_path)
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            await drain_background(service)

            # 年份走网易云详情（事实数据）；简介+歌手简介合并为一次 LLM 调用
            assert enricher.detail_calls == 1
            assert enricher.llm_calls == 1
            row = await service.info_cache.get_song("wy:1")
            assert row["year"] == 2014
            assert row["intro"] == "测试歌曲简介"
            assert row["intro_source"] == "llm"
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
            # 渲染开关全关，但卡片封面仍需 wy 详情兜底（wy 音源无 pic 实现）
            assert enricher.wy_calls == 0
            assert enricher.llm_calls == 0
            assert enricher.detail_calls == 1

    async def test_retry_ttl_zero_never_retries(self, tmp_path):
        """info_retry_days=0（默认）：抓不到就永久跳过，隔了很久也不重试。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, enricher = make_service(api, tmp_path, info_retry_days=0)
            enricher.year = None
            enricher.song_intro = None
            enricher.bio = None
            enricher.cover = None
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            detail_after_first = enricher.detail_calls
            # 把尝试时间拨到 100 天前：默认「永不重试」下仍然跳过
            row = await service.info_cache.get_song("wy:1")
            await service.info_cache.upsert_song("wy:1", info_at=row["info_at"] - 100 * 86400)
            artist = await service.info_cache.get_artist("歌手1")
            await service.info_cache.upsert_artist(
                "歌手1", attempt_at=artist["attempt_at"] - 100 * 86400
            )
            await service.handle_song_request(
                MockEvent(umo="qq:GroupMessage:88888"), "晴天", index_hint=1
            )
            await drain_background(service)
            assert enricher.detail_calls == detail_after_first
            assert enricher.llm_calls == 1

    async def test_retry_ttl_days_retries_after(self, tmp_path):
        """info_retry_days>0：超过间隔后会再试一次（负缓存不是永久）。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, enricher = make_service(api, tmp_path, info_retry_days=1)
            enricher.year = None
            enricher.song_intro = None
            enricher.bio = None
            enricher.cover = None
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            # 把尝试时间拨到 2 天前：超过 1 天的重试间隔 → 会重试
            row = await service.info_cache.get_song("wy:1")
            await service.info_cache.upsert_song("wy:1", info_at=row["info_at"] - 2 * 86400)
            artist = await service.info_cache.get_artist("歌手1")
            await service.info_cache.upsert_artist(
                "歌手1", attempt_at=artist["attempt_at"] - 2 * 86400
            )
            await service.handle_song_request(
                MockEvent(umo="qq:GroupMessage:88888"), "晴天", index_hint=1
            )
            await drain_background(service)
            assert enricher.llm_calls == 2  # 重新尝试了一次
            assert enricher.detail_calls >= 2  # 年份/封面详情也重试了

    async def test_artist_bio_from_api_preferred_over_llm(self, tmp_path):
        """歌手简介优先走网易云接口（事实数据、快）：成功后不再为它调 LLM。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, enricher = make_service(api, tmp_path)
            enricher.api_bio = "网易云的歌手简介"
            enricher.bio = "LLM 的歌手简介"
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            artist = await service.info_cache.get_artist("歌手1")
            assert artist["bio"] == "网易云的歌手简介"
            assert artist["source"] == "wy"
            row = await service.info_cache.get_song("wy:1")
            assert row["artist_id"] == "887"  # 详情顺带缓存歌手 id
            # LLM 只为歌曲简介而调，请求里不带歌手简介字段（无法直接断言字段，
            # 但 LLM 调用次数仍为 1 次：简介一次搞定）
            assert enricher.llm_calls == 1
            assert enricher.artist_api_calls == 1

    async def test_artist_bio_falls_back_to_llm_when_api_empty(self, tmp_path):
        """网易云接口没给歌手简介：回退 LLM 兜底，来源标记 llm。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, enricher = make_service(api, tmp_path)
            enricher.api_bio = None
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            artist = await service.info_cache.get_artist("歌手1")
            assert artist["bio"] == "测试歌手简介"
            assert artist["source"] == "llm"

    async def test_llm_intro_stored_as_llm_source(self, tmp_path):
        """歌曲简介来自 LLM（intro_source=llm），一次调用同时覆盖歌手简介。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, enricher = make_service(api, tmp_path)
            enricher.song_intro = "这是歌曲的介绍"
            enricher.bio = "这是歌手的介绍"
            await service.handle_song_request(MockEvent(), "晴天", index_hint=1)
            await drain_background(service)
            row = await service.info_cache.get_song("wy:1")
            assert row["intro"] == "这是歌曲的介绍"
            assert row["intro_source"] == "llm"
            artist = await service.info_cache.get_artist("歌手1")
            assert artist["bio"] == "这是歌手的介绍"
            assert artist["source"] == "llm"

    async def test_llm_master_off_skips_llm_and_render(self, tmp_path):
        """总开关关闭：年份/简介/歌手简介不获取也不渲染；热评照常走接口。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, renderer, enricher = make_service(
                api, tmp_path, song_card_llm_sync=False, song_card_repeat_sec=0
            )
            enricher.comment = {"text": "好听", "user": "网友", "likes": 123}
            event = MockEvent()
            await service.handle_song_request(event, "晴天", index_hint=1)
            await drain_background(service)
            info: CardInfo = renderer.calls[0]["info"]
            assert info.year == 0 and info.intro == "" and info.artist_bio == ""
            assert info.hot_comment == ""  # 卡片永不等待：首张是基础卡片
            assert enricher.llm_calls == 0  # 没有任何 LLM 调用
            row = await service.info_cache.get_song("wy:1")
            # 年份随封面详情顺带缓存（事实数据，不渲染进卡片）
            assert row["year"] == 2014
            assert row["hot_comment"] == "好听"  # 热评照常走接口并缓存

    async def test_failed_fetch_not_retried_on_next_play(self, tmp_path):
        """抓不到也要记时间戳：否则每次点这首歌都会重打一遍外部接口。"""
        async with FakeBackend(search_result=[track_json(1)]) as api:
            service, _, enricher = make_service(api, tmp_path)
            enricher.year = None
            enricher.song_intro = None
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
            assert enricher.llm_calls == 1  # LLM 同样只试一次


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
            await drain_background(service)
            assert renderer.calls[0]["cover"][:2] == b"\xff\xd8"  # 占位图被换成真封面
            row = await service.info_cache.get_song("wy:1")
            assert row["cover_url"] == enricher.cover  # 已写缓存，下次不再拉详情
            assert enricher.detail_calls == 1  # 补齐路径拿详情时封面/年份/简介一并入库

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
