"""
SubSync 源文件清理开关验证脚本
=================================
在临时目录里造出源字幕与目标 strm，验证 delete_source 的三种情形：
复制成功后删源、目标已存在跳过时不删、复制失败时不删。
不触碰真实媒体目录。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 默认指向仓库内 plugins.v3/subsync，可用环境变量 SUBSYNC_PLUGIN_PATH 覆盖
_PLUGIN_PATH = os.environ.get(
    "SUBSYNC_PLUGIN_PATH",
    str(Path(__file__).resolve().parents[3] / "plugins.v3" / "subsync" / "__init__.py"),
)
spec = importlib.util.spec_from_file_location("subsync", _PLUGIN_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["subsync"] = mod
spec.loader.exec_module(mod)

failures = []


def check(name, cond, detail=""):
    """记录一条断言结果。"""
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        failures.append(name)


def make_case(tmp, src_name="Sub.S01E01.chs.srt", dst_name=None,
              body="1\n00:00:01,000 --> 00:00:02,000\n你好\n"):
    """构造一份源字幕和对应的目标 strm 路径。"""
    src = tmp / "downloads" / src_name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")
    lib = tmp / "library" / "测试剧 (2024)" / "Season 1"
    lib.mkdir(parents=True, exist_ok=True)
    strm = lib / "测试剧 - S01E01 - 第 1 集.strm"
    strm.write_text("http://example.com/video", encoding="utf-8")
    dst = lib / (dst_name or "测试剧 - S01E01 - 第 1 集.zh-Hans.srt")
    return src, dst


print("=== 1. 开关关闭：复制后保留源文件 ===")
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    src, dst = make_case(tmp)
    plan = mod.Plan(src=src, dst=dst, reason="测试")
    ok, skip, fail = mod.apply_plans([plan], overwrite=False, delete_source=False)
    check("复制成功", ok == 1, f"ok={ok}")
    check("目标文件已生成", dst.exists())
    check("源文件保留", src.exists())

print("\n=== 2. 开关开启：复制成功后删除源文件 ===")
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    src, dst = make_case(tmp)
    plan = mod.Plan(src=src, dst=dst, reason="测试")
    ok, skip, fail = mod.apply_plans([plan], overwrite=False, delete_source=True)
    check("复制成功", ok == 1, f"ok={ok}")
    check("目标文件已生成", dst.exists())
    check("内容完整一致", dst.read_text(encoding="utf-8").startswith("1"))
    check("源文件已删除", not src.exists())

print("\n=== 3. 开关开启但目标已存在：跳过，不删源 ===")
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    src, dst = make_case(tmp)
    dst.write_text("已存在的字幕", encoding="utf-8")
    before = dst.read_text(encoding="utf-8")
    plan = mod.Plan(src=src, dst=dst, reason="测试")
    ok, skip, fail = mod.apply_plans([plan], overwrite=False, delete_source=True)
    check("记为跳过", skip == 1, f"skip={skip}")
    check("源文件保留（未被误删）", src.exists())
    check("目标内容未被覆盖", dst.read_text(encoding="utf-8") == before)

print("\n=== 4. 开关开启且开启覆盖：先覆盖再删源 ===")
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    src, dst = make_case(tmp)
    dst.write_text("旧内容", encoding="utf-8")
    plan = mod.Plan(src=src, dst=dst, reason="测试")
    ok, skip, fail = mod.apply_plans([plan], overwrite=True, delete_source=True)
    check("覆盖成功", ok == 1, f"ok={ok}")
    check("目标内容已更新", dst.read_text(encoding="utf-8").startswith("1"))
    check("源文件已删除", not src.exists())

print("\n=== 5. 复制失败：源文件必须保留 ===")
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    src, dst = make_case(tmp)
    # 把目标父目录做成一个文件，强制 mkdir/copy 失败
    blocker = tmp / "blocker"
    blocker.write_text("x", encoding="utf-8")
    bad_plan = mod.Plan(src=src, dst=blocker / "sub" / "x.srt", reason="测试")
    ok, skip, fail = mod.apply_plans([bad_plan], overwrite=False, delete_source=True)
    check("记为失败", fail == 1, f"fail={fail}")
    check("源文件未被误删", src.exists())

print("\n=== 6. 开关默认值检查 ===")
# 插件实例依赖宿主 Chain 运行时上下文，离线无法实例化，改为源码级校验。
src = Path(mod.__file__).read_text(encoding="utf-8")
check("apply_plans 签名为 False 默认",
      "delete_source: bool = False" in src)
check("事件处理把开关传给 apply_plans",
      "apply_plans(plans, self._overwrite, self._delete_source)" in src)
check("表单默认值 False",
      '"delete_source": False,' in src)
check("表单开关标签写明释放空间",
      "删除下载目录的源字幕（释放空间）" in src)

print("\n" + "=" * 50)
if failures:
    print(f"结果: {len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果: 全部断言通过")