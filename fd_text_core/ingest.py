"""L1 摄取：对象存储布局 -> corpus_documents（去重 / 版本退役 / 嵌入任务挂钩）。

语言中立契约：任何语言的采集器只要把 JSONL 产物写进
`<bucket>/<domain>/structured/...`，引擎就能消费。fd-law-data（Node）即如此。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .db import cursor, execute, fetch_all, fetch_one
from .provenance import Provenance

logger = logging.getLogger(__name__)

CURRENT = "current"
SUPERSEDED = "superseded"  # 中性退役词：绝不用垂域私有词汇（如法律线的「历史版本」）

VALID_ORIGINS = ("crawl", "upload")  # upload 为将来自动取通道预留，本期无对外端点

COLD_STORE_MIN_CHARS = 200_000  # 超过此长度的正文自动落 zstd 冷存，PG 只留指针

# 每批文档数。库在 tailnet（单语句往返 ~65ms），逐条对话等于把延迟乘以文档数；
# 批量化把往返压成常数级。200 篇 ≈ 200×11 个绑定参数，远低于 PG 的 65535 上限。
INGEST_BATCH = max(1, int(os.environ.get("FDTEXT_INGEST_BATCH", "200") or 200))

_NUL = "\x00"


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return _NUL in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_nul(v) for v in value)
    return False


def _strip_nul(value: Any) -> Any:
    """递归去掉 NUL（含 dict 的键）。"""
    if isinstance(value, str):
        return value.replace(_NUL, "")
    if isinstance(value, dict):
        return {_strip_nul(k): _strip_nul(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_strip_nul(v) for v in value)
    return value


@dataclass
class Doc:
    """采集器产出的规范化文档。"""

    domain: str
    source: str
    source_id: str
    title: str | None
    content: str
    metadata: dict = field(default_factory=dict)
    origin: str = "crawl"
    cold_block_key: str | None = None

    def __post_init__(self) -> None:
        if self.origin not in VALID_ORIGINS:
            raise ValueError(f"origin 非法: {self.origin}，可用={VALID_ORIGINS}")
        if not (self.domain and self.source and self.source_id):
            raise ValueError("domain/source/source_id 均不可为空")
        # PostgreSQL 的 text 列与 jsonb 都拒收 NUL：psycopg 抛
        #   DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes
        # jsonb 抛 unsupported Unicode escape sequence。NUL 只可能来自上游脏数据，
        # 而且**看原始字节查不出来**：它以 JSON 转义 `\u0000`（6 个 ASCII 字符）落盘，
        # `json.loads` 之后才变成真 NUL（实测 fd-info spool：12438 行里 13 行 content 如此）。
        # 因为一条脏数据会让**整批**入库失败（2026-11-08 起 fd-info-ship-spool 连续 13 天
        # 每 5 分钟失败一次，5000 条全被 13 条毒数据拖死），所以在 Doc 这个统一入口清洗，
        # 而不是让每个 loader 各修一遍。清洗在 content_hash 之前，保证反复摄取哈希稳定。
        cleaned: list[str] = []
        if self.content and _NUL in self.content:
            self.content = self.content.replace(_NUL, "")
            cleaned.append("content")
        if self.title and _NUL in self.title:
            self.title = self.title.replace(_NUL, "")
            cleaned.append("title")
        if _has_nul(self.metadata):
            self.metadata = _strip_nul(self.metadata)
            cleaned.append("metadata")
        if cleaned:
            logger.warning(
                "NUL 已于入口清除：%s/%s/%s 的 %s（上游脏数据；PostgreSQL 不接受 NUL 字节）",
                self.domain, self.source, self.source_id, ",".join(cleaned),
            )

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def _placement(d: Doc) -> tuple[str | None, str | None]:
    """正文归处：短正文进 content 列，长正文落冷存只留指针。

    二者不得同时为空——否则嵌入 worker 取不到文本、无法切块（实测 4 部剧本里
    3 部因此失败，见 text-engine-short-doc-storage）。
    """
    block_key = d.cold_block_key
    content_col = None
    if block_key is None:
        if len(d.content) >= COLD_STORE_MIN_CHARS:
            from .coldstore import put_block

            block_key, _ = put_block(
                d.domain,
                f"{hashlib.sha256(d.source_id.encode()).hexdigest()[:16]}.blk",
                d.content,
            )
        else:
            content_col = d.content
    if block_key is None and content_col is None:
        raise ValueError(
            f"文档 {d.domain}/{d.source}/{d.source_id} 正文无归处："
            "content 与 cold_block_key 同时为空，拒绝入库（须补全而非静默写入）"
        )
    return block_key, content_col


def _supersede(existing_id: int) -> None:
    """旧版本整行退役（保留可查），其切块与待办嵌入任务随之失效。"""
    execute(
        "UPDATE corpus_documents SET status=%s, updated_at=now() WHERE id=%s",
        (SUPERSEDED, existing_id),
    )
    execute("DELETE FROM corpus_chunks WHERE document_id=%s", (existing_id,))
    execute("DELETE FROM embed_tasks WHERE document_id=%s", (existing_id,))


def _load_one(d: Doc, bkey: str, stats: dict) -> None:
    """逐条路径。只用于「需要逐条判断」的情形：批内身份重复（后到者要看前者的结果）。

    其它情形一律走 `_load_batch`——库在 tailnet，逐条对话等于把 65ms 往返乘以文档数。
    """
    existing = fetch_one(
        # MUST pin status：同一 (domain,source,source_id) 会因版本更替存在多行，
        # 不限定就靠堆上的物理行序取"第一行"，取到已退役行会把「退役」打在死行上
        # （no-op），随后盲插新 current → 撞 uq_document_current_only。
        # 此前 test_news_versioning 能过纯属顺序碰巧（UPDATE 会把新元组挪到堆尾）。
        # 实录 2026-11-08：fd-info-ship-spool 追赶时对 cninfo/1225615052 撞唯一键。
        "SELECT id, content_hash FROM corpus_documents"
        " WHERE domain=%s AND source=%s AND source_id=%s AND status=%s",
        (d.domain, d.source, d.source_id, CURRENT),
    )
    if existing and existing["content_hash"] == d.content_hash:
        stats["skipped"] += 1
        return
    if existing:
        _supersede(existing["id"])
        stats["superseded"] += 1

    block_key, content_col = _placement(d)
    row = fetch_one(
        """
        INSERT INTO corpus_documents
            (domain, source, source_id, title, content_hash, status, origin,
             cold_block_key, content, metadata, provenance)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
        RETURNING id
        """,
        (
            d.domain, d.source, d.source_id, d.title, d.content_hash, CURRENT,
            d.origin, block_key, content_col, json.dumps(d.metadata),
            json.dumps(Provenance.now(d.source, d.source_id).to_dict()),
        ),
    )
    if row is None:
        # RETURNING 不应为空；发生了就诚实失败而非静默续跑
        raise RuntimeError(f"入库未拿到 id: {d.domain}/{d.source}/{d.source_id}")
    stats["inserted"] += 1

    execute(
        """
        INSERT INTO embed_tasks (document_id, domain, batch_key, state)
        VALUES (%s,%s,%s,'pending')
        ON CONFLICT (document_id, batch_key) DO NOTHING
        """,
        (row["id"], d.domain, bkey),
    )
    stats["queued"] += 1


def _load_batch(domain: str, source: str, chunk: list[Doc], bkey: str, stats: dict) -> None:
    """批量路径：一次取去重、一次写正文+嵌入任务。

    语义必须与 `_load_one` 逐条一致（去重 / 退役 / 冷存 / 嵌入任务四件事一件不少），
    只是把 N 次往返压成常数次。语句数用 scripts/bench_ingest_batch.py 守着。
    """
    if not chunk:
        return
    ids = [d.source_id for d in chunk]
    existing = {
        r["source_id"]: r
        for r in fetch_all(
            # 与 _load_one 同一条谓词：status 必须钉 CURRENT（见那里的实录）
            "SELECT source_id, id, content_hash FROM corpus_documents"
            " WHERE domain=%s AND source=%s AND status=%s AND source_id = ANY(%s)",
            (domain, source, CURRENT, ids),
        )
    }

    pending: list[tuple[Doc, str | None, str | None]] = []
    for d in chunk:
        ex = existing.get(d.source_id)
        if ex and ex["content_hash"] == d.content_hash:
            stats["skipped"] += 1
            continue
        if ex:
            # 版本更替仍走逐条：要清切块与待办嵌入任务，属低频路径
            _supersede(ex["id"])
            stats["superseded"] += 1
        block_key, content_col = _placement(d)
        pending.append((d, block_key, content_col))
    if not pending:
        return

    values_sql = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)"] * len(pending))
    params: list[Any] = []
    for d, block_key, content_col in pending:
        params.extend([
            domain, source, d.source_id, d.title, d.content_hash, CURRENT, d.origin,
            block_key, content_col, json.dumps(d.metadata),
            json.dumps(Provenance.now(d.source, d.source_id).to_dict()),
        ])
    insert_sql = (
        "INSERT INTO corpus_documents"
        " (domain, source, source_id, title, content_hash, status, origin,"
        " cold_block_key, content, metadata, provenance) VALUES "
        + values_sql
        + " RETURNING id, source_id"
    )
    try:
        # 一个批次一个事务：正文与嵌入任务要么一起成立，要么一起不成立
        with cursor(commit=True) as cur:
            cur.execute(insert_sql, tuple(params))
            got = cur.fetchall()
            if len(got) != len(pending):
                raise RuntimeError(f"批量插入返回 {len(got)} 行，期望 {len(pending)} 行")
            task_sql = ",".join(["(%s,%s,%s,'pending')"] * len(got))
            task_params: list[Any] = []
            for row in got:
                task_params.extend([row[0], domain, bkey])
            cur.execute(
                "INSERT INTO embed_tasks (document_id, domain, batch_key, state) VALUES "
                + task_sql
                + " ON CONFLICT (document_id, batch_key) DO NOTHING",
                tuple(task_params),
            )
    except Exception as exc:
        lo, hi = pending[0][0].source_id, pending[-1][0].source_id
        raise RuntimeError(
            f"批量入库失败（{domain}/{source}，{len(pending)} 篇，source_id {lo}…{hi}）："
            f"{type(exc).__name__}: {exc}"
        ) from exc

    stats["inserted"] += len(got)
    stats["queued"] += len(got)


def load_documents(docs: Iterable[Doc], batch_key: str | None = None) -> dict:
    """幂等批量入库。

    返回统计：inserted / skipped（未变）/ superseded（旧版退役）/ queued（嵌入任务）。
    未变则跳过且不产生嵌入任务；新版到达则旧版退役并重建索引面。

    按 `(domain, source)` 分组后**成批**入对话（`INGEST_BATCH` 篇/批）。批内身份重复的
    文档退回逐条路径，因为后者要看到前者刚写入的结果。
    """
    stats = {"inserted": 0, "skipped": 0, "superseded": 0, "queued": 0}
    bkey = batch_key or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    groups: dict[tuple[str, str], list[Doc]] = {}
    for d in docs:
        groups.setdefault((d.domain, d.source), []).append(d)

    for (domain, source), group in groups.items():
        counts: dict[str, int] = {}
        for d in group:
            counts[d.source_id] = counts.get(d.source_id, 0) + 1
        repeated = {sid for sid, n in counts.items() if n > 1}
        if repeated:
            keep_batch = [d for d in group if d.source_id not in repeated]
        else:
            keep_batch = group
        for i in range(0, len(keep_batch), INGEST_BATCH):
            _load_batch(domain, source, keep_batch[i:i + INGEST_BATCH], bkey, stats)
        if repeated:
            for d in group:
                if d.source_id in repeated:
                    _load_one(d, bkey, stats)

    return stats


def iter_structured(s3: Any, bucket: str, domain: str, limit: int = 1000) -> Iterator[dict]:
    """读 `<bucket>/<domain>/structured/*.jsonl`，按 run 折叠为最新版后产出 dict。

    桶名 SHALL 为显式参数：不从客户端属性反查（曾从 `s3._bucket` 取，而该属性
    只是测试替身的约定，真实 boto3 客户端没有——`Bucket=None` 传 botocore 必然
    TypeError，生产路径从未跑通。gov 垂域接入时实测暴露）。

    **按 run 折叠为最新版**（latest-wins）：分片键含 run id 且不可变，同一文档
    会随每次重抓出现多份分片；不折叠则逐行全入库撞版本退役路径（同一身份
    多行 → 多次退役）。规则 = 分片键字节序排序（键含 14 位 run 时间戳，
    字节序即时间序）、同一 `(source, source_id)` 后出现者胜。limit 按
    折叠后计数。
    """
    prefix = f"{domain}/structured/"
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    continuation = None
    parts: list[tuple[str, bytes]] = []  # (key, body)
    while True:
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".jsonl"):
                continue
            parts.append((key, s3.get_object(Bucket=bucket, Key=key)["Body"].read()))
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")

    folded: dict[str, dict] = {}
    for key, body in sorted(parts, key=lambda kv: kv[0]):
        for line in body.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            folded[f"{row.get('source')}:{row.get('source_id')}"] = row  # 后出现者胜

    seen = 0
    for row in folded.values():
        if seen >= limit:  # 无法用 enumerate：这是"已产出条数"，不是下标
            return
        yield row
        seen += 1


def current_document_ids(domain: str) -> list[int]:
    return [
        r["id"]
        for r in fetch_all(
            "SELECT id FROM corpus_documents WHERE domain=%s AND status=%s ORDER BY id",
            (domain, CURRENT),
        )
    ]
