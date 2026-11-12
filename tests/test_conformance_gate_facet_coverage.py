"""facet 覆盖门：两种违规都必须能红，环境缺失必须不红。

这个检查存在的理由：`facet 覆盖` 比的是「词表」与「语料里实存的取值」。它的两种
违规**症状完全一样**——按 facet 过滤查不到东西，看着像"没有这类文档"，实际是
词表落后 / 键位声明错 / 加载器没写三者之一。2026-11-13 一天内在 `papers.cat`、
`news.has_body`、`papers.origin` 三处各撞一次，每次都是靠人工连库 dump 才发现。

本文件钉住四条，缺一条这个门就会退化成"永远绿"：

1. 实存取值在词表外 → FAIL（按它过滤会被 FacetError 拒掉）；
2. facet 解析不到任何取值 → FAIL（键位错了或加载器没写）；
3. 词表里有、语料里没有 → **不算违规**（只是还没有这类文档）；
4. 库不可用 → SKIPPED 且 detail 以「缺 」开头（`--strict-ci` 只放行这一种跳过，
   否则 CI 会永久红，指的方向还是错的）。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "conformance_gate.py"
SECTION = "facet 覆盖"


def _load_gate():
    """按路径加载 gate 模块：一个测试一份干净全局态（Violations/Results 是模块级）。"""
    spec = importlib.util.spec_from_file_location("gate_under_test", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeVertical:
    def __init__(self, facets, facet_paths=None):
        self.facets = facets
        self.facet_paths = facet_paths or {}


@pytest.fixture
def gate(monkeypatch):
    """装好一个「连得上库」的假环境，垂域只挂 papers 一个，取值由用例指定。"""
    mod = _load_gate()
    mod.Violations.clear()
    mod.Results.clear()
    monkeypatch.setenv("FDTEXT_DATABASE_URL", "postgresql://stub/stub")

    from fd_text_core import db
    from fd_text_core.verticals import registry

    monkeypatch.setattr(db, "fetch_one", lambda *a, **k: {"ok": 1})
    monkeypatch.setattr(registry, "load_verticals", lambda *a, **k: None)

    vert = _FakeVertical(
        facets={"cat": {"cs.LG", "cs.AI"}},
        facet_paths={"cat": ("categories", "overlap")},
    )

    def _get(domain):
        if domain == "papers":
            return vert
        raise KeyError(domain)

    monkeypatch.setattr(registry, "get", _get)
    mod._fake_vert = vert
    return mod


def _results(mod):
    return [r for r in mod.Results if r[0] == SECTION]


def test_value_outside_vocabulary_is_a_violation(gate, monkeypatch):
    """语料里出现词表外的取值 → 按它过滤必被拒，必须点名。"""
    monkeypatch.setattr(
        gate, "_facet_observed", lambda *a, **k: {"cs.LG", "astro-ph.DOES-NOT-EXIST"}
    )
    gate.check_facet_coverage()

    hits = [x for x in gate.Violations if x.startswith(f"[{SECTION}]")]
    assert len(hits) == 1, hits
    assert "词表外取值" in hits[0]
    assert "astro-ph.DOES-NOT-EXIST" in hits[0]
    # 报错要能指路：词表外说明补词表，或该取值其实住在别的键位（facet_paths）
    assert "facet_paths" in hits[0]
    assert _results(gate)[0][1] == "FAIL"


def test_zero_observed_values_is_a_violation(gate, monkeypatch):
    """实存 0 种取值 → 键位声明错了或加载器没写，与"没有这类文档"是两件事。"""
    monkeypatch.setattr(gate, "_facet_observed", lambda *a, **k: set())
    gate.check_facet_coverage()

    hits = [x for x in gate.Violations if x.startswith(f"[{SECTION}]")]
    assert len(hits) == 1, hits
    assert "实存 0 种取值" in hits[0]
    assert _results(gate)[0][1] == "FAIL"


def test_vocabulary_entry_without_data_is_not_a_violation(gate, monkeypatch):
    """词表有、语料没有 = 还没有这类文档，不是缺陷。

    反过来（把空取值当违规）会让每加一个类目就先红一次，人就会去改闸门。
    """
    monkeypatch.setattr(gate, "_facet_observed", lambda *a, **k: {"cs.LG"})
    gate.check_facet_coverage()

    assert not [x for x in gate.Violations if x.startswith(f"[{SECTION}]")]
    status, detail = _results(gate)[0][1], _results(gate)[0][2]
    assert status == "PASS", detail
    assert "核对 1 个 facet" in detail


def test_unreachable_db_skips_instead_of_flagging(gate, monkeypatch):
    """库连不上是环境问题，不是词表问题。

    这条是修的第一个 bug：第一版没有连通性探针，库配置不全时按 facet 刷出一串
    「取值查询失败」，于是门永久红、还指着错的方向。`--strict-ci` 只放行 detail
    以「缺 」开头的跳过，所以这里也必须走 skip() 而不是记违规。
    """
    from fd_text_core import db

    def _boom(*a, **k):
        raise RuntimeError("ConfigError: 缺少必需环境变量 FDTEXT_S3_ENDPOINT")

    monkeypatch.setattr(db, "fetch_one", _boom)
    monkeypatch.setattr(gate, "_facet_observed", lambda *a, **k: {"cs.LG"})
    gate.check_facet_coverage()

    assert not [x for x in gate.Violations if x.startswith(f"[{SECTION}]")]
    status, detail = _results(gate)[0][1], _results(gate)[0][2]
    assert status == "SKIPPED"
    assert detail.startswith("缺 "), f"strict-ci 只放行「缺 …」式跳过：{detail!r}"


def test_missing_dsn_skips_with_the_variable_named(gate, monkeypatch):
    """没有 DSN 时跳过并点名变量——否则不知道要在网内补哪一项。"""
    monkeypatch.delenv("FDTEXT_DATABASE_URL", raising=False)
    gate.check_facet_coverage()

    status, detail = _results(gate)[0][1], _results(gate)[0][2]
    assert status == "SKIPPED"
    assert "FDTEXT_DATABASE_URL" in detail


def test_check_is_wired_into_the_cli():
    """门必须在 cli() 里真被调用：写成函数没接线＝不存在。

    静态闸全绿也拦不住这种（模块顶层定义合法但没人调用）。
    """
    src = GATE.read_text(encoding="utf-8")
    body = src.split("def cli()", 1)[1]
    assert "check_facet_coverage()" in body.split("if __name__", 1)[0]
