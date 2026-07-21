"""Manifest v2 validator — strict on required/types/licensing, tolerant of unknown fields."""

import copy

from manifest_validator import is_valid, validate_manifest


def _base():
    """A minimal valid Manifest v2 collection."""
    return {
        "manifest_version": 2,
        "id": "impressionism",
        "title": "Impressionism",
        "items": [
            {
                "id": "monet-sunrise",
                "title": "Impression, Sunrise",
                "image": {"full_url": "https://pub.test/a.jpg", "license": "CC0-1.0"},
            }
        ],
    }


def test_minimal_manifest_is_valid():
    assert validate_manifest(_base()) == []


def test_full_manifest_with_interpretation_is_valid():
    m = _base()
    m["publisher"] = {"id": "pub1", "name": "Jane's Gallery", "public_key": "abc=="}
    m["items"][0]["interpretation"] = {
        "author": "Jane Doe, art historian",
        "license": "CC-BY-4.0",
        "attribution": "© Jane Doe",
        "sections": [{"heading": "Composition", "body": "The horizon sits low…"}],
        "points_of_interest": [{"bbox": [0.4, 0.5, 0.2, 0.1], "commentary": "The sun's reflection…"}],
        "audio": [{"url": "https://pub.test/n.mp3", "license": "CC-BY-4.0", "attribution": "© Jane Doe"}],
    }
    assert is_valid(m), validate_manifest(m)


def test_wrong_version_fails():
    m = _base(); m["manifest_version"] = 1
    assert any("manifest_version" in e for e in validate_manifest(m))


def test_missing_item_essentials_fail():
    m = _base(); del m["items"][0]["title"]; del m["items"][0]["image"]
    errs = validate_manifest(m)
    assert any("title is required" in e for e in errs)
    assert any("image is required" in e for e in errs)


def test_image_requires_full_url_and_license():
    m = _base(); m["items"][0]["image"] = {}
    errs = validate_manifest(m)
    assert any("full_url or local_file is required" in e for e in errs)
    assert any("license is required" in e for e in errs)


def test_image_accepts_local_file_instead_of_full_url():
    """A first-party pack ships bytes: `image.local_file` satisfies the asset requirement with no
    `full_url` (the offline/local-subscription mode). Relaxation — full_url manifests still pass."""
    m = _base()
    m["items"][0]["image"] = {"local_file": "monet-sunrise.jpg", "license": "CC0-1.0"}
    assert validate_manifest(m) == []


def test_image_local_file_still_enforces_license():
    """local_file doesn't loosen anything else — a CC-BY local asset still needs attribution."""
    m = _base()
    m["items"][0]["image"] = {"local_file": "x.jpg", "license": "CC-BY-4.0"}
    assert any("attribution is required" in e for e in validate_manifest(m))


def test_default_license_satisfies_missing_image_license():
    m = _base(); m["default_license"] = "PD"; del m["items"][0]["image"]["license"]
    assert validate_manifest(m) == []


def test_cc_by_image_requires_attribution():
    m = _base(); m["items"][0]["image"]["license"] = "CC-BY-4.0"
    assert any("attribution is required" in e for e in validate_manifest(m))
    m["items"][0]["image"]["attribution"] = "© Museum"
    assert validate_manifest(m) == []


def test_interpretation_needs_its_own_author_and_license():
    m = _base()
    m["items"][0]["interpretation"] = {"sections": [{"body": "x"}]}
    errs = validate_manifest(m)
    assert any("author is required" in e for e in errs)       # separately authored
    assert any("license is required" in e for e in errs)      # separately licensed


def test_bad_bbox_fails():
    m = _base()
    m["items"][0]["interpretation"] = {
        "author": "A", "license": "CC0-1.0",
        "points_of_interest": [{"bbox": [0.4, 0.5, 2.0], "commentary": "off"}],
    }
    assert any("bbox" in e for e in validate_manifest(m))


def test_focal_point_validation():
    m = _base(); m["items"][0]["image"]["focal_point"] = [0.5, 0.33]
    assert validate_manifest(m) == []
    m["items"][0]["image"]["focal_point"] = [0.5, 1.4]   # out of range
    assert any("focal_point" in e for e in validate_manifest(m))


def test_aspect_crops_accepts_good_map():
    m = _base()
    m["items"][0]["image"]["aspect_crops"] = {
        "16:9": [0.0, 0.1, 1.0, 0.66], "9:16": [0.28, 0.0, 0.72, 1.0],
    }
    assert validate_manifest(m) == []


def test_aspect_crops_absent_is_fine():
    m = _base()
    assert "aspect_crops" not in m["items"][0]["image"]
    assert validate_manifest(m) == []


def test_aspect_crops_rejects_non_dict():
    m = _base(); m["items"][0]["image"]["aspect_crops"] = ["16:9", [0.0, 0.0, 1.0, 1.0]]
    assert any("aspect_crops" in e for e in validate_manifest(m))


def test_aspect_crops_rejects_wrong_arity():
    m = _base(); m["items"][0]["image"]["aspect_crops"] = {"16:9": [0.0, 0.1, 1.0]}  # only 3 values
    assert any("aspect_crops" in e for e in validate_manifest(m))


def test_aspect_crops_rejects_out_of_range_number():
    m = _base(); m["items"][0]["image"]["aspect_crops"] = {"16:9": [0.0, 0.1, 1.0, 1.5]}
    assert any("aspect_crops" in e for e in validate_manifest(m))


def test_access_pattern():
    m = _base(); m["items"][0]["access"] = "entitlement:gumroad:prod_123"
    assert validate_manifest(m) == []
    m["items"][0]["access"] = "paywall"
    assert any("access" in e for e in validate_manifest(m))


def test_tags_must_be_string_array():
    m = _base(); m["items"][0]["tags"] = "night, sky"
    assert any("tags" in e for e in validate_manifest(m))


def test_unknown_fields_are_ignored_forward_compat():
    m = _base()
    m["future_top_level"] = {"anything": 1}
    m["items"][0]["future_item_field"] = "ok"
    m["items"][0]["image"]["future_asset_field"] = True
    assert validate_manifest(m) == []  # unknown keys must not break an older validator


def test_validate_manifest_is_pure():
    m = _base(); before = copy.deepcopy(m)
    validate_manifest(m)
    assert m == before  # validator never mutates its input
