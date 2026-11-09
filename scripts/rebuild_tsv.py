#!/usr/bin/env python3
"""重建某垂域的 tsvector（归一化/分词路径换代后的一次性迁移）。

为什么需要显式脚本：`ensure_chunks` 幂等跳过已有块（`DO NOTHING` on conflict），
所以换分词路径后旧 tsv 会一直留着——查询用新 token、索引是旧 token，静默 0 命中。
本脚本走「重算 tsv 列」而非「删块重建」：块文本/char-offset/向量全不动
（无损不变量的边界：损的只是派生数据 tsv，不是原文），因此也不需要重新嵌入。

用法：
    export FDTEXT_DATABASE_URL=...
    python3 scripts/rebuild_tsv.py --domain screenplay [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core.db import execute, fetch_all, fetch_one
from fd_text_core.tokenize import cjk_tokens_text
from fd_text_core.verticals.registry import get as get_vertical


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True, help="垂域名（如 screenplay）")
    ap.add_argument("--batch", type=int, default=200, help="每批重算块数")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    args = ap.parse_args()

    try:
        v = get_vertical(args.domain)
    except KeyError:
        print(f"[rebuild] 未注册垂域：{args.domain}", file=sys.stderr)
        return 2
    if not v.cjk:
        # 非 CJK 垂域用 PG 直通配置，重建是 no-op（tsv 由 to_tsvector(config, text) 生成）
        print(f"[rebuild] 垂域 {args.domain} 未启用引擎分词（cjk=False），无需重建")
        return 0

    total = fetch_one("SELECT count(*)::int AS n FROM corpus_chunks WHERE domain=%s", (args.domain,))["n"]
    empty = fetch_one(
        "SELECT count(*)::int AS n FROM corpus_chunks WHERE domain=%s AND tsv IS NULL", (args.domain,)
    )["n"]
    print(f"[rebuild] {args.domain}: 块 {total}，tsv 空 {empty}，t2s={v.t2s}")
    if args.dry_run:
        print("[rebuild] dry-run：不写库")
        return 0

    done = 0
    while True:
        rows = fetch_all(
            """
            SELECT domain, document_id, chunk_no, chunk_text
            FROM corpus_chunks WHERE domain=%s
            ORDER BY document_id, chunk_no
            OFFSET %s LIMIT %s
            """,
            (args.domain, done, args.batch),
        )
        if not rows:
            break
        for r in rows:
            # 与写入侧 ensure_chunks 同一函数同一开关——同构不变量的单点
            execute(
                """
                UPDATE corpus_chunks SET tsv = to_tsvector('simple', %s), tsv_config = 'engine-cjk'
                WHERE domain=%s AND document_id=%s AND chunk_no=%s
                """,
                (
                    cjk_tokens_text(r["chunk_text"], t2s=v.t2s),
                    r["domain"], r["document_id"], r["chunk_no"],
                ),
            )
        done += len(rows)
        print(f"[rebuild] 已重算 {done}/{total}")

    after = fetch_one(
        "SELECT count(*)::int AS n FROM corpus_chunks WHERE domain=%s AND tsv IS NOT NULL", (args.domain,)
    )["n"]
    print(f"[rebuild] 完成：tsv 非空 {after}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
