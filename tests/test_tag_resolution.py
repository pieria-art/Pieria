"""Unit tests for tools.tag_resolution — the HD/4K/8K resolution tagger."""
import hashlib
import json

from PIL import Image

from tools import tag_resolution as tr


def test_tier_thresholds():
    assert tr.tier_for(1920) == "HD"
    assert tr.tier_for(3839) == "HD"
    assert tr.tier_for(3840) == "4K"      # exact 4K floor
    assert tr.tier_for(7679) == "4K"
    assert tr.tier_for(7680) == "8K"      # exact 8K cap
    assert tr.tier_for(12000) == "8K"


def _master(lib, source_url, w, h):
    """Write a master JPEG named with build_pack's stable `__<hash8>.jpg` suffix."""
    name = f"col__some-title__{hashlib.sha1(source_url.encode()).hexdigest()[:8]}.jpg"
    Image.new("RGB", (w, h), "red").save(lib / name, "JPEG")


def test_index_and_tag(tmp_path):
    lib = tmp_path / "_Library"
    lib.mkdir()
    _master(lib, "https://x/a.jpg", 5000, 3000)   # 4K (long edge 5000)
    _master(lib, "https://x/b.jpg", 8000, 6000)   # 8K (long edge 8000)
    _master(lib, "https://x/c.jpg", 2400, 1800)   # HD
    masters = tr.index_masters(lib)
    assert len(masters) == 3

    items = [
        {"title": "A", "source_url": "https://x/a.jpg"},
        {"title": "B", "source_url": "https://x/b.jpg"},
        {"title": "C", "source_url": "https://x/c.jpg"},
        {"title": "D", "source_url": "https://x/missing.jpg"},   # no master
    ]
    tagged, missing = tr.tag_items(items, masters, {})
    assert (tagged, missing) == (3, 1)
    assert (items[0]["resolution_tier"], items[0]["delivered_edge"]) == ("4K", 5000)
    assert items[1]["resolution_tier"] == "8K"
    assert items[2]["resolution_tier"] == "HD"
    assert "resolution_tier" not in items[3]                     # untagged, not crashed
    assert tr._collection_min_tier(items) == "HD"                # weakest member wins the roll-up


def test_process_dir_and_index(tmp_path, monkeypatch):
    lib = tmp_path / "art-pack" / "_Library"
    lib.mkdir(parents=True)
    _master(lib, "https://x/a.jpg", 8000, 6000)
    cat = tmp_path / "static" / "catalog"
    cat.mkdir(parents=True)
    (cat / "ukiyo-e.json").write_text(json.dumps(
        {"id": "ukiyo-e", "items": [{"title": "A", "source_url": "https://x/a.jpg"}]}))
    (cat / "index.json").write_text(json.dumps(
        {"version": 1, "collections": [{"id": "ukiyo-e", "count": 1}]}))

    masters = tr.index_masters(lib)
    mins = tr.process_dir(cat, masters, {}, write=True)
    assert mins == {"ukiyo-e": "8K"}
    tr.write_index_min_tiers(cat / "index.json", mins, write=True)

    doc = json.loads((cat / "ukiyo-e.json").read_text())
    assert doc["items"][0]["resolution_tier"] == "8K"
    idx = json.loads((cat / "index.json").read_text())
    assert idx["collections"][0]["min_tier"] == "8K"
