"""Regression tests for the Alembic single-source-of-truth boot path (ADR-035).

This is the coverage that was missing for months: nothing ever ran `alembic upgrade` from an
empty DB, so the empty-initial-migration + create_all drift stayed invisible (every fresh
install ended up stamped 9 revisions behind head). These tests lock in that:
  * migrations build the full schema from empty and reach head,
  * the built schema matches what the models declare (create_all),
  * a legacy DB stamped at a retired revision is reconciled without touching data,
  * a DB that is genuinely behind fails loud instead of being stamped current.
"""
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

import models  # noqa: F401  — registers all tables on Base.metadata
from database import Base
from db_migrate import BASELINE, RETIRED_REVISIONS, run_migrations


def _cfg(db_path: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _stamp(db_path: str):
    eng = create_engine(f"sqlite:///{db_path}")
    if "alembic_version" not in inspect(eng).get_table_names():
        return None
    with eng.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _set_legacy_stamp(db_path: str, rev: str) -> None:
    """Write alembic_version directly — command.stamp refuses retired (nonexistent) revs,
    which is exactly the state real deployed boxes are in."""
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": rev})


def _head(db_path: str) -> str:
    return ScriptDirectory.from_config(_cfg(db_path)).get_current_head()


def _columns(db_path: str) -> dict:
    insp = inspect(create_engine(f"sqlite:///{db_path}"))
    return {
        t: {c["name"] for c in insp.get_columns(t)}
        for t in insp.get_table_names() if t != "alembic_version"
    }


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "artwork.db")


def test_single_head_and_baseline_anchors_the_chain(db_path):
    """Exactly one head (guards against a stray/branching migration) and the baseline still anchors
    the chain. As post-baseline migrations are added the head moves past BASELINE — that's expected."""
    script = ScriptDirectory.from_config(_cfg(db_path))
    assert len(script.get_heads()) == 1
    assert script.get_revision(BASELINE) is not None


def test_fresh_upgrade_from_empty_reaches_head_and_matches_models(db_path, tmp_path):
    """Empty DB -> `upgrade head` builds the full schema and stamps the baseline.
    Then the migration-built schema must equal the models' create_all schema (zero drift).
    This is the exact check whose absence let the original drift ship."""
    command.upgrade(_cfg(db_path), "head")
    assert _stamp(db_path) == _head(db_path)

    ref = str(tmp_path / "ref.db")
    Base.metadata.create_all(create_engine(f"sqlite:///{ref}"))
    assert _columns(db_path) == _columns(ref)


def test_run_migrations_builds_fresh_db(db_path):
    run_migrations(_cfg(db_path))
    assert _stamp(db_path) == _head(db_path)
    cols = _columns(db_path)
    assert "artworks" in cols and "is_personal" in cols["artworks"]


@pytest.mark.parametrize("legacy_rev", ["a1b2c3d4e5f6", "940d71e4b7bf"])
def test_reconcile_from_retired_stamp(db_path, legacy_rev):
    """A create_all-built DB stamped at a retired revision (the Pi, and mis-stamped fresh
    installs) is re-stamped to the baseline — no error despite the id no longer existing."""
    assert legacy_rev in RETIRED_REVISIONS
    Base.metadata.create_all(create_engine(f"sqlite:///{db_path}"))
    _set_legacy_stamp(db_path, legacy_rev)

    run_migrations(_cfg(db_path))
    assert _stamp(db_path) == _head(db_path)


def test_reconcile_preserves_data(db_path):
    """Reconciling the Pi's exact state (a1b2c3d4e5f6 + real rows) must not touch data."""
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO artworks (filename, original_width, original_height, affinity_score, "
            "skip_count, total_display_time, is_seed, is_personal, status, focal_x, focal_y) "
            "VALUES ('keep.jpg',1,1,1.0,0,0,0,0,'approved',0.5,0.5)"
        ))
    _set_legacy_stamp(db_path, "a1b2c3d4e5f6")

    run_migrations(_cfg(db_path))
    assert _stamp(db_path) == _head(db_path)
    with eng.connect() as conn:
        assert conn.execute(text("SELECT filename FROM artworks")).scalar() == "keep.jpg"


def test_incomplete_schema_fails_loud(db_path):
    """A DB genuinely behind the models (missing a table) must raise, not be stamped current
    on a lie."""
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE subscriptions"))
    _set_legacy_stamp(db_path, "a1b2c3d4e5f6")

    with pytest.raises(RuntimeError):
        run_migrations(_cfg(db_path))


def test_run_migrations_is_idempotent(db_path):
    run_migrations(_cfg(db_path))
    run_migrations(_cfg(db_path))  # second run is a no-op, must not raise
    assert _stamp(db_path) == _head(db_path)


def test_playback_session_unique_constraint_present(db_path):
    """A8: upgrading to head yields a UNIQUE(display_id, playlist_id) on the playback sessions table."""
    command.upgrade(_cfg(db_path), "head")
    insp = inspect(create_engine(f"sqlite:///{db_path}"))
    ucs = insp.get_unique_constraints("display_playback_sessions")
    assert any(set(uc["column_names"]) == {"display_id", "playlist_id"} for uc in ucs)


def test_playback_session_rejects_duplicate(db_path):
    """A8: the constraint actually enforces one row per (display, playlist) — a duplicate insert raises,
    which is what makes the get_next_image get-or-create's IntegrityError re-query path fire."""
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session as _Session

    from models import DisplayPlaybackSessionModel
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    with _Session(eng) as s:
        s.add(DisplayPlaybackSessionModel(display_id="wall", playlist_id=1)); s.commit()
        s.add(DisplayPlaybackSessionModel(display_id="wall", playlist_id=1))
        with pytest.raises(IntegrityError):
            s.commit()
