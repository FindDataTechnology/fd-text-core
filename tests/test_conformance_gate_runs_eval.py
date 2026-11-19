"""检索评测门不许静默消失——那等于没有这道门（审计 06 §9）。

`check_retrieval_eval` 是唯一自动化的「检索还准不准」检查（混合 vs 关键词 vs 向量
的 hit@k / MRR，跑在标注集上）。但它**默认关**：

    if not domain:
        return        # 未开启：不出现在结果里

而 CI 跑的是 `python scripts/conformance_gate.py --changed-base origin/main`——
既没传 `--eval-domain` / `--eval-all`，也没传 `--strict`。于是：

* 这道门在 CI 里**从不执行**；
* 而 `--strict` 才把「缺项」当失败（`:797` `if args.strict and (failed or skipped)`），
  不传就是警告。

合起来的后果：**一个把召回率砍半的改动能过门**——因为那项检查根本没登记结果。

本文件钉住两件事：
1. 未开启时必须**登记一条 SKIPPED**（而不是 `return` 掉、让结果里没有它）；
2. CI 必须带 `--strict`，否则 SKIPPED 只是警告。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "conformance_gate.py"
CI = REPO / ".github" / "workflows" / "ci.yml"
SRC = GATE.read_text(encoding="utf-8")


def test_disabled_eval_registers_a_skip_instead_of_vanishing():
    """`if not domain: return` 是缺陷形状——它让这项检查在结果里不存在。

    **第一版这条测试是假绿**：正则要求 `return` 独占一行，而实际代码是
    `return  # 未开启：...`（带行内注释），于是没匹配到、断言"通过"。
    改成允许行尾注释。这类假绿比没有测试更糟——它让人以为守住了。
    """
    m = re.search(r"def check_retrieval_eval\(.*?\n(.*?)\n\ndef ", SRC, re.DOTALL)
    assert m, "没找到 check_retrieval_eval 的函数体"
    head = m.group(1)[:1500]

    bare_return = re.search(r"if not domain:\s*\n\s*return\b", head, re.MULTILINE)
    assert bare_return is None, (
        "`if not domain: return` 仍然存在 —— 未开启时这项检查**根本不出现在结果里**，"
        "于是 --strict 也抓不到它，一个砍半召回率的改动能过门。"
        "应当登记一条 SKIPPED（或 FAIL）。"
    )


def test_ci_runs_the_gate_with_strict():
    """CI 必须 `--strict`：不严格时缺项只是警告。"""
    assert CI.exists(), f"CI 工作流不存在：{CI}"
    ci = CI.read_text(encoding="utf-8")
    lines = [l for l in ci.splitlines() if "conformance_gate.py" in l]
    assert lines, "CI 里没有调用 conformance_gate.py"
    assert any("--strict" in l for l in lines), (
        f"CI 调用没有 --strict：{lines} —— 缺项会被当成警告而不是失败"
    )


def test_retrieval_eval_is_actually_requested_in_ci():
    """CI 还得真的**开启**这项检查（`--eval-all` 或 `--eval-domain`）。

    只加 `--strict` 不够：`--strict` 只是把「已登记但被跳过」的项判失败。
    如果这项检查压根没被请求，它连「跳过」都不会登记——除非上一条修好了。
    """
    ci = CI.read_text(encoding="utf-8")
    lines = [l for l in ci.splitlines() if "conformance_gate.py" in l]
    enabled = any(("--eval-all" in l or "--eval-domain" in l) for l in lines)
    registers_skip = "if not domain:" not in SRC.split("def check_retrieval_eval")[1][:800]
    assert enabled or registers_skip, (
        "CI 既没传 --eval-all/--eval-domain，函数里也没登记 SKIPPED —— "
        "这条检索验收线在 CI 里完全不存在"
    )
