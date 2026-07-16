"""ADR-044 unified ingestion: install_pack_subscriptions consumes build_pack's signed Manifest v2 feeds
(pack-index.json + _manifests/<id>.json) and installs the Core pack as VERIFIED LOCAL SUBSCRIPTIONS —
the same path a third-party publisher uses — minting playlists + artworks from local masters, no network.
Mirrors test_pack_preseed.py's in-memory-SQLite + tmp_path pattern.
"""

from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.lifespan as lifespan_module
import federation
import publisher
from database import Base
from models import ArtworkModel, PlaylistModel, SettingsModel, SubscriptionModel, playlist_artwork
from tools import build_pack


def _mi(title, rank, focal=None):
    return {"filename": f"{title.lower()}.jpg", "thumbnail": f"{title.lower()}_t.jpg",
            "source_url": f"https://x/{title}.jpg", "title": title, "agent_name": "A. Painter",
            "agent_role": "Artist", "cultural_context": "French", "description_narrative": "A placard.",
            "kind": "painting", "license": "Public Domain", "date_display": "1890",
            "focal_point": focal or [0.5, 0.5], "featured_rank": rank, "credit_line": "Some Museum",
            "source": "Some Museum"}


def _make_v2_pack(tmp_path, priv):
    """Build a real signed v2 pack (masters on disk + _manifests/ + pack-index.json)."""
    artwork_root = tmp_path / "Artwork"
    library_dir = artwork_root / "_Library"
    library_dir.mkdir(parents=True)
    for name in ("high.jpg", "mid.jpg", "low.jpg", "monet.jpg"):
        Image.new("RGB", (20, 20), color="red").save(library_dir / name, "JPEG")
    cols = [
        {"id": "masterpieces", "title": "Masterpieces", "description": "Best",
         "items": [_mi("Low", 10), _mi("High", 95, focal=[0.6, 0.4]), _mi("Mid", 50)]},
        {"id": "impressionism", "title": "Impressionism", "description": "", "items": [_mi("Monet", 80)]},
    ]
    build_pack._emit_v2_manifests(artwork_root, cols, signing_key=priv, generated_at="2026-07-16")
    return artwork_root, library_dir


def _install(tmp_path, monkeypatch, *, registered=True):
    priv, pub = publisher.keygen()
    artwork_root, library_dir = _make_v2_pack(tmp_path, priv)
    monkeypatch.setattr(lifespan_module, "ARTWORK_ROOT", artwork_root)
    monkeypatch.setattr(lifespan_module, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(lifespan_module, "PACK_INDEX", artwork_root / "pack-index.json")
    monkeypatch.setattr(federation, "TRUSTED_KEYS", {"screendocent": pub} if registered else {})
    return artwork_root


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def test_no_pack_index_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(lifespan_module, "PACK_INDEX", tmp_path / "pack-index.json")
    db = _db()
    try:
        assert lifespan_module.install_pack_subscriptions(db) is False
    finally:
        db.close()


def test_install_creates_verified_subscriptions_and_playlists(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, registered=True)
    db = _db()
    try:
        assert lifespan_module.install_pack_subscriptions(db) is True

        # one verified local subscription per collection
        subs = {s.collection_id: s for s in db.query(SubscriptionModel).all()}
        assert set(subs) == {"masterpieces", "impressionism"}
        for s in subs.values():
            assert s.trust == "verified"
            assert s.url.startswith("pack:")
            assert s.publisher_id == "screendocent"
            assert s.enabled is True and s.last_status == "ok"

        # playlists minted from local masters
        playlists = {p.name: p for p in db.query(PlaylistModel).all()}
        assert set(playlists) == {"Masterpieces", "Impressionism"}

        artworks = {a.filename: a for a in db.query(ArtworkModel).all()}
        assert set(artworks) == {"high.jpg", "mid.jpg", "low.jpg", "monet.jpg"}
        for a in artworks.values():
            assert a.is_seed is True and a.status == "approved"
            assert a.source_url.startswith("pack:")        # local sentinel, never fetchable
        # focal survived the round-trip
        assert (artworks["high.jpg"].focal_x, artworks["high.jpg"].focal_y) == (0.6, 0.4)

        # display_order follows the manifest's array (fame) order: High, Mid, Low
        mp_id = playlists["Masterpieces"].id
        links = db.execute(
            select(playlist_artwork.c.artwork_id, playlist_artwork.c.display_order)
            .where(playlist_artwork.c.playlist_id == mp_id)
            .order_by(playlist_artwork.c.display_order)).all()
        names = [db.get(ArtworkModel, aid).filename for aid, _ in links]
        assert names == ["high.jpg", "mid.jpg", "low.jpg"]

        # Masterpieces is the default rotation; seeded marker records the v2 path
        assert db.query(SettingsModel).filter_by(setting_key="default_playlist").first().setting_value == "Masterpieces"
        assert db.query(SettingsModel).filter_by(setting_key="pack_seeded").first().setting_value.startswith("v2:")
    finally:
        db.close()


def test_install_unregistered_key_is_community(tmp_path, monkeypatch):
    """Same pack, but the publisher key is NOT in the registry → the subscription stays 'community'."""
    _install(tmp_path, monkeypatch, registered=False)
    db = _db()
    try:
        assert lifespan_module.install_pack_subscriptions(db) is True
        for s in db.query(SubscriptionModel).all():
            assert s.trust == "community"
    finally:
        db.close()


def test_install_is_idempotent(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, registered=True)
    db = _db()
    try:
        assert lifespan_module.install_pack_subscriptions(db) is True
        n_art, n_sub, n_pl = (db.query(ArtworkModel).count(), db.query(SubscriptionModel).count(),
                              db.query(PlaylistModel).count())
        assert lifespan_module.install_pack_subscriptions(db) is True   # pack_seeded guard → no-op
        assert (db.query(ArtworkModel).count(), db.query(SubscriptionModel).count(),
                db.query(PlaylistModel).count()) == (n_art, n_sub, n_pl)
    finally:
        db.close()
