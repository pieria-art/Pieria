import os
import socket

# F1 hang (2026-08-29, CI run 35802297181): the app's lifespan starts an OOB first-boot seed
# background task (core/lifespan.seed_from_registry) that hits the REAL network. On a GH runner that
# gets a 403 forever, so the retry loop never lands — and until core/lifespan.py's shutdown learned to
# cancel+await its own background tasks with a bound, that left TestClient's anyio portal thread stuck
# forever in asyncio shutdown. This env var must be set BEFORE `config`/`app` are imported anywhere in
# the suite, which is why it's module-level code here rather than inside a fixture: pytest imports
# conftest.py for a directory before it imports that directory's test modules, so this always runs
# first. Tests that exercise the seed on purpose (tests/test_oob_seed.py) monkeypatch
# config.DISABLE_BOOT_SEED back to False.
os.environ.setdefault("SD_DISABLE_BOOT_SEED", "1")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base

# Loopback only — TestClient talks to the ASGI app in-process and shouldn't need real sockets at all;
# anything else means a test (or the app it's exercising) is reaching for the real network.
_ALLOWED_NETWORK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_network: this test legitimately needs a real outbound socket "
        "(opt-out of the autouse _block_real_network guard in conftest.py).",
    )


@pytest.fixture(autouse=True)
def _block_real_network(request, monkeypatch):
    """Belt-and-braces guard (see the SD_DISABLE_BOOT_SEED note above): block any outbound socket
    connect to a non-loopback host during tests, so a test that accidentally reaches the real network
    fails fast and loud instead of hanging a GH runner. There should be no tests in CI that need this
    opt-out — if one fails under the guard, report which and why rather than silently marking it."""
    if request.node.get_closest_marker("allow_network"):
        yield
        return

    real_connect = socket.socket.connect

    def _guarded_connect(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _ALLOWED_NETWORK_HOSTS:
            raise RuntimeError(
                f"blocked outbound network connect to {address!r} during tests "
                "(tests/conftest.py::_block_real_network) — mock/stub it instead"
            )
        return real_connect(self, address, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    yield


@pytest.fixture(autouse=True)
def _isolate_backup_restore_paths(tmp_path, monkeypatch):
    """Belt-and-braces, for EVERY test (not just backup/restore ones): a prior isolation gap let a full
    suite run write multi-GB archives into the checkout's real data/_backups and rewrite its real
    data/_restore status.json — some test somewhere exercised Backup & Restore without going through
    tests/test_backup_restore.py's own `env` fixture. Redirect every path core.backup/core.restore/
    core.restore_boot resolve, on every module that binds its own copy (this repo's established pattern
    for derived path constants — see DERIVATIVES_DIR in other tests). A test that legitimately wants
    THIS feature's own isolated tmp dirs (tests/test_backup_restore.py's `env` fixture) still gets them —
    its own monkeypatch.setattr calls simply run after this one and win.

    Deliberately does NOT touch database.SQLALCHEMY_DATABASE_URL (the whole app's DB, not just this
    feature's) — that's a much bigger blast radius than what leaked, and every test that legitimately
    needs a real DB already gets its own isolated one via `testing_session` or an app-level override."""
    backup_dir = tmp_path / "_isolated_backups"
    restore_dir = tmp_path / "_isolated_restore"
    library_dir = tmp_path / "_isolated_library"
    artwork_root = tmp_path / "_isolated_artwork_root"
    db_url = f"sqlite:///{tmp_path / '_isolated_artwork.db'}"

    import config
    monkeypatch.setattr(config, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(config, "RESTORE_DIR", restore_dir)

    import core.backup as backup_module
    monkeypatch.setattr(backup_module, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(backup_module, "STATUS_FILE", backup_dir / "status.json")
    monkeypatch.setattr(backup_module, "ARTWORK_ROOT", artwork_root)
    monkeypatch.setattr(backup_module, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(backup_module, "SQLALCHEMY_DATABASE_URL", db_url)

    import core.restore as restore_module
    monkeypatch.setattr(restore_module, "RESTORE_DIR", restore_dir)
    monkeypatch.setattr(restore_module, "UPLOAD_PATH", restore_dir / "upload.tar")
    monkeypatch.setattr(restore_module, "VALIDATED_DIR", restore_dir / "validated")
    monkeypatch.setattr(restore_module, "STAGED_DIR", restore_dir / "staged")
    monkeypatch.setattr(restore_module, "PRE_RESTORE_DB", restore_dir / "pre-restore.db")
    monkeypatch.setattr(restore_module, "STATUS_FILE", restore_dir / "status.json")

    import core.restore_boot as restore_boot_module
    monkeypatch.setattr(restore_boot_module, "RESTORE_DIR", restore_dir)
    monkeypatch.setattr(restore_boot_module, "STAGED_DIR", restore_dir / "staged")
    monkeypatch.setattr(restore_boot_module, "PRE_RESTORE_DB", restore_dir / "pre-restore.db")
    monkeypatch.setattr(restore_boot_module, "STATUS_FILE", restore_dir / "status.json")
    monkeypatch.setattr(restore_boot_module, "ARTWORK_ROOT", artwork_root)
    monkeypatch.setattr(restore_boot_module, "SQLALCHEMY_DATABASE_URL", db_url)

    yield


@pytest.fixture(scope="function")
def testing_session():
    # Use an isolated, in-memory SQLite database
    # This guarantees tests are fast and don't pollute artwork.db
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Initialize all tables in the memory engine
    Base.metadata.create_all(bind=engine)

    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
