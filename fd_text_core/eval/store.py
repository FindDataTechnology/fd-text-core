"""评测结果的持久化与数据集在库校验（本模块是唯一碰 SQL 的地方）。

为什么与 runner 分开：评审与测试要能断言「测量路径不含 SQL」——度量必须走
检索的公开入口（design D5）。把 DB 访问集中在 store 里，使那条不变量可被
机械检查（见 tests/test_eval.py）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..db import fetch_all, fetch_one
from .dataset import Dataset
from .runner import EvalReport

__all__ = ["check_expects_exist", "latest_previous", "store_run"]


def check_expects_exist(dataset: Dataset) -> list[dict]:
    """校验期望命中文档在库内存在且为 current。

    缺失逐条报告而非静默当作未命中（design Risks）：语料重建（如 gov 那次清库
    重摄取）会让 expect_doc_id 失效，静默处理会把「标注失效」伪装成「检索变差」。
    分级形态下校验全部等级文档（近邻也参与 nDCG，缺了同样失真）。
    """
    ids = sorted({g.doc_id for q in dataset.queries for g in q.grades()})
    if not ids:
        return []
    rows = fetch_all(
        "SELECT id FROM corpus_documents WHERE domain=%s AND status='current' AND id = ANY(%s)",
        (dataset.domain, ids),
    )
    alive = {r["id"] for r in rows}
    missing: list[dict] = []
    for q in dataset.queries:
        lost = [g.doc_id for g in q.grades() if g.doc_id not in alive]
        if lost:
            missing.append({"q": q.q, "expect_doc_id": lost[0] if len(lost) == 1 else None,
                            "expect_doc_ids": lost})
    return missing


def store_run(report: EvalReport, *, run_at: datetime | None = None) -> int:
    """把一次跑分写入 eval_runs，返回行 id。"""
    from ..db import execute

    when = run_at or datetime.now(timezone.utc)
    row = fetch_one(
        """
        INSERT INTO eval_runs
            (domain, run_at, k, variant, dataset_fingerprint, config_fingerprint,
             judgment_strength, n_queries,
             keyword_hits, keyword_mrr, keyword_ndcg,
             vector_hits, vector_mrr, vector_ndcg,
             hybrid_hits, hybrid_mrr, hybrid_ndcg, by_intent, degraded)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
        RETURNING id
        """,
        (
            report.domain, when, report.k, report.variant, report.dataset_fingerprint,
            report.config_fingerprint, report.judgment_strength, report.n_queries,
            report.summary.get("keyword", {}).get("hits", 0),
            report.summary.get("keyword", {}).get("mrr", 0.0),
            report.summary.get("keyword", {}).get("ndcg", 0.0),
            report.summary.get("vector", {}).get("hits", 0),
            report.summary.get("vector", {}).get("mrr", 0.0),
            report.summary.get("vector", {}).get("ndcg", 0.0),
            report.summary.get("hybrid", {}).get("hits", 0),
            report.summary.get("hybrid", {}).get("mrr", 0.0),
            report.summary.get("hybrid", {}).get("ndcg", 0.0),
            json.dumps(report.by_intent, ensure_ascii=False),
            json.dumps(report.degraded, ensure_ascii=False),
        ),
    )
    if row is None:
        raise RuntimeError(f"评测结果入库未拿到 id: {report.domain}")
    _ = execute  # 保持导入一致性（execute 供将来批量写入使用）
    return int(row["id"])


def latest_previous(report_or_domain, exclude_id: int | None = None) -> dict | None:
    """取同一（域, 变体）最近一次运行（用于对比）。

    按变体隔离：守门集与难题集的数字不可互比——跨变体取「上次」会把
    「换了评测面」伪装成趋势（EvalReport 入参形态保留旧调用兼容）。
    """
    if hasattr(report_or_domain, "variant"):
        domain, variant = report_or_domain.domain, report_or_domain.variant
    else:  # 旧调用形态：仅域（默认变体）
        domain, variant = report_or_domain, "default"
    if exclude_id is None:
        return fetch_one(
            "SELECT * FROM eval_runs WHERE domain=%s AND variant=%s "
            "ORDER BY run_at DESC, id DESC LIMIT 1",
            (domain, variant),
        )
    return fetch_one(
        "SELECT * FROM eval_runs WHERE domain=%s AND variant=%s AND id <> %s "
        "ORDER BY run_at DESC, id DESC LIMIT 1",
        (domain, variant, exclude_id),
    )
