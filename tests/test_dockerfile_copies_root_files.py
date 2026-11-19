"""镜像里必须带上"运行时要读的仓根文件"——这是打包层的「声明≠挂载」。

2026-11-13 实测：`tool-surface.json` 在仓里有、镜像里没有。于是部署物上跑
`python scripts/selfcheck_surface.py` 得到

    ✗ manifest 缺失：/app/tool-surface.json（契约：缺失即未上牌）

而**在仓库树上跑同一命令是绿的**——因为文件就在仓根。这类缺陷的结构是：

  · 代码读 `REPO/<file>`（仓根），Dockerfile 只 COPY 了 `fd_text_core/` 与 `scripts/`；
  · 测试与 CI 都在仓库树上跑，永远看不到镜像里的缺席；
  · 症状只出现在容器里，且文案（"未上牌"）听起来像配置没做，不像打包漏了。

`line-manifest.json` 已经撞过一次同样的坑（Dockerfile 第 13 行那条注释就是当时的笔记），
但当时只写了注释、没加守卫，于是同一个坑第二次踩中。本文件把它变成断言。

判据取自**代码实际读的路径**，不是手写清单：扫仓内 Python 源码里
`REPO / "<name>"` / `parents[1] / "<name>"` 形态的仓根路径字面量，要求每个命中项
要么被 Dockerfile COPY，要么被明确标注为「仅开发期使用」。这样以后新增一个仓根文件
却忘了 COPY，会在这里立刻红。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")

#: 只在这些目录里找「仓根文件」的读取点（引擎与脚本才是进镜像的那部分）。
SCAN_DIRS = ("fd_text_core", "scripts")

#: 明确不随镜像走的仓根文件（开发期/出片期用）。加一项必须写清理由。
DEV_ONLY = {
    "pyproject.toml",       # 构建期 `pip install .` 用；运行时不需要（依赖已装进 site-packages）
    "README.md",            # 同上，安装元数据用
    "Dockerfile",           # 构建期
    "tests",                # 不进镜像（Dockerfile 注释写明）
}

_ROOT_FILE_PATTERNS = (
    re.compile(r'(?:REPO|ROOT)\s*/\s*"([\w.\-]+)"'),
    re.compile(r'parents\[\d\]\s*/\s*"([\w.\-]+)"'),
)


def _root_file_reads() -> dict[str, list[str]]:
    """{仓根文件名: [引用它的源码相对路径, …]}"""
    found: dict[str, list[str]] = {}
    for d in SCAN_DIRS:
        for py in sorted((REPO / d).rglob("*.py")):
            try:
                src = py.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for pat in _ROOT_FILE_PATTERNS:
                for name in pat.findall(src):
                    if "/" in name or not name:
                        continue
                    found.setdefault(name, []).append(str(py.relative_to(REPO)))
    return found


def _copied_paths() -> set[str]:
    """Dockerfile 里 COPY 的源路径（去引号，归一化 `./`）。"""
    out: set[str] = set()
    for line in DOCKERFILE.splitlines():
        m = re.match(r"\s*COPY\s+(?!--from)(.+)$", line)
        if not m:
            continue
        parts = [p for p in m.group(1).split() if not p.startswith("--")]
        for src in parts[:-1]:  # 最后一个是目标
            out.add(src.lstrip("./").rstrip("/"))
    return out


def test_every_root_file_read_by_code_is_copied_into_the_image():
    copied = _copied_paths()
    missing = []
    for name, users in _root_file_reads().items():
        if name in DEV_ONLY:
            continue
        if name not in copied:
            missing.append(f"{name}（被 {', '.join(sorted(set(users)))} 读取）")
    assert not missing, (
        "这些仓根文件被运行时代码读取，但 Dockerfile 没有 COPY —— "
        "仓库树上测试全绿、容器里却读不到：\n  " + "\n  ".join(missing)
    )


def test_tool_surface_manifest_ships_with_the_image():
    """具体钉住本次的现场：工具面 manifest 必须进镜像。

    `selfcheck_surface.py` 的失败文案是「manifest 缺失 = 未上牌」，读起来像配置缺失，
    实际是打包漏了——这层歧义让它在仓库树上永远查不出来。
    """
    assert (REPO / "tool-surface.json").exists(), "仓根缺 tool-surface.json"
    assert "tool-surface.json" in _copied_paths(), (
        "Dockerfile 没有 COPY tool-surface.json —— 容器里跑 selfcheck_surface.py "
        "会报「未上牌」，而仓内跑是绿的"
    )


def test_line_and_tool_manifest_both_shipped():
    """两个「仓内唯一真源」都要进镜像：线自描述清单与工具面清单。"""
    copied = _copied_paths()
    for name in ("line-manifest.json", "tool-surface.json"):
        assert name in copied, f"Dockerfile 缺 COPY {name}"
