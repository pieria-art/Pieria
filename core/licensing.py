"""Pack licensing policy (INFRA spec_ccby_attribution.md Stage A; ADR-142, amends ADR-045).

Stdlib only — imported by tools/audit_licenses.py, tools/build_pack.py, manifest_validator.py and CI,
so it must never pull in FastAPI/SQLAlchemy/httpx. Single source of truth for the three ids a pack may
ship under, their licence URLs/display names, a free-text normaliser (today's catalog stores license as
prose, e.g. "Public Domain"), and the CC-BY completeness gate every served/pack row must pass.

ADR-142: packs may ship **PD, CC0 and CC BY 4.0** — never BY-SA, BY-NC or BY-ND. A CC BY row must carry
its credit exactly as given, the licence id + URL, and its source page (`attribution_url`).
"""

from __future__ import annotations

import re

# The three ids a pack may ship (ADR-142). Anything else — including CC-BY-SA — is not pack-allowed.
PACK_ALLOWED: frozenset[str] = frozenset({"PDM-1.0", "CC0-1.0", "CC-BY-4.0"})

LICENSE_URLS: dict[str, str] = {
    "PDM-1.0": "https://creativecommons.org/publicdomain/mark/1.0/",
    "CC0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
}

LICENSE_NAMES: dict[str, str] = {
    "PDM-1.0": "Public domain",
    "CC0-1.0": "CC0",
    "CC-BY-4.0": "CC BY 4.0",
}

# Substrings that mark a licence as explicitly excluded (share-alike / non-commercial / no-derivatives)
# regardless of how the rest of the string reads — these must never fall through to a PD/CC0/CC-BY match.
_EXCLUDED_RE = re.compile(r"by-sa|by-nc|by-nd|noncommercial|non-commercial|noderiv|no-derivative"
                          r"|all-rights-reserved|copyright")


def normalize_license(text: str | None) -> str | None:
    """Map today's free-text `license` values (and already-normalized ids) to a PACK_ALLOWED id, or
    None when the text is share-alike/non-commercial/no-derivatives/unrecognized.

    Handles (case/spacing/hyphenation insensitive): "Public Domain", "Public Domain (Library of
    Congress; no known restrictions)", "PD", "PD-Art", "no known restrictions", "CC0", "cc0-1.0",
    "CC BY 4.0", "CC-BY-4.0", "cc-by". Already-normalized ids pass through unchanged.
    """
    if not text or not isinstance(text, str):
        return None
    raw = text.strip()
    if raw in PACK_ALLOWED:
        return raw
    s = re.sub(r"[\s_]+", "-", raw.lower()).strip("-")
    if not s:
        return None
    if _EXCLUDED_RE.search(s):
        return None
    if "cc0" in s:
        return "CC0-1.0"
    if "cc-by" in s:          # by-sa/by-nc/by-nd already excluded above, so any remaining "cc-by" is 4.0
        return "CC-BY-4.0"
    if ("public-domain" in s or "no-known-restrictions" in s or "pd-art" in s
            or s == "pd" or s.startswith("pd-")):
        return "PDM-1.0"
    return None


def requires_attribution(license_id: str | None) -> bool:
    """True only for CC BY 4.0 — PD/CC0 carry no attribution obligation."""
    return license_id == "CC-BY-4.0"


def safe_http_url(url: str | None) -> str | None:
    """Return `url` unchanged when it's an http(s) link, else None.

    A manifest/catalog-supplied URL (license_url, attribution_url, origin_url, ...) is untrusted text
    that server code and templates turn into an `href` — a `javascript:`/`data:`/`vbscript:` value there
    is a stored-XSS vector (found in ADR-142 Stage B review). Every call site that builds a link from
    such a field must gate it through this helper first; a non-http(s) value renders as plain text, not
    a link.
    """
    if not url or not isinstance(url, str):
        return None
    s = url.strip()
    if s.lower().startswith(("http://", "https://")):
        return s
    return None


def check_pack_row(row: dict) -> list[str]:
    """Validate one catalog/pack row against the pack-ship contract. Returns [] when the row is safe
    to bundle; otherwise a list of human-readable problems.

    Checks: `license` must already be a PACK_ALLOWED id (not free text — Stage A migrates the catalog,
    so an unmigrated/foreign row fails loudly here rather than shipping silently). A CC BY 4.0 row must
    additionally carry non-empty `credit_line`, `license_url` and `attribution_url`.
    """
    problems: list[str] = []
    lic = row.get("license")
    if lic not in PACK_ALLOWED:
        problems.append(f"license {lic!r} is not pack-allowed (must be one of {sorted(PACK_ALLOWED)})")
        return problems
    if requires_attribution(lic):
        for field in ("credit_line", "license_url", "attribution_url"):
            if not (row.get(field) or "").strip():
                problems.append(f"CC BY 4.0 row is missing required {field!r}")
    return problems
