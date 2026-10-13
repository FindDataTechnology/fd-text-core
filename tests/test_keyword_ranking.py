"""关键词通道「原串精确命中优先」排序的不变量（change text-exact-match-ranking）。

行为证明来自网内评测层重跑（gov by_number 2/4 → 4/4，其余意图零变化）；
这里的单测守住结构不变量——排序因子位置（必须在 kw_score 之前）与
参数绑定顺序（本修法唯一的易错点，曾把字面 % 写成 %s 前缀直接炸）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core import querylib  # noqa: E402


class _Capture:
    """替身 fetch_all：记录 (sql, params)，返回可排序的最小行集。"""

    def __init__(self):
        self.calls: list[tuple[str, list]] = []

    def __call__(self, sql, params):
        self.calls.append((sql, list(params)))
        return [
            {
                "id": 1, "document_id": 1, "chunk_no": 0,
                "chunk_text": "占位", "char_start": 0, "char_end": 2,
                "section_path": None, "title": "t", "source": "s",
                "source_id": "1", "provenance": {}, "kw_score": 0.1,
            }
        ]


@pytest.fixture()
def capture(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(querylib, "fetch_all", cap)
    return cap


def _order_clause(sql: str) -> str:
    return sql.split("ORDER BY", 1)[1].split("LIMIT", 1)[0]


def test_cjk_branch_exact_match_factor_precedes_rank(capture):
    """CJK 分支：原串包含因子在 kw_score 之前，且参数表带 query。"""
    querylib.keyword_search("gov", "自然资办函〔2026〕1564号", limit=10)
    sql, params = capture.calls[0]
    clause = _order_clause(sql)
    assert "ILIKE '%%' || %s || '%%'" in clause
    assert clause.index("ILIKE") < clause.index("kw_score")
    # 参数序：[tsquery, domain, tsquery, 原查询, limit]
    assert params[0] == params[2]  # tsquery 两处同值
    assert params[1] == "gov"
    assert params[3] == "自然资办函〔2026〕1564号"
    assert params[-1] == 10


def test_noncjk_branch_same_factor_and_case_folding_marker(capture):
    """非 CJK 分支（papers；screenplay 因含中文语料而启用 CJK）：
    同一因子 + ILIKE（大小写不敏感，与 plainto_tsquery 对齐）。"""
    querylib.keyword_search("papers", "Oedipus Rex", limit=5)
    sql, params = capture.calls[0]
    clause = _order_clause(sql)
    assert "ILIKE" in clause  # 而非 LIKE：大小写折叠语义
    assert clause.index("ILIKE") < clause.index("kw_score")
    # 参数序：[cfg, query, domain, cfg, query, query, limit]
    assert params[1] == "Oedipus Rex"
    assert params[5] == "Oedipus Rex"
    assert params[-1] == 5


def test_no_exact_match_factor_in_where(capture):
    """排序因子只出现在 ORDER BY：召回集（WHERE）不含 ILIKE 过滤。"""
    querylib.keyword_search("gov", "任意查询", limit=10)
    sql, _ = capture.calls[0]
    where = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert "ILIKE" not in where


def test_literal_percent_is_escaped_for_psycopg(capture):
    """字面 % 必须写成 %%：psycopg 参数化下单个 % 会被当占位符前缀直接抛错
    （实现期实测踩到：ProgrammingError: only '%s', '%b', '%t' are allowed）。"""
    querylib.keyword_search("gov", "带%通配符的查询", limit=10)
    sql, _ = capture.calls[0]
    assert "ILIKE '%%'" in sql
    assert "ILIKE '%'" not in sql.replace("%%", "\x00").replace("\x00", "%%")


# ---------- 融合层 _exact_first ----------

def _fused(rid, rrf, text):
    return {"id": rid, "document_id": rid, "rrf": rrf, "chunk_text": text}


def test_exact_first_promotes_full_string_over_fusion_arithmetic():
    """单通道第 1（rrf 高）但无原串的候选，让位给含完整原串的候选——
    评测层实测：1564 查询 keyword 第 1/vector 未命中，RRF 后掉出前 10。"""
    rows = [
        _fused(101, 1 / 61, "自然资源部关于土地利用的通知"),  # 双通道在场，rrf 最大
        _fused(102, 1 / 60, "本函为自然资办函〔2026〕1564号的复函"),  # 单通道第 1
    ]
    out = querylib._exact_first(rows, "自然资办函〔2026〕1564号")
    assert [r["id"] for r in out] == [102, 101]


def test_exact_first_noop_when_no_candidate_contains_query():
    rows = [_fused(101, 1 / 61, "甲文本"), _fused(102, 1 / 60, "乙文本")]
    out = querylib._exact_first(rows, "完全不相关的语义查询")
    assert [r["id"] for r in out] == [102, 101]  # 无原串命中：保持 RRF 序（1/60 > 1/61）


def test_exact_first_case_insensitive():
    rows = [_fused(101, 1 / 61, "OEDIPUS AT COLONUS"), _fused(102, 1 / 60, "别的")]
    out = querylib._exact_first(rows, "Oedipus at Colonus")
    assert [r["id"] for r in out] == [101, 102]


def test_exact_first_stable_within_group_by_rrf():
    """同组（都含/都不含原串）内仍按 rrf 降序。"""
    rows = [
        _fused(1, 1 / 63, "含目标串自然资函〔2026〕593号"),
        _fused(2, 1 / 61, "也含目标串自然资函〔2026〕593号"),
        _fused(3, 1 / 65, "无原串"),
    ]
    out = querylib._exact_first(rows, "自然资函〔2026〕593号")
    assert [r["id"] for r in out] == [2, 1, 3]  # 含串组内 rrf 降序（1/61 > 1/63），无串垫底
