"""原位修复法规层污点节点名（幂等；不重建整图）。

**为什么需要它**：law_db 正文里存在「财政部中华人民共和国财政部令第71号」
这类重复机关名拼写（76231 行原文如此）。建图器已按
`normalize_docno_issuer` 归一，但归一规则在重建**之后**才补齐
（commit 661d20c），所以 legal_v2 里残留了 23 个带脏文号的节点名。

**做法（不重建）**：脏节点的 uuid 是 uuid5(脏名)，改名会破坏
uuid↔name 一致性；因此不原地改名，而是
  1. DETACH DELETE 脏节点（连带其边）；
  2. 只对这批 law_id 重跑确定性建图（MERGE 到干净 uuid，边/episode 一并重建）；
  3. 只嵌入这批节点的名字（23 条，不是 35,687 条）。
整图其余节点不动，LLM 抽取成本恒为零。

用法：

    python scripts/kg_statute_repair_names.py \
        --dsn postgresql://.../law_db --falkordb-uri bolt://127.0.0.1:6889 \
        --group-id legal_v2 [--apply]

不带 `--apply` 只报告（dry-run）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fd_text_core.kg_lane import statute_layer as sl  # noqa: E402
from fd_text_core.kg_lane.embedding import build_embedder  # noqa: E402
from fd_text_core.kg_lane.merge import normalize_docno_issuer  # noqa: E402

from kg_statute_build import (  # noqa: E402
    FETCH_SQL,
    embedding_settings_from_env,
)

# 名字末尾的括号文号（全角/半角）
_PAREN = ("（", "(")
_CLOSE = ("）", ")")
# 文号形态（括号内容必须命中，否则是附注不是文号）
_DOCNO_LIKE = re.compile(r"第\s*[0-9〇零一二三四五六七八九十百]+\s*号")


def docno_of(name: str) -> str | None:
    """取名字末尾括号里的文号；无括号返回 None。"""
    s = (name or "").rstrip()
    if not s or s[-1] not in _CLOSE:
        return None
    depth = 0
    for i in range(len(s) - 1, -1, -1):
        ch = s[i]
        if ch in _CLOSE:
            depth += 1
        elif ch in _PAREN:
            depth -= 1
            if depth == 0:
                return s[i + 1: -1]
    return None


def is_dirty_name(name: str) -> bool:
    """污点判据：括号内容**是文号**（含「第N号」）且归一后与原文不同。

    必须先用文号形态卡一道：`X(附:中华人民共和国政府关于领海的声明)` 这类
    合法括号附注不是文号，套归一规则会把附注削成半句（实测误判 1 例）。
    """
    dn = docno_of(name)
    if not dn or not _DOCNO_LIKE.search(dn):
        return False
    return normalize_docno_issuer(dn) != dn


def find_dirty(graph) -> list[dict]:
    """扫图找污点节点（只读）。返回 [{uuid, name, law_id, clean_name, neighbors}]。

    `neighbors` 是相邻节点的 law_id 集合：DETACH DELETE 会连带走**别的法规
    指向它的边**（如 X AMENDS 污点节点 Y），那些边只能由 X 的建图计划重建，
    因此修复时要把这些邻接法规一并重跑（MERGE 幂等）。
    """
    r = graph.query("MATCH (n:Entity) RETURN n.uuid, n.name, n.law_id")
    out = []
    for uuid, name, law_id in r.result_set:
        if not is_dirty_name(name):
            continue
        dn = docno_of(name)
        i = name.rfind(dn)
        # 只替换文号本体，保留其后的右括号等字符
        clean = name[:i] + normalize_docno_issuer(dn) + name[i + len(dn):]
        nb = graph.query(
            "MATCH (a:Entity)-[:RELATES_TO]-(b:Entity {uuid:$u}) "
            "RETURN DISTINCT a.law_id", {"u": uuid})
        neighbors = sorted({row[0] for row in nb.result_set if row[0] is not None})
        out.append({"uuid": uuid, "name": name, "law_id": law_id,
                    "clean_name": clean, "neighbors": neighbors})
    return out


def fetch_by_law_ids(dsn: str, law_ids: list[int]) -> list[dict]:
    """只读拉取指定 law_id 的行（口径与建图 CLI 完全一致，只加 WHERE 过滤）。"""
    import psycopg

    sql = FETCH_SQL.format(order_clause="ORDER BY l.id", limit_clause="")
    sql = sql.replace("WHERE l.content_status = 'ok'",
                      "WHERE l.content_status = 'ok' AND l.id = ANY(%s)")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, [law_ids])
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--falkordb-uri", required=True)
    ap.add_argument("--group-id", required=True)
    ap.add_argument("--apply", action="store_true", help="真正写图（默认 dry-run）")
    ap.add_argument("--report-out", default="")
    args = ap.parse_args()

    from falkordb import FalkorDB

    host, port = sl.parse_uri(args.falkordb_uri)
    graph = FalkorDB(host=host, port=port).select_graph(args.group_id)

    dirty = find_dirty(graph)
    report: dict = {"group_id": args.group_id, "dirty": len(dirty), "applied": False,
                    "nodes": [{"uuid": d["uuid"], "law_id": d["law_id"],
                               "before": d["name"], "after": d["clean_name"]}
                              for d in dirty]}
    print(f"dirty nodes: {len(dirty)}")
    for d in dirty:
        print(f"  law_id={d['law_id']} {d['name']}\n    -> {d['clean_name']}")

    no_law = [d for d in dirty if d["law_id"] is None]
    if no_law:
        # 无 law_id 说明不是法规层节点（散文层理论上不会产生脏名）；不猜、不改。
        print(f"SKIP {len(no_law)} nodes without law_id (not statute-layer)")

    if not args.apply or not dirty:
        print(json.dumps({k: report[k] for k in ("group_id", "dirty", "applied")},
                         ensure_ascii=False))
        if args.report_out:
            Path(args.report_out).write_text(
                json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0

    # 1) 删污点节点（连带其边）
    for d in dirty:
        graph.query("MATCH (n:Entity {uuid:$u}) DETACH DELETE n", {"u": d["uuid"]})
    print(f"deleted {len(dirty)} dirty nodes (+incident edges)")

    # 2) 只对这批 law_id（含邻接法规）重跑确定性建图
    law_ids = sorted({d["law_id"] for d in dirty if d["law_id"] is not None}
                     | {n for d in dirty for n in d["neighbors"]})
    keep = fetch_by_law_ids(args.dsn, law_ids)
    print(f"refetched {len(keep)}/{len(law_ids)} law rows from law_db")
    report["rewritten_law_ids"] = law_ids
    plan = sl.build_statute_graph(keep, args.group_id)

    # 3) 只嵌入这批节点的名字（同一事件循环）
    settings = embedding_settings_from_env(os.environ)
    if settings is not None:
        embedder = build_embedder(settings)
        emb = sl.attach_name_embeddings(plan, embedder, model=settings.model,
                                        dim=settings.dim)
        report["embedding"] = {k: emb[k] for k in
                               ("nodes", "embedded", "degraded", "failed_batches")}
        print(f"embedding: {report['embedding']}")
    else:
        report["embedding"] = {"reason": "LEXKG_EMBED_* missing — skipped"}
        print("embedding skipped (no LEXKG_EMBED_* config)")

    writer = sl.StatuteGraphWriter(args.falkordb_uri, args.group_id).connect()
    counts = writer.upsert_plan(plan)
    report.update({"applied": True, "rewritten": counts})
    print(f"rewritten: {counts}")

    # 4) 复验：脏名清零 + 没有留下无名节点（EDGE_QUERY 的 MERGE 端点兜底可能
    #    建出只有 uuid 的空节点——那是删节点的副作用，必须查出来）
    left = find_dirty(graph)
    bare = graph.query(
        "MATCH (n:Entity) WHERE n.name IS NULL OR n.name = '' RETURN count(n)")
    report["dirty_after"] = len(left)
    report["bare_nodes"] = bare.result_set[0][0] if bare.result_set else 0
    print(f"dirty after repair: {len(left)}; bare nodes: {report['bare_nodes']}")
    if args.report_out:
        Path(args.report_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if not left and not report["bare_nodes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
