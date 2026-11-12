#!/usr/bin/env python3
"""回填 news 垂域的 facet 字段（source_type / lang / has_body）。

为什么需要单独的回填脚本：引擎的摄取契约是「content_hash 未变则跳过」
（`ingest.load_documents`），故**补写一个派生 metadata 键不会随重跑生效**——
把 spool 重跑一遍只会得到 `skipped=5908`。这类「内容没变但派生字段要补」的修正是
运维动作，不改引擎语义（与 `backfill_gov_facets.py` 同款来源）。

背景（2026-11-13 实测，fd-prod）：news 词表声明了 source_type / lang / has_body 三件，
加载器只落了前两件，`has_body` 一个都没有 → 任何 `has_body` 过滤恒 0 命中。
症状与 gov 那次完全相同：**空集被读成"没有这类文档"，而不是"字段没写"**。

本脚本幂等：可反复跑，每次按当前判定重算 facet 键。
只更新 metadata 里的 facet 键，不碰 content / provenance / 切块 / 向量。

用法：
    export FDTEXT_DATABASE_URL=...
    python3 scripts/backfill_news_facets.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core.db import execute, fetch_all
from fd_text_core.verticals.news import FACETS

DOMAIN = "news"
KEYS = ("source_type", "lang", "has_body")


def facet_has_body(md: dict, row: dict) -> str:
    """正文是否可取。与加载器同判据：有 content 或有冷存指针即可取。"""
    if (row.get("content") or "").strip():
        return "true"
    if row.get("cold_block_key"):
        return "true"           # 长正文落冷存，引擎可按 offset 取回
    return "false"


def facet_lang(md: dict) -> str:
    lang = str(md.get("lang") or "zh").strip().lower()
    return lang if lang in FACETS["lang"] else "zh"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告将变更的条数，不写库")
    args = ap.parse_args()

    rows = fetch_all(
        "SELECT id, metadata, content, cold_block_key FROM corpus_documents"
        " WHERE domain=%s AND status='current' ORDER BY id",
        (DOMAIN,),
    )
    changed = 0
    detail: dict[str, int] = {}
    for row in rows:
        md = dict(row["metadata"] or {})
        before = tuple(md.get(k) for k in KEYS)
        # source_type 已由加载器写入；缺失时按"词表外归基类"的既有规则补
        if md.get("source_type") not in FACETS["source_type"]:
            md["source_type"] = "announcement"
        md["lang"] = facet_lang(md)
        md["has_body"] = facet_has_body(md, row)
        after = tuple(md.get(k) for k in KEYS)
        if before == after:
            continue
        changed += 1
        for k, b, a in zip(KEYS, before, after):
            if b != a:
                key = f"{k}: {b!r} -> {a!r}"
                detail[key] = detail.get(key, 0) + 1
        if not args.dry_run:
            execute(
                "UPDATE corpus_documents SET metadata=%s::jsonb, updated_at=now() WHERE id=%s",
                (json.dumps(md, ensure_ascii=False), row["id"]),
            )

    print(f"[{'dry-run' if args.dry_run else 'applied'}] 扫描 {len(rows)} 篇，需变更 {changed} 篇")
    for k, n in sorted(detail.items(), key=lambda x: -x[1])[:20]:
        print(f"  {n:5d}  {k}")
    if changed and args.dry_run:
        print("（dry-run：未写库。去掉 --dry-run 才落库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
