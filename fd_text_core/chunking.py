"""L2 切块：可插拔策略 + 无损还原不变量。

不变量（spec text-corpus-store）：按 chunk_no 顺序拼接 chunk_text 必须逐字符
等于清洗后正文；char_start/char_end 为原文偏移。任何策略都受此约束，
质量门在入库后抽样校验，违反则垂域验收不通过。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol


@dataclass
class Chunk:
    chunk_no: int
    chunk_text: str
    char_start: int
    char_end: int
    section_path: str | None = None


class ChunkStrategy(Protocol):
    """切块策略接口：垂域实现此 callable 即完成接入（无需改引擎代码）。"""

    def __call__(self, text: str) -> Iterable[tuple[int, int, str | None]]:
        """产出 (start, end, section_path) 三元组，必须覆盖全文且不重叠。"""
        ...


def verify_lossless(text: str, chunks: list[Chunk]) -> tuple[bool, str]:
    """拼接还原校验。返回 (是否通过, 违规明细)。"""
    if not chunks:
        return False, "无切块产出"
    rebuilt = "".join(c.chunk_text for c in chunks)
    if rebuilt != text:
        for i, _ in enumerate(rebuilt):
            if i >= len(text) or rebuilt[i] != text[i]:
                ctx = max(0, i - 30)
                return False, (
                    f"首处差异位于字符偏移 {i}: "
                    f"期望={text[ctx:i+30]!r} 实际={rebuilt[ctx:i+30]!r}"
                )
        return False, f"长度不符: 原文 {len(text)} vs 拼接 {len(rebuilt)}"
    for prev, cur in zip(chunks, chunks[1:]):
        if cur.char_start != prev.char_end:
            return False, (
                f"块 {prev.chunk_no}->{cur.chunk_no} 偏移不连续: "
                f"{prev.char_end} != {cur.char_start}"
            )
    if chunks[0].char_start != 0 or chunks[-1].char_end != len(text):
        return False, "首尾偏移未覆盖全文"
    return True, ""


@dataclass
class SlidingWindow:
    """分句铺砌式滑窗：任意一个字符恰好属于一个块（铺砌，非重叠）。

    不变量要求「拼接还原原文」，因此窗口必须首尾相接铺满全文——这与「为提
    召回率而做字符重叠」互斥。重叠需求由垂域在各自策略里另做（例如 embedding
    视图），不得破坏本层铺砌不变量。窗口边界吸附句子边界。
    """

    target: int = 1200
    max_size: int = 1800
    sentence_endings: str = "。！？.!?\\n"
    _re_split: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._re_split = re.compile(f"(?<=[{self.sentence_endings}])")

    def _sentence_spans(self, text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        pos = 0
        for piece in self._re_split.split(text):
            if not piece:
                continue
            spans.append((pos, pos + len(piece)))
            pos += len(piece)
        return spans

    def __call__(self, text: str) -> Iterable[tuple[int, int, None]]:
        if not text:
            return []
        spans = self._sentence_spans(text)
        win_start = spans[0][0]
        cur_end = spans[0][0]
        for _s, e in spans:
            # 达到目标长度即收口；单个超长句自身成块（>= max_size 时立即收）
            if cur_end - win_start >= self.target or e - win_start >= self.max_size:
                yield (win_start, e, None)
                win_start = e
            cur_end = e
        if cur_end > win_start:
            yield (win_start, cur_end, None)


def chunk_text(text: str, strategy: Callable | ChunkStrategy) -> list[Chunk]:
    """以给定策略切块，产出带序号与偏移的 Chunk 列表。"""
    return [
        Chunk(chunk_no=i, chunk_text=text[s:e], char_start=s, char_end=e, section_path=path)
        for i, (s, e, path) in enumerate(strategy(text))
    ]
