"""SubSync 源文件清理（delete_source）验证。

在临时目录里造出源字幕与目标 strm，验证三种情形：
复制成功后删源、目标已存在跳过时不删、复制失败时不删。
不触碰真实媒体目录。

需要宿主桩才能导入插件，见 harness.load_plugin()。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Tree, load_plugin, make_plugin, strm, subtitle  # noqa: E402

mod = load_plugin()
failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        failures.append(name)


def make_case(tmp, src_name="Sub.S01E01.chs.srt", dst_name=None, body=None):
    """构造一份源字幕和对应的目标 strm 路径。"""
    src = subtitle(tmp / "dl" / src_name) if body is None else \
        subtitle(tmp / "dl" / src_name, chinese=False)
    lib = tmp / "library" / "测试剧 (2024)" / "Season 1"
    s = strm(lib / "测试剧 - S01E01 - 第 1 集.strm")
    dst = lib / (dst_name or "测试剧 - S01E01 - 第 1 集.zh-Hans.srt")
    return src, dst


print("=== 1. 开关关闭：复制后保留源文件 ===")
with Tree() as tmp:
    src, dst = make_case(tmp)
    plan = mod.Plan(src=src, dst=dst, reason="测试")
    ok, skip, fail = mod.apply_plans([plan], overwrite=False, delete_source=False)
    check("复制成功", ok == 1, f"ok={ok}")
    check("目标文件已生成", dst.exists())
    check("源文件保留", src.exists())

print("\n=== 2. 开关开启：复制成功后删除源文件 ===")
with Tree() as tmp:
    src, dst = make_case(tmp)
    plan = mod.Plan(src=src, dst=dst, reason="测试")
    ok, skip, fail = mod.apply_plans([plan], overwrite=False, delete_source=True)
    check("复制成功", ok == 1, f"ok={ok}")
    check("目标文件已生成", dst.exists())
    check("内容完整一致", dst.read_text(encoding="utf-8").startswith("1"))
    check("源文件已删除", not src.exists())

print("\n=== 3. 开关开启但目标已存在：跳过，不删源 ===")
with Tree() as tmp:
    src, dst = make_case(tmp)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("已存在的字幕", encoding="utf-8")
    before = dst.read_text(encoding="utf-8")
    plan = mod.Plan(src=src, dst=dst, reason="测试")
    ok, skip, fail = mod.apply_plans([plan], overwrite=False, delete_source=True)
    check("记为跳过", skip == 1, f"skip={skip}")
    check("源文件保留（未被误删）", src.exists())
    check("目标内容未被覆盖", dst.read_text(encoding="utf-8") == before)

print("\n=== 4. 开关开启且开启覆盖：先覆盖再删源 ===")
with Tree() as tmp:
    src, dst = make_case(tmp)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("旧内容", encoding="utf-8")
    plan = mod.Plan(src=src, dst=dst, reason="测试")
    ok, skip, fail = mod.apply_plans([plan], overwrite=True, delete_source=True)
    check("覆盖成功", ok == 1, f"ok={ok}")
    check("目标内容已更新", dst.read_text(encoding="utf-8").startswith("1"))
    check("源文件已删除", not src.exists())

print("\n=== 5. 复制失败：源文件必须保留 ===")
with Tree() as tmp:
    src, dst = make_case(tmp)
    # 把目标父目录做成一个文件，强制 mkdir/copy 失败
    blocker = tmp / "blocker"
    blocker.write_text("x", encoding="utf-8")
    bad_plan = mod.Plan(src=src, dst=blocker / "sub" / "x.srt", reason="测试")
    ok, skip, fail = mod.apply_plans([bad_plan], overwrite=False, delete_source=True)
    check("记为失败", fail == 1, f"fail={fail}")
    check("源文件未被误删", src.exists())

print("\n=== 6. 整条链路：删源开关经由插件实例生效 ===")
with Tree() as tmp:
    dl, lib = tmp / "dl", tmp / "library"
    subtitle(dl / "测试剧.S01E01.chs.srt")
    strm(lib / "测试剧 (2024)/Season 1/测试剧 (2024) - S01E01 - 第 1 集.strm")
    inst = make_plugin(mod, download_tv=str(dl), library_tv=str(lib), delete_source=True)
    inst.scan_library()
    check("字幕已入库",
          (lib / "测试剧 (2024)/Season 1/测试剧 (2024) - S01E01 - 第 1 集.zh-Hans.srt").exists(),
          sorted(x.name for x in (lib / "测试剧 (2024)/Season 1").glob("*")))
    check("源字幕已删", not list(dl.rglob("*.srt")), list(dl.rglob("*.srt")))

    # 关掉开关重跑，源字幕必须留着
    subtitle(dl / "测试剧.S01E01.chs.srt")
    keep = make_plugin(mod, download_tv=str(dl), library_tv=str(lib), delete_source=False)
    keep.scan_library()
    check("关闭开关时源字幕保留", len(list(dl.rglob("*.srt"))) == 1,
          list(dl.rglob("*.srt")))

print("\n=== 7. 开关默认值检查 ===")
src_code = Path(mod.__file__).read_text(encoding="utf-8")
check("apply_plans 签名为 False 默认",
      "delete_source: bool = False" in src_code)
check("事件处理把开关传给 apply_plans",
      "apply_plans(plans, self._overwrite, self._delete_source)" in src_code)
check("表单默认值 False",
      '"delete_source": False,' in src_code)
check("表单含删除不可逆的警示",
      "无法恢复" in src_code)
inst = make_plugin(mod, delete_source=True)
check("实例读到开关为 True", inst._delete_source is True)
inst2 = make_plugin(mod)
check("未配置时默认 False", inst2._delete_source is False)

print("\n" + "=" * 50)
if failures:
    print(f"结果: {len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果: 全部断言通过")
