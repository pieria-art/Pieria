"""build_pack pure-function units (no network). Focus: the Wikimedia native-fetch upgrade
(ADR-038 / CURATION-v2) — the catalog stores width=3840 source_urls, but the pack must fetch the
native original so masters clear the >=5120 4K floor. See [[catalog-3840-vs-pack-5120]]."""

import httpx
import pytest

from tools import build_pack
from tools.aic_tiles import image_id_of, is_aic_iiif
from tools.build_pack import _pack_fetch_url


def test_pack_fetch_url_strips_wikimedia_width():
    """A width-capped Wikimedia Special:FilePath URL is rewritten to the native original (no width)."""
    su = "https://commons.wikimedia.org/wiki/Special:FilePath/Mona%20Lisa.jpg?width=3840"
    out = _pack_fetch_url(su)
    assert "width=" not in out
    assert out.startswith("https://commons.wikimedia.org/wiki/Special:FilePath/Mona%20Lisa.jpg")


def test_pack_fetch_url_preserves_other_query_params():
    su = "https://commons.wikimedia.org/wiki/Special:FilePath/X.jpg?width=3840&page=2"
    out = _pack_fetch_url(su)
    assert "width=" not in out
    assert "page=2" in out


def test_pack_fetch_url_leaves_museum_urls_unchanged():
    """Museum full/max originals are already native-max — untouched."""
    su = "https://images.metmuseum.org/CRDImages/ep/original/DP-24049-001.jpg"
    assert _pack_fetch_url(su) == su


def test_pack_fetch_url_leaves_non_filepath_wikimedia_unchanged():
    su = "https://upload.wikimedia.org/wikipedia/commons/e/ea/Some_File.jpg"
    assert _pack_fetch_url(su) == su


def test_is_aic_iiif():
    assert is_aic_iiif("https://www.artic.edu/iiif/2/abc-123/full/max/0/default.jpg")
    assert is_aic_iiif("https://www.artic.edu/iiif/2/abc/1024,0,512,512/full/0/default.jpg")
    assert not is_aic_iiif("https://commons.wikimedia.org/wiki/Special:FilePath/X.jpg")
    assert not is_aic_iiif("https://images.metmuseum.org/CRDImages/ep/original/DP.jpg")


def test_aic_image_id_of():
    assert image_id_of("https://www.artic.edu/iiif/2/4a076002-dffe/full/max/0/default.jpg") == "4a076002-dffe"
    assert image_id_of("https://www.artic.edu/iiif/2/xyz/2048,0,1024,1024/full/0/default.jpg") == "xyz"
    assert image_id_of("https://commons.wikimedia.org/wiki/Special:FilePath/X.jpg") is None


# --- size-aware streaming fetch (ADR-040 Step-5 huge-native timeout fix) ---------------------

def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_fetch_bytes_streams_full_image():
    """A normal 200 image streams through and returns the exact bytes (no total-time ceiling)."""
    body = b"\xff\xd8\xff" + b"pixels" * 500
    async with _mock_client(lambda r: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=body)) as c:
        assert await build_pack._fetch_bytes(c, "https://example.org/master.jpg") == body


@pytest.mark.asyncio
async def test_fetch_bytes_enforces_byte_cap(monkeypatch):
    """A body past FETCH_MAX_BYTES is refused (runaway guard) rather than buffered forever."""
    monkeypatch.setattr(build_pack, "FETCH_MAX_BYTES", 1024)
    big = b"x" * 8192
    async with _mock_client(lambda r: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=big)) as c:
        assert await build_pack._fetch_bytes(c, "https://example.org/gigapixel.jpg") is None


@pytest.mark.asyncio
async def test_fetch_bytes_follows_redirect_then_streams(monkeypatch):
    """A redirect hop is followed (SSRF-validated) and the final image streams back."""
    monkeypatch.setattr(build_pack.federation, "_assert_public_url", lambda url: None)  # hermetic: skip DNS
    body = b"\xff\xd8final"

    def handler(request):
        if request.url.path == "/redir.jpg":
            return httpx.Response(302, headers={"location": "https://example.org/real.jpg"})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=body)

    async with _mock_client(handler) as c:
        assert await build_pack._fetch_bytes(c, "https://example.org/redir.jpg") == body


@pytest.mark.asyncio
async def test_fetch_bytes_rejects_non_image():
    async with _mock_client(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")) as c:
        assert await build_pack._fetch_bytes(c, "https://example.org/page") is None


# --- frame-crop step (ADR-043: consume the pre-baked crop_box deterministically) -----------------
import asyncio
from io import BytesIO

from PIL import Image


def _jpeg(w: int, h: int) -> bytes:
    """A solid-colour JPEG of exact pixel dims (a stand-in native master)."""
    buf = BytesIO()
    Image.new("RGB", (w, h), (120, 60, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _dims(raw: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(raw)) as im:
        return im.size


def test_apply_crop_box_crops_to_rect():
    """A normalized box maps 1:1 onto native pixels — a centred half-box halves each edge."""
    out = build_pack._apply_crop_box(_jpeg(4000, 3000), [0.25, 0.25, 0.75, 0.75])
    assert out is not None
    w, h = _dims(out)
    assert abs(w - 2000) <= 1 and abs(h - 1500) <= 1


def test_apply_crop_box_fullframe_is_noop():
    """An 'already clean' [0,0,1,1] box returns None so the caller keeps the untouched raw."""
    assert build_pack._apply_crop_box(_jpeg(4000, 3000), [0.0, 0.0, 1.0, 1.0]) is None


@pytest.mark.parametrize("box", [None, [0.1, 0.1, 0.2], [0.8, 0.1, 0.2, 0.9], "nope", [0.0, 0.0, 0.0, 1.0]])
def test_apply_crop_box_rejects_bad_boxes(box):
    """Missing / wrong-arity / inverted / degenerate boxes are refused (caller keeps raw)."""
    assert build_pack._apply_crop_box(_jpeg(1000, 1000), box) is None


def _build_state(tmp_path):
    return build_pack.BuildState(
        pack_dir=tmp_path,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        sem=asyncio.Semaphore(1),
        min_edge=3840,
    )


@pytest.mark.asyncio
async def test_ensure_master_crop_bypasses_floor(tmp_path, monkeypatch):
    """A flagged item cropped below the 3840 floor is KEPT (frameless > absent) and counted cropped."""
    monkeypatch.setattr(build_pack, "_fetch_bytes", lambda *a, **k: _async(_jpeg(4000, 4000)))
    state = _build_state(tmp_path)
    wi = build_pack.WorkItem(kind="catalog", collection_id="impressionism", item={
        "source_url": "https://example.org/framed.jpg", "title": "Framed Work",
        "needs_frame_crop": True, "crop_box": [0.3, 0.3, 0.7, 0.7],   # -> ~1600px, below floor
    })
    name = await build_pack.ensure_master(state, wi)
    assert name is not None                         # kept despite being under the floor
    assert state.stats.master_cropped == 1
    assert state.stats.master_too_small == 0
    await state.client.aclose()


@pytest.mark.asyncio
async def test_ensure_master_uncropped_below_floor_skipped(tmp_path, monkeypatch):
    """Control: an UNflagged sub-floor master is still rejected as too-small."""
    monkeypatch.setattr(build_pack, "_fetch_bytes", lambda *a, **k: _async(_jpeg(2000, 2000)))
    state = _build_state(tmp_path)
    wi = build_pack.WorkItem(kind="catalog", collection_id="impressionism", item={
        "source_url": "https://example.org/small.jpg", "title": "Small Work",
    })
    assert await build_pack.ensure_master(state, wi) is None
    assert state.stats.master_too_small == 1
    assert state.stats.master_cropped == 0
    await state.client.aclose()


@pytest.mark.asyncio
async def test_ensure_master_cities_floor_override(tmp_path, monkeypatch):
    """cities-architecture relaxes to 3600 (ADR-042): a 3700px native clears there but not elsewhere."""
    monkeypatch.setattr(build_pack, "_fetch_bytes", lambda *a, **k: _async(_jpeg(3700, 2400)))
    state = _build_state(tmp_path)
    wi = build_pack.WorkItem(kind="catalog", collection_id="cities-architecture", item={
        "source_url": "https://example.org/photochrom.jpg", "title": "A City",
    })
    assert await build_pack.ensure_master(state, wi) is not None   # 3700 >= 3600 floor
    assert state.stats.master_too_small == 0
    await state.client.aclose()


async def _async(value):
    return value


# --- Manifest v2 emit (ADR-044: per-collection signed feeds → verified local subscriptions) ------
import json as _json

import federation
import publisher


def _mi(title, rank, *, filename=None, focal=None):
    """A v1 manifest item as `_manifest_item` produces it (input to the v2 emit)."""
    return {"filename": filename or f"{title.lower()}.jpg", "thumbnail": f"{title.lower()}_t.jpg",
            "source_url": f"https://x/{title}.jpg", "title": title, "agent_name": "A. Painter",
            "cultural_context": "French", "description_narrative": "A placard.", "kind": "painting",
            "license": "Public Domain", "needs_frame_crop": "", "focal_point": focal or [0.5, 0.5],
            "featured_rank": rank, "credit_line": "Some Museum"}


def test_v2_row_maps_local_asset_and_omits_carried_fields():
    row = build_pack._v2_row(_mi("Sunrise", 90, focal=[0.6, 0.4]))
    assert row["local_file"] == "sunrise.jpg"
    assert row["artist"] == "A. Painter" and row["culture"] == "French" and row["placard"] == "A placard."
    assert row["attribution"] == "Some Museum" and row["license"] == "Public Domain"
    assert (row["focal_x"], row["focal_y"]) == (0.6, 0.4)
    assert "featured_rank" not in row and "needs_frame_crop" not in row   # not carried into v2


def test_emit_v2_manifests_signs_and_verifies(tmp_path):
    priv, pub = publisher.keygen()
    cols = [
        {"id": "masterpieces", "title": "Masterpieces", "description": "Best",
         "items": [_mi("Low", 10), _mi("High", 95), _mi("Mid", 50)]},
        {"id": "impressionism", "title": "Impressionism", "description": "", "items": [_mi("Monet", 80)]},
    ]
    index = build_pack._emit_v2_manifests(tmp_path, cols, signing_key=priv, generated_at="2026-07-16")

    # one signed, valid, verified-tier manifest per collection
    for c in cols:
        m = _json.loads((tmp_path / "_manifests" / f"{c['id']}.json").read_text())
        assert m["manifest_version"] == 2 and m["publisher"]["id"] == "screendocent"
        assert federation.verify_signature(m) is True
        assert federation.assess_trust(m, trusted_keys={"screendocent": pub}) == "verified"
        # local asset, no remote URL
        assert m["items"][0]["image"]["local_file"] and "full_url" not in m["items"][0]["image"]

    # items emitted rank-sorted (install uses array order) → High, Mid, Low
    mp = _json.loads((tmp_path / "_manifests" / "masterpieces.json").read_text())
    assert [it["title"] for it in mp["items"]] == ["High", "Mid", "Low"]

    # pack-index lists both, masterpieces is the default rotation
    assert (tmp_path / "pack-index.json").exists()
    assert {c["id"] for c in index["collections"]} == {"masterpieces", "impressionism"}
    assert next(c for c in index["collections"] if c["id"] == "masterpieces")["default"] is True
    assert next(c for c in index["collections"] if c["id"] == "impressionism")["default"] is False


def test_emit_v2_manifests_unsigned_is_community(tmp_path):
    """No signing key → valid but unsigned manifests (community tier), so dev builds still work."""
    cols = [{"id": "impressionism", "title": "Impressionism", "description": "", "items": [_mi("Monet", 80)]}]
    build_pack._emit_v2_manifests(tmp_path, cols, signing_key=None, generated_at=None)
    m = _json.loads((tmp_path / "_manifests" / "impressionism.json").read_text())
    assert "signature" not in m
    assert federation.assess_trust(m, trusted_keys={"screendocent": "whatever"}) == "community"


def test_build_item_accepts_local_file():
    """publisher.build_item now carries image.local_file (first-party pack asset), full_url omitted."""
    it = publisher.build_item({"title": "X", "local_file": "x.jpg", "license": "Public Domain"})
    assert it["image"]["local_file"] == "x.jpg" and "full_url" not in it["image"]
