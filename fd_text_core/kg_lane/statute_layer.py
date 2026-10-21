"""法规层确定性建图器（kg-lane-delivery-v1 任务 2.3，设计决策 D3）。

法规/版本节点与时效边（施行/废止/修正链）**零 LLM** 构建：施行日来自
`expiry` 字段（注意：该字段实为**施行日**而非失效日，普查坑 1）、gzk
`document_no` 公布括号、正文尾部附则句式；废止/修正链来自标题形态与附则
废止句式。解析正则与普查报告（`reports/kg-delivery-metadata-audit.md`
附录 B）**逐字一致**——普查口径是权威定义，本模块不得擅自偏离，否则
dry-run 数字与普查不可对照。

两条普查实测净回收的扩展正则（坑 3）：

1. 汉字数字年份「自一九八七年四月一日起施行」（含「二○一二」「二〇〇七」）；
2. 「自公布之日起施行」且 `publish` 缺失时，用正文头部「X年X月X日…通过」
   句式补（施行日≈通过日——人大通过即公布的法律两者同日，近似性写进
   报告已知局限，不静默）。

纪律（spec「法规层确定性建图」+ 任务书）：

- 元数据与标题交叉矛盾（两侧均为令式且数字冲突）→ 进待修报告，**不据
  矛盾元数据建边**（矛盾面实测 0 行，但逻辑保留并有 fixture 锁定）。
- gzk 源 status 一刀切「有效」、31% 正文含「废止」字样却标有效——废止态
  是 gzk 系统盲区（坑 2）：只建可确证的废止边（标题决定形/附则句式带
  《书名号》），盲区写进报告已知局限，**不强判**。
- 缺施行日/废止信号不明的行只建节点不建时效边，进残余清单（TSV 落档），
  不猜。
- 幂等：同输入构造两次，节点/边的内容与数量不变；写入用 MERGE-on-uuid
  upsert，可重入。

graphiti-core 0.31.1 读模型兼容（2026-10-22 修复，真缺陷）：图里的 uuid 一律
确定性 UUID5（带连字符），Entity/Episodic/RELATES_TO 都写 `created_at`
（ISO8601 带时区）。Episodic 必须补齐模型必填的 `source/content/valid_at/
entity_edges`——缺任一字段都会让 `get_episodes` 在 pydantic 校验时抛错，
provenance 解析随之降级；Entity 的 `created_at` 为 NULL 则 `get_by_group_ids`
每轮抛错（重建期实测 230s/ep）。Entity 的 `name_embedding` 可选，由 CLI
`--embed`（默认开）补齐，失败降级为 None 并在报告 `embedding` 分项计数。
**嵌入不是抽取**：`stats.llm_tokens` 仍恒为 0，两项分账。

图模型（与车道查询面 `query.py` 兼容：`:Entity` 节点 + `RELATES_TO` 边，
边带 `name`(语义关系)/`fact`/`episodes`/`valid_at`/`invalid_at`）：

- 节点：法规/版本节点（label `:Entity`，属性含 status/src_db/publish_at/
  effective_at/effective_source/version_year/doc_number/kind/in_corpus/law_id）。
  身份沿用 `merge.py` 的纪律：**文号内联在写图名里参与身份**（同名不同
  文号天然不合并）；merge_key 是判等函数，写图名走 canonical_name 路径。
- `EFFECTIVE` 自环边：单体的施行窗口没有天然的第二实体，而查询面的
  as_of 过滤只认边的 valid_at/invalid_at——自环是承载「施行→废止」双时态
  窗口的最小确定性形态（valid_at=施行日，invalid_at=可确证的废止日）。
- `REPEALS` 决定→被废止法；`AMENDS` 修改决定/修正案→宿主法；
  `VERSION_OF` （YYYY修正）版本节点→基础法。
- 附则句式提到、但库内回链不上的目标法：建 in_corpus=0 的衍生节点
  （不丢「谁废止谁」这条确证事实），回链优先级见 `_resolve_target`。

不改动 lane.py/query.py/merge.py（并行任务地盘）：身份函数直接 import
复用；`parse_uri` 从 lane.py 复制最小实现（import lane 会连带 llm 模块，
无谓的依赖面）。
"""
from __future__ import annotations

import asyncio
import re
import uuid as uuid_mod
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .merge import canonical_name, inline_docno, normalize, normalize_docno_issuer

__all__ = [
    "EffectiveInfo",
    "AmendInfo",
    "ParsedStatute",
    "StatuteGraphPlan",
    "StatuteGraphWriter",
    "attach_name_embeddings",
    "build_statute_graph",
    "degraded_embedding_report",
    "field_date",
    "cn2int",
    "resolve_effective",
    "parse_doc_numbers",
    "DocNumberInfo",
]

HEAD_CHARS = 600  # 正文头部窗口：通过日期句式在此解析
TAIL_CHARS = 2000  # 正文尾部窗口：与普查口径一致（right(content, 2000)）
EPISODE_CONTENT_CHARS = 2000  # Episodic.content 上限：标题 + 来源摘要 + 正文头部
EMBED_BATCH = 64  # 嵌入接口批大小（graphiti 读模型只要求 name_embedding 可空）

# 确定性 UUID5 命名空间（固定常量，绝不改）：换命名空间会让全部节点/边换
# uuid，等于对生产图做一次全量重建。输入约定见 _node_uuid/_edge_uuid/_ep_uuid。
KG_NAMESPACE = uuid_mod.UUID("4f6a1d2e-9c3b-5e7a-8d10-2b6c9f4a7e01")

KNOWN_STATUSES = {"有效", "已修改", "已废止", "失效", "已失效", "尚未生效"}

# 关系名（写在边的 name 属性——查询面按 r.name 过滤，type(r) 恒为 RELATES_TO）
REL_EFFECTIVE = "EFFECTIVE"
REL_REPEALS = "REPEALS"
REL_AMENDS = "AMENDS"
REL_VERSION_OF = "VERSION_OF"

# 施行日来源 → 报告/边 fact 里的人话标签
SOURCE_LABELS = {
    "expiry": "expiry字段(施行日)",
    "docno": "文号公布括号",
    "tail": "附则句式",
    "tail_cn_year": "附则句式(汉字年份)",
    "tail_shishi": "附则句式(实施形态)",
    "pub_derived": "公布日推导",
    "head_pass_derived": "通过日推导(扩展)",
}


# =====================================================================
# 一、日期与数字解析（普查附录 B 逐字搬运——口径权威定义）
# =====================================================================

# 字段日期：`YYYY-MM-DD HH:MM:SS` 文本 / `YYYY/M/D` / `YYYY年M月D日`
DATE_FIELD_RE = re.compile(
    r"^\s*(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})日?\s*(?:[ T]\d{1,2}:\d{2}:\d{2}(\.\d+)?)?\s*$"
)

CN_DIGITS = {"〇": 0, "零": 0, "○": 0, "一": 1, "二": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _valid_ymd(y: int, mo: int, d: int) -> bool:
    return 1949 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31


def field_date(s: str | None) -> str | None:
    """字段值 -> `YYYY-MM-DD`；脏值（如 id=68011 的 1899-12-31）返回 None。"""
    if not s or not s.strip():
        return None
    m = DATE_FIELD_RE.match(s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if _valid_ymd(y, mo, d):
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def cn2int(s: str) -> int | None:
    """汉字数字串 -> int（一九八七 -> 1987；二十一 -> 21；纯数字串直通）。"""
    if s.isdigit():
        return int(s)
    if all(c in CN_DIGITS for c in s):
        v = 0
        for c in s:
            v = v * 10 + CN_DIGITS[c]
        return v
    m = re.match(r"^([一二三四五六七八九]?)十([一二三四五六七八九]?)$", s)
    if m:
        return (CN_DIGITS.get(m.group(1), 1) if m.group(1) else 1) * 10 + (
            CN_DIGITS.get(m.group(2), 0) if m.group(2) else 0)
    return None


def _iso(y: int, mo: int, d: int) -> str:
    return f"{y:04d}-{mo:02d}-{d:02d}"


# --- 施行句式（普查附录 B 正则逐字一致） ------------------------------

# 「自2024年2月1日起施行」
RE_ARABIC = re.compile(r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起\s*施\s*行")
# 宽松变体：`2011年6月1日之日起施行` 类 typo、`…日起施行` 框架缺「自」
RE_LOOSE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[^\n。；]{0,15}?(?:起\s*)?施\s*行")
# 「…日实施」（施行/实施一字之差；普查只记形态，本模块同样解析出日期——
# 日期与 RE_LOOSE 同语法，确定性等价，来源单独记账可回算普查口径）
RE_SHISHI = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[^\n。；]{0,10}?(?:起\s*)?(?:起)?实\s*施")
# 「自公布之日起施行」及变体（公布/发布/印刷/印发/下达/通过）
RE_PUB = re.compile(r"自\s*(?:公布|发布|印刷|印发|下达|通过)\s*(?:之\s*日(?:起)?)?\s*施\s*行")
# 汉字数字年份「自一九八七年四月一日起施行」（含「二○一二」「二〇〇七」）
RE_CN_YEAR = re.compile(
    r"自\s*([〇零一二三四五六七八九○0]{4})\s*年\s*([0-9〇零一二三四五六七八九十○]{1,3})\s*月"
    r"\s*([0-9〇零一二三四五六七八九十○]{1,3})\s*日\s*起\s*施\s*行"
)

# --- 扩展正则（坑 3：普查实测净回收的两条） --------------------------

# 「自公布之日起施行」但 publish 缺失时，正文头部「1995年3月18日第八届
# 全国人民代表大会第三次会议通过」——通过日≈施行日（人大通过即公布）。
RE_HEAD_PASS = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[^\n。；]{0,40}?(?:通过|批准)")

# --- 废止句式（普查口径 + 一条显式扩展） ------------------------------

# 普查口径：废止…《X》（gap ≤120 非句读字符，覆盖「予以废止：\n《A》《B》」清单形）
RE_REPEAL_BOOK = re.compile(r"(?:同时\s*)?废\s*止\s*[^。；\n]{0,120}?《([^》]{2,60})》")
# 扩展：《X》…（予以）同时废止（书名号在「废止」**之前**的形态，普查
# §6 样本 id=66582 实见「…《…规定（试行）》同时废止」；单独记账，不与
# 普查 666 口径混算）
RE_REPEAL_BOOK_PRE = re.compile(r"《([^》]{2,60})》[^\n。；]{0,20}?(?:予以\s*)?(?:同时\s*)?废\s*止")

# --- 修正链标题形态 ----------------------------------------------------

RE_VERSION_PAREN = re.compile(r"[（(]\s*(\d{4})\s*年?\s*(?:修正|修订|修改)[^）)]{0,4}[）)]")

# --- 文号（令式 / 发文字号）---------------------------------------------

# 令式：「国家发展改革委令第6号」「山西省人民政府令2024年第1号」「令〔2020〕第3号」
# 可选前导日期组（"(2023年12月27日国家发展改革委令第6号公布" 形）：显式吃掉日期，
# 否则贪心前缀会把日期尾部的「日」卷进机关名（日照市…令这类含「日」机关名
# 不能靠排除字符解决，只能靠日期组先行匹配）。
RE_ORDER_NO = re.compile(
    r"(?:(\d{4})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)?\s*"
    r"([\u4e00-\u9fa5]{2,20}令)\s*(?:(\d{4})\s*年\s*)?(?:〔\s*(\d{4})\s*〕\s*)?"
    r"第\s*([0-9〇零一二三四五六七八九十百]+)\s*号"
)
# 发文字号：「财会〔2001〕1053号」
RE_ISSUE_NO = re.compile(
    r"([\u4e00-\u9fa5A-Za-z]{1,12})\s*([〔[]\s*\d{4}\s*[〕\]]\s*第?\s*[0-9一二三四五六七八九十百零〇]+\s*号)"
)
# 标题里出现任意「第N号」（普查的 anchor 判据）
RE_ANY_NO = re.compile(r"第\s*[0-9〇零一二三四五六七八九十百]+\s*号")

_BOOK_TITLE_ALL = re.compile(r"[《〈]([^》〉]{2,60})[》〉]")


# =====================================================================
# 二、单行解析（纯函数）
# =====================================================================

@dataclass(frozen=True)
class EffectiveInfo:
    """施行日解析结果。date=None 且 source=None 表示完全无信号。"""

    date: str | None
    source: str | None

    @property
    def known_form(self) -> bool:
        """普查口径「施行边可建」：形态可得（含无绝对日期的实施形态）。"""
        return self.source is not None


def resolve_effective(*, expiry: str | None = None, publish: str | None = None,
                      docno: str | None = None, tail: str = "", head: str = "") -> EffectiveInfo:
    """施行日解析链（优先级 = 普查 §2 综合口径，逐级兜底）。

    expiry 字段 → document_no 施行句式 → 尾部阿拉伯句式(strict/loose) →
    汉字数字年份 → 「…日实施」→ 「自公布之日起施行」+publish →
    【扩展】publish 缺失时头部「通过日期」补。
    """
    d = field_date(expiry)
    if d:
        return EffectiveInfo(d, "expiry")
    m = RE_ARABIC.search(docno or "")
    if m:
        y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid_ymd(y, mo, dd):
            return EffectiveInfo(_iso(y, mo, dd), "docno")
    m = RE_ARABIC.search(tail) or RE_LOOSE.search(tail)
    if m:
        y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid_ymd(y, mo, dd):
            return EffectiveInfo(_iso(y, mo, dd), "tail")
    m = RE_CN_YEAR.search(tail)
    if m:
        # 普查行为：命中汉字年份形态后不再向下兜底（elif 链）；转换失败
        # 视为形态可得但日期不可解（不猜日期）。
        y, mo, dd = cn2int(m.group(1)), cn2int(m.group(2)), cn2int(m.group(3))
        if y and mo and dd and _valid_ymd(y, mo, dd):
            return EffectiveInfo(_iso(y, mo, dd), "tail_cn_year")
        return EffectiveInfo(None, "tail_cn_year")
    m = RE_SHISHI.search(tail)
    if m:
        y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid_ymd(y, mo, dd):
            return EffectiveInfo(_iso(y, mo, dd), "tail_shishi")
        return EffectiveInfo(None, "tail_shishi")
    if RE_PUB.search(tail):
        pd = field_date(publish)
        if pd:
            return EffectiveInfo(pd, "pub_derived")
        # 扩展：publish 缺失 → 头部通过日期（坑 3 第二条，~581 行可回收）
        m = RE_HEAD_PASS.search(head)
        if m:
            y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if _valid_ymd(y, mo, dd):
                return EffectiveInfo(_iso(y, mo, dd), "head_pass_derived")
    return EffectiveInfo(None, None)


@dataclass(frozen=True)
class DocNumberInfo:
    """文号解析：title 侧与 metadata 侧各自提取，矛盾判定在组装时做。"""

    title_no: str | None  # 标题令式文号（规范形：机关令第N号）
    title_no_num: int | None
    meta_no: str | None  # 元数据令式文号
    meta_no_num: int | None
    meta_issue_no: str | None  # 元数据发文字号（〔YYYY〕N号 形）
    title_has_any_no: bool  # 标题含任意「第N号」（anchor 判据）


def _order_token(m: re.Match) -> str:
    """令式匹配 -> 规范身份 token：机关令[YYYY年]第N号（年份归一到「年」形）。

    机关名经 normalize_docno_issuer 去重复前缀——源数据实测有
    「财政部中华人民共和国财政部令第71号」这类重复机关名拼写。
    """
    year = m.group(3) or m.group(4)
    return normalize_docno_issuer(f"{m.group(2)}{year + '年' if year else ''}第{m.group(5)}号")


def parse_doc_numbers(title: str, docno: str | None) -> DocNumberInfo:
    """从标题与 document_no 提取文号。

    矛盾判据沿用 gov parser 纪律（普查 §4.2）：**两侧均为令式且数字冲突
    才判矛盾、以标题为准**——只比较数字，避免「令第12号」vs「第12号令」
    的写法差异被误判。
    """
    t_no, t_num = None, None
    m = RE_ORDER_NO.search(title or "")
    if m:
        t_no, t_num = _order_token(m), cn2int(m.group(5))
    m_no, m_num = None, None
    m = RE_ORDER_NO.search(docno or "")
    if m:
        m_no, m_num = _order_token(m), cn2int(m.group(5))
    i_no = None
    m = RE_ISSUE_NO.search(docno or "")
    if m:
        i_no = normalize(m.group(1) + re.sub(r"\s+", "", m.group(2)))
    return DocNumberInfo(
        title_no=t_no, title_no_num=t_num, meta_no=m_no, meta_no_num=m_num,
        meta_issue_no=i_no, title_has_any_no=bool(RE_ANY_NO.search(title or "")),
    )


@dataclass(frozen=True)
class AmendInfo:
    """修正链信号（普查 §7 三形态）。"""

    kind: str | None  # modify_decision | amendment_bill | version_paren | None
    targets: tuple[str, ...] = ()  # 《》目标（modify_decision）或宿主法（amendment_bill）
    version_year: int | None = None  # version_paren
    base_name: str | None = None  # version_paren 去括号后的基础法名


def parse_amendment(title: str) -> AmendInfo:
    t = title or ""
    if "关于修改" in t and "的决定" in t:
        targets = tuple(dict.fromkeys(_BOOK_TITLE_ALL.findall(t)))
        return AmendInfo("modify_decision", targets)
    if "修正案" in t or "修订案" in t:
        idx = min((t.find(k) for k in ("修正案", "修订案") if k in t))
        host = t[:idx].strip()
        if host:
            return AmendInfo("amendment_bill", (host,))
        return AmendInfo("amendment_bill")
    m = RE_VERSION_PAREN.search(t)
    if m:
        base = (t[: m.start()] + t[m.end():]).strip()
        if base:
            return AmendInfo("version_paren", version_year=int(m.group(1)), base_name=base)
    return AmendInfo(None)


def parse_repeal(title: str, tail: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """废止目标解析。

    返回 (targets, source_tags)。source_tags 记录命中的来源形态：
    ``repeal_title``（标题「关于废止…的决定」，普查 491 口径）、
    ``repeal_tail``（尾部「废止…《X》」，普查 666 口径）、
    ``repeal_tail_pre``（扩展：《X》…同时废止）。
    只有命中 source_tags 且提出目标时才建 REPEALS 边；gzk 盲区（正文含
    「废止」字样但无《》目标）**不建边、不强判**。
    """
    tags: list[str] = []
    targets: list[str] = []
    if "关于废止" in (title or "") and "的决定" in (title or ""):
        tags.append("repeal_title")
        targets += _BOOK_TITLE_ALL.findall(title)
    found = RE_REPEAL_BOOK.findall(tail or "")
    if found:
        tags.append("repeal_tail")
        targets += found
    found = RE_REPEAL_BOOK_PRE.findall(tail or "")
    if found:
        tags.append("repeal_tail_pre")
        targets += found
    # 去重保序；剔除自指。自指含两类：决定尾部清单重复指向决定自己；同名继受
    # （新法废止同名旧法，500 样本实测 id=20913 形态）——同名两版无法用现有
    # 身份键区分，建自环废止边会误导时点查询，保守丢弃（不猜）。
    seen: dict[str, None] = {}
    for name in targets:
        key = canonical_name(name)
        if key and key != canonical_name(title or "") and key not in seen:
            seen[key] = None
    return tuple(seen), tuple(tags)


@dataclass
class ParsedStatute:
    """一行 law_db 记录的完整解析产物（builder 的输入单元）。"""

    law_id: int
    src_db: str
    title: str
    type_: str
    status: str
    publish: str | None
    eff: EffectiveInfo
    docno: DocNumberInfo
    docno_contradiction: bool
    identity_docno: str | None  # 参与身份的文号（矛盾时取标题侧）
    amend: AmendInfo
    repeal_targets: tuple[str, ...]
    repeal_tags: tuple[str, ...]
    base_name: str  # canonical_name(title)：去书名号/条号的规范基础名
    node_name: str  # 写图名（文号内联参与身份）
    decision_class: bool  # 决定类文书（type 含「决定」）
    anchor: bool  # 普查 anchor：publish 可解 ∪ docno 非空 ∪ 标题含第N号
    content_excerpt: str  # Episodic.content 的正文部分（头部窗口）


def parse_law_record(record: Mapping[str, Any]) -> ParsedStatute:
    """DB 行 -> 解析产物。record 字段：id/title/src_db/status/type/publish/
    expiry/content（或预切片 head_text/tail_text）/document_no。"""
    content = record.get("content") or ""
    tail = record.get("tail_text") if "tail_text" in record else content[-TAIL_CHARS:]
    head = record.get("head_text") if "head_text" in record else content[:HEAD_CHARS]
    title = record.get("title") or ""
    docno_str = record.get("document_no") or None
    publish_raw = record.get("publish") or None

    # 矛盾判定：两侧均令式且数字冲突（gov parser 纪律）
    dn = parse_doc_numbers(title, docno_str)
    contradiction = bool(
        dn.title_no_num is not None and dn.meta_no_num is not None
        and dn.title_no_num != dn.meta_no_num
    )
    if contradiction:
        identity = dn.title_no  # 以标题为准；矛盾行进待修清单，不据元数据建边
    else:
        identity = dn.title_no or dn.meta_no or dn.meta_issue_no

    # 矛盾时 docno 整体不可信：施行日解析也不吃 document_no（保守面）
    eff = resolve_effective(
        expiry=record.get("expiry") or None, publish=publish_raw,
        docno=None if contradiction else docno_str, tail=tail or "", head=head or "",
    )

    base = canonical_name(title)
    # 文号内联参与身份，且必须经 inline_docno 挂成唯一规范形态「名（文号）」——
    # 散文抽取侧（EntityMerger）同走此形态，跨层同名才能确定性合并。
    # 标题已带文号时 canonical_name 已重挂，不重复。
    name = base
    if identity and not dn.title_has_any_no:
        name = inline_docno(base, identity)

    amend = parse_amendment(title)
    repeal_targets, repeal_tags = parse_repeal(title, tail or "")

    type_ = record.get("type") or ""
    anchor = bool(
        field_date(publish_raw) or (docno_str or "").strip() or dn.title_has_any_no
    )
    return ParsedStatute(
        law_id=int(record.get("id") or 0),
        src_db=record.get("src_db") or "",
        title=title,
        type_=type_,
        status=record.get("status") or "",
        publish=field_date(publish_raw),
        eff=eff,
        docno=dn,
        docno_contradiction=contradiction,
        identity_docno=identity,
        amend=amend,
        repeal_targets=repeal_targets,
        repeal_tags=repeal_tags,
        base_name=base,
        node_name=name,
        decision_class="决定" in type_,
        anchor=anchor,
        content_excerpt=head or content[:HEAD_CHARS],
    )


# =====================================================================
# 三、图构造
# =====================================================================

# --- 图属性格式（graphiti-core 0.31.1 读模型的硬要求） ------------------
#
# uuid：标准 UUID 字符串（带连字符），UUID5 确定性生成——幂等重跑同 uuid。
# created_at：ISO8601 带时区；Entity/Episodic/RELATES_TO 全部必填（读模型
# 里 EntityNode.created_at 是 datetime、EpisodicNode 的 from_record 对
# created_at/valid_at 显式 raise，EntityEdge.created_at 也必填）。
# 空日期一律写 None（= Cypher null，属性缺失）：graphiti 的 parse_db_date
# 对空字符串会直接抛 ValueError，不能写 ""。

def _utc_iso(dt: datetime) -> str:
    """datetime -> ISO8601 带时区（naive 视为 UTC）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _day_iso(day: str) -> str:
    """`YYYY-MM-DD` -> 当日零点 ISO8601（带时区）。"""
    return f"{day}T00:00:00+00:00"


def _node_uuid(group_id: str, name: str) -> str:
    return str(uuid_mod.uuid5(KG_NAMESPACE, f"{group_id}|node|{name}"))


def _ep_uuid(group_id: str, name: str) -> str:
    return str(uuid_mod.uuid5(KG_NAMESPACE, f"{group_id}|ep|{name}"))


def _edge_uuid(group_id: str, src_uuid: str, rel: str, dst_uuid: str) -> str:
    return str(uuid_mod.uuid5(
        KG_NAMESPACE, f"{group_id}|edge|{src_uuid}|{rel}|{dst_uuid}"))


def _episode_valid_at(parsed: ParsedStatute, created_at: str) -> str:
    """Episodic.valid_at：施行日或发布日；两者皆缺时退化为构建时刻。

    读模型要求 valid_at 非空（from_record 对 None 显式 raise），而法规行
    可能两个日期都缺（残余行）——此时用 created_at 是唯一不猜语义日期的
    确定性兜底，行号进 `residual`、总数进 `stats.graph.episodes_valid_at_fallback`，
    不静默。
    """
    d = parsed.eff.date or parsed.publish
    return _day_iso(d) if d else created_at


def _episode_content(parsed: ParsedStatute) -> str:
    """Episodic.content：标题 + 来源摘要 + 正文头部（有界，不落全文）。"""
    parts = [parsed.title,
             f"来源：law_db.laws#{parsed.law_id}（{parsed.src_db or 'unknown'}源）"]
    excerpt = (parsed.content_excerpt or "").strip()
    if excerpt:
        parts.append(excerpt)
    return "\n".join(parts)[:EPISODE_CONTENT_CHARS]


@dataclass
class StatuteNode:
    uuid: str
    name: str
    group_id: str
    summary: str
    labels: list[str]
    props: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""  # ISO8601 带时区（graphiti EntityNode.created_at 必填）
    name_embedding: list[float] | None = None  # graphiti EntityNode 可空字段

    def row(self) -> dict[str, Any]:
        return {
            "uuid": self.uuid, "name": self.name, "group_id": self.group_id,
            "summary": self.summary, "labels": self.labels,
            "created_at": self.created_at, "name_embedding": self.name_embedding,
            **self.props,
        }


@dataclass
class StatuteEdge:
    uuid: str
    src: str
    dst: str
    name: str  # 语义关系（EFFECTIVE/REPEALS/AMENDS/VERSION_OF）
    fact: str
    group_id: str
    episodes: list[str]
    valid_at: str = ""
    invalid_at: str = ""
    expired_at: str = ""
    reference_time: str = ""
    created_at: str = ""  # ISO8601 带时区（graphiti Edge.created_at 必填）

    def row(self) -> dict[str, Any]:
        # 空日期 -> None：graphiti 的 parse_db_date 不接受 ""（会抛 ValueError）。
        def _dt(value: str) -> str | None:
            return value or None

        return {
            "uuid": self.uuid, "src": self.src, "dst": self.dst, "name": self.name,
            "fact": self.fact, "group_id": self.group_id, "episodes": self.episodes,
            "valid_at": _dt(self.valid_at), "invalid_at": _dt(self.invalid_at),
            "expired_at": _dt(self.expired_at), "reference_time": _dt(self.reference_time),
            "created_at": self.created_at,
        }


@dataclass
class StatuteGraphPlan:
    """确定性建图计划：dry-run 出统计，write 模式经 bolt upsert 落图。"""

    group_id: str
    nodes: dict[str, StatuteNode]
    edges: dict[str, StatuteEdge]
    episodes: dict[str, dict[str, Any]]  # episode uuid -> props（name/source_description/created_at）
    provenance: dict[str, dict[str, Any]]  # episode name -> provenance 三元组（挂载层 corpus_index 用）
    residual: list[dict[str, Any]]
    contradictions: list[dict[str, Any]]
    stats: dict[str, Any]

    def snapshot(self) -> dict[str, Any]:
        """幂等自检用确定性快照（同输入两次构造必须逐字节相等）。"""
        return {
            "nodes": {u: n.row() for u, n in sorted(self.nodes.items())},
            "edges": {u: e.row() for u, e in sorted(self.edges.items())},
            "episodes": {u: dict(sorted(p.items())) for u, p in sorted(self.episodes.items())},
            "provenance": {k: dict(sorted(v.items())) for k, v in sorted(self.provenance.items())},
            "residual": sorted(self.residual, key=lambda r: r["law_id"]),
            "contradictions": sorted(self.contradictions, key=lambda r: r["law_id"]),
            "stats": self.stats,
        }


def _episode(parsed: ParsedStatute, group_id: str, created_at: str
             ) -> tuple[str, dict[str, Any], str]:
    """(episode_uuid, props, episode_name)。episode 名 = 稳定回指键。

    props 对齐 graphiti 0.31.1 EpisodicNode 的必填字段：source/source_description/
    content/valid_at/entity_edges/created_at/name/group_id/uuid。`entity_edges`
    先给空列表，Phase 4b 按实际边回填（无边的 episode 保持空列表）。
    """
    name = f"lawdb:{parsed.src_db}:{parsed.law_id}"
    uuid = _ep_uuid(group_id, name)
    props = {
        "uuid": uuid, "name": name, "group_id": group_id,
        "source_description": f"law_db.laws#{parsed.law_id}",
        "source": "text",  # graphiti EpisodeType.text
        "content": _episode_content(parsed),
        "valid_at": _episode_valid_at(parsed, created_at),
        "entity_edges": [],
        "created_at": created_at,
    }
    return uuid, props, name


def _resolve_target(base: str, corpus_index: dict[str, list[StatuteNode]]) -> StatuteNode | None:
    """《书名号》目标名回链库内节点（普查：边目标命中率取决于回链）。

    确定性规则：优先名字恰好等于基础名的节点（无文号内联的规范名）；
    否则取同名基础名里 law_id 最小的（稳定，重跑不变）。回链不上返回
    None，由调用方建 in_corpus=0 衍生节点。
    """
    cands = corpus_index.get(base)
    if not cands:
        return None
    exact = [n for n in cands if n.name == base]
    pool = exact or cands
    return min(pool, key=lambda n: n.props.get("law_id", 0))


def build_statute_graph(records: Iterable[Mapping[str, Any]], group_id: str,
                        *, now: datetime | None = None) -> StatuteGraphPlan:
    """一批 law_db 行 -> 节点/边/残余/矛盾/统计。纯函数、幂等。

    `now` 是图内 created_at 的构建时刻（默认当前 UTC）；测试注入固定时钟即可
    断言「同输入两次构造 uuid/created_at 稳定」。uuid 本身与时钟无关。
    """
    created_at = _utc_iso(now or datetime.now(timezone.utc))
    parsed = [parse_law_record(r) for r in records]
    nodes: dict[str, StatuteNode] = {}
    episodes: dict[str, dict[str, Any]] = {}
    provenance: dict[str, dict[str, Any]] = {}
    residual: list[dict[str, Any]] = []
    contradictions: list[dict[str, Any]] = []

    eff_by_source: Counter[str] = Counter()
    repeal_rows_title = repeal_rows_tail = repeal_rows_tail_pre = 0
    amend_rows_modify = amend_rows_bill = amend_rows_version = 0
    status_repealed_no_edge = 0
    edges: dict[str, StatuteEdge] = {}
    # 边聚合：同 uuid 的 episodes 累计、属性首写不变（重复行 copies 场景）
    edge_episodes: dict[str, set[str]] = {}

    def emit_edge(e: StatuteEdge) -> None:
        e.created_at = created_at  # graphiti Edge.created_at 必填
        if e.uuid in edges:
            edge_episodes[e.uuid].update(e.episodes)
            return
        edges[e.uuid] = e
        edge_episodes[e.uuid] = set(e.episodes)

    # ---- Phase 1：行级节点 + episode + 解析统计 ----
    for p in parsed:
        eff_by_source[p.eff.source or "missing"] += 1
        node_uuid = _node_uuid(group_id, p.node_name)
        node = StatuteNode(
            uuid=node_uuid, name=p.node_name, group_id=group_id,
            labels=["Entity", "Statute"], created_at=created_at,
            summary=f"{p.title}｜{p.status or '未知'}｜{p.src_db}源"
                    + ("｜文号矛盾待修" if p.docno_contradiction else ""),
            props={
                "status": p.status or "", "src_db": p.src_db,
                "publish_at": p.publish or "", "effective_at": p.eff.date or "",
                "effective_source": p.eff.source or "",
                "version_year": p.amend.version_year if p.amend.kind == "version_paren" else 0,
                "doc_number": p.identity_docno or "",
                "kind": ("decision" if (p.decision_class or "的决定" in p.title) else
                         ("amendment" if p.amend.kind == "amendment_bill" else "statute")),
                "in_corpus": 1, "law_id": p.law_id,
            },
        )
        if node_uuid not in nodes:  # 同名同行身份：首写胜（输入顺序确定性）
            nodes[node_uuid] = node
        ep_uuid, ep_props, ep_name = _episode(p, group_id, created_at)
        episodes[ep_uuid] = ep_props
        provenance[ep_name] = {
            "source_db": "law_db", "stable_id": f"laws:{p.law_id}",
            "doc_title": p.title, "as_of": p.publish or p.eff.date,
        }

        if p.docno_contradiction:
            contradictions.append({
                "law_id": p.law_id, "title": p.title,
                "title_no": p.docno.title_no, "meta_no": p.docno.meta_no,
                "disposition": "以标题为准建节点；不据矛盾元数据建边（待修）",
            })

        if "repeal_title" in p.repeal_tags:
            repeal_rows_title += 1
        if "repeal_tail" in p.repeal_tags:
            repeal_rows_tail += 1
        if "repeal_tail_pre" in p.repeal_tags:
            repeal_rows_tail_pre += 1
        if p.amend.kind == "modify_decision":
            amend_rows_modify += 1
        elif p.amend.kind == "amendment_bill":
            amend_rows_bill += 1
        elif p.amend.kind == "version_paren":
            amend_rows_version += 1
        if p.status in ("已废止", "已失效", "失效") and not p.repeal_targets:
            status_repealed_no_edge += 1

    # ---- Phase 2：目标回链索引（基础名 -> 候选节点） ----
    # 索引键用 base_name（canonical 标题、无内联文号）：废止/修正句式提到的
    # 《书名号》名是纯法规名，必须按基础名回链，否则带文号的同名节点永远
    # 链不上（普查：边目标命中率取决于回链）。
    node_by_name: dict[str, str] = {}  # node_name -> uuid（行级节点）
    for p in parsed:
        node_by_name[p.node_name] = _node_uuid(group_id, p.node_name)
    corpus_index: dict[str, list[StatuteNode]] = {}
    for p in parsed:
        u = node_by_name[p.node_name]
        if u in nodes:
            corpus_index.setdefault(p.base_name, []).append(nodes[u])

    def target_node_for(base: str) -> StatuteNode:
        hit = _resolve_target(base, corpus_index)
        if hit is not None:
            return hit
        stub_uuid = _node_uuid(group_id, base)
        stub = nodes.get(stub_uuid)
        if stub is None:
            stub = StatuteNode(
                uuid=stub_uuid, name=base, group_id=group_id,
                labels=["Entity", "Statute"], created_at=created_at,
                summary=f"{base}｜库外目标（由废止/修正句式推导）",
                props={"status": "", "src_db": "", "publish_at": "",
                       "effective_at": "", "effective_source": "",
                       "version_year": 0, "doc_number": "", "kind": "statute",
                       "in_corpus": 0, "law_id": 0},
            )
            nodes[stub_uuid] = stub
            corpus_index.setdefault(base, []).append(stub)
        return stub

    # ---- Phase 3：边构造 ----
    # 废止日期映射：目标节点 uuid -> 最早可确证废止日（决定的施行日，缺则公布日）
    repeal_at: dict[str, str] = {}

    def _decision_date(p: ParsedStatute) -> str:
        return p.eff.date or p.publish or ""

    for p in parsed:
        ep_uuid = _episode(p, group_id, created_at)[0]
        src_uuid = _node_uuid(group_id, p.node_name)

        # EFFECTIVE 自环：有具体施行日才建（无日不猜，row 进残余）
        if p.eff.date:
            e_uuid = _edge_uuid(group_id, src_uuid, REL_EFFECTIVE, src_uuid)
            emit_edge(StatuteEdge(
                uuid=e_uuid, src=src_uuid, dst=src_uuid, name=REL_EFFECTIVE,
                fact=f"自{p.eff.date}起施行（来源：{SOURCE_LABELS[p.eff.source]}）",
                group_id=group_id, episodes=[ep_uuid],
                valid_at=_day_iso(p.eff.date),
                reference_time=_day_iso(p.publish or p.eff.date),
            ))

        # REPEALS：决定 -> 各被废止目标
        for target in p.repeal_targets:
            tnode = target_node_for(target)
            d = _decision_date(p)
            if d:
                prev = repeal_at.get(tnode.uuid)
                if not prev or d < prev:
                    repeal_at[tnode.uuid] = d
            e_uuid = _edge_uuid(group_id, src_uuid, REL_REPEALS, tnode.uuid)
            emit_edge(StatuteEdge(
                uuid=e_uuid, src=src_uuid, dst=tnode.uuid, name=REL_REPEALS,
                fact=f"废止《{target}》"
                     f"（来源：{'标题' if 'repeal_title' in p.repeal_tags else '附则'}）",
                group_id=group_id, episodes=[ep_uuid],
                valid_at=_day_iso(d) if d else "",
                reference_time=_day_iso(p.publish or d) if (p.publish or d) else "",
            ))

        # AMENDS：修改决定 -> 《》目标；修正案/修订案 -> 宿主法
        if p.amend.kind in ("modify_decision", "amendment_bill"):
            for target in p.amend.targets:
                tnode = target_node_for(target)
                d = _decision_date(p)
                e_uuid = _edge_uuid(group_id, src_uuid, REL_AMENDS, tnode.uuid)
                emit_edge(StatuteEdge(
                    uuid=e_uuid, src=src_uuid, dst=tnode.uuid, name=REL_AMENDS,
                    fact=f"修改《{target}》", group_id=group_id, episodes=[ep_uuid],
                    valid_at=_day_iso(d) if d else "",
                    reference_time=_day_iso(p.publish or d) if (p.publish or d) else "",
                ))

        # VERSION_OF：（YYYY修正）版本节点 -> 基础法
        if p.amend.kind == "version_paren" and p.amend.base_name:
            tnode = target_node_for(p.amend.base_name)
            e_uuid = _edge_uuid(group_id, src_uuid, REL_VERSION_OF, tnode.uuid)
            emit_edge(StatuteEdge(
                uuid=e_uuid, src=src_uuid, dst=tnode.uuid, name=REL_VERSION_OF,
                fact=f"{p.amend.version_year}年修正版本", group_id=group_id,
                episodes=[ep_uuid],
                reference_time=_day_iso(p.publish) if p.publish else "",
            ))

    # ---- Phase 4：废止日回填 EFFECTIVE 窗口的 invalid_at ----
    for e in edges.values():
        if e.name == REL_EFFECTIVE and e.dst in repeal_at:
            e.invalid_at = _day_iso(repeal_at[e.dst])
            e.fact += f"；{repeal_at[e.dst]}起废止"

    for u, eps in edge_episodes.items():
        edges[u].episodes = sorted(eps)

    # ---- Phase 4b：Episodic.entity_edges 回填实际边 uuid ----
    # graphiti EpisodicNode 的必填字段（默认空列表也合法）；有实际边就写实际
    # uuid，保证 provenance 沿 episode 找到的边与图内一致。
    ep_edges: dict[str, set[str]] = {u: set() for u in episodes}
    for e in edges.values():
        for ep in e.episodes:
            ep_edges.setdefault(ep, set()).add(e.uuid)
    for ep_uuid, props in episodes.items():
        props["entity_edges"] = sorted(ep_edges.get(ep_uuid, ()))

    # ---- Phase 5：残余清单（不猜：缺施行日/状态未知(非决定类)/无锚） ----
    reasons_counter: Counter[str] = Counter()
    for p in parsed:
        reasons: list[str] = []
        if p.eff.date is None:
            reasons.append("no_eff_date" if not p.eff.known_form else "eff_form_only_no_date")
        if p.status not in KNOWN_STATUSES and not (p.status == "未知" and p.decision_class):
            # 决定类文书无时效态是常态（普查 §3 建模规则），不计残余
            reasons.append("status_unknown")
        if not p.anchor:
            reasons.append("no_anchor")
        if reasons:
            for r in reasons:
                reasons_counter[r] += 1
            residual.append({
                "law_id": p.law_id, "title": p.title, "src_db": p.src_db,
                "status": p.status, "missing": ";".join(reasons),
                "publish": p.publish or "", "type": p.type_,
                "effective_source": p.eff.source or "",
                "doc_number": p.identity_docno or "",
                "tags": ";".join(p.repeal_tags) + (";amend:" + p.amend.kind if p.amend.kind else ""),
            })

    n = len(parsed)
    eff_dated = sum(1 for p in parsed if p.eff.date)
    eff_form_only = sum(1 for p in parsed if p.eff.date is None and p.eff.known_form)
    # 普查可比口径：扩展（通过日推导）之前的覆盖——与 final_summary.py 逐源对账
    audit_chain = sum(v for k, v in eff_by_source.items()
                      if k and k != "missing" and k != "head_pass_derived")
    repeal_audit_union = sum(
        1 for p in parsed
        if "repeal_title" in p.repeal_tags or "repeal_tail" in p.repeal_tags)
    amend_audit_union = sum(
        1 for p in parsed
        if ("关于修改" in p.title and "的决定" in p.title) or "修正案" in p.title
        or "修订案" in p.title or RE_VERSION_PAREN.search(p.title))
    edges_by_rel = Counter(e.name for e in edges.values())
    stats = {
        "rows": n,
        "effective": {
            "dated": eff_dated, "form_only": eff_form_only, "missing": n - eff_dated - eff_form_only,
            "by_source": dict(sorted(eff_by_source.items())),
            "audit_chain_covered": audit_chain,
            "coverage_dated": round(eff_dated / n, 4) if n else 0.0,
            "coverage_audit_chain": round(audit_chain / n, 4) if n else 0.0,
        },
        "repeal": {
            "title_form_rows": repeal_rows_title, "tail_book_rows": repeal_rows_tail,
            "tail_book_pre_ext_rows": repeal_rows_tail_pre,
            "audit_union_rows": repeal_audit_union,
            "edges": edges_by_rel.get(REL_REPEALS, 0),
            "status_repealed_without_edge": status_repealed_no_edge,
        },
        "amendment": {
            "audit_union_rows": amend_audit_union,
            "modify_decision_rows": amend_rows_modify, "bill_rows": amend_rows_bill,
            "version_paren_rows": amend_rows_version,
            "edges": edges_by_rel.get(REL_AMENDS, 0) + edges_by_rel.get(REL_VERSION_OF, 0),
        },
        "identity": {
            "nodes_with_docno": sum(1 for p in parsed if p.identity_docno),
            "docno_contradictions": len(contradictions),
        },
        "graph": {
            "nodes": len(nodes), "stub_nodes": sum(1 for x in nodes.values() if not x.props.get("in_corpus")),
            "edges": len(edges), "edges_by_relation": dict(sorted(edges_by_rel.items())),
            "episodes": len(episodes),
            # Episodic.valid_at 无施行日/发布日可用、退化为构建时刻的行数
            # （读模型要求 valid_at 非空；兜底不静默）
            "episodes_valid_at_fallback": sum(
                1 for p in parsed if not (p.eff.date or p.publish)),
        },
        "residual": {
            "rows": len(residual), "ratio": round(len(residual) / n, 4) if n else 0.0,
            "reasons": dict(sorted(reasons_counter.items())),
        },
        "llm_tokens": 0,
    }
    return StatuteGraphPlan(
        group_id=group_id, nodes=nodes, edges=edges, episodes=episodes,
        provenance=provenance, residual=residual, contradictions=contradictions,
        stats=stats,
    )


# =====================================================================
# 四、幂等写入（falkordb bolt）
# =====================================================================

def parse_uri(uri: str) -> tuple[str, int]:
    """bolt://host:port -> (host, port)。

    从 lane.py 复制的最小实现（注明）：import lane 会连带 llm/embedding
    模块与本模块无关的依赖面，不值得。
    """
    rest = uri.split("://", 1)[-1]
    host, _, port = rest.partition(":")
    return host, int(port or 6379)


class StatuteGraphWriter:
    """幂等 upsert 写入器（经 falkordb bolt，group_id 显式）。

    clone 纪律的 bolt 等价物：`FalkorDB(host, port).select_graph(group_id)`
    显式绑定图名——绝不用默认图（query.py 的教训：拿错图名不报错只返
    空集）。全部写入走 MERGE-on-uuid + SET 覆盖：同计划重复 upsert，
    节点/边数量与内容不变（幂等可重入，批处理可断点重跑）。

    属性均为字符串/整数/字符串列表——FalkorDB 只收原始类型属性
    （lane.py 实测坑：非原始类型写入即抛）。
    """

    BATCH = 500

    # 属性名逐项对齐 graphiti-core 0.31.1 读模型：
    # - EntityNode 必填 uuid/name/group_id/summary/created_at；name_embedding
    #   可空但**必须 vecf32() 写入**——FalkorDB 向量索引只认 VectorF32，
    #   写普通 list 会让 graphiti 的向量检索报 Type mismatch（重建期实测）。
    # - EpisodicNode 必填 source/source_description/content/valid_at/
    #   entity_edges/created_at —— 少一项 get_episodes 就抛 pydantic 错误。
    # - EntityEdge 必填 uuid/source/target/created_at/name/fact/episodes，
    #   valid_at/invalid_at/expired_at/reference_time 可空（写 null）。
    NODE_QUERY = """
UNWIND $rows AS row
MERGE (n:Entity {uuid: row.uuid})
SET n.name = row.name, n.group_id = row.group_id, n.summary = row.summary,
    n.labels = row.labels, n.created_at = row.created_at,
    n.name_embedding = CASE WHEN row.name_embedding IS NULL THEN NULL
                            ELSE vecf32(row.name_embedding) END,
    n.status = row.status, n.src_db = row.src_db,
    n.publish_at = row.publish_at, n.effective_at = row.effective_at,
    n.effective_source = row.effective_source, n.version_year = row.version_year,
    n.doc_number = row.doc_number, n.kind = row.kind, n.in_corpus = row.in_corpus,
    n.law_id = row.law_id
""".strip()

    EPISODE_QUERY = """
UNWIND $rows AS row
MERGE (e:Episodic {uuid: row.uuid})
SET e.name = row.name, e.group_id = row.group_id,
    e.source = row.source, e.source_description = row.source_description,
    e.content = row.content, e.valid_at = row.valid_at,
    e.entity_edges = row.entity_edges, e.created_at = row.created_at
""".strip()

    EDGE_QUERY = """
UNWIND $rows AS row
MERGE (a:Entity {uuid: row.src})
MERGE (b:Entity {uuid: row.dst})
MERGE (a)-[r:RELATES_TO {uuid: row.uuid}]->(b)
SET r.name = row.name, r.fact = row.fact, r.group_id = row.group_id,
    r.episodes = row.episodes, r.created_at = row.created_at,
    r.valid_at = row.valid_at, r.invalid_at = row.invalid_at,
    r.expired_at = row.expired_at, r.reference_time = row.reference_time
""".strip()

    def __init__(self, uri: str, group_id: str):
        self.uri = uri
        self.group_id = group_id
        self._graph: Any = None

    def connect(self) -> "StatuteGraphWriter":
        """建立 bolt 连接并显式 select_graph(group_id)。"""
        from falkordb import FalkorDB  # 懒加载：测试环境无 falkordb 服务

        host, port = parse_uri(self.uri)
        self._graph = FalkorDB(host=host, port=port).select_graph(self.group_id)
        return self

    def _run(self, cypher: str, params: dict[str, Any]) -> None:
        self._graph.query(cypher, params)

    def _batches(self, rows: list[dict[str, Any]]):
        for i in range(0, len(rows), self.BATCH):
            yield rows[i: i + self.BATCH]

    def upsert_plan(self, plan: StatuteGraphPlan) -> dict[str, int]:
        """节点 → episode → 边（边最后：MERGE 端点兜底也要求节点先在）。"""
        if self._graph is None:
            raise RuntimeError("writer not connected — call connect() first")
        node_rows = [n.row() for n in sorted(plan.nodes.values(), key=lambda x: x.uuid)]
        episode_rows = [dict(sorted(p.items()))
                        for _u, p in sorted(plan.episodes.items())]
        edge_rows = [e.row() for e in sorted(plan.edges.values(), key=lambda x: x.uuid)]
        for batch in self._batches(node_rows):
            self._run(self.NODE_QUERY, {"rows": batch})
        for batch in self._batches(episode_rows):
            self._run(self.EPISODE_QUERY, {"rows": batch})
        for batch in self._batches(edge_rows):
            self._run(self.EDGE_QUERY, {"rows": batch})
        return {"nodes": len(node_rows), "episodes": len(episode_rows),
                "edges": len(edge_rows)}


# =====================================================================
# 五、可选嵌入（不是抽取：与 llm_tokens 分账）
# =====================================================================

def degraded_embedding_report(plan: StatuteGraphPlan, *, reason: str,
                              requested: bool = True) -> dict[str, Any]:
    """未执行嵌入时的如实账单（不静默）：节点 name_embedding 全部为 None。

    requested=True 时把全部节点计入 degraded（例如缺 `LEXKG_EMBED_*` 配置）；
    requested=False（`--no-embed` 显式关闭）时计入 skipped，不算降级。
    """
    for node in plan.nodes.values():
        node.name_embedding = None
    n = len(plan.nodes)
    return {
        "requested": requested,
        "reason": reason,
        "model": None,
        "dim": None,
        "nodes": n,
        "attempted": 0,
        "embedded": 0,
        "degraded": n if requested else 0,
        "skipped": 0 if requested else n,
        "input_chars": 0,
        "batches": 0,
        "failed_batches": 0,
        "errors": [],
    }


def attach_name_embeddings(plan: StatuteGraphPlan, embedder: Any, *,
                           batch_size: int = EMBED_BATCH,
                           model: str | None = None,
                           dim: int | None = None) -> dict[str, Any]:
    """按批补 Entity.name_embedding；单批失败降级 None 并在报告记数。

    嵌入不是 LLM 抽取：本函数不产生任何抽取 token，`stats.llm_tokens` 保持 0。
    失败不抛出（跑批要能继续落图），但 `degraded/failed_batches/errors`
    必须如实写进 CLI 报告的 `embedding` 分项。

    **所有批次必须在同一个事件循环里跑**：embedder 缓存的 AsyncOpenAI 客户端
    绑定在首次使用的事件循环上，每批一次 asyncio.run 会让第二批起跨循环失败
    （重建期实测恰好一半批次降级——event loop 绑定缺陷）。
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    nodes = sorted(plan.nodes.values(), key=lambda n: n.uuid)
    report: dict[str, Any] = {
        "requested": True,
        "reason": "",
        "model": model,
        "dim": dim,
        "nodes": len(nodes),
        "attempted": 0,
        "embedded": 0,
        "degraded": 0,
        "skipped": 0,
        "input_chars": 0,
        "batches": 0,
        "failed_batches": 0,
        "errors": [],
    }

    async def _run_batches() -> None:
        for i in range(0, len(nodes), batch_size):
            batch = nodes[i: i + batch_size]
            report["batches"] += 1
            report["attempted"] += len(batch)
            texts = [n.name.replace("\n", " ") for n in batch]
            report["input_chars"] += sum(len(text) for text in texts)
            try:
                vectors = await embedder.create_batch(texts)
                if len(vectors) != len(batch):
                    raise ValueError(
                        f"vector count mismatch: got {len(vectors)}, expected {len(batch)}")
                vecs = [[float(x) for x in vec] for vec in vectors]
            except Exception as exc:  # 降级而非中断：报告里必须留痕
                report["failed_batches"] += 1
                report["degraded"] += len(batch)
                if len(report["errors"]) < 5:
                    report["errors"].append(f"{type(exc).__name__}: {exc}")
                continue
            for node, vec in zip(batch, vecs):
                node.name_embedding = vec
            report["embedded"] += len(batch)

    asyncio.run(_run_batches())
    return report
