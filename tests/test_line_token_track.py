"""词元轨的**接线**（converge-line-identity-and-keys 8.7，本线 `text-core`）。

原型：`fd-info/tests/test_line_token_track.py`（8.7 首条）→ 本仓逐条同形。规则层
（`test_line_token.py`）证「这枚票本身合不合法」，本文件证**票真的能走进路由**。
两层必须分开测：规则层全绿而接线层断，是这类改动最典型的假绿。

与 info 那份的差别只有一处（本仓的 `serve_http()` 吃的是模块级 `mcp` 而不是
`build_server()` 的返回值，所以夹具改 patche `server.mcp`），另一处必须单独钉的
断言两边一样：`serve_http()` 挂不挂中间件由**谓词**决定，谓词写成
`configured_token()` 时**只开词元轨的部署会一个中间件都不挂**——数据面对匿名请求
全放行，而日志里一行异常都没有。

三条边界各钉一次（与家族同口径）：
1. 共享钥匙照旧能用（平台代调走的就是它，动它就是动生产）；
2. 词元票能用，且身份真的挂进了 scope（`auth.SCOPE_IDENTITY`）；
3. 两个都没配时，行为与接入前**逐字节一致**。
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from fd_text_core import auth, wellknown
from fd_text_core.line_token import ENV_ISSUER, ENV_LINE, ENV_PEM, LINE_NAME
from tests.test_line_token import ISSUER, _PUB, mint

SHARED = "t0ken-family-shape"
DATA_PATH = "/mcp"
LINE_ENVS = (ENV_ISSUER, ENV_LINE, ENV_PEM)


def _run(mw, headers, *, path=DATA_PATH, scope_type="http"):
    """喂一个真 ASGI scope 进去，返回 (status, body, downstream_scope)。

    不装 fastmcp、不起 uvicorn：判的是中间件这一层，与传输层无关。
    """
    seen = {}
    sent = []

    async def downstream(scope, receive, send):
        seen["scope"] = scope
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": scope_type,
        "path": path,
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    asyncio.run(mw(downstream)(scope, receive, send))

    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, seen.get("scope")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(auth.ENV_TOKEN, raising=False)
    for env in LINE_ENVS:
        monkeypatch.delenv(env, raising=False)
    yield


def _enable_line_track(monkeypatch):
    monkeypatch.setenv(ENV_ISSUER, ISSUER)
    monkeypatch.setenv(ENV_LINE, LINE_NAME)
    monkeypatch.setenv(ENV_PEM, _PUB)


def test_gate_enabled_covers_either_track(monkeypatch):
    """门存在 = 共享钥匙**或**词元轨任一被配置；两者都空时不挂门。"""
    assert auth.gate_enabled() is False
    monkeypatch.setenv(auth.ENV_TOKEN, SHARED)
    assert auth.gate_enabled() is True
    monkeypatch.delenv(auth.ENV_TOKEN)
    monkeypatch.setenv(ENV_ISSUER, ISSUER)
    monkeypatch.setenv(ENV_PEM, _PUB)
    assert auth.gate_enabled() is True, "只开词元轨的部署也必须有门"


def test_shared_token_still_works_when_line_track_enabled(monkeypatch):
    """开词元轨不影响回退轨——平台代调走的就是共享钥匙这条。"""
    monkeypatch.setenv(auth.ENV_TOKEN, SHARED)
    _enable_line_track(monkeypatch)
    status, body, _ = _run(auth.BearerAuthMiddleware, {"authorization": f"Bearer {SHARED}"})
    assert (status, body) == (200, b"ok")


def test_line_token_passes_and_identity_reaches_scope(monkeypatch):
    """词元票 → 放行；且身份挂进 `auth.SCOPE_IDENTITY`（不是只判了个签名）。"""
    _enable_line_track(monkeypatch)
    status, _, scope = _run(
        auth.BearerAuthMiddleware,
        {"authorization": f"Bearer {mint(sub='caller-42')}"},
    )
    assert status == 200
    ident = (scope or {}).get(auth.SCOPE_IDENTITY)
    assert ident is not None, "票验过了却没把身份交给下游 = 接线层断"
    assert ident.subject == "caller-42"
    assert ident.can_write is True


def test_bad_and_foreign_tokens_are_401_with_the_same_body(monkeypatch):
    """坏票、别线的票、共享钥匙的近似值，都回**同一句** 401（不解释哪一步没过）。"""
    monkeypatch.setenv(auth.ENV_TOKEN, SHARED)
    _enable_line_track(monkeypatch)
    bodies = []
    for token in ("not-a-jwt", mint(source="wire"), mint(aud="wire"), SHARED + "x"):
        status, body, _ = _run(auth.BearerAuthMiddleware, {"authorization": f"Bearer {token}"})
        assert status == 401, token[:12]
        bodies.append(body)
    assert len(set(bodies)) == 1, "不同的失败原因不该给出不同的响应正文"


def test_unconfigured_is_byte_identical_to_before(monkeypatch):
    """两个都没配 ⇒ 门不存在：数据面与豁免面都不 401（与接入前逐字节一致）。"""
    assert auth.gate_enabled() is False
    for path in (DATA_PATH, *auth.EXEMPT_PATHS):
        status, _, _ = _run(auth.BearerAuthMiddleware, {}, path=path)
        assert status == 200, path


def test_exempt_paths_survive_with_line_track_only(monkeypatch):
    """只开词元轨时，探针与自描述端点仍匿名可读（门不能把它们一起关掉）。"""
    _enable_line_track(monkeypatch)
    for path in ("/health", wellknown.WELL_KNOWN_PATH):
        assert _run(auth.BearerAuthMiddleware, {}, path=path)[0] == 200, path
    assert _run(auth.BearerAuthMiddleware, {})[0] == 401


def test_lifespan_passes_through_with_line_track_only(monkeypatch):
    """只开词元轨时 lifespan 也必须放过去，否则应用起不来。"""
    _enable_line_track(monkeypatch)
    status, _, scope = _run(auth.BearerAuthMiddleware, {}, scope_type="lifespan")
    assert status == 200
    assert scope is not None


def test_serve_http_mounts_gate_with_line_track_only(monkeypatch):
    """**本仓最容易错的一处**：只配词元轨时 `serve_http()` 也必须走 ASGI 面。

    用假 mcp/starlette/uvicorn 观测分支（借它们证明「分支选对了、中间件挂上了」，
    不再验一次 starlette 本身）。
    """
    from fd_text_core import server as server_mod

    calls = {}

    class _Middleware:
        def __init__(self, cls, *args, **kwargs):
            self.cls = cls

    fake_mw_mod = types.ModuleType("starlette.middleware")
    fake_mw_mod.Middleware = _Middleware
    fake_starlette = types.ModuleType("starlette")
    fake_starlette.middleware = fake_mw_mod
    monkeypatch.setitem(sys.modules, "starlette", fake_starlette)
    monkeypatch.setitem(sys.modules, "starlette.middleware", fake_mw_mod)

    class FakeMcp:
        def run(self, **kwargs):
            calls["run"] = kwargs

        def http_app(self, **kwargs):
            calls["http_app"] = kwargs
            return "ASGI-APP"

    monkeypatch.setattr(server_mod, "mcp", FakeMcp())
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kwargs: calls.setdefault("uvicorn", {"app": app, **kwargs})
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    _enable_line_track(monkeypatch)  # 只开词元轨，**不设** MCP_BEARER_TOKEN
    server_mod.serve_http(9312)

    assert "http_app" in calls, "只开词元轨却没挂牌 = 数据面全裸而日志无声"
    assert len(calls["http_app"]["middleware"]) == 1
    assert calls["http_app"]["middleware"][0].cls is auth.BearerAuthMiddleware
    assert calls["uvicorn"]["port"] == 9312
    assert "run" not in calls
