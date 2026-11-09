"""双口径评测：既有混合检索（基线）对照 KG 车道。

设计要点（全部来自试点教训）：

1. **基线参数名必须按工具分别声明**。试点曾把所有工具统一传 `query`，
   而 `guide_cases_search` 收的是 `title_query`——参数错误被当成
   「基线答不出」，直接把价值实证结论带偏（修后基线 3/6 → 6/6）。
   故这里用 `BASELINE_TOOL_ARGS` 显式映射，且**每个垂域自带自己的映射**。
2. **反向结论如实记录**。试点结论是「互补非替代」：KG 命中 2 例，
   反向 2 例（KG 不如基线）也如实落档。隐藏反向证据会让价值判断失真。
3. **逐题隔离**：一侧失败不影响另一侧，也不中断整批。
4. 判定留人工位（`verdict`）——spec 要求按 `verdict_rule` 人工评阅，
   工具不自动打分。
"""
from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

DEFAULT_TIMEOUT = 60.0


@dataclass
class EvalQuestion:
    id: str
    kind: Literal["temporal", "multihop"]
    query: str
    expectation: str = ""
    verdict_rule: str = ""
    # 时点题的评阅时点（ISO8601）。有值则 KG 侧按 valid_at<=as_of<invalid_at 过滤；
    # 不填就无法真正执行「按 valid_at 过滤后作答」这条 verdict_rule。
    as_of: str = ""


def load_questions(path: str) -> list[EvalQuestion]:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, dict) and "questions" in raw:
        raw = raw["questions"]
    if not isinstance(raw, list):
        raise TypeError("questions file must be a list or {'questions': [...]}")
    out = []
    for item in raw:
        kind = item.get("kind")
        if kind not in ("temporal", "multihop"):
            raise ValueError(f"question {item.get('id')!r}: kind must be temporal|multihop")
        out.append(
            EvalQuestion(
                id=item["id"], kind=kind, query=item["query"],
                expectation=item.get("expectation", ""),
                verdict_rule=item.get("verdict_rule", ""),
                as_of=item.get("as_of", ""),
            )
        )
    return out


@dataclass
class EvalResult:
    question: EvalQuestion
    baseline_evidence: list[str] = field(default_factory=list)
    kg_evidence: list[dict[str, Any]] = field(default_factory=list)
    verdict: str | None = None  # 人工评阅位
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.question.id,
            "kind": self.question.kind,
            "query": self.question.query,
            "baseline_evidence": self.baseline_evidence,
            "kg_evidence": self.kg_evidence,
            "verdict": self.verdict,
            "ts": self.ts,
        }


@dataclass
class EvalReport:
    results: list[EvalResult]

    def to_jsonl(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r.to_dict(), ensure_ascii=False) + "\n" for r in self.results)

    @staticmethod
    def _hit(evidence: list[Any]) -> bool:
        """有非空证据且不是错误载荷，记为命中。"""
        if not evidence:
            return False
        first = evidence[0]
        if isinstance(first, dict) and "error" in first:
            return False
        return not (isinstance(first, str) and first.strip().startswith('{"error"'))

    def summary(self) -> dict[str, Any]:
        by_kind: dict[str, dict[str, int]] = {}
        for r in self.results:
            agg = by_kind.setdefault(r.question.kind, {"total": 0, "baseline_hits": 0,
                                                      "kg_hits": 0, "kg_only": 0,
                                                      "baseline_only": 0, "both": 0})
            b, k = self._hit(r.baseline_evidence), self._hit(r.kg_evidence)
            agg["total"] += 1
            agg["baseline_hits"] += int(b)
            agg["kg_hits"] += int(k)
            agg["kg_only"] += int(k and not b)
            agg["baseline_only"] += int(b and not k)
            agg["both"] += int(b and k)
        return {
            "total": len(self.results),
            "baseline_hits": sum(v["baseline_hits"] for v in by_kind.values()),
            "kg_hits": sum(v["kg_hits"] for v in by_kind.values()),
            "kg_only": sum(v["kg_only"] for v in by_kind.values()),
            "baseline_only": sum(v["baseline_only"] for v in by_kind.values()),
            "by_kind": by_kind,
        }


class BaselineClient(Protocol):
    async def call(self, tool: str, arguments: dict[str, Any]) -> list[str]: ...


class DualEvalRunner:
    """同题双口径。`kg_query_fn(question)` 返回 [{fact, valid_at, provenance}, ...]。"""

    def __init__(
        self,
        baseline: BaselineClient | None,
        kg_query_fn: Callable[[EvalQuestion], Awaitable[list[dict[str, Any]]]] | None,
        *,
        baseline_tools: dict[str, tuple[str, ...]] | None = None,
        baseline_arg_names: dict[str, str] | None = None,
    ):
        self.baseline = baseline
        self.kg_query_fn = kg_query_fn
        self.baseline_tools = baseline_tools or {
            "temporal": ("law_semantic_search",),
            "multihop": ("law_semantic_search", "guide_cases_search"),
        }
        # 工具 -> 查询参数名。**必须逐工具声明**：不同工具的形参名不同，
        # 统一传 query 会把「参数错误」伪装成「基线答不出」（试点基线 3/6→6/6）。
        # 下表按 fd-legal-search-mcp 实际签名登记（2026-10-20 复核）。
        self.baseline_arg_names = baseline_arg_names or {
            "law_semantic_search": "query",
            "law_search": "title_query",
            "guide_cases_search": "title_query",
            "legal_docs_search": "title_query",
            "wenshu_search": "title_query",
        }

    async def run(self, questions: list[EvalQuestion], *,
                  baseline: bool = True, kg: bool = True) -> EvalReport:
        if baseline and self.baseline is None:
            raise ValueError("baseline requested but no baseline client provided")
        if kg and self.kg_query_fn is None:
            raise ValueError("kg requested but no kg_query_fn provided")

        results: list[EvalResult] = []
        for q in questions:
            res = EvalResult(question=q)
            if baseline:
                res.baseline_evidence = await self._run_baseline(q)
            if kg:
                res.kg_evidence = await self._run_kg(q)
            results.append(res)
        return EvalReport(results)

    async def _run_baseline(self, q: EvalQuestion) -> list[str]:
        evidence: list[str] = []
        for tool in self.baseline_tools.get(q.kind, ("semantic_search",)):
            arg_name = self.baseline_arg_names.get(tool, "query")
            try:
                evidence.extend(await self.baseline.call(tool, {arg_name: q.query}))
            except Exception as exc:  # noqa: BLE001 - 逐题隔离，不中断整批
                evidence.append(json.dumps(
                    {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return evidence

    async def _run_kg(self, q: EvalQuestion) -> list[dict[str, Any]]:
        try:
            return await self.kg_query_fn(q)
        except Exception as exc:  # noqa: BLE001
            return [{"error": f"{type(exc).__name__}: {exc}"}]


class McpBaselineClient:
    """MCP JSON-RPC 基线客户端（stdlib，无 mcp SDK 依赖）。

    握手：initialize → notifications/initialized → tools/call。
    兼容 SSE 分帧响应与普通 JSON。
    """

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT,
                 trust_env: bool = False):
        self.base_url = base_url
        self.timeout = timeout
        self.trust_env = trust_env
        self._session_id: str | None = None
        self._initialized = False

    async def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        import httpx

        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=self.trust_env) as c:
            resp = await c.post(self.base_url, json=payload, headers=headers)
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self._session_id = sid
            body = resp.text
            if resp.status_code >= 400 and not body:
                raise RuntimeError(f"HTTP {resp.status_code} from {self.base_url}")
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                for line in body.splitlines():
                    if line.startswith("data: "):
                        body = line[6:]
                        break
            return (json.loads(body) if body.strip() else {}), dict(resp.headers)

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        await self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "kg-lane-eval", "version": "1"}},
        })
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    async def call(self, tool: str, arguments: dict[str, Any]) -> list[str]:
        await self._ensure_init()
        payload, _ = await self._post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        })
        if "error" in payload:
            raise RuntimeError(f"MCP error: {payload['error']}")
        result = payload.get("result") or {}
        if result.get("isError"):
            raise RuntimeError(f"tool {tool} returned isError")
        texts: list[str] = []
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return texts

    async def list_tools(self) -> dict[str, list[str]]:
        """tools/list -> {工具名: 形参名列表}。

        跑分前必须核对：基线各工具的查询形参名不同（`query` vs `title_query`），
        映射写错会把「参数错误」伪装成「基线答不出」。先问服务器要真实签名，
        比对不上就报错，别硬跑。
        """
        await self._ensure_init()
        payload, _ = await self._post({
            "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {},
        })
        if "error" in payload:
            raise RuntimeError(f"MCP error: {payload['error']}")
        out: dict[str, list[str]] = {}
        for tool in (payload.get("result") or {}).get("tools") or []:
            name = tool.get("name")
            if not name:
                continue
            schema = tool.get("inputSchema") or {}
            props = schema.get("properties") or {}
            out[name] = sorted(props)
        return out
