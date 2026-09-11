"""点歌 / 搜索记录持久化（SQLite）。

设计要点：
- 只写不读（统计功能后续版本再做），但表结构按统计需求设计宽字段；
- sqlite3 同步 API 统一丢进线程池（asyncio.to_thread），不阻塞事件循环；
- 单连接 + 线程锁 + WAL 模式，避免多写者锁库；
- 写入失败只记日志，绝不影响点歌主流程。
"""

import asyncio
import sqlite3
import threading
import traceback
from datetime import datetime
from pathlib import Path

from astrbot.api import logger

_SEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,               -- ISO8601 本地时间
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
    duration_ms     INTEGER NOT NULL DEFAULT 0   -- 后端耗时（毫秒）
);
"""

_PLAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS play_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,               -- ISO8601 本地时间
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
    send_mode       TEXT NOT NULL DEFAULT ''     -- 实际成功的发送模式
);
"""

_SEARCH_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_search_created ON search_records(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_search_user ON search_records(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_search_group ON search_records(group_id)",
    "CREATE INDEX IF NOT EXISTS idx_search_keyword ON search_records(keyword)",
)

_PLAY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_play_created ON play_records(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_play_user ON play_records(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_play_group ON play_records(group_id)",
    "CREATE INDEX IF NOT EXISTS idx_play_track ON play_records(track_id)",
)

# 各表允许写入的列（过滤未知字段，防止表结构漂移时报错）
_SEARCH_COLUMNS = {
    "created_at",
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
}
_PLAY_COLUMNS = {
    "created_at",
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
            for stmt in _SEARCH_INDEXES + _PLAY_INDEXES:
                self._conn.execute(stmt)
        logger.info(f"[萌音点歌] 记录库已就绪：{self.db_path}")

    @staticmethod
    def _filter_fields(fields: dict, allowed: set[str]) -> dict:
        unknown = set(fields) - allowed
        if unknown:
            logger.debug(f"[萌音点歌] 记录字段忽略未定义项：{unknown}")
        data = {k: v for k, v in fields.items() if k in allowed}
        data.setdefault("created_at", now_iso())
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
