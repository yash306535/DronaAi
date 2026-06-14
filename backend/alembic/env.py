"""Alembic migration environment for DRONA AI.

This wires Alembic to the application's own SQLAlchemy metadata and database
URL so migrations always match the ORM models and the configured backend:

- ``target_metadata`` is ``app.core.db.Base.metadata`` after importing every
  model (via ``app.models``), so autogenerate sees all current tables.
- The database URL is resolved from ``app.core.config.get_settings()`` rather
  than ``alembic.ini``; locally that is SQLite, and on Render it is the
  managed PostgreSQL ``DATABASE_URL`` env var. Secrets never live in the repo.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import the application's declarative Base and force-register all models so
# their tables appear on ``Base.metadata``.
import app.models  # noqa: F401  (import for table registration side effects)
from app.core.config import get_settings
from app.core.db import Base

# Alembic Config object (values from alembic.ini).
config = context.config

# Resolve the database URL from application settings (SQLite local / Postgres
# on deploy). This overrides the blank ``sqlalchemy.url`` in alembic.ini.
config.set_main_option("sqlalchemy.url", get_settings().DATABASE_URL)

# Configure Python logging from the ini file, if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata used for 'autogenerate' support and offline migrations.
target_metadata = Base.metadata


def _is_sqlite() -> bool:
    url = config.get_main_option("sqlalchemy.url") or ""
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a live connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        # SQLite cannot ALTER most things; batch mode renders changes as
        # table-rebuilds so the same migrations run locally and on Postgres.
        render_as_batch=_is_sqlite(),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=_is_sqlite(),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
