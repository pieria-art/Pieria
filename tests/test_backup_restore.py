"""Admin Backup & Restore (Pieria 1.1, ADR-138's reflash path).

Covers: the round-trip (backup -> restore -> same state), secrets stripped + VACUUMed from the plain
DB copy, the encrypt/decrypt path (+ wrong passphrase), the alembic-stamp / checksum / tar-safety
refusals on restore validation, the boot-time apply (success + migration-failure rollback), download
token single-use + expiry + malformed-token 404, pack-owned library files excluded from the archive,
the upload size cap, and the shared busy/409 gate.
"""
import io
import json
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import core.backup as backup_module
import core.restore as restore_module
import core.restore_boot as restore_boot_module
import db_migrate
from app import app
from models import ArtworkModel, SettingsModel, SubscriptionModel

pytestmark = pytest.mark.filterwarnings("ignore")


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _build_migrated_db(db_path: Path) -> None:
    command.upgrade(_alembic_cfg(db_path), "head")


def _session(db_path: Path):
    engine = create_engine(f"sqlite:///{db_path}")
    return sessionmaker(bind=engine)()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect every path this feature touches into tmp_path, on every module that binds its own copy
    of the constant (this repo's established pattern — see DERIVATIVES_DIR in existing tests)."""
    artwork_root = tmp_path / "Artwork"
    library_dir = artwork_root / "_Library"
    library_dir.mkdir(parents=True)
    backup_dir = tmp_path / "data" / "_backups"
    restore_dir = tmp_path / "data" / "_restore"
    db_path = tmp_path / "data" / "artwork.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _build_migrated_db(db_path)
    db_url = f"sqlite:///{db_path}"

    monkeypatch.setattr(config, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(config, "RESTORE_DIR", restore_dir)
    monkeypatch.setattr(config, "IS_APPLIANCE", False)

    monkeypatch.setattr(backup_module, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(backup_module, "STATUS_FILE", backup_dir / "status.json")
    monkeypatch.setattr(backup_module, "ARTWORK_ROOT", artwork_root)
    monkeypatch.setattr(backup_module, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(backup_module, "SQLALCHEMY_DATABASE_URL", db_url)

    monkeypatch.setattr(restore_module, "RESTORE_DIR", restore_dir)
    monkeypatch.setattr(restore_module, "UPLOAD_PATH", restore_dir / "upload.tar")
    monkeypatch.setattr(restore_module, "VALIDATED_DIR", restore_dir / "validated")
    monkeypatch.setattr(restore_module, "STAGED_DIR", restore_dir / "staged")
    monkeypatch.setattr(restore_module, "PRE_RESTORE_DB", restore_dir / "pre-restore.db")
    monkeypatch.setattr(restore_module, "STATUS_FILE", restore_dir / "status.json")

    monkeypatch.setattr(restore_boot_module, "RESTORE_DIR", restore_dir)
    monkeypatch.setattr(restore_boot_module, "STAGED_DIR", restore_dir / "staged")
    monkeypatch.setattr(restore_boot_module, "PRE_RESTORE_DB", restore_dir / "pre-restore.db")
    monkeypatch.setattr(restore_boot_module, "STATUS_FILE", restore_dir / "status.json")
    monkeypatch.setattr(restore_boot_module, "ARTWORK_ROOT", artwork_root)
    monkeypatch.setattr(restore_boot_module, "SQLALCHEMY_DATABASE_URL", db_url)

    yield {
        "artwork_root": artwork_root, "library_dir": library_dir, "backup_dir": backup_dir,
        "restore_dir": restore_dir, "db_path": db_path, "db_url": db_url,
    }

    # Teardown BEFORE monkeypatch reverts (fixture teardown order is the reverse of setup, and `env`
    # depends on `monkeypatch` — so this runs first): create_backup() spawns a raw background
    # threading.Thread, which pytest/monkeypatch has no idea about. A test that posts a backup and
    # returns without waiting for it to finish (e.g. one only checking the immediate "building"
    # response) would otherwise let that thread keep running with the NOW-REVERTED real BACKUP_DIR /
    # SQLALCHEMY_DATABASE_URL — which is exactly how a stray build leaked a snapshot of the real dev
    # database into the checkout's real data/_backups. Bounded so a genuinely stuck test still fails
    # fast instead of hanging the suite.
    deadline = time.time() + 15
    while backup_module.is_building() and time.time() < deadline:
        time.sleep(0.05)


@pytest.fixture
def client(env):
    # Same-origin, like the admin GUI — matches the auth gate every mutating endpoint here now shares
    # with the appliance update bridge (require_trusted_request in routers/health.py).
    with TestClient(app, headers={"Origin": "http://testserver"}) as c:
        yield c


def _seed_library_file(env, name: str, content: bytes = b"fake-jpeg-bytes") -> Path:
    p = env["library_dir"] / name
    p.write_bytes(content)
    return p


# --- Round-trip -----------------------------------------------------------------------------------

def test_round_trip_preserves_settings_and_personal_files(env, client):
    _seed_library_file(env, "personal_photo1.jpg")
    db = _session(env["db_path"])
    db.add(SettingsModel(setting_key="display_schedule", setting_value=json.dumps({"enabled": True})))
    db.add(ArtworkModel(filename="personal_photo1.jpg", is_personal=True, status="approved"))
    db.commit()
    db.close()

    resp = client.post("/api/backup", json={"include_secrets": False})
    assert resp.status_code == 200
    token = resp.json()["token"]  # minted only in the POST response — see test_backup_status_never_*
    _wait_ready(env)

    dl = client.get(f"/api/backup/download/{token}")
    assert dl.status_code == 200
    archive_bytes = dl.content
    # single-use: a second download of the same token is gone
    assert client.get(f"/api/backup/download/{token}").status_code == 404

    # Blow away the "device": wipe settings + the library file, simulating a factory-fresh box.
    db = _session(env["db_path"])
    db.query(SettingsModel).delete()
    db.commit()
    db.close()
    (env["library_dir"] / "personal_photo1.jpg").unlink()

    upload = client.post("/api/restore/upload", content=archive_bytes,
                         headers={"content-type": "application/x-tar"})
    assert upload.status_code == 200, upload.text
    summary = upload.json()
    assert summary["has_secrets"] is False

    confirm = client.post("/api/restore/confirm", json={})
    assert confirm.status_code == 200
    assert confirm.json()["restart_required"] is True  # non-appliance

    # Simulate the container restart: boot-time apply.
    restore_boot_module.apply_pending_restore()

    db = _session(env["db_path"])
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == "display_schedule").first()
    assert row is not None and json.loads(row.setting_value)["enabled"] is True
    db.close()
    assert (env["library_dir"] / "personal_photo1.jpg").exists()


def _wait_ready(env, timeout=10):
    status_file = env["backup_dir"] / "status.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if status_file.exists():
            try:
                data = json.loads(status_file.read_text())
            except (OSError, ValueError):
                data = {}
            if data.get("state") in ("ready", "error"):
                assert data.get("state") == "ready", data
                return data
        time.sleep(0.05)
    raise AssertionError("backup never reached 'ready'")


# --- Secrets stripped + VACUUMed -------------------------------------------------------------------

def test_secrets_stripped_and_vacuumed_from_plain_db_copy(env, client):
    secret_value = "sk-super-secret-planted-value-zzz"
    db = _session(env["db_path"])
    db.add(SettingsModel(setting_key="ai_api_key", setting_value=secret_value))
    db.commit()
    db.close()

    resp = client.post("/api/backup", json={"include_secrets": False})
    token = resp.json()["token"]
    _wait_ready(env)
    tar_path = env["backup_dir"] / f"{token}.tar"
    with tarfile.open(tar_path, "r:") as tf:
        db_bytes = tf.extractfile("artwork.db").read()
    assert secret_value.encode() not in db_bytes

    # Raw file bytes too (VACUUM must not just leave it in a freed page still on disk).
    raw = tar_path.read_bytes()
    assert secret_value.encode() not in raw


def test_pack_owned_library_files_excluded(env, client):
    _seed_library_file(env, "masterpieces__mona-lisa__abc123.jpg")
    _seed_library_file(env, "personal_keepme.jpg")
    (env["artwork_root"] / "_manifests").mkdir(parents=True)
    manifest = {"id": "masterpieces", "items": [
        {"id": "mona-lisa", "image": {"local_file": "masterpieces__mona-lisa__abc123.jpg"}}]}
    (env["artwork_root"] / "_manifests" / "masterpieces.json").write_text(json.dumps(manifest))
    db = _session(env["db_path"])
    db.add(SubscriptionModel(url="pack:masterpieces", collection_id="masterpieces"))
    db.commit()
    db.close()

    resp = client.post("/api/backup", json={"include_secrets": False})
    token = resp.json()["token"]
    _wait_ready(env)
    tar_path = env["backup_dir"] / f"{token}.tar"
    with tarfile.open(tar_path, "r:") as tf:
        names = tf.getnames()
    assert "library/personal_keepme.jpg" in names
    assert "library/masterpieces__mona-lisa__abc123.jpg" not in names


# --- Encryption -------------------------------------------------------------------------------------

def test_encrypt_decrypt_round_trip():
    blob = backup_module.encrypt_secrets({"ai_api_key": "sk-abc"}, "correct horse battery staple")
    assert backup_module.decrypt_secrets(blob, "correct horse battery staple") == {"ai_api_key": "sk-abc"}


def test_decrypt_wrong_passphrase_raises():
    import nacl.exceptions
    blob = backup_module.encrypt_secrets({"ai_api_key": "sk-abc"}, "correct horse battery staple")
    with pytest.raises(nacl.exceptions.CryptoError):
        backup_module.decrypt_secrets(blob, "wrong passphrase entirely")


def test_restore_with_secrets_wrong_passphrase_stages_nothing(env, client):
    db = _session(env["db_path"])
    db.add(SettingsModel(setting_key="ai_api_key", setting_value="sk-real-key"))
    db.commit()
    db.close()

    resp = client.post("/api/backup", json={"include_secrets": True, "passphrase": "correct horse battery x"})
    token = resp.json()["token"]
    _wait_ready(env)
    archive = (env["backup_dir"] / f"{token}.tar").read_bytes()

    client.post("/api/restore/upload", content=archive)
    resp = client.post("/api/restore/confirm", json={"passphrase": "totally wrong passphrase!"})
    assert resp.status_code == 400
    assert not (env["restore_dir"] / "staged").exists()


def test_include_secrets_requires_long_passphrase(env, client):
    resp = client.post("/api/backup", json={"include_secrets": True, "passphrase": "short"})
    assert resp.status_code == 400


# --- Validation refusals -----------------------------------------------------------------------------

def _minimal_manifest(db_rev: str, files: list[dict], **extra) -> dict:
    m = {"format": "pieria-backup", "format_version": 1, "app_version": "1.0.0",
         "alembic_revision": db_rev, "created_at": "2026-09-24T00:00:00+00:00",
         "hostname": "test", "packs": [], "conf": {}, "has_secrets": False, "files": files}
    m.update(extra)
    return m


def _sha256_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _make_tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _valid_db_bytes(env) -> bytes:
    return env["db_path"].read_bytes()


def test_unknown_alembic_stamp_refused(env, client, tmp_path):
    # The check reads the EXTRACTED db's OWN alembic_version (reviewer 7) — so to test "unknown
    # revision" specifically (as opposed to "manifest lied about the stamp"), the db's actual stamp
    # AND the manifest's claim must agree on the same unknown value.
    bad_db_path = tmp_path / "unknown_rev.db"
    shutil.copy(env["db_path"], bad_db_path)
    conn = sqlite3.connect(str(bad_db_path))
    conn.execute("UPDATE alembic_version SET version_num = 'nonexistent_future_rev'")
    conn.commit(); conn.close()
    db_bytes = bad_db_path.read_bytes()
    manifest = _minimal_manifest("nonexistent_future_rev",
                                 [{"path": "artwork.db", "size": len(db_bytes),
                                   "sha256": _sha256_bytes(db_bytes)}])
    tar = _make_tar({"manifest.json": json.dumps(manifest).encode(), "artwork.db": db_bytes})
    resp = client.post("/api/restore/upload", content=tar)
    assert resp.status_code == 400
    assert "schema revision" in resp.json()["detail"]


def test_checksum_mismatch_refused(env, client):
    db_bytes = _valid_db_bytes(env)
    rev = sqlite3.connect(str(env["db_path"])).execute("SELECT version_num FROM alembic_version").fetchone()[0]
    manifest = _minimal_manifest(rev, [{"path": "artwork.db", "size": len(db_bytes),
                                        "sha256": "0" * 64}])
    tar = _make_tar({"manifest.json": json.dumps(manifest).encode(), "artwork.db": db_bytes})
    resp = client.post("/api/restore/upload", content=tar)
    assert resp.status_code == 400
    assert "checksum" in resp.json()["detail"]


def test_unexpected_member_refused(env, client):
    db_bytes = _valid_db_bytes(env)
    rev = sqlite3.connect(str(env["db_path"])).execute("SELECT version_num FROM alembic_version").fetchone()[0]
    manifest = _minimal_manifest(rev, [{"path": "artwork.db", "size": len(db_bytes),
                                        "sha256": _sha256_bytes(db_bytes)}])
    tar = _make_tar({"manifest.json": json.dumps(manifest).encode(), "artwork.db": db_bytes,
                     "../../etc/passwd": b"nope"})
    resp = client.post("/api/restore/upload", content=tar)
    assert resp.status_code == 400


def test_traversal_library_basename_refused(env, client):
    db_bytes = _valid_db_bytes(env)
    rev = sqlite3.connect(str(env["db_path"])).execute("SELECT version_num FROM alembic_version").fetchone()[0]
    manifest = _minimal_manifest(rev, [{"path": "artwork.db", "size": len(db_bytes),
                                        "sha256": _sha256_bytes(db_bytes)}])
    tar = _make_tar({"manifest.json": json.dumps(manifest).encode(), "artwork.db": db_bytes,
                     "library/../../evil.jpg": b"x"})
    resp = client.post("/api/restore/upload", content=tar)
    assert resp.status_code == 400


def test_symlink_member_refused(env, client, tmp_path):
    db_bytes = _valid_db_bytes(env)
    rev = sqlite3.connect(str(env["db_path"])).execute("SELECT version_num FROM alembic_version").fetchone()[0]
    manifest = _minimal_manifest(rev, [{"path": "artwork.db", "size": len(db_bytes),
                                        "sha256": _sha256_bytes(db_bytes)}])
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        m_bytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo(name="manifest.json"); info.size = len(m_bytes)
        tf.addfile(info, io.BytesIO(m_bytes))
        info = tarfile.TarInfo(name="artwork.db"); info.size = len(db_bytes)
        tf.addfile(info, io.BytesIO(db_bytes))
        link = tarfile.TarInfo(name="library/evil.jpg")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    resp = client.post("/api/restore/upload", content=buf.getvalue())
    assert resp.status_code == 400


def test_missing_required_table_refused(env, client):
    # A DB with the right alembic stamp but no 'artworks' table (corrupt/truncated archive).
    bad_db_path = env["restore_dir"].parent / "bad.db"
    bad_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(bad_db_path))
    rev = sqlite3.connect(str(env["db_path"])).execute("SELECT version_num FROM alembic_version").fetchone()[0]
    conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
    conn.execute("INSERT INTO alembic_version VALUES (?)", (rev,))
    conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, setting_key TEXT, setting_value TEXT)")
    conn.commit(); conn.close()
    db_bytes = bad_db_path.read_bytes()
    manifest = _minimal_manifest(rev, [{"path": "artwork.db", "size": len(db_bytes),
                                        "sha256": _sha256_bytes(db_bytes)}])
    tar = _make_tar({"manifest.json": json.dumps(manifest).encode(), "artwork.db": db_bytes})
    resp = client.post("/api/restore/upload", content=tar)
    assert resp.status_code == 400
    assert "required table" in resp.json()["detail"]


# --- Boot-apply -------------------------------------------------------------------------------------

def _stage_a_valid_restore(env, extra_settings=None) -> None:
    staged = env["restore_dir"] / "staged"
    staged.mkdir(parents=True)
    new_db = staged / "artwork.db"
    _build_migrated_db(new_db)
    if extra_settings:
        conn = sqlite3.connect(str(new_db))
        for k, v in extra_settings.items():
            conn.execute("INSERT INTO settings (setting_key, setting_value) VALUES (?, ?)", (k, v))
        conn.commit(); conn.close()
    (staged / "restore.json").write_text(json.dumps(
        {"packs": ["masterpieces"], "conf": {"TIMEZONE": "UTC"}, "created_at": "2026-09-24T00:00:00Z"}))


def test_boot_apply_success_sets_pending_markers(env):
    _stage_a_valid_restore(env)
    restore_boot_module.apply_pending_restore()

    assert not (env["restore_dir"] / "staged").exists()
    assert not (env["restore_dir"] / "pre-restore.db").exists()
    conn = sqlite3.connect(str(env["db_path"]))
    rows = dict(conn.execute("SELECT setting_key, setting_value FROM settings "
                             "WHERE setting_key IN ('restore_pending_packs','restore_pending_conf')"))
    conn.close()
    assert json.loads(rows["restore_pending_packs"]) == ["masterpieces"]
    assert json.loads(rows["restore_pending_conf"]) == {"TIMEZONE": "UTC"}
    status = json.loads((env["restore_dir"] / "status.json").read_text())
    assert status["state"] == "restored"


def test_boot_apply_rollback_on_migration_failure(env, monkeypatch):
    _stage_a_valid_restore(env)
    original_db_bytes = env["db_path"].read_bytes()

    calls = {"n": 0}
    real_run_migrations = db_migrate.run_migrations

    def _flaky_run_migrations(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated migration failure on the restored DB")
        return real_run_migrations(*a, **kw)

    monkeypatch.setattr(restore_boot_module.db_migrate, "run_migrations", _flaky_run_migrations)
    restore_boot_module.apply_pending_restore()

    # Rolled back: the live DB is back to the pre-restore bytes, staging cleaned up, status recorded.
    assert env["db_path"].read_bytes() == original_db_bytes
    assert not (env["restore_dir"] / "staged").exists()
    status = json.loads((env["restore_dir"] / "status.json").read_text())
    assert status["state"] == "failed"


def test_no_staged_restore_boots_unchanged(env):
    restore_boot_module.apply_pending_restore()
    # No staged/ dir at all -> just a plain migration run; an already-head DB is untouched byte-for-byte
    # aside from WAL bookkeeping, so just assert nothing exploded and no restore status was written.
    assert env["db_path"].exists()
    assert not (env["restore_dir"] / "status.json").exists()


# --- Download token + upload cap + busy gate ---------------------------------------------------------

def test_malformed_token_404(client):
    assert client.get("/api/backup/download/not-a-valid-token").status_code == 404


def test_expired_archive_swept(env, client):
    resp = client.post("/api/backup", json={"include_secrets": False})
    token = resp.json()["token"]
    _wait_ready(env)
    sidecar = env["backup_dir"] / f"{token}.json"
    old = time.time() - 3700  # > 1h TTL
    import os
    os.utime(sidecar, (old, old))

    # Any backup/restore endpoint call sweeps expired archives.
    client.get("/api/backup/status")
    assert client.get(f"/api/backup/download/{token}").status_code == 404


def test_upload_over_cap_rejected(env, client, monkeypatch):
    # 2 GB "free" -> cap is (free - 1GB headroom) / 2 = 512 MB (extraction needs ~2x the upload on disk
    # at once); a declared 3 GB Content-Length must be refused before any body is even streamed.
    monkeypatch.setattr("shutil.disk_usage", lambda p: type("D", (), {"free": 2 * 1024 * 1024 * 1024})())
    resp = client.post("/api/restore/upload", content=b"x" * (2 * 1024 * 1024),
                       headers={"content-length": str(3 * 1024 * 1024 * 1024)})
    assert resp.status_code == 413


def test_upload_cap_bytes_halves_headroom():
    assert restore_module.upload_cap_bytes(3 * 1024**3) == (3 * 1024**3 - 1024**3) // 2


def test_409_while_backup_building(env, client, monkeypatch):
    monkeypatch.setattr(backup_module, "is_building", lambda: True)
    resp = client.post("/api/restore/upload", content=b"irrelevant")
    assert resp.status_code == 409


# --- Auth gate (reviewer BLOCKER 3): every mutating endpoint requires same-origin OR a valid token ---

def test_no_origin_backup_refused(env):
    with TestClient(app) as c:   # no default Origin header at all
        resp = c.post("/api/backup", json={"include_secrets": False})
    assert resp.status_code == 403


def test_cross_origin_backup_refused(env):
    with TestClient(app, headers={"Origin": "http://evil.example"}) as c:
        resp = c.post("/api/backup", json={"include_secrets": False})
    assert resp.status_code == 403


def test_same_origin_backup_allowed(env, client):
    resp = client.post("/api/backup", json={"include_secrets": False})
    assert resp.status_code == 200


def test_valid_token_backup_allowed_without_origin(env, monkeypatch):
    monkeypatch.setattr(config, "APPLIANCE_UPDATE_TOKEN", "s3cr3t-token-value")
    with TestClient(app) as c:  # no Origin header — only the token should let this through
        resp = c.post("/api/backup", json={"include_secrets": False},
                      headers={"X-Appliance-Token": "s3cr3t-token-value"})
    assert resp.status_code == 200


def test_no_origin_restore_upload_refused(env):
    with TestClient(app) as c:
        resp = c.post("/api/restore/upload", content=b"irrelevant")
    assert resp.status_code == 403


def test_no_origin_restore_confirm_refused(env):
    with TestClient(app) as c:
        resp = c.post("/api/restore/confirm", json={})
    assert resp.status_code == 403


def test_no_origin_retry_packs_refused(env):
    with TestClient(app) as c:
        resp = c.post("/api/restore/retry-packs")
    assert resp.status_code == 403


def test_no_origin_clear_pending_conf_refused(env):
    with TestClient(app) as c:
        resp = c.post("/api/restore/pending-conf/clear")
    assert resp.status_code == 403


# --- Sparse tar member refused (reviewer BLOCKER 2) --------------------------------------------------

def test_sparse_member_refused(env, client):
    db_bytes = _valid_db_bytes(env)
    rev = sqlite3.connect(str(env["db_path"])).execute("SELECT version_num FROM alembic_version").fetchone()[0]
    manifest = _minimal_manifest(rev, [
        {"path": "artwork.db", "size": len(db_bytes), "sha256": _sha256_bytes(db_bytes)},
        {"path": "library/sparse.jpg", "size": 100, "sha256": _sha256_bytes(b"x" * 100)},
    ])
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        m_bytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo(name="manifest.json"); info.size = len(m_bytes)
        tf.addfile(info, io.BytesIO(m_bytes))
        info = tarfile.TarInfo(name="artwork.db"); info.size = len(db_bytes)
        tf.addfile(info, io.BytesIO(db_bytes))
        # A GNU sparse member: tarfile.TarInfo.isreg()/isfile() both return True for this type (it's in
        # REGULAR_TYPES) — the exact gap the reviewer found. A small declared/archived size can still
        # expand to a huge logical size; our fix checks m.type against an explicit allowlist instead.
        sparse = tarfile.TarInfo(name="library/sparse.jpg")
        sparse.type = tarfile.GNUTYPE_SPARSE
        sparse.size = 100
        tf.addfile(sparse, io.BytesIO(b"x" * 100))
    resp = client.post("/api/restore/upload", content=buf.getvalue())
    assert resp.status_code == 400
    assert "non-regular-file" in resp.json()["detail"]


# --- Manifest files[] must exactly match the archive (reviewer 6) ------------------------------------

def test_manifest_missing_a_file_entry_refused(env, client):
    db_bytes = _valid_db_bytes(env)
    rev = sqlite3.connect(str(env["db_path"])).execute("SELECT version_num FROM alembic_version").fetchone()[0]
    # manifest declares only artwork.db, but the tar ALSO carries an undeclared library file.
    manifest = _minimal_manifest(rev, [{"path": "artwork.db", "size": len(db_bytes),
                                        "sha256": _sha256_bytes(db_bytes)}])
    tar = _make_tar({"manifest.json": json.dumps(manifest).encode(), "artwork.db": db_bytes,
                     "library/undeclared.jpg": b"sneaky"})
    resp = client.post("/api/restore/upload", content=tar)
    assert resp.status_code == 400
    assert "does not match" in resp.json()["detail"]


def test_manifest_lists_a_file_not_in_archive_refused(env, client):
    db_bytes = _valid_db_bytes(env)
    rev = sqlite3.connect(str(env["db_path"])).execute("SELECT version_num FROM alembic_version").fetchone()[0]
    manifest = _minimal_manifest(rev, [
        {"path": "artwork.db", "size": len(db_bytes), "sha256": _sha256_bytes(db_bytes)},
        {"path": "library/ghost.jpg", "size": 3, "sha256": _sha256_bytes(b"abc")},
    ])
    tar = _make_tar({"manifest.json": json.dumps(manifest).encode(), "artwork.db": db_bytes})
    resp = client.post("/api/restore/upload", content=tar)
    assert resp.status_code == 400
    assert "does not match" in resp.json()["detail"]


# --- Alembic stamp must match the EXTRACTED db, not just a known revision (reviewer 7) ----------------

def test_manifest_alembic_claim_mismatching_actual_db_refused(env, client):
    db_bytes = _valid_db_bytes(env)
    rev = sqlite3.connect(str(env["db_path"])).execute("SELECT version_num FROM alembic_version").fetchone()[0]
    # A DIFFERENT (but still known/valid) revision than what's actually stamped in artwork.db.
    other_rev = "0001_baseline" if rev != "0001_baseline" else "0002_playback_session_unique"
    manifest = _minimal_manifest(other_rev, [{"path": "artwork.db", "size": len(db_bytes),
                                              "sha256": _sha256_bytes(db_bytes)}])
    tar = _make_tar({"manifest.json": json.dumps(manifest).encode(), "artwork.db": db_bytes})
    resp = client.post("/api/restore/upload", content=tar)
    assert resp.status_code == 400
    assert "does not match" in resp.json()["detail"] or "alembic_revision" in resp.json()["detail"]


# --- Stuck-state resilience (reviewer SHOULD-FIX 5) ---------------------------------------------------

def test_corrupt_db_in_archive_does_not_stick_busy(env, client):
    rev = sqlite3.connect(str(env["db_path"])).execute("SELECT version_num FROM alembic_version").fetchone()[0]
    garbage = b"not a sqlite database at all"
    manifest = _minimal_manifest(rev, [{"path": "artwork.db", "size": len(garbage),
                                        "sha256": _sha256_bytes(garbage)}])
    tar = _make_tar({"manifest.json": json.dumps(manifest).encode(), "artwork.db": garbage})
    resp = client.post("/api/restore/upload", content=tar)
    assert resp.status_code == 400
    # Not stuck: the next call is not a 409.
    resp2 = client.post("/api/restore/upload", content=tar)
    assert resp2.status_code == 400


def test_truncated_tar_does_not_stick_busy(env, client):
    resp = client.post("/api/restore/upload", content=b"PK\x03\x04not-a-tar-file")
    assert resp.status_code == 400
    resp2 = client.post("/api/restore/upload", content=b"PK\x03\x04not-a-tar-file")
    assert resp2.status_code == 400


def test_wrong_passphrase_confirm_does_not_stick_busy(env, client):
    db = _session(env["db_path"])
    db.add(SettingsModel(setting_key="ai_api_key", setting_value="sk-real-key"))
    db.commit()
    db.close()
    resp = client.post("/api/backup", json={"include_secrets": True, "passphrase": "correct horse battery x"})
    token = resp.json()["token"]
    _wait_ready(env)
    archive = (env["backup_dir"] / f"{token}.tar").read_bytes()
    client.post("/api/restore/upload", content=archive)
    resp = client.post("/api/restore/confirm", json={"passphrase": "wrong passphrase entirely!"})
    assert resp.status_code == 400
    # Not stuck in "staging" — a fresh upload attempt is not a 409.
    resp2 = client.post("/api/restore/upload", content=archive)
    assert resp2.status_code in (200, 400)   # re-validates fine; the point is it's not 409


def test_stale_building_status_treated_as_idle(env, client, monkeypatch):
    from datetime import UTC, datetime, timedelta
    backup_module._write_status(state="building", progress=10, message="stuck",
                                updated_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat())
    assert backup_module.read_status()["state"] == "idle"
    assert backup_module.is_building() is False


# --- Retry-packs cross-worker lock (reviewer SHOULD-FIX 9) --------------------------------------------

def test_retry_packs_409_while_already_running(env, client, monkeypatch):
    from core import lifespan as lifespan_module
    monkeypatch.setattr(lifespan_module, "is_redownloading_packs", lambda: True)
    resp = client.post("/api/restore/retry-packs")
    assert resp.status_code == 409


# --- restore_boot crash-resumption (reviewer BLOCKER 1) ------------------------------------------------

def test_boot_apply_self_heals_pre_restore_db_with_no_live_db(env):
    """Crash between moving the old DB aside and moving the staged DB in: no marker survives this test's
    setup (simulating the marker write for 'swapped' never having happened either), so on the next boot
    there is no restore.json at all — the ONLY signal left is pre-restore.db with no live DB, which must
    be put back so the box has SOME working DB rather than none."""
    original_bytes = env["db_path"].read_bytes()
    env["db_path"].unlink()
    (env["restore_dir"]).mkdir(parents=True, exist_ok=True)
    (env["restore_dir"] / "pre-restore.db").write_bytes(original_bytes)

    restore_boot_module.apply_pending_restore()

    assert env["db_path"].exists()
    assert env["db_path"].read_bytes() == original_bytes
    assert not (env["restore_dir"] / "pre-restore.db").exists()


def test_boot_apply_resumes_after_crash_post_swap_before_marker_advance(env):
    """Crash after `shutil.move(staged_db, db_path)` succeeded but before the marker was advanced past
    'staged': db_path already holds the restored (not-yet-migrated) DB, pre-restore.db already holds the
    real original, and staged/artwork.db is gone — yet the marker still says phase='staged'. Resuming
    must NOT try to re-move a staged/artwork.db that no longer exists, and must NOT re-stash db_path
    over the already-correct pre-restore.db."""
    staged = env["restore_dir"] / "staged"
    staged.mkdir(parents=True)
    original_bytes = env["db_path"].read_bytes()
    (env["restore_dir"] / "pre-restore.db").write_bytes(original_bytes)
    # db_path already holds a migrated "restored" DB (built fresh); staged/artwork.db is already gone.
    _build_migrated_db(env["db_path"])
    (staged / "restore.json").write_text(json.dumps(
        {"packs": ["p1"], "conf": {}, "created_at": "2026-09-24T00:00:00Z", "phase": "staged"}))

    restore_boot_module.apply_pending_restore()

    assert not staged.exists()
    assert not (env["restore_dir"] / "pre-restore.db").exists()
    status = json.loads((env["restore_dir"] / "status.json").read_text())
    assert status["state"] == "restored"


def test_boot_apply_resumes_from_migrated_phase(env):
    """The marker already says 'migrated' — migrations ran and succeeded in a prior (crashed) attempt.
    Resuming must only do the remaining steps (library copy, settings, cleanup), not re-migrate or
    re-swap anything."""
    staged = env["restore_dir"] / "staged"
    (staged / "library").mkdir(parents=True)
    (staged / "library" / "personal_resume.jpg").write_bytes(b"resumed-bytes")
    (staged / "restore.json").write_text(json.dumps(
        {"packs": ["p1"], "conf": {"TIMEZONE": "UTC"}, "created_at": "2026-09-24T00:00:00Z",
         "phase": "migrated"}))
    # db_path is already the fully-migrated restored DB (this is what "migrated" phase guarantees).
    _build_migrated_db(env["db_path"])

    restore_boot_module.apply_pending_restore()

    assert (env["library_dir"] / "personal_resume.jpg").read_bytes() == b"resumed-bytes"
    conn = sqlite3.connect(str(env["db_path"]))
    row = conn.execute("SELECT setting_value FROM settings WHERE setting_key = 'restore_pending_conf'").fetchone()
    conn.close()
    assert json.loads(row[0]) == {"TIMEZONE": "UTC"}
    assert not staged.exists()


def test_boot_apply_never_overwrites_existing_pre_restore_db(env):
    """If pre-restore.db already exists when the 'staged' phase runs (a previous crashed attempt already
    stashed the real original), the swap must skip re-stashing — it must never clobber that file with
    whatever happens to be at db_path right now. Verified indirectly: the apply must complete
    successfully (using the ALREADY-stashed original, not attempting to stash again) with no error."""
    staged = env["restore_dir"] / "staged"
    staged.mkdir(parents=True)
    _build_migrated_db(staged / "artwork.db")
    (staged / "restore.json").write_text(json.dumps(
        {"packs": [], "conf": {}, "created_at": "2026-09-24T00:00:00Z", "phase": "staged"}))
    # pre-restore.db already holds SOME prior stash — must survive untouched by the (skipped) re-stash
    # step, i.e. the code must not raise trying to move db_path onto an existing path.
    sentinel = b"already-stashed-original"
    (env["restore_dir"] / "pre-restore.db").write_bytes(sentinel)

    restore_boot_module.apply_pending_restore()  # must not raise

    status = json.loads((env["restore_dir"] / "status.json").read_text())
    assert status["state"] == "restored"
    assert not (env["restore_dir"] / "pre-restore.db").exists()  # cleaned up on final success


# --- Round-2 reviewer findings ------------------------------------------------------------------------

def _mark_db(db_path, value: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES ('marker', ?)",
                (value,))
    conn.commit(); conn.close()


def _read_mark(db_path) -> str | None:
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT setting_value FROM settings WHERE setting_key = 'marker'").fetchone()
    conn.close()
    return row[0] if row else None


def _stage_marked_restore(env) -> None:
    _mark_db(env["db_path"], "ORIGINAL")
    _stage_a_valid_restore(env, {"marker": "RESTORED"})
    lib = env["restore_dir"] / "staged" / "library"
    lib.mkdir()
    (lib / "p.jpg").write_bytes(b"x")


def test_rollback_transient_failure_recovers_on_reboot2(env, monkeypatch):
    """Migration fails on the restored DB (boot1), THEN the rollback's own re-migration also fails once
    (transient) before boot1 halts non-zero. Boot2 must resume from 'rolled_back' — not re-attempt the
    swap — and this time the migration succeeds: original DB survives throughout, ends up migrated,
    status says failed (the restore itself never landed)."""
    _stage_marked_restore(env)
    real = db_migrate.run_migrations
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("transient/disk")
        return real(*a, **kw)

    monkeypatch.setattr(restore_boot_module.db_migrate, "run_migrations", flaky)
    with pytest.raises(SystemExit):
        restore_boot_module.apply_pending_restore()
    assert _read_mark(env["db_path"]) == "ORIGINAL"  # never deleted, never replaced by staged content

    restore_boot_module.apply_pending_restore()  # boot2: 3rd call succeeds

    assert _read_mark(env["db_path"]) == "ORIGINAL"
    status = json.loads((env["restore_dir"] / "status.json").read_text())
    assert status["state"] == "failed"
    assert not (env["library_dir"] / "p.jpg").exists()  # library is never merged on a rolled-back restore


def test_rollback_persistent_failure_never_loses_original_db(env, monkeypatch):
    """Migrations keep failing across MULTIPLE boots (forward attempt + repeated rollback attempts).
    The original DB must survive every single boot — this is the exact bug the reviewer found: a second
    boot re-entering with phase='swapped' used to unconditionally unlink db_path, deleting the only
    remaining copy once PRE_RESTORE_DB had already been consumed."""
    _stage_marked_restore(env)
    real = db_migrate.run_migrations
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("deterministic fail on existing DB")
        return real(*a, **kw)

    monkeypatch.setattr(restore_boot_module.db_migrate, "run_migrations", flaky)

    with pytest.raises(SystemExit):
        restore_boot_module.apply_pending_restore()  # boot1: forward fails, rollback-migrate fails -> halt
    assert _read_mark(env["db_path"]) == "ORIGINAL"

    with pytest.raises(SystemExit):
        restore_boot_module.apply_pending_restore()  # boot2: still resuming 'rolled_back', still fails
    assert _read_mark(env["db_path"]) == "ORIGINAL"
    status = json.loads((env["restore_dir"] / "status.json").read_text())
    assert status["state"] == "failed"

    restore_boot_module.apply_pending_restore()  # boot3: migrations finally succeed
    assert _read_mark(env["db_path"]) == "ORIGINAL"
    status = json.loads((env["restore_dir"] / "status.json").read_text())
    assert status["state"] == "failed"  # the restore itself never landed — this boot just finished migrating


def test_merge_library_failure_finishes_as_restored_partial(env, monkeypatch):
    """A _merge_library failure (e.g. ENOSPC) must not raise, and must NOT leave the restore stuck at
    'migrated' forever (round-4: the staged library files can't be recovered by retrying, so the restore
    proceeds through settings_set + cleanup on this SAME boot instead) — one boot call finishes the whole
    thing, staging is cleaned up, pending packs/conf are still set, and status honestly says the library
    is missing."""
    _stage_marked_restore(env)

    def boom(_staged_library):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(restore_boot_module, "_merge_library", boom)
    restore_boot_module.apply_pending_restore()  # must not raise; must not get stuck

    assert _read_mark(env["db_path"]) == "RESTORED"  # the DB restore itself DID land
    assert not (env["restore_dir"] / "staged").exists()  # cleanup still ran
    assert not (env["restore_dir"] / "pre-restore.db").exists()
    conn = sqlite3.connect(str(env["db_path"]))
    row = conn.execute("SELECT setting_value FROM settings WHERE setting_key = 'restore_pending_packs'").fetchone()
    conn.close()
    assert json.loads(row[0]) == ["masterpieces"]  # pending-packs still recorded despite the failure

    status = json.loads((env["restore_dir"] / "status.json").read_text())
    assert status["state"] == "restored_partial"
    assert "library" in status["message"]

    # A second boot (nothing left to resume — marker/staged dir are gone) must be a total no-op.
    restore_boot_module.apply_pending_restore()
    assert not (env["restore_dir"] / "status.json").exists() or \
        json.loads((env["restore_dir"] / "status.json").read_text()).get("state") == "restored_partial"


def test_migrations_always_run_when_resuming_a_later_phase(env, monkeypatch):
    """Reviewer round-4: a marker resumed at 'library_copied' (or any later phase) from a PRIOR boot
    must still call db_migrate.run_migrations() THIS boot — otherwise an update-app between boots (new
    migrations, SD_MIGRATIONS_DONE=1 already exported) would serve on a stale schema forever."""
    _stage_marked_restore(env)
    staged = env["restore_dir"] / "staged"
    marker = json.loads((staged / "restore.json").read_text())
    marker["phase"] = "library_copied"
    (staged / "restore.json").write_text(json.dumps(marker))
    # db_path already holds the "restored" DB content (as "library_copied" phase guarantees) —
    # simulate that by moving the staged DB onto it directly, skipping the swap/migrate phases.
    staged_db = staged / "artwork.db"
    if staged_db.exists():
        shutil.move(str(staged_db), str(env["db_path"]))

    calls = []
    real = db_migrate.run_migrations
    monkeypatch.setattr(restore_boot_module.db_migrate, "run_migrations",
                        lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])

    restore_boot_module.apply_pending_restore()
    assert calls, "run_migrations() was never called while resuming at a later phase"


# --- Round-2: token leak (status must never carry it) --------------------------------------------------

def test_backup_status_never_contains_token_or_download_url(env, client):
    resp = client.post("/api/backup", json={"include_secrets": False})
    assert resp.status_code == 200
    token = resp.json()["token"]
    assert token and backup_module.TOKEN_RE.fullmatch(token)

    status = _wait_ready(env)
    assert "token" not in status
    assert "download_url" not in status
    # But the token minted at POST time is still the real, working one.
    dl = client.get(f"/api/backup/download/{token}")
    assert dl.status_code == 200


def test_backup_status_poll_by_a_different_caller_cannot_recover_token(env, client):
    """Simulates a second LAN client (or a reloaded page) that only ever polls status — it must never
    be able to derive a working download token from what status.json exposes."""
    client.post("/api/backup", json={"include_secrets": False})
    _wait_ready(env)
    status = client.get("/api/backup/status").json()
    assert status.get("state") == "ready"
    assert "token" not in status and "download_url" not in status


# --- Round-2: confirm() requires state == "validated" ---------------------------------------------------

def test_confirm_after_failed_validation_refused(env, client):
    """A validation that fails AFTER extraction (newer/unknown stamp) must leave nothing confirmable —
    both because validated/ is wiped on any failure, and because confirm() itself checks status state."""
    p = env["restore_dir"].parent / "newer.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(env["db_path"], p)
    conn = sqlite3.connect(str(p))
    conn.execute("UPDATE alembic_version SET version_num = 'ffffnewer'")
    conn.commit(); conn.close()
    nb = p.read_bytes()
    manifest = _minimal_manifest("ffffnewer", [{"path": "artwork.db", "size": len(nb),
                                                "sha256": _sha256_bytes(nb)}])
    tar = _make_tar({"manifest.json": json.dumps(manifest).encode(), "artwork.db": nb})
    upload = client.post("/api/restore/upload", content=tar)
    assert upload.status_code == 400
    assert not (env["restore_dir"] / "validated").exists()  # wiped, not left populated

    confirm = client.post("/api/restore/confirm", json={})
    assert confirm.status_code == 409
    assert not (env["restore_dir"] / "staged" / "artwork.db").exists()


def test_confirm_after_checksum_mismatch_refused(env, client):
    db_bytes = _valid_db_bytes(env)
    rev = sqlite3.connect(str(env["db_path"])).execute("SELECT version_num FROM alembic_version").fetchone()[0]
    manifest = _minimal_manifest(rev, [{"path": "artwork.db", "size": len(db_bytes), "sha256": "0" * 64}])
    tar = _make_tar({"manifest.json": json.dumps(manifest).encode(), "artwork.db": db_bytes})
    upload = client.post("/api/restore/upload", content=tar)
    assert upload.status_code == 400

    confirm = client.post("/api/restore/confirm", json={})
    assert confirm.status_code == 409


def test_confirm_with_no_prior_upload_refused(env, client):
    resp = client.post("/api/restore/confirm", json={})
    assert resp.status_code == 409


# --- Round-2: permissions ------------------------------------------------------------------------------

def test_backup_dir_and_status_permissions(env, client):
    import os
    import stat
    client.post("/api/backup", json={"include_secrets": False})
    _wait_ready(env)
    assert stat.S_IMODE(os.stat(env["backup_dir"]).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(backup_module.STATUS_FILE).st_mode) == 0o600


def test_build_lock_file_permissions(env, client):
    import os
    import stat
    client.post("/api/backup", json={"include_secrets": False})
    _wait_ready(env)
    lock_path = backup_module._build_lock_path()
    assert lock_path.exists()
    assert stat.S_IMODE(os.stat(lock_path).st_mode) == 0o600


# --- Round-4: no stale "ready" visible between POST and the build thread's own first write ------------

def test_status_is_building_synchronously_after_post(env, client):
    """A stale 'ready' from a PREVIOUS build must never be visible even for the instant between the
    POST /api/backup response and the background thread's own first status write."""
    backup_module._write_status(state="ready", progress=100, message="stale from a previous build")
    resp = client.post("/api/backup", json={"include_secrets": False})
    assert resp.status_code == 200
    status = client.get("/api/backup/status").json()
    # The background thread may have already advanced progress by the time we poll (racy by design —
    # what matters is that the stale "ready" from before the POST is never visible again).
    assert status["state"] == "building"
    assert "token" not in status


# --- Round-4: test isolation guard — Backup & Restore must never touch the real checkout ---------------

def test_backup_never_touches_the_real_checkout_data_dir(env, client):
    """Guards the exact bug the reviewer found: a full suite run left multi-GB archives in the real
    repo's data/_backups and rewrote its real status.json. tests/conftest.py's autouse
    `_isolate_backup_restore_paths` fixture is what prevents this for every test in the suite — this
    test proves it actually works for the one entry point that showed the bug (the HTTP API), not just
    that the fixture exists."""
    real_backup_dir = Path("data/_backups")
    real_restore_dir = Path("data/_restore")
    before_backup = set(real_backup_dir.iterdir()) if real_backup_dir.is_dir() else set()
    before_restore = set(real_restore_dir.iterdir()) if real_restore_dir.is_dir() else set()

    resp = client.post("/api/backup", json={"include_secrets": False})
    assert resp.status_code == 200
    token = resp.json()["token"]
    _wait_ready(env)
    client.get(f"/api/backup/download/{token}")

    after_backup = set(real_backup_dir.iterdir()) if real_backup_dir.is_dir() else set()
    after_restore = set(real_restore_dir.iterdir()) if real_restore_dir.is_dir() else set()
    assert after_backup == before_backup, f"leaked into the real data/_backups: {after_backup - before_backup}"
    assert after_restore == before_restore, f"leaked into the real data/_restore: {after_restore - before_restore}"
    assert not (real_backup_dir / f"{token}.tar").exists()
