"""点歌功能访问控制：白名单 / 黑名单。

规则（与 PRD 约定一致）：
- 白名单与黑名单互斥，**白名单优先**：白名单（群号或个人）任一非空即进入白名单模式，黑名单整体失效；
- 群号判断优先于个人（群号 > QQ号）：白名单模式下群不在名单 → 群内所有人（管理员除外）都不可点歌，
  即使个人在白名单；黑名单模式下群在名单 → 整群禁用；
- 管理员（AstrBot 管理员）不受任何名单限制；
- 两个名单都为空 → 所有人所有群都可以点。
"""

import fnmatch

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

# 拒绝类型 -> 用户侧提示文案（不暴露命中了哪个名单等技术细节）
DENY_HINTS = {
    "group_not_whitelisted": "本群暂未开放点歌功能哦～",
    "user_not_whitelisted": "您暂时没有点歌权限哦～",
    "group_blacklisted": "本群暂未开放点歌功能哦～",
    "user_blacklisted": "您暂时没有点歌权限哦～",
}


def _norm_list(values) -> set[str]:
    """名单归一化：转字符串、去空白。"""
    return {str(v).strip() for v in (values or []) if str(v).strip()}


def _match_any(value: str, patterns: set[str]) -> bool:
    """名单匹配：支持 * 通配（如 12345* 匹配某前缀）。"""
    for p in patterns:
        if p == value:
            return True
        if ("*" in p or "?" in p) and fnmatch.fnmatch(value, p):
            return True
    return False


def _cfg_get(config, key: str):
    """兼容 dict（AstrBotConfig）与 dataclass（PluginConfig）两种配置载体。"""
    if hasattr(config, "get"):
        return config.get(key)
    return getattr(config, key, None)


class AccessController:
    """按事件判断是否允许使用点歌功能。"""

    def __init__(self, config):
        self.whitelist_groups = _norm_list(_cfg_get(config, "whitelist_groups"))
        self.whitelist_users = _norm_list(_cfg_get(config, "whitelist_users"))
        self.blacklist_groups = _norm_list(_cfg_get(config, "blacklist_groups"))
        self.blacklist_users = _norm_list(_cfg_get(config, "blacklist_users"))

        if self.whitelist_groups or self.whitelist_users:
            self.mode = "whitelist"
        elif self.blacklist_groups or self.blacklist_users:
            self.mode = "blacklist"
        else:
            self.mode = "off"
        logger.debug(f"[萌音点歌] 访问控制模式：{self.mode}")

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def check(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """检查事件是否允许点歌。

        Returns:
            (allowed, deny_kind)：deny_kind 仅为拒绝类别（记日志用），允许时为空。
        """
        if not self.enabled:
            return True, ""
        # 管理员不受限制
        try:
            if event.is_admin():
                return True, ""
        except Exception:
            pass

        user_id = str(event.get_sender_id() or "")
        group_id = "" if event.is_private_chat() else str(event.get_group_id() or "")

        if self.mode == "whitelist":
            if group_id:
                # 群号优先：群不在白名单，整群拒绝（含个人在白名单的情况）
                if _match_any(group_id, self.whitelist_groups):
                    return True, ""
                return False, "group_not_whitelisted"
            if _match_any(user_id, self.whitelist_users):
                return True, ""
            return False, "user_not_whitelisted"

        # 黑名单模式：群规则优先于个人规则
        if group_id and _match_any(group_id, self.blacklist_groups):
            return False, "group_blacklisted"
        if _match_any(user_id, self.blacklist_users):
            return False, "user_blacklisted"
        return True, ""

    @staticmethod
    def deny_hint(deny_kind: str) -> str:
        return DENY_HINTS.get(deny_kind, "您暂时没有点歌权限哦～")
