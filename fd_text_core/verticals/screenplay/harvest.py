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
from collections.abc import Callable
from dataclasses import dataclass, field

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
    return "©" in head and bool(_YEAR_RE.search(head))


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
    m = re.search(r"^Title:\s*(.+)$", text, re.MULTILINE)
    if m:
        title = m.group(1).strip()
    return Play(
        source="gutenberg", source_id=str(book_id), title=title or f"gutenberg-{book_id}",
        text=text, language="en", tradition="western-classic", period="early-modern",
        license_note="public-domain (Project Gutenberg)",
    )


def _wikisource_api(params: dict, fetcher: Callable | None) -> dict:
    """带退避的 API 调用：实测 Wikisource 对连发请求会回 429，
    SSL 也会偶发 UNEXPECTED_EOF 瞬断——两者都是可重试的瞬时故障。
    429 是限流（非永久拒绝），故退避要够长：5/10/20/40s——短退避会在
    密集抓取时连续撞限，实测 2s/4s/6s 不够（2026-10-11）。"""
    import json
    import urllib.parse

    url = "https://zh.wikisource.org/w/api.php?" + urllib.parse.urlencode(params)
    get = fetcher or _get
    last: Exception | None = None
    for attempt in range(5):
        if attempt:
            time.sleep(5.0 * (2 ** (attempt - 1)))  # 5/10/20/40s
        try:
            return json.loads(get(url))
        except Exception as exc:  # URLError / HTTPError 429 / SSL EOF  # noqa: BLE001
            last = exc
    raise HarvestError(f"wikisource API 重试 5 次仍失败: {last}")


def _wikisource_extract(title: str, fetcher: Callable | None) -> tuple[str, str]:
    """取单页纯文本。返回 (真实标题, 正文)。

    走 `action=parse`（取模板渲染后的 HTML 再转文本），**不**用
    `prop=extracts&explaintext`——实测后者对挂了 `{{header2}}` 等模板的页面
    只吐出正文开头一小段（清空为模板处截断）：面子問題/第一幕 explaintext
    仅 255 字，而 parse 渲染后 13163 字。原因：extract 基于 wikitext 的
    纯文本化，模板内的正文被当作模板参数吞掉；parse 全展开后不丢。
    页面不存在时 parse 返回 error，此时回落 explaintext（保留其空页处理）。
    """
    data = _wikisource_api(
        {"action": "parse", "page": title, "prop": "text", "format": "json", "redirects": "1"},
        fetcher,
    )
    parsed = data.get("parse") or {}
    if parsed.get("error") is None and parsed.get("text"):
        html = parsed["text"]
        html = html.get("*") if isinstance(html, dict) else html
        real_title = (parsed.get("title") or title)
        return real_title, html_to_text(html or "")
    # 回落：extracts 路径（旧行为；页面不存在时返回空串）
    data = _wikisource_api(
        {
            "action": "query", "prop": "extracts", "explaintext": "1",
            "format": "json", "titles": title, "redirects": "1",
        },
        fetcher,
    )
    for pg in ((data.get("query") or {}).get("pages") or {}).values():
        return pg.get("title") or title, pg.get("extract") or ""
    return title, ""


# 多卷本页面（如 西廂記/第1本）单页只有一卷；正文页 < 2000 字时
# 按 prefixsearch 找同名前缀的卷册页并拼接。阈值实测：整本「全覽」页
# 8 万+ 字，单卷 9 千字，目录壳 80-283 字——2000 能干净分开后两者。
_WIKISOURCE_FULL_MIN = 2000

# 中文数字（老舍话剧的分幕标题用「第一幕/第三場」形态；元明戏曲用「第1本」）。
# 现有 vol_no 只认 ASCII 数字 → 「茶馆/第三幕」被解析为 0，拼接顺序随机
# （2026-10-11 实测）。两套编号都要能解析。
_CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s: str) -> int | None:
    """「三」→3、「十二」→12、「二十四」→24；不可解析返回 None。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, section = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        elif ch == "十":
            section = (section or 1) * 10
        elif ch == "百":
            total += (section or 1) * 100
            section = 0
        else:
            return None
    return total + section


def _vol_no(t: str) -> int:
    """从子页标题解析序号：「第3本」/「第三幕」/「卷十二」均可，缺省 0。

    序号用于拼接排序——解析失败会让卷序错乱，故中英双形态都要覆盖。
    """
    m = re.search(r"(?:第|卷)([0-9]+|[〇零一二两三四五六七八九十百]+)", t)
    if not m:
        return 0
    return _cn_to_int(m.group(1)) or 0


def _wikisource_subpages(title: str, fetcher: Callable | None) -> list[str]:
    """枚举 `<title>/…` 子页（实测：老舍话剧的每幕/每场是独立子页，
    主页面只是一张 36 字的目录壳——三层策略的「单页」层拿到它会被判为
    目录壳拒收）。

    带消歧括号的标题（「面子問題 (老舍)」）prefixsearch 直接搜全名匹配不到，
    要用括号前的基名（实测：搜「面子問題」才返回 `面子問題 (老舍)/第一幕`）。
    故两种查询串都试，按 `<title>/` 前缀过滤去重。
    """
    out: dict[str, None] = {}
    base = title.split(" (")[0]
    seen_queries: set[str] = set()
    for q in (title, base):
        if q in seen_queries:
            continue
        seen_queries.add(q)
        data = _wikisource_api(
            {"action": "query", "list": "prefixsearch", "pssearch": q,
             "pslimit": "50", "format": "json"},
            fetcher,
        )
        for r in (data.get("query") or {}).get("prefixsearch") or []:
            t = r["title"]
            if t.startswith(f"{title}/"):
                out.setdefault(t, None)
    return list(out)


def fetch_wikisource(title: str, *, polite: Polite | None = None, fetcher: Callable | None = None) -> Play:
    """维基文库中文剧本（元杂剧/明清传奇/现代话剧）。

    取文策略（实测于 2026-10-11）：先取三个候选，再取**最长者**——
      1. `<title> (全覽)` 整本页 —— 明清传奇（牡丹亭等）的权威全文形态
      2. 主页面单页
      3. 子页拼接 —— `西廂記/第N本`（分卷）与 `茶馆/第一幕`（分幕）同一枚举，
         卷序解析器同时认「第N本」与「第三幕」（中文数字），序言/人物表类
         无编号子页按 0 排最前

    「取最长」这个判据来自两个实测反例：荷珠配主页面是 2897 字的**序言**
    （超过 2000 阈值，若按「够长就用」会只入库序言、丢掉六场正文）；
    而茶馆主页面是 36 字目录壳。同一规则同时覆盖两种形态。

    ctext.org 实测挂在 Cloudflare Turnstile 挑战之后（静态 GET 只拿到
    "Checking the security of your connection..."，2781 字节空壳），
    故中文剧本走维基文库——同为公版且可直接取文。
    """
    p = polite or Polite()
    candidates: list[str] = []

    p.wait()
    _t1, full_page = _wikisource_extract(f"{title} (全覽)", fetcher)
    if full_page.strip():
        candidates.append(full_page)

    p.wait()
    real_title, main_page = _wikisource_extract(title, fetcher)
    if main_page.strip():
        candidates.append(main_page)

    vols = _wikisource_subpages(title, fetcher)
    if vols:
        vols.sort(key=_vol_no)
        parts = []
        for v in vols:
            p.wait()
            _rt, vt = _wikisource_extract(v, fetcher)
            if vt.strip():
                parts.append(vt)
        if parts:
            candidates.append("\n\n".join(parts))

    text = max(candidates, key=len) if candidates else ""
    if not real_title:
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
    m = re.search(r"<title>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
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
