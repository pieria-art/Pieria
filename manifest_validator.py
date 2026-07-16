"""Validator for Screen Docent **Manifest v2** (see docs/manifest-v2.md).

Executable source of truth for the federation manifest format. Pure-Python, no dependency — it runs
both offline (in the catalog builder) and at runtime (when the app fetches a subscribed manifest).

Design rules:
- STRICT on required fields, types, enums, and per-asset licensing/attribution.
- TOLERANT of unknown fields (forward-compatibility — future docent/marketplace keys must not break
  an older validator; new *required* fields only ever arrive under a bumped manifest_version).

`validate_manifest(obj)` returns a list of human-readable error strings ([] means valid).
"""

from __future__ import annotations

import re

MANIFEST_VERSION = 2
_ACCESS_RE = re.compile(r"^(free|entitlement:[a-z0-9_-]+:.+)$", re.IGNORECASE)


def _requires_attribution(license_str: str) -> bool:
    """CC-BY and CC-BY-SA require attribution; CC0/PD/proprietary do not."""
    return bool(re.match(r"^cc-by", (license_str or "").strip(), re.IGNORECASE))


def _is_str(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _check_asset_license(asset: dict, path: str, errors: list, *, license_required: bool):
    lic = asset.get("license")
    if lic is None:
        if license_required:
            errors.append(f"{path}.license is required (no manifest default_license set)")
        return
    if not _is_str(lic):
        errors.append(f"{path}.license must be a non-empty string")
        return
    if _requires_attribution(lic) and not _is_str(asset.get("attribution")):
        errors.append(f"{path}.attribution is required when license is '{lic}'")


def _validate_image(image, path, errors, *, has_default_license):
    if not isinstance(image, dict):
        errors.append(f"{path} must be an object")
        return
    if not _is_str(image.get("full_url")):
        errors.append(f"{path}.full_url is required (non-empty string)")
    for k in ("width", "height"):
        if k in image and not isinstance(image[k], int):
            errors.append(f"{path}.{k} must be an integer")
    fp = image.get("focal_point")
    if fp is not None and not (isinstance(fp, (list, tuple)) and len(fp) == 2
                              and all(isinstance(n, (int, float)) and 0.0 <= n <= 1.0 for n in fp)):
        errors.append(f"{path}.focal_point must be [x, y] with each value a number in 0..1")
    _check_asset_license(image, path, errors, license_required=not has_default_license)


def _validate_interpretation(interp, path, errors):
    if not isinstance(interp, dict):
        errors.append(f"{path} must be an object")
        return
    # Interpretation is authored & licensed independently of the image.
    if not _is_str(interp.get("author")):
        errors.append(f"{path}.author is required (interpretation is separately authored)")
    _check_asset_license(interp, path, errors, license_required=True)

    sections = interp.get("sections")
    if sections is not None:
        if not isinstance(sections, list):
            errors.append(f"{path}.sections must be an array")
        else:
            for i, s in enumerate(sections):
                if not isinstance(s, dict) or not _is_str(s.get("body")):
                    errors.append(f"{path}.sections[{i}] must be an object with a non-empty 'body'")

    pois = interp.get("points_of_interest")
    if pois is not None:
        if not isinstance(pois, list):
            errors.append(f"{path}.points_of_interest must be an array")
        else:
            for i, poi in enumerate(pois):
                p = f"{path}.points_of_interest[{i}]"
                if not isinstance(poi, dict):
                    errors.append(f"{p} must be an object")
                    continue
                if not _is_str(poi.get("commentary")):
                    errors.append(f"{p}.commentary is required (non-empty string)")
                bbox = poi.get("bbox")
                if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
                        or not all(isinstance(n, (int, float)) and 0.0 <= n <= 1.0 for n in bbox)):
                    errors.append(f"{p}.bbox must be [x, y, w, h] with each value a number in 0..1")

    audio = interp.get("audio")
    if audio is not None:
        if not isinstance(audio, list):
            errors.append(f"{path}.audio must be an array")
        else:
            for i, track in enumerate(audio):
                p = f"{path}.audio[{i}]"
                if not isinstance(track, dict):
                    errors.append(f"{p} must be an object")
                    continue
                if not _is_str(track.get("url")):
                    errors.append(f"{p}.url is required (non-empty string)")
                _check_asset_license(track, p, errors, license_required=True)


def _validate_item(item, idx, errors, *, has_default_license):
    path = f"items[{idx}]"
    if not isinstance(item, dict):
        errors.append(f"{path} must be an object")
        return
    if not _is_str(item.get("id")):
        errors.append(f"{path}.id is required (non-empty string)")
    if not _is_str(item.get("title")):
        errors.append(f"{path}.title is required (non-empty string)")
    if "tags" in item and not (isinstance(item["tags"], list) and all(isinstance(t, str) for t in item["tags"])):
        errors.append(f"{path}.tags must be an array of strings")
    if "access" in item and not (isinstance(item["access"], str) and _ACCESS_RE.match(item["access"])):
        errors.append(f"{path}.access must be 'free' or 'entitlement:<provider>:<id>'")

    if "image" not in item:
        errors.append(f"{path}.image is required")
    else:
        _validate_image(item["image"], f"{path}.image", errors, has_default_license=has_default_license)

    if "interpretation" in item:
        _validate_interpretation(item["interpretation"], f"{path}.interpretation", errors)


def validate_publisher(pub, errors):
    if not isinstance(pub, dict):
        errors.append("publisher must be an object")
        return
    for k in ("id", "name"):
        if not _is_str(pub.get(k)):
            errors.append(f"publisher.{k} is required (non-empty string)")


def validate_manifest(obj) -> list[str]:
    """Validate a Manifest v2 collection document. Returns [] if valid, else a list of errors."""
    errors: list[str] = []
    if not isinstance(obj, dict):
        return ["manifest must be a JSON object"]

    if obj.get("manifest_version") != MANIFEST_VERSION:
        errors.append(f"manifest_version must be {MANIFEST_VERSION} (got {obj.get('manifest_version')!r})")
    if not _is_str(obj.get("id")):
        errors.append("id is required (non-empty string)")
    if not _is_str(obj.get("title")):
        errors.append("title is required (non-empty string)")
    if "cover_image" in obj and not _is_str(obj["cover_image"]):
        errors.append("cover_image must be a non-empty string (URL) when present")
    if "publisher" in obj:
        validate_publisher(obj["publisher"], errors)

    items = obj.get("items")
    if not isinstance(items, list):
        errors.append("items is required (array)")
    else:
        has_default_license = _is_str(obj.get("default_license"))
        for idx, item in enumerate(items):
            _validate_item(item, idx, errors, has_default_license=has_default_license)

    return errors


def is_valid(obj) -> bool:
    return not validate_manifest(obj)
