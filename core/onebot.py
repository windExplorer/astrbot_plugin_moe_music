"""OneBot(aiocqhttp) 协议端直发与撤回的共用工具。

背景（AstrBot 实测）：平台基类没有撤回 API，``event.send()`` 也不返回
message_id——撤回机器人自己发出的消息只能走协议端 ``call_action``，且 message_id
必须在**发送时**由协议端返回值捕获。本模块把「取 call_action」「群号/QQ 号归一」
「从响应里取 message_id」「拿不到 id 时反查最近消息」集中一处，供候选列表撤回
（core/commands.py）与音乐卡片发送（core/sender.py）共用，避免两边各自写死一种
调用方式而静默失效。
"""

import time
from collections.abc import Callable
from typing import Any

from astrbot.api import logger

# 协议端返回 message_id 的常见键名（不同 OneBot 实现用词不一）
_MESSAGE_ID_KEYS = ("message_id", "msg_id", "messageId")


def call_action_of(event) -> Callable | None:
    """取出协议端 call_action（仅 aiocqhttp 提供）；不支持时返回 None。

    优先 ``bot.call_action``（AstrBot 官方 aiocqhttp 适配器用的就是它），再回退
    ``bot.api.call_action``（aiocqhttp 的 ``CQHttp.api`` 属性）——不同版本的 bot
    对象暴露方式不同，写死其中一种会静默失效：消息照常发出，但永远拿不到
    message_id，也就永远撤回不了。
    """
    bot = getattr(event, "bot", None)
    ca = getattr(bot, "call_action", None)
    if callable(ca):
        return ca
    api = getattr(bot, "api", None)
    ca = getattr(api, "call_action", None)
    return ca if callable(ca) else None


def to_onebot_id(value: Any) -> int | str:
    """群号 / QQ 号归一：纯数字转 int，否则原样返回（部分协议端只认数字类型）。"""
    if value is None or value == "":
        return ""
    text = str(value).strip()
    return int(text) if text.lstrip("-").isdigit() else text


def extract_message_id(result: Any) -> int | str | None:
    """从协议端响应里取 message_id。

    兼容三种形态：扁平的 ``{"message_id": x}``、``data`` 嵌套的
    ``{"status": "ok", "data": {"message_id": x}}``，以及 ``msg_id`` / ``messageId``
    这类不同用词的键名。
    """
    if not isinstance(result, dict):
        return None
    for scope in (result, result.get("data")):
        if not isinstance(scope, dict):
            continue
        for key in _MESSAGE_ID_KEYS:
            mid = scope.get(key)
            if mid not in (None, ""):
                return to_onebot_id(mid)
    return None


def message_id_payload(message_id: Any) -> int | str:
    """delete_msg 的 message_id 参数（与 extract_message_id 同一套归一规则）。"""
    return to_onebot_id(message_id)


def _latest_self_message_id(result: Any, self_id: str, max_age_sec: float = 60) -> int | str | None:
    """从 get_*_msg_history 的响应里取「自己发出的、且刚发出的」最新一条 message_id。

    双重过滤：只认发送者是机器人自己的，且（协议端给了时间戳时）只认 60 秒内的——
    宁可放弃撤回，也不冒险撤回一条旧消息。
    """
    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list):
        return None
    now = time.time()
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
        sender_id = str(msg.get("user_id") or sender.get("user_id") or "")
        if sender_id != self_id:  # 只认机器人自己发的，避免误撤回他人消息
            continue
        ts = msg.get("time")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and now - float(ts) > max_age_sec:
            continue  # 太旧，不是本次刚发的那条
        mid = extract_message_id(msg)
        if mid is not None:
            return mid
    return None


async def fetch_recent_self_message_id(event) -> int | str | None:
    """兜底：协议端没回 message_id 时，从最近消息历史里反查机器人刚发的那条。

    少数协议端 / OneBot 实现调用 ``send_group_msg`` 时响应里不含 message_id，撤回便
    无从谈起。此时紧接着发送动作查一次消息历史，取「发送者是自己」的最新一条。

    安全约束：拿不到 ``self_id`` 时直接放弃（宁可不撤回，也绝不误撤回他人消息）；
    协议端不支持 history API 会抛异常，同样安全返回 None（退回原行为）。
    """
    ca = call_action_of(event)
    if ca is None:
        return None
    self_id = str(getattr(getattr(event, "message_obj", None), "self_id", "") or "").strip()
    if not self_id:
        logger.warning("[萌音点歌] 无法确定机器人自身 QQ 号，放弃反查消息历史（避免误撤回）")
        return None
    try:
        if event.is_private_chat():
            result = await ca(
                "get_friend_msg_history",
                user_id=to_onebot_id(event.get_sender_id()),
                count=5,
            )
        else:
            result = await ca(
                "get_group_msg_history",
                group_id=to_onebot_id(event.get_group_id()),
                count=5,
            )
    except Exception as e:
        logger.warning(f"[萌音点歌] 反查消息历史失败（协议端可能不支持）：{type(e).__name__}: {e}")
        return None
    return _latest_self_message_id(result, self_id)


async def send_message_via_onebot(
    event, message: list[dict], diag: dict | None = None
) -> tuple[bool, int | str | None]:
    """经协议端直发消息段，返回 ``(是否已发出, message_id)``。

    撤回依赖发送时拿到的 message_id，而 ``event.send()`` 返回 None，所以需要撤回的
    场景必须走这里。协议端没回 id 时会反查一次消息历史；仍拿不到则返回
    ``(True, None)``——调用方不应再降级通用发送，否则同一份内容会重复发一条。

    ``diag``：可选字典，回传「为何没拿到 message_id」的具体原因，供撤回失败时
    一条日志说清（避免再去翻发送那一刻的旧日志）。
    """

    def _note(reason: str) -> None:
        if diag is not None:
            diag["reason"] = reason

    ca = call_action_of(event)
    if ca is None:
        _note("当前平台无 call_action（非 OneBot）")
        return False, None
    if event.is_private_chat():
        action, target_key, target_value = "send_private_msg", "user_id", event.get_sender_id()
    else:
        action, target_key, target_value = "send_group_msg", "group_id", event.get_group_id()
    try:
        result = await ca(action, **{target_key: to_onebot_id(target_value), "message": message})
    except Exception as e:
        logger.warning(f"[萌音点歌] OneBot 直发失败，降级通用发送：{type(e).__name__}: {e}")
        _note(f"协议端直发失败（{type(e).__name__}: {e}），已降级通用发送")
        return False, None
    mid = extract_message_id(result)
    if mid is not None:
        logger.info(f"[萌音点歌] 消息已由协议端直发：message_id={mid}")
        return True, mid
    logger.warning(f"[萌音点歌] 协议端未返回 message_id（响应 {result!r}），尝试反查最近消息")
    mid = await fetch_recent_self_message_id(event)
    if mid is None:
        logger.warning("[萌音点歌] 反查也未拿到 message_id，本次消息无法自动撤回")
        _note(f"协议端响应无 message_id（{result!r}），反查消息历史也未命中")
    else:
        logger.info(f"[萌音点歌] 已反查到 message_id={mid}，仍可自动撤回")
    return True, mid
