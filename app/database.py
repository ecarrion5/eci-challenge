"""Engine, declarative base and session management.

- ``get_db`` is the FastAPI dependency: one session and one transaction per
  request. It commits when the handler returns and rolls back if the handler raises.
- ``session_scope`` gives scripts (seed, maintenance) the same commit/rollback
  behaviour outside FastAPI.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./eci.db")

_is_sqlite = DATABASE_URL.startswith("sqlite")

engine: Engine = create_engine(
    DATABASE_URL,
    # SQLite connections may be used from FastAPI's threadpool workers.
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    echo=os.getenv("SQL_ECHO", "0") == "1",
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")  # FKs are off by default in SQLite
        if ":memory:" not in DATABASE_URL:
            cursor.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
        cursor.close()


# Deterministic constraint names keep future Alembic migrations stable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    # Objects stay readable after commit, e.g. when building the response model.
    expire_on_commit=False,
)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one transaction per request (spec §6).

    Use it through ``app.deps.DbSession``, which sets ``scope="function"`` so the
    commit happens before the response is sent.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts: commit on success, roll back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(drop: bool = False) -> None:
    """Create every table. Pass ``drop=True`` to drop and recreate them (dev only)."""
    from app import models  # noqa: F401  (registers the models on Base.metadata)

    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
