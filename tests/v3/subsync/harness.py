"""测试共用脚手架：装载插件 + 造目录树。

**两个必须知道的坑**（都真实踩过）：

1. 测试目录**不能建在 `/tmp` 下**。插件的 `STAGING_DIRS` 含 `'tmp'`，
   而系统临时目录本身就叫 `tmp`，`is_staging()` 会把挂在它下面的所有
   strm 当成 115 中转目录丢掉 —— 表现为「插件完全不工作」，极易误判成
   插件有 bug。所以这里在测试脚本同级建 `.subsync_test_tmp/`。

2. 测试脚本必须先 `import stub_host; stub_host.install()`，
   否则 import 插件会因缺 `apscheduler` / `app.*` 直接崩在第一行。
"""
import importlib.util
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUBSCRATCH = HERE / ".subsync_test_tmp"

_ZH_SRT = "1\n00:00:01,000 --> 00:00:02,000\n你好世界\n"
_EN_SRT = "1\n00:00:01,000 --> 00:00:02,000\nhello world\n"


def load_plugin():
    """打桩宿主并导入插件模块，返回模块对象。"""
    sys.path.insert(0, str(HERE))
    import stub_host
    stub_host.install()

    path = Path(__file__).resolve().parents[3] / "plugins.v3" / "subsync" / "__init__.py"
    spec = importlib.util.spec_from_file_location("subsync", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["subsync"] = mod
    spec.loader.exec_module(mod)
    return mod


class Tree:
    """临时目录树。刻意不用 tempfile，原因见模块 docstring。"""

    def __init__(self):
        SUBSCRATCH.mkdir(parents=True, exist_ok=True)
        import tempfile
        self._base = Path(tempfile.mkdtemp(prefix="case_", dir=str(SUBSCRATCH)))

    def __enter__(self):
        return self._base

    def __exit__(self, *exc):
        shutil.rmtree(self._base, ignore_errors=True)
        return False


def strm(path: Path, url: str = "http://example.com/video") -> Path:
    """在给定路径造一个 strm 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(url, encoding="utf-8")
    return path


def subtitle(path: Path, chinese: bool = True) -> Path:
    """造一个字幕文件。chinese=False 时内容为纯英文。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ZH_SRT if chinese else _EN_SRT, encoding="utf-8")
    return path


def make_plugin(mod, **overrides):
    """造一个已按配置初始化的插件实例。

    离线环境无法走宿主 Chain 上下文，所以绕过 __init__ 直接注入数据存取。
    """
    cfg = {
        "enabled": True, "chinese_only": True, "overwrite": False,
        "delete_source": False, "use_transfer_history": False,
        "multi_version": True,
        "download_movie": "", "download_tv": "", "library_movie": "",
        "library_tv": "", "backfill_cron": "30 4 * * *",
        "recent_window_days": 1, "alias": "",
    }
    cfg.update(overrides)
    inst = mod.SubSync.__new__(mod.SubSync)
    inst._store = {}
    inst.init_plugin(cfg)
    return inst
