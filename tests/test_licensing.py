import pytest

from core.licensing import (
    LICENSE_NAMES,
    LICENSE_URLS,
    PACK_ALLOWED,
    check_pack_row,
    normalize_license,
    requires_attribution,
    safe_http_url,
)


@pytest.mark.parametrize("text,expected", [
    ("Public Domain", "PDM-1.0"),
    ("Public Domain (Library of Congress; no known restrictions)", "PDM-1.0"),
    ("no known restrictions", "PDM-1.0"),
    ("PD", "PDM-1.0"),
    ("PD-Art", "PDM-1.0"),
    ("CC0", "CC0-1.0"),
    ("cc0-1.0", "CC0-1.0"),
    ("CC0-1.0", "CC0-1.0"),
    ("CC BY 4.0", "CC-BY-4.0"),
    ("CC-BY-4.0", "CC-BY-4.0"),
    ("cc-by", "CC-BY-4.0"),
    ("PDM-1.0", "PDM-1.0"),
    ("  Public Domain  ", "PDM-1.0"),
])
def test_normalize_known_values(text, expected):
    assert normalize_license(text) == expected


@pytest.mark.parametrize("text", [
    None, "", "   ",
    "CC BY-SA 4.0", "CC-BY-SA-4.0", "cc-by-sa",
    "CC BY-NC 4.0", "CC-BY-ND-4.0",
    "All rights reserved", "Copyright 2020 Jane Doe",
    "some unrecognized string",
])
def test_normalize_excluded_or_unknown(text):
    assert normalize_license(text) is None


def test_requires_attribution():
    assert requires_attribution("CC-BY-4.0") is True
    assert requires_attribution("PDM-1.0") is False
    assert requires_attribution("CC0-1.0") is False
    assert requires_attribution(None) is False


def test_license_tables_cover_pack_allowed():
    assert set(LICENSE_URLS) == PACK_ALLOWED
    assert set(LICENSE_NAMES) == PACK_ALLOWED
    for url in LICENSE_URLS.values():
        assert url.startswith("https://creativecommons.org/")


def test_check_pack_row_rejects_non_allowed_license():
    problems = check_pack_row({"license": "Public Domain"})  # free text, not yet normalized
    assert any("not pack-allowed" in p for p in problems)

    problems = check_pack_row({"license": "CC-BY-SA-4.0"})
    assert any("not pack-allowed" in p for p in problems)


def test_check_pack_row_pd_needs_nothing_extra():
    assert check_pack_row({"license": "PDM-1.0"}) == []
    assert check_pack_row({"license": "CC0-1.0"}) == []


def test_check_pack_row_cc_by_requires_credit_license_url_attribution_url():
    row = {"license": "CC-BY-4.0"}
    problems = check_pack_row(row)
    assert len(problems) == 3
    assert any("credit_line" in p for p in problems)
    assert any("license_url" in p for p in problems)
    assert any("attribution_url" in p for p in problems)


def test_check_pack_row_cc_by_complete_passes():
    row = {
        "license": "CC-BY-4.0",
        "credit_line": "ESA/Webb, NASA & CSA",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution_url": "https://esawebb.org/images/example/",
    }
    assert check_pack_row(row) == []


def test_check_pack_row_cc_by_blank_strings_still_fail():
    row = {"license": "CC-BY-4.0", "credit_line": "  ", "license_url": "", "attribution_url": None}
    problems = check_pack_row(row)
    assert len(problems) == 3


def test_safe_http_url_accepts_http_and_https():
    assert safe_http_url("https://example.org/x") == "https://example.org/x"
    assert safe_http_url("http://example.org/x") == "http://example.org/x"
    assert safe_http_url("  https://example.org/x  ") == "https://example.org/x"
    assert safe_http_url("HTTPS://EXAMPLE.ORG/x") == "HTTPS://EXAMPLE.ORG/x"


def test_safe_http_url_rejects_other_schemes_and_junk():
    assert safe_http_url("javascript:alert(1)") is None
    assert safe_http_url("data:text/html,<script>1</script>") is None
    assert safe_http_url("vbscript:msgbox(1)") is None
    assert safe_http_url("not-a-url") is None
    assert safe_http_url("") is None
    assert safe_http_url(None) is None
    assert safe_http_url("pack:some_file.jpg") is None


def test_display_credit_drops_bare_urls_keeps_real_credits():
    from core.licensing import display_credit
    assert display_credit("https://www.artic.edu/artworks/8971") is None
    assert display_credit("  http://visipix.com/index.htm ") is None
    assert display_credit("www.example.org/x") is None
    assert display_credit("http://www.artic.edu/aic/collections/artwork/111628 (Manual stitch by x)") is None
    assert display_credit("") is None and display_credit(None) is None
    assert display_credit("https://clevelandart.org/art/1916.1044 IA") is None
    assert display_credit("NASA, ESA, CSA, STScI") == "NASA, ESA, CSA, STScI"
    assert display_credit("Photo: Jane Doe, https://example.org") == "Photo: Jane Doe, https://example.org"


@pytest.mark.parametrize("text", [
    "CC BY 2.0", "CC BY 2.5", "CC BY 3.0", "CC-BY-3.0", "cc-by-3.0-igo", "CC BY 3.0 IGO",
    "CC BY 2.0 (Flickr)", "cc-by-2.5", "CC BY v3.0", "CC-BY (3.0)", "cc-by/3.0",
])
def test_normalize_cc_by_other_versions_not_allowed(text):
    assert normalize_license(text) is None


@pytest.mark.parametrize("text", ["CC BY 4.0", "cc-by-4.0", "CC-BY-4.0", "CC BY 4.0 (ESA/Webb release)", "CC BY", "cc-by"])
def test_normalize_cc_by_4_and_unversioned_map_to_4(text):
    assert normalize_license(text) == "CC-BY-4.0"
