"""法规层确定性建图 CLI（kg-lane-delivery-v1 任务 2.3）。

用法示例（只读 law_db 抽样核对）：

    python scripts/kg_statute_build.py \
        --dsn postgresql://.../law_db \
        --group-id legal-statute-v1 \
        --dry-run --limit 500 --order seed --seed kg-audit-seed42 \
        --residual-out /tmp/kg_statute/residual.tsv \
        --report-out /tmp/kg_statute/report.json

全量建图（任务 3 由主线执行，去掉 --dry-run 即写图）：

    python scripts/kg_statute_build.py --dsn ... --group-id ... \
        --falkordb-uri bolt://127.0.0.1:6889 \
        --residual-out ... --report-out ...

纪律：law_db 只读（唯一 SQL 是 SELECT）；--dry-run 只出计划统计不写图。
LLM 成本恒为零（stats.llm_tokens = 0，spec「法规层全量建图零 LLM 成本」）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fd_text_core.kg_lane.statute_layer import (  # noqa: E402
    StatuteGraphWriter,
    build_statute_graph,
)

# 只读：head/tail 切片在 SQL 侧完成（right(content,2000)/left(content,600)），
# 避免全文字段过网。document_no 来自 legal_documents（仅 gzk 可关联，LEFT JOIN
# 其余源为空串——普查 §1.2）。
FETCH_SQL = """
SELECT l.id, l.src_db, l.title, l.type, l.status,
       coalesce(l.publish, '')  AS publish,
       coalesce(l.expiry, '')   AS expiry,
       left(l.content, 600)     AS head_text,
       right(l.content, 2000)   AS tail_text,
       coalesce(d.metadata->>'document_no', '') AS document_no
FROM laws l
LEFT JOIN legal_documents d ON l.source_id = d.content_hash
WHERE l.content_status = 'ok'
{order_clause}
{limit_clause}
"""

KNOWN_LIMITS = [
    "gzk 源 status 一刀切「有效」（10,523 行）且 31% 正文含「废止」字样——"
    "废止态是 gzk 系统盲区；本建图器只建可确证废止边（标题决定形/附则《》句式），"
    "不强判（普查坑 2）",
    "「通过日推导」为近似（施行日≈通过日，人大通过即公布的法律两者同日）；"
    "来源单独记账 head_pass_derived，可按普查口径剔除复算（坑 3 扩展）",
    "expiry 字段实为施行日而非失效日（普查坑 1）；脏值 1899-12-31 由年份窗拒绝",
    "决定类文书用「生效」而非「施行」（普查 §6）不在句式覆盖内，进残余不猜",
    "status∈已废止/失效但无可确证废止源行的，只在节点属性记状态，不建废止边"
    "（stats.repeal.status_repealed_without_edge 计数）",
]


def fetch_records(dsn: str, *, limit: int = 0, order: str = "id",
                  seed: str = "kg-audit-seed42") -> list[dict]:
    """只读拉取 law_db 行（SELECT only）。order=seed 用固定 md5 种子序（可复算）。"""
    import psycopg  # 懒加载：单测不碰真库

    if order == "seed":
        order_clause = "ORDER BY md5(%s || l.id::text)"
        params: list = [seed]
    else:
        order_clause = "ORDER BY l.id"
        params = []
    limit_clause = "LIMIT %s" if limit else ""
    if limit:
        params.append(limit)
    sql = FETCH_SQL.format(order_clause=order_clause, limit_clause=limit_clause)
    out: list[dict] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            for row in cur.fetchall():
                out.append(dict(zip(cols, row)))
    return out


RESIDUAL_HEADER = ["law_id", "title", "src_db", "status", "missing", "publish",
                   "type", "effective_source", "doc_number", "tags"]


def write_residual(path: str, plan) -> int:
    """残余清单 TSV 落档（前 7 列与普查 residual.tsv 同构，可对账）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        fh.write("\t".join(RESIDUAL_HEADER) + "\n")
        for r in plan.residual:
            fh.write("\t".join(
                str(r.get(k, "")).replace("\t", " ").replace("\n", " ")
                for k in RESIDUAL_HEADER) + "\n")
    return len(plan.residual)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", required=True, help="law_db 只读 DSN（postgresql://…）")
    ap.add_argument("--falkordb-uri", default="bolt://127.0.0.1:6889")
    ap.add_argument("--group-id", required=True, help="目标图名（= 车道 group_id）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只出计划统计，不写图")
    ap.add_argument("--residual-out", default="", help="残余清单 TSV 路径")
    ap.add_argument("--report-out", default="", help="建图报告 JSON 路径")
    ap.add_argument("--limit", type=int, default=0, help="抽样行数（0=全量）")
    ap.add_argument("--order", choices=["id", "seed"], default="id",
                    help="id=按 id 序；seed=固定 md5 种子序（抽样可复算）")
    ap.add_argument("--seed", default="kg-audit-seed42",
                    help="种子序的种子串（默认沿用普查 seed42，样本前 400 与普查 sample400 重叠）")
    args = ap.parse_args(argv)

    records = fetch_records(args.dsn, limit=args.limit, order=args.order, seed=args.seed)
    print(f"fetched {len(records)} rows (order={args.order}"
          + (f" seed={args.seed}" if args.order == "seed" else "") + ")",
          flush=True)

    plan = build_statute_graph(records, group_id=args.group_id)

    written = None
    if not args.dry_run:
        writer = StatuteGraphWriter(args.falkordb_uri, args.group_id).connect()
        written = writer.upsert_plan(plan)
        print(f"upserted: {written}")

    if args.residual_out:
        n = write_residual(args.residual_out, plan)
        print(f"residual rows written: {n} -> {args.residual_out}")

    report = {
        "group_id": args.group_id,
        "mode": "dry-run" if args.dry_run else "write",
        "input": {"rows": len(records), "limit": args.limit, "order": args.order,
                  "seed": args.seed if args.order == "seed" else None},
        "upserted": written,
        "stats": plan.stats,
        "contradictions": plan.contradictions,
        "residual_out": args.residual_out or None,
        "residual_rows": len(plan.residual),
        "provenance_index_rows": len(plan.provenance),
        "llm_tokens": 0,
        "known_limits": KNOWN_LIMITS,
    }
    if args.report_out:
        rp = Path(args.report_out)
        rp.parent.mkdir(parents=True, exist_ok=True)
        # provenance 索引一并落档：任务 3/挂载层建 corpus_index 的输入
        rp.write_text(json.dumps(
            {**report, "provenance_index": plan.provenance},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"report written -> {args.report_out}")

    s = plan.stats
    print(json.dumps({
        "rows": s["rows"],
        "effective": s["effective"],
        "repeal": s["repeal"],
        "amendment": s["amendment"],
        "graph": s["graph"],
        "residual": s["residual"],
        "contradictions": len(plan.contradictions),
        "llm_tokens": 0,
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
