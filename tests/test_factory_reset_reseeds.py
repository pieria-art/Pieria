"""Factory Reset must re-arm the seeder, not just empty the library.

Found in the 2026-07-25 UAT by actually running it. Step 5 of the reset deletes the seed artworks
deliberately, so the bootstrapper re-downloads them — but seeding is gated on the EXISTENCE of the
`pack_seeded` settings row (`core/lifespan.py:148,335`), and the reset left that row behind. Observed
end to end: 122 artworks (all is_seed=1) deleted, container restarted, and two minutes later the
library was still empty. The API's own response promises "Restart the server to re-seed masterpieces",
and on a box whose headline promise is that it can't be bricked, that promise has to be true.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import app
from database import Base, get_db
from models import ArtworkModel, SettingsModel


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c, db
    app.dependency_overrides.clear()
    db.close()


def _seeded_library(db, n=3):
    for i in range(n):
        db.add(ArtworkModel(filename=f"seed_{i}.jpg", status="approved", title=f"Seed {i}", is_seed=True))
    db.add(SettingsModel(setting_key="pack_seeded", setting_value="v2:registry"))
    db.commit()


def test_reset_clears_the_seed_gate(client):
    """The regression itself: without this the next boot decides it has already seeded and skips."""
    c, db = client
    _seeded_library(db)

    body = c.post("/api/admin/factory-reset", json={"confirm": "RESET"}).json()

    assert body["seed_gate_cleared"] is True
    assert db.query(SettingsModel).filter(SettingsModel.setting_key == "pack_seeded").first() is None


def test_reset_deletes_seed_artworks(client):
    """Pins the ACTUAL behaviour, which the UI copy used to contradict ("all except seed masterpieces")."""
    c, db = client
    _seeded_library(db)

    body = c.post("/api/admin/factory-reset", json={"confirm": "RESET"}).json()

    assert body["seed_artworks_removed"] == 3
    assert db.query(ArtworkModel).count() == 0


def test_reset_still_requires_the_typed_confirmation(client):
    """H4: the guard is server-side, not just the admin dialog. Don't let the fix above weaken it."""
    c, db = client
    _seeded_library(db)

    assert c.post("/api/admin/factory-reset", json={"confirm": "yes"}).status_code == 400
    assert c.post("/api/admin/factory-reset", json={}).status_code in (400, 422)
    assert db.query(ArtworkModel).count() == 3        # nothing was touched
    assert db.query(SettingsModel).filter(SettingsModel.setting_key == "pack_seeded").first() is not None


def test_reset_is_idempotent_on_an_already_empty_library(client):
    c, db = client
    body = c.post("/api/admin/factory-reset", json={"confirm": "RESET"}).json()
    assert body["seed_artworks_removed"] == 0
    assert body["seed_gate_cleared"] is False          # nothing to clear, and that's not an error
