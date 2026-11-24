#!/usr/bin/env python3
"""回填任意垂域的 facet（调该垂域的投影重算 metadata，零重嵌）。

为什么需要单独的回填脚本：引擎的摄取契约是「content_hash 未变则跳过」
（`ingest.load_documents`），故**修正派生 metadata 不会随重跑生效**——重跑只会得到
`skipped=N`。这类「内容没变但派生字段要重算」的修正是运维动作，不改引擎语义。

为什么按域名参数化（change: unify-facet-projection-at-source D5）：facet 怎么算是垂域
自己的知识（`verticals.<域>.facets.project_facets`）。本脚本只是通用外壳——按域名取投影、
算、与现 metadata 比、有差则 UPDATE。增量入库（load_news_spool）调的是**同一个投影**，
故两条路径产物逐键相等（design D5 不变量）。

只更新 metadata：不碰 content / provenance / 切块 / 向量（正文未变即向量有效，
content_hash 也不含 metadata，故不会产生新版本、不触发重嵌）。幂等：可反复跑。

用法：
    export FDTEXT_DATABASE_URL=...
    python3 scripts/backfill_facets.py --domain gov [--dry-run]
    python3 scripts/backfill_facets.py --domain news [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core.db import execute, fetch_all
from fd_text_core.verticals.registry import get, get_facet_projector


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True, help="垂域名（gov/news/...）")
    ap.add_argument("--dry-run", action="store_true", help="只报告将变更的条数，不写库")
    ap.add_argument("--batch", type=int, default=500, help="每批 UPDATE 行数")
    args = ap.parse_args()

    domain = args.domain
    project = get_facet_projector(domain)
    if project is None:
        print(f"[error] 垂域 {domain} 没有 facet_projector（不投影 facet），无事可做")
        return 2
    facet_keys = sorted(get(domain).facets)

    rows = fetch_all(
        "SELECT id, title, content, cold_block_key, metadata, source, source_id,"
        " metadata->>'published' AS published"
        " FROM corpus_documents WHERE domain=%s AND status='current' ORDER BY id",
        (domain,),
    )
    changed = 0
    detail: dict[str, int] = {}
    for row in rows:
        md = dict(row["metadata"] or {})
        projected = project(
            {
                "title": row["title"] or "",
                "content": row["content"],
                "published": row["published"],
                "metadata": md,
                "source": row["source"],
                "cold_block_key": row["cold_block_key"],
                "domain": domain,
            }
        )
        # 只比对本垂域声明的 facet 键（sections 不在 facets 词表里，单独处理）
        before = {k: md.get(k) for k in projected}
        after = {k: projected.get(k) for k in projected}
        if before == after:
            continue
        changed += 1
        for k in projected:
            if before[k] != after[k]:
                bk, ak = before[k], after[k]
                if k == "sections":
                    bk, ak = f"{len(bk or [])}段", f"{len(ak or [])}段"
                detail[f"{k}: {bk!r} -> {ak!r}"] = detail.get(f"{k}: {bk!r} -> {ak!r}", 0) + 1
        if not args.dry_run:
            md.update(projected)
            execute(
                "UPDATE corpus_documents SET metadata=%s::jsonb, updated_at=now() WHERE id=%s",
                (json.dumps(md, ensure_ascii=False), row["id"]),
            )

    print(
        f"[{'dry-run' if args.dry_run else 'applied'}] domain={domain} 扫描 {len(rows)} 篇，"
        f"需变更 {changed} 篇（facet 键 {facet_keys} + sections）"
    )
    for k, n in sorted(detail.items(), key=lambda x: -x[1])[:20]:
        print(f"  {n:6d}  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
