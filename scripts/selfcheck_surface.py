#!/usr/bin/env python3
"""tool-surface manifest 双向 selfcheck（mcp-surface-contract）。

断言三个方向：
  1. 一致：manifest 声明的工具 == 引擎注册的工具（新增/删除工具而 manifest
     未同步即失败，反向同理）
  2. 属性：manifest 的 tool_annotations 与运行时四项提示逐项相等（缺项/漏工具/
     声明与实现不符都失败，报出工具名 + 字段名）
  3. 闭合：任何消费侧暴露的工具集 ⊆ manifest 的 commercial 集
     ——消费侧由 --consumers 传入的 pack/套件目录提供

代码注册面取 `mcp.list_tools()`（运行时真值，优先于 grep 码点）。

本 server 的特殊性：工具名按垂域动态生成（`<domain>_<verb>`），注册表内容
取决于已加载垂域。故一致性检查按「已加载垂域 × 动词集」推导期望工具面，
而非写死清单——这样新增垂域（第三个垂域接入）时 manifest 不同步会立刻失败。

用法：
    python3 scripts/selfcheck_surface.py                    # 只做方向 1
    python3 scripts/selfcheck_surface.py --consumers <dir>  # 加方向 2
    python3 scripts/selfcheck_surface.py --write-annotations  # 从运行时重生成 manifest 的 tool_annotations
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

#: KG 车道查询工具族（kg-lane-delivery-v1 D4）：引擎级固定族，不随垂域扩张，
#: 与 annotations.KG_TOOLS / kg_tools.register_kg_tools 一一对应。
KG_TOOLS = ("kg_search_facts", "kg_walk", "kg_facts_as_of")

#: 四项行为属性必须一次给全——缺一项客户端就得靠猜自动批准策略。
REQUIRED_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


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


async def runtime_annotation_map() -> dict[str, dict]:
    """运行时每个工具的行为属性（协议层原样值）；未声明记为空 dict。"""
    sys.path.insert(0, str(REPO))
    from fd_text_core.server import mcp  # type: ignore

    tools = await mcp.list_tools()
    out: dict[str, dict] = {}
    for t in tools:
        name = getattr(t, "name", None) or t["name"]
        a = getattr(t, "annotations", None)
        if a is None:
            out[name] = {}
            continue
        out[name] = {k: getattr(a, k) for k in REQUIRED_HINTS}
    return out


def check_annotations(m: dict, declared: dict[str, str], runtime: dict[str, dict]) -> list[str]:
    """manifest 行为属性 ↔ 运行时注册值逐项相等（两个方向都查）。

    漏登记 → 客户端拿到 None，无从判断自动批准；谎报只读 → 客户端自动批准一个
    会写库的工具。两者都是上牌缺陷，故必须报出工具名 + 字段名。
    """
    ann = m.get("tool_annotations")
    if not isinstance(ann, dict):
        return ["manifest 缺 tool_annotations（行为属性须随工具一并上牌）"]

    problems: list[str] = []
    for name in sorted(set(declared) - set(ann)):
        problems.append(f"[属性缺失] {name}: manifest 未声明行为属性")
    for name in sorted(set(ann) - set(declared)):
        problems.append(f"[属性陈旧] {name}: tool_annotations 登记了未上牌的工具")
    for name in sorted(set(declared) & set(ann)):
        d = ann[name]
        missing = [k for k in REQUIRED_HINTS if k not in d]
        if missing:
            problems.append(f"[属性不全] {name}: 缺 {', '.join(missing)}")
            continue
        rt = runtime.get(name, {})
        if not rt:
            problems.append(f"[属性缺失] {name}: 运行时未声明行为属性")
            continue
        for k in REQUIRED_HINTS:
            if rt.get(k) != d[k]:
                problems.append(
                    f"[属性不符] {name}.{k}: manifest 声明 {d[k]!r} 与运行时 {rt.get(k)!r} 不符"
                )
    return problems


def write_annotations(runtime: dict[str, dict]) -> None:
    """用运行时真值重生成 manifest 的 tool_annotations（唯一写入口）。

    手抄 15 个工具的名字与四项 flag 必然漂移；生成器的真值直接取自
    `mcp.list_tools()`，与 selfcheck 的比对源是同一处。

    只替换/插入 `tool_annotations` 这一块，不重排整个 manifest——其余字段是
    人工维护的（含中文说明），整份 json.dumps 重写会产生无意义的空白 diff。
    """
    ann = {name: runtime[name] for name in sorted(runtime)}
    body = json.dumps(ann, ensure_ascii=False, indent=2).splitlines()
    block = ['  "tool_annotations": {']
    block += ["  " + line for line in body[1:-1]]
    block += ["  },"]

    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith('  "tool_annotations"')), None)
    if start is not None:
        end = next(i for i in range(start, len(lines)) if lines[i] == "  },")
        lines[start:end + 1] = block
    else:
        at = next(i for i, ln in enumerate(lines) if ln.startswith('  "tool_groups"'))
        lines[at:at] = block

    text = "\n".join(lines) + "\n"
    json.loads(text)  # 落盘前先确认仍是合法 JSON
    MANIFEST.write_text(text, encoding="utf-8")
    print(f"✓ 已写入 tool_annotations：{len(ann)} 工具 → {MANIFEST}")


def expected_tools() -> set[str]:
    """按已加载垂域推导期望工具面（与运行时独立推导，用于双向比对）。

    垂域面 = 已加载垂域 × 动词集；KG 族 = 固定三件（引擎级车道，非垂域循环）。
    """
    sys.path.insert(0, str(REPO))
    from fd_text_core.verticals.registry import list_verticals

    return {f"{d}_{v}" for d in list_verticals() for v in VERBS} | set(KG_TOOLS)


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
            except Exception:  # noqa: S112, BLE001 —— 读文件失败就跳过这一份
                continue
            for line in text.splitlines():
                if re.match(r"\s*(data_binding|mcp_servers)\s*:", line):
                    used |= set(TOOL_PATTERN.findall(line))
        if used:
            out[str(c)] = used
    return out


def fail(problems: list[str]) -> None:
    print(f"✗ {len(problems)} 项不通过：", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    raise SystemExit(1)
def check_output_structure(m: dict, declared: dict[str, str]) -> list[str]:
    """输出结构状态必须显式登记（mcp-result-contract「无静默空缺」）。

    本服务尚未逐工具声明输出结构（返回形状随输入变化，需先固化契约），故
    manifest 里登记为 `undeclared`。这条检查保证：新增工具时不会出现"既没声明、
    也没登记为未声明"的静默空缺——那种工具在客户端看来与已声明工具无从区分。

    日后某工具真的挂上 output_schema 时，本检查会失败并提示把它移进 declared：
    登记状态与运行时事实必须一致，不能靠记忆维护。
    """
    block = m.get("output_structure")
    if not isinstance(block, dict):
        return ["manifest 缺 output_structure（输出结构状态须显式可见）"]
    dec = set(block.get("declared") or [])
    und = set(block.get("undeclared") or [])
    problems: list[str] = []
    both = sorted(dec & und)
    if both:
        problems.append(f"同时出现在 declared 与 undeclared：{both}")
    silent = sorted(set(declared) - dec - und)
    if silent:
        problems.append(f"既未声明也未登记为未声明（静默空缺）：{silent}")
    stray = sorted((dec | und) - set(declared))
    if stray:
        problems.append(f"output_structure 登记了未上牌的工具：{stray}")
    return problems



async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--consumers", nargs="*", type=Path, default=[])
    ap.add_argument("--write-annotations", action="store_true",
                    help="按运行时真值重生成 manifest 的 tool_annotations 后退出")
    args = ap.parse_args()

    m = load_manifest()
    declared = manifest_tools(m)
    runtime = await runtime_annotation_map()
    actual = set(runtime)
    expected = expected_tools()
    problems: list[str] = []

    if args.write_annotations:
        write_annotations(runtime)
        return 0

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

    # 方向 1c：manifest 行为属性 vs 运行时（协议层四项提示，两个方向）
    problems.extend(check_annotations(m, declared, runtime))
    problems += check_output_structure(m, declared)

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
