"""SubSync 离线测试总入口：一次跑完全部验证脚本。

    python3 tests/v3/subsync/run_all.py

之所以做成能离线跑：插件 import 期依赖 `apscheduler` 与 `app.*`，
没有宿主时直接 ModuleNotFoundError —— 旧测试脚本就是这样长期处于
「从没被执行过」的状态，表面存在实则零覆盖。
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

SUITES = [
    ("立即补扫按钮", "verify_instant_run.py"),
    ("插件核心功能（端到端）", "verify_end_to_end.py"),
    ("源文件清理开关", "verify_delete_source.py"),
    ("转移历史通道", "verify_transfer_history.py"),
]

failed = []
for title, script in SUITES:
    print("\n" + "#" * 70)
    print(f"# {title}  ({script})")
    print("#" * 70)
    proc = subprocess.run([sys.executable, str(HERE / script)],
                          capture_output=True, text=True)
    print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    if proc.returncode != 0:
        failed.append(script)

print("\n" + "=" * 70)
if failed:
    print(f"总计: {len(failed)} 个脚本失败 -> {failed}")
    sys.exit(1)
print(f"总计: {len(SUITES)} 个脚本全部通过 ✅")
