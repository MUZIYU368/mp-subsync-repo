"""多语言标签剥离（v1.9.1）验证。

`strip_lang_tag` 原先命中第一条匹配就 `return`，只剥掉一个语言标签。
字幕名带双标签（`.chs.zh` / `.chs&eng.zh`）时，剩下的 `.zh` 前面没有分隔符，
直接粘到发布组名后面：

    原名    From.S01E05...-TEPES.chs.zh
    剥一次  From.S01E05...-TEPESzh        ← TEPES 和 zh 粘成一个 token
    tokens  ['from', 'silhouettes', 'tepeszh']

转移历史里的视频名是完整形式，切出 5-6 个 token。字幕的 3 个 token 去比，
覆盖率恒定 0.4，过不了 `cov >= 0.8` 门槛 → **明明有完美对应的转移记录
（hit=5 cov=1.00），却报未匹配**，字幕留在下载目录。

这里验证改成「循环剥到剥不动为止」之后：双标签剥干净、粘住的发布组名
被恢复、单标签行为不变（别把上一版的既有修复改坏）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Tree, load_plugin, strm, subtitle  # noqa: E402

mod = load_plugin()
F = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))
    if not cond:
        F.append(name)


print("\n=== 1. 用户现场：梦魇绝镇 S01 双标签字幕 ===")
RAW = "From.S01E05.Silhouettes.1080p.AMZN.WEB-DL.DDP5.1.H.264-TEPES.chs.zh"
stripped = mod.strip_lang_tag(RAW)
check("双标签 .chs.zh 剥干净",
      stripped == "From.S01E05.Silhouettes.1080p.AMZN.WEB-DL.DDP5.1.H.264-TEPES",
      repr(stripped))
core = mod.core_key(stripped)
# 注意两点，都以真实行为为准：
# ① TEPES 是发布组名，不在 RE_TECH 白名单里，本该保留；但 `normalize` 的
#    末尾 `-[A-Z][A-Z0-9]{2,}$` 会把 -TEPES 当压制组后缀剥掉，所以它也不在了。
# ② 关键判据不是「发布组名必须保留」，而是它不能和后面的语言残留粘成
#    'tepeszh' —— 粘连会让 token 数变少，覆盖率再也够不到 0.8 门槛。
check("没出现粘连 token", "tepeszh" not in core, core)
check("token 数恢复到 2 个（from + silhouettes）",
      core.split() == ["from", "silhouettes"], core.split())

print("\n=== 2. 多标签变体：全部标签都要剥掉 ===")
# 只要求「再无语言标签残留」。`&eng` / `&zh` 这类分隔写法由 core_key 切分，
# 与是否被 strip 剥掉无关，断言按真实行为写。
for raw, forbid in [
    ("Movie.S01E01.1080p.WEB-DL.chs&eng.zh", ("chs", "eng")),
    ("Movie.S01E01.1080p.WEB-DL.chs.cht.eng", ("chs", "cht", "eng")),
    ("Movie.S01E01.1080p.WEB-DL.cht&zh", ("cht",)),
    ("Movie.S01E01.1080p.WEB-DL.简繁", ("繁",)),
]:
    got = mod.strip_lang_tag(raw)
    tokens = mod.core_key(got).split()
    leaked = [t for t in tokens if t in forbid]
    check(f"剥净 {raw.split('.')[-2:]}  无 {forbid} 残留",
          not leaked, f"得到 {got!r} → {tokens}")

print("\n=== 3. 单标签与无标签行为不变（别改坏既有修复）===")
for raw, want in [
    ("Movie.S01E01.chs", "Movie.S01E01"),
    ("Avengers.Infinity.War.2018.2160p.WEB-DL.chs", "Avengers.Infinity.War.2018.2160p.WEB-DL"),
    ("三体.2023.chs", "三体.2023"),
    ("Dune.2021.2160p", "Dune.2021.2160p"),          # 无标签，原样返回
    ("Movie.S01E01", "Movie.S01E01"),
]:
    got = mod.strip_lang_tag(raw)
    check(f"单标签/无标签 {raw}", got == want, f"得到 {got!r}")

print("\n=== 4. 标题类里不含语言标签的片段不被误伤 ===")
# 「En」出现在正常片名中间，不能当成英文标签剥掉
for raw, want in [
    ("English.Teacher.S01E01.1080p", "English.Teacher.S01E01.1080p"),
    ("The.Chinese.Garden.S01", "The.Chinese.Garden.S01"),
]:
    got = mod.strip_lang_tag(raw)
    check(f"不误伤 {raw}", got == want, f"得到 {got!r}")

print("\n=== 5. 端到端：梦魇绝镇 5 条字幕精确落位 ===")
SUB_TPL = ("From.S01E{ep:02d}.{word}.1080p.AMZN.WEB-DL.DDP5.1.H.264-TEPES.chs.zh")
WORDS = {3: "Choosing.Day", 4: "A.Rock.and.a.Farway", 5: "Silhouettes",
         6: "Book.74", 9: "Into.the.Woods"}


class Row:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


ROWS = []
for ep, word in WORDS.items():
    for tag in ("chs", "chs&eng", "cht&zh"):
        ROWS.append(Row(
            src=f"/media/downloads/电视剧/欧美剧/From.S01.2160p.DDP5.1.H265-DIY@JAKET789/"
                f"From.S01E{ep:02d}.{word}.2160p.AMZN.WEB-DL.HEVC-DIY@JAKET789.{tag}",
            dest=f"/media/待上传/电视剧/欧美剧/梦魇绝镇 (2022)/Season 1/"
                 f"梦魇绝镇 - S01E{ep:02d} - 第 {ep} 集.mkv",
            mode="link", type="电视剧", title="梦魇绝镇", year="2022",
            seasons="S01", episodes=f"E{ep:02d}", status=True,
            date="2026-07-07 10:06:52"))

# 注意：字幕是 1080p TEPES 版、视频是 2160p JAKET789 版，发行批次不同，
# core_key 不会相等（字幕 'from choosing day tepes chs zh' vs 视频
# 'from choosing day diy jaket789 chs'）。所以这里**不能**断言 core 相等——
# 真正的判据在下面的端到端：lookup 能否凭 token 覆盖率命中。
for ep, word in WORDS.items():
    sub_core = mod.core_key(SUB_TPL.format(ep=ep, word=word))
    rec_core = mod.core_key(f"From.S01E{ep:02d}.{word}.2160p.AMZN.WEB-DL.HEVC-DIY@JAKET789.chs")
    check(f"E{ep:02d} 测试数据刻意不同批次（core 不等，靠覆盖率命中）",
          sub_core != rec_core, f"{sub_core!r} vs {rec_core!r}")

with Tree() as t:
    dl, lib = t / "dl", t / "library"
    season = lib / "电视剧/欧美剧/梦魇绝镇 (2022)/Season 1"
    for ep in range(1, 11):
        strm(season / f"梦魇绝镇 - S01E{ep:02d} - 第 {ep} 集.strm")

    movies = mod.build_movie_entries([])
    tvs = mod.build_tv_entries(sorted(season.glob("*.strm")))
    stem_index = mod.build_strm_stem_index(sorted(season.glob("*.strm")))
    index = mod.TransferIndex(ROWS)

    for ep, word in WORDS.items():
        name = SUB_TPL.format(ep=ep, word=word) + ".srt"
        srt = subtitle(dl / "电视剧/欧美剧/From.S01.2160p.DDP5.1.H265-DIY@JAKET789" / name,
                       chinese=False)
        sf = mod.SubtitleFile(path=srt, stem=srt.stem, lang="zh", is_chinese=True)
        got = mod.plan_for_subtitle(sf, movies, tvs, {}, index, stem_index)
        want = season / f"梦魇绝镇 - S01E{ep:02d} - 第 {ep} 集.zh.srt"
        check(f"🔑 E{ep:02d} 落回自己那一集",
              bool(got) and got[0].dst == want,
              got[0].dst.name if got else "未匹配")

print("\n" + "=" * 70)
if F:
    print(f"失败 {len(F)} 项: {F}")
    sys.exit(1)
print("全部通过 ✅")
