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
    """
    ids = [q.expect_doc_id for q in dataset.queries]
    if not ids:
        return []
    rows = fetch_all(
        "SELECT id FROM corpus_documents WHERE domain=%s AND status='current' AND id = ANY(%s)",
        (dataset.domain, ids),
    )
    alive = {r["id"] for r in rows}
    return [
        {"q": q.q, "expect_doc_id": q.expect_doc_id}
        for q in dataset.queries
        if q.expect_doc_id not in alive
    ]


def store_run(report: EvalReport, *, run_at: datetime | None = None) -> int:
    """把一次跑分写入 eval_runs，返回行 id。"""
    from ..db import execute

    when = run_at or datetime.now(timezone.utc)
    row = fetch_one(
        """
        INSERT INTO eval_runs
            (domain, run_at, k, dataset_fingerprint, config_fingerprint, judgment_strength,
             n_queries, keyword_hits, keyword_mrr, vector_hits, vector_mrr,
             hybrid_hits, hybrid_mrr, by_intent, degraded)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
        RETURNING id
        """,
        (
            report.domain, when, report.k, report.dataset_fingerprint,
            report.config_fingerprint, report.judgment_strength, report.n_queries,
            report.summary.get("keyword", {}).get("hits", 0),
            report.summary.get("keyword", {}).get("mrr", 0.0),
            report.summary.get("vector", {}).get("hits", 0),
            report.summary.get("vector", {}).get("mrr", 0.0),
            report.summary.get("hybrid", {}).get("hits", 0),
            report.summary.get("hybrid", {}).get("mrr", 0.0),
            json.dumps(report.by_intent, ensure_ascii=False),
            json.dumps(report.degraded, ensure_ascii=False),
        ),
    )
    if row is None:
        raise RuntimeError(f"评测结果入库未拿到 id: {report.domain}")
    _ = execute  # 保持导入一致性（execute 供将来批量写入使用）
    return int(row["id"])


def latest_previous(domain: str, exclude_id: int | None = None) -> dict | None:
    """取该域最近一次运行（用于对比）。"""
    if exclude_id is None:
        return fetch_one(
            "SELECT * FROM eval_runs WHERE domain=%s ORDER BY run_at DESC, id DESC LIMIT 1",
            (domain,),
        )
    return fetch_one(
        "SELECT * FROM eval_runs WHERE domain=%s AND id <> %s ORDER BY run_at DESC, id DESC LIMIT 1",
        (domain, exclude_id),
    )
