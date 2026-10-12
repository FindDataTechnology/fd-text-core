#!/usr/bin/env python3
"""向量索引双读等价校验（只读）。

用途：任何改变向量存取路径的变更（新建索引 / 重建索引 / 索引类型切换）在验收前，
必须在同一数据、同一查询集上分别取「变更前」与「变更后」两个通道的结果做比对。
照 fd-open-data-mcp/scripts/verify_vector_dual_read.py 的既有手法。

本脚本不修改数据，不建也不删索引。它只负责「取结果」与「比对」：

  1. 以 index 通道取一组查询的 top-K
  2. 以 seqscan 通道（强制 enable_seqscan=on, enable_indexscan=off）取同一组查询的 top-K
  3. 逐条比对结果集合与排序，输出一致 / 不一致清单

用法：
    # 产出当前基线（变更前）
    python scripts/verify_vector_dual_read.py --out baseline.json
    # 变更落地后比对
    python scripts/verify_vector_dual_read.py --against baseline.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 每个垂域各取若干 representative 向量作为查询探针（取该域有嵌入的第一条）
PROBE_DOMAINS = ("screenplay", "papers")
K = 10


def _conn():
    import psycopg

    url = os.environ.get("FDTEXT_DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("缺 FDTEXT_DATABASE_URL：本校验必须连接 fd_text 实跑，离线无意义")
    return psycopg.connect(url)


def fetch_topk(cur, domain: str, probe_sql: str, mode: str, k: int) -> list[str]:
    """以指定执行模式取某域某探针向量的 top-K 结果 stable 标识序列。"""
    if mode == "index":
        prefix = "SET LOCAL enable_seqscan = on; SET LOCAL enable_indexscan = on; SET LOCAL hnsw.ef_search = 200;"
    else:  # seqscan 通道：关掉索引扫描，强制走排序比较
        prefix = "SET LOCAL enable_seqscan = on; SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off;"
    cur.execute(prefix)
    cur.execute(
        f"""
        SELECT key FROM (
            SELECT (c.document_id::text || ':' || c.chunk_no::text) AS key,
                   c.embedding <=> ({probe_sql}) AS dist
            FROM corpus_chunks c
            WHERE c.domain = %s AND c.embedding IS NOT NULL
            ORDER BY dist
            LIMIT %s
        ) t
        """,
        (domain, k),
    )
    return [r[0] for r in cur.fetchall()]


def collect(conn) -> dict:
    out: dict = {}
    with conn.cursor() as cur:
        for dom in PROBE_DOMAINS:
            # 探针数量：该域有嵌入的前若干条的 stable_id（作为查询锚点）
            cur.execute(
                "SELECT (document_id::text || ':' || chunk_no::text) FROM corpus_chunks "
                "WHERE domain=%s AND embedding IS NOT NULL ORDER BY id LIMIT 3",
                (dom,),
            )
            probes = [r[0] for r in cur.fetchall()]
            if not probes:
                continue
            out[dom] = []
            for p in probes:
                probe_sql = (
                    "SELECT embedding FROM corpus_chunks WHERE domain=%s "
                    "AND (document_id::text || ':' || chunk_no::text)=%s LIMIT 1"
                )
                # PSQL 参数化：把具体的探针取值嵌入子查
                idx_ids = fetch_topk(
                    cur, dom, probe_sql % ("'%s'" % dom, "'%s'" % p), "index", K
                )
                seq_ids = fetch_topk(
                    cur, dom, probe_sql % ("'%s'" % dom, "'%s'" % p), "seqscan", K
                )
                out[dom].append({"probe": p, "index": idx_ids, "seqscan": seq_ids})
    return out


def compare(base: dict, new: dict) -> int:
    """比对两份快照，返回不一致条数。"""
    diffs = 0
    total = 0
    for dom in sorted(set(base) | set(new)):
        b_rows = {r["probe"]: r for r in base.get(dom, [])}
        n_rows = {r["probe"]: r for r in new.get(dom, [])}
        for probe in sorted(set(b_rows) | set(n_rows)):
            total += 1
            b = b_rows.get(probe)
            n = n_rows.get(probe)
            if not b or not n:
                print(f"  ✗ {dom}/{probe}: 探针在一侧缺失")
                diffs += 1
                continue
            if b["index"] != n["index"]:
                print(f"  ✗ {dom}/{probe}: index 通道前后不一致")
                print(f"      before={b['index'][:3]}...")
                print(f"      after ={n['index'][:3]}...")
                diffs += 1
            elif b["seqscan"] != n["seqscan"]:
                print(f"  ✗ {dom}/{probe}: seqscan 通道前后不一致")
                diffs += 1
            else:
                print(f"  ✓ {dom}/{probe}: 一致（top-{K} 集合与排序相同）")
    print(f"\n比对 {total} 个探针，不一致 {diffs} 个")
    return diffs


def self_channels(data: dict) -> int:
    """同一次快照内，index 通道与 seqscan 通道互相印证。"""
    diffs = 0
    total = 0
    for dom, rows in data.items():
        for r in rows:
            total += 1
            if r["index"] == r["seqscan"]:
                print(f"  ✓ {dom}/{r['probe']}: index 与 seqscan 一致")
            else:
                print(f"  ✗ {dom}/{r['probe']}: 两通道不一致")
                print(f"      index  ={r['index'][:3]}...")
                print(f"      seqscan={r['seqscan'][:3]}...")
                diffs += 1
    print(f"\n双通道比对 {total} 个探针，不一致 {diffs} 个")
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser(description="向量索引双读等价校验")
    ap.add_argument("--against", help="与基线快照比对（变更落地后使用）")
    ap.add_argument("--out", help="把当前快照写入该文件（变更前取基线）")
    args = ap.parse_args()

    conn = _conn()
    try:
        snap = collect(conn)
    finally:
        conn.close()

    if args.out:
        Path(args.out).write_text(json.dumps(snap, ensure_ascii=False, indent=1))
        print(f"基线快照已写入 {args.out}")
        print("（此时尚未落地索引变更；变更完成后用 --against 比对）")
        return 0

    if args.against:
        base = json.loads(Path(args.against).read_text())
        print("—— 变更前 vs 变更后 ——")
        return 1 if compare(base, snap) else 0

    print("—— 单次执行：index 通道 vs seqscan 通道 ——")
    return 1 if self_channels(snap) else 0


if __name__ == "__main__":
    sys.exit(main())
