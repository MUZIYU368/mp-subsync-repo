"""立即补扫按钮（v1.8.0）专项验证。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Tree, load_plugin, make_plugin, strm, subtitle  # noqa: E402

mod = load_plugin()
F = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))
    if not cond:
        F.append(name)


print("=== 1. API 注册 ===")
apis = mod.SubSync.get_api(mod.SubSync.__new__(mod.SubSync))
check("注册了 API", isinstance(apis, list) and len(apis) == 1, apis)
api = apis[0] if apis else {}
check("路径是 /scan_now", api.get("path") == "/scan_now", api.get("path"))
check("支持 GET", "GET" in (api.get("methods") or []), api.get("methods"))
check("有 summary", bool(api.get("summary")), api.get("summary"))

print("\n=== 2. 未启用时不执行 ===")
with Tree() as t:
    p = make_plugin(mod, download_tv=str(t / "dl"), library_tv=str(t / "lib"),
                    enabled=False)
    r = p.run_scan_now()
    check("返回失败", r.get("success") is False, r)
    check("提示未启用", "未启用" in r.get("message", ""), r.get("message"))
    check("有推送告知用户", len(p._store.get("_posted", [])) >= 1,
          p._store.get("_posted"))

print("\n=== 3. 正常触发（端到端）===")
with Tree() as t:
    dl, lib = t / "dl", t / "library"
    subtitle(dl / "三体.2023.chs.srt")
    season = lib / "电视剧/国产剧/三体 (2023)/Season 1"
    strm(season / "三体 (2023) - S01E01 - 第 1 集.strm")
    p = make_plugin(mod, download_tv=str(dl), library_tv=str(lib))
    r = p.run_scan_now()
    check("返回成功", r.get("success") is True, r)
    check("带本次统计", bool(r.get("stat")), r.get("stat"))
    check("成功数为 1", r["stat"].get("ok") == 1, r["stat"])
    check("字幕真的入库",
          (season / "三体 (2023) - S01E01 - 第 1 集.zh-Hans.srt").exists(),
          sorted(x.name for x in season.glob("*")))
    check("统计已落盘", p.get_data("last_scan", {}).get("ok") == 1,
          p.get_data("last_scan"))
    check("推送了结果", len(p._store.get("_posted", [])) >= 1,
          p._store.get("_posted"))

print("\n=== 4. 失败路径也要返回可读信息 ===")
with Tree() as t:
    (t / "dl").mkdir()          # 下载目录有效，让它走到媒体库那一步
    p = make_plugin(mod, download_tv=str(t / "dl"), library_tv="/nowhere/gone")
    r = p.run_scan_now()
    check("标记失败", r.get("success") is False, r)
    check("说明原因", "媒体库目录" in r.get("message", ""), r.get("message"))
    check("统计标了 error", (r.get("stat") or {}).get("error") is True,
          r.get("stat"))

print("\n=== 5. 详情页按钮渲染 ===")
with Tree() as t:
    p = make_plugin(mod, download_tv=str(t / "dl"), library_tv=str(t / "lib"))
    page = p.get_page()
    txt = str(page)
    check("页面返回块", isinstance(page, list) and len(page) >= 3, len(page or []))
    check("含按钮文案「立即补扫一次」", "立即补扫一次" in txt)
    check("含 VBtn 组件", '"component": "VBtn"' in txt or "'component': 'VBtn'" in txt)

    # 按钮必须指向新加的 API
    import json
    blob = json.dumps(page, ensure_ascii=False)
    check("按钮调用 scan_now", "scan_now" in blob)
    check("按钮用 VSpacer 靠右", '"VSpacer"' in blob)

print("\n=== 6. 插件停用时按钮禁用 ===")
with Tree() as t:
    p = make_plugin(mod, download_tv=str(t / "dl"), library_tv=str(t / "lib"),
                    enabled=False)
    import json
    blob = json.dumps(p.get_page(), ensure_ascii=False)
    check("disabled=True", '"disabled": true' in blob or '"disabled":true' in blob,
          "按钮未禁用")

print("\n=== 7. 详情页整体没被破坏 ===")
with Tree() as t:
    (t / "ok").mkdir()
    p = make_plugin(mod, download_movie=str(t / "ok"), library_movie=str(t / "ok"))
    txt = str(p.get_page())
    check("状态条还在", "运行中" in txt)
    check("统计磁贴还在", "媒体库 strm" in txt and "上次未匹配" in txt)
    check("运行信息面板还在", "下载目录" in txt)

print("\n" + "=" * 60)
if F:
    print(f"结果: {len(F)} 项失败")
    for x in F:
        print("   -", x)
    sys.exit(1)
print("结果: 全部通过 ✅")
