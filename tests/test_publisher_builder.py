"""Publisher core — item/manifest assembly, validation, and the sign↔verify round-trip."""

import base64

import federation
import publisher
from manifest_validator import validate_manifest


def _meta():
    return {"slug": "janes-art", "title": "Jane's Art",
            "publisher": {"id": "jane", "name": "Jane Doe", "url": "https://jane.test"}}


def _row():
    return {"title": "Sunrise", "full_url": "https://cdn.jane.test/a.jpg", "license": "CC0-1.0",
            "tags": "sea|dawn", "focal_x": "0.3", "focal_y": "0.6", "width": "2400", "height": "1600"}


def test_build_item_shape_and_omits_empties():
    it = publisher.build_item(_row())
    assert it["id"] == "sunrise" and it["title"] == "Sunrise"
    assert it["tags"] == ["sea", "dawn"]
    assert it["image"]["full_url"] == "https://cdn.jane.test/a.jpg"
    assert it["image"]["focal_point"] == [0.3, 0.6]
    assert it["image"]["width"] == 2400 and it["image"]["height"] == 1600
    # empty/absent fields are not present at all
    assert "artist" not in it and "attribution" not in it["image"]


def test_build_item_carries_series_and_resolution_tier():
    # Present values pass through as top-level item keys...
    it = publisher.build_item({**_row(), "series": "Eastern Capital", "resolution_tier": "8K"})
    assert it["series"] == "Eastern Capital" and it["resolution_tier"] == "8K"
    # ...and empties are omitted entirely, keeping the canonical (to-be-signed) bytes clean.
    bare = publisher.build_item(_row())
    assert "series" not in bare and "resolution_tier" not in bare


def test_build_item_accepts_image_url_alias_and_list_tags():
    it = publisher.build_item({"title": "X", "image_url": "https://x/y.jpg", "tags": ["a", "b"]})
    assert it["image"]["full_url"] == "https://x/y.jpg" and it["tags"] == ["a", "b"]


def test_build_manifest_top_level():
    m = publisher.build_manifest(_meta(), [_row()], generated_at="2026-06-27T00:00:00+00:00")
    assert m["manifest_version"] == 2 and m["id"] == "janes-art"
    assert m["publisher"] == {"id": "jane", "name": "Jane Doe", "url": "https://jane.test"}
    assert m["generated_at"] == "2026-06-27T00:00:00+00:00"
    assert len(m["items"]) == 1


def test_sign_verify_roundtrip():
    priv, pub = publisher.keygen()
    m, errors = publisher.assemble_validate_sign(_meta(), [_row()], priv, pub)
    assert errors == []
    assert validate_manifest(m) == []                 # still valid after signing
    assert federation.verify_signature(m) is True      # and the signature checks out
    assert m["publisher"]["public_key"] == pub


def test_invalid_input_is_not_signed():
    priv, _ = publisher.keygen()
    # CC-BY without attribution is invalid per the validator
    bad = {"title": "X", "full_url": "https://x/y.jpg", "license": "CC-BY-4.0"}
    m, errors = publisher.assemble_validate_sign(_meta(), [bad], priv)
    assert errors and "signature" not in m


def test_keygen_distinct_and_derivable():
    p1, k1 = publisher.keygen()
    p2, k2 = publisher.keygen()
    assert p1 != p2 and k1 != k2
    assert publisher.public_from_private(p1) == k1
    # sane base64 Ed25519 sizes (32-byte keys)
    assert len(base64.b64decode(p1)) == 32 and len(base64.b64decode(k1)) == 32
