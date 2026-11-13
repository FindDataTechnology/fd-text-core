"""论文垂域采集器：arXiv OAI-PMH 元数据（CC0）+ 逐篇取 PDF/LaTeX 源。

限额合规（ToU）：OAI-PMH/Export 每 3 秒至多 1 请求、单连接；不得并发绕过。
本采集器以显式节流器实现该限额，并可注入 harvest 状态以支持断点续采。

全文仅内部索引处理，绝不自服务器分发——这是 arXiv 条款的红线（见 vertical 文档）。
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

OAI_BASE = "https://oaipmh.arxiv.org/oai"
NS = {"oai": "http://www.openarchives.org/OAI/2.0/"}
# 实测（2026-10-11）：arXivRaw 下的 id/title/authors/... 不带命名空间，
# 而外层 envelope 元素在 oai 命名空间里。二者不可混用同一前缀查找。
NS_RAW = {"arxiv": "http://arxiv.org/OAI/arXivRaw/"}
MIN_INTERVAL = 3.0  # 秒；ToU 硬限额

UA = "Mozilla/5.0 (compatible; fd-text-core/1.0)"
# 实测（2026-10-11）：ListRecords 单页动辄 2.5MB、服务端 150-190s 才吐完，
# 60s 默认超时必然读空。不带 from/until 的裸 set= 查询会被服务端挂住不返回，
# 故日期窗是必需参数而非可选优化（见 harvest_metadata 的 docstring）。
LIST_RECORDS_TIMEOUT = 240
#: e-print 单篇取源的超时。TeX 源多为几百 KB，但含图的大包可到几十 MB；
#: 给到 180s 足以覆盖实测慢尾，又不至于让一篇卡死整批（失败按篇降级）。
EPRINT_TIMEOUT = 180


class HarvestError(RuntimeError):
    """采集失败（限速/网络/解析）。"""


class EprintError(HarvestError):
    """单篇 e-print 取源失败。

    带 ``arxiv_id`` 与状态码，因为调用方要按篇降级并**如实记账**：
    只报"某次采集失败"会让"为什么这篇没有全文"无从追查——全文缺失的原因
    必须能落到具体论文与具体原因上（见 LICENSE-NOTE 与 Req2 的降级条款）。
    """

    def __init__(self, arxiv_id: str, reason: str, *, status: int | None = None) -> None:
        self.arxiv_id = arxiv_id
        self.reason = reason
        self.status = status
        super().__init__(f"e-print {arxiv_id} 取源失败: {reason}" + (f" (HTTP {status})" if status else ""))


@dataclass
class Paper:
    arxiv_id: str
    title: str
    abstract: str
    authors: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    license_url: str | None = None
    raw_meta: dict = field(default_factory=dict)

    @property
    def source_url(self) -> str:
        return f"https://arxiv.org/abs/{self.arxiv_id}"

    @property
    def fulltext_url(self) -> str:
        return f"https://arxiv.org/e-print/{self.arxiv_id}"


_AUTHOR_SPLIT = re.compile(r"\s*(?:,?\s+and\s+|;|,)\s*")


def parse_authors(node) -> list[str]:
    """arXivRaw 的作者有两种形态，都要认：

    ① 扁平文本：<authors>Jens Astor and C. Adami (CalTech)</authors>（arXivRaw 常见）
    ② 嵌套结构：<authors><author><keyname>..</keyname></author></authors>
    """
    if node is None:
        return []
    nested = node.findall("arxiv:author", NS_RAW)
    if nested:
        out = []
        for a in nested:
            name = " ".join(
                x for x in (_text(a, "arxiv:forenames", NS_RAW), _text(a, "arxiv:keyname", NS_RAW)) if x
            ).strip()
            if name:
                out.append(name)
        if out:
            return out
    raw = (node.text or "").strip()
    if not raw:
        return []
    # 去机构括注：先成对清除，再兜底截掉不成对的左括号（arXivRaw 里有未闭合写法）
    cleaned = re.sub(r"\([^)]*\)", "", raw)
    cleaned = cleaned.split("(", 1)[0]
    # 空白折叠（原文存在多空格）
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    parts = [p.strip() for p in _AUTHOR_SPLIT.split(cleaned) if p.strip()]
    return parts


class Throttler:
    """单连接节流：相邻请求间隔 >= MIN_INTERVAL。"""

    def __init__(self, interval: float = MIN_INTERVAL, sleep: Callable = time.sleep) -> None:
        self.interval = interval
        self._sleep = sleep
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        gap = self.interval - (now - self._last)
        if gap > 0:
            self._sleep(gap)
        # 记录「本次请求开始」的时刻，而非 sleep 之后的时刻，
        # 否则请求自身的耗时会吃掉间隔（实测间隔掉到 2.5s）。
        self._last = max(self._last + self.interval, now) if self._last else time.monotonic()


def _get(url: str, timeout: int = LIST_RECORDS_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_eprint(
    paper: Paper,
    *,
    throttler: Throttler | None = None,
    fetcher: Callable = _get,
    timeout: int = EPRINT_TIMEOUT,
) -> bytes:
    """取单篇 e-print 原始字节（LaTeX 源或 PDF-only 提交的 PDF）。

    为什么要单独一个函数：``Paper.fulltext_url`` 早就写好了，但没有任何调用点，
    于是"逐篇取 PDF/LaTeX 源"只活在模块 docstring 里。取源是限速管辖下的网络动作，
    必须走同一个 ``Throttler``（1 请求/3 秒、单连接），不能由调用方随手 ``urlopen``
    绕开——ToU 的限额是按请求算的，不是按接口算的。

    失败抛 ``EprintError``（带 ``status``），由调用方按篇降级为元数据+摘要，
    而不是让一篇 404 打断整批采集。
    """
    t = throttler or Throttler()
    t.wait()
    url = paper.fulltext_url
    try:
        return fetcher(url, timeout)
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        raise EprintError(paper.arxiv_id, f"HTTPError: {e}", status=getattr(e, "code", None)) from e
    except Exception as e:  # 按篇降级，不中断整批
        raise EprintError(paper.arxiv_id, f"{type(e).__name__}: {e}") from e


def _text(node, path: str, ns: dict | None = None) -> str:
    """取文本。arXivRaw 子树无命名空间，故默认不带 ns 查找。"""
    el = node.find(path, ns if ns is not None else {})
    if el is None and ns is None:  # 兼容带命名空间的历史形态
        el = node.find(path, NS_RAW)
    return "".join(el.itertext()).strip() if el is not None else ""


def harvest_metadata(
    set_spec: str = "cs",
    max_records: int = 20,
    *,
    throttler: Throttler | None = None,
    fetcher: Callable = _get,
    resumption_token: str | None = None,
    from_date: str | None = None,
    until_date: str | None = None,
) -> Iterator[tuple[list[Paper], str | None]]:
    """OAI-PMH ListRecords（arXivRaw）。产出 (批次, 下一个 resumption token)。

    from_date/until_date 是必需的：实测裸 `set=` 查询（无日期窗）服务端不返回
    响应，客户端会一直挂到超时。首请求必须带日期窗，续采走 resumptionToken。
    """
    if resumption_token is None and not (from_date and until_date):
        raise HarvestError(
            "arXiv OAI 的 ListRecords 首请求必须带 from/until 日期窗："
            "裸 set= 查询实测被服务端挂住不返回（本机与集群双出口均复现）"
        )
    t = throttler or Throttler()
    seen = 0
    while seen < max_records:
        t.wait()
        if resumption_token:
            url = f"{OAI_BASE}?verb=ListRecords&resumptionToken={resumption_token}"
        else:
            url = (
                f"{OAI_BASE}?verb=ListRecords&metadataPrefix=arXivRaw"
                + (f"&set={set_spec}" if set_spec else "")
                + f"&from={from_date}&until={until_date}"
            )
        raw = fetcher(url)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise HarvestError(f"OAI 响应解析失败: {exc}") from exc
        err = root.find("oai:error", NS)
        if err is not None:
            # noRecordsMatch 是「这天没东西」而非故障：逐日续采会常态化撞到，
            # 按空批次返回，让调用方继续下一天，而不是把整轮采集中断掉。
            if err.get("code") == "noRecordsMatch":
                yield [], None
                return
            raise HarvestError(f"OAI 错误 {err.get('code')}: {err.text}")
        papers: list[Paper] = []
        for rec in root.findall(".//oai:record", NS):
            meta = rec.find(".//arxiv:arXivRaw", NS_RAW)
            if meta is None:
                meta = rec.find(".//arXivRaw")  # 兼容无命名空间形态
            if meta is None:
                continue
            # arXivRaw 自身带命名空间，其子元素随之继承 ->
            # 一律用 arxiv: 前缀查找（实测裸名字 lookup 命中不了）
            aid = _text(meta, "arxiv:id", NS_RAW)
            if not aid:
                continue
            authors_node = meta.find("arxiv:authors", NS_RAW)
            authors = parse_authors(authors_node)
            primary = meta.find("arxiv:categories", NS_RAW)
            papers.append(
                Paper(
                    arxiv_id=aid,
                    title=_text(meta, "arxiv:title", NS_RAW) or "(untitled)",
                    abstract=_text(meta, "arxiv:abstract", NS_RAW),
                    authors=authors,
                    categories=[c.strip() for c in (primary.text or "").split() if c.strip()]
                    if primary is not None and primary.text
                    else [],
                    license_url=_text(meta, "arxiv:license", NS_RAW) or None,
                    raw_meta={"datestamp": _text(rec, "oai:datestamp", NS)},
                )
            )
            seen += 1
            if seen >= max_records:
                break
        token_el = root.find(".//oai:resumptionToken", NS)
        token = (token_el.text or "").strip() if token_el is not None and token_el.text else None
        yield papers, token
        if not token:
            return
        resumption_token = token


_TEX_RE = re.compile(r"\\begin\{document\}")


def looks_like_tex(blob: bytes) -> bool:
    """判断 e-print 是否为 LaTeX 源（arXiv 对 PDF-only 提交会直接给 PDF）。"""
    head = blob[:8192]
    return bool(_TEX_RE.search(head.decode("utf-8", "ignore"))) or b"\\documentclass" in blob[:4096]
