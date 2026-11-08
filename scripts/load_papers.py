#!/usr/bin/env python3
"""论文垂域灌库脚本：arXiv OAI-PMH 实采 -> L1 入库 -> L3 切块+嵌入。

可复用：库被重建后重跑即可复原 papers 域，不必再造一次性脚本。

用法：
    export FDTEXT_DATABASE_URL=...  # 必填
    export FDTEXT_S3_ENDPOINT=... FDTEXT_S3_ACCESS_KEY=... FDTEXT_S3_SECRET_KEY=...
    export FDTEXT_EMBEDDING_API_KEY=...   # 无 key 时传 --fake-embed 走确定性假向量
    python3 scripts/load_papers.py --set cs --max 40

--fake-embed 只用于验证管线行为（1024 维确定性向量），不产生真实语义质量；
没有 key 又不加该开关时，脚本如实报错退出，不静默降级。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core import embed_worker  # noqa: E402
from fd_text_core.embedding import Embedder, provider_from_env  # noqa: E402
from fd_text_core.ingest import Doc, load_documents  # noqa: E402
from fd_text_core.provenance import Provenance  # noqa: E402
from fd_text_core.verticals.papers import harvest as H  # noqa: E402


class FakeEmbedder:
    """确定性假向量：无 SiliconFlow key 时的显式降级通道。"""

    def __init__(self, dim: int = 1024, model: str = "fake-deterministic-1024") -> None:
        self.dim = dim
        self.model = model

    def embed(self, texts):
        import math

        vectors = []
        for t in texts:
            seed = hashlib.sha256(t.encode()).digest()
            vals = []
            for i in range(self.dim):
                b = seed[i % len(seed)]
                vals.append(((b / 255.0) * 2 - 1) + (0.001 if i % 7 == 0 else 0.0))
            n = math.sqrt(sum(v * v for v in vals)) or 1.0
            vectors.append([v / n for v in vals])
        return type("R", (), {"vectors": vectors, "usage": None})()


def build_embedder(args) -> tuple[object, str]:
    if args.fake_embed:
        return FakeEmbedder(), "fake-deterministic-1024"
    import os

    base = os.environ.get("FDTEXT_EMBEDDING_API_BASE", "").strip()
    key = os.environ.get("FDTEXT_EMBEDDING_API_KEY", "").strip()
    model = os.environ.get("FDTEXT_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B").strip()
    # MRL 截断维度必须显式下发：8B 原生 4096 维超 pgvector HNSW 的 2000 上限，
    # 列定义是 vector(1024)——不下发 dimensions 时 API 回 4096 维向量，
    # 插入失败/自检维度不符（2026-10-11 实测被锚点自检门拦下，正是它的职责）。
    dim = int(os.environ.get("FDTEXT_EMBEDDING_DIM", "1024"))
    if not (base and key):
        raise SystemExit(
            "缺少 FDTEXT_EMBEDDING_API_BASE / FDTEXT_EMBEDDING_API_KEY。"
            "要验证管线而非向量质量，显式加 --fake-embed。"
        )
    # 超时/重试次数按需可调（默认沿用 Embedder 自带的 60s × 6 次）：
    # 默认退避 1→30s，最坏一次调用能卡 ~7 分钟。实测 2026-11-08：spool loader
    # 一轮撞两次限流就把 activeDeadlineSeconds 吃掉、整轮零进度。
    timeout = int(os.environ.get("FDTEXT_EMBEDDING_TIMEOUT", "60"))
    attempts = int(os.environ.get("FDTEXT_EMBEDDING_MAX_ATTEMPTS", "6"))
    return (
        Embedder(
            base,
            key,
            model,
            dimensions=dim,
            provider=provider_from_env(),
            timeout=timeout,
            max_attempts=attempts,
        ),
        model,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="cs", help="arXiv OAI set，如 cs / econ / q-fin")
    ap.add_argument("--from", dest="from_date", required=True, help="OAI 起始日期 YYYY-MM-DD（必填）")
    ap.add_argument("--until", dest="until_date", default=None, help="OAI 结束日期，默认同 --from")
    ap.add_argument("--max", type=int, default=40, help="采集条数上限")
    ap.add_argument("--embed-batch", type=int, default=20, help="每批认领任务数")
    ap.add_argument("--fake-embed", action="store_true", help="用确定性假向量（管线验证用）")
    args = ap.parse_args()

    until = args.until_date or args.from_date
    print(f"[harvest] arXiv OAI set={args.set} from={args.from_date} until={until} max={args.max}")
    docs, total = [], 0
    for batch, _token in H.harvest_metadata(
        set_spec=args.set,
        max_records=args.max,
        from_date=args.from_date,
        until_date=until,
    ):
        for p in batch:
            # 版权红线：v1 只索引摘要与元数据，不落地全文，也不对外回吐全文
            body = "\n".join(
                part for part in (p.title, "", p.abstract) if part
            ).strip()
            if not body:
                continue
            docs.append(
                Doc(
                    domain="papers",
                    source="arxiv",
                    source_id=p.arxiv_id,
                    title=p.title,
                    content=body,
                    metadata={
                        "authors": p.authors,
                        "categories": p.categories,
                        "license_url": p.license_url,
                        "provenance": Provenance.now("arxiv", p.arxiv_id).to_dict(),
                        "has_fulltext": "false",
                        "source_url": p.source_url,
                    },
                )
            )
            total += 1
        if total >= args.max:
            break
    print(f"[harvest] 组装 {total} 篇")

    stats = load_documents(docs, batch_key="load_papers")
    print(f"[ingest] {stats}")

    embedder, model = build_embedder(args)
    if args.fake_embed:
        # 自检门对假向量无意义（模型名不在锚点表里），跳过自检直接跑
        embed_worker.run_self_check = lambda *a, **k: 1.0  # type: ignore[assignment]
    done = 0
    while True:
        res = embed_worker.run_once(embedder, model, limit=args.embed_batch)
        if res.claimed == 0:
            break
        done += res.embedded
        print(f"[embed] claimed={res.claimed} embedded={res.embedded} chunks={res.chunks} failed={res.failed}")

    print(f"[done] papers 域就绪：文档 {total}、嵌入批次完成 {done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
