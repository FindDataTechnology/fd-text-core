#!/usr/bin/env python3
"""检索评测跑分器（参数化，覆盖全部垂域）。

替代原 `scripts/eval_gov_queries.py`（公文专用、硬编码 domain 与数据集路径）。
实现与设计见 `fd_text_core/eval/`（change text-retrieval-eval）。

用法：
    export FDTEXT_DATABASE_URL=... FDTEXT_S3_*... FDTEXT_EMBEDDING_API_*
    python3 scripts/eval_retrieval.py --domain gov           # 单域
    python3 scripts/eval_retrieval.py --all                  # 全部已交付标注集
    python3 scripts/eval_retrieval.py --all --k 10 --store   # 落库并判回归
    python3 scripts/eval_retrieval.py --domain gov --channels keyword   # 快跑
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core.eval import (  # noqa: E402
    DatasetError,
    list_datasets,
    load_dataset,
)
from fd_text_core.eval import runner, store  # noqa: E402


def _print_report(rep, *, show_rows: bool = True) -> None:
    print(f"\n=== {rep.domain}（k={rep.k}，{rep.n_queries} 条查询，"
          f"判定强度={rep.judgment_strength}）===")
    if rep.missing_expects:
        print(f"  ⚠ 期望命中文档在库内缺失 {len(rep.missing_expects)} 条"
              "（标注已失效，非检索退化）：")
        for m in rep.missing_expects:
            print(f"      doc_id={m['expect_doc_id']} q={m['q'][:34]!r}")
    for ch in ("keyword", "vector", "hybrid"):
        s = rep.summary.get(ch)
        if not s:
            continue
        print(f"  {ch:8s} hit@{rep.k}: {s['hit@k']:>7s}   MRR={s['mrr']:<7} nDCG={s.get('ndcg')}")
    if rep.degraded:
        print(f"  降级：{rep.degraded}")
    if rep.saturated:
        # 饱和不是失败：回归守门集就该饱和（目标必须居首）。此处如实标注，
        # 防止把「数字好看」当成「度量有效」——重排收益请用难题集。
        print("  ⚠ 本集已饱和（MRR 上限）：对重排类改进无区分度；"
              "重排收益请用难题集（--variant hard）")

    # spec 的验收线：混合通道不低于任一单通道。
    # 难题集（hard）不适用该线：它在此暴露的是「RRF 对本集单通道强项有损」
    # 这一事实（实测 hybrid 0.9352 < vector 1.0——向量在本集很强，融合被
    # 关键词通道平均拖低）。该线是守门集（default）的准入判据；难题集是
    # 改进度量面，如实呈报数字即可，不以其判 FAIL。
    line = rep.hybrid_not_worse
    if line is None:
        print("  混合通道判定：不可判定（向量通道降级）")
    else:
        kw = rep.summary.get("keyword", {})
        vec = rep.summary.get("vector", {})
        hyb = rep.summary.get("hybrid", {})
        print(f"  混合 vs 单通道：命中 {hyb.get('hits')} vs keyword {kw.get('hits')} / "
              f"vector {vec.get('hits')}；MRR {hyb.get('mrr')} vs {kw.get('mrr')} / {vec.get('mrr')}")
        if rep.variant == "hard":
            verdict_txt = "混合不劣" if line else "⚠ 融合对单通道强项有损（难题集参考值，非验收线）"
            print(f"  混合 vs 单通道（难题集，非验收判据）：{verdict_txt}")
        else:
            print(f"  验收线（混合不低于任一单通道）：{'PASS' if line else 'FAIL'}")

    if rep.by_intent:
        print("  按意图（hybrid 命中）：")
        for intent, b in sorted(rep.by_intent.items()):
            print(f"    {intent:16s} {b['hits'].get('hybrid', 0)}/{b['total']}")

    if show_rows:
        print("  逐条（rank=None 表示未进前 k）：")
        for r in rep.rows:
            g = "G" if r.get("graded") else " "
            print(f"    [{r['intent']:14s}]{g} {r['q'][:30]:32s} "
                  f"kw={str(r.get('keyword')):>4s} vec={str(r.get('vector')):>4s} "
                  f"hyb={str(r.get('hybrid')):>4s}  "
                  f"nDCG kw={r.get('keyword_ndcg')} hyb={r.get('hybrid_ndcg')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="检索评测跑分（参数化，覆盖全部垂域）")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--domain", help="垂域名（如 gov / papers / screenplay）")
    grp.add_argument("--all", action="store_true", help="全部已交付标注集")
    ap.add_argument("--k", type=int, default=10, help="hit@k 的 k（默认 10）")
    ap.add_argument("--channels", default="keyword,vector,hybrid",
                    help="要跑的通道，逗号分隔（如 keyword 可用于快速回归）")
    ap.add_argument("--store", action="store_true", help="结果写入 eval_runs 并判跨次回归")
    ap.add_argument("--json", default="", help="把完整结果写到该路径")
    ap.add_argument("--variant", default="default",
                    help="标注集变体：default=回归守门集 / hard=分级难题集")
    ap.add_argument("--no-rows", action="store_true", help="不打印逐条明细")
    ap.add_argument("--no-embed", action="store_true",
                    help="不加载嵌入器（仅 keyword 通道时用；避免无嵌入凭据时按降级跑）")
    args = ap.parse_args()

    channels = tuple(c.strip() for c in args.channels.split(",") if c.strip())
    unknown = [c for c in channels if c not in ("keyword", "vector", "hybrid")]
    if unknown:
        print(f"error: 未知通道 {unknown}；可用=keyword,vector,hybrid", file=sys.stderr)
        return 2

    from fd_text_core.embedding import embedder_from_env

    embedder = None
    need_vector = any(c in ("vector", "hybrid") for c in channels)
    if need_vector and not args.no_embed:
        embedder = embedder_from_env()
        if embedder is None:
            print("# 未配置嵌入凭据：向量与混合通道将如实降级（不冒充完整评测）",
                  file=sys.stderr)

    if args.all:
        domains = list_datasets(args.variant)
        if not domains:
            print("error: 包内没有任何标注集", file=sys.stderr)
            return 2
    else:
        domains = [args.domain]

    all_out = []
    exit_code = 0
    for dom in domains:
        try:
            ds = load_dataset(dom, variant=args.variant)
        except DatasetError as exc:
            print(f"error: {exc}", file=sys.stderr)
            exit_code = 2
            continue
        rep = runner.run_eval(ds, k=args.k, channels=channels, embedder=embedder,
                              check_expects=store.check_expects_exist)
        _print_report(rep, show_rows=not args.no_rows)
        out = rep.to_dict()

        if args.store:
            try:
                row_id = store.store_run(rep)
                prev = store.latest_previous(rep, exclude_id=row_id)
                cmp_result = runner.compare_with_previous(rep, prev)
                out["stored_id"] = row_id
                out["comparison"] = cmp_result
                print(f"  入库 id={row_id}；对比：{cmp_result['status']} — {cmp_result['message']}")
                if cmp_result.get("changes"):
                    for c in cmp_result["changes"]:
                        print(f"     变化：{c}")
                if cmp_result.get("details"):
                    for d in cmp_result["details"]:
                        print(f"     回归：{d}")
                if cmp_result["status"] == "regression":
                    exit_code = max(exit_code, 1)
                if rep.hybrid_not_worse is False:
                    exit_code = max(exit_code, 1)
            except Exception as exc:
                print(f"  ⚠ 入库失败（跑分结果仍有效）：{type(exc).__name__}: {exc}",
                      file=sys.stderr)
                exit_code = max(exit_code, 1)

        all_out.append(out)

    if args.json:
        Path(args.json).write_text(
            json.dumps(all_out if len(all_out) > 1 else all_out[0], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\n结果已写 {args.json}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
