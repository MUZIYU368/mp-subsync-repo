"""SubSync 端到端功能测试（离线打桩运行，不触碰真实媒体目录）。

覆盖两条通道（TransferComplete 事件 / 全库补扫）与三种匹配路径
（转移历史 / 启发式 / 别名映射），以及幂等、年份预过滤、目录识别、
中转目录拦截、畸形事件容错、详情页与定时服务注册。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Tree, load_plugin, make_plugin, strm, subtitle  # noqa: E402

mod = load_plugin()
failures = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}" + ("" if cond else f"   {detail}"))
    if not cond:
        failures.append(name)


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 纯函数
section("1. 文本归一化与提取")
check("剥离 .chs 尾巴", mod.strip_lang_tag("Movie.S01E01.chs") == "Movie.S01E01")
check("识别 zh-Hans", mod.parse_lang_tag("A.zh-Hans")[0] == "zh-Hans")
check("识别 eng", mod.parse_lang_tag("A.eng")[0] == "en")
check("识别繁中", mod.parse_lang_tag("A.cht")[0] == "zh-Hant")
check("core_key 剥技术参数",
      mod.core_key("Avengers.Infinity.War.2018.2160p.WEB-DL") == "avengers infinity war",
      mod.core_key("Avengers.Infinity.War.2018.2160p.WEB-DL"))
check("全角/半角冒号视为同名",
      mod.loose_title("三体：决战") == mod.loose_title("三体:决战"))
check("不同作品不误判", not mod.title_eq("三体", "球状闪电"))
check("英文虚词剔除", "the" not in mod.title_key("The Lord of the Rings"))
check("季集号提取", mod.extract_season_episode("Show.S02E05") == (2, 5))
check("中文 LCS 连续 2 字命中", mod.chinese_title_match_score("狂飙飞车", "狂飙飞车2") >= 2)
check("中文 LCS 单字不计分", mod.chinese_title_match_score("三体", "体") == 0)
check("多目录分隔符（逗号/分号/换行）",
      mod.split_paths("/a,/b；/c\n/d") == ["/a", "/b", "/c", "/d"])

print("\n  年份提取（取最后一个，片名自带年份时不能取错）：")
for name, want in [
    ("三体.2023", "2023"), ("Movie.2018", "2018"), ("三体 (2023)", "2023"),
    ("三体2023", "2023"), ("Avengers 2018", "2018"), ("1917 (2019)", "2019"),
    ("三体.2023.2160p", "2023"), ("三体", None), ("三体.S01E01", None),
    ("Movie.2160p", None), ("三体III", None),
]:
    check(f"  extract_year({name!r}) = {want}", mod.extract_year(name) == want,
          f"实得 {mod.extract_year(name)}")

# ---------------------------------------------------------------- 目录识别
section("2. 媒体库目录识别")
with Tree() as t:
    a = strm(t / "电影/外语电影/F1：狂飙飞车 (2025)/F1：狂飙飞车 (2025) - 2160p.strm")
    b = strm(t / "电影/外语电影/复仇者联盟3：无限战争/复仇者联盟3：无限战争 (2018) - 2160p.strm")
    c = strm(t / "电视剧/国产剧/三体 (2023)/Season 1/三体 (2023) - S01E01 - 第 1 集.strm")
    d = strm(t / "115/复仇者联盟3：无限战争 (2018)/复仇者联盟3：无限战争 (2018) - 2160p.strm")

    check("写法1：目录名带年份", mod.entry_meta(a)[:2] == ("F1：狂飙飞车", "2025"),
          mod.entry_meta(a))
    check("写法2：年份写在文件名", mod.entry_meta(b)[:2] == ("复仇者联盟3：无限战争", "2018"),
          mod.entry_meta(b))
    check("剧集 Season 目录定位到作品目录", mod.entry_meta(c)[0] == "三体",
          mod.entry_meta(c))
    check("115 中转目录被拒绝", mod.entry_meta(d) is None, mod.entry_meta(d))
    check("剧集不进电影条目", mod.build_movie_entries([c]) == [],
          mod.build_movie_entries([c]))
    check("剧集进剧集条目", len(mod.build_tv_entries([c])) == 1)
    check("无季集号的 strm 不进剧集条目", mod.build_tv_entries([b]) == [])

# ---------------------------------------------------------------- 电影
section("3. 电影字幕落地与幂等")
with Tree() as t:
    dl, lib = t / "dl", t / "library"
    subtitle(dl / "流浪地球.2019.2160p.chs.srt")
    subtitle(dl / "流浪地球.2019.2160p.eng.srt", chinese=False)
    d = lib / "电影/华语电影/流浪地球 (2019)"
    strm(d / "流浪地球 (2019) - 2160p.strm")

    make_plugin(mod, download_movie=str(dl), library_movie=str(lib)).scan_library()
    got = sorted(x.name for x in d.glob("*.srt"))
    check("「仅中文」只落中文字幕",
          got == ["流浪地球 (2019) - 2160p.zh-Hans.srt"], got)

    make_plugin(mod, download_movie=str(dl), library_movie=str(lib),
                chinese_only=False).scan_library()
    got = sorted(x.name for x in d.glob("*.srt"))
    check("关掉「仅中文」后英文也落", len(got) == 2, got)
    check("英文字幕命名 en", "流浪地球 (2019) - 2160p.en.srt" in got, got)

    make_plugin(mod, download_movie=str(dl), library_movie=str(lib)).scan_library()
    check("重复扫描幂等（不重复复制）", len(list(d.glob("*.srt"))) == 2)

section("4. 电影多版本全部落地")
with Tree() as t:
    dl, lib = t / "dl", t / "library"
    subtitle(dl / "流浪地球.2019.2160p.chs.srt")
    d = lib / "电影/华语电影/流浪地球 (2019)"
    for res in ("2160p", "1080p", "4K HDR"):
        strm(d / f"流浪地球 (2019) - {res}.strm")
    make_plugin(mod, download_movie=str(dl), library_movie=str(lib)).scan_library()
    got = sorted(x.name for x in d.glob("*.srt"))
    check("三个清晰度都拿到字幕", len(got) == 3, got)

    for x in d.glob("*.srt"):
        x.unlink()
    make_plugin(mod, download_movie=str(dl), library_movie=str(lib),
                multi_version=False).scan_library()
    check("关掉多版本只落一个", len(list(d.glob("*.srt"))) == 1,
          sorted(x.name for x in d.glob("*.srt")))

# ---------------------------------------------------------------- 剧集
section("5. 剧集季集号精确定位")
with Tree() as t:
    dl, lib = t / "dl", t / "library"
    subtitle(dl / "三体.S01E02.chs.srt")
    subtitle(dl / "三体.S01E03.chs.srt")
    season = lib / "电视剧/国产剧/三体 (2023)/Season 1"
    for ep in (1, 2, 3):
        strm(season / f"三体 (2023) - S01E{ep:02d} - 第 {ep} 集.strm")
    # 同名电影 —— 剧集字幕绝不能落这里
    movie = lib / "电影/华语电影/三体 (2010)"
    strm(movie / "三体 (2010) - 2160p.strm")

    make_plugin(mod, download_tv=str(dl), library_tv=str(lib)).scan_library()
    check("E02 落位", (season / "三体 (2023) - S01E02 - 第 2 集.zh-Hans.srt").exists(),
          sorted(x.name for x in season.glob("*.srt")))
    check("E03 落位", (season / "三体 (2023) - S01E03 - 第 3 集.zh-Hans.srt").exists(),
          sorted(x.name for x in season.glob("*.srt")))
    check("无字幕的 E01 不误生成",
          not (season / "三体 (2023) - S01E01 - 第 1 集.zh-Hans.srt").exists())
    check("未污染同名电影目录", not list(movie.glob("*.srt")),
          list(movie.glob("*.srt")))
    check("源字幕保留", len(list(dl.rglob("*.srt"))) == 2)

section("6. 不带季集号的剧集字幕只落首集")
with Tree() as t:
    dl, lib = t / "dl", t / "library"
    subtitle(dl / "三体.chs.srt")
    season = lib / "电视剧/国产剧/三体 (2023)/Season 1"
    for ep in (1, 2, 3):
        strm(season / f"三体 (2023) - S01E{ep:02d} - 第 {ep} 集.strm")
    make_plugin(mod, download_tv=str(dl), library_tv=str(lib)).scan_library()
    got = sorted(x.name for x in season.glob("*.srt"))
    check("只落 1 集，不铺开全季", len(got) == 1, got)

section("7. 年份预过滤拒绝跨年错配")
with Tree() as t:
    dl, lib = t / "dl", t / "library"
    subtitle(dl / "三体.2010.chs.srt")     # 年份在 stem 末尾
    season = lib / "电视剧/国产剧/三体 (2023)/Season 1"
    strm(season / "三体 (2023) - S01E01 - 第 1 集.strm")
    make_plugin(mod, download_tv=str(dl), library_tv=str(lib)).scan_library()
    check("年份不符不落位", not list(season.glob("*.srt")),
          list(season.glob("*.srt")))

# ---------------------------------------------------------------- 别名
section("8. 别名映射兜底通道")
with Tree() as t:
    dl, lib = t / "dl", t / "library"
    subtitle(dl / "Avengers.Infinity.War.2018.2160p.WEB-DL.chs.srt")
    d = lib / "电影/外语电影/复仇者联盟3：无限战争 (2018)"
    strm(d / "复仇者联盟3：无限战争 (2018) - 2160p.strm")

    make_plugin(mod, download_movie=str(dl), library_movie=str(lib)).scan_library()
    check("无别名时不硬凑（宁可不放也不放错）", not list(d.glob("*.srt")),
          list(d.glob("*")))

    make_plugin(mod, download_movie=str(dl), library_movie=str(lib),
                alias="avengers infinity war|复仇者联盟3：无限战争").scan_library()
    check("配了别名后落地",
          (d / "复仇者联盟3：无限战争 (2018) - 2160p.zh-Hans.srt").exists(),
          sorted(x.name for x in d.glob("*")))

# ---------------------------------------------------------------- 事件通道
section("9. TransferComplete 事件通道")
with Tree() as t:
    dl, lib = t / "dl", t / "library"
    subtitle(dl / "三体.S01E01.chs.srt")
    season = lib / "电视剧/国产剧/三体 (2023)/Season 1"
    strm(season / "三体 (2023) - S01E01 - 第 1 集.strm")

    inst = make_plugin(mod, download_tv=str(dl), library_tv=str(lib))
    inst.on_transfer_complete(mod.Event(
        {"transferinfo": {"target_diritem": {"path": str(season.parent)}}}))
    check("事件通道落字幕",
          (season / "三体 (2023) - S01E01 - 第 1 集.zh-Hans.srt").exists(),
          sorted(x.name for x in season.glob("*.srt")))

    # 时间窗口之外的老字幕不被事件通道捡起
    old = subtitle(dl / "老剧.S01E01.chs.srt")
    import os
    old_t = mod.time.time() - 30 * 86400
    os.utime(old, (old_t, old_t))
    old_season = lib / "电视剧/国产剧/老剧 (2020)/Season 1"
    strm(old_season / "老剧 (2020) - S01E01 - 第 1 集.strm")
    make_plugin(mod, download_tv=str(dl), library_tv=str(lib),
                recent_window_days=1).on_transfer_complete(mod.Event(
                    {"transferinfo": {"target_diritem": {"path": str(old_season.parent)}}}))
    check("超时间窗口的老字幕不被处理", not list(old_season.glob("*.srt")))

    # 未启用 → 不做事
    off = make_plugin(mod, download_tv=str(dl), library_tv=str(lib))
    off._enabled = False
    off_season = lib / "电视剧/国产剧/禁用剧 (2019)/Season 1"
    strm(off_season / "禁用剧 (2019) - S01E01 - 第 1 集.strm")
    subtitle(dl / "禁用剧.S01E01.chs.srt")
    off.on_transfer_complete(mod.Event(
        {"transferinfo": {"target_diritem": {"path": str(off_season.parent)}}}))
    check("未启用不处理", not list(off_season.glob("*.srt")))

section("10. 畸形事件不崩")
with Tree() as t:
    inst = make_plugin(mod, download_movie=str(t / "dl"), library_movie=str(t / "lib"))
    for bad in (None, {}, {"transferinfo": None}, {"transferinfo": {}},
                {"transferinfo": {"target_diritem": None}},
                {"transferinfo": {"target_diritem": {}}},
                {"transferinfo": {"target_diritem": {"path": ""}}},
                {"transferinfo": {"target_diritem": {"path": "/nowhere/zzz"}}}):
        try:
            inst.on_transfer_complete(mod.Event(bad))
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"      抛异常: {type(exc).__name__}: {exc}")
        check(f"容忍 {bad}", ok)

# ---------------------------------------------------------------- 统计与页面
section("11. 补扫统计与详情页")
with Tree() as t:
    dl, lib = t / "dl", t / "library"
    subtitle(dl / "三体.2023.chs.srt")
    season = lib / "电视剧/国产剧/三体 (2023)/Season 1"
    strm(season / "三体 (2023) - S01E01 - 第 1 集.strm")

    inst = make_plugin(mod, download_tv=str(dl), library_tv=str(lib))
    check("跑之前不显示补扫面板", "上次补扫 ·" not in str(inst.get_page()))
    inst.scan_library()
    stat = inst.get_data("last_scan", {})
    check("统计已持久化", stat.get("ok") == 1, stat)
    check("跑过后显示补扫面板", "上次补扫 ·" in str(inst.get_page()))
    check("未标错误", not stat.get("error"), stat)

    # 失败路径也必须落统计，否则页面停在旧的成功数据上
    bad = make_plugin(mod, download_tv=str(dl), library_tv="/nowhere/gone")
    msg = bad.scan_library()
    bstat = bad.get_data("last_scan", {})
    check("失败也落统计", bool(bstat.get("time")), bstat)
    check("失败标记 error", bstat.get("error") is True, bstat)
    check("失败消息记录", "失败" in str(bstat.get("msg", "")), bstat.get("msg"))
    page = str(bad.get_page())
    check("详情页显示失败标签", "上次补扫失败" in page)
    check("失败信息含原因", "媒体库目录" in msg, msg)

    # 定时任务失败必须推送，不能静默
    quiet = make_plugin(mod, download_tv=str(dl), library_tv="/nowhere/gone")
    quiet._run_backfill()
    check("失败时定时任务有推送", len(quiet._store.get("_posted", [])) >= 1,
          quiet._store.get("_posted"))

section("12. 配置自检与服务注册")
with Tree() as t:
    (t / "ok1").mkdir()
    (t / "ok2").mkdir()
    inst = make_plugin(mod, download_movie=f"{t}/ok1,/nowhere/x",
                       download_tv=str(t / "ok2"),
                       library_movie=f"{t}/ok1,/nowhere/y",
                       library_tv="")
    probs = inst._config_problems()
    check("多目录「或」关系不误报", not [x for x in probs if "下载目录" in x], probs)
    check("未填写被报出", any("媒体库·剧集 未填写" in x for x in probs), probs)

    check("未启用不注册定时服务",
          make_plugin(mod, enabled=False).get_service() == [])
    check("非法 cron 回退到默认",
          make_plugin(mod, backfill_cron="bad cron").get_service()[0]["trigger"].expr
          == "30 4 * * *")
    check("空 cron 用默认",
          make_plugin(mod, backfill_cron="").get_service()[0]["trigger"].expr
          == "30 4 * * *")
    check("合法 cron 生效",
          make_plugin(mod, backfill_cron="0 5 * * *").get_service()[0]["trigger"].expr
          == "0 5 * * *")
    check("服务 id 唯一",
          make_plugin(mod).get_service()[0]["id"] == "SubSync.Backfill")

section("13. 远程命令注册")
cmds = mod.SubSync.get_command()
check("注册 /subsync_scan_all",
      any(c.get("cmd") == "/subsync_scan_all" for c in cmds), cmds)

print("\n" + "=" * 60)
if failures:
    print(f"结果: {len(failures)} 项失败")
    for f in failures:
        print("   -", f)
    sys.exit(1)
print("结果: 全部通过")
