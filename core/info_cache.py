"""歌曲 / 歌手信息缓存（歌曲信息卡片用）。

为什么单独建表缓存：卡片上的增强信息（发行年份、网易云热评、歌手简介）都要联网或
调 LLM，首次几秒、还可能被限流；而这些信息**几乎不变**，同一首歌 / 同一位歌手反复
点歌时完全没必要重新抓。缓存后「越用越全」：第一遍基础卡片，第二遍起就是完整卡片。

两张表（与点歌记录同库，见 ``data/moe_music/records.db``）：

- ``song_meta``：按曲目维度（网易云映射 id / 年份 / 热评）；
- ``artist_meta``：按**歌手**维度（简介）——歌手天然跨歌复用，某歌手只生成一次。

**负缓存（重要）**：抓不到也要记时间戳，否则每点一次这首歌都会重打一遍外部接口
（特别是「网易云就是没有发行年份」这种永远抓不到的情况）。命中规则是
``值为空且尝试时间在 RETRY_TTL_SEC 内 → 不再重试``；超过 TTL 才重试一次。

写入失败只记日志，绝不影响点歌主流程。
"""

import asyncio
import sqlite3
import threading
import time
import traceback
from pathlib import Path

from astrbot.api import logger

# 抓取失败后的重试间隔：值为空时，这段时间内不再重复打外部接口 / LLM
RETRY_TTL_SEC = 7 * 86400

_SONG_SCHEMA = """
CREATE TABLE IF NOT EXISTS song_meta (
    track_id          TEXT PRIMARY KEY,   -- 后端曲目 id，如 wy:186016
    wy_id             TEXT,               -- 映射到的网易云曲目 id（空 = 尚未成功映射）
    year              INTEGER,            -- 发行年份（空/0 = 未知）
    intro             TEXT,               -- 歌曲简介（LLM 生成；LLM 不可用时暂存专辑文案）
    intro_source      TEXT,               -- 简介来源：llm=歌曲简介（最终）/ album=专辑文案（占位）
    cover_url         TEXT,               -- 封面直链（后端 wy 无 pic 实现时由此补）
    cover_at          REAL,               -- 封面「尝试」完成时间（含失败，负缓存用）
    hot_comment       TEXT,               -- 热评第一条正文
    hot_comment_user  TEXT,               -- 热评用户昵称
    hot_comment_likes INTEGER,            -- 热评点赞数
    hot_comment_source TEXT,              -- 热评来源平台码（wy/kw/kg）
    mapped_at         REAL,               -- 网易云 id 映射「尝试」完成时间（含失败）
    info_at           REAL,               -- 年份 / 简介 / 热评「尝试」完成时间（含失败）
    updated_at        REAL                -- 最近一次成功写入时间
);
"""

_ARTIST_SCHEMA = """
CREATE TABLE IF NOT EXISTS artist_meta (
    singer      TEXT PRIMARY KEY,   -- 歌手名（曲目里的原始写法）
    bio         TEXT,               -- 简介（空 = 尚未成功生成）
    source      TEXT,               -- 来源标记：llm
    attempt_at  REAL,               -- 「尝试」完成时间（含失败）
    updated_at  REAL                -- 最近一次成功写入时间
);
"""

_SONG_COLUMNS = {
    "track_id",
    "wy_id",
    "year",
    "intro",
    "intro_source",
    "cover_url",
    "cover_at",
    "hot_comment",
    "hot_comment_user",
    "hot_comment_likes",
    "hot_comment_source",
    "mapped_at",
    "info_at",
    "updated_at",
}

_ARTIST_COLUMNS = {"singer", "bio", "source", "attempt_at", "updated_at"}


def _now() -> float:
    return time.time()


class InfoCache:
    """歌曲 / 歌手信息缓存库（SQLite，异步接口）。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._open()
        logger.info(f"[萌音点歌] 信息缓存库已就绪：{self.db_path}")

    # ============ 连接管理（与 RecordStore 同一套思路） ============

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock, conn:
            conn.execute(_SONG_SCHEMA)
            conn.execute(_ARTIST_SCHEMA)
            self._migrate(conn)
        self._conn = conn
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """旧版缓存库补列（CREATE TABLE IF NOT EXISTS 不会给旧表加新列）。"""
        cur = conn.execute("PRAGMA table_info(song_meta)")
        existing = {row[1] for row in cur.fetchall()}
        for column in sorted(_SONG_COLUMNS - existing - {"track_id"}):
            conn.execute(f"ALTER TABLE song_meta ADD COLUMN {column}")
            logger.info(f"[萌音点歌] 信息缓存库已迁移：song_meta 新增 {column} 列")

    def _get_conn(self) -> sqlite3.Connection:
        """取可用连接；插件重载后旧连接被关闭时自动重开（同库文件，WAL 安全）。"""
        if self._conn is not None:
            try:
                self._conn.execute("SELECT 1")
                return self._conn
            except sqlite3.ProgrammingError:
                logger.info("[萌音点歌] 信息缓存库连接已关闭（插件重载），自动重连")
        return self._open()

    # ============ 歌曲维度 ============

    async def get_song(self, track_id: str) -> dict:
        """读曲目缓存；没有记录时返回空 dict。"""
        return await asyncio.to_thread(self._get_row, "song_meta", "track_id", track_id)

    async def upsert_song(self, track_id: str, **fields) -> None:
        """写入曲目缓存（只写已知列，未知键忽略）。"""
        data = {k: v for k, v in fields.items() if k in _SONG_COLUMNS}
        data["track_id"] = str(track_id)
        data["updated_at"] = _now()
        await asyncio.to_thread(self._upsert, "song_meta", "track_id", data)

    # ============ 歌手维度 ============

    async def get_artist(self, singer: str) -> dict:
        return await asyncio.to_thread(self._get_row, "artist_meta", "singer", singer)

    async def upsert_artist(self, singer: str, **fields) -> None:
        """写入歌手缓存；只传 ``attempt_at`` 时不会覆盖已有的简介。"""
        data = {k: v for k, v in fields.items() if k in _ARTIST_COLUMNS}
        data["singer"] = str(singer)
        data["updated_at"] = _now()
        await asyncio.to_thread(self._upsert, "artist_meta", "singer", data)

    # ============ 通用读写（同步，跑在线程池里） ============

    def _get_row(self, table: str, key: str, value) -> dict:
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (value,)).fetchone()
                return dict(row) if row else {}
            finally:
                conn.row_factory = None
        except Exception:
            logger.error(f"[萌音点歌] 读取信息缓存失败（{table}）：\n{traceback.format_exc()}")
            return {}

    def _upsert(self, table: str, key: str, data: dict) -> None:
        keys = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        updates = ", ".join(f"{k} = excluded.{k}" for k in data if k != key)
        sql = (
            f"INSERT INTO {table} ({keys}) VALUES ({marks}) "
            f"ON CONFLICT({key}) DO UPDATE SET {updates}"
        )
        try:
            conn = self._get_conn()
            with self._lock, conn:
                conn.execute(sql, tuple(data.values()))
        except Exception:
            logger.error(f"[萌音点歌] 写入信息缓存失败（{table}）：\n{traceback.format_exc()}")

    async def stats(self) -> tuple[int, int]:
        """(曲目缓存条数, 歌手缓存条数)——自检展示用。"""

        def _q() -> tuple[int, int]:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT (SELECT COUNT(*) FROM song_meta), (SELECT COUNT(*) FROM artist_meta)"
            )
            row = cur.fetchone()
            return (int(row[0]), int(row[1])) if row else (0, 0)

        try:
            return await asyncio.to_thread(_q)
        except Exception:
            logger.error(f"[萌音点歌] 信息缓存统计失败：\n{traceback.format_exc()}")
            return 0, 0

    async def close(self) -> None:
        """关闭连接（terminate 用）；之后仍被访问会自动重连。"""

        def _close() -> None:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

        try:
            await asyncio.to_thread(_close)
        except Exception:
            pass


def fresh(value, attempted_at, ttl: float = RETRY_TTL_SEC) -> bool:
    """``值为空`` 时是否还在「别急着重试」的窗口内（负缓存判定）。

    Args:
        value: 已缓存的值（有值即视为新鲜，无需重试）。
        attempted_at: 上次尝试时间（unix 秒，含失败）；缺失视为很久以前。
        ttl: 重试间隔。
    """
    if value:
        return True
    if not attempted_at:
        return False
    try:
        return (_now() - float(attempted_at)) < ttl
    except (TypeError, ValueError):
        return False
