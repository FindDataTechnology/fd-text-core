"""垂域 KG 配置：本体、抽取指令、episode 粒度、语料范围、准入声明。

接入一个垂域 = 提交一份配置，车道与引擎核心零改动。这是本车道存在的理由：
法律试点证明了抽取机制与领域无关，领域差异全在本体与 prompt 里。

**准入前置（spec 首条要求）**：垂域必须声明「外部无权威图谱 API 可替代」。
已有外部权威图谱的领域（如学术域有 OpenAlex）不得用本车道自建抽取——
那是重新推导现成事实，成本高而边际价值极低。

**粒度默认（D3）**：默认「不切碎」。试点实测切碎语料更贵：每段都要重付
抽取+去重的固定开销（段落粒度 5×55,944 > 整篇 171,126 tokens/篇）。
垂域若要切分，必须随附整篇 vs 切分的实测成本依据。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# episode 粒度策略：整篇 / 按切块 / 按段落
GRANULARITIES = ("document", "chunk", "paragraph")


class ConfigError(ValueError):
    """配置非法（缺准入声明、粒度缺依据、本体为空等）。"""


@dataclass(frozen=True)
class EntityTypeSpec:
    """一个实体类型：名字 + 类型语义（进抽取 prompt）。"""

    name: str
    description: str

    def __post_init__(self) -> None:
        if not self.name or not self.name[0].isalpha():
            raise ConfigError(f"entity type name must start with a letter: {self.name!r}")
        if len(self.description.strip()) < 10:
            raise ConfigError(
                f"entity type {self.name!r} needs a real description (>=10 chars) — "
                "it is injected into the extraction prompt"
            )


@dataclass(frozen=True)
class AdmissionSpec:
    """准入声明：外部是否存在可替代的权威图谱。"""

    external_authority_available: bool
    evidence: str = ""

    def validate(self) -> None:
        if self.external_authority_available:
            raise ConfigError(
                "this lane is for verticals WITHOUT an external authoritative graph API; "
                f"an external graph exists — integrate it instead of self-extracting. {self.evidence}"
            )
        if len(self.evidence.strip()) < 10:
            raise ConfigError(
                "admission requires evidence that no external authoritative graph exists "
                "(what you checked and what you found)"
            )


@dataclass(frozen=True)
class GranularitySpec:
    mode: str = "document"
    # 声明切分时必填：整篇 vs 切分的实测总 token 对比
    measured_cost_note: str = ""

    def validate(self) -> None:
        if self.mode not in GRANULARITIES:
            raise ConfigError(f"granularity mode must be one of {GRANULARITIES}, got {self.mode!r}")
        if self.mode != "document" and len(self.measured_cost_note.strip()) < 20:
            raise ConfigError(
                f"granularity mode {self.mode!r} requires a measured cost note "
                "(pilot: chunking re-pays extraction+dedup fixed cost per piece, making it "
                "MORE expensive than whole-document — provide your own measurement)"
            )


@dataclass(frozen=True)
class CorpusSpec:
    domain: str
    description: str = ""


@dataclass(frozen=True)
class KGVerticalConfig:
    """一份垂域 KG 配置。加载后即校验，非法直接拒。"""

    name: str
    entity_types: tuple[EntityTypeSpec, ...]
    extraction_instructions: str
    admission: AdmissionSpec
    corpus: CorpusSpec
    granularity: GranularitySpec = field(default_factory=GranularitySpec)
    group_id: str = ""
    language: str = "zh"
    # 多跳游走的超级边抑制：关系类型在图内的总度数超过该阈值时，walk 默认不沿它展开。
    # 缺省 50 的依据：gov 图中 HAS_DOC_TYPE 一条关系连着全部 212 篇公文的文种，
    # 这类「分类标签式」关系会把裸跳数遍历吸走（G-M2 实测返回 175 条同质文种事实）。
    # 法律域关系（审理/援引/废止）度数天然低于 50，不受影响。
    walk_super_edge_threshold: int = 50

    def validate(self) -> "KGVerticalConfig":
        if not self.name:
            raise ConfigError("config name is required")
        if self.walk_super_edge_threshold < 1:
            raise ConfigError(
                f"config {self.name!r}: walk_super_edge_threshold must be >=1, "
                f"got {self.walk_super_edge_threshold}"
            )
        if len(self.entity_types) < 2:
            raise ConfigError(
                f"config {self.name!r} needs >=2 entity types (got {len(self.entity_types)})"
            )
        names = [e.name for e in self.entity_types]
        if len(set(names)) != len(names):
            raise ConfigError(f"duplicate entity type names in {self.name!r}: {names}")
        if len(self.extraction_instructions.strip()) < 20:
            raise ConfigError(
                f"config {self.name!r} needs real extraction instructions (>=20 chars)"
            )
        if not self.corpus.domain:
            raise ConfigError(f"config {self.name!r} must declare corpus.domain")
        self.admission.validate()
        self.granularity.validate()
        return self

    def entity_type_dict(self) -> dict[str, str]:
        """{类型名: 类型语义} —— 供 prompt 构建与本体装配。"""
        return {e.name: e.description for e in self.entity_types}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "group_id": self.group_id or self.name,
            "language": self.language,
            "entity_types": [{"name": e.name, "description": e.description}
                             for e in self.entity_types],
            "extraction_instructions": self.extraction_instructions,
            "admission": {
                "external_authority_available": self.admission.external_authority_available,
                "evidence": self.admission.evidence,
            },
            "corpus": {"domain": self.corpus.domain, "description": self.corpus.description},
            "granularity": {
                "mode": self.granularity.mode,
                "measured_cost_note": self.granularity.measured_cost_note,
            },
        }


def from_dict(raw: dict[str, Any]) -> KGVerticalConfig:
    """从 dict 构造并校验。缺字段/非法值一律 ConfigError，不静默补默认。"""
    try:
        entity_types = tuple(
            EntityTypeSpec(name=e["name"], description=e.get("description", ""))
            for e in raw["entity_types"]
        )
        admission_raw = raw["admission"]
        admission = AdmissionSpec(
            external_authority_available=bool(admission_raw["external_authority_available"]),
            evidence=admission_raw.get("evidence", ""),
        )
        gran_raw = raw.get("granularity") or {}
        cfg = KGVerticalConfig(
            name=raw["name"],
            entity_types=entity_types,
            extraction_instructions=raw["extraction_instructions"],
            admission=admission,
            corpus=CorpusSpec(**raw["corpus"]),
            granularity=GranularitySpec(
                mode=gran_raw.get("mode", "document"),
                measured_cost_note=gran_raw.get("measured_cost_note", ""),
            ),
            group_id=raw.get("group_id", ""),
            language=raw.get("language", "zh"),
            walk_super_edge_threshold=int(raw.get("walk_super_edge_threshold", 50)),
        )
    except KeyError as exc:
        raise ConfigError(f"missing required key: {exc}") from exc
    return cfg.validate()


def load(path: str | Path) -> KGVerticalConfig:
    """从 JSON 文件加载垂域配置。"""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    return from_dict(json.loads(p.read_text(encoding="utf-8")))


_REGISTRY: dict[str, KGVerticalConfig] = {}


def register(cfg: KGVerticalConfig) -> None:
    cfg.validate()
    if cfg.name in _REGISTRY:
        raise ConfigError(f"KG vertical already registered: {cfg.name}")
    _REGISTRY[cfg.name] = cfg


def replace(cfg: KGVerticalConfig) -> None:
    """以模块内权威配置覆盖已注册项（与引擎垂域注册表同语义）。"""
    _REGISTRY[cfg.name] = cfg.validate()


def get(name: str) -> KGVerticalConfig:
    if name not in _REGISTRY:
        raise ConfigError(f"KG vertical not registered: {name}; registered={sorted(_REGISTRY)}")
    return _REGISTRY[name]


def list_verticals() -> list[str]:
    return sorted(_REGISTRY)


def clear() -> None:  # 测试用
    _REGISTRY.clear()
