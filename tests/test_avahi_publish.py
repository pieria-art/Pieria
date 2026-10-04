"""sd-avahi-publish: the generated `_pieria._tcp` record (escaping, fail-closed) + the identity route."""

import importlib.machinery
import importlib.util
import pathlib
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
from app import app
from database import Base, get_db

_PATH = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "appliance" / "bin" / "sd-avahi-publish"
_loader = importlib.machinery.SourceFileLoader("sd_avahi_publish", str(_PATH))
pub = importlib.util.module_from_spec(importlib.util.spec_from_loader("sd_avahi_publish", _loader))
_loader.exec_module(pub)

UUID = "123e4567-e89b-12d3-a456-426614174000"


def _txt(xml: str) -> dict:
    body = xml.split("?>", 1)[1].replace('<!DOCTYPE service-group SYSTEM "avahi-service.dtd">', "")
    svc = ET.fromstring(body).find("service")
    return {"type": svc.find("type").text, "port": svc.find("port").text,
            "txt": [t.text for t in svc.findall("txt-record")]}


def test_render_is_well_formed_xml_with_all_txt():
    out = _txt(pub.render("1.0.6", UUID.upper(), 8000))
    assert out["type"] == "_pieria._tcp" and out["port"] == "8000"
    assert out["txt"] == ["version=1.0.6", f"server_id={UUID}", "path=/api/v1"]


@pytest.mark.parametrize("version", ["1.0<6", "1.0&6", "1.0\n6", "", "a b", "</txt-record><x/>", "x" * 80])
def test_render_rejects_hostile_version(version):
    with pytest.raises(ValueError):
        pub.render(version, UUID, 8000)


@pytest.mark.parametrize("sid", ["", "not-a-uuid", UUID + "<", "'; rm -rf /", None])
def test_render_rejects_bad_server_id(sid):
    with pytest.raises(ValueError):
        pub.render("1.0.6", sid, 8000)


def test_escape_is_applied_even_for_legal_chars(monkeypatch):
    # The validators already exclude markup; prove the escape layer is still in the pipeline.
    monkeypatch.setattr(pub, "escape", lambda s: s.replace("+", "&#43;"))
    assert "version=1.0&#43;rc1" in pub.render("1.0+rc1", UUID, 8000)


def test_write_if_changed_is_atomic_and_idempotent(tmp_path):
    f = tmp_path / "pieria-api.service"
    assert pub.write_if_changed(str(f), "a") is True
    assert pub.write_if_changed(str(f), "a") is False
    assert pub.write_if_changed(str(f), "b") is True and f.read_text() == "b"
    assert (f.stat().st_mode & 0o777) == 0o644
    assert [p.name for p in tmp_path.iterdir()] == ["pieria-api.service"]


def test_unreachable_app_fails_closed_and_leaves_file(tmp_path):
    f = tmp_path / "pieria-api.service"
    f.write_text("OLD")
    rc = pub.main(["--url", "http://127.0.0.1:1", "--out", str(f), "--wait", "0"])
    assert rc == 1 and f.read_text() == "OLD"


def test_malformed_identity_fails_closed(tmp_path, monkeypatch):
    f = tmp_path / "x.service"
    monkeypatch.setattr(pub, "fetch_identity", lambda url, wait: {"version": "1.0", "server_id": "<evil>"})
    assert pub.main(["--out", str(f)]) == 1 and not f.exists()


def test_main_writes_record(tmp_path, monkeypatch):
    f = tmp_path / "x.service"
    monkeypatch.setattr(pub, "fetch_identity", lambda url, wait: {"version": "2.1.0", "server_id": UUID})
    assert pub.main(["--url", "http://127.0.0.1:8123", "--out", str(f)]) == 0
    assert _txt(f.read_text())["port"] == "8123"


def test_identity_route_unauthenticated_and_stable():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()

    def _override():
        yield db
    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as c:
            a = c.get("/api/health/identity")
            b = c.get("/api/health/identity")
        assert a.status_code == 200
        body = a.json()
        assert body["version"] == config.APP_VERSION and body["server_id"] == b.json()["server_id"]
        pub.render(body["version"], body["server_id"], 8000)   # what the host will accept
    finally:
        app.dependency_overrides.clear()
        db.close()
