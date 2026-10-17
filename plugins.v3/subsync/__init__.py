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
- 匹配优先问 MoviePilot 自己的转移历史：它已经知道这部片子整理后叫什么，
  字幕按原始资源名查表即可落到最终位置，不用猜、也就不用填别名。
  查不到才退回中文 LCS / 英文整词的启发式匹配。
- 媒体库索引认两种目录写法（「剧名 (年份)」目录，或年份写在文件名里的剧名目录），
  但一律拒绝 115 中转 / 暂存目录下的 strm，不会把字幕错放到中间目录。
- 强匹配才动手：没有明确证据（转移历史 / 中文 2 字以上 / 英文整词 / 别名）就整条放弃。
- 幂等：目标已存在且未开启覆盖时跳过，重复扫描不会反复复制。
"""
from __future__ import annotations

import importlib
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
# 年份两侧：前面不能紧邻数字（避免把 10802018 这类长数字串切错），
# 后面要么是分隔符，要么年份就是整个名字的结尾（`三体.2023` / `三体2023` / `Avengers 2018`）。
# 原先强制要求「年份前后都得有分隔符」，以年份收尾的名字提取不到年份，
# 年份预过滤随之失效，同名不同年的片可能错配。
RE_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?:[\.\s\)\]]|$)")
RE_LIB_DIR = re.compile(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)\s*$")
# 目录名不带年份时，年份写在 strm 文件名里：复仇者联盟3：无限战争 (2018) - 2160p.strm
RE_LIB_PREFIX = re.compile(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)")
RE_SEASON_DIR = re.compile(r"^(?:Season|S)\s*\d+", re.I)

# 中转 / 暂存目录：这里的 strm 还没进最终媒体库，字幕不能落在这一层
STAGING_DIRS = {"115", "downloads", "download", "staging", "strm", "tmp", "temp"}


# 目录配置的分隔符：英文/中文逗号、分号、换行都算，多个目录可以写在一格里
DIR_SEP_RE = re.compile(r"[,，;；\n\r]+")

# 提取「作品名核心」时要剥掉的噪声：分辨率、来源、编码、音轨、压制组等。
# 字幕名和转移历史里的原始资源名都会带这些，剥掉之后才能对到同一部作品。
RE_TECH = re.compile(
    r"\b(?:"
    r"2160p|1080[pi]|720p|480p|4k|8k|uhd|hdr10|hdr|dolby\W?vision|dovi|sdr|"
    r"blu\W?ray|bdrip|bd\W?remux|bdremux|brrip|remux|"
    r"web\W?(?:dl|rip)|webrip|hdtv|pdtv|sdtv|dvdrip|dvd|hdrip|"
    r"hevc|x265|x264|h\.?265|h\.?264|avc|av1|mpeg[24]|vp9|"
    r"10bit|8bit|12bit|hi10p|hi444p?|"
    r"ddp?\+|ddp?[257]\s?[01]|dd[257]|ma[57]\s?[01]|"
    r"eac3|e\W?ac3|atmos|truehd|dts\W?(?:hd|x|ma|es)?|flac|lpcm|pcm|opus|aac\d?|ac3|"
    r"[2-8]\s?(?:ch|audio|channel|track)|multi\W?audio|"
    r"complete|repack|proper|uncut|remastered|restored|imax|"
    r"内嵌|字幕组|压制组|高清|中字|简体|繁体|简中|繁中|国语|粤语"
    r")\b",
    re.I,
)

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


@dataclass
class XferRecord:
    """一条 MoviePilot 转移历史记录，压缩成匹配所需的最小信息。"""

    src_full: str  # 原始资源名归一化后的形式，用于完全相同的情况
    src_core: str  # 原始资源名剥离噪声后的作品名核心
    tokens: Tuple[str, ...]  # src_core 按空格切分
    dest_stem: str  # 整理后落地的文件名（不含扩展名）
    title: str  # MP 识别出的标题（通常是中文名）
    year: str  # MP 识别出的年份
    mtype: str  # 电影 / 电视剧
    seasons: List[int] = field(default_factory=list)
    episodes: List[int] = field(default_factory=list)


def strip_lang_tag(stem: str) -> str:
    """去掉文件名里的语言标记，返回干净的基础名。

    **循环剥到剥不动为止**（v1.9.1 修）：原先命中第一条匹配就 return，只剥掉
    一个语言标签。字幕名带双标签（`.chs.zh` / `.chs&eng.zh`）时，剩下的 `.zh`
    前面没有分隔符，会直接粘到发布组名后面——

        From.S01E05...-TEPES.chs.zh  →  ...-TEPESzh  →  token 'tepeszh'

    而转移历史里的视频名是完整形式，切出 5-6 个 token。字幕的 3 个 token 去比，
    覆盖率恒定 0.4，过不了 `cov >= 0.8` 门槛：明明有完美对应的转移记录
    （hit=5 cov=1.00）却报未匹配，字幕留在下载目录。

    只在名字**末尾**剥，避免误伤标题里的字样（`The.Chinese.Garden`、
    `English.Teacher` 里的 Chi / Eng 不是语言标签）。

    `&` 连接的多语言尾巴（`.chs&eng.zh` / `.cht&zh`）当作**整段**剥：
    匹配点落在 & 之前的那个标签上，把 `&` 后面整条语言链一起吃掉。
    """
    name = str(stem or "")
    # 一条语言尾巴：`&` 可重复，每段是语言缩写（可带 -hans/-hant 变体）
    TAIL = r"(?:&[\s.\-_]*[A-Za-z]{2,4}(?:[-_](?:Hans|Hant|Trad|Simp))?)*"
    changed = True
    while changed:
        changed = False
        for rx, _ in LANG_TAGS:
            m = rx.search(name)
            if not m:
                continue
            tail = name[m.end():]
            # 命中位置之后必须只剩分隔符与语言片段，说明这是名字尾巴上的标签
            if tail and not re.fullmatch(TAIL, tail):
                continue
            # 把 & 后面整条语言链一起吃掉（`.chs&eng.zh` → 一个标签整段剥）
            chain = re.match(TAIL, tail)
            name = (name[: m.start()] + tail[chain.end():]).strip(".-_ &")
            changed = True
            break
    return name


def parse_lang_tag(stem: str) -> Tuple[str, bool]:
    """从文件名中剥离语言标记，返回 (语言标签, 是否中文)。"""
    matched = ""
    for rx, lang in LANG_TAGS:
        if rx.search(stem):
            matched = lang
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


def core_key(s: str) -> str:
    """提取作品名核心：剥掉年份、季集号、分辨率、编码、音轨等一切噪声。

    字幕名（`Avengers.Infinity.War.2018.2160p.WEB-DL.chs`）与转移历史里的
    原始资源名（`Avengers Infinity War 2018 UHD BluRay REMUX 2160p HEVC ...`）
    发行批次不同，逐字符对不上，但剥完之后都还原成 `avengers infinity war`。
    """
    n = RE_TECH.sub(" ", normalize(s))
    n = re.sub(r"\b(?:s\d{1,2}\s*e?\s*\d{1,3}|e\d{1,3}|ep\d{1,3})\b", " ", n)
    n = re.sub(r"\b(?:19|20)\d{2}\b", " ", n)
    n = re.sub(r"\b(?:the|a|an|of|and|or|to|in|on|at|vs|part)\b", " ", n)
    n = re.sub(r"[^0-9a-z㐀-鿿豈-﫿]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def loose_title(s: str) -> str:
    """只保留中文与数字的标题指纹，忽略标点差异（全角冒号、半角冒号、空格等）。"""
    return "".join(re.findall(r"[0-9㐀-鿿豈-﫿]", str(s or "")))


def title_eq(a: str, b: str) -> bool:
    """标题是否指向同一部作品：先精确比，再比中文+数字指纹。"""
    if not a or not b:
        return False
    if a.strip() == b.strip():
        return True
    la, lb = loose_title(a), loose_title(b)
    return bool(la) and la == lb


def stem_of(path_str: Any) -> str:
    """从任意路径字符串中取出不含扩展名的文件名，容忍分隔符与多层扩展名。"""
    s = str(path_str or "").strip().rstrip("/\\")
    if not s:
        return ""
    name = re.split(r"[\\/]", s)[-1]
    if not name.startswith("."):
        name = name.rsplit(".", 1)[0]
    return name.strip()


def int_list(value: Any) -> List[int]:
    """把 `S01` / `1,2` / `08-09` 这类字段解析成整数列表。"""
    out: List[int] = []
    for m in re.finditer(r"\d{1,3}", str(value or "")):
        n = int(m.group())
        if n not in out:
            out.append(n)
    return out


def split_paths(raw: Any) -> List[str]:
    """把一格里的多个路径拆开，支持英文/中文逗号、分号与换行。"""
    return [p.strip() for p in DIR_SEP_RE.split(str(raw or "")) if p.strip()]


def has_chinese_chars(s: str) -> bool:
    """判断字符串是否包含中日韩统一表意文字。"""
    return bool(CJK_RE.search(s))


def extract_year(stem: str) -> Optional[str]:
    """从文件名中提取四位年份，取最后一个匹配。

    取最后一个而非第一个，是因为片名本身就带年份的情况很常见
    （《1917》(2019)、《1984》(2003)、《2001》），第一个匹配到的是片名不是年份；
    而发行年份几乎总排在片名之后。取错年份会让年份预过滤把正主挡掉。
    """
    last: Optional[str] = None
    for m in RE_YEAR.finditer(str(stem or "")):
        last = m.group(1)
    return last


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


def is_staging(lib: Path) -> bool:
    """判断目录是否位于 115 中转 / 暂存区下面。

    这些目录里的 strm 还没进最终媒体库，字幕落在这里会跟着中转文件一起被
    折腾掉，所以必须在这里就挡住。
    """
    p = lib
    while p != p.parent:
        if p.name.lower() in STAGING_DIRS:
            return True
        p = p.parent
    return False


def entry_meta(strm: Path) -> Optional[Tuple[str, str, Path]]:
    """解析一个 strm 属于哪部作品，返回 (剧名, 年份, 所属目录)。

    两种目录写法都认：
      1) 目录名就是「剧名 (年份)」——`电影/外语电影/F1：狂飙飞车 (2025)/...`
      2) 目录名只有剧名、年份写在文件名里——
         `电影/外语电影/复仇者联盟3：无限战争/复仇者联盟3：无限战争 (2018) - 2160p.strm`
    两种结构在同一套媒体库里混着放，只认一种会漏掉整整一片电影。
    """
    d = strm.parent
    lib = d.parent if RE_SEASON_DIR.match(d.name) else d
    if is_staging(lib):
        return None
    m = RE_LIB_DIR.match(lib.name)
    if m:
        return m.group("title"), m.group("year"), lib
    m = RE_LIB_PREFIX.match(strm.stem)
    if m:
        return m.group("title"), m.group("year"), lib
    return None


def library_owner(strm: Path) -> Optional[Path]:
    """返回 strm 所属的作品目录；不是最终媒体库结构则返回 None。"""
    meta = entry_meta(strm)
    return meta[2] if meta else None


def build_movie_entries(strm_files: List[Path]) -> List[MovieEntry]:
    """按目标目录中的 strm 列表构建电影候选条目。

    带 Season/Sxx 季目录的作品属于剧集，必须排除。剧集 strm 的父目录是
    `剧名 (年份)`，目录名同样长得像电影，一并收进来的话，一条没带季集号的
    字幕会被当成电影字幕铺到每一集上（多版本落点逻辑会放大这个问题）。
    """
    groups: Dict[Path, MovieEntry] = {}
    for strm in strm_files:
        meta = entry_meta(strm)
        if not meta:
            continue
        title, year, lib = meta
        if RE_SEASON_DIR.match(strm.parent.name):
            continue  # Season/Sxx 目录 → 剧集，交给 build_tv_entries
        if lib not in groups:
            groups[lib] = MovieEntry(title=title, year=year)
        groups[lib].strm_files.append(strm)
    return list(groups.values())


def build_tv_entries(strm_files: List[Path]) -> List[TvEntry]:
    """按目标目录中的 strm 列表构建剧集候选条目，索引到季/集。"""
    groups: Dict[Path, TvEntry] = {}
    for strm in strm_files:
        se = RE_TV_EPISODE.search(strm.stem)
        if not se:
            continue
        meta = entry_meta(strm)
        if not meta:
            continue
        title, year, lib = meta
        if lib not in groups:
            groups[lib] = TvEntry(title=title, year=year)
        groups[lib].seasons.setdefault(int(se.group(1)), {})[int(se.group(2))] = strm
    return list(groups.values())


def _open_session() -> Tuple[Any, bool]:
    """借 MoviePilot 自己的会话工厂开一个只读会话。

    返回 (会话, 是否由本函数创建)。拿不到工厂就返回 (None, False)，
    由调用方退回「不传 db」的调用形态。
    """
    for mod_path in ("app.db.session", "app.db"):
        try:
            module = importlib.import_module(mod_path)
        except Exception:
            continue
        for name in ("ScopedSession", "SessionFactory"):
            factory = getattr(module, name, None)
            if callable(factory):
                try:
                    return factory(), True
                except Exception:
                    continue
    return None, False


def fetch_transfer_rows(limit: int = 20000) -> Optional[List[Any]]:
    """读取 MoviePilot 转移历史表。

    模型类与静态方法的形态在不同 MP 版本间有差异，这里把常见的几个导入位置
    逐个尝试并全量兜底；只要拿不到就返回 None，调用方自动退回启发式匹配，
    不会影响同步本身。

    **会话是必须的**（v1.9.0 修的就是这一条）：MP v3 把 `list_by_page` 从
    「静态方法 + `db_query` 装饰器自动注入会话」改成了「classmethod + 第一个
    位置参数必填 `db: Session`」。插件原先只用关键字参数调用、不传 db，
    每次都抛 TypeError，四种调用形态全试完都失败，于是返回 None，日志打
    「取不到转移历史表」——可用户 MP 里明明躺着上万条转移历史。这是个
    **静默降级**：字幕同步照常跑，只是全部退到启发式，英文字幕配中文库
    名时必然匹配失败，用户看到的是「明明有记录却找不到」。
    """
    model = None
    for mod_path, attr in (("app.db.models.transferhistory", "TransferHistory"),
                           ("app.db.models", "TransferHistory"),
                           ("app.db.oper.transferhistory", "TransferHistoryOper"),
                           ("app.db.models.oper.transferhistory", "TransferHistoryOper")):
        try:
            module = importlib.import_module(mod_path)
            model = getattr(module, attr, None) or getattr(module, "TransferHistory", None)
        except Exception:
            continue
        if model is not None:
            break
    if model is None:
        return None
    query = (getattr(model, "list_by_page", None)
             or getattr(model, "get_all", None)
             or getattr(model, "list_by_date", None))
    if not callable(query):
        return None

    session, owned = _open_session()
    called = False
    try:
        for kwargs in ({"page": 1, "count": limit, "status": True},
                       {"page": 1, "count": limit},
                       {"date": "1970-01-01 00:00:00"},
                       {}):
            try:
                # v3 的 db 是必填位置参数，不传就 TypeError；v2 的装饰器不需要它
                rows = query(db=session, **kwargs) if session is not None \
                    else query(**kwargs)
                called = True
            except TypeError:
                if session is not None:
                    try:
                        rows = query(**kwargs)
                        called = True
                    except TypeError:
                        continue
                    except Exception:
                        return None
                else:
                    continue
            except Exception:
                return None
            if rows:
                return list(rows)[:limit]
    finally:
        if owned and session is not None:
            try:
                session.close()
            except Exception as exc:
                logger.warning(f"[字幕同步] 关闭数据库会话失败（可忽略）: {exc}")
    # 一次都没调用成功 = 压根不知道 MP 这边长什么样，按「取不到」处理
    return [] if called else None



class TransferIndex:
    """转移历史对照表：原始资源名 → 整理后的作品。"""

    def __init__(self, rows: Optional[List[Any]] = None) -> None:
        self.records: List[XferRecord] = []
        self.by_full: Dict[str, List[int]] = defaultdict(list)
        self.by_token: Dict[str, List[int]] = defaultdict(list)
        self.freq: Dict[str, int] = defaultdict(int)
        if rows is None:
            return
        for row in rows:
            rec = self._to_record(row)
            if not rec:
                continue
            idx = len(self.records)
            self.records.append(rec)
            self.by_full[rec.src_full].append(idx)
            for tok in set(rec.tokens):
                self.by_token[tok].append(idx)
                self.freq[tok] += 1

    @staticmethod
    def _to_record(row: Any) -> Optional[XferRecord]:
        """把一行 ORM 记录压成 XferRecord；缺少可用信息就丢弃这条。"""
        src = getattr(row, "src", None)
        src_stem = stem_of(src)
        core = core_key(src_stem)
        if not core:
            return None
        return XferRecord(
            src_full=title_key(src_stem),
            src_core=core,
            tokens=tuple(core.split()),
            dest_stem=stem_of(getattr(row, "dest", None)),
            title=str(getattr(row, "title", "") or "").strip(),
            year=str(getattr(row, "year", "") or "").strip(),
            mtype=str(getattr(row, "type", "") or "").strip(),
            seasons=int_list(getattr(row, "seasons", "")),
            episodes=int_list(getattr(row, "episodes", "")),
        )

    def lookup(self, stem: str, year: Optional[str]) -> Optional[XferRecord]:
        """找出与字幕名最贴合的一条转移记录；证据不足时返回 None。"""
        if not self.records:
            return None
        # 先摘掉 .chs/.en 之类的语言尾巴，否则它会算进比对词里、把覆盖率稀释掉
        stem = strip_lang_tag(stem)

        # 完全同名：字幕和原始资源出自同一次发布，最强的证据
        full = title_key(stem)
        if full and full in self.by_full:
            rec = self.records[self.by_full[full][0]]
            if not (year and rec.year and rec.year != year):
                return rec

        core = core_key(stem)
        toks = [t for t in core.split() if len(t) > 1]
        if not toks:
            return None
        # 用最稀有的词缩小候选集，避免每条字幕都全表比对
        pool: Optional[List[int]] = None
        for tok in sorted(toks, key=lambda t: self.freq.get(t, 0)):
            idxs = self.by_token.get(tok)
            if idxs:
                pool = idxs
                break
        if not pool:
            return None

        # 整条字幕只剩一个词时（如 `Dune.srt` / `三体.srt`），只有这个词足够长、
        # 或者本身就是一个完整中文片名，才允许凭一词命中，否则宁可不放也不放错
        single_ok = (len(toks) == 1
                     and (len(toks[0]) >= 4 or len(CJK_RE.findall(toks[0])) >= 2))

        best: Optional[XferRecord] = None
        best_score = 0.0
        for i in pool:
            rec = self.records[i]
            if year and rec.year and rec.year != year:
                continue
            hit = sum(1 for t in toks if t in rec.tokens)
            cov = hit / len(toks)
            if hit < 2 and not (hit == 1 and single_ok):
                continue
            if cov < 0.8:
                continue
            score = cov + hit * 0.01
            # 前缀命中说明字幕名就是原始资源名的主体，比只是包含更可信
            if rec.src_core == core:
                score += 0.5
            elif rec.src_core.startswith(core + " "):
                score += 0.2
            if score > best_score:
                best_score = score
                best = rec
        return best


def build_strm_stem_index(strms: List[Path]) -> Dict[str, List[Path]]:
    """按文件名建立 strm 索引，用于把转移历史里的目标文件名直接对上媒体库文件。"""
    out: Dict[str, List[Path]] = defaultdict(list)
    for s in strms:
        out[s.stem.lower()].append(s)
    return dict(out)


def resolve_strm(rec: XferRecord, season: Optional[int], episode: Optional[int],
                 movie_entries: List[MovieEntry], tv_entries: List[TvEntry],
                 strm_by_stem: Dict[str, List[Path]]) -> Optional[Path]:
    """把一条转移记录落到媒体库里具体的 strm 文件；落不上去返回 None。

    字幕带 SxxExx 就说明这是某一集的料，只在剧集里按季集号精确定位——
    电影和剧集完全可能同名（《沙丘》），剧集那条对不上时宁可整条放弃，
    也不能退到电影里去，否则整季的字幕会一起塞进电影目录。
    """
    is_episode = season is not None and episode is not None

    # ① 整理后的文件名与媒体库 strm 同名（MP 整理出的名字与最终 strm 一致）
    #
    # 字幕自己带的 SxxExx 比转移历史的 dest 更硬：字幕名是发布方明确写的
    # 「哪一集」，而一条转移历史常常只记了这次转移里第一个落地的文件。
    # dest 命中的 strm 若与字幕的季集号不符（例如字幕 S01E02、dest 指向
    # S01E01），照抄就会把第 2 集的字幕写成第 1 集的名字——文件名合法、
    # Emby 不报错，只是播放时字幕对不上，属于最难排查的静默错误。
    # 因此这里只接受「集号一致」或「dest 本身不带集号信息」的命中。
    if rec.dest_stem:
        hit = strm_by_stem.get(rec.dest_stem.lower())
        if hit:
            candidate = hit[0]
            if is_episode:
                cand_se = extract_season_episode(candidate.stem)
                # dest 是季集文件但集号对不上 → 拒不接受，交给下面的②按季集精确找
                if cand_se and cand_se != (season, episode):
                    pass
                else:
                    return candidate
            else:
                return candidate

    # MP 的 title 偶尔是空的，这时 dest 所在目录名就是剧名，拿来顶上
    title_hint = rec.title or rec.dest_stem

    # ② 剧集：字幕的季集号 + 转移记录 + 媒体库三者对上
    if is_episode:
        for tv in tv_entries:
            if not title_eq(tv.title, title_hint):
                continue
            if rec.year and tv.year and rec.year != tv.year:
                continue
            if rec.seasons and season not in rec.seasons:
                continue
            strm = tv.seasons.get(season, {}).get(episode)
            if strm:
                return strm
        return None

    # ③ 电影：标题 + 年份对上
    for mv in movie_entries:
        if not title_eq(mv.title, title_hint):
            continue
        if rec.year and mv.year and rec.year != mv.year:
            continue
        if mv.strm_files:
            return mv.strm_files[0]
    return None


def plans_for_movie_entry(sub: SubtitleFile, mv: MovieEntry) -> List[Plan]:
    """为一条字幕在某个电影作品下生成全部版本的落点。

    同一目录里通常躺着多个清晰度的 strm（2160p / 1080p / HDR…），
    只挑第一个落会让其余版本的目录永远缺字幕，而 Emby 多版本正是常规用法。
    字幕与媒体文件同目录同名，各版本互不影响，可以放心逐个覆盖。
    """
    out: List[Plan] = []
    for strm in mv.strm_files:
        dst = strm.parent / f"{strm.stem}.{sub.lang}{sub.path.suffix.lower()}"
        out.append(Plan(src=sub.path, dst=dst,
                        reason=f"电影匹配: {mv.title} ({mv.year}) → {strm.name}"))
    return out


def plan_for_subtitle(sub: SubtitleFile, movie_entries: List[MovieEntry],
                      tv_entries: List[TvEntry],
                      alias_map: Dict[str, Set[str]],
                      xfer: Optional[TransferIndex] = None,
                      strm_by_stem: Optional[Dict[str, List[Path]]] = None,
                      multi_version: bool = True) -> Optional[List[Plan]]:
    """为单条字幕在候选库中寻找目标 strm，返回落点计划列表（空列表=未匹配）。

    匹配优先级：转移历史对照表（零猜测）→ 中文 LCS / 英文整词启发式 → 别名映射。

    返回列表而非单条计划，是为了支持电影多版本：同一部片子的每个清晰度
    都要拿到同名字幕。multi_version=False 时退化为只取第一个版本（老行为）。
    """
    stem = sub.stem
    stem_key = title_key(stem)
    year = extract_year(stem)
    ancestor_keys = collect_ancestor_keys(sub.path)
    season_ep = extract_season_episode(stem)
    season, episode = season_ep if season_ep else (None, None)

    # ① 转移历史对照表：MP 自己已经知道这部片子整理后叫什么，直接照抄，零猜测
    if xfer is not None and xfer.records:
        rec = xfer.lookup(stem, year)
        if rec:
            strm = resolve_strm(rec, season, episode, movie_entries, tv_entries,
                                strm_by_stem or {})
            if strm:
                dst = strm.parent / f"{strm.stem}.{sub.lang}{sub.path.suffix.lower()}"
                return [Plan(src=sub.path, dst=dst,
                             reason=f"转移历史匹配: {strm.name}")]

    # ② 启发式匹配
    alias_hits: Set[str] = set()
    for at in ancestor_keys + [stem_key]:
        for tok in at.split():
            if tok in alias_map:
                alias_hits.update(alias_map[tok])

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
        return [Plan(src=sub.path, dst=dst,
                     reason=f"剧匹配(score={score}, lcs={max_lcs}): "
                            f"{tv.title} S{season:02d}E{episode:02d}")]

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

    # 电影候选为空或都不够强时，回落到「不带季集号的剧集字幕」：
    # 这种字幕（`三体.chs.srt`）说的是整部剧，落第一集是唯一合理选择，
    # 宁可只覆盖一集，也不要把电影当剧集、或整条丢掉。
    if not candidates:
        for tv in tv_entries:
            score, max_lcs, strong = score_candidate(
                tv.title, stem, ancestor_keys, alias_hits, stem_key)
            if not strong:
                continue
            if year and tv.year and tv.year != year:
                continue
            season = sorted(tv.seasons)[0] if tv.seasons else None
            episode = sorted(tv.seasons.get(season, {}))[0] if season else None
            if season is None or episode is None:
                continue
            strm = tv.seasons[season][episode]
            dst = strm.parent / f"{strm.stem}.{sub.lang}{sub.path.suffix.lower()}"
            return [Plan(src=sub.path, dst=dst,
                         reason=f"剧匹配(无季集号，取首集): "
                                f"{tv.title} S{season:02d}E{episode:02d}")]
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    score, max_lcs, _, mv = candidates[0]
    if multi_version:
        plans = plans_for_movie_entry(sub, mv)
        if plans:
            plans[0].reason = (f"电影匹配(score={score}, lcs={max_lcs}): "
                               f"{mv.title} ({mv.year}) · {len(plans)} 个版本")
            return plans
        return None
    strm = mv.strm_files[0]
    dst = strm.parent / f"{strm.stem}.{sub.lang}{sub.path.suffix.lower()}"
    return [Plan(src=sub.path, dst=dst,
                 reason=f"电影匹配(score={score}, lcs={max_lcs}): {mv.title} ({mv.year})")]


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
    plugin_version = "1.9.1"
    plugin_author = "MUZIYU368"
    plugin_label = "字幕"
    plugin_config_prefix = "subsync_"
    plugin_order = 50
    auth_level = 1

    _enabled = False
    _overwrite = False
    _delete_source = False
    _chinese_only = True
    _use_transfer_history = True
    _multi_version = True
    _recent_window = 86400.0
    _download_movie = ""
    _download_tv = ""
    _library_movie = ""
    _library_tv = ""
    _backfill_cron = ""
    _alias = ""

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。

        run_now 是「立即运行一次」的一次性触发位：用户在配置页打开它并保存，
        本方法被宿主再次调用时消费掉这个信号并同步执行一次全库补扫，
        随后清回 False，避免下次任何配置变更都重复触发。
        """
        trigger_now = False
        if config:
            trigger_now = bool(config.pop("run_now", False))
        self.stop_service()
        self._enabled = False
        self._overwrite = False
        self._delete_source = False
        self._chinese_only = True
        self._use_transfer_history = True
        self._multi_version = True
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
        self._use_transfer_history = bool(config.get("use_transfer_history", True))
        # 多版本默认开：同目录的每个清晰度都该拿到字幕
        self._multi_version = bool(config.get("multi_version", True))
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

        if trigger_now:
            # 消费掉信号并立即执行；不阻塞后续配置生效
            self._consume_run_now()

    def _consume_run_now(self) -> None:
        """执行一次「立即运行」请求，并把触发位复位。

        复位通过 update_config 写回 False 完成，否则这个开关会一直为真，
        之后每次改任何配置项都会重跑一遍补扫。
        """
        try:
            cfg = dict(self.get_config() or {})
        except Exception:
            cfg = {}
        cfg["run_now"] = False
        try:
            self.update_config(cfg)
        except Exception as exc:
            logger.debug(f"[字幕同步] 复位 run_now 失败: {exc}")

        if not self._enabled:
            logger.info("[字幕同步] 收到立即运行请求，但插件未启用，已跳过")
            self._notify("插件未启用，无法执行全库补扫", level="warning")
            return
        try:
            msg = self.scan_library()
        except Exception as exc:
            logger.error(f"[字幕同步] 立即补扫异常: {exc}")
            self._notify(f"补扫异常: {exc}", level="error")
            return
        data = self.get_data("last_scan") or {}
        self._notify(msg, level="error" if data.get("error") else "success")

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
        """返回插件 API 列表。

        ⚠️ endpoint 是必填项：缺了它宿主不会把函数挂到路由上，
        请求 /plugin/SubSync/scan_now 会直接 404，按钮点了没反应。
        """
        return [
            {
                "path": "/scan_now",
                "endpoint": self.run_scan_now,
                "methods": ["GET", "POST"],
                "summary": "立即执行一次全库补扫",
                "description": "手动触发字幕全库补扫，同步执行并返回本次结果。",
                "tags": ["SubSync"],
            }
        ]

    def run_scan_now(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """立即补扫一次。供详情页按钮与 API 调用，逻辑与定时任务完全一致。

        同步执行并把结果返回，前端据此弹出提示；统计照常落盘，
        因此按完按钮详情页的「上次补扫」面板会同步刷新。
        """
        if not self._enabled:
            msg = "插件未启用，请先在设置里打开开关"
            self._notify(msg, level="warning")
            return {"success": False, "message": msg}

        try:
            msg = self.scan_library()
        except Exception as exc:  # 按钮点击不能把接口打崩
            logger.error(f"[字幕同步] 手动补扫异常: {exc}")
            self._notify(f"补扫异常: {exc}", level="error")
            return {"success": False, "message": f"补扫异常: {exc}"}

        stat = self.get_data("last_scan") or {}
        success = not stat.get("error")
        level = "success" if success else "error"
        self._notify(msg, level=level)
        return {"success": success, "message": msg, "stat": stat}

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

    @staticmethod
    def _form_group(title: str, items: List[Tuple[str, str, str]]) -> List[dict]:
        """生成一个分组：标题 + 两列排布的配置项。

        items 每项为 (model, label, placeholder)。
        """
        cols = [
            {
                "component": "VCol",
                "props": {"cols": 12, "sm": 6, "md": 6},
                "content": [{
                    "component": "VTextField",
                    "props": {"model": model, "label": label,
                              "placeholder": placeholder, "variant": "outlined",
                              "density": "comfortable", "hideDetails": True},
                }],
            }
            for model, label, placeholder in items
        ]
        return [
            {"component": "VDivider", "props": {}},
            {"component": "VAlert",
             "props": {"variant": "text", "density": "compact", "title": title}},
            {"component": "VRow", "content": cols},
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        form: List[dict] = [
            # ---- 开关：两列并排 ----
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 6},
                 "content": [{"component": "VSwitch",
                              "props": {"model": "enabled", "label": "启用插件",
                                        "color": "primary", "hideDetails": True}}]},
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 6},
                 "content": [{"component": "VSwitch",
                              "props": {"model": "chinese_only",
                                        "label": "仅同步中文字幕",
                                        "hideDetails": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 6},
                 "content": [{"component": "VSwitch",
                              "props": {"model": "overwrite",
                                        "label": "覆盖已存在的字幕",
                                        "hideDetails": True}}]},
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 6},
                 "content": [{"component": "VSwitch",
                              "props": {"model": "delete_source",
                                        "label": "复制后删除源字幕",
                                        "color": "error", "hideDetails": True}}]},
            ]},
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 6},
                 "content": [{"component": "VSwitch",
                              "props": {"model": "use_transfer_history",
                                        "label": "用转移历史自动匹配",
                                        "color": "primary",
                                        "hideDetails": True}}]},
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 6},
                 "content": [{"component": "VSwitch",
                              "props": {"model": "multi_version",
                                        "label": "电影多版本都放字幕",
                                        "color": "primary",
                                        "hideDetails": True}}]},
            ]},
            # ---- 立即运行一次：打开后保存即触发，跑完自动复位 ----
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "sm": 6, "md": 6},
                 "content": [{"component": "VSwitch",
                              "props": {"model": "run_now",
                                        "label": "立即运行一次",
                                        "color": "info",
                                        "hideDetails": True}}]},
            ]},
            {"component": "VAlert",
             "props": {"variant": "text", "density": "compact",
                       "text": "打开「立即运行一次」并保存 → 立刻全库补扫一遍并推送结果，"
                               "跑完自动复位，下次改配置不会重复执行。"}},
        ]

        # ---- 路径分组 ----
        form += self._form_group("目录路径（容器内路径，多个用英文逗号隔开）", [
            ("download_movie", "下载目录 · 电影", "/media/downloads/电影,/media/待上传"),
            ("download_tv", "下载目录 · 剧集", "/media/downloads/电视剧,/media/待上传"),
            ("library_movie", "媒体库目录 · 电影", "/media/电影"),
            ("library_tv", "媒体库目录 · 剧集", "/media/电视剧"),
        ])

        # ---- 调度分组 ----
        form += self._form_group("运行节奏", [
            ("backfill_cron", "全库补扫 · crontab", "30 4 * * *"),
            ("recent_window_days", "事件通道扫描窗口 · 天", "1"),
        ])

        # ---- 别名映射（兜底，可留空） ----
        form += [
            {"component": "VDivider", "props": {}},
            {"component": "VAlert",
             "props": {"variant": "text", "density": "compact",
                       "title": "别名映射（兜底，可留空）"}},
            {"component": "VTextarea",
             "props": {"model": "alias", "variant": "outlined", "rows": 3,
                       "placeholder": "from|梦魇绝镇",
                       "hint": "转移历史与自动匹配都对不上时才需要。每行：英文别名|中文剧名",
                       "persistentHint": True}},
        ]

        # ---- 唯一保留的警示：不可逆操作 ----
        form += [
            {"component": "VDivider", "props": {}},
            {"component": "VAlert",
             "props": {"variant": "tonal", "type": "warning", "density": "compact",
                       "text": "「复制后删除源字幕」开启后，源字幕会被移除且无法恢复。"
                               "确认字幕已正确入库再开。"}},
        ]

        return [{"component": "VForm", "content": form}], {
            "enabled": True,
            "chinese_only": True,
            "overwrite": False,
            "delete_source": False,
            "use_transfer_history": True,
            "multi_version": True,
            "run_now": False,
            "download_movie": "/media/downloads/电影,/media/待上传",
            "download_tv": "/media/downloads/电视剧,/media/待上传",
            "library_movie": "/media/电影",
            "library_tv": "/media/电视剧",
            "backfill_cron": "30 4 * * *",
            "recent_window_days": 1,
            "alias": "",
        }

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源（调度器由宿主在禁用时清理）。"""
        return None

    @staticmethod
    def _count_files(roots: List[Path], suffixes: Set[str],
                     cap: int = 20000) -> Tuple[int, bool]:
        """统计目录下指定后缀的文件数，达到上限提前停止。返回 (数量, 是否被截断)。"""
        total = 0
        capped = False
        for r in roots or []:
            try:
                for p in r.rglob("*"):
                    # 先比后缀再 stat，避免对海量文件做无谓的系统调用
                    if p.suffix.lower() not in suffixes:
                        continue
                    try:
                        if p.is_file():
                            total += 1
                    except OSError:
                        continue
                    if total >= cap:
                        capped = True
                        break
            except OSError:
                continue
            if capped:
                break
        return total, capped

    def _config_problems(self) -> List[str]:
        """逐项校验关键路径，返回问题列表；空列表表示配置正常。

        一格里的多个目录是「或」关系：只要有一个能用就算配好了。少写的那些
        不算错误，免得升级后默认带上的路径在你机器上不存在就平白飘红。
        """
        out: List[str] = []
        for raw, label in ((self._download_tv, "下载目录·剧集"),
                          (self._download_movie, "下载目录·电影"),
                          (self._library_tv, "媒体库·剧集"),
                          (self._library_movie, "媒体库·电影")):
            items = split_paths(raw)
            if not items:
                out.append(f"{label} 未填写")
                continue
            usable = 0
            bad: List[str] = []
            for item in items:
                try:
                    p = Path(item)
                    if p.exists() and p.is_dir():
                        usable += 1
                    else:
                        bad.append(item)
                except (OSError, ValueError):
                    bad.append(item)
            if not usable:
                out.append(f"{label} 全部无效 → {'、'.join(bad)}")
            elif bad:
                logger.debug(f"[字幕同步] {label} 中这些路径不存在，已忽略: {bad}")
        return out

    @staticmethod
    def _stat_card(title: str, value: str, color: str, hint: str = "") -> dict:
        """一张统计磁贴：大数字 + 小标题 + 一行补充说明。"""
        content: List[dict] = [
            {"component": "VCardSubtitle",
             "props": {"class": "pb-0 text-caption text-medium-emphasis"},
             "text": title},
            {"component": "VCardTitle",
             "props": {"class": "text-h5 pt-1 font-weight-bold"},
             "text": value},
        ]
        if hint:
            content.append({"component": "VCardSubtitle",
                            "props": {"class": "text-caption pt-1"},
                            "text": hint})
        return {
            "component": "VCol",
            "props": {"cols": 12, "sm": 6, "md": 3},
            "content": [{"component": "VCard",
                         "props": {"variant": "tonal", "color": color,
                                   "class": "pa-3 h-100"},
                         "content": content}],
        }

    @staticmethod
    def _info_row(label: str, value: str, muted: bool = False) -> dict:
        """运行信息里的一行「标签 : 值」。"""
        return {
            "component": "div",
            "props": {"class": "d-flex justify-space-between align-start ga-3 py-1"},
            "content": [
                {"component": "span",
                 "props": {"class": "text-caption text-medium-emphasis flex-shrink-0"},
                 "text": label},
                {"component": "span",
                 "props": {"class": "text-body-2 text-break"
                                   + (" text-medium-emphasis" if muted else "")},
                 "text": value},
            ],
        }

    @staticmethod
    def _panel(title: str, rows: List[dict], color: str = "primary") -> dict:
        """一张带小标题的描边面板。"""
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-3"},
            "content": [{
                "component": "div",
                "props": {"class": "d-flex flex-column px-4 py-3"},
                "content": [{
                    "component": "div",
                    "props": {"class": "text-subtitle-2 font-weight-bold mb-1"},
                    "text": title,
                }] + rows,
            }],
        }

    def get_page(self) -> Optional[List[dict]]:
        """插件详情页。

        结构：状态条 → 四张统计磁贴 → 配置自检 → 运行信息 → 上次补扫。
        一切正常时把自检结果收成状态条上的一枚小标签，不额外占地方；
        只有真出问题才会铺开逐条告警。
        """
        src = self._source_dirs()
        lib = self._library_dirs()
        try:
            last = self.get_data("last_scan") or {}
        except Exception:
            last = {}

        strm_count, _ = self._count_files(lib, {".strm"})
        sub_count, sub_capped = self._count_files(src, SUB_EXTS)
        problems = self._config_problems()
        has_last = bool(last.get("time"))
        last_failed = bool(last.get("error"))
        enabled = self._enabled

        # ---------- ① 状态条 ----------
        chips: List[dict] = [{
            "component": "VChip",
            "props": {"size": "x-small", "variant": "tonal", "color": "primary"},
            "text": f"v{self.plugin_version}",
        }]
        if not problems:
            chips.append({
                "component": "VChip",
                "props": {"size": "x-small", "variant": "tonal", "color": "success"},
                "text": "配置正常",
            })
        if has_last and last_failed:
            # 上次补扫是失败的，别让页面装作一切正常
            chips.append({
                "component": "VChip",
                "props": {"size": "x-small", "variant": "tonal", "color": "error"},
                "text": "上次补扫失败",
            })
        chips.append({
            "component": "VChip",
            "props": {"size": "x-small", "variant": "tonal",
                      "color": "success" if self._use_transfer_history else "warning"},
            "text": "转移历史匹配" if self._use_transfer_history else "启发式匹配",
        })
        chips.append({
            "component": "VChip",
            "props": {"size": "x-small", "variant": "tonal", "color": "teal"},
            "text": f"补扫 {self._backfill_cron or '30 4 * * *'}",
        })

        header = {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-3"},
            "content": [{
                "component": "div",
                "props": {"class": "d-flex flex-wrap align-center ga-2 px-4 py-3"},
                "content": [{
                    "component": "VChip",
                    "props": {"size": "small", "variant": "flat",
                              "color": "success" if enabled else "error",
                              "class": "font-weight-bold"},
                    "text": "运行中" if enabled else "已停用",
                }] + chips,
            }],
        }

        # ---------- ② 统计磁贴 ----------
        tile_row = {
            "component": "VRow",
            "props": {"class": "mb-3"},
            "content": [
                self._stat_card("媒体库 strm", str(strm_count), "primary",
                                "最终媒体库" if strm_count else "未找到"),
                self._stat_card("下载目录字幕",
                                f"{sub_count}+" if sub_capped else str(sub_count),
                                "teal",
                                "待同步" if sub_count else "没有待处理"),
                self._stat_card("上次补扫成功",
                                ("失败" if last_failed else str(last.get("ok", "—")))
                                if has_last else "—",
                                "error" if last_failed
                                else ("success" if has_last else "grey"),
                                last.get("time") or "还没跑过"),
                self._stat_card("上次未匹配",
                                str(last.get("missed", "—")) if has_last else "—",
                                "warning" if last.get("missed") else "grey",
                                "对不上的字幕" if has_last else "—"),
            ],
        }

        blocks: List[dict] = [header, tile_row]

        # ---------- ③ 配置自检：正常已收进状态条，有问题才展开 ----------
        if problems:
            rows = [{"component": "VChip",
                     "props": {"size": "x-small", "variant": "tonal", "color": "error"},
                     "text": p}
                    for p in problems]
            rows.insert(0, {"component": "div",
                            "props": {"class": "text-caption text-medium-emphasis mb-2"},
                            "text": f"共 {len(problems)} 处需要处理"})
            blocks.append(self._panel("配置自检", rows))

        # ---------- ④ 运行信息 ----------
        alias_lines = [ln for ln in self._alias.splitlines()
                       if ln.strip() and not ln.strip().startswith("#")]
        blocks.append(self._panel("运行信息", [
            self._info_row("下载目录", " · ".join(str(d) for d in src) or "未配置"),
            self._info_row("媒体库目录", " · ".join(str(d) for d in lib) or "未配置"),
            self._info_row("匹配方式",
                           "转移历史 → 启发式 → 别名" if self._use_transfer_history
                           else "启发式 → 别名"),
            self._info_row("别名映射",
                           f"{len(alias_lines)} 条" if alias_lines else "未使用（零配置）",
                           muted=not alias_lines),
        ]))

        # ---------- ⑤ 上次补扫：跑过才有 ----------
        if has_last:
            blocks.append(self._panel(f"上次补扫 · {last.get('time')}", [
                self._info_row("结果", last.get("msg") or "—"),
                self._info_row("明细",
                               f"耗时 {last.get('cost', '—')}s · 媒体库 {last.get('strm', '—')} 个 strm / "
                               f"{last.get('entries', '—')} 个条目 · 扫描字幕 {last.get('subs', '—')} 条 · "
                               f"成功 {last.get('ok', '—')} · 跳过 {last.get('skip', '—')} · "
                               f"失败 {last.get('fail', '—')} · 未匹配 {last.get('missed', '—')}"),
            ]))

        return blocks

    # ---------- 路径工具 ----------

    def _valid_dirs(self, raws: Tuple[str, ...]) -> List[Path]:
        """把配置里的路径字符串解析为存在且去重后的目录列表。

        每格可以写多个目录，用英文/中文逗号或分号隔开——字幕既可能留在下载目录，
        也可能被 MP 一起移进待上传目录，两个都填上就不用猜。
        """
        dirs: List[Path] = []
        for raw in raws:
            for item in split_paths(raw):
                p = Path(item)
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

    def _collect_library_index(self) -> Tuple[List[MovieEntry], List[TvEntry], int,
                                              Dict[str, List[Path]]]:
        """扫描媒体库建立全量索引，只收录属于最终媒体库的 strm。"""
        strms: List[Path] = []
        for root in self._library_dirs():
            try:
                for p in root.rglob("*.strm"):
                    if library_owner(p) is not None:
                        strms.append(p)
            except OSError as exc:
                logger.warning(f"[字幕同步] 扫描媒体库失败 {root}: {exc}")
        return (build_movie_entries(strms), build_tv_entries(strms), len(strms),
                build_strm_stem_index(strms))

    def _transfer_index(self) -> Optional[TransferIndex]:
        """构建转移历史对照表。读不到就返回空表，匹配自动退回启发式。"""
        if not self._use_transfer_history:
            return None
        try:
            rows = fetch_transfer_rows()
        except Exception as exc:
            logger.warning(f"[字幕同步] 读取转移历史失败，改用启发式匹配: {exc}")
            return None
        if rows is None:
            logger.warning("[字幕同步] 取不到转移历史表，本次改用启发式匹配")
            return None
        idx = TransferIndex(rows)
        if idx.records:
            logger.info(f"[字幕同步] 转移历史对照表已载入 {len(idx.records)} 条记录")
        else:
            logger.warning("[字幕同步] 转移历史里没有可用记录，本次改用启发式匹配")
        return idx

    # ---------- 两条处理通道 ----------

    def _match_and_apply(self, subs: List[SubtitleFile],
                         movie_entries: List[MovieEntry], tv_entries: List[TvEntry],
                         source_dirs: List[Path],
                         xfer: Optional[TransferIndex] = None,
                         strm_by_stem: Optional[Dict[str, List[Path]]] = None) \
            -> Tuple[int, int, int, int]:
        """对字幕做匹配与复制，返回 (成功, 跳过, 失败, 未匹配)。"""
        alias_map = load_alias_text(self._alias)
        plans: List[Plan] = []
        missed = 0
        for sub in subs:
            try:
                if not self._allowed_source(sub.path, source_dirs):
                    continue
                found = plan_for_subtitle(sub, movie_entries, tv_entries, alias_map,
                                          xfer, strm_by_stem, self._multi_version)
            except Exception as exc:  # 单条失败不能拖垮整批
                missed += 1
                logger.warning(f"[字幕同步] 处理字幕时出错 {sub.path}: {exc}")
                continue
            if found:
                plans.extend(found)
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
        # 只认最终媒体库；115 中转目录等暂存区的 strm 会在这里被跳过
        strm_files = [s for s in strm_files if library_owner(s) is not None]
        if not strm_files:
            logger.debug(f"[字幕同步] {target_root} 下没有最终媒体库的 strm，本次跳过")
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
        ok, skip, fail, missed = self._match_and_apply(
            subs, movie_entries, tv_entries, source_dirs,
            self._transfer_index(), build_strm_stem_index(strm_files))
        if missed:
            logger.info(f"[字幕同步] 共扫描 {len(subs)} 条字幕，{missed} 条未匹配")
        if ok or fail:
            logger.info(f"[字幕同步] 完成：成功 {ok}，跳过 {skip}，失败 {fail}")

    def scan_library(self) -> str:
        """通道二：全库补扫，把下载目录里所有字幕与整个媒体库索引匹配，补齐缺失项。

        每条返回路径（含各类失败）都落统计，否则详情页会一直停留在上一次
        成功的时间与数字上，让人以为补扫还在正常跑。
        """
        source_dirs = self._source_dirs()
        if not source_dirs:
            msg = ("全库补扫失败：未找到有效的下载目录，"
                   "请检查插件设置里的两个下载目录")
            self._save_scan_stat(time=time.strftime("%Y-%m-%d %H:%M:%S"),
                                  cost=0, strm=0, entries=0, subs=0,
                                  ok=0, skip=0, fail=0, missed=0, msg=msg,
                                  error=True)
            return msg
        if not self._library_dirs():
            msg = ("全库补扫失败：未找到有效的媒体库目录，"
                   "请检查插件设置里的两个媒体库目录")
            self._save_scan_stat(time=time.strftime("%Y-%m-%d %H:%M:%S"),
                                  cost=0, strm=0, entries=0, subs=0,
                                  ok=0, skip=0, fail=0, missed=0, msg=msg,
                                  error=True)
            return msg

        movie_entries, tv_entries, strm_count, strm_by_stem = self._collect_library_index()
        if not movie_entries and not tv_entries:
            msg = ("全库补扫失败：媒体库里没有识别到有效的条目"
                   "（目录名带年份，或年份写在文件名里）")
            self._save_scan_stat(time=time.strftime("%Y-%m-%d %H:%M:%S"),
                                  cost=0, strm=strm_count, entries=0, subs=0,
                                  ok=0, skip=0, fail=0, missed=0, msg=msg,
                                  error=True)
            return msg

        subs: List[SubtitleFile] = []
        for d in source_dirs:
            # 补扫不设时间窗口，历史积压一并处理
            subs.extend(collect_subtitles(d, self._chinese_only))
        if not subs:
            msg = f"全库补扫：媒体库索引 {strm_count} 个 strm，下载目录里没有字幕文件"
            self._save_scan_stat(time=time.strftime("%Y-%m-%d %H:%M:%S"),
                                  cost=0, strm=strm_count,
                                  entries=len(movie_entries) + len(tv_entries),
                                  subs=0, ok=0, skip=0, fail=0, missed=0, msg=msg)
            return msg

        t0 = time.time()
        ok, skip, fail, missed = self._match_and_apply(subs, movie_entries, tv_entries,
                                                       source_dirs, self._transfer_index(),
                                                       strm_by_stem)
        cost = time.time() - t0
        msg = (f"全库补扫完成（{cost:.1f}s）：媒体库 {strm_count} 个 strm / "
               f"{len(movie_entries) + len(tv_entries)} 个条目，扫描字幕 {len(subs)} 条 → "
               f"成功 {ok}，已存在跳过 {skip}，失败 {fail}，未匹配 {missed}")
        logger.info(f"[字幕同步] {msg}")
        self._save_scan_stat(
            time=time.strftime("%Y-%m-%d %H:%M:%S"),
            cost=round(cost, 1), strm=strm_count,
            entries=len(movie_entries) + len(tv_entries),
            subs=len(subs), ok=ok, skip=skip, fail=fail, missed=missed,
            msg=msg,
        )
        return msg

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event) -> None:
        """处理远程命令 /subsync_scan_all。"""
        data = event.event_data or {}
        if data.get("action") != "subsync_scan_all":
            return
        if not self._enabled:
            self._notify("插件未启用，无法执行全库补扫", level="warning")
            return
        try:
            self._notify(self.scan_library())
        except Exception as exc:
            logger.error(f"[字幕同步] 全库补扫异常: {exc}")
            self._notify(f"全库补扫异常: {exc}", level="error")

    def _run_backfill(self) -> None:
        """定时服务入口。"""
        try:
            msg = self.scan_library()
            data = self.get_data("last_scan") or {}
            # 定时任务不刷屏推送：有实际补齐才提示；失败必须提示，否则静默停摆没人知道
            if data.get("error") or data.get("ok"):
                self._notify(msg, level="error" if data.get("error") else "success")
        except Exception as exc:
            logger.error(f"[字幕同步] 定时全库补扫异常: {exc}")

    # ---------- 通知与统计持久化 ----------

    def _notify(self, text: str, title: str = "外挂字幕同步",
                level: str = "success") -> None:
        """推送系统消息。

        宿主 post_message 的签名在不同版本间有差异（有 keyword-only 与单参两种形态），
        这里逐个尝试并静默兜底，避免通知失败反过来影响补扫本身。
        """
        try:
            self.post_message(title=title, text=text)
            return
        except TypeError:
            pass
        except Exception as exc:
            logger.debug(f"[字幕同步] post_message 关键字调用失败: {exc}")
        try:
            self.post_message(text)
        except Exception as exc:
            logger.debug(f"[字幕同步] post_message 单参调用失败: {exc}")

    def _save_scan_stat(self, **kw: Any) -> None:
        """保存最近一次补扫的统计结果，供详情页展示。"""
        try:
            self.save_data("last_scan", kw)
        except Exception as exc:
            logger.debug(f"[字幕同步] 保存补扫统计失败: {exc}")
