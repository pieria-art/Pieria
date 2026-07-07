"""
Database configuration and session management using SQLAlchemy.
Phase 2: Transitioning from filesystem-only to SQLite-backed state.
"""

import logging
import os
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger("artwork-display-api.database")

# Pre-flight setup: Ensure data directories exist for zero-touch deployments
os.makedirs("./data", exist_ok=True)
os.makedirs("./Artwork", exist_ok=True)

# Architectural Choice: SQLite for local, single-user performance and simplicity.
# Overridable so dev/tests/tooling can point elsewhere without editing code (D2 — same default as before).
SQLALCHEMY_DATABASE_URL = os.getenv("SD_DATABASE_URL", "sqlite:///./data/artwork.db")

# check_same_thread=False is required for SQLite under FastAPI's threadpool. `timeout` is the busy-wait
# on a locked DB (D1), the connect_args companion to the busy_timeout PRAGMA below.
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30}
)


# D1: put SQLite in WAL mode with a generous busy timeout on every connection. With 4 uvicorn workers +
# 5s heartbeats + 1s command polls + per-transition playback writes, the default rollback-journal mode
# serializes readers against a writer and surfaces "database is locked" 500s under write bursts (e.g. a
# bulk approve during playback). WAL lets readers proceed during a write; busy_timeout makes a blocked
# writer wait instead of erroring. Attached to THIS engine only — test engines are separate in-memory.
@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    """
    Base class for SQLAlchemy models.
    """
    pass

def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that provides a SQLAlchemy database session.

    Yields:
        Session: The database session.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# NOTE: schema creation is intentionally NOT done here. Alembic owns the schema as the single
# source of truth — boot runs db_migrate.run_migrations() (base -> head builds it, or a legacy
# DB is reconciled to the baseline). `Base.metadata.create_all` is used only by the test suite
# against throwaway engines. See ADR-035 for why create_all was removed from the boot path.
