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
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_papers import build_embedder

from fd_text_core import db as dbmod
from fd_text_core import embed_worker
from fd_text_core.ingest import Doc, load_documents
from fd_text_core.provenance import Provenance

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

    **按 source 公平分配 limit**（防大源挤死小源）：曾有 bug——`list(folded)[:limit]`
    按 glob 序取前 N 条，而 cninfo 单源就 2 万+，于是排在它后面的 news-rss /
    sse-interaction 永远落不到前 5000 条里、**永久零入库**（2026-11-23 实测：
    news-rss 120 条始终 skipped，text-core news 域 source 只有 cninfo/cn-gov/sse）。
    改为：每源先给 ``limit // num_sources`` 的均等份额，多轮轮转吃掉余量，
    直到用完 limit 或无源可补 —— 大源仍能靠多轮陆续入库，小源当轮必得名额。
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

    # 按 source 分组（保留各自折叠后的时间序）
    by_source: dict[str, list[dict]] = {}
    for row in folded.values():
        by_source.setdefault(str(row.get("source")), []).append(row)

    # 均等份额 + 轮转发余量，保证每源至少被看见
    if not by_source:
        return []
    picked: list[dict] = []
    cursors = {src: 0 for src in by_source}
    per_round = max(1, limit // len(by_source))
    while len(picked) < limit and any(
        cursors[s] < len(rows) for s, rows in by_source.items()
    ):
        for src, rows in by_source.items():
            if len(picked) >= limit:
                break
            start = cursors[src]
            chunk = rows[start:start + per_round]
            cursors[src] = start + len(chunk)
            picked.extend(chunk)
    return picked[:limit]


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

        # 有的源 spool 里存的是原始 HTML（cn-gov 实测 100%），在边界转成干净正文
        raw_len = len(content)
        content = _strip_html_to_text(source, content)
        if not content:
            skipped.append(f"正文无法从 HTML 还原: {source}:{source_id}（原 {raw_len} 字）")
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
        # 2026-11-13 实测：词表三件里只写了 source_type/lang，has_body 从未落库，
        # 于是任何 has_body 过滤恒 0（生产 5908 篇全无该键）。
        md["source_type"] = _facet_source_type(source)
        # 恒为 true 是**构造保证**而不是巧合成：缺正文的行在上面就被 skip 了，
        # 库里另有 ck_document_has_body 约束（content 或冷存指针非空）。
        # 保留这个字段是因为分析面据此决定能否做逐字引用；将来若接入只有摘要的源，
        # 这里就是记 false 的位置（届时须放宽上面的 skip 规则）。
        md["has_body"] = "true" if content else "false"

        title = md["raw_title"] or None
        # domain 取自 spool 行（fd-info 的 body_store 按条目 domain 写），缺省 news。
        # 根本修：loader 不再写死单一域，同一条 spool 管线可服务多垂域
        # （govcn→gov、cninfo/news-rss→news）。见 fd-info models.InfoItem.domain。
        row_domain = str(row.get("domain") or DOMAIN).strip() or DOMAIN
        docs.append(
            Doc(
                domain=row_domain,
                source=source,
                source_id=source_id,
                title=title,
                content=content,
                metadata=md,
            )
        )
    return docs, skipped


def _strip_html_to_text(source: str, content: str) -> str:
    """spool 里有的源存的是**原始 HTML**，在摄取边界转成干净正文。

    实录（2026-11-08）：cn-gov 的 spool 46 条记录 **100% 含 HTML 标签**——上游
    `fd-cn-gov.detail.fetch.fetch_detail` 返回的是 `html_content`，落盘的就是标签汤
    （`<dt> 来 源： </dt><dd id="ly">…`、`<div class="social-icon-wrapper">`）。
    结果：1275 个 gov 切片里 88% 是标记，政务检索只能捞出标记，向量化纯烧钱。

    为什么在**这里**修而不是改 fd-info：解析器（`parse_gov_doc`，纯标准库、多候选
    正文容器 + 过滤页脚/分享按钮 + 抽文号与成文日期）本来就住在 fd-text-core；
    fd-info 镜像里没有本包，在那里调会 ImportError 从而把所有政务条目判失败。
    放在摄取边界还顺带修好**已入库**的旧条目（内容变了 = 新版本 = 旧行退役）。

    spool 仍保持原样（原始 HTML 是事实档案，解析器以后改进还能重放）。
    HTML 转不出正文时返回空串，由调用方按"缺正文"跳过——宁可缺，不落脏数据。
    """
    if "<" not in content:
        return content
    try:
        from fd_text_core.verticals.gov.parser import parse_gov_doc
    except ImportError:
        return content
    try:
        doc = parse_gov_doc(content)
    except Exception:  # noqa: BLE001 —— 解析器对畸形 HTML 的失败形态不可枚举；失败即跳过
        return ""
    body = (getattr(doc, "body", "") or "").strip()
    if not body:
        return ""
    # 解析器也救不回来的（仍有大量标签）宁可不入库
    if body.count("<") > 20:
        return ""
    return body


def _facet_source_type(source: str) -> str:
    """source 名 → source_type facet（词表外归 announcement 基类）。

    注意：govcn 政策文件是 gov 域的**正文语料**，必须嵌向量（语义检索要用），
    不能落 announcement 基类——否则撞 EMBED_SKIP_SOURCE_TYPES=announcement 被跳过
    不嵌（2026-11-23 实测：14831 篇 govcn 全被当 announcement 跳过）。
    """
    mapping = {
        "cninfo": "announcement",
        "cn-gov": "ministry",
        "sse-interaction": "interaction",
        "news-rss": "news",
        "govcn": "policy",
    }
    return mapping.get(source, "announcement")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--spool", default="/data/spool", help="fd-info spool 根目录")
    ap.add_argument("--limit", type=int, default=5000, help="最多入库多少条（折叠后）")
    ap.add_argument("--embed-batch", type=int, default=50)
    ap.add_argument(
        "--embed-seconds",
        type=int,
        default=int(os.environ.get("SPOOL_EMBED_SECONDS", "120")),
        help="本轮嵌入时间预算（秒）；到点收工，余下交给下一轮",
    )
    ap.add_argument(
        "--max-backlog-hours",
        type=float,
        default=float(os.environ.get("SPOOL_MAX_BACKLOG_HOURS", "6")),
        help="嵌入积压最老条目超过此小时数即判本轮不健康（非零退出）",
    )
    ap.add_argument(
        "--embed-max-inputs",
        type=int,
        default=int(os.environ.get("SPOOL_EMBED_MAX_INPUTS", "400")),
        help="单批最多送多少块；再加就会把一次嵌入调用撑爆（实测有单篇 809 块）",
    )
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

    # 锁只护**摄取**：竞态在"先查后插"，嵌入是带 claim 的队列（SKIP LOCKED 并发安全）。
    # 2026-11-08 实测教训：锁原来覆盖整轮，而嵌入阶段可长达小时级，于是期间每一个
    # 定时 Job 都抢不到锁——修完假绿灯后它们会整批变红，把真事故淹掉。
    with run_lock() as acquired:
        if not acquired:
            # 跳过**不能算成功**：CronJob 用 Job 成败更新 status.lastSuccessfulTime，
            # 而运维看板正是拿它判"上次真成功"。实测 2026-11-08：手工 Job 占锁时，
            # 定时 Job 什么都没干却 exit 0，被记成 Complete 1/1 → 看板假绿。
            # 故这里非零退出（75 = EX_TEMPFAIL，"临时不可用，稍后重试"语义）。
            print("[skip] 另一个 loader 正在摄取（advisory lock 未获得），本轮不计成功")
            return 75
        stats = load_documents(docs, batch_key="load_news_spool")
        print(f"[ingest] {stats}")

    # 嵌入按**时间预算**推进：排空为止会让单轮撑到 deadline 被杀，作业永远不算成功
    # （lastSuccessfulTime 不前移 → 看板一直红）。做成泵：每轮嵌一段就收工，
    # 下一轮（5 分钟后）接着排，积压按同样的总速率消失，但每轮都能干净收尾。
    embedder, model = build_embedder(args)
    if args.fake_embed:
        embed_worker.run_self_check = lambda *a, **k: 1.0  # type: ignore[assignment]
    done = failed = retryable = skipped_policy = 0
    deadline = time.monotonic() + args.embed_seconds
    while True:
        try:
            res = embed_worker.run_once(
                embedder, model, limit=args.embed_batch,
                deadline=deadline, max_inputs=args.embed_max_inputs,
            )
        except embed_worker.SelfCheckUnavailable as exc:
            # 自检探针答不上（上游 5xx/超时）≠ 模型漂移：这一轮没写任何东西，
            # 直接收工是安全的；真正的「绿着但向量缺失」由下面的积压超龄门盯住。
            # 实录 2026-11-08：单次 503 打在这一个探针上就让整轮报废，
            # 于是出现「一轮只嵌十块然后整轮判红」。
            print(f"[skip] 自检探针上游不可用，本轮不做：{exc}")
            break
        if res.claimed == 0:
            break
        done += res.embedded
        failed += res.failed
        retryable += res.retryable
        skipped_policy += res.skipped
        print(f"[embed] claimed={res.claimed} embedded={res.embedded} chunks={res.chunks} failed={res.failed}")
        if time.monotonic() >= deadline:
            print(f"[embed] 达到本轮时间预算 {args.embed_seconds}s，余下批次交给下一轮")
            break

    print(f"[done] news 域本轮：文档 {len(docs)}、嵌入块 {done}、失败 {failed}"
          f"（其中可重试 {retryable}；按策略不向量化 {skipped_policy} 篇）")
    # 只有**永久性**失败才判这一轮不成功。限流/超时/连接类失败已经被
    # retry_rate_limited() 在冷却后放回 pending，下一轮照常接着嵌；把它们算成
    # Job 失败只会让看板常红、Error pod 堆成山，而队列其实在正常排空。
    # 真正的「绿着但向量缺失」由下面的积压超龄门负责（90932c4 就是为此加的），
    # 所以这里不再重复报警——但会在 [done] 行里如实列出可重试的数量。
    if failed - retryable:
        return 1

    # 队列健康也要算进"这一轮成功"：嵌入积压再深，作业每轮照样能干净收尾，
    # 于是 lastSuccessfulTime 一路前移、看板常绿——而检索面正在静默查不到这些文档
    # （实录 2026-11-08：4575 条待嵌、最老的 12.4 天，看板显示一切正常）。
    # 积压超龄即非零退出，让看板那条 job 变红；阈值默认 6h（稳态积压是分钟级）。
    #
    # **判据要排除「按策略不做的事」（2026-11-23 实撞）**：原来数的是全部
    # `state='pending'`。而 `FDTEXT_EMBED_SKIP_SOURCE_TYPES=announcement`
    # （2026-11-09 用户裁决：公告/财报不向量化）意味着这几万条 announcement
    # **永远不该被嵌**——它们只在被 claim 到那一刻才转 `skipped`，而 claim
    # 一轮只 10 条。于是这道闸看着一个永远清不完的队列，**恒红**：
    # 实测 pending 25748 里 25463 是 announcement，真需要嵌的只有 95 条（最老 4.5h）。
    # 一条恒红的闸比没有闸更糟——它教人忽略这条线，而这条线正是要抓
    # 「绿着但向量缺失」的。
    #
    # 排除项与 `embed_worker.SKIP_SOURCE_TYPES` **同源**（不另写一份），
    # 否则策略改了这边忘改，就变成「闸看的是旧策略」。
    skip_types = sorted(embed_worker.SKIP_SOURCE_TYPES)
    if skip_types:
        backlog = dbmod.fetch_one(
            "SELECT count(*) AS n,"
            " EXTRACT(EPOCH FROM (now() - min(t.created_at))) / 3600.0 AS hours"
            " FROM embed_tasks t"
            " JOIN corpus_documents d ON d.id = t.document_id"
            " WHERE t.state='pending'"
            "   AND coalesce(d.metadata->>'source_type','') <> ALL(%s)",
            (skip_types,),
        )
    else:
        backlog = dbmod.fetch_one(
            "SELECT count(*) AS n,"
            " EXTRACT(EPOCH FROM (now() - min(created_at))) / 3600.0 AS hours"
            " FROM embed_tasks WHERE state='pending'"
        )
    hours = float(backlog["hours"]) if backlog and backlog["hours"] is not None else 0.0
    if hours > args.max_backlog_hours:
        print(
            f"[warn] 嵌入积压 {backlog['n']} 条，最老 {hours:.1f}h > 阈值 "
            f"{args.max_backlog_hours}h：向量缺失会让检索静默漏结果，本轮判不健康"
            f"（已排除按策略不嵌的 {','.join(skip_types) or '无'}）"
        )
        return 1
    print(f"[queue] 嵌入积压 {backlog['n']} 条，最老 {hours:.1f}h（阈值 {args.max_backlog_hours}h）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
