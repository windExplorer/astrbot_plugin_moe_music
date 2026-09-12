"""点歌 / 搜索记录持久化（SQLite）。

设计要点：
- 只写不读（统计功能后续版本再做），但表结构按统计需求设计宽字段；
- 时间维度双存储：``created_at``（ISO 本地时间，人读）+ ``ts``（unix 秒时间戳，
  统计排序 / 区间过滤用），均为写入时自动填充；
- 耗时维度完整埋点：排队（queue_wait_ms）、后端请求（duration_ms）、
  搜索 / 取链 / 下载 / 嵌入 / 发送各阶段与全流程（total_ms）；
- sqlite3 同步 API 统一丢进线程池（asyncio.to_thread），不阻塞事件循环；
- 单连接 + 线程锁 + WAL 模式，避免多写者锁库；
- 写入失败只记日志，绝不影响点歌主流程；
- 旧版本库自动迁移：启动时按 PRAGMA table_info 补缺失列（ALTER TABLE ADD COLUMN）。
"""

import asyncio
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from astrbot.api import logger

_SEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,               -- ISO8601 本地时间
    ts              REAL,                        -- unix 秒时间戳（统计用）
    trigger_type    TEXT NOT NULL DEFAULT 'command',  -- command / llm_tool
    user_id         TEXT NOT NULL DEFAULT '',    -- 发起人 QQ 号 / 平台用户 id
    user_name       TEXT NOT NULL DEFAULT '',    -- 发起人昵称
    is_admin        INTEGER NOT NULL DEFAULT 0,  -- 是否 AstrBot 管理员
    platform        TEXT NOT NULL DEFAULT '',    -- 消息平台（aiocqhttp 等）
    message_type    TEXT NOT NULL DEFAULT '',    -- group / private
    group_id        TEXT NOT NULL DEFAULT '',    -- 群号（私聊为空）
    group_name      TEXT NOT NULL DEFAULT '',    -- 群名称
    unified_msg_origin TEXT NOT NULL DEFAULT '', -- AstrBot 统一会话源
    keyword         TEXT NOT NULL DEFAULT '',    -- 搜索关键词
    source          TEXT NOT NULL DEFAULT '',    -- 请求音源（空 = 聚合）
    result_count    INTEGER NOT NULL DEFAULT 0,  -- 命中数量
    success         INTEGER NOT NULL DEFAULT 1,  -- 搜索是否成功
    error_code      INTEGER,                     -- 失败时的错误码（业务/网络占位 -1）
    duration_ms     INTEGER NOT NULL DEFAULT 0,  -- 后端请求耗时（毫秒）
    queue_wait_ms   INTEGER                      -- 搜索任务排队耗时（毫秒）
);
"""

_PLAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS play_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,               -- ISO8601 本地时间
    ts              REAL,                        -- unix 秒时间戳（统计用）
    trigger_type    TEXT NOT NULL DEFAULT 'command',  -- command / llm_tool
    command         TEXT NOT NULL DEFAULT '',    -- 命令词（点歌/网易点歌…）或 LLM tool 名
    user_id         TEXT NOT NULL DEFAULT '',
    user_name       TEXT NOT NULL DEFAULT '',
    is_admin        INTEGER NOT NULL DEFAULT 0,
    platform        TEXT NOT NULL DEFAULT '',
    message_type    TEXT NOT NULL DEFAULT '',
    group_id        TEXT NOT NULL DEFAULT '',
    group_name      TEXT NOT NULL DEFAULT '',
    unified_msg_origin TEXT NOT NULL DEFAULT '',
    track_id        TEXT NOT NULL DEFAULT '',    -- 后端曲目 id（如 wy:123）
    track_name      TEXT NOT NULL DEFAULT '',
    singer          TEXT NOT NULL DEFAULT '',
    album           TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT '',    -- 音源平台码
    duration        INTEGER NOT NULL DEFAULT 0,  -- 曲目时长（秒）
    keyword         TEXT NOT NULL DEFAULT '',    -- 搜索关键词
    selected_index  INTEGER NOT NULL DEFAULT 0,  -- 选中序号（单曲直发 = 1）
    selection_type  TEXT NOT NULL DEFAULT '',    -- direct_index / single / picked / llm
    quality         TEXT NOT NULL DEFAULT '',    -- 实际下发音质
    send_mode       TEXT NOT NULL DEFAULT '',    -- 实际成功的发送模式
    expires_at      TEXT,                        -- 临时链接过期时间（后端签发）
    quality_requested TEXT,                      -- 期望音质（配置的默认音质）
    quality_fallback  INTEGER,                   -- 是否发生音质降级收敛（0/1）
    metadata_embedded INTEGER,                   -- 元数据嵌入结果（1 成功 / 0 失败 / NULL 不适用）
    queue_wait_ms   INTEGER,                     -- 发送任务排队耗时（毫秒）
    search_ms       INTEGER,                     -- 搜索阶段耗时（毫秒）
    resolve_ms      INTEGER,                     -- 获取播放链接耗时（毫秒）
    download_ms     INTEGER,                     -- 音频下载耗时（毫秒，本地模式）
    embed_ms        INTEGER,                     -- 元数据嵌入耗时（毫秒）
    send_ms         INTEGER,                     -- 发送消息动作耗时（毫秒）
    total_ms        INTEGER                      -- 全流程耗时（入队 → 发送完成，毫秒）
);
"""

_SEARCH_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_search_created ON search_records(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_search_ts ON search_records(ts)",
    "CREATE INDEX IF NOT EXISTS idx_search_user ON search_records(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_search_group ON search_records(group_id)",
    "CREATE INDEX IF NOT EXISTS idx_search_keyword ON search_records(keyword)",
)

_PLAY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_play_created ON play_records(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_play_ts ON play_records(ts)",
    "CREATE INDEX IF NOT EXISTS idx_play_user ON play_records(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_play_group ON play_records(group_id)",
    "CREATE INDEX IF NOT EXISTS idx_play_track ON play_records(track_id)",
)

# 各表允许写入的列（过滤未知字段，防止表结构漂移时报错）
_SEARCH_COLUMNS = {
    "created_at",
    "ts",
    "trigger_type",
    "user_id",
    "user_name",
    "is_admin",
    "platform",
    "message_type",
    "group_id",
    "group_name",
    "unified_msg_origin",
    "keyword",
    "source",
    "result_count",
    "success",
    "error_code",
    "duration_ms",
    "queue_wait_ms",
}
_PLAY_COLUMNS = {
    "created_at",
    "ts",
    "trigger_type",
    "command",
    "user_id",
    "user_name",
    "is_admin",
    "platform",
    "message_type",
    "group_id",
    "group_name",
    "unified_msg_origin",
    "track_id",
    "track_name",
    "singer",
    "album",
    "source",
    "duration",
    "keyword",
    "selected_index",
    "selection_type",
    "quality",
    "send_mode",
    "expires_at",
    "quality_requested",
    "quality_fallback",
    "metadata_embedded",
    "queue_wait_ms",
    "search_ms",
    "resolve_ms",
    "download_ms",
    "embed_ms",
    "send_ms",
    "total_ms",
}

# 旧库迁移：缺这些列时自动 ALTER TABLE ADD COLUMN（v0.5.x 及更早的库升级用）
_TABLE_COLUMNS = {
    "search_records": _SEARCH_COLUMNS,
    "play_records": _PLAY_COLUMNS,
}


def now_iso() -> str:
    """本地时间 ISO8601（秒级）。"""
    return datetime.now().isoformat(timespec="seconds")


class RecordStore:
    """异步写入的点歌记录库。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock, self._conn:
            self._conn.execute(_SEARCH_SCHEMA)
            self._conn.execute(_PLAY_SCHEMA)
            self._migrate()  # 先补列（旧库），否则基于新列的索引会创建失败
            for stmt in _SEARCH_INDEXES + _PLAY_INDEXES:
                self._conn.execute(stmt)
        logger.info(f"[萌音点歌] 记录库已就绪：{self.db_path}")

    def _migrate(self) -> None:
        """为旧版本库补齐新增列（幂等）。"""
        migrated = 0
        for table, expected in _TABLE_COLUMNS.items():
            cur = self._conn.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in cur.fetchall()}
            missing = expected - existing
            for col in sorted(missing):
                # 全部新列均为可空类型，ALTER ADD COLUMN 无需默认值
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                migrated += 1
        if migrated:
            logger.info(f"[萌音点歌] 记录库已迁移：新增 {migrated} 列")

    @staticmethod
    def _filter_fields(fields: dict, allowed: set[str]) -> dict:
        unknown = set(fields) - allowed
        if unknown:
            logger.debug(f"[萌音点歌] 记录字段忽略未定义项：{unknown}")
        data = {k: v for k, v in fields.items() if k in allowed}
        data.setdefault("created_at", now_iso())
        data.setdefault("ts", round(time.time(), 3))
        return data

    def _insert_sync(self, table: str, columns: set[str], fields: dict) -> None:
        data = self._filter_fields(fields, columns)
        keys = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        with self._lock, self._conn:
            self._conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(data.values()))

    async def add_search_record(self, **fields) -> None:
        """记录一次搜索（成功/失败都记）。失败不影响点歌流程。"""
        try:
            await asyncio.to_thread(self._insert_sync, "search_records", _SEARCH_COLUMNS, fields)
        except Exception:
            logger.error(f"[萌音点歌] 搜索记录写入失败：\n{traceback.format_exc()}")

    async def add_play_record(self, **fields) -> None:
        """记录一次成功点歌。失败不影响点歌流程。"""
        try:
            await asyncio.to_thread(self._insert_sync, "play_records", _PLAY_COLUMNS, fields)
        except Exception:
            logger.error(f"[萌音点歌] 点歌记录写入失败：\n{traceback.format_exc()}")

    async def counts(self) -> tuple[int, int]:
        """两表行数（自检/诊断用）。"""

        def _q():
            cur = self._conn.cursor()
            cur.execute("SELECT (SELECT COUNT(*) FROM search_records), (SELECT COUNT(*) FROM play_records)")
            return cur.fetchone()

        return await asyncio.to_thread(_q)

    async def close(self) -> None:
        try:
            await asyncio.to_thread(self._conn.close)
        except Exception:
            pass
