# fd-text-core — 通用文本引擎（纯基建）
# 通道: GitHub Actions -> hkccr -> cheap-3 tcr-relay -> ccr
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖先行，利于构建缓存
COPY pyproject.toml README.md ./
COPY fd_text_core/__init__.py fd_text_core/
RUN pip install --no-cache-dir .

COPY fd_text_core/ ./fd_text_core/
COPY scripts/ ./scripts/
COPY deploy/ ./deploy/

EXPOSE 8000

CMD ["python", "-m", "fd_text_core.server"]
