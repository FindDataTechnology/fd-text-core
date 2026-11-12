"""全文采集与组装：把 e-print 字节变成"可入库的 content + 可查询的状态"。

为什么单独一个模块而不是写在脚本里：``scripts/load_papers.py`` 里那段
"只拼标题+摘要"曾经是唯一的实现，于是 spec 要求的"全文入库"没有任何执行位，
解析器 180 行也零调用点。把"取源 → 判类型 → 解析 → 降级"这条链抽成纯函数，
才能用注入 fetcher 的离线单测把**每条分支**都钉住（含降级分支），
不必为了验证一次连 arXiv、连数据库。
"""
from __future__ import annotations

from collections.abc import Callable

from .harvest import EprintError, Paper, Throttler, fetch_eprint, looks_like_tex
from .parser import parse_fulltext, parse_pdf_via_grobid

#: 全文状态：既能回答"有没有全文"，也能回答"为什么没有"。
#: 只留一个 has_fulltext=false 而不说明原因，等于把六种失败压成一句"没有"。
STATUS_LATEX = "latex"
STATUS_GROBID = "grobid"
STATUS_NEEDS_GROBID = "needs-grobid"
STATUS_FETCH_ERROR = "fetch-error"
STATUS_PARSE_EMPTY = "parse-empty"
STATUS_DISABLED = "disabled"

FULLTEXT_STATUSES = (
    STATUS_LATEX,
    STATUS_GROBID,
    STATUS_NEEDS_GROBID,
    STATUS_FETCH_ERROR,
    STATUS_PARSE_EMPTY,
    STATUS_DISABLED,
)


def acquire_fulltext(
    paper: Paper,
    *,
    throttler: Throttler | None = None,
    fetcher: Callable | None = None,
    grobid_endpoint: str = "",
    grobid_fetcher: Callable[[str, bytes], str] | None = None,
) -> tuple[str, str]:
    """取一篇的全文，返回 ``(full_text, status)``。永不抛异常——按篇降级。

    分支即规格：LaTeX 源解析成功记 ``latex``；PDF-only 且 GROBID 可用记
    ``grobid``；PDF-only 而 GROBID 没配记 ``needs-grobid``（正文留空、
    ``has_fulltext=false``，绝不拿摘要冒充全文）；取源失败记 ``fetch-error``；
    取到了却解析不出正文记 ``parse-empty``。
    """
    kwargs: dict = {"throttler": throttler}
    if fetcher is not None:
        kwargs["fetcher"] = fetcher
    try:
        blob = fetch_eprint(paper, **kwargs)
    except EprintError:
        return "", STATUS_FETCH_ERROR

    parsed = parse_fulltext(blob, looks_like_tex=looks_like_tex)
    if parsed.source_kind == "needs-grobid":
        parsed = parse_pdf_via_grobid(
            blob, endpoint=grobid_endpoint, fetcher=grobid_fetcher
        )
        text = (parsed.full_text or "").strip()
        if parsed.source_kind == "grobid-tei" and text:
            return text, STATUS_GROBID
        return "", STATUS_NEEDS_GROBID

    text = (parsed.full_text or "").strip()
    if not text:
        return "", STATUS_PARSE_EMPTY
    return text, STATUS_LATEX


def compose_content(paper: Paper, full_text: str) -> str:
    """入库正文 = 标题 + 摘要 + 全文。

    摘要留在最前面不是冗余：它是 CC0 的、与正文表述一致的"最干净的一段"，
    检索对短查询的命中率靠它兜底；全文给长尾与 offset 扩展用。
    """
    parts = [paper.title, "", paper.abstract]
    if full_text:
        parts += ["", full_text]
    return "\n".join(p for p in parts if p).strip()


def fulltext_metadata(full_text: str, status: str) -> dict:
    """``has_fulltext`` 必须由**真实正文**推出，不能由开关推出。

    开关打开只是"尝试取全文"；取不到时仍为 false 并带原因。这正是本 change
    要修的那个假绿：旧实现恒定写 false，于是"没有全文"和"关了开关"无法区分。
    """
    if status not in FULLTEXT_STATUSES:  # pragma: no cover - 防新增状态漏登记
        raise ValueError(f"未知 fulltext_status: {status}")
    return {
        "has_fulltext": "true" if full_text else "false",
        "fulltext_status": status,
    }
