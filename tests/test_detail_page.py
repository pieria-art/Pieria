"""The server-hosted /art/{id} 'Learn More' page the placard QR points at."""

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


def test_detail_page_renders(client):
    c, db = client
    art = ArtworkModel(
        filename="x.jpg", title="The Starry Night", agent_name="Vincent van Gogh",
        date_display="1889", cultural_context="Post-Impressionism", medium="Oil on canvas",
        description_narrative="A night sky over a village.", tags="night, sky",
        source_url="https://museum.test/starry", status="approved")
    db.add(art); db.commit(); db.refresh(art)

    r = c.get(f"/art/{art.id}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "The Starry Night" in body
    assert "Vincent van Gogh" in body
    assert f"/artworks/{art.id}/preview" in body   # hero image points at our server, not a CDN
    assert "https://museum.test/starry" in body    # source link present


def test_detail_page_shows_credit_and_origin_link(client):
    """ADR-142: every work shows its credit + licence (linked), and the source link uses origin_url —
    the fix for pack installs whose source_url is a broken `pack:…` sentinel."""
    c, db = client
    art = ArtworkModel(
        filename="webb.jpg", title="Cosmic Cliffs", agent_name="ESA/Webb", status="approved",
        source_url="pack:webb.jpg",
        license="CC-BY-4.0", license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution="ESA/Webb, NASA & CSA, A. Martel", origin_url="https://esawebb.org/images/cliffs")
    db.add(art); db.commit(); db.refresh(art)

    r = c.get(f"/art/{art.id}")
    body = r.text
    assert "ESA/Webb, NASA &amp; CSA, A. Martel" in body
    assert 'href=\'https://creativecommons.org/licenses/by/4.0/\'' in body
    assert "CC BY 4.0" in body
    assert 'href=\'https://esawebb.org/images/cliffs\'' in body
    assert "pack:webb.jpg" not in body   # never link the broken sentinel


def test_detail_page_never_links_a_non_http_url(client):
    """ADR-142 review (BLOCKING): license_url/origin_url are manifest/catalog-supplied and untrusted —
    a javascript:/data: value must render as plain text, never as an href (stored XSS otherwise)."""
    c, db = client
    art = ArtworkModel(
        filename="evil.jpg", title="Evil Work", status="approved",
        license="CC-BY-4.0", license_url="javascript:alert(1)",
        attribution="Some Credit", origin_url="javascript:alert(document.cookie)")
    db.add(art); db.commit(); db.refresh(art)

    r = c.get(f"/art/{art.id}")
    body = r.text
    assert "javascript:" not in body            # neither URL ever reaches an href
    assert "Some Credit" in body                # the credit text itself still shows
    assert "CC BY 4.0" in body                  # licence name still shows, just unlinked
    assert "View original source" not in body   # no source link when origin_url is unsafe


def test_detail_page_hides_a_bare_url_credit(client):
    """~460 catalog rows carry a source URL in credit_line; it must not render as the credit text."""
    c, db = client
    art = ArtworkModel(
        filename="u.jpg", title="Url Credit", status="approved", license="PDM-1.0",
        attribution="https://www.artic.edu/artworks/8971", origin_url="https://example.org/work")
    db.add(art); db.commit(); db.refresh(art)
    body = c.get(f"/art/{art.id}").text
    assert "artic.edu/artworks/8971" not in body
    assert "Public domain" in body              # the licence still shows
    assert "View original source" in body


def test_detail_page_escapes_html(client):
    c, db = client
    art = ArtworkModel(filename="y.jpg", title="<script>alert(1)</script>", status="approved")
    db.add(art); db.commit(); db.refresh(art)
    r = c.get(f"/art/{art.id}")
    assert "<script>alert(1)</script>" not in r.text   # escaped, not injected
    assert "&lt;script&gt;" in r.text


def test_detail_page_404(client):
    c, _ = client
    assert c.get("/art/999999").status_code == 404
