"""剧本域解析器：中西两种形态映射到同一结构契约。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fd_text_core.chunking import SlidingWindow, chunk_text, verify_lossless  # noqa: E402
from fd_text_core.verticals.registry import get, list_verticals  # noqa: E402
from fd_text_core.verticals.screenplay.parser import parse_play  # noqa: E402

WEST = """HAMLET
ACT I
SCENE 1. Elsinore. A platform before the castle.
(Enter BERNARDO and FRANCISCO, two sentinels.)
BERNARDO: Who's there?
FRANCISCO: Nay, answer me. Stand and unfold yourself.
ACT II
SCENE 2. A room of state.
KING: Welcome, dear Rosencrantz.
"""

CN = """竇娥冤
第一折
（旦扮窦娥上，云）
妾身窦娥，三岁上亡了母亲。
（卜儿蔡婆上，云）
媳妇儿，你今日怎生这般烦恼？
（末扮张驴儿上，唱）
我今日便要娶她。
"""


def test_western_act_scene_hierarchy():
    p = parse_play(WEST)
    assert p.title == "HAMLET"
    paths = [h for h, _ in p.section_texts()]
    assert paths == ["screenplay>ACT I>SCENE 1", "screenplay>ACT II>SCENE 2"], paths
    assert "BERNARDO" in p.characters and "KING" in p.characters


def test_chinese_act_and_roles():
    p = parse_play(CN)
    assert p.title == "竇娥冤"
    paths = [h for h, _ in p.section_texts()]
    assert paths == ["screenplay>第一折>scene"], paths
    # 说话人须剥离「上/云/唱」等动作字与标点
    assert "窦娥" in p.characters
    assert "卜儿蔡婆" in p.characters
    assert "张驴儿" in p.characters
    assert not any(c.endswith("上") or c.endswith("，") for c in p.characters), p.characters


def test_stage_directions_extracted_not_mixed_into_lines():
    p = parse_play(WEST)
    stages = [s for sc in p.scenes for ln in sc.lines for s in ln.stage]
    assert any("Enter BERNARDO" in s for s in stages)
    # 舞台提示不得残留在线正文里
    joined = " ".join(t for _, t in p.section_texts())
    assert "(" not in joined and "（" not in joined


def test_both_forms_share_one_contract():
    """西方与古典必须产出同构 section_path（screenplay>act>scene 三段）。"""
    for text in (WEST, CN):
        for path, _ in parse_play(text).section_texts():
            assert path.startswith("screenplay>")
            assert path.count(">") == 2, path


def test_empty_input_degrades_not_crash():
    p = parse_play("")
    assert p.scenes == [] and p.characters == []
    assert p.section_texts() == []


def test_vertical_registered_with_facets():
    assert "screenplay" in list_verticals()
    v = get("screenplay")
    assert v.display == "剧本"
    assert v.facets["tradition"] and v.facets["language"] and v.facets["period"]


def test_screenplay_chunks_satisfy_lossless_invariant():
    """中英两例经垂域策略切块后都必须能逐字符还原。"""
    strategy = get("screenplay").strategy
    for name, text in (("west", WEST), ("cn", CN)):
        chunks = chunk_text(text, strategy)
        ok, detail = verify_lossless(text, chunks)
        assert ok, f"{name}: {detail}"


def test_unknown_text_still_parses_as_single_scene():
    p = parse_play("随便一段没有剧本结构的文字。\n第二行。")
    assert len(p.section_texts()) == 1
