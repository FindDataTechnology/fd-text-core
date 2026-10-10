"""PG 访问：懒连接池 + HNSW 会话调优。

会话级 GUC 沿用舰队既有配方（ef_search / iterative_scan），这两项不过会话
会让过滤型查询返回空集或丢簇——坑已踩过，勿删。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from psycopg_pool import ConnectionPool

from .config import Settings

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


class UpstreamUnavailable(RuntimeError):
    """上游（PG / 对象存储 / 嵌入 API）不可用。缺哪说哪。"""


def _settings() -> Settings:
    from .config import Settings as _S

    return _S.from_env()


def get_pool(settings: Settings | None = None) -> ConnectionPool:
    """进程内单例连接池；首次调用才建池（延迟失败到真正用库时）。"""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                s = settings or _settings()
                try:
                    _pool = ConnectionPool(s.database_url, min_size=1, max_size=8, open=False)
                    _pool.open(wait=True, timeout=10)
                except Exception as exc:  # 上游不可达 -> 诚实抛
                    raise UpstreamUnavailable(f"PG 不可达: {type(exc).__name__}: {exc}") from exc
    return _pool


def reset_pool() -> None:
    """测试用：丢弃当前池，下次重建。"""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass
        _pool = None


@contextmanager
def cursor(commit: bool = False) -> Iterator[Any]:
    """取一条连接并应用向量会话 GUC；commit=True 时提交。"""
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL hnsw.ef_search = 200")
            cur.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
            yield cur
        if commit:
            conn.commit()


def fetch_all(sql: str, params: tuple | None = None) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_one(sql: str, params: tuple | None = None) -> dict | None:
    with cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(cols, row)) if row else None


def execute(sql: str, params: tuple | None = None) -> int:
    """写操作，返回影响行数。"""
    with cursor(commit=True) as cur:
        cur.execute(sql, params or ())
        return cur.rowcount
