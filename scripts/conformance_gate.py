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
import re
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

# 检查结果登记表：(检查项, 状态, 说明)
# 状态三值互斥且完备：PASS（实跑且通过）/ FAIL（实跑且不通过）/ SKIPPED（未跑到）
# SKIPPED 永不计入通过数——这是本质量门可信度的核心契约。
Results: list[tuple[str, str, str]] = []

# 每项检查所缺的环境变量与网内补跑方式（用于运行末尾的未覆盖清单）
HINTS = {
    "无损切块": ("FDTEXT_DATABASE_URL", "网内（可连 fd_text 的主机）执行：python scripts/conformance_gate.py --strict"),
    "provenance": ("FDTEXT_DATABASE_URL", "同上"),
    "锚点余弦": ("FDTEXT_EMBEDDING_API_BASE + FDTEXT_EMBEDDING_API_KEY", "同上，并注入嵌入凭据"),
    "schema 漂移": ("FDTEXT_DATABASE_URL", "同上"),
    "垂域越界": ("--changed-base 或 --vertical-commits", "传入改动文件列表或垂域提交 sha"),
    "语料对账": ("FDTEXT_DATABASE_URL", "同上（连库后比对 loader 清单与库内 current 集）"),
    "检索评测": ("--eval-domain <域> 或 --eval-all（+ FDTEXT_EMBEDDING_API_*）",
                 "同上，并注入嵌入凭据；或 python3 scripts/eval_retrieval.py --all --store"),
}


def v(section: str, msg: str) -> None:
    Violations.append(f"[{section}] {msg}")


def rec(section: str, status: str, detail: str = "") -> None:
    """登记一项检查结果。status 必须是 PASS/FAIL/SKIPPED 之一。"""
    Results.append((section, status, detail))


def _observed(section: str) -> bool:
    """本次执行到现在是否已有该检查项的结果登记（用于防止重复登记）。"""
    return any(name == section for name, _, _ in Results)


def _failed(section: str) -> int:
    return sum(1 for x in Violations if x.startswith(f"[{section}]"))


def _settle(section: str, detail: str) -> None:
    """实跑完毕后按该检查项是否产生违规登记 PASS 或 FAIL。"""
    if _observed(section):
        return
    n = _failed(section)
    rec(section, "FAIL" if n else "PASS", f"{n} 项违规；{detail}" if n else detail)


def skip(section: str, missing: str) -> None:
    rec(section, "SKIPPED", f"缺 {missing}")


def _print_result(section: str) -> None:
    """把已登记的结果按统一格式输出；未登记的兜底为 SKIPPED。"""
    for name, status, detail in Results:
        if name != section:
            continue
        if detail:
            print(f"  {name}: {status}（{detail}）")
        else:
            print(f"  {name}: {status}")
        return
    hint = HINTS.get(section, ("（见 GATE 文档）", ""))
    print(f"  {section}: SKIPPED（缺 {hint[0]}）")


def _print_result_with_detail(section: str, observed_detail: str) -> None:
    """检查实跑到并已产生观测数据：按有无违规判定 PASS/FAIL 后连同观测数据输出。"""
    if not _observed(section):
        n = _failed(section)
        rec(section, "FAIL" if n else "PASS", observed_detail)
    for name, status, detail in Results:
        if name != section:
            continue
        extra = f"（{detail}）" if detail else ""
        print(f"  {name}: {status}{extra}")
        return


# ---------- 1. 无损切块 ----------
def check_lossless(sample: int = 20) -> None:
    """抽样已入库文档，重建切块并校验还原。

    覆盖两种正文归处：长文在冷存块（cold_block_key）、短文在 content 列。
    此前只查冷存文档——库里 75 篇里 74 篇是短文，等于门实际只覆盖 1 篇
    （实测「已校验 1 篇」），正是 short-doc-storage 缺陷的同类盲区。
    """
    if not os.environ.get("FDTEXT_DATABASE_URL", "").strip():
        # CI runner 进不了 tailnet：DB 类检查如实登记 SKIPPED，不硬失败也不假装通过。
        # 全量口径的跑法=网内执行本脚本 --strict（垂域验收报告里给的就是网内全绿输出）。
        skip("无损切块", "FDTEXT_DATABASE_URL")
        return
    try:
        sys.path.insert(0, str(REPO))
        from fd_text_core.chunking import verify_lossless
        from fd_text_core.db import fetch_all

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
        rec("无损切块", "FAIL", f"连接异常：{type(exc).__name__}")
        return
    if not docs:
        _settle("无损切块", "库内无 current 文档")
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
    _print_result_with_detail("无损切块", f"已校验 {checked} 篇（含冷存与内联两类正文）")


# ---------- 2. provenance ----------
def check_provenance() -> None:
    if not os.environ.get("FDTEXT_DATABASE_URL", "").strip():
        skip("provenance", "FDTEXT_DATABASE_URL")
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
        rec("provenance", "FAIL", f"连接异常：{type(exc).__name__}")
        return
    for r in bad:
        v("provenance", f"doc {r['id']} 三元组缺失（现有={r['provenance']}）｜修复=补 provenance 并重跑摄取")
    _print_result_with_detail("provenance", f"{len(bad)} 条违规")


# ---------- 3. 锚点余弦 ----------
def check_anchor() -> None:
    base = os.environ.get("FDTEXT_EMBEDDING_API_BASE", "")
    key = os.environ.get("FDTEXT_EMBEDDING_API_KEY", "")
    if not (base and key):
        skip("锚点余弦", "FDTEXT_EMBEDDING_API_BASE + FDTEXT_EMBEDDING_API_KEY")
        return
    try:
        sys.path.insert(0, str(REPO))
        from fd_text_core import embed_worker as W
        from fd_text_core.db import fetch_one
        from fd_text_core.embedding import Embedder, provider_from_env

        if not fetch_one("SELECT 1 FROM anchor_embedding WHERE tag=%s", (W.ANCHOR_TAG,)):
            rec("锚点余弦", "SKIPPED", "库内无锚点（首次运行将建立）")
            _print_result("锚点余弦")
            return
        emb = Embedder(base, key, os.environ.get("FDTEXT_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B"),
                       dimensions=int(os.environ.get("FDTEXT_EMBEDDING_DIM", "1024")),
                       provider=provider_from_env())
        sim = W.run_self_check(emb, emb.model)
        if sim < W.SIMILARITY_FLOOR:
            v("锚点余弦", f"{sim:.6f} < {W.SIMILARITY_FLOOR}：模型或通道漂移｜修复=确认模型后重建向量或回滚")
        _print_result_with_detail("锚点余弦", f"{sim:.6f}")
    except Exception as exc:
        v("锚点余弦", f"自检异常：{type(exc).__name__}: {exc}")
        # 自检跑到底且 sim 已算出时 _print_result_with_detail 已登记结果，不重复登记
        if not _observed("锚点余弦"):
            rec("锚点余弦", "FAIL", f"自检异常：{type(exc).__name__}")
            _print_result("锚点余弦")


def _declared_index_names() -> set[str]:
    """迁移文件中出现过的全部索引名（含分区自动派生的名字形态）。

    索引名在分区表上会由 PG 自动派生（母表索引 ix -> 分区索引 <分区名>_<后缀>），
    迁移文件里只写母表那一个。故判据放宽为「索引名或其去掉表名前缀后的后缀
    能匹配迁移中出现过的名字」，避免把 PG 自动建的子索引误判为手工索引。
    """
    versions = REPO / "fd_text_core" / "migrations" / "versions"
    names: set[str] = set()
    if not versions.is_dir():
        return names
    for f in versions.glob("*.py"):
        text = f.read_text()
        # 形如 CREATE INDEX [IF NOT EXISTS] name / CREATE INDEX IF NOT EXISTS "name"
        for m in re.finditer(
            r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?["\']?([A-Za-z_]\w*)',
            text,
        ):
            names.add(m.group(1))
        # 变量名形式的索引名（如 INDEX_NAME = "ix_chunks_embedding_hnsw"）
        for m in re.finditer(
            r'^\s*(?:INDEX_NAME|[A-Z_]*NAME)\s*=\s*["\']([A-Za-z_]\w*)["\']', text, re.M
        ):
            names.add(m.group(1))
        # f-string / 变量拼接的名字（如 f"{part}_embedding_hnsw"）取后缀常量兜底
        for m in re.finditer(r'["\']([A-Za-z_]*hnsw[A-Za-z_]*)["\']', text, re.IGNORECASE):
            names.add(m.group(1))
        # 约束携带的索引：CONSTRAINT name UNIQUE/PRIMARY KEY 由 PG 自行建索引
        for m in re.finditer(r'CONSTRAINT\s+["\']?([A-Za-z_]\w*)', text):
            names.add(m.group(1))
        for m in re.finditer(r'["\'](uq_[A-Za-z_]\w*)["\']', text):
            names.add(m.group(1))
    # alembic 自建的版本表索引（不在本仓迁移链内，属工具自有对象，非人工漂移）
    names.add("alembic_version_pkc")
    # 剔除正则误吞到的 SQL 关键字
    names -= {"IF", "NOT", "EXISTS", "CONCURRENTLY", "ONLY"}
    return names


def _inherited_index_names(url: str) -> set[str]:
    """分区表索引派生出来的子索引名（pg_inherits 记录的真实血缘）。

    在母表建索引后，PG 为每个分区自动建子索引并 attach。这些索引名形态不固定
    （PG 可能沿用已存在的同名/近名索引），靠名字无法可靠判定，故直接查血缘。
    """
    try:
        import psycopg

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE c.relkind = 'i'"
            )
            return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def _index_explained(idx_name: str, declared: set[str]) -> bool:
    """该索引名能否由迁移链解释。

    三种合法形态：
      1. 迁移里直接写的名字（ix_chunks_tsv 等）
      2. PG 自动为主键/唯一约束生成的（<表>_pkey / <表>_<列>_key）
      3. 分区表索引：母表索引在分区上派生的子索引，形态为 <分区名>_<后缀>，
         其后缀与母表索引名去掉 ix_/表名前缀后的部分对应
    """
    if idx_name in declared:
        return True
    # 约束类自动索引（主键 / 唯一约束）
    if idx_name.endswith("_pkey") or idx_name.endswith("_key"):
        return True
    # 分区索引由母表索引自动派生：名字形如 <分区表名>_<母表索引后缀>。
    # 迁移里只声明母表索引名（ix_chunks_tsv / ix_chunks_embedding_hnsw 等），
    # PG 在各分区上生成 corpus_chunks_papers_tsv_idx 这类派生名——不可判为手工。
    # 取法：剥掉分区表名前缀 corpus_chunks_<domain>_，看剩余后缀是否在迁移里出现过
    # （迁移名去掉 ix_ 前缀后比较，因派生名不带 ix_）。
    # 名字匹配对分区派生索引不可靠（母表 ix_chunks_tsv 在分区上可能派生
    # <分区>_tsv_idx，去掉 _idx 后再比也只是 tsv；而 embedding_idx 对应
    # embedding_hnsw 根本同名不上）。故只做宽松兜底：索引名含迁移声明名的核心段。
    cores: set[str] = set()
    for d in declared:
        name = d[3:] if d.startswith("ix_") else d
        for tbl in ("chunks_", "corpus_documents_", "embed_tasks_", "corpus_chunks_"):
            if name.startswith(tbl):
                name = name[len(tbl):]
        if name:
            cores.add(name)
    cores |= declared
    low = idx_name.lower()
    return any(c.lower() in low for c in cores if len(c) >= 3)


# ---------- 4. schema 漂移 ----------
def _declared_table_names() -> set[str]:
    """迁移文件中声明过的全部表名（静态解析，含 DO $$ 块与 f-string 分区名）。

    为什么从迁移链推导而不是硬编码清单：硬编码清单**每加一个垂域分区都要改
    质量门**，漏改的表现是「门报库内表多余（疑似手工建表）」——把正常的分区
    迁移误判成漂移（本次接入 gov 时实测踩到：corpus_chunks_gov 被判多余）。
    门的职责是「库与迁移链是否一致」，故两侧都该从迁移链推导。

    解析面覆盖三种写法（正则 DOTALL 以容忍 DO $$ 块内的缩进换行）：
      · 直接 `CREATE TABLE [IF NOT EXISTS] name`
      · `DO $$ ... CREATE TABLE name PARTITION OF ... $$`（分区迁移的标准写法）
      · `CREATE TABLE corpus_chunks_{dom}` + `INITIAL_DOMAINS = ("papers", ...)`
        （0001 用 f-string 批量建初始分区，静态解析必须展开该循环）
    另补 alembic 自建的版本表：它不由迁移文件创建，但库内必有。
    """
    versions = REPO / "fd_text_core" / "migrations" / "versions"
    names: set[str] = {"alembic_version"}  # alembic 自建，不在迁移文件里
    if not versions.is_dir():
        return names
    for f in versions.glob("*.py"):
        text = f.read_text()
        for m in re.finditer(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["\']?([A-Za-z_]\w*)',
            text,
            re.IGNORECASE | re.DOTALL,
        ):
            names.add(m.group(1))
        # f-string 模板 + 域元组：展开 `for dom in INITIAL_DOMAINS:` 这类批量分区声明。
        # 两步走：先由 f-string 里的 {var} 找到所属循环 `for <var> in <TUPLE>:`，
        # 再展开该元组。直接拿 {var} 当元组名找是错的——{dom} 是循环变量，
        # 元组名在 `for ... in INITIAL_DOMAINS` 里（实测踩到：漏展开 papers 分区）。
        for m in re.finditer(
            r'CREATE\s+TABLE\s+["\']?([A-Za-z_]+_)\{(\w+)\}',
            text,
            re.IGNORECASE | re.DOTALL,
        ):
            prefix, var = m.group(1), m.group(2)
            loop = re.search(rf'for\s+{var}\s+in\s+([A-Za-z_]\w*)\s*:', text)
            if not loop:
                continue
            tup = re.search(rf'{loop.group(1)}\s*=\s*\(([^)]*)\)', text)
            if not tup:
                continue
            for item in re.findall(r'["\']([^"\']+)["\']', tup.group(1)):
                names.add(f"{prefix}{item}")
    # 清掉未展开的模板名残留（形如 `corpus_chunks_`）——它不是真实表名，
    # 留着会让「多余表」判定漏报一个真漂移。
    return {n for n in names if not n.endswith("_")}


def check_schema_drift() -> None:
    """库内对象 vs 迁移链应建对象：多余对象=库外手工改动（漂移违规）。

    期望表集从迁移链推导（见 `_declared_table_names`），故加垂域分区只需加迁移，
    不必再改本门——否则门会成为「每接一个垂域就要改一次」的易漏点。
    """
    expected = _declared_table_names()
    url = os.environ.get("FDTEXT_DATABASE_URL", "")
    if not url:
        skip("schema 漂移", "FDTEXT_DATABASE_URL")
        return
    try:
        import psycopg

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            actual = {r[0] for r in cur.fetchall()}
            # 索引同样纳入漂移面：库外手工建的索引此前完全不被检查，
            # 正是本 change 发现的重复 HNSW 索引那类债（见 tasks 2.x 记录）。
            cur.execute(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind='i' AND n.nspname='public'"
            )
            actual_idx = {r[0] for r in cur.fetchall()}
    except Exception as exc:
        v("schema 漂移", f"无法读取 schema：{type(exc).__name__}: {exc}")
        rec("schema 漂移", "FAIL", f"连接异常：{type(exc).__name__}")
        return
    extra = sorted(actual - expected)
    missing = sorted(expected - actual)
    for t in extra:
        v("schema 漂移", f"库中表 {t} 不在迁移链内（疑似手工建表）｜修复=补迁移或删除该表")
    for t in missing:
        v("schema 漂移", f"迁移链应有表 {t} 缺失｜修复=alembic upgrade head")

    # 索引漂移判据（双轨，任一满足即为已解释）：
    #  A. 血缘轨（权威）：该索引是某个母表索引在分区上的派生子索引（pg_inherits
    #     记录），而母表索引本身在迁移链内 -> PG 自动行为，不是手工建。
    #  B. 名字轨（兜底）：索引名含迁移声明名的核心段。
    # 只建母表索引是正确写法（见 0004 注释），故派生子索引必须被判为合法。
    declared = _declared_index_names()
    inherited = _inherited_index_names(url)
    orphan_idx = sorted(
        i for i in actual_idx
        if not (i in inherited or _index_explained(i, declared))
    )
    for i in orphan_idx:
        v("schema 漂移",
          f"库内索引 {i} 未出现在任何迁移文件中（疑似库外手工建索引）"
          "｜修复=补迁移纳入该索引，或确认冗余后 DROP")
    _print_result_with_detail(
        "schema 漂移",
        f"库内 {len(actual)} 表（多余 {len(extra)}，缺失 {len(missing)}）、"
        f"{len(actual_idx)} 索引（未在迁移链的 {len(orphan_idx)} 个）",
    )


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
        skip("垂域越界", "--changed-base 或 --vertical-commits")
        _print_result("垂域越界")
        return
    if not changed:
        skip("垂域越界", "改动文件列表为空（无提交区间可查）")
        _print_result("垂域越界")
        return
    core_rel = {f"fd_text_core/{m}" for m in CORE_MODULES}
    offenders = [f for f in changed if f in core_rel]
    for f in offenders:
        v("垂域越界", f"{f} 是引擎核心模块，垂域 change 不得修改｜修复=先补引擎抽象再以配置接入")
    _print_result_with_detail("垂域越界", f"{len(changed)} 个改动文件")


def vertical_commit_files(commits: list[str]) -> list[str]:
    """取指定提交触碰的文件（精确口径：垂域提交的改动面）。"""
    files: list[str] = []
    for c in commits:
        out = subprocess.run(
            ["git", "show", "--name-only", "--format=", c], cwd=REPO,
            capture_output=True, text=True, timeout=30,
        )
        files.extend(line for line in out.stdout.splitlines() if line.strip())
    return files


# ---------- 7. 检索评测（可选第八项） ----------
def check_retrieval_eval(domain: str | None, k: int = 10) -> None:
    """检索评测：跑分器在标注集上的验收线（混合不低于任一单通道）。

    **为什么默认关、必须显式开启**：本项比既有诸项重得多（N 条查询 × 3 通道，
    每条向量查询一次嵌入调用），把它默认塞进门会让每次 CI 都慢且需要嵌入凭据。
    但一旦开启，缺标注集或缺嵌入凭据即按 SKIPPED 如实报告并按严格模式失败
    ——沿既有三值口径，不新造语义（见 change text-retrieval-eval 的 design D4）。
    """
    if not domain:
        return  # 未开启：不出现在结果里（保持既有七项的行为完全不变）
    try:
        from fd_text_core.eval import DatasetError, load_dataset
        from fd_text_core.eval import runner, store
    except Exception as exc:  # eval 包不可导入（异常安装）：如实报 FAIL
        v("检索评测", f"评测模块不可导入：{type(exc).__name__}: {exc}")
        rec("检索评测", "FAIL", f"导入异常：{type(exc).__name__}")
        return

    try:
        ds = load_dataset(domain)
    except DatasetError as exc:
        v("检索评测", f"垂域 {domain} 标注集不可用：{exc}")
        skip("检索评测", f"垂域 {domain} 的标注集（见上方说明）")
        return

    from fd_text_core.embedding import embedder_from_env

    emb = embedder_from_env()
    if emb is None:
        skip("检索评测", "FDTEXT_EMBEDDING_API_BASE + FDTEXT_EMBEDDING_API_KEY（向量通道需要）")
        return

    try:
        rep = runner.run_eval(ds, k=k, embedder=emb, check_expects=store.check_expects_exist)
    except Exception as exc:
        v("检索评测", f"跑分异常：{type(exc).__name__}: {exc}")
        rec("检索评测", "FAIL", f"跑分异常：{type(exc).__name__}")
        return

    if rep.missing_expects:
        v("检索评测",
          f"标注集有 {len(rep.missing_expects)} 条期望命中文档在库内缺失"
          "｜修复=更新标注集（语料重建会使旧 id 失效）")
    if rep.degraded:
        # 向量通道降级 -> 验收线不可判定：如实 SKIPPED 而非给通过判定
        v("检索评测", f"向量通道降级：{rep.degraded}")
        skip("检索评测", "可用的嵌入通道（当前降级，验收线不可判定）")
        return

    verdict = rep.hybrid_not_worse
    hyb = rep.summary.get("hybrid", {})
    kw = rep.summary.get("keyword", {})
    vec = rep.summary.get("vector", {})
    detail = (f"{domain} k={k}：hybrid {hyb.get('hit@k')}/MRR {hyb.get('mrr')} vs "
              f"keyword {kw.get('hit@k')}/{kw.get('mrr')} vs vector {vec.get('hit@k')}/{vec.get('mrr')}")
    if verdict and not rep.missing_expects:
        rec("检索评测", "PASS", detail)
    else:
        if not verdict:
            v("检索评测", f"混合通道未达验收线（应不低于任一单通道）：{detail}")
        rec("检索评测", "FAIL", detail)
    _print_result_with_detail("检索评测", detail)


# ---------- 6. 核心外部依赖 ----------
# ---------- 5b. 语料对账 ----------
# 守门对象：loader 脚本里的采集清单（SOURCES）与库内 current 集是否一致。
# 为什么需要：2026-10-13 实测撞到漂移——load_screenplay.py 的 SOURCES 与库内
# current 集不一致（Gutenberg 版本换过 + 两条清单漏列），意味着「重跑 loader」
# 会产出与现有库不同的结果，而这件事在此之前没有任何检查会发现。
# 判据：脚本 SOURCES 的 (source, source_id) 集合 SHALL 等于库内 current 集。
LOADERS = (
    # (loader 脚本, domain, 采集函数名前缀 -> source 名)
    (
        "scripts/load_screenplay.py",
        "screenplay",
        {"fetch_gutenberg": "gutenberg", "fetch_wikisource": "wikisource", "fetch_xikao": "xikao"},
    ),
)


def _loader_sources(path: str) -> set[tuple[str, str]]:
    """从 loader 脚本静态解析 SOURCES 的 (source, source_id) 集合。

    用正则而非 import：import 会拉起采集依赖与网络配置，门必须离线可跑。
    """
    text = (REPO / path).read_text()
    start = text.index("SOURCES = [")
    block = text[start : text.index("\n]", start)]
    kind_map: dict[str, str] = {}
    for loader, _domain, names in LOADERS:
        if loader == path:
            kind_map = names
    out: set[tuple[str, str]] = set()
    for kind, arg in re.findall(r'\(S\.(fetch_\w+),\s*"([^"]+)"', block):
        src = kind_map.get(kind)
        if src:
            out.add((src, arg))
    return out


def check_corpus_reconcile() -> None:
    url = os.environ.get("FDTEXT_DATABASE_URL", "").strip()
    if not url:
        skip("语料对账", "FDTEXT_DATABASE_URL")
        return
    try:
        import psycopg

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            total_script = 0
            total_db = 0
            for loader, domain, _names in LOADERS:
                script = _loader_sources(loader)
                cur.execute(
                    "SELECT source, source_id FROM corpus_documents "
                    "WHERE domain=%s AND status='current'",
                    (domain,),
                )
                db = {(r[0], r[1]) for r in cur.fetchall()}
                total_script += len(script)
                total_db += len(db)
                for src, sid in sorted(script - db):
                    v("语料对账",
                      f"{loader} 声明 {src}:{sid} 但库内无对应 current 行"
                      "｜修复=重跑该 loader 或修正 SOURCES")
                for src, sid in sorted(db - script):
                    v("语料对账",
                      f"库内 current 有 {src}:{sid}（domain={domain}）但 {loader} 未声明"
                      "｜修复=把该条补进 loader 的 SOURCES（库是权威）")
    except Exception as exc:
        v("语料对账", f"无法执行对账：{type(exc).__name__}: {exc}")
        rec("语料对账", "FAIL", f"连接异常：{type(exc).__name__}")
        return
    _print_result_with_detail(
        "语料对账", f"loader 声明 {total_script} 条 / 库内 current {total_db} 部"
    )


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
    _print_result_with_detail("核心依赖", f"扫描 {len(list(banned_dir.glob('*.py')))} 个核心模块")


def _changed_files(base_ref: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", base_ref], cwd=REPO,
            capture_output=True, text=True, timeout=30,
        )
        return [line for line in out.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def cli() -> int:
    ap = argparse.ArgumentParser(description="fd-text-core 只读质量门")
    ap.add_argument("--changed-base", default=os.environ.get("GATE_BASE_REF", ""),
                    help="用于垂域越界检查的 git 基线（区间口径；多 change 并行时会误报，见 check_vertical_boundary）")
    ap.add_argument("--vertical-commits", default=os.environ.get("GATE_VERTICAL_COMMITS", ""),
                    help="垂域提交 sha（逗号分隔）——精确口径，只查这些提交是否触碰引擎核心")
    ap.add_argument("--eval-domain", default=os.environ.get("GATE_EVAL_DOMAIN", ""),
                    help="开启检索评测检查项并指定垂域（默认关；--eval-all 则跑全部已交付标注集）")
    ap.add_argument("--eval-all", action="store_true",
                    help="开启检索评测并覆盖包内全部已交付标注集")
    ap.add_argument("--strict", action="store_true",
                    help="严格模式：任一 SKIPPED 或 FAIL 均导致非零退出码（垂域验收唯一合法口径）")
    args = ap.parse_args()

    print("fd-text-core conformance gate (read-only)" + (" [strict]" if args.strict else ""))
    if args.vertical_commits:
        commits = [c.strip() for c in args.vertical_commits.split(",") if c.strip()]
        print(f"（垂域越界取精确口径：{len(commits)} 个垂域提交）")
        boundary_files: list[str] | None = vertical_commit_files(commits)
    elif args.changed_base:
        print("（垂域越界取区间口径：多 change 并行时可能误报为越界）")
        boundary_files = _changed_files(args.changed_base)
    else:
        boundary_files = None

    check_lossless()
    check_provenance()
    check_anchor()
    check_schema_drift()
    check_corpus_reconcile()
    check_vertical_boundary(boundary_files)
    check_core_imports()
    # 第八项：检索评测（可选，默认关——见 check_retrieval_eval 的说明）
    if getattr(args, "eval_all", False):
        try:
            from fd_text_core.eval import list_datasets
            for _d in list_datasets():
                check_retrieval_eval(_d)
        except Exception as exc:
            v("检索评测", f"枚举标注集失败：{type(exc).__name__}: {exc}")
            rec("检索评测", "FAIL", f"枚举异常：{type(exc).__name__}")
    else:
        check_retrieval_eval(getattr(args, "eval_domain", "") or None)

    passed = [n for n, s, _ in Results if s == "PASS"]
    failed = [n for n, s, _ in Results if s == "FAIL"]
    skipped = [n for n, s, _ in Results if s == "SKIPPED"]

    print("\n—— 汇总 ——")
    print(f"  通过 {len(passed)} 项：{'、'.join(passed) if passed else '（无）'}")
    print(f"  失败 {len(failed)} 项：{'、'.join(failed) if failed else '（无）'}")
    print(f"  跳过 {len(skipped)} 项：{'、'.join(skipped) if skipped else '（无）'}")

    if skipped:
        print("\n—— 未覆盖清单（本环境未实跑，不得以任何形式视为通过）——")
        for n in skipped:
            missing, how = HINTS.get(n, ("未知", "见 GATE 文档"))
            # 优先用本次实登记的原因：静态 HINTS 是「该项一般缺什么」，而本次可能是
            # 另一种缺法（如评测项开了 --eval-domain 却缺嵌入凭据）——此时报
            # 「缺 --eval-domain」会把人引向错误的修复方向（实测踩到）。
            detail = next((d for name, st, d in Results if name == n and st == "SKIPPED"), "")
            print(f"  · {n}：{detail or f'缺 {missing}'}")
            print(f"    补跑：{how}")

    if Violations:
        print(f"\n❌ {len(Violations)} 项违规：")
        for x in Violations:
            print("  -", x)

    if args.strict and (failed or skipped):
        bad_items = failed + skipped
        print(f"\n❌ strict 模式失败：{len(bad_items)} 项未通过（失败 {len(failed)} / 跳过 {len(skipped)}）")
        print("   垂域验收只认 strict 全绿；请按上方未覆盖清单补齐连接后在网内重跑。")
        return 1
    if Violations:
        return 1
    if skipped:
        print("\n⚠️  本次有未覆盖检查项——该结果不等于垂域验收所用的 strict 全绿。")
    print("\n✅ conformance gate 全绿（本次已实跑项全部通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
