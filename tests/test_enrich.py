"""信息卡片增强抓取测试（core/enrich.py）：网易云年份/热评、同曲映射、LLM 简介。

外部接口用本地 aiohttp 假服务顶替（替换模块常量 ``WY_API_BASE``），不打真网络。
"""

import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from astrbot_plugin_moe_music.core import enrich as enrich_mod
from astrbot_plugin_moe_music.core.enrich import Enricher
from astrbot_plugin_moe_music.core.model import Track


def make_track(**kwargs) -> Track:
    data = {
        "id": "wy:186016",
        "name": "晴天",
        "singer": "周杰伦",
        "album": "叶惠美",
        "duration": 269,
        "source": "wy",
        "qualitys": ["320k"],
    }
    data.update(kwargs)
    return Track.from_api(data)


class FakeApi:
    """假后端：只实现同曲映射要用的 search。"""

    def __init__(self, results=None, error: Exception | None = None):
        self.results = results or []
        self.error = error
        self.calls: list[dict] = []

    async def search(self, keyword, limit=5, source=None, quality=None):
        self.calls.append({"keyword": keyword, "source": source})
        if self.error:
            raise self.error
        return list(self.results)


class FakeProvider:
    """假对话模型。"""

    def __init__(self, text: str = "", error: Exception | None = None):
        self.text = text
        self.error = error
        self.prompts: list[str] = []

    async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
        self.prompts.append(prompt or "")
        if self.error:
            raise self.error
        return SimpleNamespace(completion_text=self.text)


def make_enricher(api=None, provider=None, **kwargs) -> Enricher:
    async def _getter(_umo):
        return provider

    return Enricher(api or FakeApi(), provider_getter=_getter, **kwargs)


@pytest.fixture()
async def wy_server(monkeypatch):
    """假的网易云 / 酷我 / 酷狗公开接口，并把对应模块常量指到它上面。"""
    state = {
        "detail": {"songs": [{"album": {"publishTime": 1062864000000}}]},  # 2003-09-06
        "comments": {"code": 200, "hotComments": [
            {"content": "这首歌[可爱]陪我走过青春", "likedCount": 23456,
             "user": {"nickname": "某位网友"}}
        ]},
        "detail_status": 200,
        "comment_status": 200,
        "kw": {"code": "200", "hot_comments": [
            {"msg": "酷我热评[捂脸]", "u_name": "kw用户", "like_num": 99}
        ]},
        "kw_status": 200,
        "kg": {"err_code": 0, "list": [
            {"content": "酷狗热评", "user_name": "kg用户", "like": {"likenum": 55}}
        ]},
        "kg_status": 200,
        "artist": {"artist": {"briefDesc": "周杰伦，华语流行男歌手。"}},
        "wiki_search": {"query": {"search": [{"title": "晴天 (歌曲)"}]}},
        "wiki_page": {
            "query": {"pages": {"1": {
                "extract": "《晴天》是周杰伦演唱的流行歌曲，收录于 2003 年专辑《叶惠美》。"
            }}}
        },
    }
    app = web.Application()

    async def artist_intro(request):
        return web.json_response(state["artist"])

    async def wiki_api(request):
        if request.query.get("list") == "search":
            return web.json_response(state["wiki_search"])
        return web.json_response(state["wiki_page"])

    async def detail(request):
        if state["detail_status"] != 200:
            return web.json_response({"code": 404}, status=state["detail_status"])
        return web.json_response(state["detail"])

    async def comments(request):
        if state["comment_status"] != 200:
            return web.json_response({"code": 403}, status=state["comment_status"])
        return web.json_response(state["comments"])

    async def kw_comments(request):
        if state["kw_status"] != 200:
            return web.json_response({}, status=state["kw_status"])
        return web.json_response(state["kw"])

    async def kg_comments(request):
        if state["kg_status"] != 200:
            return web.json_response({}, status=state["kg_status"])
        return web.json_response(state["kg"])

    app.router.add_get("/api/song/detail/", detail)
    app.router.add_get("/api/artist/{aid}", artist_intro)
    app.router.add_get("/w/api.php", wiki_api)
    app.router.add_get("/api/v1/resource/comments/{rid}", comments)
    app.router.add_get("/com.s", kw_comments)
    app.router.add_get("/r/v1/rank/topliked", kg_comments)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(enrich_mod, "WY_API_BASE", base)
    monkeypatch.setattr(enrich_mod, "WIKI_API_BASE", f"{base}/w/api.php")
    monkeypatch.setattr(enrich_mod, "KW_COMMENT_URL", f"{base}/com.s")
    monkeypatch.setattr(enrich_mod, "KG_COMMENT_BASE", base)
    yield state
    await runner.cleanup()


class TestFetchYear:
    async def test_parses_millisecond_timestamp(self, wy_server):
        assert await make_enricher().fetch_year("186016") == 2003

    async def test_detail_returns_year_cover_artist_id(self, wy_server):
        """网易云详情一次拿年份、封面和主唱歌手 id（歌手简介直取用）。"""
        wy_server["detail"] = {
            "songs": [
                {
                    "album": {
                        "publishTime": 1062864000000,
                        "picUrl": "https://p.example/cover.jpg",
                    },
                    "artists": [{"id": 6452, "name": "周杰伦"}],
                }
            ]
        }
        detail = await make_enricher().fetch_wy_detail("1")
        assert detail["year"] == 2003
        assert detail["cover"] == "https://p.example/cover.jpg"
        assert detail["artist_id"] == "6452"
        assert "intro" not in detail

    async def test_detail_weapi_short_keys_also_supported(self, wy_server):
        """weapi 缩写格式（ar / al）同样能解析出歌手 id。"""
        wy_server["detail"] = {
            "songs": [
                {"al": {"publishTime": 1062864000000}, "ar": [{"id": 6452}]}
            ]
        }
        detail = await make_enricher().fetch_wy_detail("1")
        assert detail["year"] == 2003 and detail["artist_id"] == "6452"

    async def test_detail_without_cover(self, wy_server):
        wy_server["detail"] = {"songs": [{"album": {"publishTime": 1062864000000}}]}
        detail = await make_enricher().fetch_wy_detail("1")
        assert detail["year"] == 2003 and "cover" not in detail

    async def test_artist_intro_from_public_api(self, wy_server):
        """网易云歌手简介公开接口：briefDesc 清理后返回；没有则 None。"""
        wy_server["artist"] = {
            "artist": {"briefDesc": "周杰伦，华语流行男歌手。\n代表作《晴天》。"}
        }
        bio = await make_enricher().fetch_artist_intro_wy("6452")
        assert bio is not None and bio.startswith("周杰伦")
        assert "代表作" in bio

        wy_server["artist"] = {"artist": {"briefDesc": ""}}
        assert await make_enricher().fetch_artist_intro_wy("6452") is None
        assert await make_enricher().fetch_artist_intro_wy("") is None

    async def test_parses_second_timestamp(self, wy_server):
        wy_server["detail"] = {"songs": [{"publishTime": int(time.time())}]}
        assert await make_enricher().fetch_year("1") == time.localtime().tm_year

    async def test_missing_field_returns_none(self, wy_server):
        wy_server["detail"] = {"songs": [{"album": {}}]}
        assert await make_enricher().fetch_year("1") is None

    async def test_unreasonable_timestamp_ignored(self, wy_server):
        """把别的字段误当时间戳时不能画出「1970 年」这种年份。"""
        wy_server["detail"] = {"songs": [{"album": {"publishTime": 1}}]}
        assert await make_enricher().fetch_year("1") is None

    async def test_http_error_returns_none(self, wy_server):
        wy_server["detail_status"] = 500
        assert await make_enricher().fetch_year("1") is None
        assert await make_enricher().fetch_year("") is None

    async def test_garbage_payload_returns_none(self, wy_server):
        wy_server["detail"] = {"songs": []}
        assert await make_enricher().fetch_year("1") is None


class TestFetchHotComment:
    async def test_hot_comment_parsed(self, wy_server):
        data = await make_enricher().fetch_hot_comment("186016")
        assert data is not None
        assert data["text"] == "这首歌陪我走过青春"  # [可爱] 这类表情占位符已清理
        assert data["user"] == "某位网友"
        assert data["likes"] == 23456

    async def test_falls_back_to_comments(self, wy_server):
        wy_server["comments"] = {
            "code": 200,
            "comments": [{"content": "普通评论", "user": {"nickname": "A"}}],
        }
        data = await make_enricher().fetch_hot_comment("1")
        assert data is not None and data["text"] == "普通评论" and data["likes"] == 0

    async def test_nested_data_scope(self, wy_server):
        wy_server["comments"] = {"code": 200, "data": {"hotComments": [{"content": "嵌套", "user": {}}]}}
        data = await make_enricher().fetch_hot_comment("1")
        assert data is not None and data["text"] == "嵌套"

    async def test_empty_or_error_returns_none(self, wy_server):
        wy_server["comments"] = {"code": 200, "hotComments": [], "comments": []}
        assert await make_enricher().fetch_hot_comment("1") is None
        wy_server["comment_status"] = 403
        assert await make_enricher().fetch_hot_comment("1") is None


class TestMultiPlatformComments:
    """热评按曲目所在平台直取：酷我（免签名）/ 酷狗（md5 签名）/ 网易云。"""

    async def test_kw_comment(self, wy_server):
        data = await make_enricher().fetch_hot_comment_kw("228908")
        assert data is not None
        assert data["text"] == "酷我热评"  # [捂脸] 表情占位符已清理
        assert (data["user"], data["likes"]) == ("kw用户", 99)

    async def test_kg_comment(self, wy_server):
        data = await make_enricher().fetch_hot_comment_kg("2h2sc246")
        assert data is not None
        assert (data["text"], data["user"], data["likes"]) == ("酷狗热评", "kg用户", 55)

    async def test_kg_signature_is_md5_of_sorted_params(self):
        import hashlib

        params = "b=2&a=1"
        expected = hashlib.md5(
            f"{enrich_mod._KG_SIGN_KEY}a=1b=2{enrich_mod._KG_SIGN_KEY}".encode()
        ).hexdigest()
        assert enrich_mod._kg_signature(params) == expected

    async def test_for_track_direct_per_platform(self, wy_server):
        enricher = make_enricher()
        track_kw = make_track(id="kw:228908", source="kw")
        comment, source = await enricher.fetch_hot_comment_for_track(track_kw)
        assert source == "kw" and comment["text"] == "酷我热评"

        track_kg = make_track(id="kg:2h2sc246", source="kg")
        comment, source = await enricher.fetch_hot_comment_for_track(track_kg)
        assert source == "kg" and comment["text"] == "酷狗热评"

        track_wy = make_track()
        comment, source = await enricher.fetch_hot_comment_for_track(track_wy)
        assert source == "wy" and "走过青春" in comment["text"]

    async def test_for_track_falls_back_to_wy_mapping(self, wy_server):
        """QQ音乐评论不直取（需 songId 映射）：回退网易云同曲映射。"""
        api = FakeApi(results=[make_track(id="wy:1", name="晴天", singer="周杰伦")])
        enricher = make_enricher(api)
        comment, source = await enricher.fetch_hot_comment_for_track(
            make_track(id="tx:1", source="tx")
        )
        assert source == "wy" and comment is not None
        assert api.calls[0]["source"] == "wy"

    async def test_for_track_kw_failure_falls_back(self, wy_server):
        wy_server["kw_status"] = 500
        api = FakeApi(results=[make_track(id="wy:1", name="晴天", singer="周杰伦")])
        comment, source = await make_enricher(api).fetch_hot_comment_for_track(
            make_track(id="kw:228908", source="kw")
        )
        assert source == "wy" and comment is not None

    async def test_wy_failure_does_not_remap(self, wy_server):
        """网易云刚失败过就别再走同曲映射重复请求同一接口。"""
        api = FakeApi()
        wy_server["comments"] = {"code": 200, "hotComments": [], "comments": []}
        comment, source = await make_enricher(api).fetch_hot_comment_for_track(make_track())
        assert comment is None and source == ""
        assert api.calls == []  # 没有发起同曲映射搜索


class TestSongIntroWiki:
    """歌曲简介的维基百科开放接口：搜词条 → 取首段（校验很严）。"""

    async def test_intro_from_wiki(self, wy_server):
        intro = await make_enricher().fetch_song_intro_wiki(make_track())
        assert intro is not None
        assert "周杰伦" in intro and "晴天" in intro

    async def test_title_mismatch_rejected(self, wy_server):
        """词条标题不含歌名（同名影视页之类）不能当歌曲简介。"""
        wy_server["wiki_search"] = {"query": {"search": [{"title": "晴天 (电视剧)"}]}}
        # 标题里有「晴天」会被放行去取正文，但正文没提到歌手 → 仍被拒
        wy_server["wiki_page"] = {"query": {"pages": {"1": {"extract": "一部电视剧的剧情介绍。"}}}}
        assert await make_enricher().fetch_song_intro_wiki(make_track()) is None

    async def test_disambiguation_page_rejected(self, wy_server):
        wy_server["wiki_page"] = {
            "query": {"pages": {"1": {"extract": "晴天，消歧义页，指代多个条目。周杰伦是其中之一。"}}}
        }
        assert await make_enricher().fetch_song_intro_wiki(make_track()) is None

    async def test_singer_not_mentioned_rejected(self, wy_server):
        """正文没提到歌手：宁可不用，避免挂错歌。"""
        wy_server["wiki_page"] = {
            "query": {"pages": {"1": {"extract": "《晴天》是一部动画的片尾曲，作者另有其人。"}}}
        }
        assert await make_enricher().fetch_song_intro_wiki(make_track()) is None

    async def test_no_search_hit_returns_none(self, wy_server):
        wy_server["wiki_search"] = {"query": {"search": []}}
        assert await make_enricher().fetch_song_intro_wiki(make_track()) is None

    async def test_blank_title_returns_none(self):
        from types import SimpleNamespace

        assert await make_enricher().fetch_song_intro_wiki(
            SimpleNamespace(name="  ", singer="x")
        ) is None


class TestResolveWyId:
    async def test_own_platform_direct(self):
        enricher = make_enricher()
        assert await enricher.resolve_wy_id(make_track()) == "186016"

    async def test_cross_platform_mapping(self):
        """非网易云曲目：按「歌名 歌手」在后端找同曲，歌名必须完全一致。"""
        api = FakeApi(results=[make_track(id="wy:999", name="晴天", singer="周杰伦")])
        enricher = make_enricher(api)
        track = make_track(id="tx:0039MnYb0qxYhV", source="tx")
        assert await enricher.resolve_wy_id(track) == "999"
        assert api.calls[0]["keyword"] == "晴天 周杰伦"
        assert api.calls[0]["source"] == "wy"

    async def test_mismatched_name_rejected(self):
        """同名不同版本 / 翻唱不能拿来挂信息（歌名必须归一化后完全一致）。"""
        api = FakeApi(results=[make_track(id="wy:999", name="晴天 (Live)", singer="周杰伦")])
        enricher = make_enricher(api)
        assert await enricher.resolve_wy_id(make_track(id="tx:1", source="tx")) is None

    async def test_mismatched_singer_rejected(self):
        api = FakeApi(results=[make_track(id="wy:999", name="晴天", singer="别人")])
        enricher = make_enricher(api)
        assert await enricher.resolve_wy_id(make_track(id="tx:1", source="tx")) is None

    async def test_search_failure_returns_none(self):
        from astrbot_plugin_moe_music.core.api_client import ApiError

        enricher = make_enricher(FakeApi(error=ApiError(4290, "busy")))
        assert await enricher.resolve_wy_id(make_track(id="tx:1", source="tx")) is None


class TestCardInfoLlm:
    """一次 LLM 调用同时获取歌曲简介 + 歌手简介（+ 年份兜底），JSON 输出。"""

    async def test_json_parsed_all_fields(self):
        provider = FakeProvider(
            '{"year": 2003, "intro": "收录于《叶惠美》的抒情代表作。", '
            '"artist_bio": "华语流行歌手，代表作《晴天》。"}'
        )
        data = await make_enricher(provider=provider).fetch_card_info(
            make_track(), need_year=True, need_intro=True, need_bio=True, umo="umo"
        )
        assert data == {
            "year": 2003,
            "intro": "收录于《叶惠美》的抒情代表作。",
            "artist_bio": "华语流行歌手，代表作《晴天》。",
        }
        assert "晴天" in provider.prompts[0] and "周杰伦" in provider.prompts[0]

    async def test_code_fence_and_prose_stripped(self):
        provider = FakeProvider('好的，如下：\n```json\n{"intro": "秋日抒情小品。"}\n```')
        data = await make_enricher(provider=provider).fetch_card_info(
            make_track(), need_intro=True
        )
        assert data["intro"] == "秋日抒情小品。"

    async def test_only_requested_fields_asked(self):
        provider = FakeProvider('{"intro": "只有简介。"}')
        data = await make_enricher(provider=provider).fetch_card_info(
            make_track(), need_intro=True
        )
        assert data == {"intro": "只有简介。"}
        assert '"artist_bio"' not in provider.prompts[0]
        assert '"year"' not in provider.prompts[0]

    async def test_null_fields_omitted(self):
        provider = FakeProvider('{"year": null, "intro": null, "artist_bio": null}')
        data = await make_enricher(provider=provider).fetch_card_info(
            make_track(), need_year=True, need_intro=True, need_bio=True
        )
        assert data == {}

    async def test_unreasonable_year_rejected(self):
        provider = FakeProvider('{"year": 12345}')
        data = await make_enricher(provider=provider).fetch_card_info(
            make_track(), need_year=True
        )
        assert data == {}

    async def test_no_data_marks_rejected(self):
        provider = FakeProvider('{"intro": "抱歉，暂无资料"}')
        data = await make_enricher(provider=provider).fetch_card_info(
            make_track(), need_intro=True
        )
        assert data == {}

    async def test_garbage_output_returns_empty(self):
        provider = FakeProvider("这不是 JSON")
        data = await make_enricher(provider=provider).fetch_card_info(
            make_track(), need_intro=True
        )
        assert data == {}

    async def test_no_provider_returns_empty(self):
        data = await make_enricher(provider=None).fetch_card_info(
            make_track(), need_intro=True
        )
        assert data == {}

    async def test_provider_error_returns_empty(self):
        import asyncio

        provider = FakeProvider(error=asyncio.TimeoutError())
        data = await make_enricher(provider=provider).fetch_card_info(
            make_track(), need_intro=True
        )
        assert data == {}

    async def test_provider_getter_error_returns_empty(self):
        async def _boom(_umo):
            raise RuntimeError("no provider")

        enricher = Enricher(FakeApi(), provider_getter=_boom)
        assert await enricher.fetch_card_info(make_track(), need_intro=True) == {}

    async def test_no_title_returns_empty(self):
        provider = FakeProvider("{}")
        bare = SimpleNamespace(name="  ", singer="", source="wy", id="wy:1", display="x")
        data = await make_enricher(provider=provider).fetch_card_info(
            bare, need_intro=True
        )
        assert data == {}
        assert provider.prompts == []

    async def test_nothing_needed_returns_empty_without_llm(self):
        provider = FakeProvider("{}")
        data = await make_enricher(provider=provider).fetch_card_info(make_track())
        assert data == {}
        assert provider.prompts == []

    async def test_llm_ask_callable_preferred(self):
        """注入 llm_ask（联网搜索 agent 循环）时优先于直调 provider。"""
        calls: list[str] = []

        async def _ask(event, umo, prompt, system_prompt):
            calls.append(prompt)
            return '{"intro": "来自联网搜索的简介。"}'

        enricher = Enricher(FakeApi(), provider_getter=_boom_getter, llm_ask=_ask)
        data = await enricher.fetch_card_info(make_track(), need_intro=True, umo="umo")
        assert data["intro"] == "来自联网搜索的简介。"
        assert calls and "晴天" in calls[0]


async def _boom_getter(_umo):
    raise RuntimeError("should not be called")
