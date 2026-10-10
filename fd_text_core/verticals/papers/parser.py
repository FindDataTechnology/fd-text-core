"""论文解析：LaTeX 源优先，PDF 兜底走 GROBID（Apache-2.0）。

许可红线（务必守住，改动前先看 verticals/papers/LICENSE-NOTE.md）：
  - 禁用 PyMuPDF：AGPL-3.0 双许可，与闭源平台冲突
  - 禁用 Marker：模型权重非商用（营收 >$5M 需付费许可）
  - GROBID Apache-2.0：生产可用，产出 TEI XML -> 结构树
解析器只产出「干净文本 + 结构树 + 元数据」，不产出任何可发行的全文副本。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# 常见 LaTeX 噪声：注释、简单宏、内联数学标记（保留文本内容）
_TEX_COMMENT = re.compile(r"(?<!\\)%[^\n]*")
_TEX_KNOWN_CMDS = re.compile(
    r"\\(?:textbf|textit|emph|texttt|textsc|mathrm|mathbf|mathit|label|ref|cite|citet|citep|"
    r"footnote|caption|includegraphics|input|include|bibliography|bibliographystyle|"
    r"usepackage|documentclass|newcommand|renewcommand|def|title|author|date|thanks|"
    r"section|subsection|subsubsection|paragraph)\b"
)
_TEX_BRACES_ARG = re.compile(r"\\[a-zA-Z]+\*?(?:\s*\[[^\]]*\])?\s*\{")
_MATH_INLINE = re.compile(r"\$([^$]*)\$")
_BEGIN_END_SKIP = re.compile(
    r"\\(?:begin|end)\{(?:document|figure\*?|table\*?|tikzpicture|center|abstract)\}", re.I
)


@dataclass
class ParsedPaper:
    title: str = ""
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    sections: list[tuple[str, str]] = field(default_factory=list)  # [(路径, 文本)]
    full_text: str = ""
    source_kind: str = "unknown"  # latex | grobid-tei


def detex(tex: str) -> str:
    """极简 detex：去注释与命令壳，保留正文与数学内容文本。

    这是「够用就好」的实现——目标是干净可读文本，而非完美编译。涉及
    \\include 的多文件稿顶级ery_key水准不高，此时应回退 GROBID PDF 路径。
    """
    s = _TEX_COMMENT.sub("", tex)
    s = _BEGIN_END_SKIP.sub(" ", s)
    s = _MATH_INLINE.sub(r" \1 ", s)
    s = re.sub(r"\$\$?[^$]*\$\$?", " ", s)
    # 去掉命令名本身，保留其参数内容
    prev = None
    while prev != s:
        prev = s
        s = _TEX_KNOWN_CMDS.sub("", s)
        s = _TEX_BRACES_ARG.sub("{", s)
    s = s.replace("\\%", "%").replace("\\&", "&").replace("\\_", "_")
    s = re.sub(r"\\{2,}", "\n", s)  # \\ -> 换行
    # 数学命令展开：\sum_i -> sum_i，\alpha -> alpha（保留符号名而非吞掉内容）
    s = re.sub(r"\\([a-zA-Z]+)", r" \1", s)
    # 逐层剥离残留花括号（\textbf{a} / {Quantitative} 等会留下裸 { }）
    for _ in range(6):
        new = re.sub(r"\{([^{}]*)\}", r"\1", s)
        if new == s:
            break
        s = new
    s = s.replace("{", " ").replace("}", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


_SECTION_CMD = re.compile(
    r"\\(section|subsection|subsubsection)\*?\{([^}]*)\}", re.M
)


def parse_latex(tex: str) -> ParsedPaper:
    """LaTeX 源 -> 结构树。标题/摘要提取失败时留空，由上游元数据补齐。"""
    out = ParsedPaper(source_kind="latex")
    m = re.search(r"\\title\*?\{([^}]*)\}", tex)
    if m:
        out.title = detex(m.group(1))
    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", tex, re.S)
    if m:
        out.abstract = detex(m.group(1))

    marks = list(_SECTION_CMD.finditer(tex))
    if marks:
        for i, mk in enumerate(marks):
            start = mk.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(tex)
            heading = detex(mk.group(2))
            body = detex(tex[start:end])
            if body:
                out.sections.append((f"paper>{heading}", body))
        body_after_title = detex(tex[: marks[0].start()]) if marks[0].start() > 0 else ""
        out.full_text = "\n\n".join(
            p for p in [out.abstract, body_after_title, *[b for _, b in out.sections]] if p
        )
    else:
        out.full_text = detex(tex)
        out.sections = [("paper>body", out.full_text)]
    return out


_SECTION_MARKERS = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(abstract|introduction|related work|background|method(?:ology)?|"
    r"model|experiment(?:s|al setup)?|results?|discussion|conclusion(?:s)?|"
    r"references|acknowledg(?:e)?ment(?:s)?)\s*$",
    re.I | re.M,
)


def parse_grobid_tei(tei_xml: str) -> ParsedPaper:
    """GROBID TEI XML -> 结构树（PDF 兜底路径）。"""
    import xml.etree.ElementTree as ET

    ns = {"tei": "http://www.tei-c.org/ns/1.0"}
    out = ParsedPaper(source_kind="grobid-tei")
    try:
        root = ET.fromstring(tei_xml)
    except ET.ParseError:
        out.full_text = ""  # 解析失败如实留空，不得返回伪文本
        return out

    def txt(path: str) -> str:
        el = root.find(path, ns)
        return " ".join(el.itertext()).strip() if el is not None else ""

    title_el = root.find(".//tei:titleStmt/tei:title", ns)
    out.title = "".join(title_el.itertext()).strip() if title_el is not None else ""
    out.abstract = txt(".//tei:profileDesc/tei:abstract")
    out.authors = [
        " ".join(p.itertext()).strip()
        for p in root.findall(".//tei:sourceDesc//tei:author/tei:persName", ns)
    ]
    bodies: list[tuple[str, str]] = []
    for div in root.findall(".//tei:body//tei:div", ns):
        head = div.find("tei:head", ns)
        label = "".join(head.itertext()).strip() if head is not None else "body"
        text = " ".join(div.itertext()).strip()
        if text:
            bodies.append((f"paper>{label or 'body'}", text))
    out.sections = bodies or [("paper>body", " ".join(root.itertext()).strip())]
    out.full_text = "\n\n".join(t for _, t in out.sections)
    return out


def parse_fulltext(blob: bytes, *, looks_like_tex) -> ParsedPaper:
    """入口：判断 e-print 是 LaTeX 源还是 PDF。

    PDF 内容不在此处解析（需 GROBID 服务），返回 source_kind='needs-grobid'
    由管线转交 GROBID sidecar——避免把 AGPL 依赖塞进本仓。
    """
    if looks_like_tex(blob):
        return parse_latex(blob.decode("utf-8", errors="ignore"))
    if blob[:4] == b"%PDF":
        out = ParsedPaper(source_kind="needs-grobid")
        return out
    return ParsedPaper(source_kind="unknown")


def iter_sections(p: ParsedPaper) -> Iterable[tuple[str, str]]:
    return p.sections
