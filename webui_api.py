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
import json
import traceback
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request, stream_response

PLUGIN_NAME = "astrbot_plugin_moe_music"

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
    "timeout",
    "request_timeout",
    "queue_concurrency",
    "queue_max_pending",
    "enable_lyrics",
    "embed_metadata",
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
        days = _q_int("days", 7, 1, 365)
        since = _since_ts(days)
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
        today_start = _since_ts(1)
        today = {
            "search": await self.store.query(
                "SELECT COUNT(*) AS n FROM search_records WHERE ts >= ?", (today_start,)
            ),
            "play": await self.store.query(
                "SELECT COUNT(*) AS n FROM play_records WHERE ts >= ?", (today_start,)
            ),
        }
        overall = await self.store.query(
            "SELECT (SELECT COUNT(*) FROM search_records) AS search, "
            "(SELECT COUNT(*) FROM play_records) AS play"
        )
        s = total_search[0] if total_search else {}
        p = total_play[0] if total_play else {}
        embed_applicable = int(p.get("embed_applicable") or 0)
        return json_response(
            {
                "days": days,
                "search_total": int(s.get("n") or 0),
                "search_ok": int(s.get("ok") or 0),
                "search_avg_ms": round(float(s.get("avg_ms") or 0)),
                "play_total": int(p.get("n") or 0),
                "play_avg_total_ms": round(float(p.get("avg_total") or 0)),
                "play_avg_queue_ms": round(float(p.get("avg_queue") or 0)),
                "quality_fallback": int(p.get("fallback") or 0),
                "embed_rate": (int(p.get("embedded") or 0) / embed_applicable) if embed_applicable else None,
                "today_search": int(today["search"][0]["n"]) if today["search"] else 0,
                "today_play": int(today["play"][0]["n"]) if today["play"] else 0,
                "overall_search": int(overall[0]["search"]) if overall else 0,
                "overall_play": int(overall[0]["play"]) if overall else 0,
            }
        )

    async def stats_trend(self):
        days = _q_int("days", 14, 1, 90)
        since = _since_ts(days)
        search_rows = await self.store.query(
            "SELECT date(created_at) AS d, COUNT(*) AS n FROM search_records WHERE ts >= ? GROUP BY d",
            (since,),
        )
        play_rows = await self.store.query(
            "SELECT date(created_at) AS d, COUNT(*) AS n FROM play_records WHERE ts >= ? GROUP BY d",
            (since,),
        )
        search_map = {r["d"]: r["n"] for r in search_rows}
        play_map = {r["d"]: r["n"] for r in play_rows}
        dates = _date_range(days)
        return json_response(
            {
                "days": days,
                "dates": dates,
                "search": [int(search_map.get(d, 0)) for d in dates],
                "play": [int(play_map.get(d, 0)) for d in dates],
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
        days = _q_int("days", 30, 1, 365)
        since = _since_ts(days)
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
                "quality": quality,
                "send_mode": send_mode,
                "selection": selection,
                "trigger": trigger,
                "source": source,
                "queue_waits": [int(r["v"]) for r in cost],
            }
        )

    # ============ 实时任务 ============

    async def tasks_queue(self):
        return json_response({"queue": self.plugin.queue.snapshot()})

    async def tasks_recent(self):
        limit = _q_int("limit", 20, 1, 100)
        plays = await self.store.query(
            "SELECT id, created_at, user_name, user_id, group_name, group_id, "
            "track_name, singer, source, quality, send_mode, selection_type, "
            "trigger_type, queue_wait_ms, total_ms FROM play_records "
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
            "trigger_type, queue_wait_ms, total_ms FROM play_records "
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

    async def save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict) or not payload:
            return error_response("请求体为空")
        clean = {k: v for k, v in payload.items() if k in _EDITABLE_KEYS}
        if not clean:
            return error_response("没有可保存的配置项")
        try:
            await self.plugin.apply_webui_config(clean)
        except Exception as e:
            logger.error(f"[萌音点歌] WebUI 保存配置失败：\n{traceback.format_exc()}")
            return error_response(f"保存失败：{e}", status_code=500)
        return json_response({"saved": True, "applied": sorted(clean)})


def _q_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = request.query.get(name, default, type=int)
    except Exception:
        return default
    return max(lo, min(hi, int(v)))


def _since_ts(days: int) -> float:
    import time as _time

    return _time.time() - days * 86400


def _date_range(days: int) -> list[str]:
    """最近 N 天的日期列表（本地时区，含今天）。"""
    import datetime as _dt

    today = _dt.date.today()
    return [(today - _dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def register_web_api(plugin) -> None:
    """在插件 initialize 时调用：注册全部控制台路由（幂等安全）。"""
    ctx = plugin.context
    api = MoeWebUIApi(plugin)
    prefix = f"/{PLUGIN_NAME}"
    routes = [
        (f"{prefix}/stats/overview", api.stats_overview, ["GET"], "点歌统计总览"),
        (f"{prefix}/stats/trend", api.stats_trend, ["GET"], "点歌趋势"),
        (f"{prefix}/stats/top", api.stats_top, ["GET"], "点歌排行"),
        (f"{prefix}/stats/dist", api.stats_dist, ["GET"], "点歌分布"),
        (f"{prefix}/tasks/queue", api.tasks_queue, ["GET"], "队列快照"),
        (f"{prefix}/tasks/recent", api.tasks_recent, ["GET"], "最近记录"),
        (f"{prefix}/tasks/stream", api.tasks_stream, ["GET"], "实时任务流（SSE）"),
        (f"{prefix}/schema", api.get_schema, ["GET"], "读取配置 schema"),
        (f"{prefix}/config", api.get_config, ["GET"], "读取插件配置"),
        (f"{prefix}/config", api.save_config, ["POST"], "保存插件配置"),
    ]
    registered = 0
    for path, handler, methods, desc in routes:
        try:
            ctx.register_web_api(path, handler, methods, desc)
            registered += 1
        except Exception as e:
            logger.warning(f"[萌音点歌] WebUI 路由注册失败 {path}: {e}")
    logger.info(f"[萌音点歌] WebUI 路由已注册 {registered}/{len(routes)} 个")
