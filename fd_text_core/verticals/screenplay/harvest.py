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

# 版权判定：**不能**扫裸词 "copyright"——每份 Project Gutenberg 文件的
# 许可证样板都含该词，会 100% 误杀公版文本（2026-10-11 实测：Hamlet 等
# 也被拦）。改为两条窄规则：
#   ① 拒收标记：现代版权页的明确声明（"All rights reserved" 独立成行、
#      "版权所有" 等中文声明）——注意 PG 早期印刷版的 "_All rights reserved_"
#      是原书扉页字样，公版书也会有，故必须结合年份/现代格式判断；
#   ② PG 文件以其官方头判定：头 500 字内的 "in the public domain" 明示放行。
REJECT_MARKERS = ("版权所有", "保留所有权利", "严禁转载", "未经授权不得")
_PG_PUBLIC_DOMAIN = ("in the public domain", "not protected by u.s. copyright")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


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
    """判定是否仍在版权期内。命中即拒收，不静默入库。

    PG 文件先看官方声明：头 6000 字内出现 "in the public domain" 即放行
    （这是 PG 自己对每本书的权属结论，比关键词猜测权威）。
    其余来源扫窄规则拒收标记；中文标记命中即拒。
    """
    head = text[:6000]
    low = head.lower()
    if any(m in low for m in _PG_PUBLIC_DOMAIN):
        return False
    if any(m in head for m in REJECT_MARKERS):
        return True
    # 现代版权页形态："©" + 年份（单说 © 太宽，古典文本转录里也可能出现）
    if "©" in head and _YEAR_RE.search(head):
        return True
    return False


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


def _wikisource_api(params: dict, fetcher: Callable | None) -> dict:
    """带退避的 API 调用：实测 Wikisource 对连发请求会回 429，
    SSL 也会偶发 UNEXPECTED_EOF 瞬断——两者都是可重试的瞬时故障。"""
    import json
    import urllib.parse

    url = "https://zh.wikisource.org/w/api.php?" + urllib.parse.urlencode(params)
    get = fetcher or _get
    last: Exception | None = None
    for attempt in range(4):
        if attempt:
            time.sleep(2.0 * attempt)  # 2s/4s/6s 退避
        try:
            return json.loads(get(url))
        except Exception as exc:  # URLError / HTTPError 429 / SSL EOF
            last = exc
    raise HarvestError(f"wikisource API 重试 4 次仍失败: {last}")


def _wikisource_extract(title: str, fetcher: Callable | None) -> tuple[str, str]:
    """取单页纯文本。返回 (真实标题, 正文)。"""
    data = _wikisource_api(
        {
            "action": "query", "prop": "extracts", "explaintext": "1",
            "format": "json", "titles": title, "redirects": "1",
        },
        fetcher,
    )
    for _pid, pg in ((data.get("query") or {}).get("pages") or {}).items():
        return pg.get("title") or title, pg.get("extract") or ""
    return title, ""


# 多卷本页面（如 西廂記/第1本）单页只有一卷；正文页 < 2000 字时
# 按 prefixsearch 找同名前缀的卷册页并拼接。阈值实测：整本「全覽」页
# 8 万+ 字，单卷 9 千字，目录壳 80-283 字——2000 能干净分开后两者。
_WIKISOURCE_FULL_MIN = 2000


def fetch_wikisource(title: str, *, polite: Polite | None = None, fetcher: Callable | None = None) -> Play:
    """维基文库中文古典剧本（元杂剧/明清传奇）。

    三层取文策略（实测于 2026-10-11）：
      1. `<title> (全覽)` 整本页 —— 明清传奇（牡丹亭等）的权威全文形态
      2. 单页正文 >= 2000 字 —— 元杂剧（竇娥冤等）单页即全文
      3. 分卷拼接 —— 西廂記/第N本 形态，按前缀检索各卷后按卷序拼接

    ctext.org 实测挂在 Cloudflare Turnstile 挑战之后（静态 GET 只拿到
    "Checking the security of your connection..."，2781 字节空壳），
    故中文古典走维基文库——同为公版且可直接取文。
    """
    p = polite or Polite()
    p.wait()

    # 1) 整本页优先
    real_title, text = _wikisource_extract(f"{title} (全覽)", fetcher)
    if len(text) < _WIKISOURCE_FULL_MIN:
        # 2) 单页
        real_title, text = _wikisource_extract(title, fetcher)

    if len(text) < _WIKISOURCE_FULL_MIN:
        # 3) 分卷拼接
        data = _wikisource_api(
            {"action": "query", "list": "prefixsearch", "pssearch": title,
             "pslimit": "20", "format": "json"},
            fetcher,
        )
        vols = [
            r["title"]
            for r in ((data.get("query") or {}).get("prefixsearch") or [])
            if r["title"].startswith(f"{title}/")
        ]
        # 卷序：第N本 / 卷N 均可，按 N 数值升序
        def vol_no(t: str) -> int:
            m = re.search(r"(?:第|卷)(\d+)", t)
            return int(m.group(1)) if m else 0

        vols.sort(key=vol_no)
        parts = []
        for v in vols:
            p.wait()
            _rt, vt = _wikisource_extract(v, fetcher)
            if len(vt) >= _WIKISOURCE_FULL_MIN:
                parts.append(vt)
        if parts:
            text = "\n\n".join(parts)
            real_title = title

    if len(text) < _WIKISOURCE_FULL_MIN:
        raise HarvestError(
            f"wikisource 未取到正文（{len(text)} 字，疑似只拿到目录壳）: {title}"
        )
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
