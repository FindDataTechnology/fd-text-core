#!/usr/bin/env python3
"""嵌入模型升级/迁移脚本：校验通道 -> 清空旧向量 -> 重建锚点 -> 全量重嵌。

为什么需要显式流程：余弦自检门（>=0.999）在换模型时**必然**拦截新模型
（与旧锚点余弦不达标），这是它该做的。但运维需要一条合法通道，否则会
养成「删锚点绕过自检」的坏习惯，门就废了。本脚本把升级固化为四步、
每步打印可核验事实、失败即中断。

红线：新旧模型向量**禁混存**（混存会让检索静默返回错误排序），
故次序固定为「先清空、再重建锚点、最后重嵌」，不可调换。

用法：
    export FDTEXT_DATABASE_URL=... FDTEXT_S3_*=...
    export FDTEXT_EMBEDDING_API_BASE=https://api.siliconflow.cn/v1
    export FDTEXT_EMBEDDING_API_KEY=...
    export FDTEXT_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-8B
    export FDTEXT_EMBEDDING_DIM=1024
    python3 scripts/reembed_upgrade.py [--dry-run] [--batch 8]

--dry-run 只做第 1 步（校验新模型通道），不触碰任何数据。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core import embed_worker as W  # noqa: E402
from fd_text_core.db import execute, fetch_one  # noqa: E402
from fd_text_core.embedding import Embedder  # noqa: E402
from fd_text_core.embedding import provider_from_env  # noqa: E402


def step1_verify_channel(emb: Embedder, dim: int) -> None:
    """校验新模型通道：可达 + 维度符合预期。不通过则中断。"""
    probe = emb.embed(["嵌入升级通道校验 probe"]).vectors[0]
    if len(probe) != dim:
        raise SystemExit(
            f"[1/4] 通道校验失败：返回维度 {len(probe)} != 配置 FDTEXT_EMBEDDING_DIM={dim}。"
            "拒绝继续（维度不符会写坏 vector(1024) 列）"
        )
    print(f"[1/4] 通道校验通过：model={emb.model} dim={len(probe)}（MRL 截断={emb.dimensions}）")


def step2_clear_vectors() -> int:
    """清空全部旧向量。新旧模型向量禁混存。"""
    n = execute("UPDATE corpus_chunks SET embedding=NULL, embedded_at=NULL WHERE embedding IS NOT NULL")
    print(f"[2/4] 旧向量已清空：{n} 块（含旧模型与任何混合态）")
    return n


def step3_rebuild_anchor(emb: Embedder) -> None:
    """删除旧锚点并以新模型重建。自检门阈值不动——升级走显式重建而非放宽。"""
    old = fetch_one("SELECT model, dim FROM anchor_embedding WHERE tag=%s", (W.ANCHOR_TAG,))
    execute("DELETE FROM anchor_embedding WHERE tag=%s", (W.ANCHOR_TAG,))
    sim = W.run_self_check(emb, emb.model)
    new = fetch_one("SELECT model, dim FROM anchor_embedding WHERE tag=%s", (W.ANCHOR_TAG,))
    print(f"[3/4] 锚点重建：旧={old} -> 新={new}，首建自检={sim:.6f}")


def step4_reembed(emb: Embedder, batch: int) -> tuple[int, int]:
    """全量重嵌：有未嵌块的文档任务重置为 pending，跑 worker 至清空。"""
    n = execute(
        """
        UPDATE embed_tasks SET state='pending', claimed_at=NULL, finished_at=NULL, error=NULL
        WHERE document_id IN (SELECT DISTINCT document_id FROM corpus_chunks WHERE embedding IS NULL)
        """
    )
    print(f"[4/4] 任务重置：{n} 篇文档重新入队")
    done = failed = 0
    while True:
        r = W.run_once(emb, emb.model, limit=batch)
        if r.claimed == 0:
            break
        done += r.embedded
        failed += r.failed
        print(f"      claimed={r.claimed} embedded={r.embedded} chunks={r.chunks} failed={r.failed}")
        if r.failed:
            raise SystemExit(f"[4/4] 出现失败任务 {failed} 条，中断（不静默续跑）")
    return done, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只校验通道，不触碰数据")
    ap.add_argument("--batch", type=int, default=8, help="worker 每批认领文档数")
    args = ap.parse_args()

    base = os.environ.get("FDTEXT_EMBEDDING_API_BASE", "").strip()
    key = os.environ.get("FDTEXT_EMBEDDING_API_KEY", "").strip()
    model = os.environ.get("FDTEXT_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B").strip()
    dim = int(os.environ.get("FDTEXT_EMBEDDING_DIM", "1024"))
    if not (base and key):
        raise SystemExit("缺 FDTEXT_EMBEDDING_API_BASE / FDTEXT_EMBEDDING_API_KEY")

    emb = Embedder(base, key, model, dimensions=dim, provider=provider_from_env())
    step1_verify_channel(emb, dim)
    if args.dry_run:
        print("dry-run：仅通道校验，数据未触碰。")
        return 0

    before = fetch_one("SELECT count(*)::int AS n FROM corpus_chunks")["n"]
    step2_clear_vectors()
    step3_rebuild_anchor(emb)
    done, failed = step4_reembed(emb, args.batch)

    nulls = fetch_one("SELECT count(*)::int AS n FROM corpus_chunks WHERE embedding IS NULL")["n"]
    print(f"\n完成：文档 {done}、失败 {failed}、存量块 {before}、仍为 null 的块 {nulls}")
    if nulls:
        raise SystemExit(f"仍有 {nulls} 块无向量——升级未完成，须排查后再收口")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
