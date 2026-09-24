"""Admin Backup & Restore — build side (ADR-138's reflash path: a full-replace backup/restore so a
flashed Pi can come back with its library + settings, not just its art).

This module builds the downloadable archive: an online snapshot of the DB (secrets always stripped),
the non-pack `_Library` masters, and an optional passphrase-encrypted secrets blob. See core/restore.py
for the consuming half and core/restore_boot.py for the boot-time apply. Both backup and restore share
one `data/_backups` / `data/_restore` split (config.BACKUP_DIR / config.RESTORE_DIR) rather than
in-memory state, because the appliance runs 2 uvicorn workers and either one may serve a given request.
"""
import fcntl
import hashlib
import json
import logging
import re
import shutil
import socket
import sqlite3
import tarfile
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

import nacl.exceptions
import nacl.pwhash
import nacl.secret
import nacl.utils

import config
import host_health
from config import APP_VERSION, ARTWORK_ROOT, BACKUP_DIR, LIBRARY_DIR
from database import SQLALCHEMY_DATABASE_URL

logger = logging.getLogger("artwork-display-api.backup")

FORMAT_NAME = "pieria-backup"
FORMAT_VERSION = 1

# Deliberately narrow — this becomes a filesystem path element (data/_backups/<token>.tar) and is also
# echoed back verbatim by GET /api/backup/download/<token>, so it's checked with this exact regex
# (via .fullmatch — not .match, which would happily accept a valid prefix followed by garbage)
# BEFORE any path is built from it (no path joins on unvalidated input).
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")

# A ready archive is single-use (deleted after its one download) but may never be collected if the
# admin closes the tab — swept on any backup/restore endpoint call and at startup so a stale tar
# doesn't sit on a Pi's small SD card forever.
ARCHIVE_TTL = timedelta(hours=1)

# A "building"/"redownloading_packs" status stops counting as busy once its last heartbeat
# (`updated_at`) is this old — a worker that died mid-build without a chance to write state=error must
# not wedge the feature forever behind a permanent 409.
STALE_AFTER = timedelta(minutes=30)

STATUS_FILE = BACKUP_DIR / "status.json"
_BUSY_STATES = {"building"}


def _build_lock_path() -> Path:
    """`BACKUP_DIR / ".build.lock"`, computed fresh on every call — NOT a precomputed module constant.
    A constant derived from BACKUP_DIR at import time goes stale the moment BACKUP_DIR is repointed
    later (tests monkeypatch it; reassigning a module attribute doesn't retroactively fix up anything
    already computed from its old value) — this is what core/restore_boot.py's `_marker_path()` fix
    was for too."""
    return BACKUP_DIR / ".build.lock"

# Settings keys that are secrets. The named ones are today's actual secret settings; the regex is a
# defensive net for anything shaped like a secret that gets added later and forgotten here — belt and
# braces, since a forgotten key would otherwise ship in plaintext inside the DB copy.
SECRET_SETTINGS_KEYS = {
    "ai_api_key", "harvard_api_key", "smithsonian_api_key", "europeana_api_key",
    "publisher_private_key",
}
_SECRET_KEY_RE = re.compile(r"(api_key|private_key|token|secret|password)$", re.IGNORECASE)

# Non-secret device conf mirrored into the archive manifest (appliance only). Anything else in
# conf.json — including the appliance token and DISPLAY_ID — never travels with the backup.
_CONF_KEYS = ("TIMEZONE", "ROTATE", "EINK_ORIENTATION", "WATCHDOG", "OS_UPDATE_SCHEDULE", "OS_UPDATE_TIME")

_LIBRARY_BASENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def _is_secret_key(key: str) -> bool:
    return key in SECRET_SETTINGS_KEYS or bool(_SECRET_KEY_RE.search(key))


def _db_path() -> Path:
    """The live DB file, resolved the same way db_migrate/database.py do (D2: single URL source)."""
    prefix = "sqlite:///"
    if not SQLALCHEMY_DATABASE_URL.startswith(prefix):
        raise RuntimeError(f"backup only supports a sqlite:/// URL, got {SQLALCHEMY_DATABASE_URL!r}")
    return Path(SQLALCHEMY_DATABASE_URL[len(prefix):])


def _host_label() -> str:
    """`<hostname-or-display>` for the archive filename. Prefers the appliance's own DISPLAY_ID (more
    meaningful than a container hostname) when set; falls back to the OS hostname."""
    if config.IS_APPLIANCE:
        conf = host_health.read_conf() or {}
        display_id = (conf.get("values") or {}).get("DISPLAY_ID")
        if display_id:
            return re.sub(r"[^A-Za-z0-9_-]+", "-", str(display_id)).strip("-")[:40] or "pieria"
    host = socket.gethostname() or "pieria"
    return re.sub(r"[^A-Za-z0-9_-]+", "-", host).strip("-")[:40] or "pieria"


def _ensure_backup_dir() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        BACKUP_DIR.chmod(0o700)
    except OSError:
        pass


def _write_status(**fields) -> None:
    _ensure_backup_dir()
    fields.setdefault("updated_at", datetime.now(UTC).isoformat())
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fields))
    tmp.replace(STATUS_FILE)
    try:
        STATUS_FILE.chmod(0o600)
    except OSError:
        pass


def read_status() -> dict:
    """The current build status — self-healing: a 'building' status whose last heartbeat is older than
    STALE_AFTER is reported as idle, so a worker that died mid-build (no chance to write state=error)
    can't wedge the feature behind a permanent 409 forever. The cross-worker lock (see
    _acquire_build_lock) is the primary guard; this is defense-in-depth for whatever it misses."""
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


def is_building() -> bool:
    """Cross-worker: a stale status.json lies, but the flock releases the instant the holding process
    dies (crash, OOM-kill, container restart) — try to acquire the same lock non-blocking; if we can,
    nobody is building."""
    if read_status().get("state") != "building":
        return False
    if not _build_lock_path().exists():
        return False
    try:
        with open(_build_lock_path(), "r+") as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def sweep_expired() -> None:
    """Delete any ready-but-uncollected archive older than ARCHIVE_TTL, plus its sidecar, AND any
    leftover build temp files (`.{token}.tar.tmp`, an interrupted TemporaryDirectory that outlived its
    `with` block because the process was killed mid-build) so a crashed build doesn't eat SD card
    space forever. Best-effort — a sweep failure must never block the endpoint that triggered it."""
    if not BACKUP_DIR.is_dir():
        return
    now = time.time()
    try:
        for sidecar in BACKUP_DIR.glob("*.json"):
            if sidecar.name in ("status.json",) or sidecar.name.endswith(".tmp"):
                continue
            token = sidecar.stem
            tar_path = BACKUP_DIR / f"{token}.tar"
            try:
                age = now - sidecar.stat().st_mtime
            except OSError:
                continue
            if age > ARCHIVE_TTL.total_seconds():
                sidecar.unlink(missing_ok=True)
                tar_path.unlink(missing_ok=True)
                # status.json never carries a token (N7: minted only in the POST /api/backup response —
                # see build_backup), so there's nothing token-specific to correlate here. Any archive
                # expiring means there's no longer a downloadable backup, so a stale "ready" is
                # downgraded to idle — a fresh dict, so nothing stale survives regardless.
                if read_status().get("state") == "ready":
                    _write_status(state="idle")
        # Orphaned build temp files (only safe to remove when nobody currently holds the build lock).
        if not is_building():
            for stale in list(BACKUP_DIR.glob(".*.tar.tmp")) + list(BACKUP_DIR.glob("tmp*")):
                try:
                    age = now - stale.stat().st_mtime
                except OSError:
                    continue
                if age > ARCHIVE_TTL.total_seconds():
                    if stale.is_dir():
                        shutil.rmtree(stale, ignore_errors=True)
                    else:
                        stale.unlink(missing_ok=True)
            # Defense-in-depth: a `<token>.tar` with no matching `<token>.json` sidecar (the loop above
            # only ever finds archives by walking sidecars, so an orphaned tar is otherwise invisible to
            # it — build_backup now writes the sidecar first specifically to make this rare, but a
            # pre-existing/legacy orphan or an exotic crash window could still leave one).
            for tar_path in BACKUP_DIR.glob("*.tar"):
                sidecar = BACKUP_DIR / f"{tar_path.stem}.json"
                if sidecar.exists():
                    continue
                try:
                    age = now - tar_path.stat().st_mtime
                except OSError:
                    continue
                if age > ARCHIVE_TTL.total_seconds():
                    tar_path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"[Backup] sweep_expired: {e}")


def _acquire_build_lock():
    """Cross-worker, cross-process exclusive lock for the build itself (not just the status flag) — the
    appliance runs 2 uvicorn workers, and only a real OS-level lock (auto-released if the holder dies)
    can stop two of them from building at once. Returns an open file object to hold for the build's
    duration (release via _release_build_lock), or None if another build already holds it."""
    _ensure_backup_dir()
    lock_path = _build_lock_path()
    fd = open(lock_path, "w")
    try:
        lock_path.chmod(0o600)
    except OSError:
        pass
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return None
    return fd


def _release_build_lock(fd) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        fd.close()


def _pack_owned_filenames() -> tuple[set[str], str]:
    """Filenames under _Library that belong to an installed pack (subscriptions.url == 'pack:<cid>').
    Returns (owned, method) where method is 'manifest' if every installed pack's manifest listed its
    own filenames, or 'prefix-fallback' if at least one pack's manifest didn't list filenames and the
    `<cid>__` prefix convention was used for it instead (tools/build_pack.py's naming, verified against
    Artwork/_manifests/masterpieces.json's items[].image.local_file)."""
    engine_path = _db_path()
    owned: set[str] = set()
    method = "manifest"
    if not engine_path.exists():
        return owned, method
    conn = sqlite3.connect(str(engine_path))
    try:
        cids = [row[0][len("pack:"):] for row in conn.execute(
            "SELECT url FROM subscriptions WHERE url LIKE 'pack:%'")]
    except sqlite3.OperationalError:
        cids = []
    finally:
        conn.close()

    manifests_dir = ARTWORK_ROOT / "_manifests"
    for cid in cids:
        man_path = manifests_dir / f"{cid}.json"
        listed = set()
        try:
            manifest = json.loads(man_path.read_text())
            for item in manifest.get("items", []):
                local_file = (item.get("image") or {}).get("local_file")
                if local_file:
                    listed.add(local_file)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if listed:
            owned |= listed
        else:
            method = "prefix-fallback"
            prefix = f"{cid}__"
            owned |= {f.name for f in LIBRARY_DIR.glob(f"{prefix}*") if f.is_file() and not f.is_symlink()}
    return owned, method


def _library_files_to_include() -> list[Path]:
    """Every regular, non-symlink file directly under _Library that isn't owned by an installed pack
    (My Photos, catalog uploads/adds) — pack images are re-downloaded after restore instead."""
    if not LIBRARY_DIR.is_dir():
        return []
    owned, _method = _pack_owned_filenames()
    return sorted(
        f for f in LIBRARY_DIR.iterdir()
        if f.is_file() and not f.is_symlink() and f.name not in owned
    )


def _estimate_size_bytes() -> int:
    total = 0
    db = _db_path()
    if db.exists():
        total += db.stat().st_size
    for f in _library_files_to_include():
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def free_space_ok(estimated_bytes: int, target_dir: Path) -> bool:
    """estimated size + 10% + 200MB headroom must fit on target_dir's filesystem."""
    required = int(estimated_bytes * 1.1) + 200 * 1024 * 1024
    try:
        free = shutil.disk_usage(target_dir if target_dir.exists() else target_dir.parent).free
    except OSError:
        return True  # can't verify — don't block on a check that itself failed
    return free >= required


def _snapshot_db(dest: Path) -> None:
    """Online copy via sqlite3.Connection.backup() — WAL-safe, same approach as sd-update's SNAP_PY."""
    src = sqlite3.connect(str(_db_path()))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _strip_secrets_and_vacuum(db_path: Path) -> dict:
    """Delete every secret setting row from the (already-snapshotted, throwaway) DB copy and VACUUM so
    the deleted bytes don't linger in free pages. Returns {key: value} of what was removed (for the
    caller to optionally encrypt into secrets.enc) — never read from the archived copy again."""
    conn = sqlite3.connect(str(db_path))
    removed = {}
    try:
        rows = conn.execute("SELECT setting_key, setting_value FROM settings").fetchall()
        for key, value in rows:
            if _is_secret_key(key):
                removed[key] = value
        if removed:
            conn.executemany("DELETE FROM settings WHERE setting_key = ?",
                              [(k,) for k in removed])
            conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return removed


def _current_alembic_stamp(db_path: Path) -> Optional[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _read_conf_subset() -> dict:
    if not config.IS_APPLIANCE:
        return {}
    conf = host_health.read_conf() or {}
    values = conf.get("values") or {}
    return {k: values[k] for k in _CONF_KEYS if k in values}


def _installed_pack_ids(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[0][len("pack:"):] for row in conn.execute(
            "SELECT url FROM subscriptions WHERE url LIKE 'pack:%' ORDER BY url")]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# --- Encryption (PyNaCl argon2id + SecretBox) --------------------------------------------------------
# OPSLIMIT/MEMLIMIT_INTERACTIVE, not _SENSITIVE — this runs on Pi-class RAM (the hardware profile), and
# INTERACTIVE is still a real KDF cost, not a token-bucket rate limit.
def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return nacl.pwhash.argon2id.kdf(
        nacl.secret.SecretBox.KEY_SIZE, passphrase.encode("utf-8"), salt,
        opslimit=nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE,
        memlimit=nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE,
    )


def encrypt_secrets(secrets_dict: dict, passphrase: str) -> bytes:
    salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
    key = _derive_key(passphrase, salt)
    box = nacl.secret.SecretBox(key)
    ciphertext = box.encrypt(json.dumps(secrets_dict).encode("utf-8"))
    blob = {
        "kdf": "argon2id",
        "opslimit": nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE,
        "memlimit": nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE,
        "salt": salt.hex(),
        "ciphertext": ciphertext.hex(),
    }
    return json.dumps(blob).encode("utf-8")


def decrypt_secrets(blob: bytes, passphrase: str) -> dict:
    """Raises nacl.exceptions.CryptoError on a wrong passphrase (or corrupt blob) — the caller turns
    that into a 400 and stages nothing."""
    data = json.loads(blob.decode("utf-8"))
    salt = bytes.fromhex(data["salt"])
    key = nacl.pwhash.argon2id.kdf(
        nacl.secret.SecretBox.KEY_SIZE, passphrase.encode("utf-8"), salt,
        opslimit=int(data["opslimit"]), memlimit=int(data["memlimit"]),
    )
    box = nacl.secret.SecretBox(key)
    plaintext = box.decrypt(bytes.fromhex(data["ciphertext"]))
    return json.loads(plaintext.decode("utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_backup(include_secrets: bool, passphrase: Optional[str], token: str) -> None:
    """Synchronous archive build — run in a background thread by the router. `token` is minted by the
    CALLER (the POST /api/backup handler) and returned ONLY in that response — never written to
    status.json (N: a bare GET /api/backup/status must never hand out a bearer token for the archive;
    see routers/backup.py's create_backup). Writes progress to STATUS_FILE throughout so either worker
    can answer GET /api/backup/status. Holds the cross-worker build lock for its entire duration; any
    exception (not just the ones we anticipated) always leaves status.json in a terminal state and
    always releases the lock — a build can never wedge the feature."""
    lock_fd = _acquire_build_lock()
    if lock_fd is None:
        logger.warning("[Backup] build_backup called while another build holds the lock — skipping")
        return
    try:
        try:
            _ensure_backup_dir()
            _write_status(state="building", progress=5, message="checking free space")
            estimated = _estimate_size_bytes()
            if not free_space_ok(estimated, BACKUP_DIR):
                _write_status(state="error", message="not enough free space to build a backup safely")
                return

            tmp_tar = BACKUP_DIR / f".{token}.tar.tmp"
            library_files = _library_files_to_include()

            with tempfile.TemporaryDirectory(dir=BACKUP_DIR) as tmp:
                tmp_db = Path(tmp) / "artwork.db"
                _write_status(state="building", progress=15, message="snapshotting database")
                _snapshot_db(tmp_db)
                secrets_removed = _strip_secrets_and_vacuum(tmp_db)
                alembic_rev = _current_alembic_stamp(tmp_db)

                secrets_blob_path = None
                if include_secrets and secrets_removed:
                    _write_status(state="building", progress=35, message="encrypting keys")
                    secrets_blob_path = Path(tmp) / "secrets.enc"
                    secrets_blob_path.write_bytes(encrypt_secrets(secrets_removed, passphrase))

                _write_status(state="building", progress=45, message="writing manifest")
                files_meta = [{"path": "artwork.db", "size": tmp_db.stat().st_size,
                               "sha256": sha256_file(tmp_db)}]
                for f in library_files:
                    files_meta.append({"path": f"library/{f.name}", "size": f.stat().st_size,
                                        "sha256": sha256_file(f)})
                if secrets_blob_path:
                    files_meta.append({"path": "secrets.enc", "size": secrets_blob_path.stat().st_size,
                                        "sha256": sha256_file(secrets_blob_path)})

                manifest = {
                    "format": FORMAT_NAME, "format_version": FORMAT_VERSION,
                    "app_version": APP_VERSION, "alembic_revision": alembic_rev,
                    "created_at": datetime.now(UTC).isoformat(),
                    "hostname": _host_label(),
                    "packs": _installed_pack_ids(tmp_db),
                    "conf": _read_conf_subset(),
                    "has_secrets": secrets_blob_path is not None,
                    "files": files_meta,
                }
                manifest_path = Path(tmp) / "manifest.json"
                manifest_path.write_text(json.dumps(manifest))

                _write_status(state="building", progress=60, message="writing archive")
                with tarfile.open(tmp_tar, "w:") as tf:
                    tf.add(manifest_path, arcname="manifest.json")
                    tf.add(tmp_db, arcname="artwork.db")
                    for i, f in enumerate(library_files):
                        tf.add(f, arcname=f"library/{f.name}")
                        if i % 200 == 0:
                            pct = 60 + int(30 * (i + 1) / max(1, len(library_files)))
                            _write_status(state="building", progress=min(90, pct),
                                          message=f"packing library ({i + 1}/{len(library_files)})")
                    if secrets_blob_path:
                        tf.add(secrets_blob_path, arcname="secrets.enc")

            filename = f"pieria-backup-{_host_label()}-{datetime.now(UTC).strftime('%Y%m%d-%H%M')}.tar"
            final_tar = BACKUP_DIR / f"{token}.tar"
            # Sidecar FIRST, then the tar — never the reverse. download_backup() requires BOTH to
            # exist, so this ordering means the only possible orphan is "sidecar with no tar yet"
            # (harmless — 404 until the tar lands too), never "tar with no sidecar" (an orphan
            # sweep_expired can't even see, since it walks *.json to find archives).
            sidecar = BACKUP_DIR / f"{token}.json"
            sidecar.write_text(json.dumps({"filename": filename, "created_at": datetime.now(UTC).isoformat()}))
            sidecar.chmod(0o600)
            tmp_tar.replace(final_tar)
            final_tar.chmod(0o600)

            # No token/download_url here — status.json is unauthenticated (GET /api/backup/status);
            # the token is returned ONLY in the POST /api/backup response body.
            _write_status(state="ready", progress=100, message="backup ready")
        except Exception as e:  # noqa: BLE001 — surface any failure to the polling UI, never crash the worker
            logger.error(f"[Backup] build failed: {e}", exc_info=True)
            _write_status(state="error", message=f"{type(e).__name__}: {e}")
    finally:
        _release_build_lock(lock_fd)
