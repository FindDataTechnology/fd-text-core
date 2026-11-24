#!/usr/bin/env python3
"""回填 gov 垂域的 facet 字段（issuer 归一 / doc_type / period）。

为什么需要单独的回填脚本：引擎的摄取契约是「content_hash 未变则跳过」
（`ingest.load_documents`），故**修正派生 metadata 不会随重跑生效**——
重跑只会得到 `skipped=209`。这类「内容没变但派生字段要重算」的修正是运维动作，
不改引擎语义。

本脚本幂等：可反复跑，每次按当前解析器与词表重算 facet 字段。
只更新 metadata 里的 facet 三键，不碰 content / provenance / 切块 / 向量。

用法：
    export FDTEXT_DATABASE_URL=...
    python3 scripts/backfill_gov_facets.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core.db import execute, fetch_all
from fd_text_core.verticals.gov import FACETS
from fd_text_core.verticals.gov.parser import (
    _detect_doc_type, _issuer_from_title, _normalize_issuer, _split_sections,
)


def facet_sections(content: str | None) -> list[list[str]]:
    """从正文现算层级结构（sections），供采集侧没抽过的文档（如 govcn）。"""
    if not content:
        return []
    return [[path, text] for path, text in _split_sections(content)]

DOMAIN = "gov"


def facet_issuer(raw) -> str | None:
    if not raw:
        return None
    s = _normalize_issuer(str(raw))
    if not s:
        return None
    return s if s in FACETS["issuer"] else "其他"


def facet_doc_type(raw) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    return s if s in FACETS["doc_type"] else None


def facet_period(raw) -> str | None:
    if not raw:
        return None
    m = re.match(r"(\d{4})", str(raw))
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告将变更的条数，不写库")
    args = ap.parse_args()

    rows = fetch_all(
        "SELECT id, title, content, metadata FROM corpus_documents"
        " WHERE domain=%s AND status='current' ORDER BY id",
        (DOMAIN,),
    )
    changed = 0
    detail: dict[str, int] = {}
    for row in rows:
        md = dict(row["metadata"] or {})
        before = (md.get("issuer"), md.get("doc_type"), md.get("period"),
                  md.get("sections"))
        # issuer 归一的输入：优先 issuer_raw（采集侧原值），回落当前 issuer，
        # 再回落标题前缀（采集侧没抽过的源，如 govcn，只能从标题推）。
        raw_issuer = md.get("issuer_raw") or md.get("issuer") or _issuer_from_title(row["title"] or "")
        md["issuer"] = facet_issuer(raw_issuer)
        # doc_type：采集侧已抽则归一（词表外置 None）；没抽则从标题现算（政策库高频
        # 文种如条例/办法靠结尾词判定，纯标题即可，不必重抓 HTML）。
        md["doc_type"] = facet_doc_type(md.get("doc_type")) or _detect_doc_type(row["title"] or "")
        md["period"] = facet_period(md.get("doc_date") or md.get("published"))
        # sections：采集侧（load_gov 的公文解析器）已抽则保留；没抽则从正文现算
        # （govcn 走 news loader，正文已在 content 列，纯文本即可切层级）。
        if not md.get("sections"):
            secs = facet_sections(row["content"])
            if secs:
                md["sections"] = secs
        after = (md.get("issuer"), md.get("doc_type"), md.get("period"),
                 md.get("sections"))
        if before == after:
            continue
        changed += 1
        labels = ("issuer", "doc_type", "period", "sections")
        for k, b, a in zip(labels, before, after):
            if b == a:
                continue
            # sections 是列表，detail 只记段数变化（不打印整段）
            if k == "sections":
                bn = len(b or [])
                an = len(a or [])
                detail[f"{k}: {bn}段 -> {an}段"] = detail.get(f"{k}: {bn}段 -> {an}段", 0) + 1
            else:
                detail[f"{k}: {b!r} -> {a!r}"] = detail.get(f"{k}: {b!r} -> {a!r}", 0) + 1
        if not args.dry_run:
            execute(
                "UPDATE corpus_documents SET metadata=%s::jsonb, updated_at=now() WHERE id=%s",
                (json.dumps(md, ensure_ascii=False), row["id"]),
            )

    print(f"[{'dry-run' if args.dry_run else 'applied'}] 扫描 {len(rows)} 篇，需变更 {changed} 篇")
    for k, n in sorted(detail.items(), key=lambda x: -x[1])[:20]:
        print(f"  {n:4d}  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
