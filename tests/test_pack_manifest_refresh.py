"""F8b (ADR-148): metadata-only refresh from the registry's standalone signed manifests - no tar download,
no image work. Served over an httpx MockTransport from a real publish_pack dist/."""
import json

import httpx
import pytest

import core.lifespan as lifespan
import federation
import publisher
from core import pack_fetch
from models import ArtworkModel, SubscriptionModel
from tests.test_pack_fetch import _build_source_pack, _db, _mi
from tools import build_pack, publish_pack

URL = "https://packs.test/packs.json"


def _setup(tmp_path, monkeypatch):
    priv, pub = publisher.keygen()
    src = _build_source_pack(tmp_path, priv)
    monkeypatch.setattr(lifespan, "ARTWORK_ROOT", src)
    monkeypatch.setattr(lifespan, "LIBRARY_DIR", src / "_Library")
    monkeypatch.setattr(federation, "TRUSTED_KEYS", {"pieria": pub})
    monkeypatch.setattr(federation, "_assert_public_url", lambda url: None)
    db = _db()
    for cid in ("masterpieces", "cartography"):
        lifespan._install_collection(db, cid, json.loads((src / "_manifests" / f"{cid}.json").read_text()))
    dist = tmp_path / "dist"
    publish_pack.publish(src, dist, core={"masterpieces"}, allow_unverified=True)
    return priv, src, dist, db


def _fix_credit(src, priv, credit="Corrected Museum"):
    m = dict(_mi("Map", 60), credit_line=credit)
    build_pack._emit_v2_manifests(
        src, [{"id": "cartography", "title": "Cartography", "description": "Maps", "items": [m]}],
        signing_key=priv, generated_at="2026-10-05")


class Server:
    def __init__(self, dist):
        self.dist, self.hits = dist, []

    def transport(self):
        def handler(request):
            self.hits.append(request.url.path)
            p = self.dist / request.url.path.lstrip("/")
            return httpx.Response(200, content=p.read_bytes()) if p.is_file() else httpx.Response(404)
        return httpx.MockTransport(handler)


async def _run(db, server):
    client = httpx.AsyncClient(transport=server.transport())
    try:
        return await pack_fetch.refresh_manifests_from_registry(db, client, URL)
    finally:
        await client.aclose()


def _map(db):
    return db.query(ArtworkModel).filter(ArtworkModel.filename == "map.jpg").one()


def _sub(db):
    return db.query(SubscriptionModel).filter(SubscriptionModel.url == "pack:cartography").one()


def test_publish_emits_manifest_files_and_registry_fields(tmp_path, monkeypatch):
    _, src, dist, _ = _setup(tmp_path, monkeypatch)
    reg = json.loads((dist / "packs.json").read_text())
    for row in reg["collections"]:
        assert (dist / row["manifest_url"]).is_file()
        m = json.loads((dist / row["manifest_url"]).read_text())
        assert row["manifest_sha256"] == lifespan.manifest_hash(m)
        assert (dist / row["manifest_url"]).read_bytes() == (src / "_manifests" / f"{row['id']}.json").read_bytes()


@pytest.mark.asyncio
async def test_unchanged_manifest_means_no_fetch(tmp_path, monkeypatch):
    _, src, dist, db = _setup(tmp_path, monkeypatch)
    server = Server(dist)
    assert await _run(db, server) == {}
    assert server.hits == ["/packs.json"]   # registry only - no manifest fetched


@pytest.mark.asyncio
async def test_newer_manifest_is_fetched_verified_and_applied(tmp_path, monkeypatch):
    priv, src, dist, db = _setup(tmp_path, monkeypatch)
    _fix_credit(src, priv)
    publish_pack.publish_manifests_only(src, dist)
    server = Server(dist)
    out = await _run(db, server)
    assert set(out) == {"cartography"}
    assert _map(db).attribution == "Corrected Museum"
    assert not any(h.endswith(".tar") for h in server.hits)   # no tar download
    on_disk = json.loads((src / "_manifests" / "cartography.json").read_text())
    assert _sub(db).applied_manifest_hash == lifespan.manifest_hash(on_disk)
    assert _sub(db).metadata_refreshed_at is not None
    assert await _run(db, Server(dist)) == {}   # now current


@pytest.mark.asyncio
async def test_sha_mismatch_refused_nothing_written(tmp_path, monkeypatch):
    priv, src, dist, db = _setup(tmp_path, monkeypatch)
    before = (src / "_manifests" / "cartography.json").read_bytes()
    _fix_credit(src, priv)
    publish_pack.publish_manifests_only(src, dist)
    # tamper with the hosted file AFTER the registry recorded its hash
    f = dist / "manifests" / "cartography.json"
    m = json.loads(f.read_text())
    m["title"] = "Evil"
    f.write_text(json.dumps(m))
    (src / "_manifests" / "cartography.json").write_bytes(before)
    assert await _run(db, Server(dist)) == {}
    assert (src / "_manifests" / "cartography.json").read_bytes() == before
    assert _map(db).attribution == "Some Museum"


@pytest.mark.asyncio
async def test_bad_signature_refused_even_when_sha_matches(tmp_path, monkeypatch):
    priv, src, dist, db = _setup(tmp_path, monkeypatch)
    before = (src / "_manifests" / "cartography.json").read_bytes()
    _fix_credit(src, priv)
    publish_pack.publish_manifests_only(src, dist)
    f = dist / "manifests" / "cartography.json"
    m = json.loads(f.read_text())
    m["items"][0]["title"] = "Forged"          # signed content changed, signature left as-is
    f.write_text(json.dumps(m))
    reg = json.loads((dist / "packs.json").read_text())
    for row in reg["collections"]:             # a hostile registry vouches for the forged bytes
        if row["id"] == "cartography":
            row["manifest_sha256"] = lifespan.manifest_hash(m)
    (dist / "packs.json").write_text(json.dumps(reg))
    (src / "_manifests" / "cartography.json").write_bytes(before)
    assert await _run(db, Server(dist)) == {}
    assert (src / "_manifests" / "cartography.json").read_bytes() == before
    assert _map(db).attribution == "Some Museum"
    assert _sub(db).applied_manifest_hash != row["manifest_sha256"]


@pytest.mark.asyncio
async def test_registry_down_is_a_noop(tmp_path, monkeypatch):
    _, _, dist, db = _setup(tmp_path, monkeypatch)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert await pack_fetch.refresh_manifests_from_registry(db, client, URL) == {}
    await client.aclose()


@pytest.mark.asyncio
async def test_old_registry_without_manifest_fields_is_a_noop(tmp_path, monkeypatch):
    priv, src, dist, db = _setup(tmp_path, monkeypatch)
    reg = json.loads((dist / "packs.json").read_text())
    for row in reg["collections"]:
        row.pop("manifest_url", None)
        row.pop("manifest_sha256", None)
    (dist / "packs.json").write_text(json.dumps(reg))
    server = Server(dist)
    assert await _run(db, server) == {}
    assert server.hits == ["/packs.json"]
