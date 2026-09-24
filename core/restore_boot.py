"""Boot-time restore apply — invoked by docker-entrypoint.sh as `python -m core.restore_boot`, in
place of the old inline `python -c "import db_migrate; db_migrate.run_migrations()"`. Single-process,
before workers (ADR-037 — the same reason migrations moved to the entrypoint in the first place).

If an admin staged a restore (core/restore.py's confirm() wrote data/_restore/staged/restore.json), the
staged DB is swapped in BEFORE migrations run, so a restored DB that's behind head gets brought current
like any other legacy DB. A migration failure on the restored DB rolls back to the pre-restore DB and
re-migrates IT instead (ADR-035's fail-loud stays: if the rollback also fails, boot halts non-zero — a
broken restore must never silently strand the box on neither DB).

Resumable by construction: every step is gated on a `phase` recorded in restore.json (advanced and
persisted immediately after each step completes), so a crash/kill/power-loss between steps resumes at
the NEXT step on the following boot instead of redoing (or worse, re-swapping) a step that already
landed. `apply_pending_restore()` also self-heals the one state a crash mid-swap can leave behind:
pre-restore.db present with no live DB at all.
"""
import json
import logging
import shutil
import sqlite3
import sys
from pathlib import Path

import db_migrate
from config import ARTWORK_ROOT, RESTORE_DIR
from database import SQLALCHEMY_DATABASE_URL

logger = logging.getLogger("artwork-display-api.restore_boot")

STAGED_DIR = RESTORE_DIR / "staged"
PRE_RESTORE_DB = RESTORE_DIR / "pre-restore.db"
STATUS_FILE = RESTORE_DIR / "status.json"

# Phase order. Each `if phase == X` block below does X's work then advances+persists to the next name
# before falling through — so re-entering after a crash always resumes at the first phase whose work
# hasn't been recorded as done yet. "rolled_back" is a terminal-failure phase, not a step of the
# success chain — see _resume_rolled_back below.
_PHASES = ("staged", "swapped", "migrated", "library_copied", "settings_set", "rolled_back")


def _db_path() -> Path:
    prefix = "sqlite:///"
    if not SQLALCHEMY_DATABASE_URL.startswith(prefix):
        raise RuntimeError(f"restore boot-apply only supports a sqlite:/// URL, got "
                            f"{SQLALCHEMY_DATABASE_URL!r}")
    return Path(SQLALCHEMY_DATABASE_URL[len(prefix):])


def _checkpoint_and_drop_wal(db_path: Path) -> None:
    """Flush the WAL fully into the main DB file, then drop the -wal/-shm sidecars so the file we move
    aside is the complete, self-contained pre-restore snapshot (not just the base file)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _write_status(**fields) -> None:
    RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(fields))


def _marker_path() -> Path:
    """`STAGED_DIR / "restore.json"`, computed fresh on every call — deliberately NOT a precomputed
    module constant (that bit us once already: a constant derived from STAGED_DIR at import time goes
    stale the moment a test — or anything else — repoints STAGED_DIR later, since reassigning the
    module attribute doesn't retroactively update anything already computed from its old value)."""
    return STAGED_DIR / "restore.json"


def _read_marker() -> dict | None:
    marker = _marker_path()
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text())
    except (OSError, ValueError):
        # A marker that exists but won't parse is unrecoverable data, not "no restore pending" — fail
        # loud rather than silently discarding a staged restore.
        raise RuntimeError(f"{marker} exists but is not valid JSON — cannot safely resume or discard "
                           f"the staged restore")


def _write_marker(payload: dict) -> None:
    """Atomic (write-then-rename) so a crash mid-write can never leave a half-written, corrupt marker —
    the phase this function is recording either fully lands or the marker still reads the PREVIOUS
    phase, which is always a safe (if redundant) place to resume from."""
    STAGED_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STAGED_DIR / ".restore.json.tmp"
    tmp.write_text(json.dumps(payload))
    tmp.replace(_marker_path())


def _set_settings(db_path: Path, values: dict) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings (setting_key, setting_value) VALUES (?, ?) "
                "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                (key, value))
        conn.commit()
    finally:
        conn.close()


def _merge_library(staged_library: Path) -> None:
    """Different bind mounts (staged/ under ./data, _Library under ./Artwork) — copy, never rename.
    Overwrites a same-name file (the restore is a full replace). Idempotent — safe to re-run after a
    crash mid-copy, since every file is just overwritten again."""
    if not staged_library.is_dir():
        return
    dest = ARTWORK_ROOT / "_Library"
    dest.mkdir(parents=True, exist_ok=True)
    for f in staged_library.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name)


def _rollback_to_pre_restore(db_path: Path) -> None:
    """Move PRE_RESTORE_DB back over db_path — but ONLY if it still exists. Idempotent/resumable: once
    a prior attempt has already moved it back (PRE_RESTORE_DB gone), re-calling this must be a pure
    no-op on the file layer — never unlink db_path speculatively, or a second call after a successful
    first one would delete the only copy of the DB there is. Always re-runs migrations on whatever is
    at db_path now, so a transient failure gets retried on every boot until it clears."""
    if PRE_RESTORE_DB.exists():
        if db_path.exists():
            db_path.unlink()
        shutil.move(str(PRE_RESTORE_DB), str(db_path))
    db_migrate.run_migrations()


def _resume_rolled_back(payload: dict) -> None:
    """Terminal-failure phase: a previous boot already gave up on the restore and rolled back (or is
    mid-rollback). This NEVER touches library/packs/settings — a rolled-back restore never landed, so
    there's nothing of the new state to apply. Just make sure the live (original) DB is migrated and
    keep reporting failure; if the rollback migration itself still fails, halt boot (ADR-035)."""
    try:
        _rollback_to_pre_restore(_db_path())
    except Exception as rollback_err:
        logger.critical(f"[RestoreBoot] rollback migration still failing ({rollback_err}) — halting "
                        f"boot (ADR-035 fail-loud)")
        _write_status(state="failed", message=f"restore failed AND rollback failed: {rollback_err}")
        sys.exit(1)
    _write_status(state="failed",
                  message=f"restore migration failed; rolled back to the original DB (created_at="
                          f"{payload.get('created_at')!r})")
    shutil.rmtree(STAGED_DIR, ignore_errors=True)


def apply_pending_restore() -> None:
    """Advance a staged restore by exactly the phases that haven't landed yet, then migrate. Returns
    normally whether or not a restore was applied; the no-staged-restore path is a single plain
    run_migrations() call, unchanged from before this module existed."""
    db_path = _db_path()

    # Self-heal FIRST, independent of restore.json's state: a crash between moving the live DB aside
    # and moving the staged DB in leaves pre-restore.db as the only copy of anything. This must run
    # before we look at the marker at all, because the marker might already be gone (cleanup ran) while
    # this specific crash window can still have left the live DB missing in an unrelated way — in which
    # case there is nothing to serve requests, and having the original back is strictly better than not.
    if PRE_RESTORE_DB.exists() and not db_path.exists():
        logger.warning("[RestoreBoot] found pre-restore.db with no live DB — a previous apply crashed "
                       "mid-swap; putting the original DB back.")
        shutil.move(str(PRE_RESTORE_DB), str(db_path))

    marker = _read_marker()
    if marker is None:
        db_migrate.run_migrations()
        return

    payload = marker
    phase = payload.get("phase") or "staged"
    if phase not in _PHASES:
        phase = "staged"  # unrecognized/legacy marker — safest is to restart from the first phase
    staged_db = STAGED_DIR / "artwork.db"
    logger.warning(f"[RestoreBoot] Applying staged restore (phase={phase!r}, "
                   f"created_at={payload.get('created_at')!r})")

    if phase == "rolled_back":
        _resume_rolled_back(payload)
        return

    if phase == "staged":
        # NEVER overwrite an existing pre-restore.db — if one is already there, a prior attempt already
        # stashed the original and we must not clobber it with a (by now already-restored) live DB.
        if staged_db.exists():
            if db_path.exists() and not PRE_RESTORE_DB.exists():
                _checkpoint_and_drop_wal(db_path)
                shutil.move(str(db_path), str(PRE_RESTORE_DB))
            shutil.move(str(staged_db), str(db_path))
        # else: the swap already happened in a prior attempt that crashed before the marker was
        # advanced — db_path already holds the restored DB (or self-heal above just put it back);
        # nothing further to do for this phase.
        payload["phase"] = phase = "swapped"
        _write_marker(payload)

    just_migrated_this_call = False
    if phase == "swapped":
        try:
            db_migrate.run_migrations()
        except Exception as e:
            logger.error(f"[RestoreBoot] migrations failed on the restored DB ({e}) — rolling back to "
                         f"the pre-restore DB")
            # Persist the "rolled_back" phase BEFORE attempting the rollback migration itself. If THAT
            # also crashes (or this whole process gets killed mid-rollback), the marker must already
            # say "rolled_back" — re-entering at "swapped" would try to re-stash db_path into
            # PRE_RESTORE_DB (now possibly the wrong content) or, worse, unconditionally unlink db_path
            # believing a pre-restore copy still exists when it may already have been consumed.
            payload["phase"] = "rolled_back"
            _write_marker(payload)
            _resume_rolled_back(payload)
            return  # boot continues on the old (pre-restore) DB — see _resume_rolled_back
        payload["phase"] = phase = "migrated"
        _write_marker(payload)
        just_migrated_this_call = True

    if not just_migrated_this_call and phase in ("migrated", "library_copied", "settings_set"):
        # Resuming at a LATER phase from a PRIOR boot: the swap + first migration already succeeded
        # then, but this boot's own migrations were never run (a bare `if phase == "swapped"` only
        # fires the call above on the SAME call that just transitioned into "migrated" — a resumed
        # later phase skips it entirely). That's a real bug: e.g. a library-copy failure left the
        # marker at "migrated" forever, and a later `update-app` (shipping NEW migrations, with
        # SD_MIGRATIONS_DONE=1 already exported by the entrypoint) would then serve on a stale schema
        # with no boot ever calling run_migrations() again. Every boot must re-verify/advance the
        # schema regardless of phase — a no-op when already at head.
        db_migrate.run_migrations()

    if phase == "migrated":
        library_copy_error = None
        try:
            _merge_library(STAGED_DIR / "library")
        except OSError as e:
            # The DB itself is already fully restored and migrated at this point — only the library
            # copy failed (e.g. ENOSPC). Don't get stuck retrying forever: proceed through the rest of
            # the phases (pending packs/conf, cleanup) same as a full success, but record the honest
            # truth — the staged library files are about to be deleted with the rest of staging and
            # CANNOT be recovered from a retry; only a fresh restore attempt gets them back.
            logger.error(f"[RestoreBoot] library copy failed ({e}) — DB is restored; proceeding without "
                         f"the library (the staged library files will be discarded, not retried)")
            library_copy_error = str(e)
        payload["phase"] = phase = "library_copied"
        payload["library_copy_error"] = library_copy_error
        _write_marker(payload)

    if phase == "library_copied":
        _set_settings(db_path, {
            "restore_pending_packs": json.dumps(payload.get("packs") or []),
            "restore_pending_conf": json.dumps(payload.get("conf") or {}),
        })
        payload["phase"] = phase = "settings_set"
        _write_marker(payload)

    if phase == "settings_set":
        shutil.rmtree(STAGED_DIR, ignore_errors=True)
        PRE_RESTORE_DB.unlink(missing_ok=True)
        library_copy_error = payload.get("library_copy_error")
        if library_copy_error:
            _write_status(state="restored_partial", created_at=payload.get("created_at"),
                          message=f"restore applied, but copying the library failed and the staged "
                                  f"library files were discarded (not retried): {library_copy_error} — "
                                  f"some artwork files are missing; restore again to recover them")
            logger.warning("[RestoreBoot] Restore applied with a partial failure (library not copied); "
                           "migrations at head.")
        else:
            _write_status(state="restored", created_at=payload.get("created_at"))
            logger.warning("[RestoreBoot] Restore applied successfully; migrations at head.")


if __name__ == "__main__":
    apply_pending_restore()
