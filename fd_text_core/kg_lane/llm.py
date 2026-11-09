"""容错 LLM 客户端：把模型回吐的四种坏形状都吃下来。

四类失败全部来自 2026-10-18~19 法律试点的 live 实测（510 条 episode 跑批），
不是假设：

  A. **schema 信封**——json_object 模式下模型把 schema 片段当输出结构包一层，
     实测两种形状：`{"properties": {...}}` 与 `{"properties": {...}, "type": "object"}`。
     graphiti 只剥 ``` 围栏，不解信封，下游会拿到外壳（表现为缺字段校验错）。
  B. **未转义控制字符**——中文长文本里出现裸换行，严格 `json.loads` 直接抛
     `Invalid control character`。
  C. **尾部截断**——推理模型把 JSON 写到一半被 max_tokens 截断，
     抛 `Unterminated string`。
  D. **schema 噪声值**——模型把 `{'anyOf': [...], 'title': 'Invalid At'}` 这种
     schema 片段塞进字段值，pydantic 报 "Input should be a valid string"。

同时把 provider 钉扎交给网关（词元通道下账号级配置会覆盖客户端传值），
故这里不主动下发 provider 字段。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "deepseek-v4.1-flash"
# 推理型模型先产 reasoning_content 再产正文；16384 实测会被思考吃光导致截断。
DEFAULT_MAX_TOKENS = 32000

# schema 关键字：用于判定「这是外壳/噪声」而非业务数据
SCHEMA_ONLY_KEYS = {
    "type", "title", "description", "format", "enum", "items", "properties",
    "required", "additionalProperties", "$defs", "definitions", "$schema",
    "default", "examples", "pattern", "minimum", "maximum", "minLength",
    "maxLength", "const", "anyOf", "allOf", "oneOf",
}
# 出现任一即强烈指示「这是 schema 片段」——单靠 type/format 会误伤业务值
# （如 {'type': '中级'} 是合法业务数据）。
STRUCTURAL_KEYS = {"anyOf", "allOf", "oneOf", "$defs", "definitions", "items",
                   "additionalProperties", "pattern"}


def strip_code_fences(text: str) -> str:
    """剥 ``` / ```json 围栏（graphiti 同款行为，这里显式化以便单测）。"""
    import re

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*[ \t]*\r?\n?", "", stripped)
        stripped = re.sub(r"\r?\n?```[ \t]*$", "", stripped)
    return stripped.strip()


def unwrap_schema_envelope(data: Any) -> Any:
    """剥 A 类信封：除 properties 外只含 schema 关键字时才剥。

    出现任何业务字段说明这是真实结果，不能剥。
    """
    if not isinstance(data, dict):
        return data
    props = data.get("properties")
    if not isinstance(props, dict):
        return data
    extra = set(data.keys()) - {"properties"} - SCHEMA_ONLY_KEYS
    if extra:
        return data
    return props


def strip_schema_artifacts(data: Any) -> Any:
    """剥 D 类噪声值（递归）：纯 schema 片段构成的值降级为 None。

    触发条件必须含 STRUCTURAL_KEYS 之一——否则 `{'type': '中级'}` 这类
    合法业务值会被误伤。
    """
    if isinstance(data, dict):
        keys = set(data.keys())
        if data and keys & STRUCTURAL_KEYS and keys <= SCHEMA_ONLY_KEYS:
            return None
        return {k: strip_schema_artifacts(v) for k, v in data.items()}
    if isinstance(data, list):
        return [strip_schema_artifacts(v) for v in data]
    return data


def _close_unterminated(candidate: str) -> str:
    """截断修复：先闭合未结束的字符串，再按**栈逆序**补上未闭合的括号。

    两个必须避开的坑（都是实测踩出来的）：

    1. **不能按固定顺序补括号**。``{"entities": [{"name": "X"`` 的未闭合栈是
       `{`→`[`→`{`，正确收尾是 `}]}`（后进先出）。先补 `]` 再补 `}` 会造出
       `]}}`，仍然解析失败。
    2. **不能直接数字符串里出现的括号**。``count("[")`` 会把 JSON 字符串内容
       里的方括号也算进去，导致补多或补少。必须逐字符扫描并跳过字符串内部。

    只在字符串外维护括号栈；发现右括号时若与栈顶匹配才出栈（不匹配说明原始
    数据本身畸形，保持栈不动，让下游继续截断重试）。

    3. **截断点恰在反斜杠后**（``"foo\\``）：直接补 `"` 会被吃成转义引号
       （``"foo\\"`` 在 JSON 语法里字符串仍未闭合）。先把悬挂反斜杠补成
       完整的 `\\\\`（字面反斜杠，保留模型原内容）再闭引号。
    """
    in_string = False
    escaped = False
    stack: list[str] = []
    for ch in candidate:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            expected = "{" if ch == "}" else "["
            if stack and stack[-1] == expected:
                stack.pop()
    if in_string:
        if escaped:
            # 悬挂转义：不处理的话，下面补的引号会被吃成 \"，字符串仍未闭合
            candidate += "\\"
        candidate += '"'
    for opener in reversed(stack):
        candidate += "}" if opener == "{" else "]"
    return candidate


def _strip_leading_open_braces(text: str) -> str:
    """去掉 JSON 主体前多余的 `{`（nemotron-ultra 实测输出 `{\\n{"entities": ...}`）。

    判据用**试解析**而不是括号计数：逐个剥掉前缀 `{`，哪个能让剩下的部分真的
    解析成功，就取哪个。括号计数在这个场景不可靠——剥掉后主体往往仍以 `{`
    开头（合法的嵌套对象也是），计数法区分不了。

    保守性：返回的是**第一个能解析成功的变体**；一个都不成才原样返回，交给
    下游截断修复。绝不因为「看起来像」就改动输入。
    """
    stripped = text.lstrip()
    seen = []
    while stripped.startswith("{") and len(seen) < 4:
        body = stripped[1:].lstrip()
        if not body:
            break
        seen.append(body)
        # 优先保留原文；只在剥掉后能严格解析时才采用
        try:
            json.loads(body)
            return body
        except json.JSONDecodeError:
            pass
        stripped = body
    return text if not seen else seen[-1] if _any_parses(seen) else text


def _any_parses(variants: list[str]) -> bool:
    for v in variants:
        try:
            json.loads(v)
            return True
        except json.JSONDecodeError:
            continue
    return False


def loads_forgiving(text: str, max_repair_steps: int = 2000) -> Any:
    """解析 C 类截断 + B 类控制字符：去多余括号 → 严格 → strict=False → 截断修复。"""
    text = text.strip()
    candidates = [text, _strip_leading_open_braces(text)]
    for cand in candidates:
        if not cand:
            continue
        for loader in (json.loads, lambda t: json.loads(t, strict=False)):
            try:
                return loader(cand)
            except json.JSONDecodeError:
                pass

        trimmed = cand
        for _ in range(max_repair_steps):
            try:
                return json.loads(_close_unterminated(trimmed), strict=False)
            except json.JSONDecodeError:
                cut = max(trimmed.rfind(","), trimmed.rfind(":"))
                if cut <= 0:
                    break
                trimmed = trimmed[:cut]
    raise ValueError(
        f"unparseable LLM JSON (len={len(text)}): {text[:200]!r} ... {text[-120:]!r}"
    )


def null_to_empty_list(data: Any, list_keys: tuple[str, ...]) -> Any:
    """把指定键的 `null` 归一为 `[]`（递归到嵌套 dict）。

    为什么必须做：graphiti 的 `EdgeDuplicate` 要求 `duplicate_facts` /
    `contradicted_facts` 是 list。模型在「没有重复/没有矛盾」时常直接写
    `null`，pydantic 于是抛 ValidationError，整条 episode 报废。语义上
    `null` 就是「空集」，归一不是掩盖错误，而是把模型的口语表达对齐到契约。
    """
    wanted = set(list_keys)
    if isinstance(data, dict):
        return {
            k: ([] if v is None and k in wanted else null_to_empty_list(v, list_keys))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [null_to_empty_list(v, list_keys) for v in data]
    return data


def parse_llm_json(raw: str, *, list_keys: tuple[str, ...] = ()) -> Any:
    """完整管线：剥围栏 → 容错解析 → 剥信封 → 剥噪声 → null 归一。

    `list_keys` 由调用方按目标模型声明（graphiti 的抽取出参键名）。
    """
    parsed = loads_forgiving(strip_code_fences(raw))
    parsed = unwrap_schema_envelope(parsed)
    parsed = strip_schema_artifacts(parsed)
    if list_keys:
        parsed = null_to_empty_list(parsed, list_keys)
    return parsed


def drop_incomplete_items(data: Any, response_model: Any) -> tuple[Any, int]:
    """丢掉列表里缺必填字段的项（**不猜值**），返回 (数据, 丢弃数)。

    实测坑（2026-10-22 重建）：模型偶尔漏写 `relation_type`，
    `ExtractedEdges` 校验整表失败 → **整篇 episode 报废**（212 篇里 2 篇）。
    一条坏边远轻于丢一整篇：丢项并记数，让 episode 继续入图。

    只处理「响应模型里有 list 字段、且其元素类型是 pydantic 模型」的情形；
    必填字段从元素模型取（`is_required()`）。取不到模型信息就原样返回，
    宁可漏修也不猜字段名。
    """
    if response_model is None or not isinstance(data, dict):
        return data, 0
    fields = getattr(response_model, "model_fields", None)
    if not isinstance(fields, dict):
        return data, 0
    dropped = 0
    out = dict(data)
    for name, info in fields.items():
        items = out.get(name)
        if not isinstance(items, list) or not items:
            continue
        ann = getattr(info, "annotation", None)
        args = getattr(ann, "__args__", ())
        item_model = None
        for cand in (ann, *args):
            if hasattr(cand, "model_fields"):
                item_model = cand
                break
        if item_model is None:
            continue
        item_fields = getattr(item_model, "model_fields", {})
        required = [k for k, v in item_fields.items()
                    if callable(getattr(v, "is_required", None)) and v.is_required()]
        if not required:
            continue
        kept = [it for it in items
                if isinstance(it, dict) and all(it.get(k) not in (None, "") for k in required)]
        dropped += len(items) - len(kept)
        out[name] = kept
    return out, dropped


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key: str
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    # native -> json_schema；json_object -> json_object；hint -> json_object+text 降级
    structured_mode: str = "json_object"


def _list_fields(response_model: Any) -> tuple[str, ...]:
    """从 pydantic 模型里取出所有可接受 list 的字段名。

    用来把模型返回的 `null` 归一成 `[]`。取不到（None/非 pydantic）就返回空，
    宁可漏归一也不猜键名——猜错会把真正的缺失字段填成空列表。

    注意 `list[int] | None` 的 `__origin__` 是 `typing.Union`，只认 `list`
    会漏判（实测踩过），故用 `list` 出现在类型参数里的方式判断。
    """
    if response_model is None:
        return ()
    fields = getattr(response_model, "model_fields", None)
    if not isinstance(fields, dict):
        return ()
    import typing

    out = []
    for name, info in fields.items():
        ann = getattr(info, "annotation", None)
        if ann is list:
            out.append(name)
            continue
        args = getattr(ann, "__args__", ())
        # list[X] -> (X,)；list[X] | None -> (list[X], NoneType)
        if getattr(ann, "__origin__", None) is list or any(
            getattr(a, "__origin__", None) is list for a in args
        ) or typing.get_origin(ann) is typing.Union and any(
            typing.get_origin(a) is list for a in args
        ):
            out.append(name)
    return tuple(out)


def build_llm_client(settings: LLMSettings, client=None, hint_mode: bool = False,
                      ledger=None):
    """构造 graphiti 兼容的 LLM 客户端（接管解析，其余与父类一致）。

    `ledger` 给定时，把底层 AsyncOpenAI 包一层记账——graphiti 的
    `_generate_response` 会丢弃 usage，只有那一层还看得到 token 数。
    """
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    mode_map = {"native": "json_schema", "json_object": "json_object", "hint": "json_object"}
    if settings.structured_mode not in mode_map:
        raise ValueError(
            f"structured_mode must be one of {sorted(mode_map)}, got {settings.structured_mode!r}"
        )

    if client is None:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url)
    if ledger is not None:
        from .cost import RecordingOpenAI

        client = RecordingOpenAI(client, ledger)

    class _ForgivingClient(OpenAIGenericClient):
        def _build_response_format(self, response_model):
            if hint_mode:
                return {"type": "text"}
            return super()._build_response_format(response_model)

        async def _generate_response(self, messages, response_model=None,
                                     max_tokens=DEFAULT_MAX_TOKENS, model_size=None,
                                     *, model=None):
            import openai
            from graphiti_core.llm_client.errors import EmptyResponseError, RateLimitError

            openai_messages = []
            for m in messages:
                content = self._clean_input(m.content)
                if m.role in ("user", "system"):
                    openai_messages.append({"role": m.role, "content": content})
            try:
                response = await self.client.chat.completions.create(
                    model=model or self.model or DEFAULT_MODEL,
                    messages=openai_messages,
                    temperature=self.temperature,
                    max_tokens=max_tokens,
                    response_format=self._build_response_format(response_model),
                )
                result = response.choices[0].message.content or ""
                if not result.strip():
                    raise EmptyResponseError("LLM returned an empty response")
                parsed = parse_llm_json(result, list_keys=_list_fields(response_model))
                parsed, dropped = drop_incomplete_items(parsed, response_model)
                if dropped:
                    # 不静默：丢了几条边要能在日志里查到（整篇失败才是更坏的结果）
                    logger.warning(
                        "dropped %d incomplete item(s) from %s response",
                        dropped, getattr(response_model, "__name__", response_model))
                return parsed
            except openai.RateLimitError as exc:
                raise RateLimitError from exc
            except (EmptyResponseError, RateLimitError, ValueError):
                raise
            except Exception as exc:
                logger.error("Error in generating LLM response: %s", exc)
                raise

    return _ForgivingClient(
        config=LLMConfig(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
        ),
        client=client,
        structured_output_mode=mode_map[settings.structured_mode],
        max_tokens=settings.max_tokens,
    )
