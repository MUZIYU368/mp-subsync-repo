"""立即运行一次（v1.8.1）验证。

覆盖两条真实可用的触发路径：
  ① 配置页「立即运行一次」开关 → 保存即触发（init_plugin 消费信号）
  ② REST 接口 GET/POST /plugin/SubSync/scan_now → 必须挂上 endpoint
"""
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


print("=== 1. API 必须带 endpoint（v1.8.0 漏了它导致 404）===")
inst = mod.SubSync.__new__(mod.SubSync)
inst._store = {}
apis = inst.get_api()
check("注册了 API", isinstance(apis, list) and len(apis) == 1, apis)
api = apis[0] if apis else {}
check("路径是 /scan_now", api.get("path") == "/scan_now", api.get("path"))
check("🔑 有 endpoint 字段", "endpoint" in api, list(api))
check("endpoint 指向 run_scan_now", api.get("endpoint") == inst.run_scan_now,
      api.get("endpoint"))
check("endpoint 是可调用对象", callable(api.get("endpoint")))
check("支持 GET/POST", set(api.get("methods") or []) >= {"GET", "POST"},
      api.get("methods"))

print("\n=== 2. run_now 开关存在且默认关 ===")
form, defaults = inst.get_form()
check("默认值 run_now=False", defaults.get("run_now") is False, defaults.get("run_now"))
import json
blob = json.dumps(form, ensure_ascii=False)
check("表单含 run_now 开关", '"run_now"' in blob)
check("标签是「立即运行一次」", "立即运行一次" in blob)

print("\n=== 3. 打开开关保存 → 真的跑一次 ===")
with Tree() as t:
    dl, lib = t / "dl", t / "library"
    subtitle(dl / "三体.2023.chs.srt")
    season = lib / "电视剧/国产剧/三体 (2023)/Season 1"
    strm(season / "三体 (2023) - S01E01 - 第 1 集.strm")

    p = mod.SubSync.__new__(mod.SubSync)
    p._store = {}
    p._cfg = {}
    p.get_config = lambda: dict(p._cfg)
    p.update_config = lambda cfg: p._cfg.update(cfg)

    cfg = {"enabled": True, "run_now": True,
           "download_tv": str(dl), "library_tv": str(lib)}
    p.init_plugin(cfg)

    check("字幕真的入库了",
          (season / "三体 (2023) - S01E01 - 第 1 集.zh-Hans.srt").exists(),
          sorted(x.name for x in season.glob("*")))
    check("统计落盘 ok=1", p.get_data("last_scan", {}).get("ok") == 1,
          p.get_data("last_scan"))
    check("推了结果", len(p._store.get("_posted", [])) >= 1, p._store.get("_posted"))
    check("🔑 run_now 已复位为 False", p._cfg.get("run_now") is False, p._cfg.get("run_now"))
    check("run_now 没混进运行态属性", not hasattr(p, "_run_now"))

print("\n=== 4. 复位后改别的配置不会重复跑 ===")
with Tree() as t:
    dl, lib = t / "dl", t / "library"
    subtitle(dl / "三体.2023.chs.srt")
    season = lib / "电视剧/国产剧/三体 (2023)/Season 1"
    strm(season / "三体 (2023) - S01E01 - 第 1 集.strm")

    p = mod.SubSync.__new__(mod.SubSync)
    p._store = {}
    p._cfg = {}
    p.get_config = lambda: dict(p._cfg)
    p.update_config = lambda cfg: p._cfg.update(cfg)

    p.init_plugin({"enabled": True, "run_now": True,
                   "download_tv": str(dl), "library_tv": str(lib)})
    first = p.get_data("last_scan", {}).get("time")
    check("第一次真的跑了", bool(first), first)

    subtitle(dl / "流浪地球.2019.chs.srt")
    strm(lib / "电影/华语电影/流浪地球 (2019)/流浪地球 (2019) - 2160p.strm")
    # 再 init 一次，但 run_now 已被复位为 False
    p.init_plugin({"enabled": True, "run_now": p._cfg.get("run_now"),
                   "download_tv": str(dl), "library_tv": str(lib)})
    second = p.get_data("last_scan", {}).get("time")
    check("🔑 第二次没有重复触发", first == second, f"{first} -> {second}")
    check("新字幕没被自动处理",
          not list((lib / "电影/华语电影/流浪地球 (2019)").glob("*.srt")))

print("\n=== 5. 未启用时点开关 → 明确告知，不静默 ===")
with Tree() as t:
    p = mod.SubSync.__new__(mod.SubSync)
    p._store = {}
    p._cfg = {}
    p.get_config = lambda: dict(p._cfg)
    p.update_config = lambda cfg: p._cfg.update(cfg)
    p.init_plugin({"enabled": False, "run_now": True,
                   "download_tv": str(t / "dl"), "library_tv": str(t / "lib")})
    posted = p._store.get("_posted", [])
    check("仍复位了 run_now", p._cfg.get("run_now") is False, p._cfg.get("run_now"))
    check("推送了提示", len(posted) >= 1, posted)
    check("提示是未启用", any("未启用" in str(m) for m in posted), posted)

print("\n=== 6. API 端点 run_scan_now 直接调 ===")
with Tree() as t:
    dl, lib = t / "dl", t / "library"
    subtitle(dl / "三体.2023.chs.srt")
    season = lib / "电视剧/国产剧/三体 (2023)/Season 1"
    strm(season / "三体 (2023) - S01E01 - 第 1 集.strm")
    p = make_plugin(mod, download_tv=str(dl), library_tv=str(lib))
    r = p.run_scan_now()
    check("返回 success", r.get("success") is True, r)
    check("字幕入库",
          (season / "三体 (2023) - S01E01 - 第 1 集.zh-Hans.srt").exists(),
          sorted(x.name for x in season.glob("*")))

    off = make_plugin(mod, download_tv=str(dl), library_tv=str(lib), enabled=False)
    r2 = off.run_scan_now()
    check("未启用返回失败", r2.get("success") is False, r2)

print("\n=== 7. 详情页已撤掉假按钮 ===")
with Tree() as t:
    p = make_plugin(mod, download_movie=str(t / "dl"), library_movie=str(t / "lib"))
    blob = json.dumps(p.get_page(), ensure_ascii=False)
    check("详情页没有 VBtn", '"VBtn"' not in blob)
    check("详情页没有假 onClick", "onClick" not in blob)
    check("状态条还在", "运行中" in blob)
    check("统计磁贴还在", "媒体库 strm" in blob)

print("\n" + "=" * 60)
if F:
    print(f"结果: {len(F)} 项失败")
    for x in F:
        print("   -", x)
    sys.exit(1)
print("结果: 全部通过 ✅")
