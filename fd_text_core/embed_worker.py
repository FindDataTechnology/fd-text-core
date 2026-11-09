"""嵌入 worker：claim-ledger 认领 + 锚点余弦自检门 + 批量 upsert。

自检门（spec text-indexing-search）：开工前用库内锚点重算向量，与历史锚点
余弦 >= 0.999 才允许执行。低于阈值说明模型或通道漂移了——继续写入会污染
整库向量且难以回滚，因此拒开工并明确报错。

claim-ledger：一次认领一批 pending 任务并置 claimed，成功后置 finished；中断
后重跑只处理未完成任务，绝不重复已完成工作（02.1 的断点等价性要求）。
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

from . import coldstore
from .chunking import chunk_text, verify_lossless
from .db import cursor, execute, fetch_all, fetch_one
from .embedding import Embedder
from .tokenize import cjk_tokens_text
from .verticals.registry import get_strategy

ANCHOR_TAG = "worker-startup"
# 数字占比超过此阈值的切块不送嵌入（表格碎片，无语义内容）。
# 0 或负数 = 关闭过滤。实测 news 语料 11.3% 的块 > 0.3。
MAX_DIGIT_RATIO = float(os.environ.get("FDTEXT_EMBED_MAX_DIGIT_RATIO", "0.3") or 0.3)

SIMILARITY_FLOOR = 0.999


class GateFailure(RuntimeError):
    """自检门未通过，拒绝开工。"""


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"维度不符: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


class SelfCheckUnavailable(RuntimeError):
    """The anchor probe could not be answered (upstream 5xx / timeout).

    Distinct from a similarity failure on purpose: "cannot check" is a transient
    upstream condition — nothing is written, so skipping the round is safe — while
    "cosine below floor" means the model or channel drifted and MUST stop the
    worker. Production 2026-11-08: a single 503 on this one probe aborted whole
    rounds, which is why some rounds embedded ~10 chunks and then exited red.
    """


def run_self_check(embedder: Embedder, model: str, *, bootstrap_text: str | None = None) -> float:
    """锚点余弦自检。首次（无锚点）写入锚点并返回 1.0。"""
    anchor = fetch_one("SELECT embedding FROM anchor_embedding WHERE tag=%s", (ANCHOR_TAG,))
    text = bootstrap_text or "fd-text-core 嵌入自检锚点 · embedding self-check anchor"
    try:
        vec = embedder.embed([text]).vectors[0]
    except Exception as exc:
        raise SelfCheckUnavailable(str(exc)[:200]) from exc
    if anchor is None:
        execute(
            "INSERT INTO anchor_embedding (tag, model, dim, embedding) VALUES (%s,%s,%s,%s::vector)",
            (ANCHOR_TAG, model, len(vec), vector_literal(vec)),
        )
        return 1.0
    historical = _parse_vector(anchor["embedding"])
    sim = cosine(historical, vec)
    if sim < SIMILARITY_FLOOR:
        raise GateFailure(
            f"嵌入自检未通过：与历史锚点余弦 {sim:.6f} < {SIMILARITY_FLOOR}"
            f"（模型 {model} 或通道疑似漂移，拒绝执行以免污染向量库）"
        )
    return sim


def _parse_vector(raw) -> list[float]:
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    s = str(raw).strip().strip("[]")
    return [float(x) for x in s.split(",") if x]


@dataclass
class WorkerResult:
    claimed: int = 0
    embedded: int = 0
    chunks: int = 0
    failed: int = 0
    retryable: int = 0  # 失败中属瞬时的（限流/超时/连接）——已被 cooldown 后放回 pending
    reclaimed: int = 0  # 本次开工回收的陈旧 claim 数


def ensure_chunks(doc_row: dict, text: str) -> int:
    """按垂域策略切块入库（幂等：已有切块则跳过），返回写入块数。"""
    existing = fetch_one(
        "SELECT count(*)::int AS n FROM corpus_chunks WHERE document_id=%s", (doc_row["id"],)
    )
    if existing and existing["n"] > 0:
        return 0
    strategy = get_strategy(doc_row["domain"])
    chunks = chunk_text(text, strategy)
    ok, detail = verify_lossless(text, chunks)
    if not ok:
        raise ValueError(f"document {doc_row['id']} 切块不无损：{detail}")
    cjk = _cjk_enabled(doc_row["domain"])
    t2s = _t2s_enabled(doc_row["domain"])
    cfg = _tsv_config(doc_row["domain"])
    for c in chunks:
        if cjk:
            # 引擎分词：token 以空格连接喂 simple 配置（simple 按空白切且不做
            # 词干化/停用词，正好承接我们的 token）。查询侧走 cjk_tsquery 同构
            # （含同一 t2s 归一开关——两侧取值必须一致，见 tokenize 模块）。
            execute(
                """
                INSERT INTO corpus_chunks
                    (domain, document_id, chunk_no, chunk_text, char_start, char_end,
                     section_path, tsv, tsv_config)
                VALUES (%s,%s,%s,%s,%s,%s,%s, to_tsvector('simple', %s), 'engine-cjk')
                ON CONFLICT (domain, document_id, chunk_no) DO NOTHING
                """,
                (
                    doc_row["domain"], doc_row["id"], c.chunk_no, c.chunk_text,
                    c.char_start, c.char_end, c.section_path,
                    cjk_tokens_text(c.chunk_text, t2s=t2s),
                ),
            )
        else:
            execute(
                """
                INSERT INTO corpus_chunks
                    (domain, document_id, chunk_no, chunk_text, char_start, char_end,
                     section_path, tsv, tsv_config)
                VALUES (%s,%s,%s,%s,%s,%s,%s, to_tsvector(%s::regconfig, %s), %s)
                ON CONFLICT (domain, document_id, chunk_no) DO NOTHING
                """,
                (
                    doc_row["domain"], doc_row["id"], c.chunk_no, c.chunk_text,
                    c.char_start, c.char_end, c.section_path,
                    cfg, c.chunk_text, cfg,
                ),
            )
    return len(chunks)


def _cjk_enabled(domain: str) -> bool:
    from .verticals.registry import get

    try:
        return bool(get(domain).cjk)
    except KeyError:
        return False


def _t2s_enabled(domain: str) -> bool:
    """垂域繁简归一开关（默认 True）。未注册垂域回退 True（与垂域默认同值）。"""
    from .verticals.registry import get

    try:
        return bool(get(domain).t2s)
    except KeyError:
        return True


def _tsv_config(domain: str) -> str:
    """垂域配置优先（与 querylib 同源）；未注册垂域回退环境变量。

    此前实现完全忽略 domain、只读环境变量，与查询侧读垂域配置的行为不一致
    ——两处配置面分裂会让写入与查询用不同分词器，正是要防的静默失配。
    """
    import os

    from .verticals.registry import get

    try:
        v = get(domain)
        if v.tsv_config:
            return v.tsv_config
    except KeyError:
        pass
    return os.environ.get("FDTEXT_TSV_CONFIG", "simple")


def reclaim_stale(stale_after: str = "5 minutes") -> int:
    """回收陈旧 claim：worker 崩溃/被杀会把任务留在 claimed 态，

    而 claim_batch 只挑 pending，于是这批任务会永久悬空（规模冒烟中实测
    撞到：脚本达上限退出后 74 条任务卡 claimed，对应 73 篇文档有正文却无切块）。
    开工前先回收，保证「中断后续跑」的等价性承诺成立。
    """
    return execute(
        "UPDATE embed_tasks SET state='pending', claimed_at=NULL "
        "WHERE state='claimed' AND claimed_at < now() - %s::interval",
        (stale_after,),
    )


#: 可重试的失败原因。命中其一才放回 pending——否则一条永远失败的脏文本会每轮
#: 重烧一次 API（`claim_batch` 只看 pending，故重试必须由这里显式做）。
RETRYABLE_HINTS = ("429", "rate limit", "status=5", "timeout", "Connection")

#: Inputs per embedding HTTP call. A document carries ~248 unembedded chunks on
#: average (p90 487, max 567 — measured 2026-11-08), so the old "one call for the
#: whole batch" sent ~2,500 inputs per request and the upstream answered 503 /
#: timed out: the affected documents could then never finish, and the head of the
#: FIFO queue stayed there retrying forever. Slicing keeps every request small;
#: the vectors are concatenated back in order, so callers see no difference.
EMBED_INPUTS_PER_CALL = int(os.environ.get("FDTEXT_EMBEDDING_MAX_INPUTS", "96"))


def _embed_all(embedder, texts: list[str]) -> list[list[float]]:
    """Embed every text, in slices of EMBED_INPUTS_PER_CALL, order preserved."""
    out: list[list[float]] = []
    for start in range(0, len(texts), EMBED_INPUTS_PER_CALL):
        chunk = texts[start:start + EMBED_INPUTS_PER_CALL]
        vectors = embedder.embed(chunk).vectors
        if len(vectors) != len(chunk):
            raise RuntimeError(
                f"嵌入返回 {len(vectors)} 条，期望 {len(chunk)} 条"
                f"（slice {start}..{start + len(chunk)}）"
            )
        out.extend(vectors)
    return out


def is_retryable(error: str | None) -> bool:
    """True when the failure is transient (rate limit / timeout / connection).

    The same list drives the SQL below, so the classifier a caller uses and the
    requeue rule cannot drift apart.
    """
    text = (error or "").lower()
    return any(hint.lower() in text for hint in RETRYABLE_HINTS)


_RETRYABLE_SQL = " OR ".join(["error LIKE %s"] * len(RETRYABLE_HINTS))


def retry_rate_limited(stale_after: str = "10 minutes", limit: int = 500) -> int:
    """把因**可重试原因**失败的任务放回 pending（限时 + 限量，避免打转）。

    实录（2026-11-08）：一轮嵌入撞上游 `429 Upstream rate limit exceeded` 后，
    2 篇文档落 `state='failed'` 就再也没人管——`claim_batch` 只挑 pending，
    `reclaim_stale` 只回收 claimed。于是"限流一次 = 这几篇永久没向量"，
    而检索面缺块不会报错，只会慢慢查不到。
    故失败的里只挑可重试原因、且已过冷却期、且每次限量，噪声与打转都被挡住。
    """
    return execute(
        "UPDATE embed_tasks SET state='pending', error=NULL, finished_at=NULL "
        "WHERE id IN ("
        "  SELECT id FROM embed_tasks WHERE state='failed'"
        "   AND finished_at < now() - %s::interval"
        f"   AND ({_RETRYABLE_SQL})"
        "   ORDER BY finished_at LIMIT %s"
        ")",
        (stale_after, *[f"%{hint}%" for hint in RETRYABLE_HINTS], limit),
    )


def claim_batch(limit: int = 50) -> list[dict]:
    rows = fetch_all(
        """
        UPDATE embed_tasks SET state='claimed', claimed_at=now()
        WHERE id IN (
            SELECT id FROM embed_tasks WHERE state='pending'
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s
        )
        RETURNING id, document_id, domain, batch_key
        """,
        (limit,),
    )
    return rows


def run_once(embedder: Embedder, model: str, limit: int = 50) -> WorkerResult:
    """跑一批。开工前必过自检门。

    嵌入调用**按整批合并**（2026-11-08 批量化）：库在 tailnet，一次 HTTP 往返换来的
    向量数与 input 条数无关——实测 1 条 3.9s、8 条 2.2s，逐篇调用等于把这段延迟
    按文档数乘一遍。合并后单批 10 篇的 40+ 块只发 1 次请求。

    合并失败时退回逐篇，保住「哪一篇坏」的归因（否则一条脏文本会让整批记失败，
    与"失败必须可定位"冲突）。
    """
    run_self_check(embedder, model)
    res = WorkerResult()
    res.reclaimed = reclaim_stale()
    retried = retry_rate_limited()
    if retried:
        print(f"[embed] 可重试失败的任务回到队列：{retried} 条（限流/超时，非内容问题）")
    tasks = claim_batch(limit)
    res.claimed = len(tasks)

    # ── 阶段一：逐篇准备（取文档 / 切块 / 收集待嵌块），失败就地归因 ──
    prepared: list[tuple[dict, dict, list[dict]]] = []
    failed = 0
    retryable = 0
    for t in tasks:
        try:
            doc = fetch_one("SELECT * FROM corpus_documents WHERE id=%s", (t["document_id"],))
            if doc is None:
                execute("UPDATE embed_tasks SET state='missing_doc', finished_at=now() WHERE id=%s", (t["id"],))
                failed += 1
                continue
            text = _document_text(doc)
            res.chunks += ensure_chunks(doc, text)
            # 只嵌尚未嵌入的块，断点续嵌天然幂等。
            # 数字密集块（表格碎片）不嵌：它们没有语义内容，向量只会把近邻检索往
            # "报表行"上带偏。实测 news 语料 11.3% 的块数字占比 > 0.3，典型长这样：
            # `67 4,800.82 6 苏商创投 金张科技5.24%股权 - 7,335.54`。
            # 阈值 FDTEXT_EMBED_MAX_DIGIT_RATIO 可调，<=0 关闭过滤（回退到全嵌）。
            targets = fetch_all(
                "SELECT id, chunk_text FROM corpus_chunks"
                " WHERE document_id=%s AND embedding IS NULL"
                "   AND (%s::float <= 0 OR"
                "        (length(chunk_text) - length(regexp_replace(chunk_text, '[0-9]', '', 'g')))::float"
                "        / greatest(length(chunk_text), 1) < %s::float)"
                " ORDER BY chunk_no",
                (doc["id"], MAX_DIGIT_RATIO, MAX_DIGIT_RATIO),
            )
            prepared.append((t, doc, targets))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)[:500]
            execute(
                "UPDATE embed_tasks SET state='failed', finished_at=now(), error=%s WHERE id=%s",
                (message, t["id"]),
            )
            failed += 1
            if is_retryable(message):
                retryable += 1

    # ── 阶段二：整批一次嵌入（跨文档）──
    texts = [row["chunk_text"] for _, _, targets in prepared for row in targets]
    vectors: list[list[float]] | None = None
    if texts:
        try:
            vectors = _embed_all(embedder, texts)
        except Exception as exc:  # noqa: BLE001 —— 退回逐篇，见 docstring
            print(f"[embed] 批量嵌入失败，退回逐篇归因：{type(exc).__name__}: {str(exc)[:120]}")
            vectors = None

    # ── 阶段三：写回并收尾 ──
    pos = 0
    for t, doc, targets in prepared:
        try:
            if vectors is not None:
                mine = vectors[pos:pos + len(targets)]
                pos += len(targets)
            elif targets:
                mine = _embed_all(embedder, [row["chunk_text"] for row in targets])
            else:
                mine = []
            if targets:
                with cursor(commit=True) as cur:
                    cur.executemany(
                        "UPDATE corpus_chunks SET embedding=%s::vector, embedded_at=now()"
                        " WHERE id=%s AND domain=%s",
                        [(vector_literal(v), row["id"], doc["domain"])
                         for row, v in zip(targets, mine)],
                    )
            execute(
                "UPDATE embed_tasks SET state='finished', finished_at=now() WHERE id=%s", (t["id"],)
            )
            res.embedded += 1
        except Exception as exc:  # noqa: BLE001
            message = str(exc)[:500]
            execute(
                "UPDATE embed_tasks SET state='failed', finished_at=now(), error=%s WHERE id=%s",
                (message, t["id"]),
            )
            failed += 1
            if is_retryable(message):
                retryable += 1
    res.failed = failed
    res.retryable = retryable
    return res


def _document_text(doc: dict) -> str:
    """取正文：短文档在 content 列，长文档在冷存块；二者皆空如实报错。"""
    if doc.get("content"):
        return doc["content"]
    if doc.get("cold_block_key"):
        return coldstore.get_block(doc["cold_block_key"])
    raise RuntimeError(
        f"document {doc['id']} 无正文可用（content 与 cold_block_key 皆为空）——"
        "属数据管线缺口，须补全而非静默跳过"
    )
