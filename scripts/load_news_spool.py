"""fd-info spool → news 垂域入库（wire-info-stream-program 活水闭环）。

fd-info 的 ``SpoolBodyStore`` 把条目正文按
``<spool>/<source>/structured/<YYYY-MM-DD>.jsonl`` 追加到本地卷；本脚本读该卷、
按 ``(source, source_id)`` 折叠最新版（与 ``ingest.iter_structured`` 的 latest-wins
同规则——行序即时间序）、转 ``Doc`` 入库并驱动嵌入。设计上不走 S3：活水通道的
JSONL 由 fd-info 持有，S3 冷存约定不因此改变（静态语料仍走 S3）。

用法（fd-info spool 卷挂在 /data 时）::

    FDTEXT_DATABASE_URL=... python scripts/load_news_spool.py --spool /data/spool

嵌入凭据沿用 ``load_papers.build_embedder``（FDTEXT_EMBEDDING_* 环境变量）。
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_papers import build_embedder  # noqa: E402
from fd_text_core import db as dbmod  # noqa: E402
from fd_text_core import embed_worker  # noqa: E402
from fd_text_core.ingest import Doc, load_documents  # noqa: E402
from fd_text_core.provenance import Provenance  # noqa: E402

DOMAIN = "news"

# 同一批 loader 只允许一个实例：两个实例各自「先查后插」，谁都看不见对方未提交的
# 行，于是双双插入 → 撞 uq_document_current_only（2026-11-08 手工 Job 与定时 Job
# 撞车时实测）。CronJob 的 concurrencyPolicy=Forbid 只管它自己创建的 Job，管不住
# 人手 `kubectl create job`，故在脚本里加会话级 advisory lock 兜底。
_LOCK_KEY = 0x66645F6E65777301  # "fd_news" + 1


@contextmanager
def run_lock():
    """拿到锁 yield True；锁被别的 loader 持有则 yield False（本轮跳过，下轮再来）。"""
    pool = dbmod.get_pool()
    with pool.connection() as conn:
        # autocommit：会话级 advisory lock 绑连接，不必开一整个长事务（否则跑几小时
        # 会把 xmin 拖住、挡住 vacuum）。
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_LOCK_KEY,))
            if not cur.fetchone()[0]:
                yield False
                return
        try:
            yield True
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))


def iter_spool(spool: Path, limit: int = 5000) -> list[dict]:
    """读 spool 全量 JSONL 并折叠最新版。

    spool 是追加式：同一 (source, source_id) 修订会产生多行，后出现者胜
    （与 iter_structured 的字节序折叠同理；这里按 (source, day) 名排序读，
    天然时间序）。
    """
    folded: dict[str, dict] = {}
    files = sorted(spool.glob("*/structured/*.jsonl"))
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            source = str(row.get("source") or "").strip()
            source_id = str(row.get("source_id") or "").strip()
            if not (source and source_id):
                continue
            folded[f"{source}:{source_id}"] = row
    rows = list(folded.values())
    return rows[:limit]


def rows_to_docs(rows: list[dict]) -> tuple[list[Doc], list[str]]:
    docs: list[Doc] = []
    skipped: list[str] = []
    for row in rows:
        source = str(row.get("source") or "").strip()
        source_id = str(row.get("source_id") or "").strip()
        content = str(row.get("content") or "").strip()
        if not (source and source_id):
            skipped.append(f"缺 source/source_id: {row.get('url')!r}")
            continue
        if not content:
            skipped.append(f"缺正文: {source}:{source_id}")
            continue

        md: dict = {}
        md["provenance"] = Provenance.now(source, source_id).to_dict()
        md["source_url"] = str(row.get("url") or "")
        md["published"] = row.get("published_at")
        md["lang"] = row.get("lang") or "zh"
        entities = row.get("entities") or []
        md["entities"] = entities if isinstance(entities, list) else []
        md["raw_title"] = str(row.get("title") or "")

        # facet 投影（news 词表：source_type/lang/has_body）——facet 字段必须
        # 显式落库，否则按 facet 过滤恒 0 命中（load_gov 同款教训）。
        md["source_type"] = _facet_source_type(source)

        title = md["raw_title"] or None
        docs.append(
            Doc(
                domain=DOMAIN,
                source=source,
                source_id=source_id,
                title=title,
                content=content,
                metadata=md,
            )
        )
    return docs, skipped


def _facet_source_type(source: str) -> str:
    """source 名 → news 词表的 source_type facet（词表外归 announcement 基类）。"""
    mapping = {
        "cninfo": "announcement",
        "cn-gov": "ministry",
        "sse-interaction": "interaction",
    }
    return mapping.get(source, "announcement")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--spool", default="/data/spool", help="fd-info spool 根目录")
    ap.add_argument("--limit", type=int, default=5000, help="最多入库多少条（折叠后）")
    ap.add_argument("--embed-batch", type=int, default=50)
    ap.add_argument("--fake-embed", action="store_true")
    args = ap.parse_args()

    spool = Path(args.spool)
    rows = iter_spool(spool, limit=args.limit)
    print(f"[read] {spool} 折叠后 {len(rows)} 条")
    if not rows:
        print("[done] spool 为空，无事可做")
        return 0

    docs, skipped = rows_to_docs(rows)
    for s in skipped[:10]:
        print(f"[warn] 跳过: {s}")
    print(f"[ingest] 待入库 {len(docs)} 条")

    with run_lock() as acquired:
        if not acquired:
            # 跳过**不能算成功**：CronJob 用 Job 成败更新 status.lastSuccessfulTime，
            # 而运维看板正是拿它判"上次真成功"。实测 2026-11-08：手工 Job 占锁时，
            # 定时 Job 什么都没干却 exit 0，被记成 Complete 1/1 → 看板假绿。
            # 故这里非零退出（75 = EX_TEMPFAIL，"临时不可用，稍后重试"语义）。
            # 正常路径不会撞锁：concurrencyPolicy=Forbid 已保证 CronJob 自身不重叠，
            # 只有人手 create job 才会争用——那种情况本就该红，不该装成绿的。
            print("[skip] 另一个 loader 正在运行（advisory lock 未获得），本轮不计成功")
            return 75
        stats = load_documents(docs, batch_key="load_news_spool")
        print(f"[ingest] {stats}")

        embedder, model = build_embedder(args)
        if args.fake_embed:
            embed_worker.run_self_check = lambda *a, **k: 1.0  # type: ignore[assignment]
        done = failed = 0
        while True:
            res = embed_worker.run_once(embedder, model, limit=args.embed_batch)
            if res.claimed == 0:
                break
            done += res.embedded
            failed += res.failed
            print(f"[embed] claimed={res.claimed} embedded={res.embedded} chunks={res.chunks} failed={res.failed}")

    print(f"[done] news 域本轮：文档 {len(docs)}、嵌入块 {done}、失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
