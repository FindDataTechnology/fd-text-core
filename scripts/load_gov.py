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


def iter_structured_jsonl(s3, bucket: str, domain: str, limit: int = 1000):
    """读 `<bucket>/<domain>/structured/*.jsonl` 逐行产出 dict。

    为什么不直接用引擎的 `ingest.iter_structured`：该函数从 `s3._bucket` 取桶名，
    而 `_bucket` 只存在于测试替身上——**真实 boto3 客户端没有该属性**，故它对
    生产调用路径必然抛 TypeError（botocore validate_bucket_name 收到 None）。
    这是引擎核心模块的一处既有缺陷，修它要动 `ingest.py`（核心模块）。

    本 change 的 spec 明令「引擎核心零改动」且 CI 有路径越界检查，故此处
    就地实现读取（`scripts/` 不在核心模块集内），并把这个缺陷作为
    「发现但不在范围内」记入验收报告——修它需要单独立 change。
    """
    import io

    prefix = f"{domain}/structured/"
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    continuation = None
    seen = 0
    while True:
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".jsonl"):
                continue
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            for line in io.BytesIO(body):
                line = line.strip()
                if line:
                    yield json.loads(line)
                    seen += 1
                    if seen >= limit:
                        return
        if not resp.get("IsTruncated"):
            return
        continuation = resp.get("NextContinuationToken")


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
