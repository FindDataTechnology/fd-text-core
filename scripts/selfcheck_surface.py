#!/usr/bin/env python3
"""tool-surface manifest 双向 selfcheck（mcp-surface-contract）。

断言两个方向：
  1. 一致：manifest 声明的工具 == 引擎注册的工具（新增/删除工具而 manifest
     未同步即失败，反向同理）
  2. 闭合：任何消费侧暴露的工具集 ⊆ manifest 的 commercial 集
     ——消费侧由 --consumers 传入的 pack/套件目录提供

代码注册面取 `mcp.list_tools()`（运行时真值，优先于 grep 码点）。

本 server 的特殊性：工具名按垂域动态生成（`<domain>_<verb>`），注册表内容
取决于已加载垂域。故一致性检查按「已加载垂域 × 动词集」推导期望工具面，
而非写死清单——这样新增垂域（第三个垂域接入）时 manifest 不同步会立刻失败。

用法：
    python3 scripts/selfcheck_surface.py                    # 只做方向 1
    python3 scripts/selfcheck_surface.py --consumers <dir>  # 加方向 2
退出码：0 通过 / 1 失败（列出差异）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "tool-surface.json"
SERVER_NAME = "fd-text-core"

TOOL_PATTERN = re.compile(rf"mcp__{SERVER_NAME}__([A-Za-z0-9_.\-]+)")
VERBS = ("search", "semantic_search", "read", "presets", "federated")


def load_manifest() -> dict:
    if not MANIFEST.exists():
        fail([f"manifest 缺失：{MANIFEST}（契约：缺失即未上牌）"])
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def manifest_tools(m: dict) -> dict[str, str]:
    """工具名 → 暴露级别。"""
    out: dict[str, str] = {}
    for g in m.get("tool_groups", []):
        level = g.get("level")
        if level not in ("commercial", "internal"):
            fail([f"tool_groups.{g.get('group')} 的 level 非法：{level!r}"])
        for t in g.get("tools", []):
            if t in out and out[t] != level:
                fail([f"工具 {t} 在多个 group 中级别冲突"])
            out[t] = level
    return out


async def runtime_tools() -> set[str]:
    """从运行时取真值（引擎按已加载垂域动态注册工具）。"""
    sys.path.insert(0, str(REPO))
    from fd_text_core.server import mcp  # type: ignore

    tools = await mcp.list_tools()
    return {getattr(t, "name", None) or t["name"] for t in tools}


def expected_tools() -> set[str]:
    """按已加载垂域推导期望工具面（与运行时独立推导，用于双向比对）。"""
    sys.path.insert(0, str(REPO))
    from fd_text_core.verticals.registry import list_verticals

    return {f"{d}_{v}" for d in list_verticals() for v in VERBS}


def consumer_tools(consumers: list[Path]) -> dict[str, set[str]]:
    """消费侧（套件/技能）**必需依赖**中实际点名的本 server 工具。

    判定：frontmatter 的 `data_binding` 行 = 必需依赖（受闭合约束）；
    正文里为说明降级而提及 internal 工具不算越界——看的是依赖声明。
    """
    out: dict[str, set[str]] = {}
    for c in consumers:
        if not c.exists():
            fail([f"消费侧路径不存在：{c}"])
        used: set[str] = set()
        for f in list(c.rglob("*.json")) + list(c.rglob("*.md")):
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for line in text.splitlines():
                if re.match(r"\s*(data_binding|mcp_servers)\s*:", line):
                    used |= set(TOOL_PATTERN.findall(line))
        if used:
            out[str(c)] = used
    return out


def fail(problems: list[str]) -> "None":
    print(f"✗ {len(problems)} 项不通过：", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    raise SystemExit(1)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--consumers", nargs="*", type=Path, default=[])
    args = ap.parse_args()

    m = load_manifest()
    declared = manifest_tools(m)
    actual = await runtime_tools()
    expected = expected_tools()
    problems: list[str] = []

    # 方向 1a：运行时 vs 期望（垂域集合变化而 server 注册面未跟上）
    if actual != expected:
        for t in sorted(actual - expected):
            problems.append(f"[注册表漂移] 运行时注册了非期望工具：{t}")
        for t in sorted(expected - actual):
            problems.append(f"[注册表缺失] 期望工具未注册：{t}")

    # 方向 1b：manifest vs 运行时（双向）
    missing_in_manifest = sorted(actual - set(declared))
    stale_in_manifest = sorted(set(declared) - actual)
    if missing_in_manifest:
        problems.append(f"[注册表漂移] 代码已注册但 manifest 未声明：{', '.join(missing_in_manifest)}")
    if stale_in_manifest:
        problems.append(f"[manifest 陈旧] manifest 声明但代码未注册：{', '.join(stale_in_manifest)}")

    expected_count = m.get("registry", {}).get("expected_tool_count")
    if expected_count is not None and len(actual) != expected_count:
        problems.append(f"[工具数不符] 运行时 {len(actual)} ≠ manifest 声明 {expected_count}")

    # 方向 2：闭合（消费侧 ⊆ commercial）
    internal = {t for t, lv in declared.items() if lv == "internal"}
    if args.consumers:
        for where, used in consumer_tools(args.consumers).items():
            over = sorted(used & internal)
            if over:
                problems.append(
                    f"[消费侧越界] {where} 点名了 internal 工具：{', '.join(over)}"
                    f"（manifest 定级 internal，不得进商业供给面）"
                )

    commercial = sorted(t for t, lv in declared.items() if lv == "commercial")
    print(f"manifest：{len(declared)} 工具（commercial {len(commercial)} / internal {len(internal)}）")
    print(f"运行时：{len(actual)} 工具")
    if internal:
        print(f"internal（消费侧不得点名）：{', '.join(sorted(internal))}")

    if problems:
        fail(problems)
    print("✓ selfcheck 双向断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
