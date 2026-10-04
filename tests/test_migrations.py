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


def test_0004_backfills_existing_gallery_links(db_path):
    """A Gallery installed BEFORE the source-collection link existed gets linked to its Collection on
    upgrade (matched by name -> pack subscription). Guards the real deploy path (populated 0003 -> head)."""
    cfg = _cfg(db_path)
    command.upgrade(cfg, "0003_display_now_playing")  # pre-link schema
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as c:
        c.execute(text("INSERT INTO subscriptions (url,title,enabled,trust,item_count,created_at) "
                       "VALUES ('pack:masterpieces','Masterpieces',1,'verified',40,'2026-07-18')"))
        for name in ("Masterpieces", "My Faves"):  # one auto-seeded from the pack, one custom
            c.execute(text("INSERT INTO playlists (name,display_time,default_mode,shuffle,is_personal,"
                           "placard_initial_wait_sec,placard_initial_show_sec,placard_interaction_show_sec) "
                           f"VALUES ('{name}',30,'ken-burns',0,0,5,15,10)"))
    command.upgrade(cfg, "head")  # 0004 upgrade() incl. backfill
    with eng.connect() as c:
        rows = dict(c.execute(text("SELECT name, source_subscription_id FROM playlists")).fetchall())
    eng.dispose()
    assert rows["Masterpieces"] == 1   # seeded from the pack -> linked
    assert rows["My Faves"] is None    # user-built -> untouched


def test_unknown_nonretired_stamp_is_left_untouched_and_unmigrated(db_path, caplog, monkeypatch):
    """A stamp this code has never heard of = DB from a NEWER release (sd-update rollback). Leave the
    stamp alone, don't upgrade, don't raise, warn."""
    import logging

    import db_migrate
    Base.metadata.create_all(create_engine(f"sqlite:///{db_path}"))
    _set_legacy_stamp(db_path, "ffffdeadbeef")
    assert "ffffdeadbeef" not in RETIRED_REVISIONS

    def _no_upgrade(*a, **k):
        raise AssertionError("command.upgrade must not run for a newer-release DB")
    monkeypatch.setattr(db_migrate.command, "upgrade", _no_upgrade)

    with caplog.at_level(logging.WARNING, logger="artwork-display-api.db_migrate"):
        run_migrations(_cfg(db_path))
    assert _stamp(db_path) == "ffffdeadbeef"
    assert any("ffffdeadbeef" in r.message and "newer release" in r.message for r in caplog.records)


def test_broken_migration_file_fails_loud(db_path, tmp_path):
    """An unimportable migration script must make run_migrations RAISE, not read the real baseline stamp
    as 'unknown/newer' and boot on the old schema (ADR-035)."""
    import shutil
    scripts = tmp_path / "migrations"
    shutil.copytree("migrations", scripts, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts / "versions" / "9999_broken.py").write_text("def upgrade(:\n")
    Base.metadata.create_all(create_engine(f"sqlite:///{db_path}"))
    _set_legacy_stamp(db_path, BASELINE)
    cfg = _cfg(db_path)
    cfg.set_main_option("script_location", str(scripts))
    with pytest.raises(Exception):
        run_migrations(cfg)
    assert _stamp(db_path) == BASELINE


def test_0008_api_tokens_upgrades_a_populated_0007_db(db_path):
    """The real deploy path: a populated 0007 DB gains api_tokens + active_displays.kind, keeps its rows,
    and reaches head (0009 is head)."""
    cfg = _cfg(db_path)
    command.upgrade(cfg, "0007_artwork_attribution")
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as c:
        c.execute(text("INSERT INTO active_displays (display_id, last_seen_at) VALUES ('wall','2026-10-01')"))
    command.upgrade(cfg, "head")
    assert _stamp(db_path) == _head(db_path)
    cols = _columns(db_path)
    assert {"id", "name", "token_hash", "scopes", "created_at", "last_used_at", "revoked_at"} <= cols["api_tokens"]
    assert "kind" in cols["active_displays"]
    insp = inspect(create_engine(f"sqlite:///{db_path}"))   # fresh engine: a reused one serves cached index info
    assert any(ix["unique"] and ix["column_names"] == ["token_hash"] for ix in insp.get_indexes("api_tokens"))
    with eng.connect() as c:
        assert c.execute(text("SELECT display_id, kind FROM active_displays")).fetchall() == [("wall", None)]
    eng.dispose()


def test_0008_is_idempotent_on_a_create_all_db(db_path):
    """A create_all-built DB already has the table + column; the guarded upgrade must skip them."""
    Base.metadata.create_all(create_engine(f"sqlite:///{db_path}"))
    _set_legacy_stamp(db_path, "0007_artwork_attribution")
    command.upgrade(_cfg(db_path), "head")
    assert _stamp(db_path) == _head(db_path)


def test_newer_stamp_logic_still_reconciles_with_0008_as_head(db_path):
    """A retired-stamped legacy DB (schema complete incl. the new table/column) is reconciled to the
    baseline and then upgraded through every migration to the new head without touching data."""
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO api_tokens (name, token_hash, scopes, created_at) "
                          "VALUES ('keep','h','read','2026-10-01')"))
    _set_legacy_stamp(db_path, "a1b2c3d4e5f6")
    run_migrations(_cfg(db_path))
    assert _stamp(db_path) == _head(db_path)
    with eng.connect() as conn:
        assert conn.execute(text("SELECT name FROM api_tokens")).scalar() == "keep"


def test_0009_pack_refresh_tracking_upgrades_a_populated_0008_db(db_path):
    """ADR-148 F8: a populated 0008 DB gains artworks.user_edited_fields (existing rows read '[]') and
    subscriptions.applied_manifest_hash (NULL), keeping its data."""
    cfg = _cfg(db_path)
    command.upgrade(cfg, "0008_api_tokens")
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as c:
        c.execute(text("INSERT INTO artworks (filename, status, original_width, original_height, affinity_score, skip_count, total_display_time, is_seed, is_personal, focal_x, focal_y) VALUES ('a.jpg','approved',0,0,1.0,0,0,0,0,0.5,0.5)"))
        c.execute(text("INSERT INTO subscriptions (url, trust, enabled, item_count, created_at) VALUES ('pack:x','verified',1,0,'2026-10-01')"))
    command.upgrade(cfg, "head")
    assert _stamp(db_path) == _head(db_path)
    with eng.connect() as c:
        assert c.execute(text("SELECT filename, user_edited_fields FROM artworks")).fetchall() == [("a.jpg", "[]")]
        assert c.execute(text("SELECT url, applied_manifest_hash FROM subscriptions")).fetchall() == [("pack:x", None)]
    eng.dispose()


def test_0009_is_idempotent_on_a_create_all_db(db_path):
    Base.metadata.create_all(create_engine(f"sqlite:///{db_path}"))
    _set_legacy_stamp(db_path, "0008_api_tokens")
    command.upgrade(_cfg(db_path), "head")
    assert _stamp(db_path) == _head(db_path)


def test_0010_adds_metadata_refreshed_at(db_path):
    cfg = _cfg(db_path)
    command.upgrade(cfg, "0009_pack_refresh_tracking")
    command.upgrade(cfg, "head")
    assert _stamp(db_path) == _head(db_path)
    assert "metadata_refreshed_at" in _columns(db_path)["subscriptions"]


def test_0011_adds_key_pinning_columns_from_0010(db_path):
    cfg = _cfg(db_path)
    command.upgrade(cfg, "0010_pack_metadata_refreshed_at")
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as c:
        c.execute(text("INSERT INTO subscriptions (url, trust, enabled, item_count, created_at) VALUES ('https://x.test/m.json','community',1,0,'2026-10-01')"))
    command.upgrade(cfg, "head")
    assert _stamp(db_path) == _head(db_path)
    assert {"pinned_public_key", "key_status", "pending_public_key"} <= _columns(db_path)["subscriptions"]
    with eng.connect() as c:
        assert tuple(c.execute(text("SELECT key_status, pinned_public_key FROM subscriptions")).one()) == ("ok", None)


def test_0012_adds_applied_generated_at_through_0011(db_path):
    """Populated 0010 DB goes 0010 -> 0011 -> 0012 as one chain."""
    cfg = _cfg(db_path)
    command.upgrade(cfg, "0010_pack_metadata_refreshed_at")
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as c:
        c.execute(text("INSERT INTO subscriptions (url, trust, enabled, item_count, created_at) VALUES ('pack:x','verified',1,0,'2026-10-01')"))
    command.upgrade(cfg, "head")
    assert _stamp(db_path) == _head(db_path) == "0012_pack_applied_generated_at"
    cols = _columns(db_path)["subscriptions"]
    assert {"pinned_public_key", "applied_generated_at"} <= cols
    with eng.connect() as c:
        assert c.execute(text("SELECT applied_generated_at FROM subscriptions")).scalar() is None
