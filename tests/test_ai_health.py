"""AI engine health reporting — why enrichment failed, surfaced instead of swallowed.

`has_key` only proves a key EXISTS. A key that is invalid, expired, revoked, or over quota passes every
configuration check and then fails at call time. Before this, that produced an artwork in the Review
Queue with a null title and no explanation anywhere but the server log — found in the 2026-07-25 UAT on
the documented quick-start path (README tells users to put GEMINI_API_KEY in .env, and an .env key never
passes through the in-app "Test & Save" validation that would have caught it).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import ai_client
from app import app
from database import Base, get_db
from models import SettingsModel


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = Session()

    # ai_client opens its OWN session (callers record failures mid-rollback) — point it at the same
    # in-memory engine, or the health write lands in a different database than the assertions read.
    monkeypatch.setattr(ai_client, "SessionLocal", Session)

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c, db
    app.dependency_overrides.clear()
    db.close()


def test_record_and_read_failure(client):
    _, db = client
    ai_client.record_failure("Model API error 400: Please pass a valid API key")

    got = ai_client.get_failure()
    assert "Please pass a valid API key" in got["detail"]
    assert got["at"]                                  # ISO timestamp recorded


def test_clear_failure_on_success(client):
    _, db = client
    ai_client.record_failure("boom")
    assert ai_client.get_failure()["detail"]

    ai_client.clear_failure()
    assert ai_client.get_failure()["detail"] == ""


def test_failure_is_surfaced_on_the_settings_endpoint(client):
    """The admin already polls /api/settings/ai for `has_key` — health rides along, no new polling."""
    c, _ = client
    ai_client.record_failure("Model API error 429: quota exceeded")

    body = c.get("/api/settings/ai").json()
    assert "quota exceeded" in body["last_error"]
    assert body["last_error_at"]


def test_healthy_engine_reports_no_error(client):
    c, _ = client
    body = c.get("/api/settings/ai").json()
    assert body["last_error"] == ""


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-abcdefghijklmnop",
    "AIzaSyBKtxltZejlhT5LEnoi8tqzEm",
    "ghp_aaaaaaaaaaaaaaaaaaaa",
])
def test_credentials_are_redacted_before_storage(client, secret):
    """Some providers echo the offending key back in the error body. This record is read straight into
    the admin UI, so a leaked key would be rendered on screen and persisted in the settings table."""
    _, db = client
    ai_client.record_failure(f"401 unauthorized for key {secret}")

    stored = ai_client.get_failure()["detail"]
    assert secret not in stored
    assert "<redacted>" in stored


def test_failure_detail_is_truncated(client):
    _, db = client
    ai_client.record_failure("x" * 5000)
    assert len(ai_client.get_failure()["detail"]) <= 300


def test_record_failure_survives_a_rolled_back_caller_session(client):
    """agents.process_artwork records the failure from inside an `except` that already rolled its own
    session back. The health write must use a private session or it is lost with that rollback."""
    _, db = client
    db.add(SettingsModel(setting_key="scratch", setting_value="uncommitted"))
    db.rollback()

    ai_client.record_failure("recorded after a rollback")
    assert "recorded after a rollback" in ai_client.get_failure()["detail"]
