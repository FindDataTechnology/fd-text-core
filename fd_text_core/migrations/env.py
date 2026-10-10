"""Alembic env：URL 取自 FDTEXT_DATABASE_URL，绝不回落到默认值。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parents[1]  # fd_text_core/
if str(ROOT.parent) not in sys.path:  # repo root
    sys.path.insert(0, str(ROOT.parent))

config = context.config

_db_url = os.environ.get("FDTEXT_DATABASE_URL", "").strip()
if not _db_url:
    raise RuntimeError("FDTEXT_DATABASE_URL 未设置——引擎不许为迁移猜测默认库")
config.set_main_option("sqlalchemy.url", _db_url)

target_metadata = None  # 本引擎以显式 SQL 迁移，不强依赖 ORM metadata


def run_migrations_offline() -> None:
    context.configure(url=_db_url, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
