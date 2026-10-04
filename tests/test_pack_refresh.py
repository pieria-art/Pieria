"""ADR-135 / ADR-148 F8: re-installing a pack (or a boot with a changed on-disk manifest) refreshes the
pack-sourced metadata of EXISTING works, while per-field user edits, personal photos, status and
affinity are never touched."""
import json

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.lifespan as lifespan
import federation
import publisher
from core.artwork_edits import edited_fields, mark_edited
from database import Base
from models import ArtworkModel, SubscriptionModel
from tools import build_pack

CC_BY_CREDIT = "ESA/Webb, NASA & CSA, A. Martel (CC BY 4.0) — exact as given"


def _mi(title, **kw):
    row = {"filename": f"{title.lower()}.jpg", "thumbnail": f"{title.lower()}_t.jpg",
           "source_url": f"https://x/{title}.jpg", "title": title, "agent_name": "A. Painter",
           "agent_role": "Artist", "cultural_context": "French", "description_narrative": "A placard.",
           "kind": "painting", "license": "Public Domain", "date_display": "1890", "series": "",
           "resolution_tier": "", "focal_point": [0.5, 0.5], "featured_rank": 50,
           "credit_line": "Old Museum", "source": "Old Museum"}
    row.update(kw)
    return row


def _setup(tmp_path, monkeypatch, items):
    priv, pub = publisher.keygen()
    root = tmp_path / "Artwork"
    lib = root / "_Library"
    lib.mkdir(parents=True)
    for n in ("alpha.jpg", "beta.jpg", "gamma.jpg"):
        Image.new("RGB", (20, 20), color="red").save(lib / n, "JPEG")
    monkeypatch.setattr(lifespan, "ARTWORK_ROOT", root)
    monkeypatch.setattr(lifespan, "LIBRARY_DIR", lib)
    monkeypatch.setattr(federation, "TRUSTED_KEYS", {"pieria": pub})
    return root, priv


def _publish(root, priv, items):
    build_pack._emit_v2_manifests(
        root, [{"id": "masterpieces", "title": "Masterpieces", "description": "", "items": items}],
        signing_key=priv, generated_at="2026-10-04")
    return json.loads((root / "_manifests" / "masterpieces.json").read_text())


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def _art(db, title_or_file):
    return db.query(ArtworkModel).filter(ArtworkModel.filename == f"{title_or_file}.jpg").one()


def _v1_items():
    return [_mi("Alpha"), _mi("Beta"), _mi("Gamma")]


def _install(db, root):
    m = json.loads((root / "_manifests" / "masterpieces.json").read_text())
    return lifespan._install_collection(db, "masterpieces", m)


def test_reinstall_corrects_title_and_credit_on_existing_row(tmp_path, monkeypatch):
    root, priv = _setup(tmp_path, monkeypatch, None)
    _publish(root, priv, _v1_items())
    db = _db()
    _install(db, root)
    assert _art(db, "alpha").attribution == "Old Museum"
    _publish(root, priv, [_mi("Alpha Fixed", filename="alpha.jpg", source_url="https://x/Alpha.jpg",
                              credit_line="Corrected Museum"), _mi("Beta"), _mi("Gamma")])
    _install(db, root)
    a = _art(db, "alpha")
    assert (a.title, a.attribution) == ("Alpha Fixed", "Corrected Museum")
    assert db.query(ArtworkModel).count() == 3   # refreshed in place, no duplicates


def test_cc_by_credit_lands_exactly_as_in_manifest(tmp_path, monkeypatch):
    root, priv = _setup(tmp_path, monkeypatch, None)
    _publish(root, priv, _v1_items())
    db = _db()
    _install(db, root)
    _publish(root, priv, [_mi("Alpha", license="CC-BY-4.0",
                              license_url="https://creativecommons.org/licenses/by/4.0/",
                              credit_line=CC_BY_CREDIT, attribution_url="https://x/credit"),
                          _mi("Beta"), _mi("Gamma")])
    _install(db, root)
    a = _art(db, "alpha")
    assert a.attribution == CC_BY_CREDIT   # byte-exact, never rewritten (ADR-142)
    assert a.license == "CC-BY-4.0"
    assert a.license_url == "https://creativecommons.org/licenses/by/4.0/"
    assert a.attribution_url == "https://x/credit"


def test_user_edited_fields_are_preserved(tmp_path, monkeypatch):
    root, priv = _setup(tmp_path, monkeypatch, None)
    _publish(root, priv, _v1_items())
    db = _db()
    _install(db, root)
    a = _art(db, "alpha")
    a.title = "My Title"
    a.focal_x, a.focal_y = 0.1, 0.9
    a.crop_x = 5.0
    mark_edited(a, ["title", "focal_x", "focal_y"])
    a.status, a.affinity_score = "rejected", 3.3
    db.commit()
    _publish(root, priv, [_mi("Alpha Pack", filename="alpha.jpg", source_url="https://x/Alpha.jpg", focal_point=[0.7, 0.2], credit_line="New Credit"),
                          _mi("Beta"), _mi("Gamma")])
    _install(db, root)
    a = _art(db, "alpha")
    assert a.title == "My Title"
    assert (a.focal_x, a.focal_y) == (0.1, 0.9)
    assert a.crop_x == 5.0
    assert a.attribution == "New Credit"            # untouched-by-user fields still refresh
    assert (a.status, a.affinity_score) == ("rejected", 3.3)


def test_personal_and_non_pack_rows_untouched(tmp_path, monkeypatch):
    root, priv = _setup(tmp_path, monkeypatch, None)
    _publish(root, priv, _v1_items())
    db = _db()
    _install(db, root)
    a, b = _art(db, "alpha"), _art(db, "beta")
    a.is_personal = True
    b.is_seed = False   # a user's own file that merely shares the filename
    db.commit()
    _publish(root, priv, [_mi("Alpha NEW"), _mi("Beta NEW"), _mi("Gamma")])
    _install(db, root)
    assert _art(db, "alpha").title == "Alpha"
    assert _art(db, "beta").title == "Beta"


def test_boot_refresh_runs_on_hash_change_and_noops_when_unchanged(tmp_path, monkeypatch):
    root, priv = _setup(tmp_path, monkeypatch, None)
    _publish(root, priv, _v1_items())
    db = _db()
    _install(db, root)
    assert lifespan.refresh_installed_packs(db) == {}   # version unchanged -> no-op
    _art(db, "alpha").title = "Local drift"             # a pack-sourced field changed locally...
    db.commit()
    assert lifespan.refresh_installed_packs(db) == {}   # ...is NOT reverted while the hash is unchanged
    assert _art(db, "alpha").title == "Local drift"

    _publish(root, priv, [_mi("Alpha", credit_line="Fixed in app update"), _mi("Beta"), _mi("Gamma")])
    out = lifespan.refresh_installed_packs(db)
    assert set(out) == {"masterpieces"}
    a = _art(db, "alpha")
    assert a.attribution == "Fixed in app update" and a.title == "Alpha"
    sub = db.query(SubscriptionModel).filter(SubscriptionModel.url == "pack:masterpieces").one()
    assert sub.applied_manifest_hash == lifespan.manifest_hash(json.loads(sub.cached_manifest))
    assert lifespan.refresh_installed_packs(db) == {}   # applied -> quiet again


def test_boot_refresh_does_not_relink_or_create(tmp_path, monkeypatch):
    root, priv = _setup(tmp_path, monkeypatch, None)
    _publish(root, priv, _v1_items())
    db = _db()
    _install(db, root)
    from sqlalchemy import delete

    from models import playlist_artwork
    db.execute(delete(playlist_artwork))   # user removed every work from the gallery
    db.query(ArtworkModel).filter(ArtworkModel.filename == "gamma.jpg").delete()
    db.commit()
    _publish(root, priv, [_mi("Alpha", credit_line="X"), _mi("Beta"), _mi("Gamma")])
    lifespan.refresh_installed_packs(db)
    assert db.execute(playlist_artwork.select()).first() is None
    assert db.query(ArtworkModel).count() == 2


def test_edit_routes_mark_only_changed_fields(tmp_path, monkeypatch):
    from app import app
    from database import get_db
    root, priv = _setup(tmp_path, monkeypatch, None)
    _publish(root, priv, _v1_items())
    db = _db()
    _install(db, root)
    aid = _art(db, "alpha").id
    app.dependency_overrides[get_db] = lambda: db
    try:
        c = TestClient(app)
        body = {"title": "Edited", "agent_name": "A. Painter", "agent_role": "Artist", "creation_date": "",
                "cultural_context": "French", "medium": "", "date_display": "1890",
                "description_narrative": "A placard.", "tags": ""}
        r = c.patch(f"/artworks/{aid}/metadata", json=body)
        assert r.status_code == 200, r.text
        db.expire_all()
        assert edited_fields(_art(db, "alpha")) == {"title"}
        r = c.patch(f"/artworks/{aid}/crop", json={"crop_x": 1, "crop_y": 2, "crop_width": 3, "crop_height": 4,
                                                   "focal_x": 0.2, "focal_y": 0.3})
        assert r.status_code == 200, r.text
        db.expire_all()
        assert {"title", "crop_x", "crop_y", "crop_width", "crop_height", "focal_x", "focal_y"} <= edited_fields(_art(db, "alpha"))
    finally:
        app.dependency_overrides.clear()


def _cc_by_then_cc0(tmp_path, monkeypatch, edit=None):
    root, priv = _setup(tmp_path, monkeypatch, None)
    by = _mi("Alpha", license="CC-BY-4.0", license_url="https://creativecommons.org/licenses/by/4.0/",
             credit_line=CC_BY_CREDIT, attribution_url="https://x/credit")
    _publish(root, priv, [by, _mi("Beta"), _mi("Gamma")])
    db = _db()
    _install(db, root)
    if edit:
        a = _art(db, "alpha")
        mark_edited(a, [edit])
        db.commit()
    _publish(root, priv, [_mi("Alpha", license="CC0-1.0", credit_line=""), _mi("Beta"), _mi("Gamma")])
    _install(db, root)
    return _art(db, "alpha")


def test_license_change_clears_stale_credit_fields(tmp_path, monkeypatch):
    a = _cc_by_then_cc0(tmp_path, monkeypatch)
    assert a.license == "CC0-1.0"
    assert a.license_url != "https://creativecommons.org/licenses/by/4.0/"
    assert a.attribution is None and a.attribution_url is None   # no BY credit beside a CC0 licence


def test_license_change_respects_user_edited_credit(tmp_path, monkeypatch):
    a = _cc_by_then_cc0(tmp_path, monkeypatch, edit="attribution")
    assert a.license == "CC0-1.0"
    assert a.attribution == CC_BY_CREDIT   # user-edited field wins even when stale
    assert a.attribution_url is None


def test_reinstall_from_older_manifest_skips_refresh_and_keeps_stamp(tmp_path, monkeypatch):
    root, priv = _setup(tmp_path, monkeypatch, None)

    def pub(gen, credit):
        build_pack._emit_v2_manifests(
            root, [{"id": "masterpieces", "title": "Masterpieces", "description": "",
                    "items": [_mi("Alpha", credit_line=credit), _mi("Beta"), _mi("Gamma")]}],
            signing_key=priv, generated_at=gen)
    pub("2026-10-05", "Newer credit")
    db = _db()
    _install(db, root)
    sub = db.query(SubscriptionModel).one()
    stamp, h = sub.applied_generated_at, sub.applied_manifest_hash
    assert _art(db, "alpha").attribution == "Newer credit"
    pub("2026-09-01", "Old credit")       # replayed older signed tar/manifest
    _install(db, root)
    db.refresh(sub)
    assert _art(db, "alpha").attribution == "Newer credit"
    assert (sub.applied_generated_at, sub.applied_manifest_hash) == (stamp, h)
    assert lifespan.refresh_installed_packs(db) == {}   # boot refresh doesn't apply the stale disk copy either



def test_user_edited_license_does_not_wipe_other_credit_fields(tmp_path, monkeypatch):
    a = _cc_by_then_cc0(tmp_path, monkeypatch, edit="license")
    assert a.license == "CC-BY-4.0"                       # user's licence kept
    assert a.attribution == CC_BY_CREDIT                  # nothing wiped on the strength of a licence delta
    assert a.attribution_url == "https://x/credit"
