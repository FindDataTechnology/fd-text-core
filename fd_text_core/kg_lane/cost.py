"""成本账本：按 episode × 操作分项记账，并支持 lane 规模外推。

为什么分项而不是只记总数：试点实测显示**去重占 47%、抽取占 53%**，
而二者的成本驱动因素不同（抽取随文本长度，去重随图规模）。
只记总数会让外推失真——短文本 lane 的固定开销占比会更高。

外推必须带 assumption 声明（spec 要求）：单 episode 实测值向目标规模
折算时的每一条假设都要写出来，否则数字无法被复核。
"""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 操作分类：与 graphiti 管线阶段一一对应
OPS = ("extraction", "dedup", "invalidation", "embedding", "other")

_current: ContextVar[tuple[str | None, str]] = ContextVar("kg_cost_ctx", default=(None, "other"))

# graphiti 大部分调用不传 prompt_name，只能靠提示词特征归类。
# 顺序有意义：invalidation 的提示词里也常出现 "extract"，必须先判。
_INVALIDATION_MARKERS = ("invalidat",)
_DEDUP_MARKERS = ("dedup", "duplicate")
_EXTRACTION_MARKERS = ("extract", "entities", "relationships", "classify")


def classify_operation(prompt_name: str | None, messages: list[Any] | None = None) -> str:
    """按 prompt_name / 前两条消息内容判定这次 LLM 调用属于哪个环节。"""
    text = prompt_name or ""
    if not text and messages:
        text = " ".join(getattr(m, "content", "") or "" for m in messages[:2])
    text = text.lower()
    if any(k in text for k in _INVALIDATION_MARKERS):
        return "invalidation"
    if any(k in text for k in _DEDUP_MARKERS):
        return "dedup"
    if any(k in text for k in _EXTRACTION_MARKERS):
        return "extraction"
    return "other"


@dataclass
class CostRecord:
    episode_id: str
    operation: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ts: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_jsonl(self) -> str:
        return json.dumps(
            {
                "episode_id": self.episode_id,
                "operation": self.operation,
                "model": self.model,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "ts": self.ts,
                "meta": self.meta,
            },
            ensure_ascii=False,
        )


class CostLedger:
    """JSONL 台账：一行一次调用；`begin_episode` 设定后续记录的默认 episode。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._episode_id = "unknown"
        self.records: list[CostRecord] = []

    def begin_episode(self, episode_id: str) -> None:
        self._episode_id = episode_id

    def record(self, *, operation: str, model: str, prompt_tokens: int = 0,
               completion_tokens: int = 0, episode_id: str | None = None,
               meta: dict[str, Any] | None = None) -> CostRecord:
        if operation not in OPS:
            operation = "other"
        rec = CostRecord(
            episode_id=episode_id or self._episode_id,
            operation=operation,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            meta=meta or {},
        )
        self.records.append(rec)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(rec.to_jsonl() + "\n")
        return rec

    def load_existing(self) -> CostLedger:
        """把已有 JSONL 读回内存（续跑/跨进程汇总时必须，否则 aggregate 只看本次）。"""
        if not self.path.exists():
            return self
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue  # 中途被打断写了半行是预期内的
            self.records.append(
                CostRecord(
                    episode_id=raw.get("episode_id") or "unknown",
                    operation=raw.get("operation") or "other",
                    model=raw.get("model") or "unknown",
                    prompt_tokens=int(raw.get("prompt_tokens") or 0),
                    completion_tokens=int(raw.get("completion_tokens") or 0),
                    ts=float(raw.get("ts") or 0.0),
                    meta=raw.get("meta") or {},
                )
            )
        return self

    # ---------------------------------------------------------- aggregation

    def aggregate(self) -> dict[str, dict[str, dict[str, int]]]:
        """{episode: {operation: {calls, prompt_tokens, completion_tokens, total_tokens}}}"""
        out: dict[str, dict[str, dict[str, int]]] = {}
        for rec in self.records:
            per = out.setdefault(rec.episode_id, {})
            agg = per.setdefault(
                rec.operation,
                {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )
            agg["calls"] += 1
            agg["prompt_tokens"] += rec.prompt_tokens
            agg["completion_tokens"] += rec.completion_tokens
            agg["total_tokens"] += rec.prompt_tokens + rec.completion_tokens
        return out


def summarize(ledger: CostLedger) -> dict[str, Any]:
    """按操作汇总全量账单（spec 要求的「分项落档」形态）。"""
    per_op: dict[str, dict[str, int]] = {op: {"calls": 0, "prompt_tokens": 0,
                                              "completion_tokens": 0, "total_tokens": 0}
                                         for op in OPS}
    episodes: set[str] = set()
    for rec in ledger.records:
        agg = per_op[rec.operation]
        agg["calls"] += 1
        agg["prompt_tokens"] += rec.prompt_tokens
        agg["completion_tokens"] += rec.completion_tokens
        agg["total_tokens"] += rec.prompt_tokens + rec.completion_tokens
        if rec.episode_id and rec.episode_id != "unknown":
            episodes.add(rec.episode_id)
    total = sum(v["total_tokens"] for v in per_op.values())
    return {
        "episodes": len(episodes),
        "total_tokens": total,
        "by_operation": {k: v for k, v in per_op.items() if v["calls"]},
        "tokens_per_episode": (total / len(episodes)) if episodes else None,
    }


def extrapolate(
    measured: dict[str, Any],
    target_episode_count: int,
    *,
    usd_per_million_tokens: float | None = None,
    fixed_cost_share: float | None = None,
) -> dict[str, Any]:
    """把实测单 episode 成本外推到目标规模，并**声明全部假设**。

    `fixed_cost_share`：短文本 lane 的固定开销占比。为 None 时按线性折算
    （即假设成本与文本量无关地同比放大）；给值时按「固定部分不随文本缩短」
    做保守折算——试点显示抽取+去重里有相当部分是每条 episode 的固定开销。
    """
    per_ep = measured.get("tokens_per_episode")
    if not per_ep:
        raise ValueError("measured stats lack tokens_per_episode; cannot extrapolate")
    projected = per_ep * target_episode_count
    assumptions = [
        f"单 episode 实测均值 {per_ep:,.0f} token（来自 {measured.get('episodes')} 条实测）",
        "按同一单价与同一模型折算，未考虑规模效应或缓存命中",
        "未计入失败重试成本（实测失败条目不产生或产生少量 token）",
    ]
    result: dict[str, Any] = {
        "target_episodes": target_episode_count,
        "projected_tokens": round(projected),
        "assumptions": assumptions,
    }
    if usd_per_million_tokens is not None:
        result["usd_per_million_tokens"] = usd_per_million_tokens
        result["projected_usd"] = round(projected / 1e6 * usd_per_million_tokens, 2)
        assumptions.append(f"单价 ${usd_per_million_tokens:.3f}/M token（实测口径）")
    if fixed_cost_share is not None:
        result["fixed_cost_share"] = fixed_cost_share
        assumptions.append(
            f"另有保守口径：假设 {fixed_cost_share:.0%} 为固定开销（不随文本缩短下降），"
            "适用于以短文本折算自长文本基准的场景"
        )
    return result


# ---------------------------------------------------------------- 记账包装
# graphiti 的 _generate_response 会丢弃 usage，只有 AsyncOpenAI 那一层还能看到
# token 数。所以分三层包：底层截 usage -> 中层按 prompt 归类 operation ->
# 外层 embedder 按条数记账。少任何一层，账单都会缺项。


def _delegate(attr: str):
    """透传属性，但内层未绑定前抛 AttributeError（否则会无限递归）。"""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        inner = self.__dict__.get(attr)
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    return __getattr__


class _RecordingCompletions:
    def __init__(self, inner: Any, ledger: CostLedger):
        self._inner = inner
        self._ledger = ledger

    async def create(self, **kwargs: Any) -> Any:
        response = await self._inner.create(**kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            episode_id, operation = _current.get()
            self._ledger.record(
                operation=operation,
                model=str(getattr(response, "model", None) or kwargs.get("model") or "unknown"),
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                episode_id=episode_id,
            )
        return response


class RecordingOpenAI:
    """AsyncOpenAI 形状的包装：拦 chat.completions.create 记 usage，其余透传。"""

    def __init__(self, inner: Any, ledger: CostLedger):
        self._inner = inner
        self.chat = type("_Chat", (), {})()
        self.chat.completions = _RecordingCompletions(inner.chat.completions, ledger)

    __getattr__ = _delegate("_inner")


try:  # 必须真继承：Graphiti 用 pydantic 对 llm_client 做 isinstance 校验
    from graphiti_core.llm_client.client import LLMClient as _LLMClientBase
except ModuleNotFoundError:  # pragma: no cover - 无 graphiti 环境

    class _LLMClientBase:  # type: ignore[no-redef]
        async def generate_response(self, messages: list[Any], **kwargs: Any) -> Any:
            raise NotImplementedError


class CostRecordingLLMClient(_LLMClientBase):  # type: ignore[misc,valid-type]
    """graphiti LLMClient 的记账包装：判定 operation -> 设 ContextVar -> 委托内层。

    必须真继承 LLMClient——graphiti 构造 GraphitiClients 时按 pydantic 类型校验，
    鸭子类型会被直接拒（cross_encoder/embedder 也是同一规矩）。
    父类字段（config/cache 等）用 __getattr__ 透传给内层。
    """

    def __init__(self, inner: Any, ledger: CostLedger):
        self._inner = inner
        self._ledger = ledger

    # 父类字段（config/cache_enabled/tracer 等）一律透传给内层——不调用
    # super().__init__，因为内层已经持有真实配置，重复初始化只会造出两份真相。
    __getattr__ = _delegate("_inner")

    async def generate_response(self, messages: list[Any], response_model: Any = None,
                                max_tokens: int | None = None, model_size: Any = None,
                                group_id: str | None = None, prompt_name: str | None = None,
                                **kwargs: Any) -> Any:
        operation = classify_operation(prompt_name, messages)
        forward: dict[str, Any] = {
            "response_model": response_model,
            "max_tokens": max_tokens,
            "group_id": group_id,
            "prompt_name": prompt_name,
            **kwargs,
        }
        if model_size is not None:
            forward["model_size"] = model_size
        token = _current.set((self._ledger._episode_id, operation))
        try:
            return await self._inner.generate_response(messages, **forward)
        finally:
            _current.reset(token)

    async def _generate_response(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner._generate_response(*args, **kwargs)


try:  # 必须真继承：Graphiti 构造期对 embedder 做 isinstance 校验（试点踩过）
    from graphiti_core.embedder.client import EmbedderClient as _EmbedderClient
except ModuleNotFoundError:  # pragma: no cover - 无 graphiti 环境退化为鸭子类型

    class _EmbedderClient:  # type: ignore[no-redef]
        async def create(self, input_data: Any) -> list[float]:
            raise NotImplementedError

        async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
            raise NotImplementedError


class LedgerEmbedder(_EmbedderClient):  # type: ignore[misc,valid-type]
    """包在任意 embedder 外层记嵌入成本（按请求与文本条数，token 记 0）。

    嵌入端点的 usage 各家口径不一（多数不返回），故以条数记账；
    成本折算时用「条数 × 平均 token」或直接按文本长度估。
    """

    def __init__(self, inner: Any, ledger: CostLedger, model: str):
        self._inner = inner
        self._ledger = ledger
        self._model = model

    __getattr__ = _delegate("_inner")

    def _note(self, texts: int) -> None:
        self._ledger.record(
            operation="embedding", model=self._model,
            meta={"requests": 1, "texts": texts},
        )

    async def create(self, input_data: Any) -> list[float]:
        result = await self._inner.create(input_data)
        self._note(1)
        return result

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        result = await self._inner.create_batch(input_data_list)
        self._note(len(input_data_list))
        return result


def wire_cost_recording(llm: Any, embedder: Any, ledger: CostLedger,
                        *, model: str) -> tuple[Any, Any]:
    """给已有客户端套上记账层，返回 (llm, embedder)。

    注意：LLM 的 usage 只有在底层 AsyncOpenAI 被包住时才拿得到。若 llm 是用
    默认 client 构造的，本函数只能归到 "other"（仍记 episode 归属，不会串账）。
    """
    return CostRecordingLLMClient(llm, ledger), LedgerEmbedder(embedder, ledger, model)
