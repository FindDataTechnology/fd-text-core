"""剧本域解析器：干净文本 -> 场景/角色/台词/动作线结构树。

中西两种戏剧形态映射到同一结构契约（与论文域的 paper>section 同构）：
  西方：act>ACT I > scene>SCENE 1 > (speaker, line)
  古典：act>第一折 > scene>（旦扮窦娥上）> (speaker, line)

设计取舍：标记识别中英并列，绝不写死一种形态——这是本垂域作为「引擎抽象
第二次检验」的关键点（若只能认一种，说明抽象不通用，应先补引擎侧抽象）。
括号内容抽为独立字段（舞台提示），不混进台词参与嵌入，以提升检索信噪比。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 场景/幕/场/折/出：中英并列
_RE_ACT_WEST = re.compile(r"^\s*(ACT|SCENE)\s+([IVXLC0-9]+|[A-Z0-9]+)\b[.:]?\s*(.*)$", re.IGNORECASE)
_RE_SCENE_WEST = re.compile(r"^\s*(INT\.|EXT\.|INT\b|EXT\b)\s*[.:-]?\s*(.*)$", re.IGNORECASE)
_RE_ACT_CN = re.compile(r"^\s*(?:={1,4}\s*)?第\s*([一二三四五六七八九十百零〇\d]+)\s*(折|出|幕|本|齣)\s*(?:={1,4})?\s*(.*)$")
_RE_SCENE_CN_HEAD = re.compile(r"^\s*(?:={1,4}\s*)?(楔子|序幕|尾声)\s*(?:={1,4})?\s*(.*)$")
_RE_SCENE_CN = re.compile(r"^\s*(?:={1,4}\s*)?(楔子|序幕|尾声|第\s*[一二三四五六七八九十百零〇\d]+\s*(?:折|出|幕|场|齣))\s*(?:={1,4})?\s*(.*)$")

# 说话人：现代 `名字:` / `名字：`；古典 `（末云）`、`（旦唱）` 或行首 `末云：`
_RE_SPEAKER_MODERN = re.compile(r"^\s*([^\s:：]{1,20})\s*[:：]\s*(.*)$")
_RE_SPEAKER_CN = re.compile(r"^[（(〔【]\s*([^）)〕】]{1,12}?)\s*(云|唱|白|念|科|詩云|白云)\s*[）)〕】]\s*(.*)$")
_RE_SPEAKER_CN_BARE = re.compile(r"^\s*([^\s:：]{1,8})\s*(云|唱|白)\s*[:：]\s*(.*)$")
# 古典登场括注里的角色名需剥离「上/云/唱」等动作字（否则说话人变成「窦娥上，云」）
_RE_ROLE_TAIL = re.compile(r"(?:雲|云|唱|白|科|上|下|詩|念|做[^，,]{0,6})[，,、。]*$")


def _strip_role_tail(name: str) -> str:
    """反复剥离尾部动作字与标点（「窦娥上，云」->「窦娥」）。"""
    prev = None
    while prev != name:
        prev = name
        name = _RE_ROLE_TAIL.sub("", name).strip()
    return name

_RE_STAGE = re.compile(r"[（(〔【]([^）)〕】]*)[）)〕】]")  # 括号=舞台提示（含〔〕【】）
_RE_ROLE_CN = re.compile(r"[（(〔【]\s*([^）)〕】]{0,10}?)\s*扮\s*([^）)〕】]{1,12}?)\s*[）)〕】]")  # （旦扮窦娥上）


@dataclass
class Line:
    """一行台词单元。"""

    speaker: str = ""
    text: str = ""
    stage: list[str] = field(default_factory=list)  # 舞台提示（括号内容，不参与嵌入主文本）
    kind: str = "line"  # line | action | heading


@dataclass
class Scene:
    act: str = "act"
    name: str = "scene"
    lines: list[Line] = field(default_factory=list)

    @property
    def path(self) -> str:
        return f"screenplay>{self.act}>{self.name}"

    @property
    def speakers(self) -> list[str]:
        seen: list[str] = []
        for ln in self.lines:
            if ln.speaker and ln.speaker not in seen:
                seen.append(ln.speaker)
        return seen


@dataclass
class ParsedPlay:
    title: str = ""
    scenes: list[Scene] = field(default_factory=list)
    characters: list[str] = field(default_factory=list)
    source_kind: str = "screenplay"

    def section_texts(self) -> list[tuple[str, str]]:
        """产出 (section_path, 文本)，交给引擎切块器。

        只给台词与动作线文本（舞台提示已抽离），保持嵌入信号干净。
        """
        out = []
        for sc in self.scenes:
            body = "\n".join(
                (f"{ln.speaker}: {ln.text}" if ln.speaker else ln.text)
                for ln in sc.lines
                if ln.text
            )
            if body:
                out.append((sc.path, body))
        return out


def _strip_stage(text: str) -> tuple[str, list[str]]:
    """抽出括号舞台提示，返回 (去提示文本, 提示列表)。"""
    stages = [m.group(1).strip() for m in _RE_STAGE.finditer(text)]
    cleaned = _RE_STAGE.sub(" ", text)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return cleaned, stages


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def parse_play(text: str) -> ParsedPlay:
    """解析剧本全文。识别失败时退化为单场景，绝不抛错（诚实降级而非崩）。"""
    play = ParsedPlay()
    if not text or not text.strip():
        return play

    lines = text.splitlines()
    first = _clean(lines[0]) if lines else ""
    # 首行作标题，但不得吞掉标题式章节行（如 `== 楔子 ==`）——
    # 否则第一个场景会丢失（实测《竇娥冤》首行正是楔子标题）。
    heading_like = bool(
        _RE_ACT_WEST.match(first)
        or _RE_ACT_CN.match(first)
        or _RE_SCENE_CN_HEAD.match(first)
        or _RE_SCENE_WEST.match(first)
        or _RE_SCENE_CN.match(first)
    )
    if first and len(first) < 120 and not heading_like:
        play.title = first
        lines = lines[1:]

    cur_act = "act"
    cur = Scene(act=cur_act, name="scene")
    scenes: list[Scene] = [cur]
    pending_stage: list[str] = []

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue

        m = _RE_ACT_WEST.match(line)
        if m:
            label = m.group(1).upper()
            # ACT 与 SCENE 是两个层级：只有 ACT 才推进 act，SCENE 只开新场景，
            # 否则 SCENE 会把 ACT 覆盖掉（实测 ACT I 被 SCENE 1 吃掉）。
            if label == "ACT":
                cur_act = f"ACT {m.group(2)}".strip()
            cur = Scene(act=cur_act, name=(f"SCENE {m.group(2)}" if label == "SCENE" else "scene"))
            scenes.append(cur)
            rest = _clean(m.group(3))
            if rest:
                cur.lines.append(Line(text=rest, kind="heading"))
            continue
            cur = Scene(act=cur_act, name="scene")
            scenes.append(cur)
            rest = _clean(m.group(3))
            if rest:
                cur.lines.append(Line(text=rest, kind="heading"))
            continue

        m = _RE_SCENE_CN_HEAD.match(line)
        if m:
            cur = Scene(act=cur_act, name=m.group(1))
            scenes.append(cur)
            rest = _clean(m.group(2))
            if rest:
                cur.lines.append(Line(text=rest, kind="heading"))
            continue

        m = _RE_ACT_CN.match(line)
        if m:
            cur_act = f"第{m.group(1)}{m.group(2)}"
            cur = Scene(act=cur_act, name="scene")
            scenes.append(cur)
            rest = _clean(m.group(3))
            if rest:
                cur.lines.append(Line(text=rest, kind="heading"))
            continue

        m = _RE_SCENE_WEST.match(line) or _RE_SCENE_CN.match(line)
        if m:
            name = _clean(m.group(2)) or _clean(m.group(1))
            cur = Scene(act=cur_act, name=name or "scene")
            scenes.append(cur)
            continue

        # 古典角色登场括注（旦扮窦娥上）兼作说话人线索
        role = _RE_ROLE_CN.search(line)
        if role and not _RE_SPEAKER_MODERN.match(line):
            speaker = _strip_role_tail(role.group(2).strip())
            # 括注行本身只是登场提示：说话人记下，正文留空由后续行补
            cleaned, stages = _strip_stage(line)
            cur.lines.append(Line(speaker=speaker, text="", stage=stages) if not cleaned else Line(speaker=speaker, text=cleaned, stage=stages))
            continue

        m = _RE_SPEAKER_CN.match(line)
        if m:
            cleaned, stages = _strip_stage(m.group(3) or "")
            cur.lines.append(Line(speaker=_strip_role_tail(m.group(1).strip()), text=cleaned, stage=pending_stage + stages))
            pending_stage = []
            continue

        m = _RE_SPEAKER_CN_BARE.match(line)
        if m:
            cleaned, stages = _strip_stage(m.group(3) or "")
            cur.lines.append(Line(speaker=_strip_role_tail(m.group(1).strip()), text=cleaned, stage=pending_stage + stages))
            pending_stage = []
            continue

        # 【曲牌】开头的行是唱词而非说话人，不参与说话人识别
        stripped = line.strip()
        if stripped.startswith("【"):
            cleaned, stages = _strip_stage(line)
            if cleaned:
                cur.lines.append(Line(text=cleaned, kind="action", stage=stages))
            continue

        m = _RE_SPEAKER_MODERN.match(line)
        if m and m.group(2).strip():
            cleaned, stages = _strip_stage(m.group(2))
            cur.lines.append(Line(speaker=_clean(m.group(1)), text=cleaned, stage=pending_stage + stages))
            pending_stage = []
            continue

        # 纯括号行 = 舞台提示，挂到下一句而非单独成行
        if stripped[:1] in "(（〔【" and stripped[-1:] in ")）〕】":
            pending_stage.append(stripped.strip("()（）〔〕【】").strip())
            continue

        cleaned, stages = _strip_stage(line)
        if cleaned:
            cur.lines.append(Line(text=cleaned, kind="action", stage=stages))

    play.scenes = [s for s in scenes if s.lines]
    chars: list[str] = []
    for s in play.scenes:
        for c in s.speakers:
            if c not in chars:
                chars.append(c)
    play.characters = chars
    return play
