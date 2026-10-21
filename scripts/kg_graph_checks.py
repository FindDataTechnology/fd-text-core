"""图 schema 判据检查（只读，只取名字，绝不拉向量）。

**为什么不用 `MATCH (n) RETURN n`**：legal_v2 有 7 万节点、每个带 1024 维
`name_embedding`，整节点回传等于把 ~300MB 结果集在小内存机上物化——
2026-10-22 实测这么查之后 zihan 用户态整体卡死近半小时（sshd/HTTP 都只
握手不回包）。判据只需要名字，`RETURN n.name` 让服务端不碰向量。

判据：
  ①「法规名+条号」分裂节点清零（条级不入图 schema）
  ②同名不同文号实体可分（113/71 口径）
  ③规模统计（节点/边/条级属性）
  ④脏文号（重复机关名）清零

用法（zihan）:
    python scripts/kg_graph_checks.py legal_v2 gov_v3
    python scripts/kg_graph_checks.py legal_v2 --json-out /tmp/checks.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys

_ARTICLE = re.compile(r"第[0-9零一二三四五六七八九十百千]+条(?:之[一二三四五六七八九十]+)?$")
_SUFFIX = ("法", "法典", "条例", "规定", "办法", "细则", "解释", "决定", "通知", "批复")


def _is_dirty(node_name: str) -> bool:
    """脏文号判据与修复脚本同一实现（只看括号里的文号，不看正文里的国名）。"""
    from kg_statute_repair_names import is_dirty_name

    return is_dirty_name(node_name)


def check_graph(db, gid: str) -> dict:
    g = db.select_graph(gid)
    names = [r[0] or "" for r in g.query("MATCH (n) RETURN n.name").result_set]
    edges = g.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    statute = g.query(
        "MATCH (n:Entity) WHERE n.law_id IS NOT NULL RETURN count(n)").result_set[0][0]
    split = [n for n in names
             if (m := _ARTICLE.search(n)) and n[: m.start()].endswith(_SUFFIX)]
    dirt = [n for n in names if _is_dirty(n)]
    rules = sorted(n for n in names if "行政单位财务规则" in n)
    out = {
        "graph": gid, "nodes": len(names), "edges": edges,
        "statute_layer_nodes": statute,
        "split_article_nodes": len(split), "split_samples": split[:10],
        "dirty_docno_nodes": len(dirt), "dirty_samples": dirt[:10],
        "行政单位财务规则": rules,
    }
    print(f"[{gid}] nodes={len(names)} edges={edges} statute_layer={statute}")
    print(f"[{gid}] 判据① 条级分裂节点: {len(split)} -> "
          f"{'PASS' if not split else 'FAIL'}")
    for n in split[:5]:
        print(f"    SPLIT: {n}")
    print(f"[{gid}] 判据④ 脏文号节点: {len(dirt)} -> "
          f"{'PASS' if not dirt else 'FAIL'}")
    for n in dirt[:5]:
        print(f"    DIRT: {n}")
    print(f"[{gid}] 判据② 行政单位财务规则*: {rules or '(none)'}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("graphs", nargs="*", default=["legal_v2", "gov_v3"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6889)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    from falkordb import FalkorDB

    db = FalkorDB(host=args.host, port=args.port)
    results = [check_graph(db, gid) for gid in args.graphs]
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)
    bad = [r for r in results
           if r["split_article_nodes"] or r["dirty_docno_nodes"]]
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.exit(main())
