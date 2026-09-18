"""萌音控制台 WebUI 后端 API。

通过 AstrBot 官方插件 Pages 机制提供（docs/zh/dev/star/guides/plugin-pages.md）：
- 路由经 context.register_web_api 注册，前缀 /astrbot_plugin_moe_music/...；
- handler 只用 astrbot.api.web 的 request / json_response / error_response /
  stream_response，不暴露底层框架请求对象；
- 前端 bridge endpoint 为相对路径（不含插件名）。

页面（pages/moe-console/，Vue3 + Naive UI + ECharts 构建产物）：
- 统计页：总量 / 趋势 / 用户·群·歌曲排行 / 音质与发送方式分布
- 实时任务页：队列快照 + 最近记录（SSE 推送）
- 配置页：读 schema 结构化渲染，保存走 AstrBotConfig
"""

import asyncio
import datetime as _dt
import json
import traceback
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request, stream_response

from .core.config import parse_str_list

PLUGIN_NAME = "astrbot_plugin_moe_music"

# 当前活跃的插件实例（register_web_api 更新）。插件重载后旧路由经此转发到新实例。
_active_plugin = None

# 统计范围定义：key -> (显示名, 趋势分桶, 自然单位数)
# bucket: hour=按整点小时 / day=按日期 / month=按月份
_RANGE_DEFS = {
    "today": ("今天", "hour", 1),
    "24h": ("近一天", "hour", 1),
    "3d": ("近三天", "day", 3),
    "7d": ("近一周", "day", 7),
    "14d": ("近14天", "day", 14),
    "30d": ("近一月", "day", 30),
    "90d": ("近90天", "day", 90),
    "1y": ("近一年", "month", 12),
}

# 业务枚举中文名（WebUI 展示用）
SEND_MODE_NAMES = {
    "card": "音乐卡片",
    "record_link": "语音链接",
    "record_local": "本地语音",
    "file_link": "文件链接",
    "file_local": "本地文件",
    "text": "文本链接",
}
SELECTION_NAMES = {
    "direct_index": "命令序号",
    "single": "单曲直发",
    "picked": "回复序号",
    "llm": "AI 点歌",
    "share": "分享识别",
}
TRIGGER_NAMES = {"command": "命令", "llm_tool": "AI", "share": "分享识别"}
SOURCE_NAMES = {
    "kw": "酷我",
    "kg": "酷狗",
    "tx": "QQ音乐",
    "wy": "网易云",
    "mg": "咪咕",
    "xm": "虾米",
    "bd": "百度",
}


def _resolve_range(range_key: str) -> tuple[str, str, float]:
    """解析统计范围，返回 (标准化 key, 分桶方式, 起始 unix 时间戳)。"""
    key = range_key if range_key in _RANGE_DEFS else "7d"
    label, bucket, units = _RANGE_DEFS[key]
    now = _dt.datetime.now()
    if key == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif key == "24h":
        start = now - _dt.timedelta(hours=24)
    elif key == "1y":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for _ in range(units - 1):
            start = (start - _dt.timedelta(days=1)).replace(day=1)
    else:
        start = (now - _dt.timedelta(days=units - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return key, bucket, start.timestamp()


# 配置页允许通过 WebUI 保存的键（与 _conf_schema.json 对齐；未列出的键忽略）
_EDITABLE_KEYS = {
    "api_base_url",
    "api_key",
    "public_base_url",
    "default_source",
    "default_quality",
    "song_limit",
    "selection_display",
    "send_modes",
    "record_via_onebot",
    "timeout",
    "request_timeout",
    "queue_concurrency",
    "queue_max_pending",
    "enable_lyrics",
    "embed_metadata",
    "file_quality",
    "file_embed_metadata",
    "recall_candidate",
    "share_auto_play",
    "share_send_modes",
    "share_quality",
    "song_card_enable",
    "song_card_year",
    "song_card_intro",
    "song_card_llm_sync",
    "song_card_comment",
    "song_card_artist_bio",
    "song_card_repeat_sec",
    "proxy",
    "enable_self_test",
    "whitelist_groups",
    "whitelist_users",
    "blacklist_groups",
    "blacklist_users",
}


class MoeWebUIApi:
    """WebUI 后端 handler 集合（方法体通过 plugin 访问运行时组件）。"""

    def __init__(self, plugin):
        self.plugin = plugin

    @property
    def store(self):
        return self.plugin.store

    # ============ 统计 ============

    async def stats_overview(self):
        range_key, _, since = _resolve_range(_q_str("range", "7d"))
        total_search = await self.store.query(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(success), 0) AS ok, "
            "COALESCE(AVG(duration_ms), 0) AS avg_ms FROM search_records WHERE ts >= ?",
            (since,),
        )
        total_play = await self.store.query(
            "SELECT COUNT(*) AS n, "
            "COALESCE(AVG(total_ms), 0) AS avg_total, "
            "COALESCE(AVG(queue_wait_ms), 0) AS avg_queue, "
            "COALESCE(SUM(quality_fallback), 0) AS fallback, "
            "COALESCE(SUM(metadata_embedded = 1), 0) AS embedded, "
            "COALESCE(SUM(metadata_embedded IS NOT NULL), 0) AS embed_applicable "
            "FROM play_records WHERE ts >= ?",
            (since,),
        )
        overall = await self.store.query(
            "SELECT (SELECT COUNT(*) FROM search_records) AS search, "
            "(SELECT COUNT(*) FROM play_records) AS play"
        )
        s = total_search[0] if total_search else {}
        p = total_play[0] if total_play else {}
        embed_applicable = int(p.get("embed_applicable") or 0)
        return json_response(
            {
                "range": range_key,
                "range_label": _RANGE_DEFS[range_key][0],
                "db_path": str(self.plugin.store.db_path),
                "search_total": int(s.get("n") or 0),
                "search_ok": int(s.get("ok") or 0),
                "search_avg_ms": round(float(s.get("avg_ms") or 0)),
                "play_total": int(p.get("n") or 0),
                "play_avg_total_ms": round(float(p.get("avg_total") or 0)),
                "play_avg_queue_ms": round(float(p.get("avg_queue") or 0)),
                "quality_fallback": int(p.get("fallback") or 0),
                "embed_rate": (int(p.get("embedded") or 0) / embed_applicable) if embed_applicable else None,
                "overall_search": int(overall[0]["search"]) if overall else 0,
                "overall_play": int(overall[0]["play"]) if overall else 0,
            }
        )

    async def stats_trend(self):
        range_key, bucket, since = _resolve_range(_q_str("range", "7d"))
        if bucket == "hour":
            expr = "strftime('%Y-%m-%d %H:00', created_at)"
            keys, labels = _hour_buckets(range_key, since)
        elif bucket == "month":
            expr = "strftime('%Y-%m', created_at)"
            keys, labels = _month_buckets()
        else:
            expr = "date(created_at)"
            keys, labels = _day_buckets(range_key)

        search_rows = await self.store.query(
            f"SELECT {expr} AS d, COUNT(*) AS n FROM search_records WHERE ts >= ? GROUP BY d",
            (since,),
        )
        play_rows = await self.store.query(
            f"SELECT {expr} AS d, COUNT(*) AS n FROM play_records WHERE ts >= ? GROUP BY d",
            (since,),
        )
        search_map = {r["d"]: r["n"] for r in search_rows}
        play_map = {r["d"]: r["n"] for r in play_rows}
        return json_response(
            {
                "range": range_key,
                "bucket": bucket,
                "dates": labels,
                "search": [int(search_map.get(k, 0)) for k in keys],
                "play": [int(play_map.get(k, 0)) for k in keys],
            }
        )

    async def stats_top(self):
        limit = _q_int("limit", 10, 1, 50)
        users = await self.store.query(
            "SELECT user_id, MAX(user_name) AS user_name, COUNT(*) AS n "
            "FROM play_records WHERE user_id != '' GROUP BY user_id ORDER BY n DESC LIMIT ?",
            (limit,),
        )
        groups = await self.store.query(
            "SELECT group_id, MAX(group_name) AS group_name, COUNT(*) AS n "
            "FROM play_records WHERE group_id != '' GROUP BY group_id ORDER BY n DESC LIMIT ?",
            (limit,),
        )
        tracks = await self.store.query(
            "SELECT track_id, MAX(track_name) AS track_name, MAX(singer) AS singer, "
            "MAX(source) AS source, COUNT(*) AS n "
            "FROM play_records WHERE track_id != '' GROUP BY track_id ORDER BY n DESC LIMIT ?",
            (limit,),
        )
        return json_response({"users": users, "groups": groups, "tracks": tracks})

    async def stats_dist(self):
        range_key, _, since = _resolve_range(_q_str("range", "30d"))
        quality = await self.store.query(
            "SELECT COALESCE(NULLIF(quality, ''), '未知') AS k, COUNT(*) AS n "
            "FROM play_records WHERE ts >= ? GROUP BY k ORDER BY n DESC",
            (since,),
        )
        send_mode = await self.store.query(
            "SELECT COALESCE(NULLIF(send_mode, ''), '未知') AS k, COUNT(*) AS n "
            "FROM play_records WHERE ts >= ? GROUP BY k ORDER BY n DESC",
            (since,),
        )
        selection = await self.store.query(
            "SELECT COALESCE(NULLIF(selection_type, ''), '未知') AS k, COUNT(*) AS n "
            "FROM play_records WHERE ts >= ? GROUP BY k ORDER BY n DESC",
            (since,),
        )
        trigger = await self.store.query(
            "SELECT COALESCE(NULLIF(trigger_type, ''), '未知') AS k, COUNT(*) AS n "
            "FROM play_records WHERE ts >= ? GROUP BY k ORDER BY n DESC",
            (since,),
        )
        source = await self.store.query(
            "SELECT COALESCE(NULLIF(source, ''), '未知') AS k, COUNT(*) AS n "
            "FROM play_records WHERE ts >= ? GROUP BY k ORDER BY n DESC",
            (since,),
        )
        cost = await self.store.query(
            "SELECT queue_wait_ms AS v FROM play_records "
            "WHERE ts >= ? AND queue_wait_ms IS NOT NULL ORDER BY v",
            (since,),
        )
        return json_response(
            {
                "range": range_key,
                "quality": quality,
                "send_mode": _translate(send_mode, SEND_MODE_NAMES),
                "selection": _translate(selection, SELECTION_NAMES),
                "trigger": _translate(trigger, TRIGGER_NAMES),
                "source": _translate(source, SOURCE_NAMES),
                "queue_waits": [int(r["v"]) for r in cost],
            }
        )

    # ============ 实时任务 ============

    async def tasks_queue(self):
        counts = await self.store.counts()
        return json_response(
            {
                "queue": self.plugin.queue.snapshot(),
                "db": {
                    "path": str(self.plugin.store.db_path),
                    "search_total": counts[0],
                    "play_total": counts[1],
                },
            }
        )

    async def tasks_recent(self):
        limit = _q_int("limit", 20, 1, 100)
        plays = await self.store.query(
            "SELECT id, created_at, user_name, user_id, group_name, group_id, "
            "track_name, singer, source, quality, send_mode, selection_type, "
            "trigger_type, queue_wait_ms, search_ms, process_ms, total_ms FROM play_records "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        searches = await self.store.query(
            "SELECT id, created_at, user_name, user_id, group_name, keyword, "
            "result_count, success, error_code, duration_ms, queue_wait_ms "
            "FROM search_records ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return json_response({"plays": plays, "searches": searches})

    async def _tasks_snapshot(self, limit: int = 10) -> dict:
        """SSE 快照：字段必须与前端表格列一致（缺列会渲染成 undefined）。"""
        plays = await self.store.query(
            "SELECT id, created_at, user_name, user_id, group_name, group_id, "
            "track_name, singer, source, quality, send_mode, selection_type, "
            "trigger_type, queue_wait_ms, search_ms, process_ms, total_ms FROM play_records "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        searches = await self.store.query(
            "SELECT id, created_at, user_name, user_id, group_name, keyword, "
            "result_count, success, error_code, duration_ms, queue_wait_ms "
            "FROM search_records ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return {"queue": self.plugin.queue.snapshot(), "plays": plays, "searches": searches}

    async def tasks_stream(self):
        """SSE：每 2 秒推送队列快照与最近记录（实时任务页订阅）。"""

        async def events():
            try:
                while True:
                    payload = json.dumps(await self._tasks_snapshot(10), ensure_ascii=False)
                    yield f"data: {payload}\n\n"
                    await asyncio.sleep(2)
            except asyncio.CancelledError:  # 页面关闭断开
                raise

        return stream_response(events())

    # ============ 配置 ============

    async def get_schema(self):
        schema_path = Path(__file__).parent / "_conf_schema.json"
        try:
            return json_response(json.loads(schema_path.read_text(encoding="utf-8")))
        except Exception as e:
            return error_response(f"读取配置 schema 失败：{e}", status_code=500)

    async def get_config(self):
        raw = self.plugin.config
        return json_response({k: raw.get(k) for k in _EDITABLE_KEYS if k in raw})

    async def get_changelog(self):
        """更新日志（CHANGELOG.md 原文，前端渲染）。"""
        path = Path(__file__).parent / "CHANGELOG.md"
        try:
            return json_response({"content": path.read_text(encoding="utf-8")})
        except Exception as e:
            return error_response(f"读取更新日志失败：{e}", status_code=500)

    async def save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict) or not payload:
            return error_response("请求体为空")
        clean = _normalize_payload({k: v for k, v in payload.items() if k in _EDITABLE_KEYS})
        if not clean:
            return error_response("没有可保存的配置项")
        try:
            await self.plugin.apply_webui_config(clean)
        except Exception as e:
            logger.error(f"[萌音点歌] WebUI 保存配置失败：\n{traceback.format_exc()}")
            return error_response(f"保存失败：{e}", status_code=500)
        return json_response({"saved": True, "applied": sorted(clean)})


def _schema_specs() -> dict:
    """读取 _conf_schema.json（配置键 -> 该配置项的声明）。"""
    try:
        path = Path(__file__).parent / "_conf_schema.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _schema_top_keys() -> set[str]:
    """配置 schema 的全部顶层键。"""
    return set(_schema_specs())


def _normalize_payload(payload: dict) -> dict:
    """按 schema 声明规范化待保存的值。

    名单类（``type == "list"`` 且无 options）一律归一为字符串列表：前端文本域、
    AstrBot 配置页或直接调用接口传字符串时，若不规范化就会把 ``"123456"`` 原样
    落库，读取端再按字符逐个拆开，名单等于失效。
    """
    specs = _schema_specs()
    normalized: dict = {}
    for key, value in payload.items():
        spec = specs.get(key) or {}
        if spec.get("type") == "list" and not spec.get("options"):
            normalized[key] = parse_str_list(value)
        else:
            normalized[key] = value
    return normalized


def warn_schema_keys_not_editable() -> list[str]:
    """schema 里存在、但未加入 _EDITABLE_KEYS 的键。

    这类键 WebUI 读不到（前端显示默认值）、改了也不会落库——v0.10.4 的
    recall_candidate 就因此表现为「开关保存后刷新又变回关闭」。返回缺失键列表。
    """
    missing = sorted(_schema_top_keys() - _EDITABLE_KEYS)
    if missing:
        logger.warning(f"[萌音点歌] 以下配置项未加入 WebUI 可编辑白名单，改后不生效：{missing}")
    return missing


def _q_str(name: str, default: str) -> str:
    try:
        v = request.query.get(name, default)
        return str(v) if v else default
    except Exception:
        return default


def _q_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = request.query.get(name, default, type=int)
    except Exception:
        return default
    return max(lo, min(hi, int(v)))


def _translate(rows: list[dict], names: dict[str, str]) -> list[dict]:
    """分布数据的枚举键转中文展示名。"""
    return [{**r, "k": names.get(r.get("k"), r.get("k"))} for r in rows]


def _hour_buckets(range_key: str, since_ts: float) -> tuple[list[str], list[str]]:
    """小时桶：today 为当天 0-23 点（标签 HH:00）；24h 为最近 24 个整点（标签 MM-DD HH:MM）。"""
    start = _dt.datetime.fromtimestamp(since_ts).replace(minute=0, second=0, microsecond=0)
    keys: list[str] = []
    labels: list[str] = []
    for i in range(24):
        t = start + _dt.timedelta(hours=i)
        if range_key == "today" and t.date() != _dt.date.today():
            continue
        keys.append(t.strftime("%Y-%m-%d %H:00"))
        labels.append(t.strftime("%H:00") if range_key == "today" else t.strftime("%m-%d %H:%M"))
    return keys, labels


def _day_buckets(range_key: str) -> tuple[list[str], list[str]]:
    units = _RANGE_DEFS[range_key][2]
    today = _dt.date.today()
    keys = [(today - _dt.timedelta(days=i)).isoformat() for i in range(units - 1, -1, -1)]
    labels = [k[5:] for k in keys]  # MM-DD
    return keys, labels


def _month_buckets() -> tuple[list[str], list[str]]:
    d = _dt.date.today().replace(day=1)
    months: list[str] = []
    for _ in range(12):
        months.append(d.strftime("%Y-%m"))
        d = (d - _dt.timedelta(days=1)).replace(day=1)
    return months[::-1], months[::-1]


def register_web_api(plugin) -> None:
    """在插件 initialize 时调用：注册全部控制台路由。

    注册的是模块级稳定包装 handler，内部经 ``_active_plugin`` 转发到**当前活跃**
    的插件实例——插件重载后即使旧路由短暂残留，请求也会落到新实例的记录库，
    不会打到已被 terminate 关闭的旧连接上。
    """
    global _active_plugin
    _active_plugin = plugin
    warn_schema_keys_not_editable()
    ctx = plugin.context
    prefix = f"/{PLUGIN_NAME}"
    routes = [
        (f"{prefix}/stats/overview", "stats_overview", ["GET"], "点歌统计总览"),
        (f"{prefix}/stats/trend", "stats_trend", ["GET"], "点歌趋势"),
        (f"{prefix}/stats/top", "stats_top", ["GET"], "点歌排行"),
        (f"{prefix}/stats/dist", "stats_dist", ["GET"], "点歌分布"),
        (f"{prefix}/tasks/queue", "tasks_queue", ["GET"], "队列快照"),
        (f"{prefix}/tasks/recent", "tasks_recent", ["GET"], "最近记录"),
        (f"{prefix}/tasks/stream", "tasks_stream", ["GET"], "实时任务流（SSE）"),
        (f"{prefix}/schema", "get_schema", ["GET"], "读取配置 schema"),
        (f"{prefix}/config", "get_config", ["GET"], "读取插件配置"),
        (f"{prefix}/config", "save_config", ["POST"], "保存插件配置"),
        (f"{prefix}/changelog", "get_changelog", ["GET"], "更新日志"),
    ]

    def _handler(name: str):
        async def view(*args, **kwargs):
            api = MoeWebUIApi(_active_plugin if _active_plugin is not None else plugin)
            return await getattr(api, name)(*args, **kwargs)

        return view

    registered = 0
    for path, name, methods, desc in routes:
        try:
            ctx.register_web_api(path, _handler(name), methods, desc)
            registered += 1
        except Exception as e:
            logger.warning(f"[萌音点歌] WebUI 路由注册失败 {path}: {e}")
    logger.info(f"[萌音点歌] WebUI 路由已注册 {registered}/{len(routes)} 个")
