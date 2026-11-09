#!/usr/bin/env python3
"""摄取吞吐对照：逐条路径 vs 批量路径（ingest-batch-throughput 1.2）。

为什么要有它：`fd_text` 库在 tailnet，单语句往返 ~65ms；"快不快"取决于**往返块数**，
而不是 CPU。所以本脚本同时报墙钟与往返块数——后者受网络抖动影响小，是主指标。

用法（pod 内或本地配好 FDTEXT_DATABASE_URL）::

    python scripts/bench_ingest_batch.py --n 200
    python scripts/bench_ingest_batch.py --n 200 --mode single   # 只跑逐条
    python scripts/bench_ingest_batch.py --n 200 --mode batch    # 只跑批量

两种模式用**同一批合成文档**（不同 source_id），各自先删后插，互不干扰。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fd_text_core import ingest

SRC = "_bench_ingest"


def _docs(n: int, tag: str) -> list[ingest.Doc]:
    return [
        ingest.Doc(
            domain="news",
            source=SRC,
            source_id=f"{tag}-{i}",
            title=f"基准文档 {i}",
            content=("这是用于对照入库吞吐的正文，长度接近真实新闻条目。" * 20) + str(i),
            metadata={"lang": "zh", "bench": tag},
        )
        for i in range(n)
    ]


def _clean(tag: str) -> None:
    ingest.execute(
        "DELETE FROM embed_tasks WHERE document_id IN"
        " (SELECT id FROM corpus_documents WHERE domain=%s AND source=%s AND source_id LIKE %s)",
        ("news", SRC, f"{tag}-%"),
    )
    ingest.execute(
        "DELETE FROM corpus_documents WHERE domain=%s AND source=%s AND source_id LIKE %s",
        ("news", SRC, f"{tag}-%"),
    )


class RTT:
    """往返块计数器：db 助手的每次调用 = 1 块（内部 2 条 SET LOCAL + 1 条语句）。"""

    def __init__(self) -> None:
        self.blocks = 0
        self._orig = {}

    def __enter__(self) -> RTT:
        for name in ("execute", "fetch_one", "fetch_all"):
            self._orig[name] = getattr(ingest, name)

            def wrap(fn, _name=name):
                def inner(*a, **kw):
                    self.blocks += 1
                    return fn(*a, **kw)

                return inner

            setattr(ingest, name, wrap(self._orig[name]))
        self._orig["cursor"] = ingest.cursor

        def cursor_wrap(*a, **kw):
            self.blocks += 1
            return self._orig["cursor"](*a, **kw)

        ingest.cursor = cursor_wrap
        return self

    def __exit__(self, *exc) -> None:
        for name in ("execute", "fetch_one", "fetch_all", "cursor"):
            setattr(ingest, name, self._orig[name])


def _run_single(docs: list[ingest.Doc], bkey: str) -> dict:
    stats = {"inserted": 0, "skipped": 0, "superseded": 0, "queued": 0}
    for d in docs:
        ingest._load_one(d, bkey, stats)  # 与批量化之前的 load_documents 逐条体一致
    return stats


def _run_batch(docs: list[ingest.Doc], bkey: str) -> dict:
    return ingest.load_documents(docs, batch_key=bkey)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--mode", choices=["both", "single", "batch"], default="both")
    ap.add_argument("--batch-size", type=int, default=ingest.INGEST_BATCH)
    args = ap.parse_args()

    docs = _docs(args.n, "b")
    single_docs = _docs(args.n, "s")
    out = []

    if args.mode in ("both", "single"):
        _clean("s")
        try:
            with RTT() as rtt:
                t = time.time()
                st = _run_single(single_docs, "bench-single")
                took = time.time() - t
            out.append(("single", st, took, rtt.blocks))
        finally:
            _clean("s")

    if args.mode in ("both", "batch"):
        _clean("b")
        try:
            with RTT() as rtt:
                t = time.time()
                st = _run_batch(docs, "bench-batch")
                took = time.time() - t
            out.append(("batch", st, took, rtt.blocks))
        finally:
            _clean("b")

    print(f"n={args.n}  FDTEXT_INGEST_BATCH={args.batch_size}")
    for mode, st, took, blocks in out:
        per = took / args.n * 1000 if args.n else 0
        print(f"  {mode:6s} {took:7.2f}s  {per:7.1f}ms/篇  往返块 {blocks:5d}  {st}")
    if len(out) == 2 and out[1][3]:
        print(f"  → 往返块比 {out[0][3] / out[1][3]:.0f}×，墙钟比 {out[0][2] / out[1][2]:.1f}×")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
