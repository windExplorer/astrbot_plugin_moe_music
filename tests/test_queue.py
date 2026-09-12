"""任务队列与时间统计字段测试（v0.6.0）。"""

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
from astrbot_plugin_moe_music.core.queue import SongTaskQueue


class TestSongTaskQueue:
    async def test_submit_returns_result_and_records_queue_wait(self):
        q = SongTaskQueue(concurrency=1, max_pending=5)
        await q.start()
        try:
            got = {}

            async def job(value, queue_wait_ms=0):
                got["wait"] = queue_wait_ms
                return value * 2

            assert await q.submit(job, 21) == 42
            assert isinstance(got["wait"], int) and got["wait"] >= 0
            assert q.submitted == 1
        finally:
            await q.stop()

    async def test_concurrency_limited(self):
        q = SongTaskQueue(concurrency=2, max_pending=20)
        await q.start()
        try:
            running = 0
            peak = 0

            async def job(queue_wait_ms=0):
                nonlocal running, peak
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.05)
                running -= 1

            await asyncio.gather(*(q.submit(job) for _ in range(8)))
            assert peak <= 2  # 并发度上限生效
        finally:
            await q.stop()

    async def test_queue_full_rejects(self):
        q = SongTaskQueue(concurrency=1, max_pending=1)
        block = asyncio.Event()

        async def slow(queue_wait_ms=0):
            await block.wait()

        q._running = True  # 阻止 submit 懒启动 worker，手工占位
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        q._queue.put_nowait((slow, (), {}, fut, time.monotonic()))  # 占满队列

        async def job(queue_wait_ms=0):
            return 1

        with pytest.raises(asyncio.QueueFull):
            await q.submit(job)
        assert q.rejected == 1
        block.set()

    async def test_exception_propagates(self):
        q = SongTaskQueue(concurrency=1, max_pending=5)
        await q.start()
        try:

            async def boom(queue_wait_ms=0):
                raise ValueError("boom")

            with pytest.raises(ValueError):
                await q.submit(boom)
        finally:
            await q.stop()

    async def test_lazy_start(self):
        # 未调用 start 时 submit 应自动拉起 worker，不挂起
        q = SongTaskQueue(concurrency=1, max_pending=5)

        async def job(queue_wait_ms=0):
            return "ok"

        assert await asyncio.wait_for(q.submit(job), timeout=3) == "ok"
        await q.stop()


class TestTimingFields:
    """记录库时间统计字段（v0.6.0）。"""

    def _make_store(self, tmp_path: Path):
        from astrbot_plugin_moe_music.core.storage import RecordStore

        return RecordStore(tmp_path / "records.db")

    async def test_new_columns_present_and_written(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        await store.add_play_record(
            user_id="1",
            track_id="wy:1",
            expires_at="2026-09-12T00:00:00",
            quality_requested="flac",
            quality_fallback=1,
            metadata_embedded=1,
            queue_wait_ms=123,
            search_ms=456,
            resolve_ms=78,
            download_ms=999,
            embed_ms=12,
            send_ms=34,
            total_ms=1700,
        )
        row = store._conn.execute(
            "SELECT ts, queue_wait_ms, total_ms, search_ms, resolve_ms, download_ms, "
            "embed_ms, send_ms, expires_at, quality_requested, quality_fallback, metadata_embedded "
            "FROM play_records"
        ).fetchone()
        assert row[0] is not None and abs(row[0] - time.time()) < 60  # ts 自动填充
        assert (row[1], row[2]) == (123, 1700)
        assert row[5] == 999 and row[6] == 12 and row[7] == 34
        assert row[8] == "2026-09-12T00:00:00"
        assert row[9] == "flac" and row[10] == 1 and row[11] == 1
        await store.close()

    async def test_search_record_queue_wait(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        await store.add_search_record(user_id="1", keyword="x", queue_wait_ms=77, duration_ms=210)
        row = store._conn.execute("SELECT queue_wait_ms, duration_ms, ts FROM search_records").fetchone()
        assert row == (77, 210, row[2]) and row[2] is not None
        await store.close()

    async def test_migration_from_old_schema(self, tmp_path: Path):
        """v0.5.x 旧库（无新列）打开后自动补列且旧数据保留。"""
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE play_records (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
            "user_id TEXT NOT NULL DEFAULT '', track_id TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO play_records (created_at, user_id, track_id) VALUES ('2026-01-01', 'u1', 'wy:9')"
        )
        conn.commit()
        conn.close()

        from astrbot_plugin_moe_music.core.storage import RecordStore

        store = RecordStore(db_path)
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(play_records)").fetchall()}
        assert {"ts", "queue_wait_ms", "total_ms", "expires_at", "metadata_embedded"} <= cols
        row = store._conn.execute("SELECT user_id, track_id FROM play_records").fetchone()
        assert row == ("u1", "wy:9")  # 旧数据完好

        # 旧库上仍可正常写入（新列自动填充）
        await store.add_play_record(user_id="u2", track_id="wy:10", total_ms=55)
        n = store._conn.execute("SELECT COUNT(*) FROM play_records").fetchone()[0]
        assert n == 2
        await store.close()


class TestTsBackfill:
    """旧记录 ts 回填（v0.10.2）：迁移补列后 ts 为 NULL 的旧行会被统计过滤。"""

    def test_backfill_from_created_at(self, tmp_path: Path):
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE play_records (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "created_at TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '')"
        )
        conn.execute("INSERT INTO play_records (created_at, user_id) VALUES ('2026-09-01T10:30:00', 'u1')")
        conn.commit()
        conn.close()

        from astrbot_plugin_moe_music.core.storage import RecordStore

        store = RecordStore(db_path)
        row = store._conn.execute("SELECT ts FROM play_records WHERE id = 1").fetchone()
        assert row[0] is not None
        # 回填的时间戳与 created_at 解析一致（本地时区）
        assert abs(row[0] - time.mktime(time.strptime("2026-09-01T10:30:00", "%Y-%m-%dT%H:%M:%S"))) < 1
        # 统计查询（ts 过滤）能看到旧记录
        rows = store._conn.execute(
            "SELECT COUNT(*) FROM play_records WHERE ts >= 0"
        ).fetchone()
        assert rows[0] == 1
        # 幂等：再次打开不重复回填也不报错
        store2 = RecordStore(db_path)
        n = store2._conn.execute("SELECT COUNT(*) FROM play_records WHERE ts IS NULL").fetchone()[0]
        assert n == 0
        store.close()
        store2.close()

    def test_invalid_created_at_skipped(self, tmp_path: Path):
        db_path = tmp_path / "bad.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE search_records (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "created_at TEXT NOT NULL, keyword TEXT NOT NULL DEFAULT '')"
        )
        conn.execute("INSERT INTO search_records (created_at, keyword) VALUES ('not-a-date', 'x')")
        conn.commit()
        conn.close()

        from astrbot_plugin_moe_music.core.storage import RecordStore

        store = RecordStore(db_path)  # 不应抛异常
        row = store._conn.execute("SELECT ts FROM search_records").fetchone()
        assert row[0] is None  # 无法解析的保持 NULL
        store.close()


class TestConnSelfHeal:
    """连接自愈（v0.10.4）：terminate 关库后旧引用访问自动重连（插件重载场景）。"""

    def test_query_after_close_reconnects(self, tmp_path: Path):
        from astrbot_plugin_moe_music.core.storage import RecordStore

        store = RecordStore(tmp_path / "records.db")
        asyncio.run(store.add_play_record(user_id="u1", track_id="wy:1"))
        # 模拟插件重载：连接被关闭
        store._conn.close()
        store._conn = None
        # 旧引用再查询：应自动重连并返回数据（而非 "closed database"）
        got = asyncio.run(store.query("SELECT user_id FROM play_records"))
        assert got == [{"user_id": "u1"}]

    def test_write_after_close_reconnects(self, tmp_path: Path):
        from astrbot_plugin_moe_music.core.storage import RecordStore

        store = RecordStore(tmp_path / "records.db")
        store._conn.close()
        store._conn = None
        asyncio.run(store.add_play_record(user_id="u2"))
        _, play_n = asyncio.run(store.counts())
        assert play_n == 1
