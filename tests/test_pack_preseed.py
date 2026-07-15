"""Boot-time pack pre-seed (ADR-038): tools/build_pack.py's pack-manifest.json consumed with zero
network calls — masters already sit in LIBRARY_DIR, so boot only has to mint playlists + artworks
from local data. Mirrors tests/test_playlist_resume.py's in-memory-SQLite + tmp_path pattern.
"""

import json

from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.lifespan as lifespan_module
from database import Base
from models import ArtworkModel, PlaylistModel, SettingsModel, playlist_artwork


def _make_pack(tmp_path, monkeypatch):
    """Two collections sharing one work (same source_url/filename in both) — the dedup case."""
    artwork_root = tmp_path / "Artwork"
    library_dir = artwork_root / "_Library"
    library_dir.mkdir(parents=True)

    for name in ("a.jpg", "shared.jpg", "c.jpg"):
        Image.new("RGB", (20, 20), color="red").save(library_dir / name, "JPEG")

    def _item(filename, rank, source_url, focal=None):
        return {
            "filename": filename, "thumbnail": "", "source_url": source_url,
            "title": f"Title for {filename}", "agent_name": "Some Artist", "agent_role": "Artist",
            "creation_date": "1890", "cultural_context": "Test Context", "medium": "Oil on canvas",
            "date_display": "1890", "description_narrative": "A narrative.",
            "tags": "test,pack", "focal_point": focal or [0.5, 0.5],
            "featured_rank": rank, "credit_line": "", "source": "test", "license": "public-domain",
        }

    item_a = _item("a.jpg", 100, "https://example.org/a")
    item_shared = _item("shared.jpg", 80, "https://example.org/shared", focal=[0.3, 0.7])
    item_c = _item("c.jpg", 30, "https://example.org/c")

    manifest = {
        "version": "v1",
        "display_max_edge": 7680,
        "collections": [
            {
                "id": "greatest_hits", "title": "Greatest Hits", "description": "",
                "items": [item_a, item_shared],
            },
            {
                "id": "impressionism", "title": "Impressionism", "description": "",
                "items": [item_shared, item_c],
            },
        ],
    }
    manifest_path = artwork_root / "pack-manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    monkeypatch.setattr(lifespan_module, "ARTWORK_ROOT", artwork_root)
    monkeypatch.setattr(lifespan_module, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(lifespan_module, "PACK_MANIFEST", manifest_path)
    return artwork_root, library_dir, manifest_path


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def test_no_manifest_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(lifespan_module, "ARTWORK_ROOT", tmp_path)
    monkeypatch.setattr(lifespan_module, "LIBRARY_DIR", tmp_path / "_Library")
    monkeypatch.setattr(lifespan_module, "PACK_MANIFEST", tmp_path / "pack-manifest.json")
    db = _db()
    try:
        assert lifespan_module.pre_seed_from_pack(db) is False
    finally:
        db.close()


def test_pre_seed_mints_playlists_and_dedupes(tmp_path, monkeypatch):
    _make_pack(tmp_path, monkeypatch)
    db = _db()
    try:
        assert lifespan_module.pre_seed_from_pack(db) is True

        playlists = {p.name: p for p in db.query(PlaylistModel).all()}
        assert set(playlists) == {"Greatest Hits", "Impressionism"}

        artworks = {a.filename: a for a in db.query(ArtworkModel).all()}
        # Exactly 3 distinct artworks — the shared work is NOT duplicated across collections.
        assert set(artworks) == {"a.jpg", "shared.jpg", "c.jpg"}

        for art in artworks.values():
            assert art.is_seed is True
            assert art.status == "approved"
            assert art.title == f"Title for {art.filename}"
            assert art.agent_name == "Some Artist"

        # affinity mapping: rank 50 -> 1.0 neutral; 100 -> 1.5; 0 -> 0.5.
        assert artworks["a.jpg"].affinity_score == 1.5          # rank 100
        assert artworks["shared.jpg"].affinity_score == 1.3     # rank 80
        assert artworks["c.jpg"].affinity_score == 0.8          # rank 30

        # Focal point parsed from the manifest (non-default for shared.jpg).
        assert artworks["shared.jpg"].focal_x == 0.3
        assert artworks["shared.jpg"].focal_y == 0.7
        assert artworks["a.jpg"].focal_x == 0.5 and artworks["a.jpg"].focal_y == 0.5

        # display_order follows featured_rank descending within EACH collection.
        gh_links = db.execute(
            select(playlist_artwork.c.artwork_id, playlist_artwork.c.display_order)
            .where(playlist_artwork.c.playlist_id == playlists["Greatest Hits"].id)
            .order_by(playlist_artwork.c.display_order)
        ).all()
        assert [artworks_by_id_name(db, aid) for aid, _ in gh_links] == ["a.jpg", "shared.jpg"]
        assert [order for _, order in gh_links] == [0, 1]

        imp_links = db.execute(
            select(playlist_artwork.c.artwork_id, playlist_artwork.c.display_order)
            .where(playlist_artwork.c.playlist_id == playlists["Impressionism"].id)
            .order_by(playlist_artwork.c.display_order)
        ).all()
        assert [artworks_by_id_name(db, aid) for aid, _ in imp_links] == ["shared.jpg", "c.jpg"]
        assert [order for _, order in imp_links] == [0, 1]

        # The shared work is ONE ArtworkModel with TWO playlist links (deduped by source_url).
        shared_id = artworks["shared.jpg"].id
        shared_links = db.execute(
            select(playlist_artwork).where(playlist_artwork.c.artwork_id == shared_id)
        ).all()
        assert len(shared_links) == 2
        assert {link.playlist_id for link in shared_links} == {
            playlists["Greatest Hits"].id, playlists["Impressionism"].id,
        }

        # default-playlist setting honors "Greatest Hits" when present.
        default_row = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
        assert default_row.setting_value == "Greatest Hits"

        pack_seeded_row = db.query(SettingsModel).filter(SettingsModel.setting_key == "pack_seeded").first()
        assert pack_seeded_row.setting_value == "v1"
    finally:
        db.close()


def test_second_call_is_a_noop(tmp_path, monkeypatch):
    _make_pack(tmp_path, monkeypatch)
    db = _db()
    try:
        assert lifespan_module.pre_seed_from_pack(db) is True
        artwork_count_1 = db.query(ArtworkModel).count()
        playlist_count_1 = db.query(PlaylistModel).count()

        assert lifespan_module.pre_seed_from_pack(db) is True   # guarded by pack_seeded — no-op

        assert db.query(ArtworkModel).count() == artwork_count_1
        assert db.query(PlaylistModel).count() == playlist_count_1
    finally:
        db.close()


def test_missing_master_is_skipped_not_fatal(tmp_path, monkeypatch):
    artwork_root, library_dir, manifest_path = _make_pack(tmp_path, monkeypatch)
    manifest = json.loads(manifest_path.read_text())
    manifest["collections"][0]["items"].append({
        "filename": "does-not-exist.jpg", "thumbnail": "", "source_url": "https://example.org/missing",
        "title": "Missing", "agent_name": "", "agent_role": "", "creation_date": "", "cultural_context": "",
        "medium": "", "date_display": "", "description_narrative": "", "tags": "",
        "focal_point": [0.5, 0.5], "featured_rank": 90, "credit_line": "", "source": "", "license": "",
    })
    manifest_path.write_text(json.dumps(manifest))

    db = _db()
    try:
        assert lifespan_module.pre_seed_from_pack(db) is True
        assert db.query(ArtworkModel).filter(ArtworkModel.filename == "does-not-exist.jpg").first() is None
    finally:
        db.close()


def artworks_by_id_name(db, artwork_id):
    return db.query(ArtworkModel).filter(ArtworkModel.id == artwork_id).first().filename
