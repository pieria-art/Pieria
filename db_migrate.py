"""Boot-time schema management — Alembic is the single source of truth.

Replaces the old boot sequence (swallowed `alembic upgrade head`, then
`create_all` papering over the failure). See migrations/versions/0001_baseline.py
and ADR-035 for the drift this fixes.

Contract:
  * Fresh/empty DB      -> `upgrade head` builds the baseline schema.
  * Legacy/populated DB stamped at a retired revision (or unstamped) -> verify the
    schema is complete, stamp the baseline (metadata only, no DDL, no data), then
    upgrade to head.
  * Any migration failure PROPAGATES so the caller can halt boot loudly — it is
    never swallowed into a running-but-broken server.
"""
import logging
from typing import Optional

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect, text

import models  # noqa: F401  — registers every table on Base.metadata
from database import Base

logger = logging.getLogger("artwork-display-api.db_migrate")

BASELINE = "0001_baseline"

# Revision ids from the pre-squash chain (940d71e4b7bf .. a1b2c3d4e5f6), collapsed into
# 0001_baseline. A DB stamped at any of these already carries the full current schema
# (built historically by create_all and/or those migrations), so it is reconciled to the
# baseline with a metadata-only stamp rather than re-running DDL that would collide.
RETIRED_REVISIONS = frozenset({
    "940d71e4b7bf", "6c2b48b03a18", "aedcf08a6f43", "9918fa64b736",
    "b1c7e2a4f9d0", "c3f1a9d27e80", "d4e5f6a7b8c9", "e1f2a3b4c5d6",
    "f2a3b4c5d6e7", "a1b2c3d4e5f6",
})


def _alembic_config() -> Config:
    # D2: single-source the DB URL — the boot migration path uses the same (env-overridable) URL the
    # app engine does, instead of alembic.ini's separate hardcoded literal drifting from database.py.
    from database import SQLALCHEMY_DATABASE_URL
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", SQLALCHEMY_DATABASE_URL)
    return cfg


def _current_stamp(engine) -> Optional[str]:
    """The DB's recorded alembic revision, or None if never stamped."""
    insp = inspect(engine)
    if "alembic_version" not in insp.get_table_names():
        return None
    with engine.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
    return row[0] if row else None


def _is_known_revision(cfg: Config, rev: str) -> bool:
    """True if `rev` resolves in the current migration scripts (baseline or later)."""
    script = ScriptDirectory.from_config(cfg)
    try:
        return script.get_revision(rev) is not None
    except Exception:
        return False


def _force_stamp(engine, rev: str) -> None:
    """Set alembic_version directly to `rev`.

    We cannot use `command.stamp` here: it resolves the DB's *current* revision to compute
    the stamp step, and a legacy DB's current id (a retired revision) no longer exists in the
    scripts, so alembic raises "Can't locate revision". Writing the version row directly is
    exactly what a stamp does at the storage level, and it sidesteps that resolution.
    """
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        ))
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": rev})


def _assert_schema_complete(engine) -> None:
    """Every table + column the models declare must already exist.

    Guards the reconcile path: we only stamp a legacy DB as up-to-date when its schema
    genuinely matches the models. A DB that is really behind (missing a table/column) must
    fail loud here instead of being marked current on a lie.
    """
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    gaps = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing:
            gaps.append(f"missing table '{table_name}'")
            continue
        cols = {c["name"] for c in insp.get_columns(table_name)}
        for col in table.columns:
            if col.name not in cols:
                gaps.append(f"missing column '{table_name}.{col.name}'")
    if gaps:
        raise RuntimeError(
            "Refusing to reconcile Alembic stamp: DB schema is incomplete relative to the "
            "models (" + "; ".join(gaps) + "). This DB is genuinely behind and needs a real "
            "migration path, not a stamp."
        )


def run_migrations(cfg: Optional[Config] = None) -> None:
    """Bring the schema to head. Raises on any failure (caller halts boot)."""
    cfg = cfg or _alembic_config()
    url = cfg.get_main_option("sqlalchemy.url")
    # ADR-037: match database.py — WAL + a generous busy_timeout so the leader's `command.upgrade`
    # can't deadlock against a follower worker opening the DB during a 4-worker boot (which hangs
    # run_migrations forever, before the seed ever runs). connect_args["timeout"] is the busy-wait.
    engine = create_engine(url, connect_args={"timeout": 30} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _mig_pragmas(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
    try:
        core_present = "artworks" in inspect(engine).get_table_names()
        if core_present:
            current = _current_stamp(engine)
            needs_reconcile = (
                current is None
                or current in RETIRED_REVISIONS
                or not _is_known_revision(cfg, current)
            )
            if needs_reconcile and current != BASELINE:
                _assert_schema_complete(engine)
                logger.warning(
                    "Reconciling legacy Alembic stamp %r -> %s (schema already present; "
                    "metadata-only stamp, no data touched).", current, BASELINE,
                )
                _force_stamp(engine, BASELINE)
        # Fresh DB -> builds baseline. Reconciled/at-head DB -> no-op. Post-baseline
        # migrations (once we add any) -> applied. Failure propagates.
        command.upgrade(cfg, "head")
        logger.info("Alembic migrations complete (schema at head).")
    finally:
        engine.dispose()
