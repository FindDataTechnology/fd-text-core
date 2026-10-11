#!/usr/bin/env python3
"""fd-text-core 质量门（只读）。

四检 + 两附加：
  1. 无损切块还原       —— 拼接是否逐字符还原正文、偏移是否连续
  2. provenance 完整性  —— 三元组 source/stable_id/as_of 是否齐备
  3. 嵌入锚点余弦       —— 当前模型向量 vs 历史锚点（>=0.999）
  4. schema 漂移        —— 库内实际对象 vs alembic 迁移链所建对象
  5. 垂域越界           —— 非垂域目录触碰引擎核心模块（这时引擎抽象有漏）
  6. 核心外部依赖       —— 引擎核心模块 import 外部 keyed API

只读、不改数据。违规逐条点名并给修复指引（照 fd-industry-data conformance_gate 文化）。
"""
from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE_MODULES = {
    "config.py", "db.py", "provenance.py", "ingest.py", "chunking.py",
    "coldstore.py", "embedding.py", "embed_worker.py", "querylib.py", "server.py",
}
CORE_DIR = REPO / "fd_text_core"
BANNED_IMPORTS = {"openai", "anthropic", "requests_oauthlib"}

Violations: list[str] = []


def v(section: str, msg: str) -> None:
    Violations.append(f"[{section}] {msg}")


# ---------- 1. 无损切块 ----------
def check_lossless(sample: int = 20) -> None:
    """抽样已入库文档，重建切块并校验还原。

    覆盖两种正文归处：长文在冷存块（cold_block_key）、短文在 content 列。
    此前只查冷存文档——库里 75 篇里 74 篇是短文，等于门实际只覆盖 1 篇
    （实测「已校验 1 篇」），正是 short-doc-storage 缺陷的同类盲区。
    """
    if not os.environ.get("FDTEXT_DATABASE_URL", "").strip():
        # CI runner 进不了 tailnet：DB 类检查如实标跳过，不硬失败也不假装通过。
        # 全量口径的跑法=网内执行本脚本（垂域验收报告里给的就是网内全绿输出）。
        print("  无损切块: 跳过（无 FDTEXT_DATABASE_URL，离线模式）")
        return
    try:
        sys.path.insert(0, str(REPO))
        from fd_text_core.chunking import verify_lossless
        from fd_text_core.db import fetch_all
        from fd_text_core.verticals.registry import get_strategy

        # 冷存与内联各抽一半，避免长文占比低时覆盖不到短文
        docs = fetch_all(
            "(SELECT id, domain, cold_block_key, content FROM corpus_documents "
            " WHERE status='current' AND cold_block_key IS NOT NULL ORDER BY id DESC LIMIT %s) "
            "UNION ALL "
            "(SELECT id, domain, cold_block_key, content FROM corpus_documents "
            " WHERE status='current' AND content IS NOT NULL ORDER BY id DESC LIMIT %s)",
            (sample, sample),
        )
    except Exception as exc:
        v("无损切块", f"无法连接数据库执行校验：{type(exc).__name__}: {exc}")
        return
    if not docs:
        return
    from fd_text_core import coldstore

    checked = 0
    no_body = 0
    for d in docs:
        if d["cold_block_key"]:
            try:
                text = coldstore.get_block(d["cold_block_key"])
            except Exception as exc:
                v("无损切块", f"doc {d['id']} 冷存读取失败：{exc}")
                continue
        elif d["content"]:
            text = d["content"]
        else:
            no_body += 1
            v("无损切块", f"doc {d['id']} 正文无归处（content 与 cold_block_key 皆空）｜修复=重跑摄取")
            continue
        try:
            rows = fetch_all(
                "SELECT chunk_no, chunk_text, char_start, char_end FROM corpus_chunks "
                "WHERE document_id=%s ORDER BY chunk_no", (d["id"],)
            )
        except Exception as exc:
            v("无损切块", f"doc {d['id']} 读取失败：{exc}")
            continue
        if not rows:
            v("无损切块", f"doc {d['id']} 有正文却无切块（管线缺口）")
            continue

        class _C:  # 轻量适配 verify_lossless
            def __init__(self, r):
                self.chunk_no, self.chunk_text = r["chunk_no"], r["chunk_text"]
                self.char_start, self.char_end = r["char_start"], r["char_end"]

        ok, detail = verify_lossless(text, [_C(r) for r in rows])
        if not ok:
            v("无损切块", f"doc {d['id']} 还原失败：{detail}｜修复=重跑该文档切块并核对策略")
        checked += 1
    print(f"  无损切块: 已校验 {checked} 篇（含冷存与内联两类正文）")


# ---------- 2. provenance ----------
def check_provenance() -> None:
    if not os.environ.get("FDTEXT_DATABASE_URL", "").strip():
        print("  provenance: 跳过（无 FDTEXT_DATABASE_URL，离线模式）")
        return
    try:
        sys.path.insert(0, str(REPO))
        from fd_text_core.db import fetch_all
        bad = fetch_all(
            "SELECT id, provenance FROM corpus_documents WHERE status='current' AND ("
            "  provenance->>'source' IS NULL OR provenance->>'source'=''"
            "  OR provenance->>'stable_id' IS NULL OR provenance->>'stable_id'=''"
            "  OR provenance->>'as_of' IS NULL OR provenance->>'as_of'='')"
        )
    except Exception as exc:
        v("provenance", f"无法执行校验：{type(exc).__name__}: {exc}")
        return
    for r in bad:
        v("provenance", f"doc {r['id']} 三元组缺失（现有={r['provenance']}）｜修复=补 provenance 并重跑摄取")
    print(f"  provenance: {len(bad)} 条违规")


# ---------- 3. 锚点余弦 ----------
def check_anchor() -> None:
    base = os.environ.get("FDTEXT_EMBEDDING_API_BASE", "")
    key = os.environ.get("FDTEXT_EMBEDDING_API_KEY", "")
    if not (base and key):
        print("  锚点余弦: 跳过（未配置嵌入凭据，CI 可不联网）")
        return
    try:
        sys.path.insert(0, str(REPO))
        from fd_text_core import embed_worker as W
        from fd_text_core.db import fetch_one
        from fd_text_core.embedding import Embedder

        if not fetch_one("SELECT 1 FROM anchor_embedding WHERE tag=%s", (W.ANCHOR_TAG,)):
            print("  锚点余弦: 无锚点（首次运行将建立）")
            return
        emb = Embedder(base, key, os.environ.get("FDTEXT_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B"),
                       dimensions=int(os.environ.get("FDTEXT_EMBEDDING_DIM", "1024")))
        sim = W.run_self_check(emb, emb.model)
        print(f"  锚点余弦: {sim:.6f}")
        if sim < W.SIMILARITY_FLOOR:
            v("锚点余弦", f"{sim:.6f} < {W.SIMILARITY_FLOOR}：模型或通道漂移｜修复=确认模型后重建向量或回滚")
    except Exception as exc:
        v("锚点余弦", f"自检异常：{type(exc).__name__}: {exc}")


# ---------- 4. schema 漂移 ----------
def check_schema_drift() -> None:
    """库内对象 vs 迁移链应建对象：多余对象=库外手工改动（漂移违规）。"""
    expected = {
        "corpus_documents", "corpus_chunks", "corpus_chunks_papers",
        "corpus_chunks_screenplay", "embed_tasks", "anchor_embedding", "alembic_version",
    }
    url = os.environ.get("FDTEXT_DATABASE_URL", "")
    if not url:
        print("  schema 漂移: 跳过（无 FDTEXT_DATABASE_URL）")
        return
    try:
        import psycopg

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            actual = {r[0] for r in cur.fetchall()}
    except Exception as exc:
        v("schema 漂移", f"无法读取 schema：{type(exc).__name__}: {exc}")
        return
    extra = sorted(actual - expected)
    missing = sorted(expected - actual)
    for t in extra:
        v("schema 漂移", f"库中表 {t} 不在迁移链内（疑似手工建表）｜修复=补迁移或删除该表")
    for t in missing:
        v("schema 漂移", f"迁移链应有表 {t} 缺失｜修复=alembic upgrade head")
    print(f"  schema 漂移: 库内 {len(actual)} 表，多余 {len(extra)}，缺失 {len(missing)}")


# ---------- 5. 垂域越界 ----------
def check_vertical_boundary(changed: list[str] | None) -> None:
    """垂域改动不得触碰引擎核心模块。

    口径说明：越界检查问的是「**垂域接入这个动作**是否改了引擎核心」。
    `--changed-base` 取区间 diff 会把区间内**并行的独立引擎 change**
    （如 text-cjk-tokenization 改 querylib）也算成越界——实测误报 6 项。
    故支持 `--vertical-commits` 精确指定垂域提交，只查这些提交的改动；
    未指定时退回区间 diff 并打印口径警告（宁可多报也别漏报，但要说清）。
    """
    if changed is None:
        print("  垂域越界: 跳过（未传入改动文件列表）")
        return
    core_rel = {f"fd_text_core/{m}" for m in CORE_MODULES}
    offenders = [f for f in changed if f in core_rel]
    for f in offenders:
        v("垂域越界", f"{f} 是引擎核心模块，垂域 change 不得修改｜修复=先补引擎抽象再以配置接入")


def vertical_commit_files(commits: list[str]) -> list[str]:
    """取指定提交触碰的文件（精确口径：垂域提交的改动面）。"""
    files: list[str] = []
    for c in commits:
        out = subprocess.run(
            ["git", "show", "--name-only", "--format=", c], cwd=REPO,
            capture_output=True, text=True, timeout=30,
        )
        files.extend(l for l in out.stdout.splitlines() if l.strip())
    return files


# ---------- 6. 核心外部依赖 ----------
def check_core_imports() -> None:
    """引擎核心模块不得 import 外部 keyed API 客户端。"""
    banned_dir = CORE_DIR  # 核心模块直接在包根，垂域在 verticals/ 子包
    for py in banned_dir.glob("*.py"):
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError as exc:
            v("核心依赖", f"{py.name} 语法错误：{exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for n in names:
                if n in BANNED_IMPORTS:
                    v("核心依赖", f"{py.name} 引入外部 keyed 客户端 {n}｜修复=外部依赖移入 verticals/*/connector")


def _changed_files(base_ref: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", base_ref], cwd=REPO,
            capture_output=True, text=True, timeout=30,
        )
        return [l for l in out.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def cli() -> int:
    ap = argparse.ArgumentParser(description="fd-text-core 只读质量门")
    ap.add_argument("--changed-base", default=os.environ.get("GATE_BASE_REF", ""),
                    help="用于垂域越界检查的 git 基线（区间口径；多 change 并行时会误报，见 check_vertical_boundary）")
    ap.add_argument("--vertical-commits", default=os.environ.get("GATE_VERTICAL_COMMITS", ""),
                    help="垂域提交 sha（逗号分隔）——精确口径，只查这些提交是否触碰引擎核心")
    args = ap.parse_args()

    print("fd-text-core conformance gate (read-only)")
    check_lossless()
    check_provenance()
    check_anchor()
    check_schema_drift()
    if args.vertical_commits:
        commits = [c.strip() for c in args.vertical_commits.split(",") if c.strip()]
        files = vertical_commit_files(commits)
        print(f"  垂域越界: 精确口径（{len(commits)} 个垂域提交，{len(files)} 个文件）")
        check_vertical_boundary(files)
    elif args.changed_base:
        print("  垂域越界: 区间口径（多 change 并行时会把独立引擎 change 误报为越界）")
        check_vertical_boundary(_changed_files(args.changed_base))
    else:
        check_vertical_boundary(None)
    check_core_imports()

    if Violations:
        print(f"\n❌ {len(Violations)} 项违规：")
        for x in Violations:
            print("  -", x)
        return 1
    print("\n✅ conformance gate 全绿")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
