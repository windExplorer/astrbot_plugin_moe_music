"""白名单 / 黑名单访问控制测试。"""

from astrbot_plugin_moe_music.core.access import AccessController


class FakeEvent:
    def __init__(self, user_id="10001", group_id="20001", private=False, admin=False):
        self._user_id = user_id
        self._group_id = group_id
        self._private = private
        self._admin = admin

    def get_sender_id(self):
        return self._user_id

    def get_group_id(self):
        return self._group_id

    def is_private_chat(self):
        return self._private

    def is_admin(self):
        return self._admin


def make_controller(cfg: dict) -> AccessController:
    return AccessController(cfg)


class TestModeSelection:
    def test_both_empty_is_off(self):
        assert make_controller({}).mode == "off"
        assert not make_controller({}).enabled

    def test_whitelist_wins_over_blacklist(self):
        ctl = make_controller({"whitelist_groups": ["20001"], "blacklist_groups": ["20002"]})
        assert ctl.mode == "whitelist"

    def test_blacklist_when_no_whitelist(self):
        ctl = make_controller({"blacklist_users": ["10001"]})
        assert ctl.mode == "blacklist"


class TestWhitelist:
    def test_group_in_whitelist_allows_everyone(self):
        ctl = make_controller({"whitelist_groups": ["20001"]})
        allowed, _ = ctl.check(FakeEvent(user_id="99999", group_id="20001"))
        assert allowed

    def test_group_not_in_whitelist_denies_even_whitelisted_user(self):
        ctl = make_controller({"whitelist_groups": ["20001"], "whitelist_users": ["10001"]})
        allowed, kind = ctl.check(FakeEvent(user_id="10001", group_id="20002"))
        assert not allowed
        assert kind == "group_not_whitelisted"

    def test_private_chat_checks_user_whitelist(self):
        ctl = make_controller({"whitelist_users": ["10001"]})
        allowed, kind = ctl.check(FakeEvent(user_id="10001", group_id="", private=True))
        assert allowed
        allowed, kind = ctl.check(FakeEvent(user_id="10002", group_id="", private=True))
        assert not allowed
        assert kind == "user_not_whitelisted"

    def test_admin_bypasses_whitelist(self):
        ctl = make_controller({"whitelist_groups": ["20001"]})
        allowed, _ = ctl.check(FakeEvent(user_id="10001", group_id="99999", admin=True))
        assert allowed

    def test_wildcard_pattern(self):
        ctl = make_controller({"whitelist_groups": ["200*"]})
        allowed, _ = ctl.check(FakeEvent(group_id="20099"))
        assert allowed
        allowed, _ = ctl.check(FakeEvent(group_id="30001"))
        assert not allowed


class TestBlacklist:
    def test_blacklisted_group_denies_everyone(self):
        ctl = make_controller({"blacklist_groups": ["20001"]})
        allowed, kind = ctl.check(FakeEvent(user_id="anyone", group_id="20001"))
        assert not allowed
        assert kind == "group_blacklisted"

    def test_blacklisted_user_denied_when_group_ok(self):
        ctl = make_controller({"blacklist_users": ["10001"]})
        allowed, kind = ctl.check(FakeEvent(user_id="10001", group_id="20001"))
        assert not allowed
        assert kind == "user_blacklisted"

    def test_other_users_unaffected(self):
        ctl = make_controller({"blacklist_users": ["10001"], "blacklist_groups": ["20001"]})
        allowed, _ = ctl.check(FakeEvent(user_id="10002", group_id="20002"))
        assert allowed

    def test_admin_bypasses_blacklist(self):
        ctl = make_controller({"blacklist_users": ["10001"]})
        allowed, _ = ctl.check(FakeEvent(user_id="10001", group_id="20001", admin=True))
        assert allowed

    def test_private_chat_blacklist(self):
        ctl = make_controller({"blacklist_users": ["10001"]})
        allowed, _ = ctl.check(FakeEvent(user_id="10001", group_id="", private=True))
        assert not allowed

    def test_deny_hints(self):
        assert "本群" in AccessController.deny_hint("group_blacklisted")
        assert "您" in AccessController.deny_hint("user_not_whitelisted")
