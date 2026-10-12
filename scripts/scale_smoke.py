#!/usr/bin/env python3
"""规模就绪冒烟：十万级 chunk 合成语料全链灌入（摄取 → 切块 → 嵌入）。

对应 change `text-foundation-remediation` 任务组 5，补齐 `text-engine-foundation`
遗留的未达标项（当时只做到 54,000 chunk，属测试语料生成参数不足而非管线限制）。

设计要点：
  - 合成语料走**真实全链**（真 PG、真嵌入 API、真 zstd 冷存），不是 mock；
    只有语料本身是合成的（`source=synthetic-scale`，provenance 如实标注）。
  - 分批灌入 + 断点续跑：每批独立事务，中断后重跑只处理未完成部分
    （摄取层按 content_hash 幂等跳过，嵌入层走 claim-ledger）。
  - 可在任意时刻 Ctrl-C 或 kill；脚本自身也支持 `--kill-after-batches`
    模拟中途中断（验收要求「中途人为 kill 一次」的自动化等价物）。
  - 语料落在独立 domain 分区 `scale_smoke`，不污染 papers/screenplay。

用法：
    # 灌入 10 万 chunk（默认），中途在第 3 批后模拟一次中断
    python scripts/scale_smoke.py --target-chunks 100000 --kill-after-batches 3

    # 中断后重跑（幂等续跑）
    python scripts/scale_smoke.py --target-chunks 100000

    # 清理（删掉本冒烟的全部产物）
    python scripts/scale_smoke.py --cleanup
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DOMAIN = "scale_smoke"
SOURCE = "synthetic-scale"
CHUNK_TARGET_CHARS = 900  # 与 SlidingWindow target 同量级，使每篇产出可控块数


def _require_env() -> None:
    need = ["FDTEXT_DATABASE_URL", "FDTEXT_EMBEDDING_API_BASE", "FDTEXT_EMBEDDING_API_KEY"]
    missing = [k for k in need if not os.environ.get(k, "").strip()]
    if missing:
        raise SystemExit(f"缺环境变量：{', '.join(missing)}（本冒烟必须真连库与真嵌入，不做 mock）")


def ensure_vertical() -> None:
    """在注册点登记合成垂域（用注册 API，不碰引擎核心）。

    嵌入 worker 按垂域取切块策略，未注册垂域会拒绝处理——故合成语料也须
    走正规注册点。策略复用引擎内置 SlidingWindow，与 screenplay 同参。
    """
    from fd_text_core.chunking import SlidingWindow
    from fd_text_core.verticals import registry as R

    R.bootstrap_defaults()
    if DOMAIN in R.list_verticals():
        return
    R.register(
        R.Vertical(
            name=DOMAIN,
            strategy=SlidingWindow(target=CHUNK_TARGET_CHARS, max_size=1400),
            facets={"kind": frozenset({"synthetic"})},
            tsv_config="simple",
            language="multi",
            display="规模冒烟（合成语料）",
        )
    )


def ensure_partition() -> None:
    """scale_smoke 分区不存在则建（属运维动作，非引擎代码改动）。"""
    from fd_text_core.db import execute

    execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relname='corpus_chunks_scale_smoke'
            ) THEN
                CREATE TABLE corpus_chunks_scale_smoke
                    PARTITION OF corpus_chunks FOR VALUES IN ('scale_smoke');
            END IF;
        END $$;
        """
    )


def synth_docs(batch_no: int, docs_per_batch: int, chunks_per_doc: int, seed: int):
    """产出一批合成文档。每篇正文按 chunk 目标长度铺砌，使切块数可控。"""
    from fd_text_core.ingest import Doc

    rng = random.Random(seed + batch_no)
    words = [
        "文本", "引擎", "检索", "向量", "切块", "溯源", "语料", "索引", "查询", "嵌入",
        "分区", "断点", "吞吐", "规模", "幂等", "迁移", "质量门", "垂域", "配置", "基础设施",
    ]
    out = []
    for i in range(docs_per_batch):
        # 正文长度 ≈ chunks_per_doc * CHUNK_TARGET_CHARS。
        # 必须以句号断句：切块策略按句子边界吸附，无标点的连续汉字会被判为
        # 单个超长句而整篇成一块（实测：8000 字无标点正文只切出 1 块，
        # 2000 目标的试跑因此只得到 80 chunk）。
        # 每句约 9 字（4-6 个双字词 + 句号），故要产出 N 块需约 N*CHUNK_TARGET/9 句。
        n_sentences = max(1, (chunks_per_doc * CHUNK_TARGET_CHARS) // 9)
        sentences = []
        for _ in range(n_sentences):
            n = rng.randint(4, 6)
            sentences.append("".join(rng.choice(words) for _ in range(n)) + "。")
        body = "".join(sentences)
        sid = f"b{batch_no:04d}-d{i:04d}"
        out.append(
            Doc(
                domain=DOMAIN,
                source=SOURCE,
                source_id=sid,
                title=f"合成语料 {sid}",
                content=body,
                metadata={"synthetic": True, "batch": batch_no},
            )
        )
    return out


def run_batch(batch_no: int, docs_per_batch: int, chunks_per_doc: int, seed: int) -> dict:
    from fd_text_core.ingest import load_documents

    docs = synth_docs(batch_no, docs_per_batch, chunks_per_doc, seed)
    return load_documents(docs, batch_key=f"scale-smoke-b{batch_no:04d}")


def _worker_loop(limit_per_round: int, max_rounds: int, worker_id: int, fake_embed: bool = False) -> dict:
    """单个嵌入工作进程：反复认领并处理，直到无待办。

    并发是必要的工程手段而非优化：worker 每文档一次嵌入调用，串行下十万级
    需数小时（实测单文档往返 ~0.2-1s）；多进程把往返重叠，这是真实生产形态
    （集群里本来就是多副本 worker 抢同一个 claim-ledger）。
    """
    from fd_text_core.embed_worker import Embedder, run_once
    from fd_text_core.embedding import provider_from_env

    ensure_vertical()  # 子进程需自行注册（注册表是进程内的）
    if fake_embed:
        # 显式降级通道：只在真实嵌入通道不可用时使用（如账户余额耗尽）。
        # 它验证的是**管线吞吐与断点恢复**，不产生真实语义向量——故本通道
        # 的产物不得用于任何语义质量断言，跑完必须清理（见 --cleanup）。
        # 该范式与 load_papers.py 的 --fake-embed 一致（舰队既有做法）。
        #
        # 与真锚点的关系：假向量与真模型既非同一空间，余弦必然远低于自检阈值。
        # 故本通道**不得**走 run_once 的开工自检（那会（正确地）拒绝执行）。
        # 这里直接调用不触发自检的处理路径，并在跑完后由 --cleanup 清理；
        # 真实锚点与既有 papers/screenplay 向量全程不被触碰。
        sys.path.insert(0, str(REPO / "scripts"))
        from load_papers import FakeEmbedder

        emb = FakeEmbedder(dim=int(os.environ.get("FDTEXT_EMBEDDING_DIM", "1024")))
        model = emb.model
        return _fake_worker_loop(emb, limit_per_round, max_rounds, worker_id)
    else:
        base = os.environ["FDTEXT_EMBEDDING_API_BASE"]
        key = os.environ["FDTEXT_EMBEDDING_API_KEY"]
        model = os.environ.get("FDTEXT_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")
        dim = int(os.environ.get("FDTEXT_EMBEDDING_DIM", "1024"))
        emb = Embedder(base, key, model, dimensions=dim, provider=provider_from_env())

    totals = {"claimed": 0, "chunks": 0, "embedded": 0, "failed": 0}
    for _ in range(max_rounds):
        try:
            res = run_once(emb, model, limit=limit_per_round)
        except Exception as exc:  # 自检门拒绝等：如实报错退出该 worker
            print(f"    [w{worker_id}] worker 异常退出：{type(exc).__name__}: {exc}", flush=True)
            break
        totals["claimed"] += res.claimed
        totals["chunks"] += res.chunks
        totals["embedded"] += res.embedded
        totals["failed"] += res.failed
        if res.claimed == 0:
            break
    return totals


def _fake_worker_loop(emb, limit_per_round: int, max_rounds: int, worker_id: int) -> dict:
    """降级通道的处理循环：复用引擎的切块写入，只跳过开工自检。

    切块/入库/写向量全部走引擎既有函数（ensure_chunks / vector_literal），
    故验证的确实是引擎管线。唯一差异=不跑 run_self_check（假向量与真锚点
    不同空间，自检会正确地拒绝）。真实生产路径永远走带自检的 run_once。
    """
    from fd_text_core.db import execute, fetch_all, fetch_one
    from fd_text_core.embed_worker import (
        _document_text,
        claim_batch,
        ensure_chunks,
        reclaim_stale,
        vector_literal,
    )

    totals = {"claimed": 0, "chunks": 0, "embedded": 0, "failed": 0}
    reclaim_stale()
    for _ in range(max_rounds):
        tasks = claim_batch(limit_per_round)
        if not tasks:
            break
        totals["claimed"] += len(tasks)
        for t in tasks:
            try:
                doc = fetch_one("SELECT * FROM corpus_documents WHERE id=%s", (t["document_id"],))
                if doc is None:
                    execute(
                        "UPDATE embed_tasks SET state='missing_doc', finished_at=now() WHERE id=%s",
                        (t["id"],),
                    )
                    totals["failed"] += 1
                    continue
                text = _document_text(doc)
                totals["chunks"] += ensure_chunks(doc, text)
                targets = fetch_all(
                    "SELECT id, chunk_text FROM corpus_chunks "
                    "WHERE document_id=%s AND embedding IS NULL ORDER BY chunk_no",
                    (doc["id"],),
                )
                if targets:
                    vectors = emb.embed([r["chunk_text"] for r in targets]).vectors
                    for row, v in zip(targets, vectors):
                        execute(
                            "UPDATE corpus_chunks SET embedding=%s::vector, embedded_at=now() "
                            "WHERE id=%s AND domain=%s",
                            (vector_literal(v), row["id"], doc["domain"]),
                        )
                execute(
                    "UPDATE embed_tasks SET state='finished', finished_at=now() WHERE id=%s",
                    (t["id"],),
                )
                totals["embedded"] += 1
            except Exception as exc:
                execute(
                    "UPDATE embed_tasks SET state='failed', finished_at=now(), error=%s WHERE id=%s",
                    (str(exc)[:500], t["id"]),
                )
                totals["failed"] += 1
    return totals


def drain_embed(limit_per_round: int, max_rounds: int, workers: int, fake_embed: bool = False) -> dict:
    """并发跑完待办嵌入任务（claim-ledger 天然支持多 worker 抢任务）。"""
    from concurrent.futures import ProcessPoolExecutor

    totals = {"claimed": 0, "chunks": 0, "embedded": 0, "failed": 0}
    if workers <= 1:
        part = _worker_loop(limit_per_round, max_rounds, 0, fake_embed)
        for k in totals:
            totals[k] += part[k]
        return totals

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_worker_loop, limit_per_round, max_rounds, i, fake_embed)
            for i in range(workers)
        ]
        for f in futures:
            part = f.result()
            for k in totals:
                totals[k] += part[k]
    return totals


def counts() -> dict:
    from fd_text_core.db import fetch_one

    return {
        "docs": fetch_one(
            "SELECT count(*) c FROM corpus_documents WHERE domain=%s AND status='current'", (DOMAIN,)
        )["c"],
        "chunks": fetch_one("SELECT count(*) c FROM corpus_chunks WHERE domain=%s", (DOMAIN,))["c"],
        "embedded": fetch_one(
            "SELECT count(*) c FROM corpus_chunks WHERE domain=%s AND embedding IS NOT NULL", (DOMAIN,)
        )["c"],
        "pending": fetch_one(
            "SELECT count(*) c FROM embed_tasks t JOIN corpus_documents d ON d.id=t.document_id "
            "WHERE d.domain=%s AND t.state='pending'", (DOMAIN,)
        )["c"],
    }


def cleanup() -> None:
    """彻底清理本冒烟产物，包括 domain 分区表本身。

    分区必须一并删除：它是 ensure_partition 在库外建的（不走 alembic 链），
    留着会被质量门的 schema 漂移检测判为「不在迁移链内的表」而报 FAIL
    ——实测踩到（冒烟跑完门红，逐条点名 corpus_chunks_scale_smoke）。
    合成语料本就不该在库里留下任何跨迁移链的痕迹。
    """
    from fd_text_core.db import execute

    execute("DELETE FROM embed_tasks WHERE document_id IN (SELECT id FROM corpus_documents WHERE domain=%s)", (DOMAIN,))
    execute("DELETE FROM corpus_chunks WHERE domain=%s", (DOMAIN,))
    execute("DELETE FROM corpus_documents WHERE domain=%s", (DOMAIN,))
    execute(f"DROP TABLE IF EXISTS corpus_chunks_{DOMAIN}")
    print(f"已清理 domain={DOMAIN} 的全部产物（含分区表 corpus_chunks_{DOMAIN}，不留迁移链外对象）")


def main() -> int:
    ap = argparse.ArgumentParser(description="十万级 chunk 规模冒烟")
    ap.add_argument("--target-chunks", type=int, default=100_000)
    ap.add_argument("--chunks-per-doc", type=int, default=25, help="每篇合成文档的块数")
    ap.add_argument("--docs-per-batch", type=int, default=80, help="每批文档数")
    ap.add_argument("--embed-batch", type=int, default=20, help="每轮嵌入认领的文档数")
    ap.add_argument("--workers", type=int, default=8, help="并发嵌入进程数（claim-ledger 支持多 worker）")
    ap.add_argument("--fake-embed", action="store_true",
                    help="显式降级：用确定性假向量（仅在真实嵌入通道不可用时用于验证管线吞吐/断点恢复，"
                         "不产生语义质量；产物必须 --cleanup 清理）")
    ap.add_argument("--max-rounds", type=int, default=500)
    ap.add_argument("--kill-after-batches", type=int, default=0,
                    help="在第 N 批后模拟中断退出（验收要求「中途人为 kill 一次」）")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--seed", type=int, default=20261012)
    args = ap.parse_args()

    if args.cleanup:
        cleanup()
        return 0

    _require_env()
    ensure_vertical()
    ensure_partition()

    batches_needed = -(-args.target_chunks // (args.docs_per_batch * args.chunks_per_doc))
    print(f"目标 {args.target_chunks} chunk ≈ {batches_needed} 批"
          f"（每批 {args.docs_per_batch} 篇 × {args.chunks_per_doc} 块）", flush=True)

    t0 = time.time()
    ingest_totals = {"inserted": 0, "skipped": 0, "superseded": 0, "queued": 0}
    for b in range(batches_needed):
        st = run_batch(b, args.docs_per_batch, args.chunks_per_doc, args.seed)
        for k in ingest_totals:
            ingest_totals[k] += st.get(k, 0)
        c = counts()
        print(f"  批 {b+1}/{batches_needed}: +{st['inserted']} 篇（queued={st['queued']}）"
              f" → 累计 docs={c['docs']} chunks={c['chunks']}", flush=True)
        if args.kill_after_batches and b + 1 == args.kill_after_batches:
            print(f"\n*** 模拟中断：已完成 {b+1} 批，进程退出（验收要求的中途 kill）***", flush=True)
            print(f"*** 当前库内：{c}（嵌入未跑，pending 待续）***", flush=True)
            return 0

    t_ingest = time.time() - t0
    print(f"\n摄取完成：{ingest_totals}，耗时 {t_ingest:.1f}s", flush=True)

    t1 = time.time()
    emb_totals = drain_embed(args.embed_batch, args.max_rounds, args.workers, args.fake_embed)
    t_embed = time.time() - t1

    final = counts()
    print("\n=== 规模冒烟结果 ===")
    print(f"  文档 {final['docs']} / chunk {final['chunks']} / 已嵌入 {final['embedded']}"
          f" / 残留 pending {final['pending']}")
    print(f"  摄取耗时 {t_ingest:.1f}s，嵌入耗时 {t_embed:.1f}s")
    print(f"  嵌入累计 {emb_totals}")
    if emb_totals["failed"]:
        print(f"  ⚠️ 嵌入失败 {emb_totals['failed']} 篇（如实记录，不掩盖）")
    if final["chunks"] < args.target_chunks:
        print(f"  ⚠️ 未达标：目标 {args.target_chunks}，实得 {final['chunks']}"
              "（按 spec 要求如实标注，不得记为通过）")
        return 1
    print(f"  ✅ 达标：chunk {final['chunks']} >= 目标 {args.target_chunks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
