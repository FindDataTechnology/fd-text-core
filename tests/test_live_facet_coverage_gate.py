"""`facet 覆盖` 这道门**自己**能不能真跑、能不能真红——真库，`pytest --live` 才跑。

为什么需要这层：`tests/test_conformance_gate_facet_coverage.py` 用桩验证了判据逻辑
（两类违规各自怎么判），但 `_facet_observed()` 里那三条 SQL（`eq` / `overlap` /
`column`）**从来没对着真库跑过**。它们只在生产 pod 里被手工执行过一次——那种验证
不会留在仓库里，也不会在下次改 schema 时提醒任何人。

本文件钉两件事，缺一件这道门就还是"纸面上的门"：

1. 对着真实注册表与真实语料跑 `check_facet_coverage()`，结果必须是 **PASS**，且
   核对到的 facet 数等于注册表里声明的总数（少核对了就是静默漏检）；
2. 把某个 facet 的观测值**人为掺进一个词表外取值**，同一函数必须判 **FAIL** 并点名
   该 facet——证明"绿"不是因为它什么都不查。

第二条尤其重要：这道门在 2026-11-13 上线后一直报 PASS，而 PASS 既可能是"数据真的
对齐了"，也可能是"查询恒返回空集合所以永远没差异"。只有掺入一条已知会违规格的值，
才能把这两种 PASS 分开。

用法（网内）：
    FDTEXT_DATABASE_URL=… pytest tests/test_live_facet_coverage_gate.py --live
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

pytestmark = pytest.mark.skipif(
    not os.environ.get("FDTEXT_DATABASE_URL"),
    reason="live 用例：需要 FDTEXT_DATABASE_URL",
)

SECTION = "facet 覆盖"
BOGUS = "not.a.real.category-for-gate-selfcheck"


def _gate():
    """每次取一份干净模块（Violations/Results 是模块级全局）。"""
    import importlib

    import conformance_gate

    importlib.reload(conformance_gate)
    conformance_gate.Violations.clear()
    conformance_gate.Results.clear()
    return conformance_gate


def _need(request):
    if not request.config.getoption("--live"):
        pytest.skip("live 用例：网内以 pytest --live 实跑")


def _declared_facet_count() -> int:
    from fd_text_core.verticals import registry

    registry.load_verticals()
    total = 0
    for domain in ("papers", "gov", "screenplay", "news"):
        try:
            total += len(registry.get(domain).facets)
        except KeyError:
            continue
    return total


def test_gate_passes_on_real_corpus_and_checks_every_declared_facet(request):
    _need(request)
    g = _gate()
    g.check_facet_coverage()

    results = [r for r in g.Results if r[0] == SECTION]
    assert results, "检查项没有登记结果 —— 它要么没跑，要么把 return 掉了"
    _, status, detail = results[0]
    assert status == "PASS", f"真实语料上应通过，实际 {status}：{detail}\n违规：{g.Violations}"

    declared = _declared_facet_count()
    assert declared > 0, "注册表里一个 facet 都没声明？"
    assert f"核对 {declared} 个 facet" in detail, (
        f"只核对了 {detail!r} 里那个数，注册表声明了 {declared} 个 —— 有 facet 被静默跳过"
    )


def test_gate_flags_a_value_outside_the_vocabulary(request, monkeypatch):
    """掺一个词表外取值 → 必须 FAIL 并点名。证明 PASS 不是"什么都没查"。"""
    _need(request)
    g = _gate()

    real = g._facet_observed

    def tainted(domain, facet, path):
        got = real(domain, facet, path)
        if (domain, facet) == ("papers", "cat"):
            return set(got) | {BOGUS}
        return got

    monkeypatch.setattr(g, "_facet_observed", tainted)
    g.check_facet_coverage()

    hits = [x for x in g.Violations if x.startswith(f"[{SECTION}]")]
    assert hits, "掺入词表外取值后没有任何违规 —— 这道门查不出它自己该查的东西"
    assert any(BOGUS in h for h in hits), f"违规没点名那个取值：{hits}"
    _, status, _ = [r for r in g.Results if r[0] == SECTION][0]
    assert status == "FAIL", f"应判 FAIL，实际 {status}"


def test_gate_flags_a_facet_that_observed_nothing(request, monkeypatch):
    """某 facet 实存 0 种取值 → 必须 FAIL（键位错了或加载器没写）。"""
    _need(request)
    g = _gate()

    real = g._facet_observed
    monkeypatch.setattr(
        g, "_facet_observed",
        lambda d, f, p: set() if (d, f) == ("news", "has_body") else real(d, f, p),
    )
    g.check_facet_coverage()

    hits = [x for x in g.Violations if "实存 0 种取值" in x]
    assert any("news.has_body" in h for h in hits), f"没点名 news.has_body：{g.Violations}"
