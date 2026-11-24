"""公文域解析器：干净文本 -> 公文结构树。

设计依据（2026-10-13 **两轮**真实样本实取验证，夹具在 tests/fixtures/gov/）：
  第一轮（2 样本，定义模板差异）：
  · 自然资源部公告（mnr_notice_2026.html）：元数据是 **HTML 表格**形态，
    `<td>标签</td><td>值</td>` 相邻对，含 名称/索引号/主送/发文字号/成文日期/发布机构
  · 财政部通知（mof_treasury_notice_2026.html）：**TRS CMS** 模板，无元数据表，
    文号与日期藏在正文内（如「财办债〔2026〕218号」「2026年10月10日」）
  第二轮（20 页面抽样，抓出 5 个第一轮夹具掩盖的缺陷，见下）：新增
  · ndrc_notice_cms_2026.html —— 发改委模板：`<title>` 带 CMS 装饰（【标题】-站点名），
    但 `<meta name="ArticleTitle">` 是干净的
  · mof_order_116_2024.html —— 财政部令模板：层级是「第X条」而非「一、」，
    日期在正文第二行（公布日期），正文首句另有审议通过日期
  · mnr_www_notice_2026.html —— www.mnr.gov.cn 模板：正文在 `id="content1"` 内，
    外层 `ky_article` 只有标题与分享按钮（曾误取 90 字符而丢掉正文）

第二轮修出的 5 个缺陷（都是「夹具过拟合」型，只有上真实分布才暴露）：
  1. 正文容器误选：候选顺序把泛化的 `article|detail|zoom` 排在专用容器前，
     且「>50 字符即接受」的门限太松 → 选到只有页头的容器，正文全丢
  2. 成文日期取「正文第一个日期」→ 拿到正文里引用的历史日期/反馈截止日，
     甚至出现未来日期（实测 2026-10-31 > 抓取日 2026-10-13）
  3. 标题取 `<title>` → 发改委模板带「【】-站点名」装饰
  4. 发布机构落款后缀表过窄（缺 司/厅/中心 等）→ 8 个缺失样本里 4 个本可救回
  5. 文号只认「〔〕」形态 → 部委令/公告的「财政部令第116号」形态抽不到

故本解析器采用「标签驱动 + 多候选兜底」而非固定选择器：
  1. 元数据：先找标签-值表格对；找不到则从正文首尾用正则抽文号与日期
  2. 正文容器：多候选按「专用优先」排序，逐个取平衡 div 并**过滤页脚/导航杂质**
  3. 附件：按 pdf/docx/xlsx 后缀链接识别（各模板一致）

字段优先级（实测确定，非猜测）：
  · 标题：`<meta ArticleTitle>` -> `<title>`（去【】装饰）-> 元数据表「名称」
  · 成文日期：正文末「独立成行的日期」= 落款成文日期 -> 元数据表 -> 正文首个日期
    （落款优先的理由：公文自身的成文日期以落款为准；实测有 1/20 样本站点元数据表
      与落款不一致，此时以落款为准，站点值另行保留在 metadata 里可追溯）
  · 发布机构：元数据表 -> 标题「中华人民共和国XX令/公告」前缀 -> 落款行

**结构抽取边界**（spec `text-vertical-onboarding` 要求显式声明，见 STABLE_FIELDS /
UNSTABLE_FIELDS）：只抽取可从 HTML 稳定获取的元素，不对不可获取元素做启发式硬凑。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------- 结构抽取边界声明（供质量门与验收逐条核对）----------

STABLE_FIELDS = {
    "title": "标题（<meta ArticleTitle> / 元数据表「名称」/ <title>，去站点装饰）",
    "doc_number": "发文字号（〔〕形态如 财办债〔2026〕218号；编号形态如 财政部令第116号）",
    "issuer": "发布机构（元数据表标签，或标题「中华人民共和国XX令/公告」前缀，或正文落款）",
    "addressee": "主送机关（存在时；公告类文种按体例无此项）",
    "date": "成文日期（落款独立成行日期优先，元数据表兜底）",
    "index_no": "索引号（部分部委有）",
    "doc_type": "文种（通知/公告/批复/函/意见/命令，从标题尾部词或编号形态判定）",
    "sections": "正文层级（一、（一）、1.；条例/办法类为「第X条」）",
    "attachments": "附件清单（pdf/docx/xlsx 链接）",
    "cited_docs": "正文中引用的其它公文文号",
}

UNSTABLE_FIELDS = {
    "seal": "印章——图像，非文本",
    "cc_and_issue": "抄送与印发信息——常在页脚或图片中",
    "subject_terms": "主题词——2012 年《党政机关公文处理工作条例》起已不再要求，不建模",
}

# 文种：标题尾部常见形态。末尾的「公示」是实测补入——部委公告栏里「XX结果的公示」
# 占相当比例（20 页抽样里 3 例），它虽非法定文种但确是稳定的文类标记，故建模。
# 「条例/办法/规定/细则/方案/规划」为政策文件库高频文种（gov.cn 政策库大量以这些结尾），
# 补入以覆盖政策语料；与 verticals/gov/__init__.py::FACETS["doc_type"] 必须同源。
_DOC_TYPES = (
    "通知", "公告", "批复", "函", "意见", "决定", "命令", "通报", "报告", "请示", "纪要",
    "公示", "复函", "条例", "办法", "规定", "细则", "方案", "规划",
)

# 发文字号：机关代字 + 〔年份〕 + 序号 + 号。兼容 〔〕[]［］ 与全角数字
_RE_DOC_NUMBER = re.compile(r"([\u4e00-\u9fff]{1,12}?[〔\[［]\s*(\d{4})\s*[〕\]］]\s*(\d+)\s*号)")
# 编号形态（部委令/公告）：财政部令第116号 / 财政部公告2025年第8号 / 自然资源部公告2026年第15号
_RE_NUMBERED_DOC = re.compile(
    r"((?:中华人民共和国)?[\u4e00-\u9fff]{2,12}?(?:令|公告)\s*(?:\d{4}\s*年\s*)?第?\s*\d+\s*号)"
)
# 成文日期：2026年10月10日
_RE_DATE_CN = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# 独立成行的日期（落款形态：整行只有日期，无「于…前」等修饰）
_RE_STANDALONE_DATE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")
# 条例/办法类的层级是「第X条」（令、办法、条例体裁）
_RE_SEC_TIAO = re.compile(r"^\s*(第[一二三四五六七八九十百]+条)\s*(.*)$")
# 正文层级：一、/（一）/1. ——只认行首
_RE_SEC_L1 = re.compile(r"^\s*([一二三四五六七八九十百]+)\s*[、.．]\s*(.*)$")
_RE_SEC_L2 = re.compile(r"^\s*[（(]\s*([一二三四五六七八九十百]+)\s*[）)]\s*(.*)$")
_RE_SEC_L3 = re.compile(r"^\s*(\d{1,3})\s*[.．、]\s*(.*)$")

# 元数据标签别名表：不同部委用词不同，归一到统一字段名
_LABEL_ALIASES = {
    "名称": "title",
    "标题": "title",
    "索引号": "index_no",
    "发文字号": "doc_number",
    "文号": "doc_number",
    "主送": "addressee",
    "主送机关": "addressee",
    "成文日期": "date",
    "发布日期": "date",
    "发布机构": "issuer",
    "发文机关": "issuer",
    "体裁": "doc_type",
    "文种": "doc_type",
}

# 正文容器候选：**专用容器优先**，泛化 class 放最后。
# 实测教训：泛化的 `article|detail|zoom` 若排在前面，会命中只含标题与分享按钮的
# 外层容器（www.mnr.gov.cn 的 ky_article：90 字符），正文全丢且无任何报错。
_BODY_CONTAINER_PATTERNS = (
    re.compile(r'<div[^>]*id="content"[^>]*>', re.IGNORECASE),
    re.compile(r'<div[^>]*id="content1"[^>]*>', re.IGNORECASE),
    re.compile(r'<div[^>]*id="doccon"[^>]*>', re.IGNORECASE),
    re.compile(r'<div[^>]*class="[^"]*TRS_Editor[^"]*"[^>]*>', re.IGNORECASE),
    re.compile(r'<div[^>]*class="[^"]*box_content[^"]*"[^>]*>', re.IGNORECASE),
    re.compile(r'<div[^>]*id="Zoom"[^>]*>', re.IGNORECASE),
    re.compile(r'<div[^>]*class="[^"]*(?:article_con|detail_con|zoom)[^"]*"[^>]*>', re.IGNORECASE),
    re.compile(r'<div[^>]*class="[^"]*(?:article|detail)[^"]*"[^>]*>', re.IGNORECASE),
)

# 站点框架杂质：候选容器里出现这些词说明选到了页脚/导航/排行榜而非正文。
# 命中即拒绝该候选并继续尝试下一个（选错容器是静默丢正文，比报错危险）。
_CHROME_MARKERS = ("网站地图", "京ICP备", "版权所有", "国务院部门网站", "主办：", "排行榜", "政府网站标识码")

# 站点装饰：发改委模板的 <title> 形如「【正文标题】-国家发展和改革委员会」
_RE_TITLE_DECOR_HEAD = re.compile(r"^\s*【(.+?)】\s*")
_RE_TITLE_DECOR_TAIL = re.compile(r"\s*[-—－]\s*[\u4e00-\u9fff]{2,20}(?:网站|委员会|部|局|厅|政府)\s*$")

# 落款机关名后缀。按实测 20 页补齐（原表缺 司/厅/中心，8 个缺失样本里 4 个本可救回）
_ORG_SUFFIX = re.compile(
    r"(部|委|委员会|总局|总署|署|局|厅|政府|办公厅|办公室|公署|院|中心|司|处|科|行|会|组|社)$"
)

_RE_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_ATTR_HREF = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_RE_ATTACH_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|rar)$", re.IGNORECASE)


@dataclass
class Attachment:
    """附件：只登记不解析（见 design D4）。"""

    name: str
    url: str
    kind: str


@dataclass
class GovDoc:
    """公文结构树。字段为 None 表示该文档确实无此项（非解析失败）。"""

    title: str | None = None
    doc_number: str | None = None
    issuer: str | None = None
    addressee: str | None = None
    date: str | None = None
    index_no: str | None = None
    doc_type: str | None = None
    body: str = ""
    sections: list[tuple[str, str]] = field(default_factory=list)  # (层级路径, 文本)
    attachments: list[Attachment] = field(default_factory=list)
    cited_docs: list[str] = field(default_factory=list)
    source_note: str = ""
    site_date: str | None = None  # 站点元数据表日期；与落款不一致时保留以便追溯
    issuer_raw: str | None = None  # 归一前的原始机关名（可追溯，不参与 facet 过滤）

    def section_texts(self) -> list[tuple[str, str]]:
        """切块策略消费的形态：[(层级路径, 文本)]（照剧本 parser 的契约）。"""
        return self.sections or [("", self.body)]


def _clean_text(html: str) -> str:
    """去脚本/样式与标签，归一空白。保留段落边界（\\n）以便层级识别。"""
    s = _RE_SCRIPT_STYLE.sub(" ", html)
    # 块级标签转换行，保住段落
    s = re.sub(r"</?(?:p|div|br|tr|li|h[1-6])[^>]*>", "\n", s, flags=re.IGNORECASE)
    s = _RE_TAG.sub("", s)
    s = (
        s.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _extract_labeled_values(html: str) -> dict[str, str]:
    """从 `<td>标签</td><td>值</td>` 相邻对抽元数据（照 labeledValues 算法思路）。

    实测形态（自然资源部）：`<td class="hui"><p ...>索引号</p></td><td>000019174/2026-00027</td>`
    —— 标签在 td 内可能包一层 p/span，值可能被 HTML 注释包裹，故先清洗再配对。
    值里的 `<!--...-->` 注释与多余空白一并剥掉。
    """
    out: dict[str, str] = {}
    tds = re.findall(r"<td[^>]*>(.*?)</td>", html, re.IGNORECASE | re.DOTALL)
    for i in range(len(tds) - 1):
        label = _clean_text(tds[i]).replace(" ", "")
        value = _clean_text(tds[i + 1])
        # 标签归一：去掉 letter-spacing 造成的字间空格后比对别名表
        for alias, fieldname in _LABEL_ALIASES.items():
            if label == alias or label.replace("　", "") == alias:
                if value and fieldname not in out:
                    out[fieldname] = value
                break
    return out


def _find_body_container(html: str) -> str:
    """按候选优先级找正文容器，返回其内部 HTML（平衡标签截取）。

    两级拒绝，防「静默丢正文」：
      · 长度门限 50 字符 —— 太短说明抓错了容器
      · 站点框架杂质标记 —— 候选里出现页脚/导航词说明选到了框架而非正文

    实测教训（第二轮 20 页抽样）：www.mnr.gov.cn 的正文在 `id="content1"`，
    而外层 `ky_article` 只有标题与分享按钮（90 字符，**过了长度门限**）。
    旧候选表把泛化的 `article|detail|zoom` 排在前面且无杂质检查，
    于是整篇正文被静默丢弃、无任何报错。故：专用容器优先 + 杂质即拒绝。
    """
    fallback = ""
    for pat in _BODY_CONTAINER_PATTERNS:
        m = pat.search(html)
        if not m:
            continue
        start = m.end()
        # 平衡 div 截取：从容器开标签后开始数嵌套
        depth = 1
        pos = start
        for tag in re.finditer(r"<(/?)div\b[^>]*>", html[start:], re.IGNORECASE):
            if tag.group(1):
                depth -= 1
            else:
                depth += 1
            if depth == 0:
                pos = start + tag.start()
                break
        else:
            pos = len(html)
        inner = html[start:pos]
        text = _clean_text(inner)
        if len(text) <= 50:
            continue
        if any(mark in text for mark in _CHROME_MARKERS):
            # 选到页脚/导航：记住最长的候选做兜底，但优先继续找专用容器
            if len(text) > len(fallback):
                fallback = text
            continue
        return inner
    if fallback:
        return fallback
    return html  # 兜底：全文（清洗后仍是正文，杂质在入库前由调用方判定）


def _normalize_date(value: str | None) -> str | None:
    """成文日期归一到 YYYY-MM-DD。中文形态（2026年06月25日）与已 ISO 的原样通过。

    统一格式的意义：facet 的 period 过滤与时间线预设都依赖可比较的日期；
    各部委原始形态不一（自然资源部用「2026年06月25日」，财政部页内用「2026年8月25日」）。
    """
    if not value:
        return None
    v = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    m = _RE_DATE_CN.search(v)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return v or None


def _clean_title(raw: str | None) -> str | None:
    """去站点装饰后的标题。

    发改委模板的 `<title>` 形如「【关于印发…的通知】-国家发展和改革委员会」——
    装饰部分是站点名，不是标题。`<meta ArticleTitle>` 是干净的，优先取它；
    只有 `<title>` 可用时才剥装饰。
    """
    if not raw:
        return None
    t = _clean_text(raw)
    m = _RE_TITLE_DECOR_HEAD.match(t)
    if m:
        t = m.group(1).strip()
    t = _RE_TITLE_DECOR_TAIL.sub("", t).strip()
    return t or None


def _meta_content(html: str, name: str) -> str | None:
    """取 `<meta name="X" content="...">` 的值（属性顺序两种都容忍）。"""
    pat = re.compile(
        rf'<meta[^>]+name=["\']{name}["\'][^>]*content=["\']([^"\']*)["\']', re.IGNORECASE
    )
    m = pat.search(html)
    if not m:
        pat2 = re.compile(
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*name=["\']{name}["\']', re.IGNORECASE
        )
        m = pat2.search(html)
    return (m.group(1).strip() or None) if m else None


def _closing_date(body: str) -> str | None:
    """落款成文日期：正文中**独立成行**的日期，取最后一个。

    为什么不取「第一个日期」：公文正文里常引用历史日期（引用文件成文日）、
    或写反馈截止日（实测出现 2026-10-31，晚于抓取日 2026-10-13 的假未来日期）。
    公文的成文日期以落款为准，而落款在正文末尾且独立成行。
    """
    hits = [
        ln.strip()
        for ln in body.split("\n")
        if _RE_STANDALONE_DATE.fullmatch(ln.strip())
    ]
    return hits[-1] if hits else None


def _normalize_issuer(raw: str | None) -> str | None:
    """发布机构归一到 facet 词表可用的机关名。

    为什么必须归一（实测 209 篇的真实分布）：原始值有 51 种形态，同一条目
    在不同页面写作「财政部」「财 政 部」（字间全角空格）、「财政部办公厅」
    「财政部会计司」「财政部债务管理司」——若不归一，按机关过滤会漏掉大半
    语料，而 facet 词表要么被迫列 51 个值（且每次采集都要改词表）要么形同虚设。

    归一规则（只做**可逆的信息保留**：原文另存 metadata.issuer_raw）：
      · 去字间空白（实测有「国 家 能 源 局」「税　务　总　局」）
      · 剥内部机构后缀（办公厅/办公室/各司/各处/各局/中心）→ 归到主体机关
      · 「部委联合发文」这类非机关值如实保留（站点原值，不臆造机关名）
    """
    if not raw:
        return None
    s = re.sub(r"[\s\u3000\xa0]+", "", raw).strip()
    if not s:
        return None
    # 剥内部机构后缀：财政部办公厅 -> 财政部；国家发展改革委就业收入分配和消费司 -> 国家发展改革委
    m = re.match(
        r"^(.+?(?:部|委|委员会|总局|总署|署|局|厅|政府|行|院|会|社|集团|公司))"
        r"(?:办公厅|办公室|党组|[\u4e00-\u9fff]{1,12}?(?:司|局|处|中心|部|办))?$",
        s,
    )
    if m:
        return m.group(1)
    return s


def _issuer_from_title(title: str | None) -> str | None:
    """从「中华人民共和国财政部令第116号」这类标题前缀取发布机构。

    令/公告体裁无落款，机构名嵌在标题里；这是实测唯一可靠的来源。
    """
    if not title:
        return None
    m = re.match(r"^(?:中华人民共和国)?([\u4e00-\u9fff]{2,12}?(?:部|委|委员会|总局|署|局|厅|政府|行))(?=令|公告)", title)
    return m.group(1) if m else None


# 落款日期：与机关名同行或以独立行出现的成文日期，剥掉后剩下的才是机关名。
_RE_TAIL_DATE = re.compile(
    "(?:\\d{4}\\s*年\\s*\\d{1,2}\\s*月\\s*\\d{1,2}\\s*日|\\d{4}-\\d{2}-\\d{2}"
    "|\\d{4}\\s*年\\s*\\d{1,2}\\s*月|\\d{4}\\.\\d{1,2}\\.\\d{1,2})\\s*$"
)
# 像机关名的行：以机关后缀收尾，长度有界。
_RE_AGENCY_LIKE = re.compile(
    "^(?:中华人民共和国)?[\\u4e00-\\u9fff]{2,22}?"
    "(?:部|委(?:员会)?|总局|总署|署|局|厅|政府|院|会|社|集团|公司)$"
)
# 落款里不该当机关的常见非机关行（人名头衔、附件清单、联系方式、公开标注）。
_RE_NOT_AGENCY = re.compile(
    "(?:总理|委员长|主席|部长|局长|主任|省长|市长|县长|联系人|联系电话|电话|邮箱|地址|邮编"
    "|附件|此件|公开发布|抄送|主题词|负责人)"
)


def _issuer_from_signature(body: str | None, tail_lines: int = 8) -> str | None:
    """从正文**落款**取发文机关（文末机关署名）。

    为什么需要它（实测 2026-11-24）：gov.cn 政策标题 96% 是「中共中央 国务院 关于…」
    型，不含发文机关名，仅靠标题只能抽到 4%；而**正文落款**（文末署名，如「市场监管
    总局2024年6月20日」独立成行）含词表机关名的占 40.6%。

    判据必须**保守**（宁可判空，不可把引用他机关误判成发文机关）：只扫尾部 tail_lines
    个非空行、要求整行（剥掉落款日期后）基本就是机关名（有机关后缀、长度有界）、
    排除人名头衔/附件/联系方式等非机关行。命中即返回**归一前**的机关名，归一与词表
    落地由投影负责。
    """
    if not body:
        return None
    lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
    for ln in reversed(lines[-tail_lines:] if tail_lines else lines):
        if _RE_NOT_AGENCY.search(ln):
            continue
        cand = _RE_TAIL_DATE.sub("", ln).strip()
        if not cand or len(cand) > 24:
            continue
        if _RE_AGENCY_LIKE.match(cand):
            return cand
    return None


def _detect_doc_type(title: str | None) -> str | None:
    """文种判定：编号形态（令/公告）-> 标题结尾词 -> 标题内「的X」-> None。

    编号形态必须优先：实测「中华人民共和国财政部令第116号--注册会计师行业
    严重失信主体名单管理办法」以「办法」结尾，按尾部词判不出任何文种，
    而「令」本身就是文种（命令类）。

    结尾词必须优先于「的X」：实测「关于公开征求《…规范探矿权招标出让的通知
    （征求意见稿）》意见的公告」——本文种是**公告**，但标题内引用的被征求意见稿
    名字里含「的通知」，若先匹配「的X」会把文种误判成通知。
    """
    if not title:
        return None
    m = _RE_NUMBERED_DOC.match(title)
    if m:
        if "令" in m.group(1):
            return "命令"
        return "公告"
    types = sorted(_DOC_TYPES, key=len, reverse=True)
    for t in types:
        if title.endswith(t):
            return t
    for t in types:
        if f"的{t}" in title:
            return t
    return None


def _split_sections(body: str) -> list[tuple[str, str]]:
    """按层级标记切分，产出 [(层级路径, 文本)]。

    两套层级体系，按体裁判定（实测各占一半样本）：
      · 通知/函/意见类：一、/（一）/1.
      · 令/办法/条例类：「第X条」——实测「财政部令第116号」正文含 30 处「第X条」，
        若只认「一、」，整篇层级会退化成一块，丢掉条级定位能力

    与切块策略配合：层级单元是公文的价值单位（用户问「哪份文件规定了这件事」，
    命中一段而答不出属于哪一条等于没答）。
    """
    out: list[tuple[str, str]] = []
    cur_l1 = cur_l2 = cur_tiao = ""
    buf: list[str] = []

    def flush(path: str) -> None:
        text = "\n".join(buf).strip()
        if text:
            out.append((path, text))
        buf.clear()

    def path() -> str:
        return " > ".join(p for p in (cur_tiao, cur_l1, cur_l2) if p)

    for line in body.split("\n"):
        mt = _RE_SEC_TIAO.match(line)
        m3 = _RE_SEC_L3.match(line)
        m2 = _RE_SEC_L2.match(line)
        m1 = _RE_SEC_L1.match(line)
        if mt:
            # 条级：优先于「一、」——条的正文里常含「（一）」款项，不应另起层级
            flush(path())
            cur_tiao = mt.group(1)
            cur_l1 = cur_l2 = ""
            buf.append(line)
        elif m1:
            flush(path())
            cur_l1, cur_l2 = f"{m1.group(1)}、", ""
            buf.append(line)
        elif m2:
            flush(path())
            cur_l2 = f"（{m2.group(1)}）"
            buf.append(line)
        elif m3 and (cur_l1 or cur_tiao):  # 三级仅在已有上级上下文时认，避免误切编号列表
            flush(path())
            buf.append(line)
        else:
            buf.append(line)
    flush(path())
    return out


def parse_gov_doc(html: str) -> GovDoc:
    """HTML -> 公文结构树。字段缺失如实留空，不臆造。

    字段优先级见模块 docstring（标题/日期/发布机构三条都是实测定的）。
    """
    doc = GovDoc()
    labeled = _extract_labeled_values(html)

    # 标题：<meta ArticleTitle>（干净）-> <title>（剥装饰）-> 元数据表「名称」
    doc.title = _clean_title(_meta_content(html, "ArticleTitle"))
    if not doc.title:
        mt = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if mt:
            doc.title = _clean_title(mt.group(1))
    if labeled.get("title"):
        doc.title = labeled["title"] or doc.title

    doc.index_no = labeled.get("index_no")
    doc.doc_number = labeled.get("doc_number")
    doc.issuer = labeled.get("issuer")
    doc.addressee = labeled.get("addressee")
    # 站点元数据表的日期单独留档：实测 1/20 样本与落款不一致，落款为准但要可追溯
    doc.site_date = _normalize_date(labeled.get("date"))

    body_html = _find_body_container(html)
    doc.body = _clean_text(body_html)

    # 兜底：TRS 模板无元数据表，文号与日期在正文里
    if not doc.doc_number:
        m = _RE_DOC_NUMBER.search(doc.body)
        if m:
            doc.doc_number = m.group(1)
        else:
            # 编号形态（令/公告）：财政部令第116号
            m2 = _RE_NUMBERED_DOC.search(doc.body) or _RE_NUMBERED_DOC.search(doc.title or "")
            if m2:
                doc.doc_number = m2.group(1)

    # 成文日期：落款独立成行的日期优先（公文成文日以落款为准），元数据表兜底
    closing = _closing_date(doc.body)
    if closing:
        doc.date = _normalize_date(closing)
    elif doc.site_date:
        doc.date = doc.site_date
    else:
        m = _RE_DATE_CN.search(doc.body)
        if m:
            doc.date = _normalize_date(m.group(0))

    # 文号与标题交叉校验：元数据表的文号偶尔取到「同时废止」的旧令号
    # （9 行实测，如 113 号发布页 labeled 文号=被废止的 71 号），导致索引头与
    # 正文标题自相矛盾，下游 KG 抽取把新令的施行日挂到旧令上。
    # 标题是文档自己的封面，冲突时以标题为准。**只在两侧都是令形态时改写**：
    # 标题里引用的他文档号（如「关于废止…令第45号的通知」）不得触发改写。
    # 注意：本块必须放在上面完整的 if/elif/else 日期链**之后**——曾因插进
    # elif 与 else 之间把悬挂 else 挂到本块上，令日期兜底被错误触发（实测）。
    if doc.title and doc.doc_number:
        mt = _RE_NUMBERED_DOC.search(doc.title)
        md = _RE_NUMBERED_DOC.search(doc.doc_number)
        if mt and md and mt.group(1) != md.group(1):
            n_t = re.search(r"第(\d+)号", mt.group(1))
            n_d = re.search(r"第(\d+)号", md.group(1))
            if n_t and n_d and n_t.group(1) != n_d.group(1):
                doc.doc_number = mt.group(1).replace("中华人民共和国", "")

    if not doc.issuer:
        doc.issuer = _issuer_from_title(doc.title)
    if not doc.issuer:
        # 落款：正文末段以机关名结尾的短行（后缀表按实测 20 页补齐，含 司/厅/中心）
        for line in reversed(doc.body.split("\n")[-10:]):
            if _ORG_SUFFIX.search(line) and len(line) <= 30:
                doc.issuer = line.strip()
                break
    # 发布机构归一：原值留档 issuer_raw（可追溯），issuer 归到 facet 词表可用的机关名。
    # 不归一则按机关过滤会漏掉大半语料（实测 51 种原始形态）。
    doc.issuer_raw = doc.issuer
    doc.issuer = _normalize_issuer(doc.issuer)

    # 引用文号：正文内出现的其它公文文号（去重、排除自身）
    seen = set()
    for m in _RE_DOC_NUMBER.finditer(doc.body):
        num = m.group(1)
        if num != doc.doc_number and num not in seen:
            seen.add(num)
            doc.cited_docs.append(num)

    doc.doc_type = _detect_doc_type(doc.title)

    # 附件：按扩展名识别，相对路径转绝对（需调用方给 base_url 时才转，此处留原样）
    att_seen = set()
    for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL):
        href, text = m.group(1), _clean_text(m.group(2))
        if _RE_ATTACH_EXT.search(href) and href not in att_seen:
            att_seen.add(href)
            kind = _RE_ATTACH_EXT.search(href).group(1).lower()
            doc.attachments.append(Attachment(name=text or href.rsplit("/", 1)[-1], url=href, kind=kind))

    doc.sections = _split_sections(doc.body)
    return doc
