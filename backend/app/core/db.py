"""Database engine, session factory, and the declarative ``Base``.

Provides:

- ``Base``: the declarative base every ORM model inherits from.
- ``engine``: a lazily-built SQLAlchemy engine driven by
  ``config.get_settings().DATABASE_URL`` (SQLite locally, PostgreSQL on deploy).
- ``SessionLocal``: a ``sessionmaker`` bound to that engine.
- ``get_db``: a FastAPI-style dependency that yields a session and always
  closes it.
- ``init_db`` / ``create_all``: helpers that create tables from the registered
  model metadata.

The engine is created lazily so importing this module never forces secret
validation (``get_settings`` validates required secrets) until a database
connection is actually needed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


# Lazily-initialized singletons. Built on first use so module import has no
# side effects (and does not require secrets to be present).
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _engine_kwargs(database_url: str) -> dict[str, Any]:
    """Return engine kwargs appropriate for the configured database backend."""
    kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        # SQLite needs this flag to be usable across threads (e.g. FastAPI
        # request handlers and the WS/event loop).
        kwargs["connect_args"] = {"check_same_thread": False}
    return kwargs


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        database_url = get_settings().DATABASE_URL
        _engine = create_engine(database_url, **_engine_kwargs(database_url))
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory, creating it on first use."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )
    return _session_factory


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a database session and closing it after use."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def _import_models() -> None:
    """Import the models package so all tables register on ``Base.metadata``."""
    # Imported for its registration side effects only.
    import app.models  # noqa: F401


def create_all(engine: Engine | None = None) -> None:
    """Create all tables defined on the registered models."""
    _import_models()
    Base.metadata.create_all(bind=engine or get_engine())


def init_db() -> None:
    """Initialize the database schema using the configured engine."""
    create_all(get_engine())
