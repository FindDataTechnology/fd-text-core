# fd-text-core — 通用文本引擎（纯基建）
# 通道: GitHub Actions -> hkccr -> the relay node tcr-relay -> ccr
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖先行，利于构建缓存
# 线自描述端点 /.well-known/line-manifest.json（change converge-line-identity-and-keys 1.4）：
# 仓内那份是唯一真源，端点原样回吐；不 COPY 进镜像则镜像里永远 404。
COPY line-manifest.json ./
COPY pyproject.toml README.md ./
COPY fd_text_core/__init__.py fd_text_core/
RUN pip install --no-cache-dir .

# KG 车道的可选依赖（graphiti-core / falkordb / openai）随镜像装上——
# kg_tools 的只读后端要 graphiti，不装的话 kg_* 工具一律降级为
# UPSTREAM_UNAVAILABLE（引擎本身不受影响）。
# **装失败不拖垮引擎**：车道隔离不变量要求核心不依赖它，故失败只告警。
RUN pip install --no-cache-dir '.[kg]' \
 || echo "WARN: kg extra 未装成 —— KG 工具将降级为 UPSTREAM_UNAVAILABLE（引擎照常）"

COPY fd_text_core/ ./fd_text_core/
COPY scripts/ ./scripts/
COPY deploy/ ./deploy/
# 工具面 manifest（mcp-surface-contract 的「上牌」）：`scripts/selfcheck_surface.py`
# 从仓根读它，读了才能断言「注册的工具 == 声明的工具」。不 COPY 进镜像，容器里跑
# selfcheck 只会得到「manifest 缺失 = 未上牌」——部署物上的表面契约无法自证。
# 与第 13 行 line-manifest 同一个坑，故这次一并加了守卫测试（test_dockerfile_copies_*）。
COPY tool-surface.json ./

EXPOSE 8000

CMD ["python", "-m", "fd_text_core.server"]
