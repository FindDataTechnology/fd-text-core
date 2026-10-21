"""基线侧双口径跑分：对现役 fd-legal-search-mcp 只读调用，逐题落档证据。

口径要点（试点教训，写死在代码里）：
  - **逐工具声明参数名**：`law_semantic_search` 收 `query`，而 `law_search` /
    `guide_cases_search` / `legal_docs_search` / `wenshu_search` 收 `title_query`。
    统一传 query 会把「参数错误」伪装成「基线答不出」（试点基线 3/6 -> 6/6 就栽在这）。
  - tools/list 先核对签名，与本脚本声明的映射比对，不一致就报错而不是硬跑。
  - 每题的每次调用错误都作为证据入档（标注 error 字段），不静默丢弃。

用法（zihan）:
    python scripts/eval_baseline_probe.py \
        --questions /home/ubuntu/lexkg-data/eval_questions_legal.json \
        --url http://cheap6.mesh.finddata.cn:31884/mcp \
        --out /home/ubuntu/lexkg-data/baseline_eval_legal.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fd_text_core.kg_lane import eval as eval_mod  # noqa: E402
from fd_text_core.kg_lane.eval import McpBaselineClient  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--url", default="http://cheap6.mesh.finddata.cn:31884/mcp")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    client = McpBaselineClient(args.url)
    questions = eval_mod.load_questions(args.questions)
    if args.limit:
        questions = questions[: args.limit]

    # 先用 tools/list 核对真实签名，防止参数名映射失真
    tools = await client.list_tools()
    declared = set(tools)
    for kind, names in (("temporal", ("law_semantic_search",)),
                        ("multihop", ("law_semantic_search", "guide_cases_search"))):
        for n in names:
            if n not in declared:
                print(f"WARN: {n} not in baseline tool list for {kind}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for q in questions:
            row = {"id": q.id, "kind": q.kind, "query": q.query,
                   "expectation": q.expectation, "verdict": None, "hits": []}
            for tool in (("law_semantic_search",)
                         if q.kind == "temporal"
                         else ("law_semantic_search", "guide_cases_search")):
                arg_name = ("query" if tool == "law_semantic_search" else "title_query")
                try:
                    hits = await client.call(tool, {arg_name: q.query})
                    row["hits"].append({"tool": tool, "arg": arg_name,
                                        "count": len(hits), "items": hits[:10]})
                    print(f"{q.id} {tool}({arg_name}): {len(hits)} hits", flush=True)
                except Exception as exc:  # noqa: BLE001 - 单题故障不中断
                    row["hits"].append({"tool": tool, "arg": arg_name,
                                        "error": f"{type(exc).__name__}: {exc}"})
                    print(f"{q.id} {tool}: ERROR {type(exc).__name__}", flush=True)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()

    print(f"baseline done -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
