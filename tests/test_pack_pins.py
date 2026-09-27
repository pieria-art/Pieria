"""Pack pins (2026-09-22, option A): the served catalog was re-sourced off artic.edu to Wikimedia
Commons, but the signed art packs must keep shipping their July AIC masters for those works. Pure
unit tests for apply_pack_pins/load_pack_pins (tmp dirs / fake data only, no network, no writes under
art-pack/) plus an end-to-end build() run against a tiny fake catalog + fake pre-populated _Library so
zero downloads happen. See tools/build_pack.py generate_pack_pins/apply_pack_pins."""

import asyncio
import json

import pytest

from tools import build_pack

# --------------------------------------------------------------------------- apply_pack_pins (pure)

def test_apply_pack_pins_replaces_served_row_by_identity():
    served = [
        {"title": "Apollo and Marsyas", "agent_name": "Hans Thoma", "source_url": "https://commons.example/x.jpg"},
        {"title": "Untouched Work", "agent_name": "Someone Else", "source_url": "https://commons.example/y.jpg"},
    ]
    pin = {"title": "Apollo and Marsyas", "agent_name": "Hans Thoma", "source_url": "https://www.artic.edu/iiif/2/abc/full/max/0/default.jpg"}
    merged, replaced, parked = build_pack.apply_pack_pins(served, [pin])
    assert replaced == 1
    assert parked == 0
    assert len(merged) == 2
    assert merged[0]["source_url"].startswith("https://www.artic.edu")  # pinned row replaced in place
    assert merged[1] == served[1]  # non-pinned row untouched


def test_apply_pack_pins_skips_parked_row_not_in_served():
    """ADR-140/141 (2026-09-27, Josh): a pin with no served match is parked — NOT shipped in packs, since
    it predates ADR-140's re-grounding and would be the only unverified placard. It stays out of the
    merged list entirely (still recoverable from _pack_pins.json itself, just not appended here)."""
    served = [{"title": "Still Here", "agent_name": "A", "source_url": "https://commons.example/a.jpg"}]
    parked_pin = {"title": "The Gulf Stream", "agent_name": "Winslow Homer",
                  "source_url": "https://www.artic.edu/iiif/2/gulf/full/max/0/default.jpg"}
    merged, replaced, parked = build_pack.apply_pack_pins(served, [parked_pin])
    assert replaced == 0
    assert parked == 1
    assert len(merged) == 1
    assert merged == served  # unaffected — parked pin not appended, not shipped


def test_apply_pack_pins_mix_of_replace_and_skip_preserves_others():
    served = [
        {"title": "A", "agent_name": "X", "source_url": "u1"},
        {"title": "B", "agent_name": "Y", "source_url": "u2"},
        {"title": "C", "agent_name": "Z", "source_url": "u3"},
    ]
    pins = [
        {"title": "B", "agent_name": "Y", "source_url": "pinned-u2"},   # replaces
        {"title": "Parked", "agent_name": "W", "source_url": "u4"},      # skipped, not shipped
    ]
    merged, replaced, parked = build_pack.apply_pack_pins(served, pins)
    assert replaced == 1 and parked == 1
    assert [it["title"] for it in merged] == ["A", "B", "C"]
    assert merged[1]["source_url"] == "pinned-u2"
    assert merged[0] == served[0] and merged[2] == served[2]


def test_pin_identity_prefers_aic_accession_when_both_sides_have_it():
    served = {"title": "Different Title Now", "agent_name": "Someone", "_aic_accession": "1933.1241"}
    pin = {"title": "The Gulf Stream", "agent_name": "Winslow Homer", "_aic_accession": "1933.1241"}
    assert build_pack._pin_identity_match(pin, served) is True


def test_pin_identity_falls_back_to_title_and_agent_when_no_accession():
    served = {"title": "The Gulf Stream", "agent_name": "Winslow Homer"}
    pin = {"title": "The Gulf Stream", "agent_name": "Winslow Homer", "_aic_accession": "1933.1241"}
    assert build_pack._pin_identity_match(pin, served) is True
    assert build_pack._pin_identity_match({"title": "Other", "agent_name": "Winslow Homer"}, served) is False


# --------------------------------------------------------------------------- ADR-140 qualified-name matching

@pytest.mark.parametrize("pin_agent, served_agent", [
    ("El Greco", "Workshop of El Greco"),
    ("Alessandro Allori", "Attributed to Alessandro Allori"),
    ("Adriaen van der Spelt", "Adriaen van der Spelt and Frans van Mieris the Elder"),
])
def test_pin_identity_matches_served_qualified_or_collaboration_name(pin_agent, served_agent):
    pin = {"title": "Some Work", "agent_name": pin_agent}
    served = {"title": "Some Work", "agent_name": served_agent}
    assert build_pack._pin_identity_match(pin, served) is True


def test_pin_identity_does_not_match_different_artist_same_title():
    pin = {"title": "Some Work", "agent_name": "Titian"}
    served = {"title": "Some Work", "agent_name": "Tintoretto"}
    assert build_pack._pin_identity_match(pin, served) is False


# --------------------------------------------------------------------------- apply_pack_pins: image-only merge

def test_apply_pack_pins_merges_image_fields_only_keeps_served_content():
    served = [{
        "title": "Re-grounded Work", "agent_name": "Workshop of El Greco", "agent_role": "painter",
        "creation_date": "1600", "medium": "oil on canvas", "current_repository": "Museum X",
        "description_narrative": "new placard text", "tags": ["new-tag"], "featured_rank": 3,
        "source_url": "https://commons.example/new.jpg", "thumbnail_url": "https://commons.example/new_thumb.jpg",
        "source": "wikimedia", "credit_line": "Commons credit", "license": "CC0", "license_basis": "pd-old",
        "license_url": "https://commons.example/license", "license_verdict": "ok", "license_verified": True,
        "focal_point": [0.1, 0.1], "aspect_crops": {"square": "x"}, "delivered_edge": 2000,
        "resolution_tier": "hd", "crop_box": None, "needs_frame_crop": False,
    }]
    pin = {
        "title": "Re-grounded Work", "agent_name": "El Greco",
        "source_url": "https://www.artic.edu/iiif/2/x/full/max/0/default.jpg",
        "thumbnail_url": "https://www.artic.edu/thumb.jpg", "source": "artic.edu",
        "credit_line": "AIC credit", "license": "CC0", "license_basis": "pd-aic",
        "license_url": "https://www.artic.edu/license", "license_verdict": "ok", "license_verified": True,
        "focal_point": [0.5, 0.5], "aspect_crops": {"square": "y"}, "delivered_edge": 8000,
        "resolution_tier": "4k", "crop_box": [0, 0, 1, 1], "needs_frame_crop": True,
        "source_rotate": 90,
    }
    merged, replaced, parked = build_pack.apply_pack_pins(served, [pin])
    assert replaced == 1 and parked == 0
    row = merged[0]
    # content fields untouched — the served (re-grounded) values survive
    for field in ("agent_name", "agent_role", "creation_date", "medium", "current_repository",
                  "description_narrative", "tags", "featured_rank"):
        assert row[field] == served[0][field]
    # image fields come from the pin
    for field in build_pack._PIN_IMAGE_FIELDS:
        assert row[field] == pin[field]
    assert row["source_rotate"] == 90  # present-in-pin optional field also carried


def test_apply_pack_pins_missing_optional_image_field_keeps_served_value():
    served = [{"title": "T", "agent_name": "A", "source_url": "old.jpg", "needs_frame_crop": True}]
    pin = {"title": "T", "agent_name": "A", "source_url": "new.jpg"}  # no needs_frame_crop key
    merged, replaced, _ = build_pack.apply_pack_pins(served, [pin])
    assert replaced == 1
    assert merged[0]["source_url"] == "new.jpg"
    assert merged[0]["needs_frame_crop"] is True  # not overridden — pin didn't carry the field


# --------------------------------------------------------------------------- load_pack_pins

def test_load_pack_pins_missing_file_returns_empty(tmp_path):
    assert build_pack.load_pack_pins(tmp_path / "does_not_exist.json") == {}


def test_load_pack_pins_reads_collections(tmp_path):
    p = tmp_path / "_pack_pins.json"
    p.write_text(json.dumps({"_note": "x", "collections": {"c1": [{"title": "T"}]}}))
    pins = build_pack.load_pack_pins(p)
    assert pins == {"c1": [{"title": "T"}]}


# --------------------------------------------------------------------------- build() end-to-end, no pins vs pins

def _write_catalog(catalog_dir, cid, items, title="Col"):
    catalog_dir.mkdir(parents=True, exist_ok=True)
    (catalog_dir / f"{cid}.json").write_text(json.dumps({
        "id": cid, "title": title, "description": "", "items": items,
    }))


def _run_build(out, **kw):
    return asyncio.run(build_pack.build(out, scope={"catalog"}, limit=None, collections_filter=None,
                                         concurrency=2, created=None, min_edge=1, **kw))


@pytest.mark.asyncio
async def test_build_absent_pins_file_behaves_unchanged(tmp_path, monkeypatch):
    """No static/catalog/_pack_pins.json on disk -> build() output is identical to before pins existed."""
    catalog_dir = tmp_path / "catalog"
    _write_catalog(catalog_dir, "demo", [
        {"title": "Only Item", "agent_name": "A", "source_url": "https://example.org/only.jpg", "focal_point": [0.5, 0.5]},
    ])
    monkeypatch.setattr(build_pack, "CATALOG_DIR", catalog_dir)
    monkeypatch.setattr(build_pack, "PINS_FILE", tmp_path / "no_pins_here.json")
    monkeypatch.setattr(build_pack, "SEED_FILE", tmp_path / "no_seed.json")

    out = tmp_path / "pack"
    (out / "_Library").mkdir(parents=True)
    # pre-seed the expected master so no network fetch is attempted (dest.exists() short-circuit)
    fn = build_pack.master_filename("demo", "Only Item", "https://example.org/only.jpg")
    (out / "_Library" / fn).write_bytes(b"fake")

    rc = await build_pack.build(out, scope={"catalog"}, limit=None, collections_filter=None,
                                 concurrency=2, created=None, min_edge=1, signing_key=None)
    assert rc == 0
    manifest = json.loads((out / "pack-manifest.json").read_text())
    titles = [it["title"] for it in manifest["collections"][0]["items"]]
    assert titles == ["Only Item"]


@pytest.mark.asyncio
async def test_build_applies_pins_replace_and_skips_parked(tmp_path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    _write_catalog(catalog_dir, "demo", [
        {"title": "Replace Me", "agent_name": "A", "source_url": "https://commons.example/replace.jpg", "focal_point": [0.5, 0.5]},
        {"title": "Untouched", "agent_name": "B", "source_url": "https://example.org/untouched.jpg", "focal_point": [0.5, 0.5]},
    ])
    monkeypatch.setattr(build_pack, "CATALOG_DIR", catalog_dir)
    monkeypatch.setattr(build_pack, "SEED_FILE", tmp_path / "no_seed.json")

    pin_replace = {"title": "Replace Me", "agent_name": "A",
                   "source_url": "https://www.artic.edu/iiif/2/replace/full/max/0/default.jpg",
                   "focal_point": [0.5, 0.5]}
    pin_parked = {"title": "Parked Work", "agent_name": "C",
                  "source_url": "https://www.artic.edu/iiif/2/parked/full/max/0/default.jpg",
                  "focal_point": [0.5, 0.5]}
    pins_file = tmp_path / "_pack_pins.json"
    pins_file.write_text(json.dumps({"_note": "test", "collections": {"demo": [pin_replace, pin_parked]}}))
    monkeypatch.setattr(build_pack, "PINS_FILE", pins_file)

    out = tmp_path / "pack"
    (out / "_Library").mkdir(parents=True)
    for title, su in [
        ("Replace Me", pin_replace["source_url"]),
        ("Untouched", "https://example.org/untouched.jpg"),
        ("Parked Work", pin_parked["source_url"]),
    ]:
        fn = build_pack.master_filename("demo", title, su)
        (out / "_Library" / fn).write_bytes(b"fake")

    rc = await build_pack.build(out, scope={"catalog"}, limit=None, collections_filter=None,
                                 concurrency=2, created=None, min_edge=1, signing_key=None)
    assert rc == 0

    manifest = json.loads((out / "pack-manifest.json").read_text())
    items = manifest["collections"][0]["items"]
    by_title = {it["title"]: it for it in items}
    # ADR-140/141 (2026-09-27): "Parked Work" has no served match, so it is NOT shipped in the pack.
    assert set(by_title) == {"Replace Me", "Untouched"}
    assert by_title["Replace Me"]["source_url"] == pin_replace["source_url"]  # replaced
    assert by_title["Untouched"]["source_url"] == "https://example.org/untouched.jpg"  # unaffected

    # the _catalog/<id>.json copy build_pack writes must reflect what actually went into the pack
    catalog_copy = json.loads((out / "_catalog" / "demo.json").read_text())
    copy_titles = {it["title"] for it in catalog_copy["items"]}
    assert copy_titles == {"Replace Me", "Untouched"}


# --------------------------------------------------------------------------- compute_expected_masters / coverage

def test_compute_expected_masters_dedups_shared_source_url_across_collections(tmp_path, monkeypatch):
    """Two collections listing the SAME work (same source_url) must resolve to the SAME expected
    filename — named by whichever collection is processed first (glob-sorted), exactly mirroring
    ensure_master's real dedup. This caught a false-positive 'miss' during manual verification."""
    catalog_dir = tmp_path / "catalog"
    shared_url = "https://commons.example/shared.jpg"
    _write_catalog(catalog_dir, "aaa-collection", [{"title": "Shared Work", "agent_name": "X", "source_url": shared_url}])
    _write_catalog(catalog_dir, "zzz-collection", [{"title": "Shared Work", "agent_name": "X", "source_url": shared_url}])
    monkeypatch.setattr(build_pack, "CATALOG_DIR", catalog_dir)
    monkeypatch.setattr(build_pack, "PINS_FILE", tmp_path / "no_pins.json")

    expected = build_pack.compute_expected_masters()
    aaa_fn = dict(expected["aaa-collection"])["Shared Work"]
    zzz_fn = dict(expected["zzz-collection"])["Shared Work"]
    assert aaa_fn == zzz_fn == build_pack.master_filename("aaa-collection", "Shared Work", shared_url)


def test_compute_expected_masters_indexed_aligns_with_served_order(tmp_path, monkeypatch):
    """reground_placards.py resolves a served-catalog item to its pack master by INDEX into this list —
    an item without source_url must leave a None placeholder so later indices stay aligned, and pins
    must replace in-slot rather than shifting anything."""
    catalog_dir = tmp_path / "catalog"
    _write_catalog(catalog_dir, "demo", [
        {"title": "No Source", "agent_name": "A"},
        {"title": "Has Source", "agent_name": "B", "source_url": "https://commons.example/b.jpg"},
    ])
    monkeypatch.setattr(build_pack, "CATALOG_DIR", catalog_dir)
    monkeypatch.setattr(build_pack, "PINS_FILE", tmp_path / "no_pins.json")

    indexed = build_pack.compute_expected_masters_indexed()
    assert indexed["demo"][0] is None
    assert indexed["demo"][1] == build_pack.master_filename("demo", "Has Source", "https://commons.example/b.jpg")


def test_check_pack_pins_coverage_reports_hits_and_misses(tmp_path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    _write_catalog(catalog_dir, "demo", [
        {"title": "Present", "agent_name": "A", "source_url": "https://example.org/present.jpg"},
        {"title": "Missing", "agent_name": "B", "source_url": "https://example.org/missing.jpg"},
    ])
    monkeypatch.setattr(build_pack, "CATALOG_DIR", catalog_dir)
    monkeypatch.setattr(build_pack, "PINS_FILE", tmp_path / "no_pins.json")

    library_dir = tmp_path / "_Library"
    library_dir.mkdir()
    present_fn = build_pack.master_filename("demo", "Present", "https://example.org/present.jpg")
    (library_dir / present_fn).write_bytes(b"x")

    report = build_pack.check_pack_pins_coverage(library_dir)
    assert report["expected"] == 2
    assert report["hits"] == 1
    assert len(report["misses"]) == 1
    assert report["misses"][0]["title"] == "Missing"
