"""公版剧本采集器：Gutenberg + ctext + 戏考（权属灰但如实标注）。

权属原则（spec screenplay-vertical）：
- 只收公有领域文本；在版权期内的现代影视/剧本杀剧本一律拒收并记录原因
- 戏考（xikao.com）为志愿者转录：底本公版但整理权属存疑 -> 收录但 provenance
  必须带转录来源与整理说明
采集遵守既有网络规则：海外源走代理、用前探测；礼貌限速（默认 1.5s/请求）。
"""
from __future__ import annotations

import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

UA = "fd-text-core/0.1 (public-domain corpus collection)"
DEFAULT_INTERVAL = 1.5

# 明确在版权期内 -> 拒收（演示用；真实清单由垂域维护）
COPYRIGHTED_MARKERS = ("©", "copyright", "all rights reserved", "版权所有，禁止转载")


class HarvestError(RuntimeError):
    """采集失败。"""


@dataclass
class Play:
    source: str
    source_id: str
    title: str
    text: str
    language: str = "zh"
    tradition: str = "chinese-classic"
    period: str = "ancient"
    provenance_extra: dict = field(default_factory=dict)
    license_note: str = "public-domain"

    @property
    def url(self) -> str:
        if self.source == "gutenberg":
            return f"https://www.gutenberg.org/ebooks/{self.source_id}"
        if self.source == "ctext":
            return f"https://ctext.org/{self.source_id}"
        if self.source == "wikisource":
            return "https://zh.wikisource.org/wiki/" + self.source_id
        if self.source == "xikao":
            return f"https://www.xikao.com/{self.source_id}"
        return ""


def _get(url: str, *, timeout: int = 45, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="ignore")


def looks_copyrighted(text: str) -> bool:
    """粗判是否仍在版权期内。命中即拒收，不静默入库。"""
    head = text[:4000].lower()
    return any(m.lower() in head for m in COPYRIGHTED_MARKERS)


_TAG = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    """极简 HTML -> 文本（够用即可；不引第三方解析依赖）。"""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", s)
    s = _TAG.sub(" ", s)
    s = (
        s.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


class Polite:
    """礼貌限速：相邻请求间隔 >= interval。"""

    def __init__(self, interval: float = DEFAULT_INTERVAL, sleep: Callable = time.sleep) -> None:
        self.interval = interval
        self._sleep = sleep
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        gap = self.interval - (now - self._last)
        if gap > 0:
            self._sleep(gap)
        self._last = max(self._last + self.interval, now) if self._last else time.monotonic()


def fetch_gutenberg(book_id: str, *, polite: Polite | None = None, fetcher: Callable | None = None) -> Play:
    """Gutenberg 纯文本。优先 /cache/epub/<id>/pg<id>.txt。"""
    p = polite or Polite()
    p.wait()
    url = f"https://www.gutenberg.org/cache/epub/{book_id}/pg{book_id}.txt"
    text = (fetcher or _get)(url)
    if looks_copyrighted(text):
        raise HarvestError(f"gutenberg {book_id} 疑似仍在版权期，拒收")
    title = ""
    m = re.search(r"^Title:\s*(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()
    return Play(
        source="gutenberg", source_id=str(book_id), title=title or f"gutenberg-{book_id}",
        text=text, language="en", tradition="western-classic", period="early-modern",
        license_note="public-domain (Project Gutenberg)",
    )


def fetch_wikisource(title: str, *, polite: Polite | None = None, fetcher: Callable | None = None) -> Play:
    """维基文库中文古典剧本（元杂剧/明清传奇），如 竇娥冤。

    ctext.org 实测挂在 Cloudflare Turnstile 挑战之后（静态 GET 只拿到
    "Checking the security of your connection..."，2781 字节空壳），
    故中文古典走维基文库——同为公版且可直接取文。
    """
    import urllib.parse

    p = polite or Polite()
    p.wait()
    url = "https://zh.wikisource.org/w/api.php?" + urllib.parse.urlencode(
        {
            "action": "query", "prop": "extracts", "explaintext": "1",
            "format": "json", "titles": title, "redirects": "1",
        }
    )
    raw = (fetcher or _get)(url)
    import json

    data = json.loads(raw)
    pages = (data.get("query") or {}).get("pages") or {}
    text = ""
    real_title = title
    for _pid, pg in pages.items():
        real_title = pg.get("title") or title
        text = pg.get("extract") or ""
        break
    if not text:
        raise HarvestError(f"wikisource 未取到正文: {title}")
    if looks_copyrighted(text):
        raise HarvestError(f"wikisource {title} 疑似仍在版权期，拒收")
    return Play(
        source="wikisource", source_id=title, title=real_title,
        text=text, language="zh", tradition="chinese-classic", period="ancient",
        license_note="public-domain (Wikisource / CC BY-SA 古籍原文)",
    )


def fetch_ctext(slug: str, *, polite: Polite | None = None, fetcher: Callable | None = None) -> Play:
    """ctext.org —— 注意：该站现挂 Cloudflare 挑战，静态请求取不到正文。

    保留入口以备其开放静态或走渲染通道；当前中文古典请改用 fetch_wikisource。
    """
    p = polite or Polite()
    p.wait()
    url = f"https://ctext.org/{slug}"
    html = (fetcher or _get)(url)
    if "Checking the security" in html or len(html) < 5000:
        raise HarvestError(
            f"ctext {slug} 命中 Cloudflare 挑战（静态取不到正文），请改用 wikisource"
        )
    text = html_to_text(html)
    if looks_copyrighted(html):
        raise HarvestError(f"ctext {slug} 疑似仍在版权期，拒收")
    return Play(
        source="ctext", source_id=slug, title=slug,
        text=text, language="zh", tradition="chinese-classic", period="ancient",
        license_note="public-domain (ctext.org classical text)",
    )


def fetch_xikao(page: str, *, polite: Polite | None = None, fetcher: Callable | None = None) -> Play:
    """戏考：底本公版但转录整理权属存疑 -> 收录并如实标注。

    实测剧本正文在 scripts.xikao.com/play/<id>（www.xikao.com 首页只有目录）。
    """
    p = polite or Polite()
    p.wait()
    if page.startswith("http"):
        url = page
    elif page.isdigit():
        url = f"https://scripts.xikao.com/play/{page}"
    else:
        url = f"https://www.xikao.com/{page}"
    html = (fetcher or _get)(url)
    text = html_to_text(html)
    if looks_copyrighted(html):
        raise HarvestError(f"xikao {page} 疑似仍在版权期，拒收")
    title = page
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if m:
        title = re.sub(r"\s*[-|]\s*.*$", "", m.group(1)).strip() or page
    return Play(
        source="xikao", source_id=page, title=title,
        text=text, language="zh", tradition="chinese-classic", period="modern",
        provenance_extra={
            "transcription_source": "戏考 xikao.com 志愿者转录",
            "rights_note": "底本公版；转录与整理工作权属存疑，仅内部索引处理，对外如实标注来源",
        },
        license_note="底本公版 / 转录权属存疑（如实标注）",
    )
