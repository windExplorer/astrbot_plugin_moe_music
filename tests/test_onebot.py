"""OneBot 协议端直发 / 撤回工具测试（core/onebot.py）。"""

import time
import types

from astrbot_plugin_moe_music.core.onebot import (
    call_action_of,
    extract_message_id,
    fetch_recent_self_message_id,
    message_id_payload,
    send_message_via_onebot,
    to_onebot_id,
)

SELF_ID = "9999"


class FakeEvent:
    def __init__(self, *, private=False, bot=None, self_id=SELF_ID):
        self.bot = bot
        self._private = private
        self.message_obj = types.SimpleNamespace(self_id=self_id)

    def is_private_chat(self):
        return self._private

    def get_sender_id(self):
        return "10001"

    def get_group_id(self):
        return "20001"


class FakeBot:
    """可编程 call_action：发送返回 message_id，history 由 history 参数提供。"""

    def __init__(self, result=None, error=None, history=None):
        self.result = {"message_id": 4321} if result is None else result
        self.error = error
        self.history = history
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action, **payload):
        self.calls.append((action, payload))
        if self.error:
            raise self.error
        if action in ("get_group_msg_history", "get_friend_msg_history"):
            if self.history is None:
                raise RuntimeError("history API not supported")
            return self.history
        return self.result


class TestCallActionOf:
    def test_prefers_bot_call_action(self):
        """bot 自身有 call_action 时优先用它（AstrBot 官方适配器的结构）。"""
        bot = FakeBot()
        bot.api = FakeBot()
        assert call_action_of(FakeEvent(bot=bot)) == bot.call_action

    def test_falls_back_to_bot_api(self):
        """旧版本只在 bot.api 上暴露 call_action：回退路径必须可用。"""
        bot = type("Bot", (), {})()
        bot.api = FakeBot()
        assert call_action_of(FakeEvent(bot=bot)) == bot.api.call_action

    def test_none_when_unsupported(self):
        assert call_action_of(FakeEvent(bot=object())) is None
        assert call_action_of(FakeEvent(bot=None)) is None


class TestIdHelpers:
    def test_to_onebot_id(self):
        assert to_onebot_id("20001") == 20001
        assert to_onebot_id(20001) == 20001
        assert to_onebot_id("abc") == "abc"
        assert to_onebot_id(None) == ""
        assert message_id_payload("4321") == 4321  # delete_msg 的 message_id 参数

    def test_extract_message_id(self):
        assert extract_message_id({"message_id": 4321}) == 4321
        assert extract_message_id({"message_id": "4321"}) == 4321
        assert extract_message_id({"status": "ok", "data": {"message_id": "99"}}) == 99
        # 不同 OneBot 实现的键名用词
        assert extract_message_id({"msg_id": 77}) == 77
        assert extract_message_id({"messageId": "88"}) == 88
        assert extract_message_id({"message_id": 0}) == 0
        assert extract_message_id({}) is None
        assert extract_message_id(None) is None
        assert extract_message_id("not-a-dict") is None
        assert extract_message_id({"message_id": ""}) is None


class TestSendMessageViaOnebot:
    async def test_group_send_returns_id(self):
        bot = FakeBot()
        sent, mid = await send_message_via_onebot(
            FakeEvent(bot=bot), [{"type": "text", "data": {"text": "hi"}}]
        )
        assert sent is True and mid == 4321
        action, payload = bot.calls[0]
        assert action == "send_group_msg"
        assert payload["group_id"] == 20001  # 群号归一为 int（部分协议端只认数字）
        assert payload["message"] == [{"type": "text", "data": {"text": "hi"}}]

    async def test_private_send(self):
        bot = FakeBot()
        sent, mid = await send_message_via_onebot(FakeEvent(bot=bot, private=True), [])
        assert sent is True and mid == 4321
        assert bot.calls[0][0] == "send_private_msg"
        assert bot.calls[0][1]["user_id"] == 10001

    async def test_not_sent_on_error(self):
        """发送抛异常 → (False, None)：调用方可以安全降级通用发送。"""
        bot = FakeBot(error=RuntimeError("boom"))
        sent, mid = await send_message_via_onebot(FakeEvent(bot=bot), [])
        assert sent is False and mid is None

    async def test_sent_without_id(self):
        """已发出但协议端没回 id、反查也不可用 → (True, None)：不能重发。"""
        bot = FakeBot(result={})
        sent, mid = await send_message_via_onebot(FakeEvent(bot=bot), [])
        assert sent is True and mid is None

    async def test_falls_back_to_message_history(self):
        """协议端不回 message_id 时，从最近消息历史反查机器人自己发的那条。"""
        history = {
            "messages": [
                {"message_id": 111, "user_id": "10001", "sender": {"user_id": "10001"}},
                {"message_id": 222, "user_id": SELF_ID, "sender": {"user_id": SELF_ID}},
                {"message_id": 333, "user_id": SELF_ID, "sender": {"user_id": SELF_ID}},
            ]
        }
        bot = FakeBot(result={}, history=history)
        sent, mid = await send_message_via_onebot(FakeEvent(bot=bot), [])
        assert sent is True and mid == 333  # 取最新的「自己发的」
        assert bot.calls[-1][0] == "get_group_msg_history"

    async def test_history_skips_others_messages(self):
        """历史最新一条是群友发的时，必须继续往前找自己的消息。"""
        history = {
            "messages": [
                {"message_id": 222, "user_id": SELF_ID},
                {"message_id": 444, "user_id": "10001"},
            ]
        }
        bot = FakeBot(result={}, history=history)
        _, mid = await send_message_via_onebot(FakeEvent(bot=bot), [])
        assert mid == 222

    async def test_history_ignores_stale_messages(self):
        """历史里 60 秒前的旧消息不算「本次刚发的」，宁可放弃也不误撤回。"""
        history = {
            "messages": [
                {"message_id": 666, "user_id": SELF_ID, "time": time.time() - 600},
            ]
        }
        bot = FakeBot(result={}, history=history)
        _, mid = await send_message_via_onebot(FakeEvent(bot=bot), [])
        assert mid is None

    async def test_history_accepts_fresh_messages(self):
        history = {
            "messages": [
                {"message_id": 777, "user_id": SELF_ID, "time": time.time() - 3},
            ]
        }
        bot = FakeBot(result={}, history=history)
        _, mid = await send_message_via_onebot(FakeEvent(bot=bot), [])
        assert mid == 777

    async def test_unsupported_platform(self):
        sent, mid = await send_message_via_onebot(FakeEvent(bot=None), [])
        assert sent is False and mid is None


class TestFetchRecentSelfMessageId:
    async def test_skipped_without_self_id(self):
        """拿不到机器人自身 QQ 号时放弃反查——绝不冒误撤回他人消息的风险。"""
        bot = FakeBot(result={}, history={"messages": [{"message_id": 333, "user_id": "10001"}]})
        assert await fetch_recent_self_message_id(FakeEvent(bot=bot, self_id="")) is None
        assert all(a != "get_group_msg_history" for a, _ in bot.calls)

    async def test_none_when_history_unsupported(self):
        bot = FakeBot(result={})
        assert await fetch_recent_self_message_id(FakeEvent(bot=bot)) is None

    async def test_none_when_malformed_history(self):
        bot = FakeBot(result={}, history={"messages": "oops"})
        assert await fetch_recent_self_message_id(FakeEvent(bot=bot)) is None

    async def test_private_chat_uses_friend_history(self):
        bot = FakeBot(result={}, history={"messages": [{"message_id": 555, "user_id": SELF_ID}]})
        mid = await fetch_recent_self_message_id(FakeEvent(bot=bot, private=True))
        assert mid == 555
        assert bot.calls[-1][0] == "get_friend_msg_history"
