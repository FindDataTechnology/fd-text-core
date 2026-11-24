"""backfill_facets 通用回填脚本的测试（change: unify-facet-projection-at-source task 5.1/5.2）。

不起真库：用一个假的 db 模块注入，验证脚本「只发 UPDATE metadata、不走 ingest、
按域取投影、冷存文档不被误算」。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_script(monkeypatch, rows):
    """把 backfill_facets.py 作为模块加载，并桩掉 db 的 fetch_all/execute。"""
    calls = {"execute": []}

    def fake_fetch_all(sql, params=None):
        return rows

    def fake_execute(sql, params=None):
        calls["execute"].append((sql, params))

    import fd_text_core.db as dbmod

    monkeypatch.setattr(dbmod, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(dbmod, "execute", fake_execute)

    spec = importlib.util.spec_from_file_location("backfill_facets", REPO / "scripts" / "backfill_facets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, calls


def test_gov_backfill_updates_only_metadata(monkeypatch, capsys):
    row = {
        "id": 1,
        "title": "财政部关于印发办法的通知",
        "content": "正文\n财政部\n2021年1月1日",
        "cold_block_key": None,
        "metadata": {},  # 无 facet
        "source": "govcn",
        "source_id": "u1",
        "published": "2021-01-01",
    }
    mod, calls = _load_script(monkeypatch, [row])
    monkeypatch.setattr(sys, "argv", ["backfill_facets", "--domain", "gov"])
    assert mod.main() == 0
    # 只发 UPDATE metadata 语句，绝不碰 content/chunk/embedding
    assert len(calls["execute"]) == 1
    sql = calls["execute"][0][0]
    assert "UPDATE corpus_documents SET metadata" in sql
    assert "content" not in sql.replace("metadata", "")
    assert "corpus_chunks" not in sql and "embed_tasks" not in sql


def test_cold_store_row_has_body_true_not_wrong_false(monkeypatch):
    row = {
        "id": 2,
        "title": "某公告",
        "content": None,  # 冷存：正文在 S3，行里没有
        "cold_block_key": "blk",
        "metadata": {"source_type": "announcement", "lang": "zh", "has_body": "true"},
        "source": "cninfo",
        "source_id": "u2",
        "published": "2020-01-01",
    }
    mod, calls = _load_script(monkeypatch, [row])
    monkeypatch.setattr(sys, "argv", ["backfill_facets", "--domain", "news"])
    assert mod.main() == 0
    # 冷存行投影判 has_body=true，与现值一致 → 零变更（不会误判 false 而多写一次）
    assert len(calls["execute"]) == 0


def test_domain_without_projector_errors(monkeypatch, capsys):
    mod, _ = _load_script(monkeypatch, [])
    monkeypatch.setattr(sys, "argv", ["backfill_facets", "--domain", "papers"])
    assert mod.main() == 2
    assert "没有 facet_projector" in capsys.readouterr().out
