"""检索评测层测试：数据集契约 / 指纹 / 对比判定 / 解耦不变量。

不联网、不连库：契约与对比逻辑是纯函数；库相关路径（入库、期望校验）在
验收报告里有真库实证（reports/acceptance.md）。此处守住的是**逻辑正确性**
与**度量不得改被度量**这条不变量。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core.eval import dataset as DS  # noqa: E402
from fd_text_core.eval import runner as R  # noqa: E402


# ---------- 数据集契约 ----------


def _write(tmp_path: Path, domain: str, payload: dict) -> Path:
    d = tmp_path / domain
    d.mkdir(parents=True, exist_ok=True)
    p = d / "annotated_queries.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _good_payload(n: int = 10) -> dict:
    return {
        "_provenance": {
            "how_targets_chosen": "目标文档的文号在库内唯一，故命中即正确",
            "judgment_strength": "strong",
        },
        "queries": [
            {"q": f"查询{i}", "expect_doc_id": 100 + i, "intent": "by_number", "why": "文号唯一"}
            for i in range(n)
        ],
    }


def test_load_dataset_happy_path(tmp_path, monkeypatch):
    _write(tmp_path, "demo", _good_payload())
    monkeypatch.setattr(DS, "DATASETS_DIR", tmp_path)
    ds = DS.load_dataset("demo")
    assert len(ds) == 10
    assert ds.judgment_strength == "strong"
    assert ds.queries[0].expect_doc_id == 100


def test_dataset_requires_how_targets_chosen(tmp_path, monkeypatch):
    """缺标注口径必须被拒——没有口径则『命中即正确』不成立（design D2）。"""
    payload = _good_payload()
    del payload["_provenance"]["how_targets_chosen"]
    _write(tmp_path, "demo", payload)
    monkeypatch.setattr(DS, "DATASETS_DIR", tmp_path)
    with pytest.raises(DS.DatasetError, match="how_targets_chosen"):
        DS.load_dataset("demo")


def test_dataset_requires_judgment_strength(tmp_path, monkeypatch):
    payload = _good_payload()
    payload["_provenance"]["judgment_strength"] = "very-good"
    _write(tmp_path, "demo", payload)
    monkeypatch.setattr(DS, "DATASETS_DIR", tmp_path)
    with pytest.raises(DS.DatasetError, match="judgment_strength"):
        DS.load_dataset("demo")


def test_dataset_reports_every_bad_query_with_position(tmp_path, monkeypatch):
    """错误必须点名「哪一条、缺什么」——泛泛报错无法指导修复。"""
    payload = _good_payload()
    del payload["queries"][2]["why"]
    del payload["queries"][5]["expect_doc_id"]
    payload["queries"][7]["expect_doc_id"] = "不是整数"
    _write(tmp_path, "demo", payload)
    monkeypatch.setattr(DS, "DATASETS_DIR", tmp_path)
    with pytest.raises(DS.DatasetError) as e:
        DS.load_dataset("demo")
    msg = str(e.value)
    assert "queries[2]" in msg and "why" in msg
    assert "queries[5]" in msg and "expect_doc_id" in msg
    assert "queries[7]" in msg
    assert "3 处不合契约" in msg


def test_dataset_min_queries_enforced(tmp_path, monkeypatch):
    _write(tmp_path, "demo", _good_payload(n=3))
    monkeypatch.setattr(DS, "DATASETS_DIR", tmp_path)
    with pytest.raises(DS.DatasetError, match="低于要求"):
        DS.load_dataset("demo")


def test_missing_dataset_names_the_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(DS, "DATASETS_DIR", tmp_path)
    with pytest.raises(DS.DatasetError, match="修复="):
        DS.load_dataset("nosuch")


def test_fingerprint_sensitive_to_bytes(tmp_path):
    """同内容同指纹；改一个字即变（宁可多报不可比较，不可漏报）。"""
    p = tmp_path / "a.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    f1 = DS.compute_fingerprint(p)
    p.write_text('{"a": 1}', encoding="utf-8")
    assert DS.compute_fingerprint(p) == f1
    p.write_text('{"a": 2}', encoding="utf-8")
    assert DS.compute_fingerprint(p) != f1


def test_gov_dataset_is_shipped_in_package():
    """gov 标注集必须随包走（design D1：tests 不进镜像）。"""
    path = DS.DATASETS_DIR / "gov" / "annotated_queries.json"
    assert path.is_file(), "gov 标注集不在包内——容器与质量门将读不到"
    ds = DS.load_dataset("gov")
    assert len(ds) >= 10
    assert ds.judgment_strength == "strong"
    # 该域的判定口径必须是可验证的（文号唯一），不能是空话
    assert "唯一" in ds.how_targets_chosen


def test_illegal_domain_rejected():
    with pytest.raises(DS.DatasetError, match="非法垂域名"):
        DS.load_dataset("../etc")


# ---------- 跑分与对比判定（不连库） ----------


def _report(domain="demo", hits=(9, 10, 10), mrr=(0.5, 0.6, 0.7), degraded=()):
    rep = R.EvalReport(
        domain=domain, k=10, dataset_fingerprint="d1", config_fingerprint="c1",
        judgment_strength="strong", how_targets_chosen="x", n_queries=12,
    )
    rep.summary = {
        "keyword": {"hits": hits[0], "hit@k": f"{hits[0]}/12", "mrr": mrr[0]},
        "vector": {"hits": hits[1], "hit@k": f"{hits[1]}/12", "mrr": mrr[1]},
        "hybrid": {"hits": hits[2], "hit@k": f"{hits[2]}/12", "mrr": mrr[2]},
    }
    rep.degraded = list(degraded)
    return rep


def test_hybrid_not_worse_pass_and_fail():
    assert _report(hits=(9, 10, 10)).hybrid_not_worse is True
    # 混合命中低于单通道 -> False
    assert _report(hits=(9, 10, 8)).hybrid_not_worse is False
    # 命中持平但 MRR 更低 -> 也算不达标（排序质量下降）
    assert _report(hits=(10, 10, 10), mrr=(0.5, 0.6, 0.4)).hybrid_not_worse is False


def test_hybrid_verdict_not_decidable_when_degraded():
    """向量通道降级时不得给通过判定——半份结果不能冒充完整评测。"""
    assert _report(degraded=["vector: 嵌入重试耗尽"]).hybrid_not_worse is None


def test_compare_no_baseline():
    out = R.compare_with_previous(_report(), None)
    assert out["status"] == "no_baseline"


def test_compare_detects_regression_when_fingerprints_match():
    prev = {
        "dataset_fingerprint": "d1", "config_fingerprint": "c1", "k": 10,
        "keyword_hits": 10, "keyword_mrr": 0.8,
        "vector_hits": 10, "vector_mrr": 0.8, "hybrid_hits": 10, "hybrid_mrr": 0.8,
    }
    out = R.compare_with_previous(_report(hits=(9, 10, 10), mrr=(0.5, 0.6, 0.7)), prev)
    assert out["status"] == "regression"
    assert any("keyword" in d for d in out["details"])


def test_compare_ok_when_same_or_better():
    prev = {
        "dataset_fingerprint": "d1", "config_fingerprint": "c1", "k": 10,
        "keyword_hits": 9, "keyword_mrr": 0.5,
        "vector_hits": 10, "vector_mrr": 0.6, "hybrid_hits": 10, "hybrid_mrr": 0.7,
    }
    out = R.compare_with_previous(_report(), prev)
    assert out["status"] == "ok"


def test_compare_refuses_when_dataset_changed():
    """标注集变了就不能比——否则会把「标注变容易了」读成「检索变好了」。"""
    prev = {
        "dataset_fingerprint": "OTHER", "config_fingerprint": "c1", "k": 10,
        "keyword_hits": 1, "keyword_mrr": 0.1,
        "vector_hits": 1, "vector_mrr": 0.1, "hybrid_hits": 1, "hybrid_mrr": 0.1,
    }
    out = R.compare_with_previous(_report(), prev)
    assert out["status"] == "not_comparable"
    assert any("标注集已变" in c for c in out["changes"])


def test_compare_refuses_when_config_changed():
    """配置变了（换模型/换切块）也不能直接比（design D3 的核心）。"""
    prev = {
        "dataset_fingerprint": "d1", "config_fingerprint": "OTHER", "k": 10,
        "keyword_hits": 1, "keyword_mrr": 0.1,
        "vector_hits": 1, "vector_mrr": 0.1, "hybrid_hits": 1, "hybrid_mrr": 0.1,
    }
    out = R.compare_with_previous(_report(), prev)
    assert out["status"] == "not_comparable"
    assert any("检索配置已变" in c for c in out["changes"])


def test_compare_refuses_when_k_changed():
    prev = {
        "dataset_fingerprint": "d1", "config_fingerprint": "c1", "k": 5,
        "keyword_hits": 1, "keyword_mrr": 0.1,
        "vector_hits": 1, "vector_mrr": 0.1, "hybrid_hits": 1, "hybrid_mrr": 0.1,
    }
    out = R.compare_with_previous(_report(), prev)
    assert out["status"] == "not_comparable"
    assert any("k 已变" in c for c in out["changes"])


# ---------- 解耦不变量（本 change 的守门条款）----------


def test_eval_module_does_not_import_query_internals():
    """评测模块只许调 hybrid_search 这一公开入口，不得直接查表/拼 SQL。

    度量必须与被度量走同一条路径（design D5）；若评测自己拼 SQL，
    「检索改好了」与「评测的复刻实现改好了」就分不清了。
    """
    import ast

    src = (REPO / "fd_text_core" / "eval" / "runner.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    allowed_from_querylib = {"hybrid_search"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("querylib"):
            names = {a.name for a in node.names}
            assert names <= allowed_from_querylib, (
                f"评测只许用 {allowed_from_querylib}，实得 {names}——"
                "不得依赖检索内部实现"
            )
    # 不得出现裸 SQL（评测不该自己查语料表）
    for kw in ("SELECT ", "corpus_chunks", "rrf_fuse", "keyword_search(", "vector_search("):
        assert kw not in src, f"评测模块出现 {kw!r}：疑似绕开公开检索入口"


def test_eval_module_has_no_llm_dependency():
    """评测层不引 LLM/判官依赖（生成质量评测是 law-bench 的领域，见 design D0）。"""
    src = (REPO / "fd_text_core" / "eval" / "runner.py").read_text(encoding="utf-8")
    for banned in ("deepeval", "openai", "anthropic", "litellm", "crewai"):
        assert banned not in src, f"评测模块引入了 {banned}：超出本 change 的边界"


# ---------- 分级相关性 + nDCG（change eval-graded-harder-set） ----------

from fd_text_core.eval.dataset import Grade, Query  # noqa: E402
from fd_text_core.eval.runner import ndcg_at_k  # noqa: E402


def test_ndcg_ideal_order_is_one():
    """理想序（grade2 打头）nDCG=1.0。"""
    grades = (Grade(1, 2, "所求"), Grade(2, 1, "近邻"))
    assert ndcg_at_k([1, 2, 99], grades, k=10) == 1.0


def test_ndcg_penalizes_grade1_ahead_of_grade2():
    """近邻（grade1）被排到所求（grade2）之前必须扣分——这正是重排收益面。

    hit@k/MRR 对此完全无感（它们只看 grade2 是否出现），nDCG 是唯一能看见
    「排得不好」的指标（本 change 的立项理由）。
    """
    grades = (Grade(1, 2, "所求"), Grade(2, 1, "近邻"))
    ideal = ndcg_at_k([1, 2], grades, k=10)
    degraded = ndcg_at_k([2, 1], grades, k=10)
    assert degraded < ideal, "grade1 排前未被扣分：nDCG 对排序不敏感，度量无效"
    assert 0.0 < degraded < 1.0


def test_ndcg_missing_expected_is_zero():
    grades = (Grade(1, 2, "所求"),)
    assert ndcg_at_k([7, 8, 9], grades, k=10) == 0.0


def test_ndcg_never_exceeds_one_with_chunk_duplicates():
    """同一文档多切块进前 k 时不得重复计增益。

    实现期实测抓到的真错：不去重时 gov hybrid 报 nDCG=2.1005。
    nDCG 的取值域 [0,1] 是契约，超界即算错——本用例锁死该回归。
    """
    grades = (Grade(1, 2, "所求"),)
    dup = ndcg_at_k([1, 1, 1, 1, 1], grades, k=10)
    assert dup <= 1.0, f"重复切块把 nDCG 推过 1（={dup}）：增益被重复计入"
    assert dup == 1.0  # 去重后目标在首位，应为理想


def test_binary_query_equivalent_to_single_grade2():
    """二值形态在指标层等价于单条 grade=2（向后兼容的语义基础）。"""
    q = Query(q="x", intent="t", why="w", expect_doc_id=42)
    assert q.graded is False
    assert q.max_grade_ids() == (42,)
    assert q.grades() == (Grade(doc_id=42, grade=2, why="w"),)


def test_graded_query_max_grade_ids_allows_ties():
    q = Query(q="x", intent="t", why="", expect=(
        Grade(1, 2, "a"), Grade(2, 2, "b"), Grade(3, 1, "邻居"),
    ))
    assert q.graded is True
    assert set(q.max_grade_ids()) == {1, 2}  # 并列所求都算命中判据


def test_graded_dataset_requires_grade2(tmp_path):
    """缺 grade=2 的查询不可判，契约必须拒载。"""
    import json as _json

    from fd_text_core.eval.dataset import DatasetError, load_dataset

    d = tmp_path / "gov"
    d.mkdir()
    (d / "annotated_queries_hard.json").write_text(_json.dumps({
        "_provenance": {"how_targets_chosen": "口径", "judgment_strength": "medium"},
        "queries": [
            {"q": f"q{i}", "intent": "t", "why": "w",
             "expect": [{"doc_id": i, "grade": 1, "why": "只有邻居没所求"}]}
            for i in range(12)
        ],
    }, ensure_ascii=False), encoding="utf-8")

    import fd_text_core.eval.dataset as dsmod

    orig = dsmod.DATASETS_DIR
    dsmod.DATASETS_DIR = tmp_path
    try:
        with pytest.raises(DatasetError, match="无 grade=2"):
            load_dataset("gov", variant="hard")
    finally:
        dsmod.DATASETS_DIR = orig


def test_variant_isolation_in_compare():
    """跨变体不可比：守门集与难题集的数字不许混比。"""
    from fd_text_core.eval.runner import EvalReport, compare_with_previous

    base = dict(domain="gov", k=10, dataset_fingerprint="d", config_fingerprint="c",
                judgment_strength="strong", how_targets_chosen="h", n_queries=12,
                summary={"hybrid": {"hits": 12, "mrr": 1.0, "ndcg": 1.0}})
    now = EvalReport(variant="hard", **base)
    prev = {"dataset_fingerprint": "d", "config_fingerprint": "c", "k": 10,
            "variant": "default", "hybrid_hits": 12, "hybrid_mrr": 1.0}
    out = compare_with_previous(now, prev)
    assert out["status"] == "not_comparable"
    assert any("变体" in c for c in out["changes"])


def test_saturated_detection():
    """饱和检测：MRR 上限即标注无区分度；降级时不报饱和。"""
    from fd_text_core.eval.runner import EvalReport

    base = dict(domain="gov", k=10, dataset_fingerprint="d", config_fingerprint="c",
                judgment_strength="strong", how_targets_chosen="h", n_queries=12)
    sat = EvalReport(summary={"hybrid": {"hits": 12, "mrr": 1.0, "ndcg": 1.0}}, **base)
    assert sat.saturated is True
    unsat = EvalReport(summary={"hybrid": {"hits": 11, "mrr": 0.9, "ndcg": 0.9}}, **base)
    assert unsat.saturated is False
    deg = EvalReport(summary={"hybrid": {"hits": 12, "mrr": 1.0, "ndcg": 1.0}},
                     degraded=["vector: x"], **base)
    assert deg.saturated is False  # 降级时判定不可信
