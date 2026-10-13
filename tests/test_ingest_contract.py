"""对象存储摄取契约与版本退役的防复发用例（change ingest-supersede-engine-fix）。

三处缺陷都曾长期潜伏，根因是测试形态：
  · iter_structured 只被测试替身覆盖（替身才有 _bucket 属性）——真 boto3
    必然 TypeError；
  · 版本退役只测到两版本（v1 退役 + v2 入库）——第三次变更撞
    uq_document_current 唯一约束；
  · 引擎契约无折叠语义——多 run 产物逐行入库即撞上述约束。
故本文件的用例形态对着根因设计：live 真客户端、三版本、多 run 折叠。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core import ingest  # noqa: E402


# ---------- 假 S3 客户端：接口与 boto3 对齐（无 _bucket 等私有属性） ----------

class FakeS3:
    """list_objects_v2 / get_object 最小替身。分页协议同真客户端。"""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def list_objects_v2(self, **kw):
        prefix = kw["Prefix"]
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def get_object(self, **kw):
        return {"Body": FakeS3._Body(self.objects[kw["Key"]])}

    class _Body:
        def __init__(self, data: bytes):
            self._data = data

        def read(self) -> bytes:
            return self._data


def _row(source: str, source_id: str, content: str) -> dict:
    return {"source": source, "source_id": source_id, "content": content,
            "title": f"t-{source_id}"}


def _jsonl(*rows: dict) -> bytes:
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8")


# ---------- 缺陷 ①：桶名显式参数 + 折叠 ----------

def test_iter_structured_takes_explicit_bucket():
    """签名即契约：桶名是第二参数，替身不再需要 _bucket 属性。"""
    import inspect

    sig = inspect.signature(ingest.iter_structured)
    names = list(sig.parameters)
    assert names[:3] == ["s3", "bucket", "domain"]


def test_iter_structured_folds_multi_run_latest_wins():
    """同 source_id 跨两个 run 分片：只产出键序（时间序）较新一版。"""
    old = _row("mnr", "d1", "旧版正文 v1")
    new = _row("mnr", "d1", "新版正文 v2")
    other = _row("mnr", "d2", "另一篇")
    s3 = FakeS3({
        # 键含 14 位 run 时间戳：字节序即时间序；旧 run 分片内还有另一篇
        "gov/structured/run-20261013090000-part001.jsonl": _jsonl(old, other),
        "gov/structured/run-20261013150000-part001.jsonl": _jsonl(new),
    })
    rows = list(ingest.iter_structured(s3, "fd-text", "gov"))
    by_id = {r["source_id"]: r for r in rows}
    assert len(rows) == 2  # 折叠后 d1 只有一行
    assert by_id["d1"]["content"] == "新版正文 v2"
    assert by_id["d2"]["content"] == "另一篇"


def test_iter_structured_limit_counts_after_folding():
    """limit 按折叠后计数（折叠语义进引擎的组成部分）。"""
    s3 = FakeS3({
        "gov/structured/run-20261013090000-part001.jsonl":
            _jsonl(*(_row("mnr", f"d{i}", "x") for i in range(5))),
    })
    rows = list(ingest.iter_structured(s3, "fd-text", "gov", limit=2))
    assert len(rows) == 2


# ---------- 缺陷 ②：版本退役支持任意次 ----------

def _insert_doc(cur, domain, source, sid, content_hash, content):
    cur.execute(
        """INSERT INTO corpus_documents
           (domain,source,source_id,title,content_hash,status,content)
           VALUES (%s,%s,%s,%s,%s,'current',%s) RETURNING id""",
        (domain, source, sid, f"t-{sid}", content_hash, content),
    )
    return cur.fetchone()[0]


def test_three_version_supersede_never_hits_constraint():
    """v1/v2/v3 依次退役：v1、v2 均退役、v3 current，全程无约束违规。

    修前第三次退役抛 `uq_document_current` UniqueViolation（handoff ② 复现
    脚本同型）——本用例在修前会红。
    """
    psycopg = pytest.importorskip("psycopg")
    url = os.environ.get("FDTEXT_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("缺 FDTEXT_DATABASE_URL")

    with psycopg.connect(url.replace("postgresql://", "postgres://")) as c, c.cursor() as cur:
        try:
            id1 = _insert_doc(cur, "_t_ingest", "x", "sid1", "h1", "v1")
            cur.execute("UPDATE corpus_documents SET status='superseded' WHERE id=%s", (id1,))
            id2 = _insert_doc(cur, "_t_ingest", "x", "sid1", "h2", "v2")
            cur.execute("UPDATE corpus_documents SET status='superseded' WHERE id=%s", (id2,))
            id3 = _insert_doc(cur, "_t_ingest", "x", "sid1", "h3", "v3")
            cur.execute("SELECT status FROM corpus_documents WHERE id=ANY(%s) ORDER BY id",
                        ([id1, id2, id3],))
            assert [r[0] for r in cur.fetchall()] == ["superseded", "superseded", "current"]
        finally:
            c.rollback()


def test_partial_unique_index_still_enforced():
    """约束删除不弱化语义：同一身份第二个 current 行仍被拒绝。"""
    psycopg = pytest.importorskip("psycopg")
    url = os.environ.get("FDTEXT_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("缺 FDTEXT_DATABASE_URL")

    with psycopg.connect(url.replace("postgresql://", "postgres://")) as c, c.cursor() as cur:
        try:
            _insert_doc(cur, "_t_ingest", "x", "sid2", "h1", "v1")
            with pytest.raises(psycopg.errors.UniqueViolation):
                _insert_doc(cur, "_t_ingest", "x", "sid2", "h2", "v2")
        finally:
            c.rollback()


# ---------- 防复发之根：真客户端 live 用例 ----------

def test_live_real_boto3_client(request):
    """真 boto3 客户端（非替身）读取真桶 `<FDTEXT_S3_BUCKET>/gov/structured/`。

    本缺陷能长期潜伏的根因是测试只覆盖替身形态，故必须存在一个骗不了人的
    用例。默认跳过（CI 无网内通道）；网内 `pytest --live` 实跑。
    """
    if not request.config.getoption("--live"):
        pytest.skip("live 用例：网内以 pytest --live 实跑")
    boto3 = pytest.importorskip("boto3")
    endpoint = os.environ.get("FDTEXT_S3_ENDPOINT")
    bucket = os.environ.get("FDTEXT_S3_BUCKET", "fd-text")
    if not endpoint:
        pytest.skip("缺 FDTEXT_S3_ENDPOINT")
    s3 = boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=os.environ["FDTEXT_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["FDTEXT_S3_SECRET_KEY"],
        region_name="us-east-1",
    )
    rows = list(ingest.iter_structured(s3, bucket, "gov", limit=1))
    assert rows, "真桶 gov/structured/ 应有产物（gov 垂域已摄取 18 分片）"


# ---------- loader 回接 ----------

def test_load_gov_uses_engine_contract():
    """loader 不再有本地读取实现（折叠语义随引擎统一）。"""
    src = (REPO / "scripts" / "load_gov.py").read_text(encoding="utf-8")
    assert "iter_structured_jsonl" not in src
    assert "from fd_text_core.ingest import" in src
    assert "iter_structured" in src
