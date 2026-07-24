"""DESIGN SPIKE (pack-build plan Phase 1b / the GATE): prove a first-party pack can enter the catalog
through the SAME federation/subscription path a third-party publisher uses — as a *verified local
subscription* — with NO network and no ugly special-casing.

What this locks in (all against the real federation/publisher/validator code, no mocks of them):
1. A signed Manifest v2 whose items ship `image.local_file` (not `full_url`) validates.
2. Signing with a build-time key whose PUBLIC half is in the trusted registry → assess_trust == "verified".
3. `manifest_item_to_catalog` maps a local item to an on-disk reference with a stable `pack:` sentinel
   source_url (dedups like any URL) and never yields a fetchable http URL.
4. Tamper detection still holds (a mutated signed manifest fails verify_signature).

GATE VERDICT (recorded in .ai/decision_log.md ADR-044): PASS — local mode is a clean second asset
mode, and the local-master-reference mechanism already exists & is proven in pre_seed_from_pack.
"""

import copy

import federation
import publisher
from manifest_validator import validate_manifest


def _local_manifest():
    """A minimal 2-item Manifest v2 whose assets are LOCAL (pack ships the bytes)."""
    return {
        "manifest_version": 2,
        "id": "pieria-core",
        "title": "Pieria — Core",
        "publisher": {"id": "pieria", "name": "Pieria"},
        "default_license": "Public Domain",
        "items": [
            {"id": "monet-sunrise", "title": "Impression, Sunrise", "artist": "Claude Monet",
             "image": {"local_file": "impressionism_impression_sunrise.jpg", "focal_point": [0.62, 0.48]}},
            {"id": "vermeer-pearl", "title": "Girl with a Pearl Earring", "artist": "Johannes Vermeer",
             "image": {"local_file": "dutch_girl_with_a_pearl.jpg"}},
        ],
    }


def test_local_manifest_validates():
    assert validate_manifest(_local_manifest()) == []


def test_signed_local_pack_is_verified_when_key_registered():
    """The pack is signed at build time; only the PUBLIC key ships (in the registry). A publisher whose
    registry key matches its manifest key is promoted to 'verified' — no code change, no network."""
    priv, pub = publisher.keygen()
    signed = publisher.sign_manifest(_local_manifest(), priv)

    # untrusted registry → a valid self-signed feed is only 'community'
    assert federation.assess_trust(signed, trusted_keys={}) == "community"
    # the build-time public key registered under the publisher id → 'verified'
    assert federation.assess_trust(signed, trusted_keys={"pieria": pub}) == "verified"
    assert federation.verify_signature(signed) is True


def test_tampered_local_manifest_fails_verification():
    priv, pub = publisher.keygen()
    signed = publisher.sign_manifest(_local_manifest(), priv)
    tampered = copy.deepcopy(signed)
    tampered["items"][0]["title"] = "Not Sunrise"          # mutate signed content
    assert federation.verify_signature(tampered) is False
    assert federation.assess_trust(tampered, trusted_keys={"pieria": pub}) == "community"


def test_local_item_maps_to_on_disk_reference_not_a_url():
    """The add/display layer gets a stable non-http source_url + the local_file to reference in place —
    the SSRF guard / downloader can branch on local_file and never touch the network."""
    m = _local_manifest()
    mapped = [federation.manifest_item_to_catalog(it) for it in m["items"]]

    a = mapped[0]
    assert a["local_file"] == "impressionism_impression_sunrise.jpg"
    assert a["source_url"] == "pack:impressionism_impression_sunrise.jpg"
    assert not a["source_url"].startswith(("http://", "https://"))   # never fetchable
    assert a["thumbnail_url"].startswith("pack:")
    assert a["focal_point"] == [0.62, 0.48]                          # focal survives the round-trip
    # stable + unique → dedups and drives the browse `added` flag exactly like a URL
    assert mapped[0]["source_url"] != mapped[1]["source_url"]


def test_remote_mapping_unchanged_regression():
    """The remote path (third-party full_url) is untouched by the local-mode addition."""
    item = {"id": "x", "title": "X", "image": {"full_url": "https://pub.test/a.jpg", "license": "CC0-1.0"}}
    out = federation.manifest_item_to_catalog(item)
    assert out["source_url"] == "https://pub.test/a.jpg"
    assert out["local_file"] is None
