"""Unit tests for tools.resource_artic's resolution-tier stamping (see .ai/spec_resolution_tags.md)
and its licence gate (ADR-045, unbound-only — a Commons mirror can carry a non-free licence even
when the underlying museum work is PD, so a re-source must check the mirror, not assume the museum's
open-access policy)."""
import json
from unittest.mock import patch

from tools.build_pack import GRANDFATHER_MIN_EDGE
from tools.resource_artic import license_check, resolution_fields


def test_below_hard_floor_is_parked():
    assert resolution_fields(2000) is None
    assert resolution_fields(GRANDFATHER_MIN_EDGE - 1) is None


def test_at_hard_floor_is_grandfathered_hd():
    fields = resolution_fields(GRANDFATHER_MIN_EDGE)
    assert fields == {
        "delivered_edge": GRANDFATHER_MIN_EDGE, "resolution_tier": "HD", "below_floor_ok": True,
    }


def test_below_4k_floor_is_grandfathered_hd():
    fields = resolution_fields(3000)
    assert fields == {"delivered_edge": 3000, "resolution_tier": "HD", "below_floor_ok": True}


def test_true_4k_is_not_flagged_below_floor():
    fields = resolution_fields(5000)
    assert fields == {"delivered_edge": 5000, "resolution_tier": "4K"}
    assert "below_floor_ok" not in fields


def test_native_above_display_cap_is_capped_and_tagged_8k():
    fields = resolution_fields(20000)
    assert fields["delivered_edge"] == 7680
    assert fields["resolution_tier"] == "8K"
    assert "below_floor_ok" not in fields


class _FakeResponse:
    """Minimal stand-in for urllib's HTTPResponse: a context manager whose .read() returns JSON
    bytes, exactly what json.load(resp) needs."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _commons_page(title: str, license_short: str | None, license_url: str = "", credit: str = ""):
    em = {}
    if license_short is not None:
        em["LicenseShortName"] = {"value": license_short}
    if license_url:
        em["LicenseUrl"] = {"value": license_url}
    if credit:
        em["Credit"] = {"value": credit}
    return {"title": f"File:{title}", "imageinfo": [{"extmetadata": em}]}


def test_license_check_pd_file_is_bundle_safe():
    fake = {"query": {"pages": {"1": _commons_page("PD work.jpg", "Public domain",
                                                     credit="Art Institute of Chicago")}}}
    with patch("urllib.request.urlopen", return_value=_FakeResponse(fake)):
        result = license_check(["PD work.jpg"], cache={})
    verdict, detail, license_url, credit_line = result["PD work.jpg"]
    assert verdict == "pd"
    assert detail == "Public domain"
    assert credit_line == "Art Institute of Chicago"


def test_license_check_flags_non_free_commons_mirror():
    """The catalog work is PD, but a Commons PHOTO of it can still be CC-BY-SA — must not be
    classified as bundle-safe just because the underlying museum work is public domain."""
    fake = {"query": {"pages": {"1": _commons_page("Restricted photo.jpg", "CC BY-SA 4.0")}}}
    with patch("urllib.request.urlopen", return_value=_FakeResponse(fake)):
        result = license_check(["Restricted photo.jpg"], cache={})
    verdict, detail, _url, _credit = result["Restricted photo.jpg"]
    assert verdict == "cc-by-sa"
    assert detail == "CC BY-SA 4.0"


def test_license_check_transient_error_is_never_a_verdict():
    """A Commons fetch failure must surface as 'error', never get baked in as pd/unknown — the
    caller (build_plan) relies on this to leave the item untouched rather than parking it."""
    import urllib.error

    err = urllib.error.HTTPError(url="", code=503, msg="down", hdrs={}, fp=None)
    with patch("urllib.request.urlopen", side_effect=err), patch("time.sleep"):
        result = license_check(["Whatever.jpg"], cache={})
    verdict, *_rest = result["Whatever.jpg"]
    assert verdict == "error"
