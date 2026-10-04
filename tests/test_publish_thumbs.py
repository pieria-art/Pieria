"""ADR-148: self-hosted catalog thumbnails — derivation, naming, index.json, catalog rewrite, the admin
onerror fallback, and the local installed-pack thumb route. Offline; no network."""
import hashlib
import io
import json
from pathlib import Path

from PIL import Image

import routers.catalog as routers_catalog
from tests.test_catalog import ITEM_A, _no_dns, client  # noqa: F401  (fixtures)
from tools import build_pack, publish_thumbs

ROOT = Path(__file__).resolve().parent.parent


def _jpeg(path: Path, size=(1600, 1000), exif=False):
    img = Image.new("RGB", size, (200, 30, 30))
    kw = {}
    if exif:
        ex = Image.Exif()
        ex[0x010F] = "SomeCamera"
        kw["exif"] = ex
    img.save(path, "JPEG", **kw)


def test_derive_thumbnail_size_format_and_stripping(tmp_path):
    src = tmp_path / "m.jpg"
    _jpeg(src, (1600, 1000), exif=True)
    data = publish_thumbs.derive_thumbnail(src)
    im = Image.open(io.BytesIO(data))
    assert im.format == "JPEG" and im.mode == "RGB"
    assert max(im.size) == 600 and im.size == (600, 375)
    assert im.info.get("progressive") == 1
    assert len(im.getexif()) == 0 and "icc_profile" not in im.info


def test_derive_never_upscales_and_handles_png_alpha():
    buf = io.BytesIO()
    Image.new("RGBA", (200, 100), (0, 0, 0, 128)).save(buf, "PNG")
    im = Image.open(io.BytesIO(publish_thumbs.derive_thumbnail(buf.getvalue())))
    assert im.size == (200, 100) and im.mode == "RGB"


def test_naming_is_deterministic_sha256_of_source_url():
    u = "https://example.test/a/full.jpg"
    assert publish_thumbs.thumb_name(u) == hashlib.sha256(u.encode()).hexdigest() + ".jpg"
    assert publish_thumbs.thumb_name(u) == publish_thumbs.thumb_name(u)
    assert publish_thumbs.thumb_name(u) != publish_thumbs.thumb_name(u + "x")
    assert publish_thumbs.thumb_url("https://packs.curwe.ai/", u) == \
        f"https://packs.curwe.ai/thumbs/{publish_thumbs.thumb_name(u)}"


def test_default_base_url_derives_from_registry():
    assert publish_thumbs.default_base_url() == "https://packs.curwe.ai"


def _fixture(tmp_path):
    """catalog with 2 items (one has a pack thumb, one only a master) + a pack-only manifest item."""
    cat = tmp_path / "catalog"
    cat.mkdir()
    a = dict(ITEM_A, title="Alpha", source_url="https://x.test/a.jpg", thumbnail_url="https://x.test/a_t.jpg")
    b = dict(ITEM_A, title="Beta", source_url="https://x.test/b.jpg", thumbnail_url="https://x.test/b_t.jpg")
    (cat / "demo.json").write_text(json.dumps({"id": "demo", "title": "Demo", "items": [a, b]}))
    (cat / "index.json").write_text(json.dumps({"version": 1, "collections": [
        {"id": "demo", "title": "Demo", "cover_thumbnail": a["thumbnail_url"]}]}))
    pack = tmp_path / "art-pack"
    (pack / "_catalog_thumbs").mkdir(parents=True)
    (pack / "_Library").mkdir()
    (pack / "_manifests").mkdir()
    _jpeg(pack / "_catalog_thumbs" / build_pack.thumb_filename(a["source_url"]), (600, 400))
    _jpeg(pack / "_Library" / "demo__beta.jpg", (4000, 3000))
    _jpeg(pack / "_Library" / "demo__gamma.jpg", (800, 800))
    items = [
        {"title": "Beta", "image": {"local_file": "demo__beta.jpg", "origin_url": b["source_url"]}},
        {"title": "Gamma", "image": {"local_file": "demo__gamma.jpg", "origin_url": "https://x.test/g.jpg"}},
    ]
    (pack / "_manifests" / "demo.json").write_text(json.dumps({"id": "demo", "items": items}))
    return cat, pack, a, b


def test_build_thumbs_writes_files_and_index(tmp_path):
    cat, pack, a, b = _fixture(tmp_path)
    before = sorted(p.name for p in pack.rglob("*"))
    out = tmp_path / "out" / "thumbs"
    index = publish_thumbs.build_thumbs(cat, pack, out)
    assert index.pop("_failed") == []
    on_disk = json.loads((out / "index.json").read_text())
    assert on_disk == index and on_disk["version"] == 1
    # every catalog item + the pack-only Gamma
    assert set(on_disk["items"]) == {a["source_url"], b["source_url"], "https://x.test/g.jpg"}
    ea = on_disk["items"][a["source_url"]]
    assert ea["origin"] == "pack-thumb" and on_disk["items"][b["source_url"]]["origin"] == "master"
    assert ea["path"] == f"thumbs/{publish_thumbs.thumb_name(a['source_url'])}"
    f = out / publish_thumbs.thumb_name(a["source_url"])
    assert hashlib.sha256(f.read_bytes()).hexdigest() == ea["sha256"] and f.stat().st_size == ea["bytes"]
    assert max(Image.open(out / publish_thumbs.thumb_name(b["source_url"])).size) == 600
    assert sorted(p.name for p in pack.rglob("*")) == before   # pack untouched (read-only)
    # deterministic: a second run produces identical bytes
    out2 = tmp_path / "out2"
    publish_thumbs.build_thumbs(cat, pack, out2)
    assert (out2 / "index.json").read_text() == (out / "index.json").read_text()


def test_missing_source_is_reported_not_fetched(tmp_path):
    cat, pack, a, b = _fixture(tmp_path)
    (pack / "_Library" / "demo__beta.jpg").unlink()
    index = publish_thumbs.build_thumbs(cat, pack, tmp_path / "o")
    assert index["_failed"] == [b["source_url"]]   # no network without --fetch-remote


def test_catalog_rewrite_keeps_provenance_and_is_idempotent(tmp_path):
    cat, pack, a, b = _fixture(tmp_path)
    index = publish_thumbs.build_thumbs(cat, pack, tmp_path / "o")
    items = {k: v for k, v in index["items"].items() if k != "https://x.test/g.jpg"}
    del items[b["source_url"]]   # b has no uploaded thumb -> keeps its hotlink
    base = "https://packs.curwe.ai"
    publish_thumbs.rewrite_catalog(cat, items, base)
    publish_thumbs.rewrite_catalog(cat, items, base)
    col = json.loads((cat / "demo.json").read_text())
    ia, ib = col["items"]
    assert ia["thumbnail_url"] == publish_thumbs.thumb_url(base, a["source_url"])
    assert ia["thumbnail_source_url"] == a["thumbnail_url"]       # original, not overwritten on rerun
    assert ib["thumbnail_url"] == b["thumbnail_url"] and "thumbnail_source_url" not in ib
    idx = json.loads((cat / "index.json").read_text())
    assert idx["collections"][0]["cover_thumbnail"] == ia["thumbnail_url"]
    assert idx["collections"][0]["cover_thumbnail_source"] == a["thumbnail_url"]


def test_admin_has_onerror_fallback_and_cache_bump():
    js = (ROOT / "static" / "admin.js").read_text()
    assert "thumbnail_source_url" in js and 'onerror="catalogThumbFallback(this)"' in js
    assert "function catalogThumbFallback" in js
    assert "admin.js?v=20261005b" in (ROOT / "static" / "admin.html").read_text()


# --- local installed-pack thumb route ---------------------------------------------------------------
def _install_thumb(tmp_path, source_url):
    h = hashlib.sha1(source_url.encode()).hexdigest()[:12]
    d = tmp_path / "_catalog_thumbs"
    d.mkdir(exist_ok=True)
    _jpeg(d / f"{h}.jpg", (60, 40))
    return h


def test_local_thumb_route_serves_installed_thumb(client, tmp_path):  # noqa: F811
    c, _ = client
    h = _install_thumb(tmp_path, ITEM_A["source_url"])
    r = c.get(f"/api/catalog/thumb/{h}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(r.content)).size == (60, 40)


def test_catalog_items_use_local_thumb_when_installed(client, tmp_path):  # noqa: F811
    c, _ = client
    h = _install_thumb(tmp_path, ITEM_A["source_url"])
    items = {i["title"]: i for i in c.get("/api/catalog/demo").json()["items"]}
    assert items["Test Sunrise"]["thumbnail_url"] == f"/api/catalog/thumb/{h}"
    assert items["Test Sunrise"]["thumbnail_source_url"] == ITEM_A["thumbnail_url"]
    assert items["Test Dusk"]["thumbnail_url"].startswith("https://")   # not installed -> unchanged
    hit = c.get("/api/catalog/search", params={"q": "sunrise"}).json()["results"][0]
    assert hit["thumbnail_url"] == f"/api/catalog/thumb/{h}"


def test_local_thumb_route_refuses_bad_ids(client, tmp_path):  # noqa: F811
    c, _ = client
    _install_thumb(tmp_path, ITEM_A["source_url"])
    (tmp_path / "outside.jpg").write_bytes(b"x")
    for bad in ("..%2foutside", "%2e%2e%2f%2e%2e%2fetc%2fpasswd", "%2Fetc%2Fpasswd",
                "zzzzzzzzzzzz", "ABCDEF123456", "abc", "0123456789abcd", "outside", "0123456789ab"):
        assert c.get(f"/api/catalog/thumb/{bad}").status_code == 404, bad
    # the HTTP client normalises a bare `..` away before routing, so prove the handler itself refuses it
    import asyncio

    import pytest
    from fastapi import HTTPException

    for bad in ("..", ".", "../outside", "/etc/passwd", "..\\outside"):
        with pytest.raises(HTTPException) as e:
            asyncio.run(routers_catalog.get_catalog_thumb(bad))
        assert e.value.status_code == 404
