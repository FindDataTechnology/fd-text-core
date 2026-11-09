"""公文垂域：四件配置（解析器 + 切块策略 + facet 词表 + 预设包）。

接入只走注册，不改引擎核心。CI 有路径越界检查守着。

本垂域是「垂域=四件配置」这条守门不变量的**第三次验证**（前两次=论文、剧本）。
公文带来的新考验是「层级结构必须保留到切块上」——用户问「哪份文件规定了这件事」，
命中一段而答不出属于哪一条等于没答。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...chunking import SlidingWindow
from ...presets import Preset
from ..registry import Vertical, bootstrap_defaults, replace

FACETS = {
    # 发文机关：**按实测语料分布定**（2026-10-13，209 篇）。词表只收「本部委级」机关名——
    # 解析器已把「财政部办公厅」「财政部会计司」这类内部机构归一到主体机关
    # （见 parser._normalize_issuer），故这里不需要列司/处级取值。
    # 「其他」承接实测出现的联合发文与非部委主体（部委联合发文/企业/法院等），
    # 它们是真实数据的一部分，如实归入而不臆造机关名。
    "issuer": frozenset(
        {
            "自然资源部",
            "财政部",
            "国家发展改革委",
            "生态环境部",
            "交通运输部",
            "水利部",
            "国家能源局",
            "国家数据局",
            "国家税务总局",
            "中国人民银行",
            "市场监管总局",
            "最高人民法院",
            "最高人民检察院",
            "国家核安全局",
            "国家外汇管理局",
            "其他",
        }
    ),
    # 文种：按《党政机关公文处理工作条例》主要类别 + 实测补入的「公示」
    # （部委公告栏里「XX结果的公示」占相当比例，非法定文种但文类标记稳定）
    "doc_type": frozenset(
        {"通知", "公告", "批复", "函", "意见", "决定", "命令", "通报", "报告", "请示", "纪要", "公示"}
    ),
    # 时段：**年份须覆盖语料实际跨度**（实测 2019-2026；词表窄于语料会让过滤静默漏掉旧文档）
    "period": frozenset({"2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026"}),
}

PRESETS = {
    "by_number": Preset(
        name="by_number",
        description=(
            "文号精查：给定发文字号（如 财办债〔2026〕218号）定位单篇公文。"
            "查询文本直接传号本身（query 参数），无需额外具名参数"
        ),
        params={"channels": "keyword", "limit": 10},
    ),
    "by_issuer_type": Preset(
        name="by_issuer_type",
        description="机关+文种：看某机关某类文书（如财政部的通知）的发文脉络",
        params={"channels": "hybrid", "limit": 30},
    ),
    "timeline": Preset(
        name="timeline",
        description="政策沿革：某主题在不同时期的公文，按时间排列",
        params={"channels": "hybrid", "limit": 40},
    ),
    "similar": Preset(
        name="similar",
        description="相似段落：给一段文本，找语义相近的其它公文段落（纯向量通道）",
        params={"channels": "vector", "limit": 10},
    ),
}

# 层级标记：与解析器同一套（切块要在文本上重新定位它们）
# 「第X条」是令/办法/条例体裁的层级（实测「财政部令第116号」含 30 处），
# 不认它则整篇退化成一块、丢掉条级定位能力——与解析器 _RE_SEC_TIAO 对应。
_RE_TIAO = re.compile(r"^\s*第[一二三四五六七八九十百]+条\s*", re.MULTILINE)
_RE_L1 = re.compile(r"^\s*[一二三四五六七八九十百]+\s*[、.．]", re.MULTILINE)
_RE_L2 = re.compile(r"^\s*[（(]\s*[一二三四五六七八九十百]+\s*[）)]", re.MULTILINE)
_RE_L3 = re.compile(r"^\s*\d{1,3}\s*[.．、]", re.MULTILINE)
_RE_SENT = re.compile(r"(?<=[。！？；])")


def _split_sentences(text: str, target: int, max_size: int) -> list[str]:
    """按句切分，累计到 target 即收口；单句超长则硬切。"""
    out: list[str] = []
    buf = ""
    for piece in _RE_SENT.split(text):
        if not piece:
            continue
        if buf and len(buf) + len(piece) > target:
            out.append(buf)
            buf = piece
        else:
            buf += piece
        while len(buf) > max_size:  # 单句过长硬切
            out.append(buf[:max_size])
            buf = buf[max_size:]
    if buf:
        out.append(buf)
    return out


@dataclass
class GovSectionStrategy:
    """公文切块：以正文层级单元为首选边界，超长层级再按句滑窗兜底。

    为什么不用引擎内置的 SlidingWindow：公文的检索单位是「条」而非「篇」。
    纯滑窗会把层级边界切碎，丢掉「这段话属于哪一条」的定位能力。
    但层级段落若过长（如附则里的长条款），仍要按句切开以免单块超出嵌入模型的
    有效上下文。

    不变量：产出必须覆盖全文且不重叠（引擎的 verify_lossless 会校验）。
    section_path 记录该块所属层级路径，供检索结果回答「属于哪一条」。
    """

    target: int = 800  # 层级段超过此长度即按句切分
    max_size: int = 1500
    _fallback: SlidingWindow = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._fallback = SlidingWindow(target=self.target, max_size=self.max_size)

    def _heading_positions(self, text: str) -> list[tuple[int, str]]:
        """找出所有层级标题的行首偏移及其路径标签。

        「第X条」自成一条路径层级（令/办法/条例体裁）；「一、」类层级在其下，
        与解析器 _split_sections 的判定顺序一致（条级优先于「一、」）。
        """
        marks: list[tuple[int, str]] = []
        l1 = tiao = ""
        for m in re.finditer(r"(?m)^.*$", text):
            line, off = m.group(0), m.start()
            if _RE_TIAO.match(line):
                tiao = line.strip().split()[0][:10]
                l1 = ""
                marks.append((off, tiao))
            elif _RE_L1.match(line):
                l1 = line.strip().split()[0][:8]
                label = f"{tiao} > {l1}" if tiao else l1
                marks.append((off, label))
            elif _RE_L2.match(line):
                prefix = " > ".join(p for p in (tiao, l1) if p)
                label = f"{prefix} > {line.strip().split()[0][:8]}" if prefix else line.strip()[:8]
                marks.append((off, label))
            elif _RE_L3.match(line) and (l1 or tiao):
                prefix = " > ".join(p for p in (tiao, l1) if p)
                marks.append((off, f"{prefix} > {line.strip().split()[0][:8]}"))
        return marks

    def __call__(self, text: str) -> list[tuple[int, int, str | None]]:
        if not text:
            return []
        marks = self._heading_positions(text)
        if not marks:
            # 无层级标记（如公告类短文）：整篇一块，但超长时按句切。
            # 注意 SlidingWindow 产出的是 (start, end, path) **三元组**，
            # 不是 Chunk 对象——按对象取属性会 AttributeError（实测在
            # 6465 字符的招聘公示上炸出：该类文档无层级标记且超长）。
            if len(text) <= self.max_size:
                return [(0, len(text), None)]
            return list(self._fallback(text))

        spans: list[tuple[int, int, str | None]] = []
        # 首个标题之前的前言（标题/文号/主送等）自成一块
        if marks[0][0] > 0:
            spans.append((0, marks[0][0], "前言"))
        for i, (start, label) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
            if end - start <= self.max_size:
                spans.append((start, end, label))
            else:
                # 超长层级：按句切成若干块，共享同一层级路径
                cursor = start
                for piece in _split_sentences(text[start:end], self.target, self.max_size):
                    piece_end = cursor + len(piece)
                    spans.append((cursor, piece_end, label))
                    cursor = piece_end
        # 合并零长块并保证覆盖连续
        return [(s, e, p) for s, e, p in spans if e > s]


class GovParser:
    """域解析器：HTML -> 公文结构树（实现见 parser 模块）。"""

    def __call__(self, text: str):
        from .parser import parse_gov_doc

        return parse_gov_doc(text)


# 垂域自身持有权威配置（bootstrap 只是「无垂域目录也能跑」的兜底）
bootstrap_defaults()
replace(
    Vertical(
        name="gov",
        strategy=GovSectionStrategy(),
        facets=FACETS,
        parser=GovParser(),
        # 中文垂域：引擎原生 CJK 分词（unigram+bigram）+ 繁简归一
        cjk=True,
        presets=PRESETS,
        tsv_config="simple",
        language="zh",
        display="公文",
    )
)
