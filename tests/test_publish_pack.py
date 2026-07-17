"""ADR-040 #4 modular packs: `publish_pack` slices a built art-pack into per-collection artifacts + a
`packs.json` registry, and `install_downloaded_collection` appends one on-demand pack alongside the baked
Core WITHOUT re-seeding (multi-pack append, not replace). Mirrors test_pack_install's pattern.
"""
import json
import shutil
import tarfile
from pathlib import Path

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.lifespan as lifespan_module
import federation
import publisher
from database import Base
from models import ArtworkModel, PlaylistModel, SettingsModel, SubscriptionModel
from tools import build_pack, publish_pack


def _mi(title, rank):
    return {"filename": f"{title.lower()}.jpg", "thumbnail": f"{title.lower()}_t.jpg",
            "source_url": f"https://x/{title}.jpg", "title": title, "agent_name": "A. Painter",
            "agent_role": "Artist", "cultural_context": "French", "description_narrative": "A placard.",
            "kind": "painting", "license": "Public Domain", "date_display": "1890",
            "focal_point": [0.5, 0.5], "featured_rank": rank, "credit_line": "Some Museum",
            "source": "Some Museum"}


def _build_source_pack(tmp_path, priv):
    """A minimal but real two-collection v2 pack: masters on disk + signed _manifests/ + pack-index."""
    root = tmp_path / "art-pack"
    lib = root / "_Library"
    thumbs = root / "_catalog_thumbs"
    lib.mkdir(parents=True)
    thumbs.mkdir(parents=True)
    cols = [
        {"id": "masterpieces", "title": "Masterpieces", "description": "Best", "default": True,
         "items": [_mi("Mona-Lisa", 99), _mi("Starry", 98)]},
        {"id": "cartography", "title": "Cartography", "description": "Maps",
         "items": [_mi("Map", 60)]},
    ]
    build_pack._emit_v2_manifests(root, cols, signing_key=priv, generated_at="2026-07-17")
    # Create the master files the manifests reference (image.local_file) + their thumbnails.
    for cid in ("masterpieces", "cartography"):
        manifest = json.loads((root / "_manifests" / f"{cid}.json").read_text())
        for item in manifest["items"]:
            img = item["image"]
            Image.new("RGB", (30, 20), "red").save(lib / img["local_file"], "JPEG")
            tn = publish_pack._thumb_name(img.get("thumbnail_url"))
            if tn:
                Image.new("RGB", (12, 8), "blue").save(thumbs / tn, "JPEG")
    return root


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def _point_lifespan_at(monkeypatch, pub, root: Path):
    monkeypatch.setattr(lifespan_module, "ARTWORK_ROOT", root)
    monkeypatch.setattr(lifespan_module, "LIBRARY_DIR", root / "_Library")
    monkeypatch.setattr(lifespan_module, "PACK_INDEX", root / "pack-index.json")
    monkeypatch.setattr(federation, "TRUSTED_KEYS", {"screendocent": pub})


def _extract_into(tar_path: Path, dest_root: Path):
    """Simulate the on-device download+extract of a modular pack: merge the artifact's _Library and its
    _manifests/<id>.json into dest_root (what the runtime fetch flow will do)."""
    tmp = dest_root.parent / ("_x_" + tar_path.stem)
    if tmp.exists():
        shutil.rmtree(tmp)
    with tarfile.open(tar_path) as tf:
        tf.extractall(tmp, filter="data")
    inner = tmp / tar_path.stem  # arcname == cid == tar stem
    (dest_root / "_Library").mkdir(parents=True, exist_ok=True)
    (dest_root / "_manifests").mkdir(parents=True, exist_ok=True)
    for f in (inner / "_Library").iterdir():
        f.rename(dest_root / "_Library" / f.name)
    for f in (inner / "_manifests").iterdir():
        f.rename(dest_root / "_manifests" / f.name)


def test_publish_slices_registry_and_valid_artifacts(tmp_path):
    priv, _pub = publisher.keygen()
    src = _build_source_pack(tmp_path, priv)
    out = tmp_path / "dist"
    reg = publish_pack.publish(src, out, core={"masterpieces"})

    assert reg["core"] == ["masterpieces"]
    by_id = {c["id"]: c for c in reg["collections"]}
    assert set(by_id) == {"masterpieces", "cartography"}
    assert by_id["masterpieces"]["core"] is True and by_id["cartography"]["core"] is False
    assert by_id["masterpieces"]["item_count"] == 2 and by_id["cartography"]["item_count"] == 1
    assert by_id["cartography"]["category"] == "map"          # from COLLECTION_KIND
    assert by_id["masterpieces"]["category"] == "featured"    # overlay fallback
    for c in reg["collections"]:
        assert len(c["sha256"]) == 64 and c["bytes"] > 0
        assert (out / c["download"]).exists()

    # each tar is a self-contained, valid single-collection pack
    with tarfile.open(out / "cartography.tar") as tf:
        names = tf.getnames()
    assert "cartography/_manifests/cartography.json" in names
    assert "cartography/pack-index.json" in names
    assert any(n.startswith("cartography/_Library/") for n in names)


def test_downloaded_collection_appends_without_reseeding(tmp_path, monkeypatch):
    priv, pub = publisher.keygen()
    src = _build_source_pack(tmp_path, priv)
    dist = tmp_path / "dist"
    publish_pack.publish(src, dist, core={"masterpieces"})

    # Fresh device: bake ONLY the Core (masterpieces) — extract it as the on-disk pack.
    device = tmp_path / "device"
    _extract_into(dist / "masterpieces.tar", device)
    # a Core-only pack-index (what the .img would carry)
    (device / "pack-index.json").write_text(json.dumps({
        "pack_version": "2", "publisher": {"id": "screendocent"},
        "collections": [{"id": "masterpieces", "title": "Masterpieces",
                         "manifest": "_manifests/masterpieces.json", "item_count": 2, "default": True}],
    }))
    _point_lifespan_at(monkeypatch, pub, device)

    db = _db()
    assert lifespan_module.install_pack_subscriptions(db) is True
    assert db.query(SubscriptionModel).count() == 1
    assert db.query(ArtworkModel).count() == 2
    default = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    assert default.setting_value == "Masterpieces"

    # Now "download" cartography: extract its files alongside, then APPEND-install it.
    _extract_into(dist / "cartography.tar", device)
    assert lifespan_module.install_downloaded_collection(db, "cartography") is True

    # appended, not replaced: both collections present, Core untouched, default unchanged
    assert db.query(SubscriptionModel).count() == 2
    assert {s.url for s in db.query(SubscriptionModel).all()} == {"pack:masterpieces", "pack:cartography"}
    assert db.query(PlaylistModel).filter(PlaylistModel.name == "Cartography").count() == 1
    assert db.query(ArtworkModel).count() == 3
    default = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    assert default.setting_value == "Masterpieces"   # a downloaded pack must NOT hijack the default

    # idempotent: re-appending the same collection changes nothing
    assert lifespan_module.install_downloaded_collection(db, "cartography") is True
    assert db.query(SubscriptionModel).count() == 2
    assert db.query(ArtworkModel).count() == 3


def test_downloaded_collection_missing_manifest_returns_false(tmp_path, monkeypatch):
    priv, pub = publisher.keygen()
    device = tmp_path / "device"
    (device / "_manifests").mkdir(parents=True)
    _point_lifespan_at(monkeypatch, pub, device)
    db = _db()
    assert lifespan_module.install_downloaded_collection(db, "nope") is False
