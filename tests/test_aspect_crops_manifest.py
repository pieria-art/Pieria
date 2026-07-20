"""`aspect_crops` end-to-end through the PACK/MANIFEST chain (mirrors ADR-052's series/resolution_tier
plumbing): tools/build_pack._v2_row -> publisher.build_item -> manifest_validator -> federation."""

import federation
import publisher
from manifest_validator import validate_manifest
from tools import build_pack


def _mi(title, *, aspect_crops=None, focal=None):
    """A v1 manifest item as `tools/build_pack._manifest_item` produces it (input to the v2 emit)."""
    item = {"filename": f"{title.lower()}.jpg", "thumbnail": f"{title.lower()}_t.jpg",
            "source_url": f"https://x/{title}.jpg", "title": title, "agent_name": "A. Painter",
            "cultural_context": "French", "description_narrative": "A placard.", "kind": "painting",
            "license": "Public Domain", "needs_frame_crop": "", "focal_point": focal or [0.5, 0.5],
            "featured_rank": 50, "credit_line": "Some Museum"}
    if aspect_crops is not None:
        item["aspect_crops"] = aspect_crops
    return item


AC = {"16:9": [0.0, 0.1, 1.0, 0.66], "9:16": [0.28, 0.0, 0.72, 1.0], "4:3": [0.0, 0.0, 1.0, 0.9]}


def test_manifest_item_carries_aspect_crops_via_placard_fields():
    """`_manifest_item` copies every `_PLACARD_FIELDS` key straight from the catalog item."""
    filled = build_pack._manifest_item({"aspect_crops": AC, "source_url": "https://x/y.jpg"},
                                        "y.jpg", None)
    assert filled["aspect_crops"] == AC
    bare = build_pack._manifest_item({"source_url": "https://x/y.jpg"}, "y.jpg", None)
    assert bare["aspect_crops"] == ""  # `_PLACARD_FIELDS` default when absent from the catalog item


def test_v2_row_carries_aspect_crops():
    row = build_pack._v2_row(_mi("Sunrise", aspect_crops=AC))
    assert row["aspect_crops"] == AC


def test_v2_row_omits_aspect_crops_when_absent():
    row = build_pack._v2_row(_mi("Sunrise"))
    assert row["aspect_crops"] is None


def test_full_chain_row_to_signed_manifest_to_catalog():
    """`_v2_row` -> `publisher.build_item` -> validate -> sign -> `federation.manifest_item_to_catalog`
    round-trips the field intact, and the manifest validates + verifies."""
    row = build_pack._v2_row(_mi("Sunrise", aspect_crops=AC))
    row["local_file"] = "sunrise.jpg"
    item = publisher.build_item(row)
    assert item["image"]["aspect_crops"] == AC

    meta = {"slug": "test-col", "title": "Test Col", "publisher": {"id": "pub1", "name": "Pub"}}
    priv, pub = publisher.keygen()
    manifest, errors = publisher.assemble_validate_sign(meta, [item], priv, pub)
    assert errors == []
    assert validate_manifest(manifest) == []
    assert federation.verify_signature(manifest) is True

    catalog_item = federation.manifest_item_to_catalog(manifest["items"][0])
    assert catalog_item["aspect_crops"] == AC


def test_manifest_item_to_catalog_omits_absent_aspect_crops():
    row = build_pack._v2_row(_mi("Sunrise"))
    row["local_file"] = "sunrise.jpg"
    item = publisher.build_item(row)
    assert "aspect_crops" not in item["image"]
    catalog_item = federation.manifest_item_to_catalog(item)
    assert catalog_item["aspect_crops"] is None
