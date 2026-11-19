"""读取分页必须**无损**：跟着 `next_offset` 走一遍，必须逐字符还原正文。

2026-11-13 生产实测的缺陷：`next_offset` 取的是本页最后一个块的 `char_end`，而 `text`
被 `[:limit]` 截断。只要本页各块总长超过 `limit`，被截掉的尾巴就**永久不可达**：

    doc 7467（2041 字符）limit=500
    → text 500 字符，next_offset=2041
    → 按 next_offset 续读拿回 0 字符 ⇒ 中间 1541 字符（75%）丢掉，且不报错

影响面：news 5083 篇里 1748 篇、screenplay 17/17 篇超过默认 limit 4000（最长 19 万字符），
一次默认调用就能静默丢掉十几万字符。

本文件的核心是一条**属性测试**：拿一篇合成文档、用小 limit 反复续读，拼起来必须与原文
逐字符相等。这比逐条断言更能抓住"游标跳过内容"这类错——它不关心实现怎么写，只问结果丢没丢。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fd_text_core.querylib as q  # noqa: E402

DOC_ID = 42
CHUNK = 100
CONTENT = "".join(f"{i:03d}" for i in range(40))  # 120 字符，便于逐字符比对


@pytest.fixture
def fake_doc(monkeypatch):
    """把库调用换成一篇合成的内存文档（块按 CHUNK 切，与引擎切块同形）。"""
    chunks = [
        {"chunk_no": n, "chunk_text": CONTENT[i:i + CHUNK], "char_start": i,
         "char_end": min(i + CHUNK, len(CONTENT)), "section_path": None}
        for n, i in enumerate(range(0, len(CONTENT), CHUNK))
    ]

    monkeypatch.setattr(q, "fetch_one", lambda sql, params=(), _c=chunks: (
        {"id": DOC_ID, "domain": "papers", "title": "t", "source": "s",
         "source_id": "1", "provenance": {}, "cold_block_key": None, "metadata": {}}
        if "FROM corpus_documents" in sql
        else ({"x": 1} if "SELECT 1 AS x" in sql and params[1] < len(CONTENT) else None)
    ))
    monkeypatch.setattr(
        q, "fetch_all",
        lambda sql, params=(), _c=chunks: [c for c in _c if c["char_end"] > params[1]],
    )
    return chunks


def test_paging_with_small_limit_reconstructs_the_whole_document(fake_doc):
    """属性：按 next_offset 一直读，拼起来必须等于原文（不丢、不重）。"""
    parts, off, hops = [], 0, 0
    while off is not None:
        got = q.read_chunks(DOC_ID, offset=off, limit=17)  # 故意给个不能整除的小 limit
        parts.append(got["text"])
        off = got["next_offset"]
        hops += 1
        assert hops < 100, "游标没有推进，疑似死循环"
    assert "".join(parts) == CONTENT, (
        f"拼接结果与原文不等：len={len(''.join(parts))} vs {len(CONTENT)}\n"
        f"拼出来={''.join(parts)!r}"
    )
    assert hops > 1, "limit 远小于正文却只读了一页，说明分页没生效"


def test_next_offset_equals_chars_actually_delivered(fake_doc):
    """`next_offset` 必须等于 offset + len(text)——不是最后一个块的 char_end。"""
    got = q.read_chunks(DOC_ID, offset=0, limit=50)
    assert len(got["text"]) == 50
    assert got["next_offset"] == 50, (
        f"交付了 {len(got['text'])} 字符却把游标推到 {got['next_offset']} —— "
        "中间的字符按 next_offset 续读拿不回来"
    )


def test_offset_inside_a_chunk_starts_exactly_there(fake_doc):
    """offset 落在块中间时，text 必须从该字符开始（不能回退到块起点）。"""
    got = q.read_chunks(DOC_ID, offset=37, limit=20)
    assert got["text"] == CONTENT[37:57], f"起点错了：{got['text']!r}"
    assert got["next_offset"] == 57


def test_end_of_document_returns_none(fake_doc):
    """读完最后一页 → next_offset 为 None（客户端据此终止，不必猜）。"""
    got = q.read_chunks(DOC_ID, offset=0, limit=10_000)
    assert got["text"] == CONTENT
    assert got["next_offset"] is None


def test_missing_document_returns_none(monkeypatch):
    monkeypatch.setattr(q, "fetch_one", lambda sql, params=(), **kw: None)
    assert q.read_chunks(99999) is None
