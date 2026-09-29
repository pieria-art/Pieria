"""GET /api/credits — Admin -> About -> Credits (ADR-142): every installed work requiring attribution."""

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


def test_credits_lists_only_cc_by_works(client):
    c, db = client
    db.add(ArtworkModel(filename="a.jpg", title="Public domain work", status="approved",
                         license="PDM-1.0"))
    db.add(ArtworkModel(filename="b.jpg", title="Cosmic Cliffs", agent_name="ESA/Webb",
                         status="approved", license="CC-BY-4.0",
                         license_url="https://creativecommons.org/licenses/by/4.0/",
                         attribution="ESA/Webb, NASA & CSA, A. Martel",
                         origin_url="https://esawebb.org/images/cliffs"))
    db.commit()

    r = c.get("/api/credits")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["title"] == "Cosmic Cliffs"
    assert item["license"] == "CC-BY-4.0"
    assert item["license_name"] == "CC BY 4.0"
    assert item["attribution"] == "ESA/Webb, NASA & CSA, A. Martel"
    assert item["source_page_url"] == "https://esawebb.org/images/cliffs"
    assert "public domain" in body["note"].lower()


def test_credits_falls_back_to_license_urls_when_row_has_none(client):
    """A CC BY row missing its own license_url (e.g. an older install) still gets a linkable licence
    URL — falls back to core.licensing.LICENSE_URLS by id."""
    c, db = client
    db.add(ArtworkModel(filename="c.jpg", title="Nebula", status="approved", license="CC-BY-4.0",
                         attribution="Some Photographer"))
    db.commit()

    r = c.get("/api/credits")
    item = r.json()["items"][0]
    assert item["license_url"] == "https://creativecommons.org/licenses/by/4.0/"


def test_credits_empty_when_no_cc_by_installed(client):
    c, db = client
    db.add(ArtworkModel(filename="a.jpg", title="PD work", status="approved", license="PDM-1.0"))
    db.commit()

    r = c.get("/api/credits")
    assert r.json()["items"] == []
