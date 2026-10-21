"""从引擎 DB 导出某垂域的 episode JSONL（只读）。

用法:
    python scripts/kg_export_episodes.py --domain gov --out /tmp/gov_episodes.jsonl

输出字段与试点 eps 一致（episode_id/kind/content/valid_at/provenance），
使跑批器对两个垂域一视同仁。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dsn", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    dsn = args.dsn or os.environ.get("FD_TEXT_DSN") or os.environ.get("FD_DATABASE_URL")
    if not dsn:
        raise SystemExit("need --dsn or FD_TEXT_DSN")

    import psycopg

    sql = """
        select id, source, source_id, title, content, metadata, provenance
        from corpus_documents
        where domain = %s and status = 'current' and content is not null
        order by id
    """
    written = 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(dsn) as conn, out.open("w", encoding="utf-8") as fh:
        with conn.cursor() as cur:
            cur.execute(sql, (args.domain,))
            for doc_id, source, source_id, title, content, meta, prov in cur:
                meta = meta or {}
                prov = prov or {}
                valid_at = meta.get("doc_date") or meta.get("published") or prov.get("as_of")
                rec = {
                    "episode_id": f"{args.domain}:{source}:{source_id or doc_id}",
                    # kind 用文种：让跑批按文种分组看成本，也便于后续时点查询挑样本
                    "kind": meta.get("doc_type") or source,
                    "content": content,
                    "valid_at": valid_at,
                    "provenance": {
                        "source_db": "fd_text",
                        "stable_id": f"corpus_documents:{doc_id}",
                        "doc_title": title,
                        "issuer": meta.get("issuer"),
                        "doc_number": meta.get("doc_number"),
                        "as_of": valid_at,
                    },
                }
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written += 1
                if args.limit and written >= args.limit:
                    break
    print(f"wrote {written} episodes -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
