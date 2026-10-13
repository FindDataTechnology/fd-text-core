#!/usr/bin/env python3
"""公文垂域灌库脚本：读 `<bucket>/gov/structured/*.jsonl` -> 入库 -> 切块 + 嵌入。

与论文/剧本 loader 的区别：公文语料**不由本仓采集**（采集在 fd-cn-gov 的
`fd_cn_gov.detail` 模块，跨仓借用的增量能力）。本脚本只做消费侧：
引擎的 `ingest.iter_structured` 读对象存储的 JSONL，转成 `Doc` 入库。

这正是引擎「语言中立摄取契约」的验证点：采集器可以是任何语言/任何仓，
只要把 JSONL 写进 `<bucket>/<domain>/structured/`。

用法：
    export FDTEXT_DATABASE_URL=...           # 必填
    export FDTEXT_S3_ENDPOINT=... FDTEXT_S3_ACCESS_KEY=... FDTEXT_S3_SECRET_KEY=...
    python3 scripts/load_gov.py --fake-embed         # 管线验证
    python3 scripts/load_gov.py                      # 真嵌入通道
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core import embed_worker  # noqa: E402
from fd_text_core.coldstore import _s3  # noqa: E402
from fd_text_core.ingest import Doc, load_documents  # noqa: E402
from fd_text_core.provenance import Provenance  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_papers import build_embedder  # noqa: E402

DOMAIN = "gov"


def _facet_issuer(raw) -> str | None:
    """把机关名对到 facet 词表；词表外归「其他」。

    为什么归「其他」而不是原值：facet 校验是 fail-closed（词表外取值直接报错），
    留着词表外的值会让按机关过滤整体不可用。归「其他」保留了「这条有机关信息、
    只是不在词表内」的事实，且原值仍在 metadata.issuer_raw 可追溯。
    """
    from fd_text_core.verticals.gov import FACETS

    if not raw:
        return None
    s = str(raw).strip()
    if s in FACETS["issuer"]:
        return s
    return "其他"


def _facet_doc_type(raw) -> str | None:
    from fd_text_core.verticals.gov import FACETS

    if not raw:
        return None
    s = str(raw).strip()
    return s if s in FACETS["doc_type"] else None


def _facet_period(raw) -> str | None:
    """年份 facet：取成文日期（落款优先）的年份，落回发布年份。

    为何用 doc_date 优先：公文的「时期」是其成文时间，发布页日期可能晚数月
    （实测有 2026 年发布、成文 2024 年的令）。词表外的年份如实返回该年份——
    词表是随语料扩充的，返回真实值比截断成词表内的值更诚实。
    """
    if not raw:
        return None
    m = re.match(r"(\d{4})", str(raw))
    return m.group(1) if m else None


def _index_header(md: dict, row: dict) -> str:
    """构造进索引的检索头（文号 · 机关 · 成文日期 · 文种）。

    只放**结构化字段**，不放正文内容——头的作用是让「元数据表里才有、正文里
    没有」的检索键（尤其是文号）进入索引面。实测该类文档占 35/126。
    头为空的文档不拼（避免给每篇都加一行无信息的噪音）。
    """
    parts = []
    if md.get("doc_number"):
        parts.append(f"文号：{md['doc_number']}")
    if md.get("issuer"):
        parts.append(f"发文机关：{md['issuer']}")
    if row.get("doc_date"):
        parts.append(f"成文日期：{row['doc_date']}")
    if md.get("doc_type"):
        parts.append(f"文种：{md['doc_type']}")
    return " ".join(parts)


def iter_structured_jsonl(s3, bucket: str, domain: str, limit: int = 1000):
    """读 `<bucket>/<domain>/structured/*.jsonl`，**按 run 折叠为最新版**后逐行产出。

    为什么必须折叠（实测踩到）：分片键含 run id 且不可变，故同一文档会随每次
    重抓出现多份分片（实测 3 次重抓后同一篇有 3 行）。若逐行全部入库：
      · 同批内先插入 A 版、再插入 B 版 → 引擎把 A 标记为 superseded，
        再遇 C 版时又要标记 B 为 superseded，而 **A 已是 superseded**，
        撞上 `uq_document_current (domain,source,source_id,status)` 唯一约束
        （该约束允许一 current + 一 superseded，但不允许两个 superseded）。
      · 结果整批入库失败，且报错指向约束而非根因。

    折叠规则（照 fd-law-data 的成熟形态）：分片键按字节序排序即 run 时间序，
    同一 source_id 后出现者胜（latest-wins）。这样每篇只入库一行，
    且是最新一次抓取的结果。

    为什么不改用引擎的 `ingest.iter_structured`：见模块 docstring（它对真实
    boto3 客户端必然抛 TypeError，属引擎核心缺陷，不在本 change 改动面内）。
    """
    prefix = f"{domain}/structured/"
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    continuation = None
    # 按分片键排序后折叠：键含 14 位 run 时间戳，字节序即时间序
    parts: list[tuple[str, str]] = []  # (key, body_text)
    while True:
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".jsonl"):
                continue
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            parts.append((key, body.decode("utf-8")))
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")

    folded: dict[str, dict] = {}
    for key, text in sorted(parts, key=lambda kv: kv[0]):
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            sid = f"{row.get('source')}:{row.get('source_id')}"
            folded[sid] = row  # 后出现者胜（分片已按 run 时间序排列）

    for i, row in enumerate(folded.values()):
        if i >= limit:
            return
        yield row


def rows_to_docs(rows) -> tuple[list[Doc], list[str]]:
    """JSONL 行 -> Doc。返回 (docs, 跳过原因)。

    跳过条件必须如实报告而非静默：缺正文的行不入库（否则嵌入 worker 取不到
    文本、白占一条待办），但调用方要能看到跳了几条、为什么。
    """
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

        md = dict(row.get("metadata") or {})
        # provenance 三元组随文档落库（spec 要求 source/stable_id/as_of 齐备）
        md["provenance"] = Provenance.now(source, source_id).to_dict()
        md["source_url"] = str(row.get("url") or "")
        md["published"] = row.get("published")
        md["doc_date"] = row.get("doc_date")

        # facet 投影：检索按 metadata->>'<facet>' 过滤，故 facet 字段必须**显式落库**。
        # 不做这步的表现是「facet 过滤恒 0 命中」——采集侧 metadata 里已有 issuer/doc_type，
        # 但 period 谁都没算，而词表里列了它（实测按机关过滤也 0 命中：字段名对但
        # 值域未归一，已在 parser 侧修）。此处统一补齐三个 facet 的**归一后**取值。
        md["issuer"] = _facet_issuer(md.get("issuer"))
        md["doc_type"] = _facet_doc_type(md.get("doc_type"))
        md["period"] = _facet_period(md.get("doc_date") or md.get("published"))

        # 检索头：把文号/机关/成文日期拼进正文首行。
        #
        # 为什么必须做（实测，不是设计洁癖）：**35/126 篇的文号只存在于站点的
        # 元数据表里，正文中根本不出现**（实测「自然资办函〔2026〕1564号」这类
        # 自然资源部文档，正文直接以「各省、自治区…」开头）。切块与 tsv 只覆盖
        # content 列，故这些文档的文号**永远进不了索引**——「按文号精查单篇」
        # 这个 spec 明列的预设对其必然 0 命中。实测 6 篇抽样里 keyword 通道
        # 只命中 2 篇，正是这个原因。
        #
        # 选择拼进正文而非改引擎：这是**垂域的摄取期组装**，与 load_papers.py
        # 把「标题+摘要」组成 body 同一手法（引擎的 content 就是「要索引的文本」）。
        # 拼接可逆（头是确定前缀），且 provenance 里另存原始正文长度便于核对。
        header = _index_header(md, row)
        if header and not content.startswith(header):
            content = f"{header}\n{content}"
            md["index_header"] = header

        docs.append(
            Doc(
                domain=DOMAIN,
                source=source,
                source_id=source_id,
                title=(str(row.get("title") or "").strip() or None),
                content=content,
                metadata=md,
            )
        )
    return docs, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000, help="最多读多少行 JSONL")
    ap.add_argument("--embed-batch", type=int, default=20, help="每批认领任务数")
    ap.add_argument("--fake-embed", action="store_true", help="用确定性假向量（管线验证用）")
    args = ap.parse_args()

    s3, bucket = _s3()
    print(f"[read] s3://{bucket}/{DOMAIN}/structured/  (limit={args.limit})")
    rows = list(iter_structured_jsonl(s3, bucket, DOMAIN, limit=args.limit))
    print(f"[read] 读到 {len(rows)} 行")

    docs, skipped = rows_to_docs(rows)
    if skipped:
        print(f"[warn] 跳过 {len(skipped)} 行（不入库）：")
        for s in skipped[:10]:
            print(f"       - {s}")
    print(f"[ingest] 待入库 {len(docs)} 篇")

    stats = load_documents(docs, batch_key=f"load_{DOMAIN}")
    print(f"[ingest] {stats}")

    embedder, model = build_embedder(args)
    if args.fake_embed:
        # 自检门对假向量无意义（模型名不在锚点表里），跳过自检直接跑
        embed_worker.run_self_check = lambda *a, **k: 1.0  # type: ignore[assignment]
    done = 0
    failed = 0
    while True:
        res = embed_worker.run_once(embedder, model, limit=args.embed_batch)
        if res.claimed == 0:
            break
        done += res.embedded
        failed += res.failed
        print(f"[embed] claimed={res.claimed} embedded={res.embedded} chunks={res.chunks} failed={res.failed}")

    print(f"[done] {DOMAIN} 域就绪：文档 {len(docs)}、嵌入块 {done}、失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
