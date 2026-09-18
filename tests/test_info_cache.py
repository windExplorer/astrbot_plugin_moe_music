"""信息缓存测试（core/info_cache.py）：部分更新不覆盖旧值、负缓存 TTL 语义。"""

import time
from pathlib import Path

from astrbot_plugin_moe_music.core.info_cache import RETRY_TTL_SEC, InfoCache, fresh


def make_cache(tmp_path: Path) -> InfoCache:
    return InfoCache(tmp_path / "records.db")


class TestSongMeta:
    async def test_upsert_and_read(self, tmp_path):
        cache = make_cache(tmp_path)
        await cache.upsert_song("wy:1", wy_id="1", year=2014)
        row = await cache.get_song("wy:1")
        assert row["wy_id"] == "1"
        assert row["year"] == 2014

    async def test_missing_returns_empty(self, tmp_path):
        assert await make_cache(tmp_path).get_song("wy:404") == {}

    async def test_partial_update_keeps_other_fields(self, tmp_path):
        """后写 year 不能把先前的 wy_id / 热评冲掉（列级 UPSERT）。"""
        cache = make_cache(tmp_path)
        await cache.upsert_song("wy:1", wy_id="1", mapped_at=1.0)
        await cache.upsert_song(
            "wy:1", hot_comment="好听", hot_comment_user="网友", hot_comment_likes=12
        )
        await cache.upsert_song("wy:1", year=2014)
        row = await cache.get_song("wy:1")
        assert row["wy_id"] == "1"
        assert row["hot_comment"] == "好听"
        assert row["hot_comment_likes"] == 12
        assert row["year"] == 2014
        assert row["mapped_at"] == 1.0

    async def test_unknown_fields_ignored(self, tmp_path):
        cache = make_cache(tmp_path)
        await cache.upsert_song("wy:1", year=2014, not_a_column="x")
        assert "not_a_column" not in await cache.get_song("wy:1")


class TestArtistMeta:
    async def test_bio_cached(self, tmp_path):
        cache = make_cache(tmp_path)
        await cache.upsert_artist("周杰伦", bio="华语流行歌手", source="llm")
        row = await cache.get_artist("周杰伦")
        assert row["bio"] == "华语流行歌手"
        assert row["source"] == "llm"

    async def test_attempt_only_does_not_wipe_bio(self, tmp_path):
        """只记「尝试过」时不能把已有简介写成空（失败重试会覆盖成功结果）。"""
        cache = make_cache(tmp_path)
        await cache.upsert_artist("周杰伦", bio="华语流行歌手")
        await cache.upsert_artist("周杰伦", attempt_at=123.0)
        row = await cache.get_artist("周杰伦")
        assert row["bio"] == "华语流行歌手"
        assert row["attempt_at"] == 123.0


class TestStatsAndFresh:
    async def test_stats(self, tmp_path):
        cache = make_cache(tmp_path)
        await cache.upsert_song("wy:1", year=2014)
        await cache.upsert_song("wy:2", year=2015)
        await cache.upsert_artist("周杰伦", bio="x")
        assert await cache.stats() == (2, 1)

    async def test_fresh_semantics(self):
        now = time.time()
        assert fresh("有值", None) is True  # 有值就不用重试
        assert fresh("", None) is False  # 从没试过 → 该去试
        assert fresh("", now - 60, ttl=RETRY_TTL_SEC) is True  # 刚试过且没拿到 → 先别急
        assert fresh("", now - RETRY_TTL_SEC - 10, ttl=RETRY_TTL_SEC) is False  # 过了 TTL → 重试
        assert fresh("", "坏数据") is False  # 时间戳不可解析 → 当作需要重试
