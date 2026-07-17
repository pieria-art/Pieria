"""Offline install verification for a built art-pack (Phase 4 of the pack build; ADR-044 unified ingestion).

Points core.lifespan at a REAL built pack, installs it into a throwaway in-memory DB with the network
HARD-BLOCKED, and asserts the ADR-044 contract end-to-end on the actual manifests:

  * every collection installs as a local SubscriptionModel (trust tier reported: verified / community),
  * playlists + artworks mint from the LOCAL masters (array order == fame order), a default is chosen,
  * every minted artwork references a master that EXISTS on disk, and a sample of them decode.

Because the whole thing runs with `socket.socket` replaced by a raising stub, a PASS is positive proof the
appliance paints this pack with the ethernet unplugged — the "own the art" guarantee behind ADR-038/044.

    python -m tools.verify_pack_offline --pack ./art-pack        # after a full build
    python -m tools.verify_pack_offline --pack /tmp/art-pack-smoke

Exit 0 = contract holds. Non-zero = a problem (printed). Read-only w.r.t. the pack (temp DB only); the
manifests' trust tier reflects the real registry/trusted_publishers.json (signed build -> 'verified')."""
import argparse
import socket
import sys
from collections import Counter
from pathlib import Path

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", type=Path, default=Path("./art-pack"), help="built pack dir (has pack-index.json)")
    ap.add_argument("--sample", type=int, default=12, help="how many minted masters to decode-check")
    args = ap.parse_args()

    pack = args.pack.resolve()
    if not (pack / "pack-index.json").exists():
        print(f"FAIL: no pack-index.json under {pack} — is this a built pack?")
        return 1

    # Import the real boot module + models, then repoint its path constants at this pack (same override
    # points test_pack_install.py monkeypatches). assess_trust keeps the REAL registry keys on purpose.
    import core.lifespan as lifespan_module
    from database import Base
    from models import ArtworkModel, PlaylistModel, SettingsModel, SubscriptionModel

    lifespan_module.ARTWORK_ROOT = pack
    lifespan_module.LIBRARY_DIR = pack / "_Library"
    lifespan_module.PACK_INDEX = pack / "pack-index.json"

    # HARD network block: install_pack_subscriptions must be local-only — any socket attempt is a failure.
    def _no_net(*_a, **_k):
        raise RuntimeError("network blocked (offline verify): pack install must not touch the network")
    socket.socket = _no_net

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    installed = lifespan_module.install_pack_subscriptions(db)
    if not installed:
        print("FAIL: install_pack_subscriptions returned False (no v2 pack-index?)")
        return 1

    subs = db.query(SubscriptionModel).all()
    playlists = db.query(PlaylistModel).all()
    artworks = db.query(ArtworkModel).all()
    trust = Counter(s.trust for s in subs)
    default = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    seeded = db.query(SettingsModel).filter(SettingsModel.setting_key == "pack_seeded").first()

    lib = lifespan_module.LIBRARY_DIR
    missing = [a.filename for a in artworks if not (lib / a.filename).exists()]
    step = max(1, len(artworks) // max(args.sample, 1))
    sample = artworks[::step][:args.sample]
    bad = []
    for a in sample:
        try:
            with Image.open(lib / a.filename) as im:
                im.verify()
        except Exception as e:  # noqa: BLE001 — any decode failure is a real problem to report
            bad.append((a.filename, str(e)[:60]))

    print("=== OFFLINE PACK INSTALL VERIFY (ADR-044, network blocked) ===")
    print(f"pack:            {pack}")
    print(f"subscriptions:   {len(subs)}  trust={dict(trust)}")
    print(f"playlists:       {len(playlists)}  default={default.setting_value if default else None}")
    print(f"artworks minted: {len(artworks)}")
    print(f"pack_seeded:     {seeded.setting_value if seeded else None}")
    print(f"missing masters: {len(missing)}" + (f"  e.g. {missing[:3]}" if missing else ""))
    print(f"decode sample:   {len(sample) - len(bad)}/{len(sample)} ok" + (f"  BAD={bad}" if bad else ""))

    problems = []
    if not subs:
        problems.append("no subscriptions installed")
    if not artworks:
        problems.append("no artworks minted")
    if missing:
        problems.append(f"{len(missing)} artworks reference missing masters")
    if bad:
        problems.append(f"{len(bad)}/{len(sample)} sample masters failed to decode")
    if not default:
        problems.append("no default_playlist set")
    if problems:
        print("RESULT: FAIL — " + "; ".join(problems))
        return 1
    print(f"RESULT: PASS — {len(subs)} collections install offline, {len(artworks)} works paint-ready "
          f"({trust.get('verified', 0)} verified / {trust.get('community', 0)} community).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
