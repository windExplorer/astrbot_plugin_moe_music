"""SQLite 记录存储测试。"""

from pathlib import Path

from astrbot_plugin_moe_music.core.storage import RecordStore


class TestRecordStore:
    async def test_play_record_roundtrip(self, tmp_path: Path):
        store = RecordStore(tmp_path / "records.db")
        await store.add_play_record(
            trigger_type="command",
            command="点歌",
            user_id="10001",
            user_name="测试用户",
            is_admin=0,
            platform="aiocqhttp",
            message_type="group",
            group_id="20001",
            group_name="测试群",
            unified_msg_origin="qq:GroupMessage:20001",
            track_id="wy:123",
            track_name="晴天",
            singer="周杰伦",
            album="叶惠美",
            source="wy",
            duration=269,
            keyword="晴天",
            selected_index=1,
            selection_type="direct_index",
            quality="320k",
            send_mode="card",
        )
        search_n, play_n = await store.counts()
        assert play_n == 1
        assert search_n == 0

        row = store._conn.execute(
            "SELECT track_name, singer, group_name, quality, send_mode, created_at FROM play_records"
        ).fetchone()
        assert row[0] == "晴天"
        assert row[1] == "周杰伦"
        assert row[2] == "测试群"
        assert row[3] == "320k"
        assert row[4] == "card"
        assert row[5]  # created_at 已自动填充

    async def test_search_record_with_error(self, tmp_path: Path):
        store = RecordStore(tmp_path / "records.db")
        await store.add_search_record(
            trigger_type="command",
            user_id="10001",
            keyword="晴天",
            source="wy",
            result_count=0,
            success=0,
            error_code=4290,
            duration_ms=123,
        )
        await store.add_search_record(
            trigger_type="llm_tool",
            user_id="10002",
            keyword="稻香",
            result_count=5,
            success=1,
            duration_ms=45,
        )
        search_n, play_n = await store.counts()
        assert search_n == 2
        assert play_n == 0
        rows = store._conn.execute(
            "SELECT keyword, success, error_code, trigger_type FROM search_records ORDER BY id"
        ).fetchall()
        assert rows[0] == ("晴天", 0, 4290, "command")
        assert rows[1][2] is None  # 成功记录无错误码

    async def test_unknown_fields_ignored(self, tmp_path: Path):
        store = RecordStore(tmp_path / "records.db")
        await store.add_play_record(track_id="wy:1", not_a_column="x")
        _, play_n = await store.counts()
        assert play_n == 1  # 未知字段被忽略，写入不报错

    async def test_write_failure_does_not_raise(self, tmp_path: Path):
        store = RecordStore(tmp_path / "records.db")
        await store.close()
        # 连接关闭后写入应静默失败（只记日志），不抛异常
        await store.add_play_record(track_id="wy:1")
