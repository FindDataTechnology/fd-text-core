"""实体归并后处理：确定性规则，不交给 LLM。

**为什么不交给 LLM**：条号、书名号是正则可解析的结构。试点发现 Statute 类里
约 15% 是「法规名+条号」的独立节点（`中华人民共和国合同法第四百二十四条`、
`《中华人民共和国刑法》第313条`），与法规主体节点（`中华人民共和国刑法`）
互不相连——导致「哪些案例引用了刑法」这类聚合漏掉全部条级引用。
让模型去合并只会引入幻觉：它可能把「第 313 条」归到错误的法。

规则（全部可逆、可解释）：
  1. 去书名号：`《中华人民共和国刑法》` -> `中华人民共和国刑法`
  2. 拆分「法规名 + 条号」：`中华人民共和国合同法第四百二十四条` ->
     (法规主体 `中华人民共和国合同法`, 条号 `第四百二十四条`)
  3. 同主体归并：连到法规主体节点，条号作为边的属性保留（不丢信息）

归并是**幂等**的：重复执行结果不变——这是一条硬要求，跑批可重入。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

# 中文条号：第四百二十四条 / 第313条 / 第十二条之一
_ARTICLE = re.compile(r"第[0-9零一二三四五六七八九十百千]+条(?:之[一二三四五六七八九十]+)?")
_BOOK_TITLE = re.compile(r"[《〈]([^》〉]+)[》〉]")
# 文号消费分两形（kg-lane-delivery-v1 集成期定案）：
# ① 括号包裹形 `X（财政部令第113号）`：整段消费（含机关令前缀与年份）——
#    括号天然界定文号边界，机关名无歧义。这是语料/抽取实际产出的主流形态，
#    也是与法规层确定性建图器（statute_layer）写图名对齐的唯一规范形态。
# ② 裸拼接形 `X财政部令第113号`：保持旧行为——只消费序号段，机关令名留在
#    主体里。无法则后缀判据时裸形的机关名边界不可判定（实测贪婪正则会把
#    「行政单位财务规则财政部」整段当机关名吞掉），旧行为在拼接变体之间
#    内部一致（既有测试锁定），不强行统一。
_ISSUER_HEAD = r"[一-龥]{1,20}(?:部|委|局|署|府|厅|会|院|行|司|关)"
_SEQ_NO = r"第[0-9零一二三四五六七八九十百]+号"
_DOC_NUM_CORE = rf"(?:{_ISSUER_HEAD}令{_SEQ_NO}|\d{{4}}年{_SEQ_NO}|[〔\[]\d{{4}}[〕\]]\s*\d+号|{_SEQ_NO})"
_DOC_NUM_WRAPPED = re.compile(rf"[（(]\s*({_DOC_NUM_CORE})\s*[)）]")
_DOC_NUM = re.compile(r"[〔\[]\d{4}[〕\]]\s*\d+号|第[0-9零一二三四五六七八九十百]+号")
# 法规名后缀：用于判定「这是法规主体名」
_STATUTE_SUFFIXES = ("法", "法典", "条例", "规定", "办法", "细则", "解释", "决定", "通知", "批复")
_WS = re.compile(r"[\s\u3000]+")


def normalize(text: str) -> str:
    """统一宽度与空白（避免「刑法 」与「刑法」被当成两个实体）。"""
    return _WS.sub("", unicodedata.normalize("NFKC", text or ""))


def strip_book_titles(text: str) -> str:
    """去书名号，保留内容：`《刑法》` -> `刑法`。"""
    return _BOOK_TITLE.sub(r"\1", text)


def split_statute_article(name: str) -> tuple[str, str | None]:
    """把「法规名+条号」拆成 (法规主体, 条号)。

    只在整个名字**以条号结尾**时拆（`刑法第四百二十四条` 拆；
    `关于第四百二十四条的解释` 不拆——条号在中间说明它是正文一部分）。
    """
    n = normalize(name)
    m = _ARTICLE.search(n)
    if not m or m.end() != len(n):
        return n, None
    head = n[: m.start()]
    if not head or not head.endswith(_STATUTE_SUFFIXES):
        return n, None
    return head, m.group(0)


def normalize_docno_issuer(docno: str | None) -> str | None:
    """文号机关名去重复前缀（源数据实测脏形态的确定性清理）。

    实测源数据存在「财政部中华人民共和国财政部令第71号」这类**重复机关名**
    拼写（law_db 正文原文如此，见 76231 行）；「中华人民共和国」是机关规范名
    的起始词，其之前的重复机关名是源拼接残留。规则：出现「中华人民共和国」
    时取该处起的后缀；否则原样返回。幂等。
    """
    if not docno:
        return docno
    idx = docno.find("中华人民共和国")
    return docno[idx:] if idx > 0 else docno


def split_doc_number(name: str) -> tuple[str, str | None]:
    """把「名称 + 文号」拆成 (去文号的名, 文号全形)。

    文号是法规/公文的**版本身份**：113 号与 71 号都叫《行政单位财务规则》，
    但它们是两个不同版本，各有自己的有效期。实测中抽取把 113 号的施行日挂到了
    71 号上，根因就是同名实体被当成同一个。有文号的名称，文号必须参与身份。

    括号包裹形返回**文号全形**（含机关令前缀，如「财政部令第113号」）；
    裸拼接形按旧行为只返回序号段（机关令名留在主体，拼接变体间内部一致）。
    拆完主体为空则不拆（宁留不完美名字，也不造空主体假归并）。
    """
    n = normalize(name)
    m = _DOC_NUM_WRAPPED.search(n)
    if m:
        docno, span = m.group(1), m.span()
    else:
        m = _DOC_NUM.search(n)
        if not m:
            return n, None
        docno, span = m.group(0), m.span()
    base = _WS.sub("", n[: span[0]] + n[span[1]:])
    if not base:
        return n, None
    return base, normalize_docno_issuer(docno)


def inline_docno(base: str, docno: str | None) -> str:
    """文号内联进图名的**唯一规范形态**：`X（财政部令第113号）`。

    散文抽取侧（EntityMerger 改写名）与法规层确定性建图器（statute_layer
    写图名）都必须经本函数拼名——两侧各拼各的会导致同一法规在跨层时分裂。
    """
    return f"{base}（{docno}）" if docno else base


def canonical_name(name: str) -> str:
    """归并后的规范名：去书名号 + 去空白 + 拆条号 + 文号重挂为唯一形态。

    文号以「名（文号）」形态内联保留（参与身份，同名不同文号不合并），
    且对不同表面形态（括号包裹/直接拼接/书名号包裹）产出同一字符串。
    """
    base = strip_book_titles(normalize(name))
    stripped, docno = split_doc_number(base)
    head, _article = split_statute_article(stripped)
    return inline_docno(head, docno)


def merge_key(name: str) -> tuple[str, ...]:
    """归并键：规范名 + 文号（同键即同一实体）。

    **文号必须参与身份**：同名不同文号是同一法规的不同版本（各有自己的有效期），
    合并它们会让时点查询给出错误答案。无文号的名称退化为纯规范名。

    返回元组而非「名|文号」拼接串——实测拼接串会被二次拆分（文号子串在键里
    再次命中 _DOC_NUM），违反幂等硬要求；元组天然无此问题。
    """
    base = strip_book_titles(normalize(name))
    stripped, doc_no = split_doc_number(base)
    head, _article = split_statute_article(stripped)
    return (head, doc_no) if doc_no else (head,)


def plan_merge(names: list[str]) -> dict[str, list[str]]:
    """给出一批实体名的归并计划：{规范名: [原始名, ...]}（只列有多名的组）。"""
    groups: dict[str, list[str]] = {}
    for raw in names:
        groups.setdefault(merge_key(raw), []).append(raw)
    return {k: v for k, v in groups.items() if len(v) > 1}


def is_idempotent(sample: list[str]) -> bool:
    """自检：对样本连做两次归并，结果必须一致。"""
    once = [canonical_name(n) for n in sample]
    twice = [canonical_name(n) for n in once]
    return once == twice


# ------------------------------- 归并层应用（接线：merge-lane-delivery 2.1 / 2.2）
# 抽取产物（graphiti 抽取响应的中间表示）在这里统一改写后才会进入图：
#   - 实体名 → 别名映射 → canonical_name（确定性身份：同名即同实体，
#     文号留在名字里参与身份——同名不同文号天然不合并）；
#   - 「法规名+条号」拆出的条号 → 转移到引用边的 fact 属性标记上，
#     图中不再出现条级实体节点。

# 条号在边 fact 上的确定性标记：查询侧可按 `article：` 正则还原结构化条号。
ARTICLE_MARKER = "article："


@dataclass(frozen=True)
class EntitySplit:
    """一个原始实体名的归并拆解结果。"""

    name: str  # 写入图的名字（= canonical_name，兼具身份与展示）
    article: str | None  # 「法规名+条号」拆出的条号（无则 None）
    doc_number: str | None  # 拆出的文号（无则 None；仍内联保留在 name 里）
    alias: str = ""  # 命中的别名（未命中为空）


class EntityMerger:
    """确定性实体归并器：别名词表 + 规则归并，作用于抽取产物。

    身份与展示名的关系（任务 2.1 的约定落地）：

    - **身份**用 :func:`merge_key` 判等（文号参与身份：同名不同文号不同实体）。
    - **写入图的名字**用 :func:`canonical_name`（= split 的 name，经 inline_docno
      把文号重挂为唯一规范形态）。为什么不是 merge_key 本身：merge_key 是元组，
      不是合法节点名；canonical_name 幂等（is_idempotent 已锁），文号内联在
      名字里参与身份（同名不同文号天然不合并）。graphiti 的节点身份就是名字
      （同名节点确定性合并），所以 canonical_name 即身份的物质化，merge_key
      仍是归并层的判等函数（plan_merge / 幂等自检继续用它，元组无二次拆分问题）。

    别名先映射再进归并（任务 2.2）：别名按规范化后的**整体名精确匹配**，
      不做子串替换——子串替换会误伤「最高人民法院公报」这类长名。
    """

    def __init__(self, alias_lexicon: Mapping[str, str] | None = None):
        self.alias_lexicon: dict[str, str] = dict(alias_lexicon or {})

    def split(self, raw: str) -> EntitySplit:
        """别名映射 + 归并拆解：返回写图名、条号、文号。

        拆解顺序：拆文号 → 拆条号 → 文号按唯一规范形态重挂（inline_docno）。
        必须与 canonical_name 同一条管线——任何绕开它的拼名方式都会让
        同一法规在不同表面形态下写成不同节点名（集成期实证）。
        """
        base = strip_book_titles(normalize(raw))
        alias_hit = ""
        if base in self.alias_lexicon:
            alias_hit = base
            base = normalize(self.alias_lexicon[base])
        stripped, doc_no = split_doc_number(base)
        head, article = split_statute_article(stripped)
        return EntitySplit(
            name=inline_docno(head, doc_no), article=article, doc_number=doc_no, alias=alias_hit
        )

    def canonical(self, raw: str) -> str:
        """原始名 → 写入图的名字。"""
        return self.split(raw).name

    def identity(self, raw: str) -> tuple[str, ...]:
        """原始名 → 归并身份键（别名映射后的 merge_key）。"""
        base = strip_book_titles(normalize(raw))
        if base in self.alias_lexicon:
            base = normalize(self.alias_lexicon[base])
        return merge_key(base)

    # ---------------- 抽取产物（中间表示）改写 ----------------
    # graphiti 0.31.1 抽取响应的稳定形状（prompts/extract_nodes.py 与
    # prompts/extract_edges.py，含 combined 路径）：
    #   {"extracted_entities": [{"name": str, ...}]}
    #   {"edges": [{"source_entity_name": str, "target_entity_name": str,
    #               "fact": str, ...}]}

    def rewrite_entity_payload(self, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """改写实体的 name 为写图名。原 dict 不动，返回新列表（可重入）。"""
        out = []
        for e in entities:
            if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"].strip():
                e = {**e, "name": self.canonical(e["name"])}
            out.append(e)
        return out

    def rewrite_edge_payload(self, edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """改写边端点为写图名，并把端点拆出的条号转移到边的 fact 属性标记上。

        条号为什么落在 fact：graphiti 0.31.1 从抽取响应构造 EntityEdge 时
        只取枚举字段（source/target/relation_type/fact/时间），EntityEdge 的
        结构化 attributes 仅由 edge_types 机型（又一次 LLM 调用）填充——本
        车道不用它（概率性 + 试点 FalkorDB「Property values can only be of
        primitive types」实测坑）。fact 是图里边上的持久属性，把条号以
        `（article：第X条）` 标记并入 fact 是唯一的确定性落点。
        """
        out = []
        for e in edges:
            if not (isinstance(e, dict) and isinstance(e.get("fact"), str)):
                out.append(e)
                continue
            edge = dict(e)
            articles: list[str] = []
            for end in ("source_entity_name", "target_entity_name"):
                raw = edge.get(end)
                if not isinstance(raw, str) or not raw.strip():
                    continue
                sp = self.split(raw)
                edge[end] = sp.name
                if sp.article and sp.article not in articles:
                    articles.append(sp.article)
            fact = edge.get("fact") or ""
            if articles and ARTICLE_MARKER not in fact:
                missing = [a for a in articles if a not in fact]
                if missing:
                    edge["fact"] = f"{fact}（{ARTICLE_MARKER}{'、'.join(missing)}）"
            out.append(edge)
        return out


def default_merger(alias_lexicon: Mapping[str, str] | None = None) -> EntityMerger:
    """构造归并器（无词表时也要做规则归并——归并与垂域无关，别名才分垂域）。"""
    return EntityMerger(alias_lexicon)
