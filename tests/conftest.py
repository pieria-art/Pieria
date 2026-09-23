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
