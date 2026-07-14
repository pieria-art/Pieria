#!/usr/bin/env python3
"""Audit the redistribution license of every catalog item — the go/no-go gate for BUNDLING.

Linking to a museum's copy (today's model) is legally lighter than *shipping* a copy (bundling).
This tool measures how many catalog items are safe to redistribute, so we know exactly how much
(if anything) a bundle-safe filter would shave off the collection:

  - Museum open-access sources (Met, Art Institute of Chicago, Cleveland, Rijksmuseum) release
    their public-domain-work images as CC0 / Open Access -> classified bundle-safe BY POLICY,
    no network call.
  - Wikimedia Commons hosts MIXED licenses per file, so each is checked LIVE against the Commons
    API (extmetadata LicenseShortName) and bucketed: pd/cc0, cc-by, cc-by-sa, restricted, unknown.

    python -m tools.audit_licenses                  # audit catalog + seed, print summary + flags
    python -m tools.audit_licenses --json           # machine-readable per-item verdicts
    python -m tools.audit_licenses --limit 40       # sample (cap Wikimedia items checked)
    python -m tools.audit_licenses --strict         # exit non-zero if any item needs review (CI)

Bundle-safe = pd/cc0 (ship freely) OR cc-by / cc-by-sa (ship WITH attribution, which we already
carry via agent_name/source). restricted (NC/ND) and unknown are FLAGGED for human review, never
auto-shipped. Wikimedia is politely throttled (shared _wm_throttle); batches 50 titles per query.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from config import SD_USER_AGENT
from scout import _wm_throttle

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_FILE = REPO_ROOT / "static" / "factory_seed.json"
CATALOG_DIR = REPO_ROOT / "static" / "catalog"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Museum sources whose public-domain-work images are released CC0 / Open Access — bundle-safe by
# published policy, so no per-item network check is needed. (Provenance for the audit trail.)
OPEN_ACCESS_POLICY = {
    "The Metropolitan Museum of Art": "CC0 — Met Open Access",
    "Art Institute of Chicago": "CC0 — AIC public-domain works",
    "Cleveland Museum of Art": "CC0 — CMA Open Access",
    "Rijksmuseum": "Public Domain / CC0 — Rijksstudio",
}
# host -> canonical source, used when an item lacks an explicit `source` field (e.g. the seed).
HOST_SOURCE = {
    "commons.wikimedia.org": "Wikimedia Commons",
    "upload.wikimedia.org": "Wikimedia Commons",
    "images.metmuseum.org": "The Metropolitan Museum of Art",
    "www.artic.edu": "Art Institute of Chicago",
    "openaccess-cdn.clevelandart.org": "Cleveland Museum of Art",
}
BUNDLE_SAFE = {"pd", "cc-by", "cc-by-sa"}   # cc-by/-sa are safe *with* attribution (we carry it)


@dataclass
class Item:
    origin: str          # seed | <collection id>
    title: str
    source: str
    source_url: str
    license_label: str   # the label WE stored (usually "Public Domain")
    commons_title: str | None = None   # File:... when Wikimedia, else None
    verdict: str = ""    # pd | cc-by | cc-by-sa | restricted | unknown | error
    detail: str = ""     # actual Commons LicenseShortName, policy note, or error
    # Attribution payload harvested in the SAME Commons pass (feeds the placard credit line +
    # satisfies CC-BY/-SA attribution). Precise "where it hangs" (Wikidata P195) is a follow-on.
    credit_line: str = ""   # best available: Attribution > Credit > Artist > source
    artist: str = ""
    credit: str = ""
    license_url: str = ""


_TAG = re.compile(r"<[^>]+>")


def _strip_html(s: str | None) -> str:
    """Commons Artist/Credit/Attribution values are HTML fragments (links) — flatten to text."""
    return re.sub(r"\s+", " ", _TAG.sub("", s)).strip() if s else ""


def _em(extmeta: dict, key: str) -> str | None:
    return (extmeta.get(key) or {}).get("value")


def _commons_title(source_url: str) -> str | None:
    """Extract the `File:<name>` title from a Commons Special:FilePath URL, else None."""
    p = urlparse(source_url or "")
    if "wikimedia.org" not in p.netloc:
        return None
    marker = "/Special:FilePath/"
    i = p.path.find(marker)
    if i == -1:
        return None
    return "File:" + unquote(p.path[i + len(marker):])


def _source_of(item: dict) -> str:
    if item.get("source"):
        return item["source"]
    host = urlparse(item.get("source_url") or "").netloc
    return HOST_SOURCE.get(host, "(unknown)")


def load_items(scopes: set[str]) -> list[Item]:
    items: list[Item] = []
    if "catalog" in scopes:
        for f in sorted(CATALOG_DIR.glob("*.json")):
            if f.name.startswith("_") or "index" in f.name:
                continue
            d = json.loads(f.read_text())
            for it in (d.get("items") or d.get("artworks") or []):
                su = it.get("source_url", "")
                items.append(Item(origin=f.stem, title=it.get("title", "?"), source=_source_of(it),
                                   source_url=su, license_label=it.get("license") or "(none)",
                                   commons_title=_commons_title(su)))
    if "seed" in scopes and SEED_FILE.exists():
        seed = json.loads(SEED_FILE.read_text())
        for it in (seed if isinstance(seed, list) else seed.get("items", [])):
            su = it.get("source_url", "")
            items.append(Item(origin="seed", title=it.get("title", "?"), source=_source_of(it),
                              source_url=su, license_label=it.get("license") or "(none)",
                              commons_title=_commons_title(su)))
    return items


def classify(license_short: str | None) -> str:
    """Bucket a Commons LicenseShortName into our redistribution verdicts."""
    if not license_short:
        return "unknown"
    s = license_short.lower()
    if "public domain" in s or "cc0" in s or s.startswith("pd"):
        return "pd"
    if "sa" in s and "by" in s:        # CC BY-SA x.y
        return "cc-by-sa"
    if "nc" in s or "nd" in s or "noncommercial" in s or "noderiv" in s:
        return "restricted"
    if "by" in s:                       # CC BY x.y (attribution only)
        return "cc-by"
    return "unknown"


async def _commons_batch(client: httpx.AsyncClient, titles: list[str]) -> dict[str, dict]:
    """Return {File-title -> {license, artist, credit, attribution, license_url}} for up to 50
    Commons titles in one query — all read from the single extmetadata response (no extra calls)."""
    await _wm_throttle()
    params = {"action": "query", "format": "json", "prop": "imageinfo",
              "iiprop": "extmetadata", "redirects": "1", "titles": "|".join(titles)}
    r = await client.get(COMMONS_API, params=params, timeout=60.0)
    data = r.json()
    q = data.get("query", {})
    # Resolve each REQUESTED title -> its final page through normalization + redirects (renamed
    # Commons files are common for featured works; without this they'd read as "not evaluated").
    norm = {n["from"]: n["to"] for n in q.get("normalized", [])}
    redir = {rd["from"]: rd["to"] for rd in q.get("redirects", [])}
    pages_by_title = {p.get("title", ""): p for p in q.get("pages", {}).values()}
    out: dict[str, dict] = {}
    for req in titles:
        page = pages_by_title.get(redir.get(norm.get(req, req), norm.get(req, req)), {})
        ii = page.get("imageinfo")
        em = (ii[0].get("extmetadata") or {}) if ii else {}
        out[req] = {
            "license": _em(em, "LicenseShortName"),
            "artist": _strip_html(_em(em, "Artist")),
            "credit": _strip_html(_em(em, "Credit")),
            "attribution": _strip_html(_em(em, "Attribution")),
            "license_url": _em(em, "LicenseUrl") or "",
        }
    return out


async def audit_wikimedia(items: list[Item], limit: int | None) -> None:
    wm = [it for it in items if it.commons_title]
    if limit:
        wm = wm[:limit]
    if not wm:
        return
    async with httpx.AsyncClient(headers={"User-Agent": SD_USER_AGENT}, follow_redirects=True) as client:
        for i in range(0, len(wm), 50):
            batch = wm[i:i + 50]
            titles = list({it.commons_title for it in batch})   # de-dup: >1 item can share one file
            try:
                lics = await _commons_batch(client, titles)
            except Exception as e:
                for it in batch:
                    it.verdict, it.detail = "error", f"{e.__class__.__name__}: {e}"
                continue
            for it in batch:                                     # apply per item (duplicates included)
                meta = lics.get(it.commons_title) or {}
                it.verdict = classify(meta.get("license"))
                it.detail = meta.get("license") or "no license metadata returned"
                it.artist = meta.get("artist", "")
                it.credit = meta.get("credit", "")
                it.license_url = meta.get("license_url", "")
                it.credit_line = (meta.get("attribution") or meta.get("credit")
                                  or meta.get("artist") or it.source)
            print(f"  …checked {min(i + 50, len(wm))}/{len(wm)} Wikimedia files", file=sys.stderr)


def classify_museum(items: list[Item]) -> None:
    """Non-Wikimedia items: verdict by published source policy (no network)."""
    for it in items:
        if it.commons_title:
            continue
        policy = OPEN_ACCESS_POLICY.get(it.source)
        if policy:
            it.verdict, it.detail = "pd", policy
            it.credit_line = f"Collection of {it.source}"
        else:
            it.verdict, it.detail = "unknown", f"no open-access policy on record for '{it.source}'"


def report(items: list[Item], as_json: bool) -> int:
    for it in items:
        if not it.verdict:
            it.verdict, it.detail = "unknown", "not evaluated"
    flagged = [it for it in items if it.verdict not in BUNDLE_SAFE]
    safe = [it for it in items if it.verdict in BUNDLE_SAFE]

    if as_json:
        print(json.dumps([it.__dict__ for it in items], indent=2))
        return 1 if flagged else 0

    by_source = Counter(it.source for it in items)
    by_verdict = Counter(it.verdict for it in items)
    wm_verdicts = Counter(it.verdict for it in items if it.commons_title)

    print(f"\n=== LICENSE AUDIT — {len(items)} items ===")
    print(f"BUNDLE-SAFE: {len(safe)}   NEEDS REVIEW: {len(flagged)}   "
          f"({100*len(safe)//max(len(items),1)}% safe)")
    print("\nby source:")
    for s, n in by_source.most_common():
        pol = OPEN_ACCESS_POLICY.get(s, "per-file check" if s == "Wikimedia Commons" else "—")
        print(f"  {n:4}  {s:32}  {pol}")
    print("\nby verdict:")
    for v, n in by_verdict.most_common():
        print(f"  {n:4}  {v}")
    if wm_verdicts:
        print("\nWikimedia per-file license breakdown:")
        for v, n in wm_verdicts.most_common():
            print(f"  {n:4}  {v}")
    need_attr = by_verdict.get("cc-by", 0) + by_verdict.get("cc-by-sa", 0)
    with_credit = sum(1 for it in items if it.credit_line)
    print(f"\ncredit line captured: {with_credit}/{len(items)}"
          + (f"  (REQUIRED for {need_attr} CC-BY/-SA items)" if need_attr else ""))
    if flagged:
        print(f"\n=== {len(flagged)} FLAGGED (review before bundling) ===")
        for it in flagged[:60]:
            print(f"  [{it.verdict}] {it.title[:50]:50}  {it.source:20}  {it.detail[:40]}")
        if len(flagged) > 60:
            print(f"  … and {len(flagged) - 60} more (use --json for the full list)")
    return 1 if flagged else 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", default="catalog,seed", help="comma list: catalog,seed")
    ap.add_argument("--limit", type=int, default=None, help="cap Wikimedia files checked (sampling)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="exit non-zero if any item needs review")
    args = ap.parse_args()

    scopes = {s.strip() for s in args.scope.split(",") if s.strip()}
    items = load_items(scopes)
    if not items:
        print("no items loaded", file=sys.stderr)
        return 2
    print(f"loaded {len(items)} items ({sum(1 for i in items if i.commons_title)} Wikimedia, "
          f"{sum(1 for i in items if not i.commons_title)} museum/other)", file=sys.stderr)
    classify_museum(items)
    await audit_wikimedia(items, args.limit)
    rc = report(items, args.json)
    return rc if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
