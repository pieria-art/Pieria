"""Admin Backup & Restore — restore side (ADR-138's reflash path).

Three steps, each guarding the next: `validate_uploaded_archive` streams-in-place validates the tar
already written to `UPLOAD_PATH` WITHOUT touching anything live (extracts to `validated/`); `confirm`
decrypts secrets (if any) and stages the replacement into `staged/`; `core/restore_boot.py` swaps
`staged/` in at the next container start, single-process, before workers. See core/backup.py for the
archive format this reads and config.RESTORE_DIR for the shared directory both this module and
restore_boot.py use.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

import nacl.exceptions
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException

from config import RESTORE_DIR
from core.backup import FORMAT_NAME, FORMAT_VERSION, decrypt_secrets, sha256_file

logger = logging.getLogger("artwork-display-api.restore")

UPLOAD_PATH = RESTORE_DIR / "upload.tar"
VALIDATED_DIR = RESTORE_DIR / "validated"
STAGED_DIR = RESTORE_DIR / "staged"
PRE_RESTORE_DB = RESTORE_DIR / "pre-restore.db"
STATUS_FILE = RESTORE_DIR / "status.json"

_LIBRARY_BASENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
# manifest.json / artwork.db / secrets.enc are exact names; library/<basename> is the only prefixed form.
_ALLOWED_TOP_NAMES = {"manifest.json", "artwork.db", "secrets.enc"}

# Only these two tar types are ever extracted. tarfile.TarInfo.isreg()/isfile() are NOT safe for this —
# REGULAR_TYPES also includes GNUTYPE_SPARSE ('S'): a sparse member can declare a small archived size
# while its logical/extracted size is enormous (a 10KB tar member that expands to hundreds of MB on
# disk). CONTTYPE ('7', tar's rare "contiguous file" type) is excluded too — nothing this app produces
# uses it, so accepting it would only be attack surface.
_ALLOWED_TAR_TYPES = (tarfile.REGTYPE, tarfile.AREGTYPE)

# A "uploading"/"validating"/"staging" restore status stops counting as busy once its last heartbeat is
# this old — mirrors core.backup.STALE_AFTER (self-healing against a worker that died mid-step).
STALE_AFTER = timedelta(minutes=30)
_BUSY_STATES = {"uploading", "validating", "staging"}

# Extraction needs roughly 2x the uploaded bytes on disk at once (the tar itself, plus the extracted
# copy) before the tar is deleted at the end of confirm() — so the upload itself is capped at HALF of
# (free space - 1GB headroom), leaving room for its own extraction.
UPLOAD_HEADROOM_BYTES = 1024 * 1024 * 1024


def upload_cap_bytes(free_bytes: int) -> int:
    return max(0, (free_bytes - UPLOAD_HEADROOM_BYTES) // 2)


def _write_status(**fields) -> None:
    RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    fields.setdefault("updated_at", datetime.now(UTC).isoformat())
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fields))
    tmp.replace(STATUS_FILE)
    try:
        STATUS_FILE.chmod(0o600)
    except OSError:
        pass


def read_status() -> dict:
    try:
        status = json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {"state": "idle"}
    if status.get("state") in _BUSY_STATES:
        ts = status.get("updated_at")
        try:
            when = datetime.fromisoformat(ts) if ts else None
        except ValueError:
            when = None
        if when is None or datetime.now(UTC) - when > STALE_AFTER:
            return {"state": "idle"}
    return status


def is_busy() -> bool:
    return read_status().get("state") in _BUSY_STATES


def _secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _member_dest(name: str) -> Path:
    """Map a validated tar member name to its explicit extraction path. Raises ValueError for anything
    outside the allowlist — the caller never extracts a member it hasn't mapped through here. Also used
    to validate manifest.json's own `files[].path` entries (item 6) before they're ever joined."""
    if name in _ALLOWED_TOP_NAMES:
        return VALIDATED_DIR / name
    if name.startswith("library/"):
        basename = name[len("library/"):]
        if basename in (".", "..") or not _LIBRARY_BASENAME_RE.match(basename):
            raise ValueError(f"unsafe library member name: {name!r}")
        return VALIDATED_DIR / "library" / basename
    raise ValueError(f"unexpected archive member: {name!r}")


def _known_alembic_revision(rev: Optional[str]) -> bool:
    if not rev:
        return False
    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    try:
        return script.get_revision(rev) is not None
    except Exception:
        return False


def _extracted_db_stamp(db_path: Path) -> Optional[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def validate_uploaded_archive() -> dict:
    """Validate `UPLOAD_PATH` (already fully written to disk) and extract it member-by-member into
    VALIDATED_DIR. Raises HTTPException(400) with a clear message on any failure — the upload itself
    stays on disk (a retry doesn't need a fresh POST) until the next upload overwrites it, but
    VALIDATED_DIR is ALWAYS wiped on a failure (see the wrapper below): extraction runs well before the
    later checks (newer-stamp, checksum, table presence) that can fail, so a refused archive can still
    leave a fully-populated (if invalid) validated/ sitting there — confirm() must never be able to
    stage that."""
    if VALIDATED_DIR.exists():
        shutil.rmtree(VALIDATED_DIR)
    _secure_mkdir(VALIDATED_DIR)

    try:
        return _validate_extracted_archive()
    except BaseException:
        shutil.rmtree(VALIDATED_DIR, ignore_errors=True)
        raise


def _validate_extracted_archive() -> dict:
    try:
        tf = tarfile.open(UPLOAD_PATH, "r:")
    except tarfile.TarError as e:
        raise HTTPException(status_code=400, detail=f"not a valid backup archive: {e}")

    with tf:
        members = tf.getmembers()
        member_names = set()
        for m in members:
            if m.type not in _ALLOWED_TAR_TYPES:
                raise HTTPException(status_code=400,
                                     detail=f"archive contains a non-regular-file member: {m.name!r} "
                                            f"(type {m.type!r}) — sparse/symlink/hardlink/device/dir "
                                            f"members are refused")
            try:
                _member_dest(m.name)  # validates the name; raises before any path is ever joined
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            member_names.add(m.name)

        if "manifest.json" not in member_names:
            raise HTTPException(status_code=400, detail="archive is missing manifest.json")

        # Free-space check BEFORE extracting anything: sum the archive's own declared (logical) sizes —
        # trustworthy for a plain REGTYPE/AREGTYPE member (no sparse expansion possible, we already
        # refused those) — against free space, mirroring the upload cap's own headroom.
        total_declared = sum(m.size for m in members)
        try:
            free = shutil.disk_usage(RESTORE_DIR if RESTORE_DIR.exists() else RESTORE_DIR.parent).free
        except OSError:
            free = None
        if free is not None and total_declared > max(0, free - UPLOAD_HEADROOM_BYTES):
            raise HTTPException(status_code=400,
                                 detail="archive extracts to more data than there is free space for")

        for m in members:
            dest = _member_dest(m.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                raise HTTPException(status_code=400, detail=f"could not read member {m.name!r}")
            written = 0
            with src, dest.open("wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > m.size:
                        raise HTTPException(status_code=400,
                                             detail=f"member {m.name!r} read more bytes than its "
                                                    f"declared size — archive is malformed")
                    out.write(chunk)
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass

    manifest_path = VALIDATED_DIR / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="manifest.json is not valid JSON")

    if manifest.get("format") != FORMAT_NAME or manifest.get("format_version") != FORMAT_VERSION:
        raise HTTPException(status_code=400,
                             detail=f"not a Pieria backup (format={manifest.get('format')!r}, "
                                    f"format_version={manifest.get('format_version')!r})")

    # The manifest's `files` list must cover EXACTLY the set of extracted members besides manifest.json
    # itself — missing an entry OR listing one that isn't actually in the archive both refused. Every
    # entry's path is re-validated through the SAME member-name allowlist before any join (a manifest is
    # archive-supplied data, not trusted more than the tar members it describes).
    manifest_paths = set()
    for entry in manifest.get("files", []):
        path_str = entry.get("path", "")
        try:
            _member_dest(path_str)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"manifest.json lists an invalid path: {e}")
        manifest_paths.add(path_str)

    archive_paths = member_names - {"manifest.json"}
    if manifest_paths != archive_paths:
        missing = manifest_paths - archive_paths
        extra = archive_paths - manifest_paths
        detail = "manifest.json does not match the archive's contents exactly"
        if missing:
            detail += f" — missing: {sorted(missing)[:5]}"
        if extra:
            detail += f" — undeclared: {sorted(extra)[:5]}"
        raise HTTPException(status_code=400, detail=detail)

    for entry in manifest.get("files", []):
        path = VALIDATED_DIR / entry["path"]
        if sha256_file(path) != entry.get("sha256"):
            raise HTTPException(status_code=400, detail=f"checksum mismatch for {entry['path']} — "
                                                          f"archive is corrupt or was tampered with")

    db_path = VALIDATED_DIR / "artwork.db"
    # The alembic stamp check reads the EXTRACTED DB's own alembic_version table — not just the
    # manifest's claim (a manifest is archive-supplied data an attacker fully controls) — requires it
    # to be a revision this install's migrations actually know, AND requires it to equal what the
    # manifest claimed (catching a manifest that lies about the DB it's shipping).
    actual_rev = _extracted_db_stamp(db_path)
    claimed_rev = manifest.get("alembic_revision")
    if not _known_alembic_revision(actual_rev):
        raise HTTPException(status_code=400,
                             detail=f"this backup's schema revision ({actual_rev!r}) is unknown to "
                                    f"this install — it was made by a newer version of Pieria. Update "
                                    f"this app before restoring it.")
    if actual_rev != claimed_rev:
        raise HTTPException(status_code=400,
                             detail="manifest.json's alembic_revision does not match artwork.db's own "
                                    "stamp — archive is corrupt or was tampered with")

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    missing_tables = {"artworks", "settings"} - tables
    if missing_tables:
        raise HTTPException(status_code=400,
                             detail=f"artwork.db is missing required table(s): "
                                    f"{', '.join(sorted(missing_tables))}")

    has_secrets = bool(manifest.get("has_secrets")) and (VALIDATED_DIR / "secrets.enc").exists()
    summary = {
        "created_at": manifest.get("created_at"),
        "app_version": manifest.get("app_version"),
        "hostname": manifest.get("hostname"),
        "packs": manifest.get("packs", []),
        "conf": manifest.get("conf", {}),
        "has_secrets": has_secrets,
        "artwork_count": conn_artwork_count(db_path),
        "library_file_count": sum(1 for _ in (VALIDATED_DIR / "library").glob("*")) if
                               (VALIDATED_DIR / "library").is_dir() else 0,
    }
    _write_status(state="validated", **summary)
    return summary


def conn_artwork_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]
    finally:
        conn.close()


def confirm(passphrase: Optional[str], skip_secrets: bool) -> dict:
    """Stage the validated archive for boot-time apply. Decrypts secrets.enc into the staged DB copy
    when requested; a wrong passphrase raises HTTPException(400) and stages NOTHING (the validated copy
    is untouched, so the admin can just retry with the right passphrase).

    Requires status state == "validated" — NOT just "does validated/ look populated": a validation that
    ultimately failed (e.g. the newer-stamp/checksum/table checks, which all run AFTER extraction) can
    leave a fully-extracted-but-invalid validated/ behind; validate_uploaded_archive() now wipes it on
    any failure, but this check is the actual contract confirm() relies on, not an implementation detail
    of the caller it happens to clean up after."""
    status_state = read_status().get("state")
    if status_state != "validated":
        raise HTTPException(status_code=409,
                             detail=f"no validated backup ready to confirm (state={status_state!r}) — "
                                    f"upload one first")
    # The state transition into "staging" happens HERE, atomically with the check above (from this
    # function's point of view — there is only one admin on a LAN kiosk box, so a true CAS isn't
    # warranted). Any failure below reverts to "validated" (the validated copy is untouched by a
    # failure that happens before STAGED_DIR is touched), so a retry doesn't need a fresh upload.
    _write_status(state="staging")
    try:
        return _confirm_validated(passphrase, skip_secrets)
    except HTTPException:
        _write_status(state="validated")
        raise
    except Exception as e:  # noqa: BLE001 — never leave "staging" stuck on an unanticipated crash
        logger.error(f"[Restore] confirm failed unexpectedly: {e}", exc_info=True)
        _write_status(state="validated")
        raise HTTPException(status_code=400, detail="could not stage the restore — try again")


def _confirm_validated(passphrase: Optional[str], skip_secrets: bool) -> dict:
    manifest_path = VALIDATED_DIR / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=400, detail="no validated backup to confirm — upload one first")
    manifest = json.loads(manifest_path.read_text())
    db_path = VALIDATED_DIR / "artwork.db"
    has_secrets = bool(manifest.get("has_secrets")) and (VALIDATED_DIR / "secrets.enc").exists()

    if has_secrets and not skip_secrets:
        if not passphrase:
            raise HTTPException(status_code=400, detail="this backup has encrypted keys — enter the "
                                                          "passphrase or choose 'restore without keys'")
        blob = (VALIDATED_DIR / "secrets.enc").read_bytes()
        try:
            secrets_dict = decrypt_secrets(blob, passphrase)
        except (nacl.exceptions.CryptoError, ValueError, KeyError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="wrong passphrase — nothing was staged")
        conn = sqlite3.connect(str(db_path))
        try:
            for key, value in secrets_dict.items():
                conn.execute(
                    "INSERT INTO settings (setting_key, setting_value) VALUES (?, ?) "
                    "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                    (key, value))
            conn.commit()
        finally:
            conn.close()

    if STAGED_DIR.exists():
        shutil.rmtree(STAGED_DIR)
    _secure_mkdir(STAGED_DIR)
    shutil.move(str(db_path), str(STAGED_DIR / "artwork.db"))
    (STAGED_DIR / "artwork.db").chmod(0o600)
    library_src = VALIDATED_DIR / "library"
    if library_src.is_dir():
        shutil.move(str(library_src), str(STAGED_DIR / "library"))
        _secure_mkdir(STAGED_DIR / "library")
        for f in (STAGED_DIR / "library").iterdir():
            if f.is_file():
                try:
                    f.chmod(0o600)
                except OSError:
                    pass
    restore_json = {
        "packs": manifest.get("packs", []),
        "conf": manifest.get("conf", {}),
        "created_at": manifest.get("created_at"),
        "phase": "staged",
    }
    (STAGED_DIR / "restore.json").write_text(json.dumps(restore_json))
    (STAGED_DIR / "restore.json").chmod(0o600)
    shutil.rmtree(VALIDATED_DIR, ignore_errors=True)
    UPLOAD_PATH.unlink(missing_ok=True)

    _write_status(state="staged", staged_at=datetime.now(UTC).isoformat(),
                  packs=restore_json["packs"], conf=restore_json["conf"])
    return {"staged": True}
