"""WebUI 后端 API 测试（统计 SQL / 任务快照 / 配置读写）。"""

import sys
import time  # noqa: F401
from pathlib import Path

from astrbot_plugin_moe_music.core.queue import SongTaskQueue
from astrbot_plugin_moe_music.core.storage import RecordStore
from astrbot_plugin_moe_music.webui_api import MoeWebUIApi


class FakePlugin:
    def __init__(self, store, queue, config=None):
        self.store = store
        self.queue = queue
        self.config = config or {}
        self.applied = None

    async def apply_webui_config(self, clean):
        self.applied = clean
        self.config.update(clean)


def make_api(tmp_path: Path):
    store = RecordStore(tmp_path / "records.db")
    queue = SongTaskQueue(2, 20)
    plugin = FakePlugin(store, queue, {"api_key": "sk-test", "song_limit": 5})
    return MoeWebUIApi(plugin), store, plugin


async def seed(store: RecordStore):
    await store.add_search_record(
        user_id="u1",
        user_name="甲",
        group_id="g1",
        group_name="一群",
        keyword="晴天",
        success=1,
        result_count=5,
        duration_ms=200,
        queue_wait_ms=10,
    )
    await store.add_play_record(
        user_id="u1",
        user_name="甲",
        group_id="g1",
        group_name="一群",
        track_id="wy:1",
        track_name="晴天",
        singer="周杰伦",
        album="叶惠美",
        source="wy",
        quality="128k",
        send_mode="text",
        selection_type="picked",
        trigger_type="command",
        queue_wait_ms=15,
        total_ms=800,
        expires_at="2026-09-12T00:00:00",
        quality_requested="320k",
        quality_fallback=1,
        metadata_embedded=None,
    )


def stub_request_json(payload):
    stub = sys.modules["astrbot.api.web"].request

    async def fake_json(default=None):
        return payload

    stub.json = fake_json


class TestStats:
    async def test_overview(self, tmp_path: Path):
        api, store, _ = make_api(tmp_path)
        await seed(store)
        kind, value, status = await api.stats_overview()
        assert kind == "json" and status == 200
        assert value["search_total"] == 1 and value["play_total"] == 1
        assert value["today_play"] == 1
        assert value["quality_fallback"] == 1
        assert value["overall_play"] == 1

    async def test_trend_shape(self, tmp_path: Path):
        api, store, _ = make_api(tmp_path)
        await seed(store)
        _, value, _ = await api.stats_trend()
        assert len(value["dates"]) == 14
        assert len(value["search"]) == 14 and len(value["play"]) == 14
        assert value["play"][-1] == 1  # 今天有 1 次点歌
        assert sum(value["play"]) == 1

    async def test_top_and_dist(self, tmp_path: Path):
        api, store, _ = make_api(tmp_path)
        await seed(store)
        _, top, _ = await api.stats_top()
        assert top["users"][0]["user_id"] == "u1"
        assert top["groups"][0]["group_name"] == "一群"
        assert top["tracks"][0]["track_name"] == "晴天"

        _, dist, _ = await api.stats_dist()
        assert dist["quality"][0]["k"] == "128k"
        assert dist["send_mode"][0]["k"] == "text"
        assert dist["queue_waits"] == [15]


class TestTasks:
    async def test_queue_snapshot(self, tmp_path: Path):
        api, _, _ = make_api(tmp_path)
        kind, value, _ = await api.tasks_queue()
        assert kind == "json"
        assert value["queue"]["concurrency"] == 2
        assert value["queue"]["pending"] == 0

    async def test_recent(self, tmp_path: Path):
        api, store, _ = make_api(tmp_path)
        await seed(store)
        _, value, _ = await api.tasks_recent()
        assert len(value["plays"]) == 1
        assert len(value["searches"]) == 1
        assert value["plays"][0]["track_name"] == "晴天"


class TestConfig:
    async def test_get_config(self, tmp_path: Path):
        api, _, plugin = make_api(tmp_path)
        _, value, _ = await api.get_config()
        assert value["api_key"] == "sk-test"

    async def test_get_schema(self, tmp_path: Path):
        api, _, _ = make_api(tmp_path)
        _, schema, _ = await api.get_schema()
        assert "api_key" in schema
        assert schema["song_limit"]["type"] == "int"

    async def test_save_config_applies(self, tmp_path: Path):
        api, _, plugin = make_api(tmp_path)
        stub_request_json({"song_limit": 9, "hacker_key": "x"})
        kind, value, _ = await api.save_config()
        assert kind == "json" and value["saved"] is True
        assert plugin.applied == {"song_limit": 9}  # 非白名单键被过滤
        assert plugin.config["song_limit"] == 9

    async def test_save_config_rejects_empty(self, tmp_path: Path):
        api, _, _ = make_api(tmp_path)
        stub_request_json({})
        kind, value, status = await api.save_config()
        assert kind == "error"
