"""双口径评测的 KG 侧探针：逐题落档 KG 原始证据（不自动评分）。

为什么只做 KG 侧：基线侧要走现役 MCP 工具（另一台机器/另一套凭据），
两边分开跑再并排，比一个脚本里既连图库又连 MCP 更好排查。

用法（zihan）:
    set -a; . /home/ubuntu/lexkg-data/pilot.env; set +a
    python scripts/kg_lane_eval_probe.py --vertical legal \
        --questions <path> --episodes /home/ubuntu/lexkg-data/pilot_episodes.jsonl \
        --out /home/ubuntu/lexkg-data/lane_eval_legal.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fd_text_core.kg_lane import config as cfg_mod
from fd_text_core.kg_lane import cost as cost_mod
from fd_text_core.kg_lane import eval as eval_mod
from fd_text_core.kg_lane import lane as lane_mod


def load_episodes(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertical", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--episodes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg_path = args.config or (
        Path(__file__).resolve().parents[1] / "fd_text_core/kg_lane/verticals"
        / f"{args.vertical}.kg.json"
    )
    cfg = cfg_mod.load(cfg_path)
    settings = lane_mod.settings_from_env()
    ledger = cost_mod.CostLedger(f"{args.out}.ledger.jsonl").load_existing()
    graphiti = lane_mod.build_graphiti(settings, ledger=ledger)
    kg = lane_mod.KGLane(config=cfg, settings=settings, graphiti=graphiti, ledger=ledger)
    kg.corpus_index = lane_mod.build_corpus_index(load_episodes(args.episodes))

    questions = eval_mod.load_questions(args.questions)
    if args.limit:
        questions = questions[: args.limit]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:  # noqa: ASYNC230 —— 一次性离线探针
        for q in questions:
            evidence: list[dict] = []
            as_of = None
            if q.as_of:
                try:
                    as_of = datetime.fromisoformat(q.as_of)
                except ValueError:
                    evidence.append({"error": f"as_of 不是合法 ISO8601: {q.as_of!r}"})
            try:
                facts = await kg.query_surface().search_facts(q.query, limit=8, as_of=as_of)
                evidence.extend(f.to_dict() for f in facts)
                # 多跳题补二跳链条：从命中事实的主体出发游走
                if q.kind == "multihop" and facts:
                    seen = {e.get("fact") for e in evidence}
                    for start in {f.subject for f in facts[:2] if f.subject}:
                        for view in await kg.query_surface().walk(start, max_hops=2):
                            d = view.to_dict()
                            if d["fact"] not in seen:
                                evidence.append(d)
                                seen.add(d["fact"])
            except Exception as exc:  # noqa: BLE001 - 单题故障不中断整批
                evidence.append({"error": f"{type(exc).__name__}: {exc}"})
            row = {
                "id": q.id, "kind": q.kind, "query": q.query,
                "as_of": q.as_of or None,
                "expectation": q.expectation, "verdict_rule": q.verdict_rule,
                "kg_evidence": evidence,
                "verdict": None,  # 人工评阅位
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"{q.id}: {len(evidence)} evidence facts", flush=True)

    await graphiti.close()
    print(json.dumps(kg.summarize_costs(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
