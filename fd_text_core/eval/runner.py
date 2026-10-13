"""参数化跑分器：三通道 hit@k / MRR + 双指纹 + 跨次回归判定。

只消费检索公开契约（`querylib.hybrid_search`），不自己拼 SQL、不复制 RRF——
度量与被度量必须走同一条路径，否则「检索变好了」与「评测的复刻实现改好了」
分不清（design D5）。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from ..embedding import Embedder
from .dataset import Dataset

CHANNELS = ("keyword", "vector", "hybrid")


def ndcg_at_k(ranked_ids: list[int], grades: tuple, k: int) -> float:
    """nDCG@k（分级相关性）。

    DCG  = Σ (2^g_i - 1) / log2(i + 1)，i 从 1 起；
    IDCG = 标注等级降序铺满 k 的理想值；nDCG = DCG / IDCG。

    **按 document_id 去重保首现**：检索返回切块（chunk），同一文档可有多个
    切块进前 k；不去重则该文档的增益被重复计入，nDCG 会超过 1（实现期实测
    抓到：gov hybrid 报 2.1005——nDCG 的取值域 [0,1] 是硬约束，超界即算错）。
    文档的「排名」= 其任一相关切块的最优位次，与 hit/MRR 的判定口径一致。

    为什么有它：MRR/hit@k 只看最高等级文档的位置，对「同簇近邻（grade=1）
    排在所求（grade=2）之前」完全无感——而近邻挤占 log2 折损位正是重排类
    改进的收益所在。二值标注（命中=grade2/未中=0）下 nDCG 是该尺度的退化，
    与 hit 排序敏感版一致。

    IDCG=0 只可能发生在标注不含任何相关文档时——契约强制每条至少一篇
    grade=2，故此处防御性返回 0.0（宁可显 0 也不抛，指标层不制造中断）。
    """
    gmap = {g.doc_id: g.grade for g in grades}
    seen: set[int] = set()
    ranked_docs: list[int] = []
    for did in ranked_ids:
        if did not in seen:
            seen.add(did)
            ranked_docs.append(did)

    dcg = 0.0
    for i, did in enumerate(ranked_docs[:k], start=1):
        g = gmap.get(did, 0)
        if g:
            dcg += (2**g - 1) / _log2(i + 1)
    ideal = sorted((g.grade for g in grades), reverse=True)[:k]
    idcg = sum((2**g - 1) / _log2(i + 1) for i, g in enumerate(ideal, start=1))
    if idcg <= 0:
        return 0.0
    return dcg / idcg


def _log2(x: float) -> float:
    from math import log2

    return log2(x)


@dataclass
class EvalReport:
    """一次跑分的完整结果。"""

    domain: str
    k: int
    dataset_fingerprint: str
    config_fingerprint: str
    judgment_strength: str
    how_targets_chosen: str
    n_queries: int
    summary: dict = field(default_factory=dict)  # channel -> {hits, mrr, ndcg, hit@k}
    by_intent: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)
    degraded: list = field(default_factory=list)
    missing_expects: list = field(default_factory=list)
    variant: str = "default"

    @property
    def saturated(self) -> bool:
        """本集对重排类改进是否有区分度。

        判据：全部查询的最高等级文档在三通道（至少 hybrid）皆居首位
        ⇔ MRR 达到理论上限。此时集合对重排无区分度——如实标注，**但不判
        FAIL**：回归守门集就该饱和（目标必须居首），把它标成失败会诱导人
        为数字好看而弱化守门集。
        """
        if self.degraded:
            return False
        hyb = self.summary.get("hybrid", {})
        return bool(
            self.n_queries > 0
            and hyb.get("hits", 0) == self.n_queries
            and hyb.get("mrr", 0.0) >= 1.0
        )

    @property
    def hybrid_not_worse(self) -> bool | None:
        """混合通道不低于任一单通道（spec 的验收线）。缺向量时返回 None。"""
        if self.degraded:
            return None
        hyb = self.summary.get("hybrid", {})
        for ch in ("keyword", "vector"):
            other = self.summary.get(ch, {})
            if hyb.get("hits", 0) < other.get("hits", 0):
                return False
            if hyb.get("mrr", 0.0) < other.get("mrr", 0.0):
                return False
        return True

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "k": self.k,
            "variant": self.variant,
            "dataset_fingerprint": self.dataset_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "judgment_strength": self.judgment_strength,
            "how_targets_chosen": self.how_targets_chosen,
            "n_queries": self.n_queries,
            "summary": self.summary,
            "by_intent": self.by_intent,
            "rows": self.rows,
            "degraded": self.degraded,
            "missing_expects": self.missing_expects,
            "hybrid_not_worse": self.hybrid_not_worse,
            "saturated": self.saturated,
        }


def compute_config_fingerprint(domain: str) -> str:
    """配置指纹：影响检索结果的引擎侧配置。

    为什么要它（design D3）：只存分数无法回答「这次比上次低，是检索变差还是我
    换了模型/切块策略」。记下这些配置使对比可判——指纹不同即明确输出
    「不可直接比较」，而不是静默比较两个不可比数字。
    """
    import hashlib
    import os

    from ..verticals.registry import get

    parts = [
        f"embedding_model={os.environ.get('FDTEXT_EMBEDDING_MODEL', 'Qwen/Qwen3-Embedding-8B')}",
        f"embedding_dim={os.environ.get('FDTEXT_EMBEDDING_DIM', '1024')}",
        f"provider_only={os.environ.get('FDTEXT_EMBEDDING_PROVIDER_ONLY', '')}",
        f"rrf_k={os.environ.get('FDTEXT_RRF_K', '60')}",
    ]
    try:
        v = get(domain)
        strat = v.strategy
        parts += [
            f"strategy={type(strat).__name__}",
            f"strategy_params={sorted((k, str(x)) for k, x in vars(strat).items() if not k.startswith('_'))}",
            f"cjk={v.cjk}",
            f"t2s={v.t2s}",
            f"tsv_config={v.tsv_config}",
        ]
    except KeyError:
        parts.append("strategy=<unregistered>")
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def run_eval(
    dataset: Dataset,
    *,
    k: int = 10,
    channels: tuple[str, ...] = CHANNELS,
    embedder: Embedder | None = None,
    check_expects: Callable[[Dataset], list[dict]] | None = None,
) -> EvalReport:
    """对一个标注集跑分。只调 hybrid_search，不碰库表与 SQL（design D5）。

    `check_expects` 由调用方注入（store.check_expects_exist）：默认 None 即跳过
    在库校验，使本函数保持零 DB 依赖、可离线单测。缺失逐条报告而非静默当作
    未命中——语料重建会让 expect_doc_id 失效，静默处理会把「标注失效」伪装成
    「检索变差」。
    """
    from ..querylib import hybrid_search

    cfg_fp = compute_config_fingerprint(dataset.domain)
    report = EvalReport(
        domain=dataset.domain,
        k=k,
        dataset_fingerprint=dataset.fingerprint,
        config_fingerprint=cfg_fp,
        judgment_strength=dataset.judgment_strength,
        how_targets_chosen=dataset.how_targets_chosen,
        n_queries=len(dataset.queries),
        missing_expects=(check_expects(dataset) if check_expects else []),
        variant=dataset.variant,
    )

    rows: list[dict] = []
    for q in dataset.queries:
        top_ids = q.max_grade_ids()  # 最高等级（分级下可并列）
        row: dict = {
            "q": q.q,
            "intent": q.intent,
            # 二值形态保留原字段名（既有报告消费方的兼容面）
            "expect": q.expect_doc_id,
            "expect_ids": list(top_ids),
            "graded": q.graded,
        }
        for ch in channels:
            res = hybrid_search(dataset.domain, q.q, embedder=embedder, limit=k, channels=ch)
            for d in res.get("degraded") or []:
                if d not in report.degraded:
                    report.degraded.append(d)
            ids = [x.get("document_id") for x in res["results"]]
            # 命中判据：任一最高等级文档进前 k；排名取其中最优者
            ranks = [ids.index(t) + 1 for t in top_ids if t in ids]
            row[ch] = min(ranks) if ranks else None
            row[f"{ch}_ndcg"] = round(ndcg_at_k(ids, q.grades(), k), 4)
        rows.append(row)
    report.rows = rows

    summary: dict = {}
    for ch in channels:
        ranks = [r[ch] for r in rows if r.get(ch)]
        ndcgs = [r[f"{ch}_ndcg"] for r in rows]
        summary[ch] = {
            "hits": len(ranks),
            "hit@k": f"{len(ranks)}/{len(rows)}",
            "mrr": round(sum(1.0 / r for r in ranks) / max(1, len(rows)), 4),
            "ndcg": round(sum(ndcgs) / max(1, len(ndcgs)), 4),
        }
    report.summary = summary

    by_intent: dict = defaultdict(lambda: {"total": 0, "hits": {ch: 0 for ch in channels}})
    for r in rows:
        b = by_intent[r["intent"]]
        b["total"] += 1
        for ch in channels:
            if r.get(ch):
                b["hits"][ch] += 1
    report.by_intent = {k2: v for k2, v in by_intent.items()}
    return report


def run_eval_all(
    *, k: int = 10, embedder: Embedder | None = None, variant: str = "default"
) -> list[EvalReport]:
    from .dataset import load_all

    return [run_eval(ds, k=k, embedder=embedder) for ds in load_all(variant=variant)]


def compare_with_previous(report: EvalReport, previous: dict | None) -> dict:
    """本次 vs 上次的对比判定（纯函数，不碰库——由调用方取出 previous 行传入）。

    两指纹都相同才判回归；任一不同则明确「不可直接比较」并列出变化项——
    静默比较两个不可比数字会让趋势图误导人（design D3）。
    """
    if not previous:
        return {"status": "no_baseline", "message": "无历史运行，本次为基线"}

    changed = []
    if previous.get("dataset_fingerprint") != report.dataset_fingerprint:
        changed.append(
            f"标注集已变（{previous.get('dataset_fingerprint')} -> {report.dataset_fingerprint}）"
        )
    if previous.get("config_fingerprint") != report.config_fingerprint:
        changed.append(
            f"检索配置已变（{previous.get('config_fingerprint')} -> {report.config_fingerprint}）"
        )
    if previous.get("k") != report.k:
        changed.append(f"k 已变（{previous.get('k')} -> {report.k}）")
    # 两个变体（守门集/难题集）不可互比：混比会让「换了评测面」伪装成趋势
    prev_variant = previous.get("variant") or "default"
    if prev_variant != report.variant:
        changed.append(f"评测变体已变（{prev_variant} -> {report.variant}）")
    if changed:
        return {
            "status": "not_comparable",
            "message": "配置或标注集已变，指标不可直接比较",
            "changes": changed,
        }

    regressions = []
    for ch in CHANNELS:
        prev_hits = int(previous.get(f"{ch}_hits") or 0)
        prev_mrr = float(previous.get(f"{ch}_mrr") or 0.0)
        now = report.summary.get(ch, {})
        if now.get("hits", 0) < prev_hits:
            regressions.append(
                f"{ch}: 命中 {prev_hits} -> {now.get('hits')}（降 {prev_hits - now.get('hits', 0)}）"
            )
        if now.get("mrr", 0.0) < prev_mrr:
            regressions.append(f"{ch}: MRR {prev_mrr} -> {now.get('mrr')}")
        prev_ndcg = previous.get(f"{ch}_ndcg")
        if prev_ndcg is not None and now.get("ndcg", 0.0) < float(prev_ndcg):
            regressions.append(f"{ch}: nDCG {prev_ndcg} -> {now.get('ndcg')}")
    if regressions:
        return {"status": "regression", "message": "检出回归", "details": regressions}
    return {"status": "ok", "message": "与上次相比无回归"}
