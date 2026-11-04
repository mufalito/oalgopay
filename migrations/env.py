"""Alembic environment: runs both standalone (CLI) and programmatically.

The gateway runs migrations at boot via db.connect() -> alembic.command.upgrade,
which passes its already-configured engine through config.attributes. Standalone
CLI use resolves the URL the same way the app does: OALGOPAY_DATABASE_URL env
or config `database_url` key, else the derived SQLite default.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import db as app_db  # noqa: E402
from app.config import DEFAULT_DATA_DIR, ConfigError, database_url, load_config  # noqa: E402
from app.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    try:
        return database_url(load_config())
    except ConfigError:
        # no usable config.json (e.g. a fresh checkout): the repo-local default
        return f"sqlite:///{(DEFAULT_DATA_DIR / 'gateway.db').as_posix()}"


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of hitting a database."""
    url = context.config.get_main_option("sqlalchemy.url") or _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Reuse the caller's engine when driven programmatically (db.connect);
    otherwise build one with the app's engine-factory (SQLite PRAGMAs included)."""
    engine = context.config.attributes.get("engine")
    if engine is None:
        engine = app_db._make_engine(_resolve_url())

    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
