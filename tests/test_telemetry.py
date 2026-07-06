"""Director affinity math — the /api/telemetry/heartbeat feedback loop.

Canvas clients report how long each artwork was shown and whether it was skipped. That signal
drives affinity_score, which in turn weights the bag-shuffle draw. This pins the v1 math (a naive
scheme flagged for future evolution) so a later "evolution" can't silently change it unnoticed.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import app
from database import Base, get_db
from models import ArtworkModel


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


def _artwork(db, **kw):
    art = ArtworkModel(filename="a.jpg", status="approved", **kw)
    db.add(art); db.commit(); db.refresh(art)
    return art


def _beat(c, art_id, secs, skipped):
    return c.post("/api/telemetry/heartbeat",
                  json={"artwork_id": art_id, "display_time_sec": secs, "skipped": skipped})


def test_natural_display_rewards_affinity(client):
    c, db = client
    art = _artwork(db)                               # affinity defaults to 1.0
    r = _beat(c, art.id, 30, skipped=False)          # 30s == one interval == +0.05
    assert r.status_code == 200
    assert r.json()["affinity"] == pytest.approx(1.05)


def test_skip_penalizes_affinity(client):
    c, db = client
    art = _artwork(db)
    assert _beat(c, art.id, 2, skipped=True).json()["affinity"] == pytest.approx(0.9)   # -0.1


def test_skip_penalty_floors_at_min(client):
    c, db = client
    art = _artwork(db)
    for _ in range(20):                              # 20 skips would drive it negative if unclamped
        last = _beat(c, art.id, 1, skipped=True).json()["affinity"]
    assert last == pytest.approx(0.1)                # clamped to the 0.1 floor


def test_reward_caps_at_ceiling(client):
    c, db = client
    art = _artwork(db)
    last = _beat(c, art.id, 3600, skipped=False).json()["affinity"]   # 120 intervals == +6.0 uncapped
    assert last == pytest.approx(5.0)                # clamped to the 5.0 ceiling


def test_raw_counters_accumulate(client):
    c, db = client
    art = _artwork(db)
    _beat(c, art.id, 30, skipped=False)
    _beat(c, art.id, 45, skipped=True)
    db.refresh(art)
    assert art.total_display_time == 75              # both heartbeats' seconds summed
    assert art.skip_count == 1                        # only the skipped one counts


def test_heartbeat_unknown_artwork_404(client):
    c, _ = client
    assert _beat(c, 999999, 10, skipped=False).status_code == 404
