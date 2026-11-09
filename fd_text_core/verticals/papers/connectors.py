"""外部学术检索 connector（住垂域层，引擎核心零外部依赖）。

三条免费友好通道：
  - arXiv Export API（元数据/摘要，1 req/3s）
  - OpenAlex（100 req/s 上限、$1/日免费额度；本次只用单实体与筛选）
  - Semantic Scholar Graph API（免费 key 1 RPS）

绝不引入谷歌学术：无官方 API 且主动反自动化，批量抓违反其 ToS。

任一 connector 失联 -> 返回 degraded 标注，本地语料结果照常返回。
外部 keyed API 不得成为引擎生产的硬依赖（见 quality gate 的核心依赖检查）。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

ARXIV_API = "http://export.arxiv.org/api/query"
OPENALEX_API = "https://api.openalex.org/works"
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"


@dataclass
class ConnectorResult:
    hits: list[dict] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)


def _http_json(url: str, *, timeout: int = 30, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "fd-text-core/0.1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# 引用图（正向 references / 反向 cited_by）
#
# 礼貌池：OpenAlex 要求可识别的 User-Agent（含 mail），否则可能被限流。
# 失败口径：一律记进 ConnectorResult.degraded 并附原因（quota / upstream /
# not_indexed），由上层区分「上游坏了」与「该作品无引用」——
# 返回空 hits 时**必须**伴随 degraded 条目，否则调用方无法判别。
# ---------------------------------------------------------------------------

CITATION_TIMEOUT = 30
CITATION_LIMIT_MAX = 200


def _polite_headers(mailto: str) -> dict:
    base = "fd-text-core/0.1"
    return {"User-Agent": f"{base} (mailto:{mailto})" if mailto else base}


def _classify_http_error(exc: Exception, source: str, what: str) -> str:
    """把 HTTP/网络异常归成可判别的 degraded 原因（不是「无数据」）。"""
    import urllib.error

    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return f"{source}:{what} quota_exceeded (HTTP 429)"
        if 500 <= exc.code < 600:
            return f"{source}:{what} upstream_unavailable (HTTP {exc.code})"
        return f"{source}:{what} http_error (HTTP {exc.code})"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return f"{source}:{what} unreachable ({type(exc).__name__})"
    return f"{source}:{what} {type(exc).__name__}: {exc}"


def _resolve_work_id(doi: str = "", work_id: str = "", *, mailto: str = "",
                     api_key: str = "") -> tuple[str, str]:
    """按 doi 或 work_id 定位作品。返回 (work_id, degraded_reason)。"""
    wid = (work_id or "").strip().rstrip("/").split("/")[-1]
    if wid:
        return wid, ""
    doi = (doi or "").strip()
    if not doi:
        return "", "openalex:resolve missing_identifier (need doi or work_id)"
    params = {"filter": f"doi:{doi}", "select": "id", "per-page": "1"}
    if api_key:
        params["api_key"] = api_key
    elif mailto:
        params["mailto"] = mailto
    url = f"{OPENALEX_API}?{urllib.parse.urlencode(params)}"
    try:
        data = _http_json(url, timeout=CITATION_TIMEOUT, headers=_polite_headers(mailto))
    except Exception as exc:  # noqa: BLE001
        return "", _classify_http_error(exc, "openalex", "resolve")
    results = data.get("results") or []
    if not results:
        # 未收录 ≠ 不存在：如实标注
        return "", "openalex:resolve not_indexed (不等于该作品不存在)"
    return str(results[0].get("id", "")).rstrip("/").split("/")[-1], ""


def openalex_references(doi: str = "", work_id: str = "", *, mailto: str = "",
                        api_key: str = "") -> ConnectorResult:
    """正向引用图：某作品引用了哪些作品。

    返回 hits=[{work_id, reference_count, reference_ids, source, license}]；
    上游问题进 degraded，且 hits 为空时必有 degraded 原因。
    """
    wid, reason = _resolve_work_id(doi, work_id, mailto=mailto, api_key=api_key)
    if reason:
        return ConnectorResult(degraded=[reason])
    params = {"select": "id,referenced_works_count,referenced_works"}
    if api_key:
        params["api_key"] = api_key
    elif mailto:
        params["mailto"] = mailto
    url = f"{OPENALEX_API}/{wid}?{urllib.parse.urlencode(params)}"
    try:
        data = _http_json(url, timeout=CITATION_TIMEOUT, headers=_polite_headers(mailto))
    except Exception as exc:  # noqa: BLE001
        return ConnectorResult(degraded=[_classify_http_error(exc, "openalex", "references")])
    refs = data.get("referenced_works") or []
    return ConnectorResult(
        hits=[
            {
                "work_id": wid,
                "reference_count": data.get("referenced_works_count"),
                "reference_ids": [str(r).rstrip("/").split("/")[-1] for r in refs],
                "source": "openalex",
                "license": "CC0",
            }
        ]
    )


def openalex_citations(doi: str = "", work_id: str = "", limit: int = 10, *,
                       mailto: str = "", api_key: str = "") -> ConnectorResult:
    """反向引用图：哪些作品引用了该作品（filter=cites）。

    返回 hits=施引作品列表（每条带 total_count）；上游问题进 degraded。
    """
    wid, reason = _resolve_work_id(doi, work_id, mailto=mailto, api_key=api_key)
    if reason:
        return ConnectorResult(degraded=[reason])
    params = {
        "filter": f"cites:{wid}",
        "per-page": str(min(limit, CITATION_LIMIT_MAX)),
        "select": "id,title,publication_year",
    }
    if api_key:
        params["api_key"] = api_key
    elif mailto:
        params["mailto"] = mailto
    url = f"{OPENALEX_API}?{urllib.parse.urlencode(params)}"
    try:
        data = _http_json(url, timeout=CITATION_TIMEOUT, headers=_polite_headers(mailto))
    except Exception as exc:  # noqa: BLE001
        return ConnectorResult(degraded=[_classify_http_error(exc, "openalex", "citations")])
    out = []
    total = (data.get("meta") or {}).get("count")
    for w in data.get("results") or []:
        out.append(
            {
                "citing_work_id": str(w.get("id", "")).rstrip("/").split("/")[-1],
                "title": w.get("title"),
                "year": w.get("publication_year"),
                "total_count": total,
                "source": "openalex",
                "license": "CC0",
            }
        )
    return ConnectorResult(hits=out)


def arxiv_search(query: str, limit: int = 10, *, throttler=None, fetcher=None) -> ConnectorResult:
    """arXiv Export API。遵守 1 req/3s（调用方注入 throttler）。"""
    if throttler is not None:
        throttler.wait()
    url = (
        f"{ARXIV_API}?search_query=all:{urllib.parse.quote(query)}"
        f"&start=0&max_results={min(limit, 50)}"
    )
    try:
        if fetcher is not None:
            root_text = fetcher(url)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "fd-text-core/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                root_text = r.read()
    except Exception as exc:  # noqa: BLE001
        return ConnectorResult(degraded=[f"arxiv: {type(exc).__name__}: {exc}"])
    import xml.etree.ElementTree as ET

    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(root_text)
    except ET.ParseError as exc:
        return ConnectorResult(degraded=[f"arxiv: 响应不可解析 {exc}"])
    out = []
    for e in root.findall("a:entry", ns):
        link_el = e.find("a:id", ns)
        arxiv_id = (link_el.text or "").rsplit("/", 1)[-1] if link_el is not None else ""
        out.append(
            {
                "source": "arxiv",
                "arxiv_id": arxiv_id,
                "title": (e.findtext("a:title", default="", namespaces=ns) or "").strip(),
                "abstract": (e.findtext("a:summary", default="", namespaces=ns) or "").strip(),
                "published": e.findtext("a:published", default="", namespaces=ns),
                "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "",
            }
        )
    return ConnectorResult(hits=out)


def openalex_search(query: str, limit: int = 10, *, mailto: str = "", api_key: str = "") -> ConnectorResult:
    params = {"search": query, "per-page": str(min(limit, 50))}
    if api_key:
        params["api_key"] = api_key
    elif mailto:  # polite pool
        params["mailto"] = mailto
    url = f"{OPENALEX_API}?{urllib.parse.urlencode(params)}"
    try:
        data = _http_json(url)
    except Exception as exc:  # noqa: BLE001
        return ConnectorResult(degraded=[f"openalex: {type(exc).__name__}: {exc}"])
    out = []
    for w in data.get("results", []):
        out.append(
            {
                "source": "openalex",
                "doi": w.get("doi"),
                "title": w.get("title") or w.get("display_name") or "",
                "year": w.get("publication_year"),
                "cited_by": w.get("cited_by_count"),
                "url": w.get("doi") or (w.get("primary_location") or {}).get("landing_page_url") or "",
            }
        )
    return ConnectorResult(hits=out)


def semantic_scholar_search(query: str, limit: int = 10, *, api_key: str = "") -> ConnectorResult:
    params = {"query": query, "limit": str(min(limit, 50)), "fields": "title,abstract,year,externalIds,url,authors"}
    url = f"{S2_API}?{urllib.parse.urlencode(params)}"
    headers = {"x-api-key": api_key} if api_key else {}
    try:
        data = _http_json(url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return ConnectorResult(degraded=[f"semantic_scholar: {type(exc).__name__}: {exc}"])
    out = []
    for p in data.get("data", []):
        ext = p.get("externalIds") or {}
        out.append(
            {
                "source": "semantic_scholar",
                "arxiv_id": ext.get("ArXiv"),
                "doi": ext.get("DOI"),
                "title": p.get("title") or "",
                "abstract": p.get("abstract") or "",
                "year": p.get("year"),
                "url": p.get("url") or "",
            }
        )
    return ConnectorResult(hits=out)


def federated_search(
    query: str,
    limit: int = 10,
    *,
    use: tuple[str, ...] = ("arxiv", "openalex", "semantic_scholar"),
    throttler=None,
    openalex_mailto: str = "",
    openalex_key: str = "",
    s2_key: str = "",
) -> ConnectorResult:
    """三源编排：任一失联只记 degraded，不影响其余。"""
    merged: list[dict] = []
    degraded: list[str] = []
    if "arxiv" in use:
        r = arxiv_search(query, limit, throttler=throttler)
        merged += r.hits
        degraded += r.degraded
    if "openalex" in use:
        r = openalex_search(query, limit, mailto=openalex_mailto, api_key=openalex_key)
        merged += r.hits
        degraded += r.degraded
    if "semantic_scholar" in use:
        r = semantic_scholar_search(query, limit, api_key=s2_key)
        merged += r.hits
        degraded += r.degraded
    return ConnectorResult(hits=merged, degraded=degraded)


class _ArxivThrottler:  # 轻量 1 req/3s（与 harvest.Throttler 同语义，避免跨层依赖）
    def __init__(self, interval: float = 3.0) -> None:
        self.interval = interval
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        gap = self.interval - (now - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = max(self._last + self.interval, now) if self._last else time.monotonic()
