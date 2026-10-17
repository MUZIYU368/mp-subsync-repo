"""转移历史通道（v1.9.0）验证。

覆盖两件事：

① **读取签名兼容** —— MP v3 把 `TransferHistory.list_by_page` 从「静态方法 +
   `db_query` 装饰器自动注入会话」改成了「classmethod + 第一个位置参数必填
   `db: Session`」。插件原先只用关键字参数调用，不传 `db` → 每次都 TypeError →
   四种调用形态全试完都失败 → 返回 None → 日志打印「取不到转移历史表」，
   静默退回启发式。**用户在 MP 里明明有上万条转移历史，插件却说没有。**
   这里用两个版本的模型形态各测一遍，确保旧的 v2 形态不被改坏。

② **Doctor X 真实场景端到端** —— 字幕名纯英文（`[MT]Doctor.X.Mafia.in.White
   .S01E01...chs&kor.ass`），媒体库目录是中文（`Doctor X：白色黑手党时代 (2026)`），
   中间的 `电视剧/日韩剧/downloads/media` 全是分类目录、被 `CATEGORY_DIRS` 过滤掉，
   于是启发式通道手里**一个中文字都拿不到**，必然匹配失败。这正是转移历史通道
   存在的理由：它记着「这个英文资源名 → 整理后叫什么」。
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Tree, load_plugin, strm, subtitle  # noqa: E402

mod = load_plugin()
F = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))
    if not cond:
        F.append(name)


# --------------------------------------------------------------------------
# 假数据：直接照抄用户 MP 里 Doctor X 的真实转移历史行
# --------------------------------------------------------------------------
class Row:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


SUB_TPL = ("/media/downloads/电视剧/日韩剧/"
           "[MT]Doctor.X.Mafia.in.White.S01E{ep}.1080p.NF.WEB-DL.AAC2.0.H.264-MWeb.chs&kor.ass")
MKV_TPL = ("/media/downloads/电视剧/日韩剧/"
           "Doctor.X.Mafia.in.White.S01E{ep}.1080p.NF.WEB-DL.AAC2.0.H.264-MWeb.mkv")
DST_TPL = ("/media/待上传/电视剧/日韩剧/Doctor X：白色黑手党时代 (2026)/Season 1/"
           "Doctor X：白色黑手党时代 - S01E{ep} - 第 {ep} 集.ass")

ROWS = []
for _ep in (1, 2, 3):
    ROWS.append(Row(src=SUB_TPL.format(ep=_ep), dest=DST_TPL.format(ep=_ep),
                    mode="link", type="电视剧", title="Doctor X：白色黑手党时代",
                    year="2026", seasons="S01", episodes=f"E0{_ep}", status=True,
                    date="2026-10-17 18:46:19"))
ROWS.append(Row(src=MKV_TPL.format(ep=1),
                dest=("/media/待上传/电视剧/日韩剧/Doctor X：白色黑手党时代 (2026)/Season 1/"
                      "Doctor X：白色黑手党时代 - S01E01 - 第 1 集.mkv"),
                mode="link", type="电视剧", title="Doctor X：白色黑手党时代",
                year="2026", seasons="S01", episodes="E01", status=True,
                date="2026-10-17 18:47:06"))


class FakeSession:
    """能通过 MP 装饰器的 `isinstance(x, Session)` 鸭子判断即可。"""
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


# --------------------------------------------------------------------------
# ① 读取签名兼容
# --------------------------------------------------------------------------
def install_db_stubs(model_cls):
    """把假 MP 数据库包塞进 sys.modules，让插件的 importlib 能拿到。"""
    for name in ("app.db", "app.db.models", "app.db.models.transferhistory",
                 "app.db.session"):
        sys.modules.pop(name, None)
    app = sys.modules["app"]
    pkg_db = types.ModuleType("app.db")
    pkg_db.__path__ = []
    pkg_models = types.ModuleType("app.db.models")
    pkg_models.__path__ = []
    pkg_models.TransferHistory = model_cls
    mod_th = types.ModuleType("app.db.models.transferhistory")
    mod_th.TransferHistory = model_cls
    pkg_session = types.ModuleType("app.db.session")
    pkg_session.ScopedSession = lambda: FakeSession()
    pkg_session.SessionFactory = lambda: FakeSession()
    pkg_db.models = pkg_models
    pkg_db.ScopedSession = pkg_session.ScopedSession
    pkg_db.SessionFactory = pkg_session.SessionFactory
    app.db = pkg_db
    sys.modules["app.db"] = pkg_db
    sys.modules["app.db.models"] = pkg_models
    sys.modules["app.db.models.transferhistory"] = mod_th
    sys.modules["app.db.session"] = pkg_session


print("=== 1. MP v3 形态：classmethod + 必填 db 参数（当前 bug 所在）===")


class HistoryV3:
    """照抄 v3 签名：classmethod，第一个参数是必填的 db: Session。"""

    calls = []

    @classmethod
    def list_by_page(cls, db, page=1, count=30, status=None):
        if db is None:
            raise TypeError("list_by_page() missing 1 required positional argument: 'db'")
        HistoryV3.calls.append((page, count, status))
        return list(ROWS)


install_db_stubs(HistoryV3)
got = mod.fetch_transfer_rows()
check("v3 签名下读到了记录", bool(got), f"返回 {got!r}（这就是日志里「取不到转移历史表」的来源）")
check("分页参数按 limit 传下去",
      bool(HistoryV3.calls) and HistoryV3.calls[0][1] == 20000, HistoryV3.calls)
check("状态过滤传了 status=True",
      bool(HistoryV3.calls) and HistoryV3.calls[0][2] is True, HistoryV3.calls)

print("\n=== 2. MP v2 形态：静态方法 + db_query 装饰器（别被改坏）===")


class HistoryV2:
    """照抄 v2 签名：静态方法，db 由装饰器注入。"""

    calls = []

    @staticmethod
    def list_by_page(db=None, page=1, count=30, status=None):
        HistoryV2.calls.append((page, count, status))
        return list(ROWS)


install_db_stubs(HistoryV2)
got2 = mod.fetch_transfer_rows()
check("v2 签名下也读到了记录", bool(got2), f"返回 {got2!r}")
check("v2 传了 status=True",
      bool(HistoryV2.calls) and HistoryV2.calls[0][2] is True, HistoryV2.calls)

print("\n=== 3. 拿不到模型时返回 None（不是空表）===")
for name in ("app.db", "app.db.models", "app.db.models.transferhistory"):
    sys.modules.pop(name, None)
check("模型缺失 → None", mod.fetch_transfer_rows() is None, mod.fetch_transfer_rows())

install_db_stubs(HistoryV3)
index = mod.TransferIndex(mod.fetch_transfer_rows())
check("对照索引可建立", len(index.records) == len(ROWS), len(index.records))

# --------------------------------------------------------------------------
# ④ Doctor X 真实场景端到端
# --------------------------------------------------------------------------
print("\n=== 4. Doctor X：英文字幕名 → 中文媒体库（真实失败现场）===")
SUB_NAME = "[MT]Doctor.X.Mafia.in.White.S01E01.1080p.NF.WEB-DL.AAC2.0.H.264-MWeb.chs&kor.ass"

with Tree() as t:
    dl, lib = t / "dl", t / "library"
    # 分类目录名全部在 CATEGORY_DIRS 里，启发式通道拿不到任何中文线索
    sub = subtitle(dl / "电视剧/日韩剧" / SUB_NAME, chinese=False)
    season = lib / "电视剧/日韩剧/Doctor X：白色黑手党时代 (2026)/Season 1"
    for ep in (1, 2, 3):
        strm(season / f"Doctor X：白色黑手党时代 - S01E{ep:02d} - 第 {ep} 集.strm")

    subs = mod.collect_subtitles(dl)
    check("字幕被收集到了", len(subs) == 1, [s.stem for s in subs])
    check("识别为简体", bool(subs) and subs[0].lang == "zh-Hans",
          subs[0].lang if subs else None)

    movies, tvs = mod.build_movie_entries([]), mod.build_tv_entries(
        sorted(season.glob("*.strm")))
    check("媒体库建出 1 个剧集条目", len(tvs) == 1, [t_.title for t_ in tvs])
    stem_index = mod.build_strm_stem_index(sorted(season.glob("*.strm")))

    s0 = subs[0]
    # 启发式通道（转移历史关掉）—— 复现用户看到的「未匹配」
    heur = mod.plan_for_subtitle(s0, movies, tvs, {}, None, stem_index)
    check("🔑 只走启发式时匹配失败（这就是用户看到的现象）", not heur,
          heur[0].dst.name if heur else "")

    # 转移历史通道 —— 修好后应精确落到第 1 集
    fixed = mod.plan_for_subtitle(s0, movies, tvs, {}, index, stem_index)
    check("🔑 转移历史通道匹配成功", bool(fixed), "修好读取后仍匹配不上")
    if fixed:
        want = season / "Doctor X：白色黑手党时代 - S01E01 - 第 1 集.zh-Hans.ass"
        check("落点是第 1 集（集号取自字幕自己，不是转移历史那一条）",
              fixed[0].dst == want, fixed[0].dst.name)
        check("理由标注了走的是转移历史", "转移历史" in fixed[0].reason, fixed[0].reason)

    # 集号必须跟着字幕走：转移历史里有 E01/E02/E03 三条，不能全落到同一集
    for ep in (1, 2, 3):
        srt = subtitle(dl / f"电视剧/日韩剧/[MT]Doctor.X.Mafia.in.White.S01E{ep:02d}"
                               f".1080p.NF.WEB-DL.AAC2.0.H.264-MWeb.chs&kor.srt", chinese=False)
        sf = mod.SubtitleFile(path=srt, stem=srt.stem, lang="zh-Hans", is_chinese=False)
        got_p = mod.plan_for_subtitle(sf, movies, tvs, {}, index, stem_index)
        want_p = season / f"Doctor X：白色黑手党时代 - S01E{ep:02d} - 第 {ep} 集.zh-Hans.srt"
        check(f"E{ep:02d} 精确落到自己那一集",
              bool(got_p) and got_p[0].dst == want_p,
              got_p[0].dst.name if got_p else "未匹配")

print("\n" + "=" * 70)
if F:
    print(f"失败 {len(F)} 项: {F}")
    sys.exit(1)
print("全部通过 ✅")
