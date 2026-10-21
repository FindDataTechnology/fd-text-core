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

# 中文条号：第四百二十四条 / 第313条 / 第十二条之一
_ARTICLE = re.compile(r"第[0-9零一二三四五六七八九十百千]+条(?:之[一二三四五六七八九十]+)?")
_BOOK_TITLE = re.compile(r"[《〈]([^》〉]+)[》〉]")
# 文号：财会〔2001〕1053号 / 财政部令第113号 / 2026年第28号
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


def split_doc_number(name: str) -> tuple[str, str | None]:
    """把「名称 + 文号」拆成 (去文号的名, 文号)。

    文号是法规/公文的**版本身份**：113 号与 71 号都叫《行政单位财务规则》，
    但它们是两个不同版本，各有各的有效期。实测中抽取把 113 号的施行日挂到了
    71 号上，根因就是同名实体被当成同一个。有文号的名称，文号必须参与身份。
    """
    n = normalize(name)
    m = _DOC_NUM.search(n)
    if not m:
        return n, None
    base = (n[: m.start()] + n[m.end():]).strip(_WS.pattern.strip("^$") or " \t")
    base = _WS.sub("", base or n)
    return base, m.group(0)


def canonical_name(name: str) -> str:
    """归并后的规范名：去书名号 + 去空白 + 若有条号则只留法规主体。"""
    base = strip_book_titles(normalize(name))
    head, _article = split_statute_article(base)
    return head


def merge_key(name: str) -> str:
    """归并键：规范名 + 文号（同键即同一实体）。

    **文号必须参与身份**：同名不同文号是同一法规的不同版本（各有自己的有效期），
    合并它们会让时点查询给出错误答案。无文号的名称退化为纯规范名。
    """
    base = strip_book_titles(normalize(name))
    stripped, doc_no = split_doc_number(base)
    head, _article = split_statute_article(stripped)
    return f"{head}|{doc_no}" if doc_no else head


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
