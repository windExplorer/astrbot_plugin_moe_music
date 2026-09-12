"""测试环境：注入 astrbot 框架 stub，使插件模块可在无 AstrBot 运行时的环境下导入与测试。"""

import asyncio
import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _install_astrbot_stubs():
    """构造最小化的 astrbot 包结构 stub。

    覆盖插件用到的符号：
    - astrbot.api.logger
    - astrbot.api.event（filter / AstrMessageEvent）
    - astrbot.api.star（Context / Star）
    - astrbot.api.message_components（Plain / Record / File / Image）
    - astrbot.core.config.astrbot_config.AstrBotConfig
    - astrbot.core.utils.session_waiter（session_waiter 等真实实现不可用时用简单 stub）
    - astrbot.core.platform...AiocqhttpMessageEvent
    - astrbot.core.utils.astrbot_path
    """
    if "astrbot" in sys.modules and getattr(sys.modules["astrbot"], "__is_test_stub__", False):
        return

    logger_stub = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )

    astrbot = types.ModuleType("astrbot")
    astrbot.__is_test_stub__ = True
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.logger = logger_stub

    # ---- message_components ----
    class _Comp:
        def __init__(self, *args, **kwargs):
            self.args = args
            for key, value in kwargs.items():
                setattr(self, key, value)

    class Plain(_Comp):
        pass

    class Record(_Comp):
        @staticmethod
        def fromURL(url):
            return Record(file=url)

        @staticmethod
        def fromFileSystem(path):
            return Record(file=str(path))

    class File(_Comp):
        pass

    class Image(_Comp):
        @staticmethod
        def fromBytes(data):
            return Image(file=data)

    comp_mod = types.ModuleType("astrbot.api.message_components")
    comp_mod.Plain = Plain
    comp_mod.Record = Record
    comp_mod.File = File
    comp_mod.Image = Image

    # ---- event / filter ----
    class AstrMessageEvent:
        pass

    class _FilterNS:
        EventMessageType = types.SimpleNamespace(ALL="all", GROUP_MESSAGE="group", PRIVATE_MESSAGE="private")

        @staticmethod
        def command(*args, **kwargs):
            def deco(fn):
                return fn

            return deco

        @staticmethod
        def event_message_type(*args, **kwargs):
            def deco(fn):
                return fn

            return deco

        @staticmethod
        def llm_tool(*args, **kwargs):
            def deco(fn):
                return fn

            return deco

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.filter = _FilterNS
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageEventResult = type("MessageEventResult", (), {})

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Context:
        pass

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Star = Star
    star_mod.Context = Context

    # ---- AstrBotConfig ----
    class AstrBotConfig(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

    cfg_mod = types.ModuleType("astrbot.core.config.astrbot_config")
    cfg_mod.AstrBotConfig = AstrBotConfig

    # ---- session_waiter：轻量 stub（真实实现依赖完整框架运行时） ----
    try:
        raise ImportError
    except ImportError:

        class SessionController:
            def __init__(self):
                self.stopped = False

            def stop(self, error=None):
                self.stopped = True

        class SessionFilter:
            def filter(self, event):
                return event.unified_msg_origin

        def session_waiter(timeout=30, record_history_chains=False):
            def decorator(func):
                async def wrapper(event, session_filter=None, *args, **kwargs):
                    controller = SessionController()
                    await func(controller, event)
                    return controller

                return wrapper

            return decorator

    sw_mod = types.ModuleType("astrbot.core.utils.session_waiter")
    sw_mod.SessionController = SessionController
    sw_mod.SessionFilter = SessionFilter
    sw_mod.session_waiter = session_waiter

    # ---- aiocqhttp event ----
    class AiocqhttpMessageEvent(AstrMessageEvent):
        bot = None

    aiocq_mod = types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event")
    aiocq_mod.AiocqhttpMessageEvent = AiocqhttpMessageEvent

    # ---- astrbot_path ----
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_temp_path = lambda: str(Path(__file__).parent / "_tmp")

    # ---- astrbot.api.web（插件 Pages 后端 API） ----
    class _Query:
        def get(self, name, default=None, type=None):
            return default

        def getlist(self, name):
            return []

    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.request = types.SimpleNamespace(
        query=_Query(), username="tester", method="GET", path="/test", plugin_name="astrbot_plugin_moe_music"
    )
    web_mod.json_response = lambda value, status_code=200: ("json", value, status_code)
    web_mod.error_response = lambda msg, status_code=400: ("error", msg, status_code)
    web_mod.stream_response = lambda gen: ("stream", gen)
    web_mod.file_response = lambda *a, **kw: ("file",)
    web_mod.PluginUploadFile = type("PluginUploadFile", (), {})

    # ---- 组装包结构 ----
    for name, mod in {
        "astrbot": astrbot,
        "astrbot.api": astrbot_api,
        "astrbot.api.event": event_mod,
        "astrbot.api.star": star_mod,
        "astrbot.api.message_components": comp_mod,
        "astrbot.api.web": web_mod,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.config": types.ModuleType("astrbot.core.config"),
        "astrbot.core.config.astrbot_config": cfg_mod,
        "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
        "astrbot.core.utils.session_waiter": sw_mod,
        "astrbot.core.utils.astrbot_path": path_mod,
        "astrbot.core.platform": types.ModuleType("astrbot.core.platform"),
        "astrbot.core.platform.sources": types.ModuleType("astrbot.core.platform.sources"),
        "astrbot.core.platform.sources.aiocqhttp": types.ModuleType(
            "astrbot.core.platform.sources.aiocqhttp"
        ),
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": aiocq_mod,
    }.items():
        sys.modules.setdefault(name, mod)

    astrbot_api.logger = logger_stub


_install_astrbot_stubs()

# 让插件以其正式包名（astrbot_plugin_moe_music）导入，路径与其在 AstrBot 中
# 作为 data.plugins.<name> 包加载时一致
sys.path.insert(0, str(PLUGIN_ROOT.parent))


@pytest.fixture()
def event_loop_policy():
    return asyncio.get_event_loop_policy()
