"""给 SubSync 插件打上 MoviePilot 宿主的桩，使其可脱离宿主离线导入。

插件在 import 期依赖 `apscheduler` 与 `app.*`（MoviePilot 本体），
没有宿主时直接 ModuleNotFoundError，测试连第一条断言都跑不到。
这里只桩掉 import 期真正用到的东西，不模拟任何业务逻辑。

导入本文件后调用 install()：
    import stub_host; stub_host.install()
"""
import sys
import types
from enum import Enum

_STUBBED = []


def _mod(name):
    m = types.ModuleType(name)
    m.__path__ = []
    sys.modules[name] = m
    _STUBBED.append(name)
    return m


def install():
    """安装桩。已安装则直接返回，可重复调用。"""
    if "app.log" in sys.modules and getattr(sys.modules["app.log"], "_subsync_stub", False):
        return

    # ---------- apscheduler ----------
    ap = _mod("apscheduler")
    trig = _mod("apscheduler.triggers")
    cron = _mod("apscheduler.triggers.cron")

    class CronTrigger:
        """只校验 crontab 是否为 5 段，具体调度交给真 apscheduler。"""

        @staticmethod
        def from_crontab(expr):
            if len(str(expr).split()) != 5:
                raise ValueError(f"invalid crontab expression: {expr!r}")
            return CronTrigger(expr)

        def __init__(self, expr):
            self.expr = expr

    cron.CronTrigger = CronTrigger
    trig.cron = cron
    ap.triggers = trig

    # ---------- app.core.event ----------
    app = _mod("app")
    core = _mod("app.core")
    event_mod = _mod("app.core.event")

    class Event:
        def __init__(self, event_data=None, **_kw):
            self.event_data = event_data or {}

    class _EventManager:
        """record 装饰器直接放行，把注册信息挂在函数上供断言使用。"""

        def register(self, *_a, **_kw):
            def deco(fn):
                fn.__event_registered__ = True
                return fn
            return deco

        def asyncmode(self, *_a, **_kw):
            def deco(fn):
                return fn
            return deco

    event_mod.Event = Event
    event_mod.eventmanager = _EventManager()

    # ---------- app.log ----------
    log_mod = _mod("app.log")
    log_mod._subsync_stub = True

    class _Logger:
        def __init__(self):
            self.records = []

        def _add(self, level, msg):
            self.records.append((level, str(msg)))

        def debug(self, m):
            self._add("DEBUG", m)

        def info(self, m):
            self._add("INFO", m)

        def warning(self, m):
            self._add("WARNING", m)

        def error(self, m):
            self._add("ERROR", m)

    log_mod.logger = _Logger()

    # ---------- app.plugins ----------
    plugins_mod = _mod("app.plugins")

    class _PluginBase:
        """最小可用宿主基类：数据存取 + 消息推送。"""

        def __init__(self, *_a, **_kw):
            self._store = {}

        def get_data(self, key, default=None):
            return self._store.get(key, default)

        def save_data(self, key, value):
            self._store[key] = value

        def post_message(self, **kwargs):
            self._store.setdefault("_posted", []).append(kwargs)

    plugins_mod._PluginBase = _PluginBase

    # ---------- app.schemas.types ----------
    schemas = _mod("app.schemas")
    types_mod = _mod("app.schemas.types")

    class EventType(Enum):
        TransferComplete = "transfer_complete"
        PluginAction = "plugin_action"

    types_mod.EventType = EventType


def cleanup():
    """移除所有桩模块，仅供同一进程内反复加载插件时使用。"""
    for name in list(_STUBBED):
        sys.modules.pop(name, None)
    _STUBBED.clear()
