"""
SubSync —— 外挂字幕同步
===========================
订阅 MoviePilot 的 TransferComplete 事件，在整理完成的那一刻，
把**下载目录**里的外挂字幕按 strm「同名复制」到最终媒体库目录。

设计要点
--------
- 双通道：
  1) 事件驱动——整理完成立即处理，延迟最低；
  2) 定时全库补扫——兜底。事件时机不理想、字幕晚到、容器重建等情况都能补齐。
- 落点永远是 strm 所在目录（strm.parent），不做分类推断，
  因此字幕与媒体文件必然同目录、同名主体，仅多一个语言标签。
- 媒体库索引只认「剧名 (年份)」结构的目录，因此 115 中转目录
  （平铺 strm，无该结构）会被自然跳过，不会把字幕错放到中间目录。
- 强匹配才动手：没有明确证据（中文 2 字以上 / 英文整词 / 别名）就整条放弃。
- 幂等：目标已存在且未开启覆盖时跳过，重复扫描不会反复复制。
"""
from __future__ import annotations

import re
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}

LANG_TAGS = [
    (re.compile(r"\.chs\.?(?!a)", re.I), "zh-Hans"),
    (re.compile(r"\.gb\.?", re.I), "zh-Hans"),
    (re.compile(r"\.zh-hans\.?", re.I), "zh-Hans"),
    (re.compile(r"\.简体中文\.?", re.I), "zh-Hans"),
    (re.compile(r"\.简中\.?", re.I), "zh-Hans"),
    (re.compile(r"\.(?<!中)简(?!体)(?!中)(?!单)(?!单字)", re.I), "zh-Hans"),
    (re.compile(r"\.cht\.?(?!a)", re.I), "zh-Hant"),
    (re.compile(r"\.big5\.?", re.I), "zh-Hant"),
    (re.compile(r"\.zh-hant\.?", re.I), "zh-Hant"),
    (re.compile(r"\.zh-tw\.?", re.I), "zh-Hant"),
    (re.compile(r"\.繁体中文\.?", re.I), "zh-Hant"),
    (re.compile(r"\.繁中\.?", re.I), "zh-Hant"),
    (re.compile(r"\.eng\.?", re.I), "en"),
    (re.compile(r"\.en\.?(?![-_])", re.I), "en"),
    (re.compile(r"\.english\.?", re.I), "en"),
    (re.compile(r"\.jp\.?", re.I), "ja"),
    (re.compile(r"\.jpn\.?", re.I), "ja"),
    (re.compile(r"\.ja\.?(?![-_])", re.I), "ja"),
    (re.compile(r"\.kr\.?", re.I), "ko"),
    (re.compile(r"\.kor\.?", re.I), "ko"),
    (re.compile(r"\.ko\.?(?![-_])", re.I), "ko"),
    (re.compile(r"\.zh\.?(?![-_])", re.I), "zh"),
    (re.compile(r"\.cn\.?(?![-_])", re.I), "zh"),
    (re.compile(r"\.chi\.?(?![-_])", re.I), "zh"),
]

CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")
RE_TV_EPISODE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
RE_YEAR = re.compile(r"[\.\s\(\[](19\d{2}|20\d{2})[\.\s\)\]]")
RE_LIB_DIR = re.compile(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)\s*$")
RE_SEASON_DIR = re.compile(r"^(?:Season|S)\s*\d+", re.I)

# 分类目录名：向上查找标题线索时跳过，避免「电影」「欧美剧」这类目录名参与打分
CATEGORY_DIRS = {
    "外语电影", "华语电影", "动画电影", "电视剧", "欧美剧", "国产剧",
    "日韩剧", "日番", "国漫", "儿童", "纪录片", "综艺",
    "电影", "downloads", "media", "Media", "MEDIA",
}


@dataclass
class SubtitleFile:
    """一条待处理的外挂字幕。"""

    path: Path
    stem: str
    lang: str
    is_chinese: bool


@dataclass
class Plan:
    """一条字幕复制计划。"""

    src: Path
    dst: Path
    reason: str = ""


@dataclass
class MovieEntry:
    """媒体库中的一个电影候选条目。"""

    title: str
    year: Optional[str]
    strm_files: List[Path] = field(default_factory=list)


@dataclass
class TvEntry:
    """媒体库中的一个剧集候选条目。"""

    title: str
    year: Optional[str]
    seasons: Dict[int, Dict[int, Path]] = field(default_factory=dict)


def parse_lang_tag(stem: str) -> Tuple[str, bool]:
    """从文件名中剥离语言标记，返回 (语言标签, 是否中文)。"""
    name = stem
    matched = ""
    for rx, lang in LANG_TAGS:
        m = rx.search(name)
        if m:
            matched = lang
            name = (name[: m.start()] + name[m.end():]).strip(".-_ ")
            break
    return matched or "zh", matched.startswith("zh")


def sniff_chinese(path: Path, max_bytes: int = 4096) -> bool:
    """抽样读取字幕头部判断是否中文字幕；非 srt 文件直接按中文字幕处理。"""
    if path.suffix.lower() != ".srt":
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(max_bytes)
        text = head.decode("utf-8", errors="ignore")
        if not text.strip():
            text = head.decode("gbk", errors="ignore")
        cjk = len(CJK_RE.findall(text))
        alpha = sum(1 for ch in text if ch.isalpha())
        if cjk == 0 and alpha == 0:
            return False
        return (cjk / max(cjk + alpha, 1)) > 0.30
    except OSError:
        return True


def collect_subtitles(root: Path, chinese_only: bool = True,
                      since: Optional[float] = None) -> List[SubtitleFile]:
    """递归收集指定目录下的字幕文件。

    since 给定时，只保留 mtime 不早于该时刻的文件。事件通道用它避免每次整理
    都全量遍历下载目录；全库补扫不传 since，以便处理历史积压。
    """
    out: List[SubtitleFile] = []
    if not root.exists() or not root.is_dir():
        return out
    try:
        children = list(root.rglob("*"))
    except OSError:
        return out
    for sub in children:
        if not sub.is_file() or sub.suffix.lower() not in SUB_EXTS:
            continue
        if since is not None:
            try:
                if sub.stat().st_mtime < since:
                    continue
            except OSError:
                continue
        lang, is_zh = parse_lang_tag(sub.stem)
        if chinese_only and not (is_zh or sniff_chinese(sub)):
            continue
        out.append(SubtitleFile(path=sub, stem=sub.stem, lang=lang, is_chinese=is_zh))
    return out


def normalize(s: str) -> str:
    """去掉发布组、分辨率、编码等噪声，得到可比对的基础标题。"""
    s = re.sub(r"^\[[^\]]+\]\s*", "", s)
    s = re.sub(r"\[[^\]]+\]", "", s)
    s = re.sub(r"-[A-Z][A-Z0-9]{2,}$", "", s)
    s = re.sub(r"\b\d{3,4}p\b", " ", s, flags=re.I)
    s = re.sub(
        r"\b(BluRay|BDRip|BDRemux|WEB[-.]?DL|HDRip|HDR|HEVC|x264|x265|H\.?265|H\.?264|AVC|"
        r"DDP5\.1|Atmos|TrueHD|DTS[-.]?HD|MA5\.1|MA7\.1|REMUX|UHD|DV|DoVi)\b",
        " ", s, flags=re.I)
    s = re.sub(r"\b(AMZN|NF|NETFLIX|DSNP|HULU|STAN|Apple)\b", " ", s, flags=re.I)
    s = re.sub(r"[\.\(\)\[\]\-_]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def title_key(s: str) -> str:
    """归一化标题并去掉英文虚词，用于整词包含匹配。"""
    n = normalize(s)
    n = re.sub(r"\b(the|a|an|of|and|or)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def has_chinese_chars(s: str) -> bool:
    """判断字符串是否包含中日韩统一表意文字。"""
    return bool(CJK_RE.search(s))


def extract_year(stem: str) -> Optional[str]:
    """从文件名中提取四位年份。"""
    m = RE_YEAR.search(stem)
    return m.group(1) if m else None


def extract_season_episode(stem: str) -> Optional[Tuple[int, int]]:
    """从文件名中提取季号与集号，未命中返回 None。"""
    m = RE_TV_EPISODE.search(stem)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def chinese_title_match_score(name_a: str, name_b: str) -> int:
    """返回两个中文串之间最长公共子串的长度，连续不足 2 字不计分。"""
    sa = "".join(CJK_RE.findall(name_a))
    sb = "".join(CJK_RE.findall(name_b))
    if not sa or not sb:
        return 0
    best = 0
    n, m = len(sa), len(sb)
    for i in range(n):
        for j in range(m):
            k = 0
            while i + k < n and j + k < m and sa[i + k] == sb[j + k]:
                k += 1
            if k >= 2 and k > best:
                best = k
    return best


def cjk_only(s: str) -> str:
    """只保留中文片段，用于避免单字误命中。"""
    return "".join(CJK_RE.findall(s))


def load_alias_text(text: str) -> Dict[str, Set[str]]:
    """解析别名配置，返回「英文 token -> 中文标题集合」的映射。

    每行格式：英文别名|中文剧名A|中文剧名B
    """
    out: Dict[str, Set[str]] = defaultdict(set)
    if not text:
        return out
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) < 2:
            continue
        english, *chinese = parts
        for tok in re.split(r"\s+", english.lower()):
            if len(tok) >= 2:
                for cn in chinese:
                    out[tok].add(cn)
    return out


def collect_ancestor_keys(path: Path) -> List[str]:
    """收集字幕所在目录及其所有祖先目录名，作为标题线索。"""
    keys: List[str] = []
    p = path.parent
    while p != p.parent:
        if p.name not in CATEGORY_DIRS:
            k = title_key(p.name)
            if k:
                keys.append(k)
        p = p.parent
    return keys


def score_candidate(title: str, stem: str, ancestor_keys: List[str],
                    alias_hits: Set[str], stem_key: str) -> Tuple[int, int, bool]:
    """给候选标题打分，返回 (得分, 最长中文公共子串, 是否强匹配)。

    「强匹配」要求存在明确证据：中文 2 字以上命中、英文整词命中或别名命中。
    仅字面相近不足以作为匹配依据，避免字幕被复制到错误的剧集目录。
    """
    score = 0
    strong = False
    max_lcs = 0

    if has_chinese_chars(title):
        stem_lcs = chinese_title_match_score(title, stem)
        max_lcs = max(max_lcs, stem_lcs)
        if stem_lcs >= 2:
            strong = True
            score += 4
        for at in ancestor_keys:
            if len(CJK_RE.findall(at)) >= 3:
                cs = chinese_title_match_score(title, at)
                if cs >= 2:
                    strong = True
                max_lcs = max(max_lcs, cs)
                score += cs
        if alias_hits:
            t_cjk = cjk_only(title)
            if any(cjk_only(a) and cjk_only(a) in t_cjk for a in alias_hits):
                strong = True
                score += 8
    else:
        tnorm = title_key(title)
        tokens = [t for t in re.split(r"\s+", tnorm) if len(t) >= 3]
        if tnorm and tnorm in stem_key:
            strong = True
            score += 4
        for at in ancestor_keys + [stem_key]:
            hits = sum(1 for tok in tokens if tok and tok in at)
            if hits:
                strong = True
                score += 2 * hits
        if alias_hits and any(tok in tnorm for tok in alias_hits):
            strong = True
            score += 8

    return score, max_lcs, strong


def library_owner(strm: Path) -> Optional[Path]:
    """返回 strm 所属的「剧名 (年份)」目录；不符合结构则返回 None。

    这是区分「真正的媒体库」与「115 中转目录 / 下载目录」的关键：
    中转目录里的 strm 是平铺的，取不到「剧名 (年份)」这一层，直接判否。
    """
    d = strm.parent
    lib = d.parent if RE_SEASON_DIR.match(d.name) else d
    return lib if RE_LIB_DIR.match(lib.name) else None


def build_movie_entries(strm_files: List[Path]) -> List[MovieEntry]:
    """按目标目录中的 strm 列表构建电影候选条目。"""
    groups: Dict[Path, MovieEntry] = {}
    for strm in strm_files:
        d = strm.parent
        if d not in groups:
            m = RE_LIB_DIR.search(d.name)
            if not m:
                continue
            groups[d] = MovieEntry(title=m.group("title"), year=m.group("year"))
        groups[d].strm_files.append(strm)
    return list(groups.values())


def build_tv_entries(strm_files: List[Path]) -> List[TvEntry]:
    """按目标目录中的 strm 列表构建剧集候选条目，索引到季/集。"""
    groups: Dict[Path, TvEntry] = {}
    for strm in strm_files:
        se = RE_TV_EPISODE.search(strm.stem)
        if not se:
            continue
        d = strm.parent
        lib_dir = d.parent if RE_SEASON_DIR.match(d.name) else d
        if lib_dir not in groups:
            m = RE_LIB_DIR.search(lib_dir.name)
            if not m:
                continue
            groups[lib_dir] = TvEntry(title=m.group("title"), year=m.group("year"))
        groups[lib_dir].seasons.setdefault(int(se.group(1)), {})[int(se.group(2))] = strm
    return list(groups.values())


def plan_for_subtitle(sub: SubtitleFile, movie_entries: List[MovieEntry],
                      tv_entries: List[TvEntry],
                      alias_map: Dict[str, Set[str]]) -> Optional[Plan]:
    """为单条字幕在候选库中寻找目标 strm。"""
    stem = sub.stem
    stem_key = title_key(stem)
    year = extract_year(stem)
    ancestor_keys = collect_ancestor_keys(sub.path)

    alias_hits: Set[str] = set()
    for at in ancestor_keys + [stem_key]:
        for tok in at.split():
            if tok in alias_map:
                alias_hits.update(alias_map[tok])

    season_ep = extract_season_episode(stem)
    if season_ep:
        season, episode = season_ep
        candidates: List[Tuple[int, int, int, TvEntry]] = []
        for tv in tv_entries:
            # 季/集先做字典命中过滤，避免全库逐条打分
            if season not in tv.seasons or episode not in tv.seasons[season]:
                continue
            score, max_lcs, strong = score_candidate(
                tv.title, stem, ancestor_keys, alias_hits, stem_key)
            if not strong:
                continue
            if year and tv.year == year:
                score += 1
            candidates.append((score, max_lcs, len(tv.title), tv))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (-x[0], -x[1], -x[2]))
        score, max_lcs, _, tv = candidates[0]
        strm = tv.seasons[season][episode]
        dst = strm.parent / f"{strm.stem}.{sub.lang}{sub.path.suffix.lower()}"
        return Plan(src=sub.path, dst=dst,
                    reason=f"剧匹配(score={score}, lcs={max_lcs}): "
                           f"{tv.title} S{season:02d}E{episode:02d}")

    candidates = []
    for mv in movie_entries:
        # 年份两侧都已知且不一致时不可能是同一部片，直接跳过（全库扫描的性能关键）
        if year and mv.year and mv.year != year:
            continue
        score, max_lcs, strong = score_candidate(
            mv.title, stem, ancestor_keys, alias_hits, stem_key)
        if not strong:
            continue
        if year and mv.year == year:
            score += 1
        candidates.append((score, max_lcs, len(mv.title), mv))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    score, max_lcs, _, mv = candidates[0]
    strm = mv.strm_files[0]
    dst = strm.parent / f"{strm.stem}.{sub.lang}{sub.path.suffix.lower()}"
    return Plan(src=sub.path, dst=dst,
                reason=f"电影匹配(score={score}, lcs={max_lcs}): {mv.title} ({mv.year})")


def apply_plans(plans: List[Plan], overwrite: bool,
                delete_source: bool = False) -> Tuple[int, int, int]:
    """执行复制计划，返回 (成功, 跳过, 失败) 计数。

    开启 delete_source 时，仅在字幕**成功复制到媒体库之后**才删除下载目录
    里的源文件，从而腾出空间。复制失败或目标已存在而跳过时一律保留源文件，
    避免字幕丢失。
    """
    ok = skip = fail = 0
    removed = 0
    for p in plans:
        if p.dst.exists() and not overwrite:
            skip += 1
            continue
        try:
            p.dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p.src, p.dst)
            ok += 1
        except OSError as exc:
            logger.error(f"[字幕同步] 复制失败 {p.src} → {p.dst}: {exc}")
            fail += 1
            continue

        logger.info(f"[字幕同步] {p.src.name} → {p.dst.name}（{p.reason}）")
        if delete_source:
            try:
                p.src.unlink()
                removed += 1
                logger.info(f"[字幕同步] 已删除源文件: {p.src}")
            except OSError as exc:
                # 源文件删除失败不影响已完成的复制，只提示不计入失败
                logger.warning(f"[字幕同步] 源文件删除失败 {p.src}: {exc}")
    if removed:
        logger.info(f"[字幕同步] 已清理下载目录源文件 {removed} 个")
    return ok, skip, fail


class SubSync(_PluginBase):
    """外挂字幕同步：把下载目录的字幕按 strm 同名复制到媒体库。"""

    plugin_name = "外挂字幕同步"
    plugin_desc = "整理完成后把下载目录里的外挂字幕按 strm 同名复制到最终媒体库目录。"
    plugin_icon = "subtitle.png"
    plugin_version = "1.3.0"
    plugin_author = "MUZIYU368"
    plugin_label = "字幕"
    plugin_config_prefix = "subsync_"
    plugin_order = 50
    auth_level = 1

    _enabled = False
    _overwrite = False
    _delete_source = False
    _chinese_only = True
    _recent_window = 86400.0
    _download_movie = ""
    _download_tv = ""
    _library_movie = ""
    _library_tv = ""
    _backfill_cron = ""
    _alias = ""

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._overwrite = False
        self._delete_source = False
        self._chinese_only = True
        self._recent_window = 86400.0
        self._download_movie = ""
        self._download_tv = ""
        self._library_movie = ""
        self._library_tv = ""
        self._backfill_cron = ""
        self._alias = ""
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._overwrite = bool(config.get("overwrite"))
        # 默认关闭源文件清理：删除不可逆，需用户显式开启
        self._delete_source = bool(config.get("delete_source", False))
        self._chinese_only = bool(config.get("chinese_only", True))
        try:
            days = float(config.get("recent_window_days", 1) or 1)
        except (TypeError, ValueError):
            days = 1.0
        self._recent_window = max(days, 0.1) * 86400
        self._download_movie = str(config.get("download_movie") or "")
        self._download_tv = str(config.get("download_tv") or "")
        self._library_movie = str(config.get("library_movie") or "")
        self._library_tv = str(config.get("library_tv") or "")
        self._backfill_cron = str(config.get("backfill_cron") or "")
        self._alias = str(config.get("alias") or "")

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令，可直接在 MP 对话框输入 /subsync_scan_all 触发。"""
        return [
            {
                "cmd": "/subsync_scan_all",
                "event": EventType.PluginAction,
                "desc": "立即全库补扫字幕",
                "category": "字幕",
                "data": {"action": "subsync_scan_all"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时全库补扫服务，作为事件通道的兜底。"""
        if not self.get_state():
            return []
        cron = (self._backfill_cron or "").strip() or "30 4 * * *"
        try:
            trigger = CronTrigger.from_crontab(cron)
        except (ValueError, TypeError):
            logger.warning(f"[字幕同步] 补扫 cron 表达式无效: {cron}，回退到默认 30 4 * * *")
            trigger = CronTrigger.from_crontab("30 4 * * *")
        return [
            {
                "id": "SubSync.Backfill",
                "name": "外挂字幕同步 · 全库补扫",
                "trigger": trigger,
                "func": self._run_backfill,
                "kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {"model": "enabled", "label": "启用插件"},
                    },
                    {
                        "component": "VSwitch",
                        "props": {"model": "chinese_only", "label": "仅同步中文字幕"},
                    },
                    {
                        "component": "VSwitch",
                        "props": {"model": "overwrite", "label": "覆盖已存在的目标字幕"},
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "delete_source",
                            "label": "复制成功后删除下载目录的源字幕（释放空间）",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "download_movie",
                            "label": "下载目录（电影）",
                            "placeholder": "/media/downloads/电影",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "download_tv",
                            "label": "下载目录（剧集）",
                            "placeholder": "/media/downloads/电视剧",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "library_movie",
                            "label": "媒体库目录（电影）",
                            "placeholder": "/media/电影",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "library_tv",
                            "label": "媒体库目录（剧集）",
                            "placeholder": "/media/电视剧",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "backfill_cron",
                            "label": "全库补扫定时（crontab 格式，留空则每天 04:30）",
                            "placeholder": "30 4 * * *",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "recent_window_days",
                            "label": "事件通道的下载目录扫描时间窗口（天）",
                            "placeholder": "1",
                        },
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "alias",
                            "label": "别名映射（每行：英文别名|中文剧名A|中文剧名B）",
                            "placeholder": "from|梦魇绝镇|迷离小镇",
                            "rows": 4,
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "text": "字幕一定落在对应 strm 所在的目录，不需要在这里填分类规则。"
                                    "媒体库只需配置到分类根目录（如 /media/电影），插件会自动向下"
                                    "识别「剧名 (年份)」结构；115 中转目录因为没有这个结构会被自动跳过。",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "text": "「复制成功后删除源字幕」默认关闭。开启后字幕会被移动到媒体库，"
                                    "下载目录中的原文件不再保留，且无法自动恢复。"
                                    "仅在确认字幕已正确入库后再开启。",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "text": "所有路径都按 MoviePilot 容器内看到的路径填写。"
                                    "若宿主机是 /volume1/Media 而容器内挂载为 /media，"
                                    "这里应填 /media/... 。可执行 ls 确认。",
                        },
                    },
                ],
            }
        ], {
            "enabled": True,
            "chinese_only": True,
            "overwrite": False,
            "delete_source": False,
            "download_movie": "/media/downloads/电影",
            "download_tv": "/media/downloads/电视剧",
            "library_movie": "/media/电影",
            "library_tv": "/media/电视剧",
            "backfill_cron": "30 4 * * *",
            "recent_window_days": 1,
            "alias": "",
        }

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源（调度器由宿主在禁用时清理）。"""
        return None

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面；本插件不提供独立页面。"""
        return None

    # ---------- 路径工具 ----------

    def _valid_dirs(self, raws: Tuple[str, ...]) -> List[Path]:
        """把配置里的路径字符串解析为存在且去重后的目录列表。"""
        dirs: List[Path] = []
        for raw in raws:
            if not raw:
                continue
            p = Path(raw.strip())
            try:
                if p.exists() and p.is_dir() and p not in dirs:
                    dirs.append(p)
            except OSError:
                continue
        return dirs

    def _source_dirs(self) -> List[Path]:
        """下载目录：字幕的来源。"""
        return self._valid_dirs((self._download_movie, self._download_tv))

    def _library_dirs(self) -> List[Path]:
        """媒体库目录：strm 的所在范围。"""
        return self._valid_dirs((self._library_movie, self._library_tv))

    @staticmethod
    def _allowed_source(path: Path, roots: List[Path]) -> bool:
        """判断路径是否位于给定目录内。roots 为空时不做限制。"""
        if not roots:
            return True
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _collect_library_index(self) -> Tuple[List[MovieEntry], List[TvEntry], int]:
        """扫描媒体库建立全量索引，只收录位于「剧名 (年份)」目录下的 strm。"""
        strms: List[Path] = []
        for root in self._library_dirs():
            try:
                for p in root.rglob("*.strm"):
                    if library_owner(p) is not None:
                        strms.append(p)
            except OSError as exc:
                logger.warning(f"[字幕同步] 扫描媒体库失败 {root}: {exc}")
        return build_movie_entries(strms), build_tv_entries(strms), len(strms)

    # ---------- 两条处理通道 ----------

    def _match_and_apply(self, subs: List[SubtitleFile],
                         movie_entries: List[MovieEntry], tv_entries: List[TvEntry],
                         source_dirs: List[Path]) -> Tuple[int, int, int, int]:
        """对字幕做匹配与复制，返回 (成功, 跳过, 失败, 未匹配)。"""
        alias_map = load_alias_text(self._alias)
        plans: List[Plan] = []
        missed = 0
        for sub in subs:
            try:
                if not self._allowed_source(sub.path, source_dirs):
                    continue
                plan = plan_for_subtitle(sub, movie_entries, tv_entries, alias_map)
            except Exception as exc:  # 单条失败不能拖垮整批
                missed += 1
                logger.warning(f"[字幕同步] 处理字幕时出错 {sub.path}: {exc}")
                continue
            if plan:
                plans.append(plan)
            else:
                missed += 1
                logger.debug(f"[字幕同步] 未匹配到目标，跳过: {sub.path.name}")
        if not plans:
            return 0, 0, 0, missed
        ok, skip, fail = apply_plans(plans, self._overwrite, self._delete_source)
        return ok, skip, fail, missed

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event) -> None:
        """通道一：整理完成时，只处理本次整理涉及的目标目录。"""
        if not self._enabled:
            return
        data = event.event_data or {}
        transfer = data.get("transferinfo")
        if not transfer:
            return

        target = transfer.get("target_diritem")
        if not target or not target.get("path"):
            return
        target_root = Path(target["path"])
        try:
            if not target_root.exists():
                return
        except OSError:
            return

        try:
            strm_files = sorted(target_root.rglob("*.strm"))
        except OSError as exc:
            logger.warning(f"[字幕同步] 扫描目标目录失败 {target_root}: {exc}")
            return
        # 只认「剧名 (年份)」结构；115 中转目录等平铺 strm 会在这里被跳过
        strm_files = [s for s in strm_files if library_owner(s) is not None]
        if not strm_files:
            logger.debug(f"[字幕同步] {target_root} 下没有媒体库结构的 strm，本次跳过")
            return

        source_dirs = self._source_dirs()
        if not source_dirs:
            logger.warning("[字幕同步] 未找到有效的下载目录，请在插件设置里检查路径")
            return

        since = time.time() - self._recent_window
        subs: List[SubtitleFile] = []
        for d in source_dirs:
            subs.extend(collect_subtitles(d, self._chinese_only, since))
        if not subs:
            logger.debug(
                f"[字幕同步] 下载目录近 {self._recent_window / 86400:.1f} 天内没有新增字幕")
            return

        movie_entries = build_movie_entries(strm_files)
        tv_entries = build_tv_entries(strm_files)
        ok, skip, fail, missed = self._match_and_apply(subs, movie_entries, tv_entries, source_dirs)
        if missed:
            logger.info(f"[字幕同步] 共扫描 {len(subs)} 条字幕，{missed} 条未匹配")
        if ok or fail:
            logger.info(f"[字幕同步] 完成：成功 {ok}，跳过 {skip}，失败 {fail}")

    def scan_library(self) -> str:
        """通道二：全库补扫，把下载目录里所有字幕与整个媒体库索引匹配，补齐缺失项。"""
        source_dirs = self._source_dirs()
        if not source_dirs:
            return "全库补扫失败：未找到有效的下载目录，请检查插件设置里的两个下载目录"
        if not self._library_dirs():
            return "全库补扫失败：未找到有效的媒体库目录，请检查插件设置里的两个媒体库目录"

        movie_entries, tv_entries, strm_count = self._collect_library_index()
        if not movie_entries and not tv_entries:
            return "全库补扫失败：媒体库里没有识别到「剧名 (年份)」结构的条目"

        subs: List[SubtitleFile] = []
        for d in source_dirs:
            # 补扫不设时间窗口，历史积压一并处理
            subs.extend(collect_subtitles(d, self._chinese_only))
        if not subs:
            return f"全库补扫：媒体库索引 {strm_count} 个 strm，下载目录里没有字幕文件"

        t0 = time.time()
        ok, skip, fail, missed = self._match_and_apply(subs, movie_entries, tv_entries, source_dirs)
        cost = time.time() - t0
        msg = (f"全库补扫完成（{cost:.1f}s）：媒体库 {strm_count} 个 strm / "
               f"{len(movie_entries) + len(tv_entries)} 个条目，扫描字幕 {len(subs)} 条 → "
               f"成功 {ok}，已存在跳过 {skip}，失败 {fail}，未匹配 {missed}")
        logger.info(f"[字幕同步] {msg}")
        return msg

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event) -> None:
        """处理远程命令 /subsync_scan_all。"""
        data = event.event_data or {}
        if data.get("action") != "subsync_scan_all":
            return
        if not self._enabled:
            self.post_message("[字幕同步] 插件未启用，无法执行全库补扫")
            return
        try:
            self.post_message(self.scan_library())
        except Exception as exc:
            logger.error(f"[字幕同步] 全库补扫异常: {exc}")
            self.post_message(f"全库补扫异常: {exc}")

    def _run_backfill(self) -> None:
        """定时服务入口。"""
        try:
            self.scan_library()
        except Exception as exc:
            logger.error(f"[字幕同步] 定时全库补扫异常: {exc}")
