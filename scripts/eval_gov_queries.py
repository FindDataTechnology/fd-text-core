#!/usr/bin/env python3
"""公文垂域标注查询集跑分：hit@k 对比 keyword / vector / hybrid 三通道。

为什么建标注集（spec 任务 5.1）：只看「有返回」无法分辨检索质量——实测
by_number 预设能返回结果却把目标排在第 8 位。有标注才能把「排序是否对」
变成可比较的数字，也才能验证「混合检索不低于任一单通道」这条验收线。

指标是 **hit@k**（目标文档是否进前 k），不是精确率：公文间大量互相引用，
其它文档合理地含查询词，故「命中即正确」只在标注目标处可判定。

用法：
    export FDTEXT_DATABASE_URL=... FDTEXT_S3_*... FDTEXT_EMBEDDING_API_*
    python3 scripts/eval_gov_queries.py [--k 10] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core.embedding import embedder_from_env  # noqa: E402
from fd_text_core.querylib import hybrid_search  # noqa: E402

QUERY_SET = REPO / "tests" / "fixtures" / "gov" / "annotated_queries.json"


def load_queries(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["queries"]


def run(k: int) -> dict:
    queries = load_queries(QUERY_SET)
    emb = embedder_from_env()
    if emb is None:
        raise SystemExit("缺 FDTEXT_EMBEDDING_API_BASE / FDTEXT_EMBEDDING_API_KEY")

    per_channel: dict[str, list[dict]] = defaultdict(list)
    for item in queries:
        row = {"q": item["q"], "intent": item.get("intent"), "expect": item["expect_doc_id"]}
        for ch in ("keyword", "vector", "hybrid"):
            res = hybrid_search("gov", item["q"], embedder=emb, limit=k, channels=ch)
            ids = [x.get("document_id") for x in res["results"]]
            rank = ids.index(item["expect_doc_id"]) + 1 if item["expect_doc_id"] in ids else None
            row[ch] = rank
        per_channel["rows"].append(row)

    rows = per_channel["rows"]
    summary = {}
    for ch in ("keyword", "vector", "hybrid"):
        hits = sum(1 for r in rows if r[ch])
        mrr = sum((1.0 / r[ch]) for r in rows if r[ch]) / max(1, len(rows))
        summary[ch] = {"hit@k": f"{hits}/{len(rows)}", "hits": hits, "mrr": round(mrr, 4)}
    by_intent = defaultdict(lambda: {"total": 0, "hybrid_hits": 0})
    for r in rows:
        b = by_intent[r["intent"]]
        b["total"] += 1
        if r["hybrid"]:
            b["hybrid_hits"] += 1
    return {"k": k, "summary": summary, "by_intent": dict(by_intent), "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--json", default="", help="把完整结果写到该路径")
    args = ap.parse_args()

    out = run(args.k)
    print(f"=== 标注查询集跑分（k={out['k']}，{len(out['rows'])} 条查询）===")
    for ch, s in out["summary"].items():
        print(f"  {ch:8s} hit@{out['k']}: {s['hit@k']:>7s}   MRR={s['mrr']}")
    print("\n  按意图（hybrid 通道）：")
    for intent, b in sorted(out["by_intent"].items()):
        print(f"    {intent:16s} {b['hybrid_hits']}/{b['total']}")
    print("\n  逐条（rank=None 表示未进前 k）：")
    for r in out["rows"]:
        print(f"    [{r['intent']:14s}] {r['q'][:32]:34s} kw={str(r['keyword']):>4s} vec={str(r['vector']):>4s} hyb={str(r['hybrid']):>4s}")

    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n  结果已写 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
