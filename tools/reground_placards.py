"""Re-ground every pack placard from retrieved facts instead of an unsupported LLM guess.

The audit (scratchpad/audit/verdicts_batch_*.jsonl, 150 works) found 11% of placards with a major
factual error. All were written by tools/build_catalog.py `enrich_item` using the fast model
(gemini-3.1-flash-lite), text-only, from title/artist/date/medium/source alone — no retrieval, so the
model invents biography, misattributes the famous version's story to a different physical object, and
defaults medium to "Oil on canvas".

This tool fixes that by inverting the pipeline:
  1. Resolve each work's identity (reusing tools.audit_placards's resolver — never guess) and assemble
     a per-item FACTS BUNDLE from museum APIs / Wikidata / Commons extmetadata, each fact tagged with
     its source + licence.
  2. Fill structured fields (medium, date, repository, dimensions, agent) DETERMINISTICALLY from that
     bundle, by source precedence — the model never writes these.
  3. Have the model write ONLY the narrative prose, constrained to the bundle, with every claim traced
     back to a fact key and automatically checked; an unbacked or wrong claim triggers one regeneration,
     then a deterministic template fallback.

Read-only against the repo catalog; writes only under the scratchpad (facts/, catalog/, report.json).
Reuses tools.audit_placards's Fetcher (disk cache + retry/backoff + concurrency<=4) so the two tools
share one polite HTTP cache.

    python -m tools.reground_placards --only-sample   # the 150 audit works (fast, for verification)
    python -m tools.reground_placards                  # the whole catalog (resumable via facts/ cache)
    python -m tools.reground_placards --limit 20
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import io
import json
import logging
import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import httpx
from dotenv import load_dotenv

load_dotenv()

from pathlib import Path

from PIL import Image

import ai_client
from core.licensing import normalize_license, requires_attribution
from tools import catalog_spec
from tools.audit_placards import (
    AUDIT_DIR,
    CACHE_DIR,
    CLEVELAND_SEARCH,
    UA,
    WD_SPARQL,
    Fetcher,
    _commons_structured,  # noqa: F401  (re-exported for callers/tests that want it)
    _museum_record,
    _norm,
    _slug,
    _wikipedia_lead,
    resolve_work,
)
from tools.audit_placards import load_catalog as _audit_load_catalog
from tools.verify_placards import load_deferred_drops, pre_drop_index

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("reground")

# Round 9: NEVER /tmp — the original scratchpad-derived location (AUDIT_DIR.parent / "reground") lived
# on tmpfs and a laptop reboot wiped facts/packets/previews/batches/catalog/cache entirely; only the
# writers' narratives survived, because they happened to be backed up outside it by hand. Durable by
# default, its own independent env var (not derived from AUDIT_DIR — the two are siblings under
# ~/pieria-img/, not parent/child). `set_workdir()` below lets --workdir override every derived path
# at runtime, for tests and for pointing a single run somewhere else without touching the environment.
REGROUND_DIR = Path(os.environ.get("REGROUND_DIR", str(Path.home() / "pieria-img" / "reground")))
FACTS_DIR = REGROUND_DIR / "facts"
OUT_CATALOG_DIR = REGROUND_DIR / "catalog"
REPORT_PATH = REGROUND_DIR / "report.json"
PACKETS_DIR = REGROUND_DIR / "packets"
PREVIEWS_DIR = REGROUND_DIR / "previews"
BATCHES_DIR = REGROUND_DIR / "batches"
WRITTEN_DIR = REGROUND_DIR / "written"
IMPORT_REPORT_PATH = REGROUND_DIR / "import_report.json"
IDENTITY_MISMATCHES_PATH = REGROUND_DIR / "identity_mismatches.json"
MUSEUM_MATCH_CHANGES_PATH = REGROUND_DIR / "museum_match_changes.json"
DUPLICATE_IMAGES_PATH = REGROUND_DIR / "duplicate_images.json"
PACKET_INDEX_PATH = REGROUND_DIR / "packets_index.json"
BLANK_MASTERS_PATH = REGROUND_DIR / "blank_masters.json"
DROPS_REPORT_PATH = REGROUND_DIR / "drops_report.json"
# Default input for --mode apply-drops (~/pieria-img/curation/deferred_drops.json) — a curation-review
# artifact, not a reground/ run artifact, so it deliberately lives beside applied.json rather than
# under REGROUND_DIR, and is NOT re-derived by set_workdir below (it names a fixed input file, not a
# per-run output path). Override per-call via run_apply_drops(drops_path=...) / --drops.
DEFAULT_DROPS_PATH = Path.home() / "pieria-img" / "curation" / "deferred_drops.json"


def set_workdir(path: Path) -> None:
    """Override REGROUND_DIR and every path derived from it, at runtime (the --workdir CLI flag).
    Module-level globals, not a class, because every function in this file already reads them as
    bare names — this keeps that contract instead of threading a config object through everything."""
    global REGROUND_DIR, FACTS_DIR, OUT_CATALOG_DIR, REPORT_PATH, PACKETS_DIR, PREVIEWS_DIR, \
        BATCHES_DIR, WRITTEN_DIR, IMPORT_REPORT_PATH, IDENTITY_MISMATCHES_PATH, \
        MUSEUM_MATCH_CHANGES_PATH, DUPLICATE_IMAGES_PATH, PACKET_INDEX_PATH, BLANK_MASTERS_PATH, \
        DROPS_REPORT_PATH
    REGROUND_DIR = Path(path)
    FACTS_DIR = REGROUND_DIR / "facts"
    OUT_CATALOG_DIR = REGROUND_DIR / "catalog"
    REPORT_PATH = REGROUND_DIR / "report.json"
    PACKETS_DIR = REGROUND_DIR / "packets"
    PREVIEWS_DIR = REGROUND_DIR / "previews"
    BATCHES_DIR = REGROUND_DIR / "batches"
    WRITTEN_DIR = REGROUND_DIR / "written"
    IMPORT_REPORT_PATH = REGROUND_DIR / "import_report.json"
    IDENTITY_MISMATCHES_PATH = REGROUND_DIR / "identity_mismatches.json"
    MUSEUM_MATCH_CHANGES_PATH = REGROUND_DIR / "museum_match_changes.json"
    DUPLICATE_IMAGES_PATH = REGROUND_DIR / "duplicate_images.json"
    PACKET_INDEX_PATH = REGROUND_DIR / "packets_index.json"
    BLANK_MASTERS_PATH = REGROUND_DIR / "blank_masters.json"
    DROPS_REPORT_PATH = REGROUND_DIR / "drops_report.json"


ROOT = Path(__file__).resolve().parent.parent
ART_PACK_LIBRARY = ROOT / "art-pack" / "_Library"
ART_PACK_MANIFESTS = ROOT / "art-pack" / "_manifests"
# --mode land's target — the SERVED catalog (never OUT_CATALOG_DIR, which is the import's own output).
# Not re-derived by set_workdir: it names the repo's static assets, not a per-run reground/ path.
STATIC_CATALOG_DIR = ROOT / "static" / "catalog"
# --catalog-dir: the catalog every mode READS (packets/import/recheck/...) and --mode land WRITES. None ->
# the default (audit_placards.CATALOG_DIR for reads, STATIC_CATALOG_DIR for land) — both static/catalog.
CATALOG_SRC_DIR: Path | None = None


def set_catalog_dir(path: Path | str | None) -> None:
    global CATALOG_SRC_DIR
    CATALOG_SRC_DIR = Path(path).expanduser() if path else None


def load_catalog() -> dict[str, list[dict]]:
    """{collection: items} from --catalog-dir when set, else audit_placards' default catalog."""
    if CATALOG_SRC_DIR is None:
        return _audit_load_catalog()
    out = {}
    for f in sorted(CATALOG_SRC_DIR.glob("*.json")):
        if f.name == "index.json" or f.name.startswith("_"):
            continue
        out[f.stem] = json.loads(f.read_text()).get("items", [])
    return out

WD_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
AIC_SEARCH = "https://api.artic.edu/api/v1/artworks/search"
PREVIEW_MAX_PX = 1024

# Wikidata properties consulted for the facts bundle. English-labelled claims only.
CLAIM_PROPS = {
    "P170": "creator", "P571": "inception", "P186": "made_from_material", "P136": "genre",
    "P135": "movement", "P195": "collection", "P276": "location", "P180": "depicts",
    "P2048": "height", "P2049": "width",
}
# "is a version of" signals — never guessed, only reported when Wikidata states one of these.
VERSION_PROPS = {"P1877": "after a work by", "P144": "based on", "P629": "edition or translation of"}

NARRATIVE_MODEL = "gemini-3.5-flash"  # primary ai_model per spec — explicitly NOT the fast model,
# and explicitly NOT whatever the live app's DB settings happen to be configured to right now (a dev
# box may have Admin -> AI Engine pointed at a different provider entirely). Built fresh from the
# Gemini preset + GEMINI_API_KEY so this tool's model choice never silently follows that setting.


def _narrative_cfg() -> dict:
    preset = ai_client.PRESETS["gemini"]
    return {
        "provider": "gemini", "base_url": preset["base_url"],
        "api_key": os.getenv("GEMINI_API_KEY") or "",
        "model": NARRATIVE_MODEL, "model_fast": NARRATIVE_MODEL, "temperature": None,
    }

# ----------------------------------------------------------------------- accession-year guard
# Accession/object numbers look like "1940.116", "2019.34.1", "78.PA.1" — a leading 4-digit "year"
# segment followed by a dotted registrar suffix. A bare "1940" or "c. 1874" is NOT accession-shaped.
_ACCESSION_RE = re.compile(r"^\s*\d{2,4}\s*\.\s*\d+(\s*\.\s*\d+)*\s*$")


def looks_like_accession_number(value: str) -> bool:
    """True when `value` is shaped like a museum accession/object number rather than a date."""
    if not value:
        return False
    return bool(_ACCESSION_RE.match(str(value).strip()))


# ----------------------------------------------------------------------- medium bucketing
_MEDIUM_BUCKETS = [
    ("oil", re.compile(r"\boil\b", re.I)),
    ("watercolor", re.compile(r"\bwater\s*colou?r\b|\bgouache\b", re.I)),
    ("print", re.compile(r"\bwoodblock\b|\bwoodcut\b|\bengraving\b|\betching\b|\blithograph\b|\bprint\b", re.I)),
    ("photograph", re.compile(r"\bphotograph\b|\bgelatin silver\b|\balbumen\b", re.I)),
    ("sculpture_bronze", re.compile(r"\bbronze\b", re.I)),
    ("sculpture_marble", re.compile(r"\bmarble\b", re.I)),
    ("drawing", re.compile(r"\bpencil\b|\bcharcoal\b|\bink\b|\bgraphite\b|\bdrawing\b", re.I)),
]


def medium_bucket(medium: str) -> str | None:
    for name, pat in _MEDIUM_BUCKETS:
        if pat.search(medium or ""):
            return name
    return None


# ----------------------------------------------------------------------- fact record helpers
def _fact(key, value, source, source_url, licence):
    return {"key": key, "value": value, "source": source, "source_url": source_url, "licence": licence}


async def _commons_extmetadata(fx: Fetcher, filename: str) -> dict | None:
    """Commons file extmetadata (DateTimeOriginal, ObjectName, Artist, Credit, medium/institution
    fields where present). CHECK-ONLY for free text (CC BY-SA); structured date/artist fields are
    treated as low-precedence facts."""
    body, err = await fx.get_json(COMMONS_API, {
        "action": "query", "titles": f"File:{filename}", "prop": "imageinfo",
        "iiprop": "extmetadata", "format": "json",
    })
    if not body:
        return None
    pages = ((body.get("query") or {}).get("pages") or {})
    page = next(iter(pages.values()), {}) if pages else {}
    ii = (page.get("imageinfo") or [{}])[0]
    meta = ii.get("extmetadata") or {}
    out = {}
    for k in ("DateTimeOriginal", "DateTime", "ObjectName", "Artist", "Credit", "Institution",
              "Medium", "ImageDescription"):
        v = (meta.get(k) or {}).get("value")
        if v:
            out[k] = _clean_commons_text(str(v))
    return out or None


# ----------------------------------------------------------------------- capture-timestamp rejection
# Round 3 bug: Commons "DateTimeOriginal" sometimes holds the SCAN/UPLOAD timestamp, not the artwork's
# creation date (e.g. "13 March 2008, 13:55:16" on a 19th-century Remington painting) — a time-of-day
# component is the giveaway (nobody knows the hour a 19th-century painting was finished), as is a year
# at/after the file's own upload year, or after the artist died.
_TIME_OF_DAY_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")


def looks_like_capture_timestamp(text: str, upload_year: int | None = None,
                                  death_year: int | None = None) -> bool:
    if not text:
        return False
    if _TIME_OF_DAY_RE.search(text):
        return True
    y = _extract_year(text)
    if y:
        if upload_year and y >= upload_year:
            return True
        if death_year and y > death_year:
            return True
    return False


# Round 3 #9: Commons DateTimeOriginal can survive `_clean_commons_text`'s QS/HTML stripping still
# garbled — e.g. "1632 Baroque (late 16th century" (a style label bled into the date field, unbalanced
# parenthesis left over from a template). Reject anything that doesn't read as a clean date expression.
_STYLE_WORD_IN_DATE_RE = re.compile(
    r"\b(Baroque|Renaissance|Rococo|Gothic|Romantic\w*|Impressionis\w*|Realis\w*|Neoclassic\w*|"
    r"Modernis\w*|Mannerist\w*)\b", re.I,
)


def looks_like_garbled_date(text: str) -> bool:
    if not text or not text.strip():
        return True
    if text.count("(") != text.count(")") or text.count("[") != text.count("]"):
        return True
    if _STYLE_WORD_IN_DATE_RE.search(text):
        return True
    if not _extract_year(text) and not re.search(r"\b\d{1,2}(st|nd|rd|th)\s+century\b", text, re.I):
        return True  # no year and no century phrase — not a recognisable date at all
    return False


async def _creator_death_year(fx: Fetcher, creator_qid: str) -> int | None:
    body, err = await fx.get_json(WD_API, {
        "action": "wbgetentities", "ids": creator_qid, "props": "claims", "format": "json",
    })
    if not body:
        return None
    ent = (body.get("entities") or {}).get(creator_qid) or {}
    vals = (ent.get("claims") or {}).get("P570") or []  # date of death
    if not vals:
        return None
    dv = vals[0].get("mainsnak", {}).get("datavalue", {}).get("value", {})
    if not isinstance(dv, dict) or "time" not in dv:
        return None
    return _extract_year(dv["time"])


async def _death_year_by_artist_name(fx: Fetcher, name: str) -> int | None:
    """Fallback for when the WORK itself has no confident Wikidata match but the catalog's own
    agent_name is a real, well-known artist (round 7: "Farmyard in Normandy" — Monet, died 1926 — had
    no work-level match, so the death-year guard on its bogus "2024-04-10" catalog date never even
    ran). Top wbsearchentities hit only — imprecise for an obscure name, but adequate for this one
    yes/no question ("could this genuinely be that artist's date"), and never used for attribution."""
    name = (name or "").strip()
    if not name or name.lower() in ("unknown artist", "unknown"):
        return None
    body, err = await fx.get_json(WD_API, {
        "action": "wbsearchentities", "search": name, "language": "en", "type": "item",
        "limit": 1, "format": "json",
    })
    cands = (body or {}).get("search") or []
    if not cands:
        return None
    return await _creator_death_year(fx, cands[0]["id"])


# Commons extmetadata date/artist/title fields routinely embed a hidden Wikidata "quick statement"
# bot annotation right after the human-readable text — e.g. "December 1888date QS:P571,+1888-12-00T00
# :00:00Z/10" or "Fish and Rocks"label QS:Len,"Fish and Rocks"" — strip it, plus HTML tags, so a
# structured field never ships that garbage to the model or the catalog. The annotation always starts
# at "QS:" and runs to the end of the value; the word/quote glued directly onto its left (no space —
# "date", or an opening quote + "label") is part of the annotation marker, not the content, and goes
# with it.
_QS_TAIL_RE = re.compile(r"\bQS:.*$")
_QS_MARKER_RE = re.compile(r'(?:date|"label)\s*$')


def _clean_commons_text(v: str) -> str:
    v = re.sub(r"<[^>]+>", "", v)
    v = _QS_TAIL_RE.sub("", v)
    v = _QS_MARKER_RE.sub("", v)
    return re.sub(r"\s+", " ", v).strip()


# ----------------------------------------------------------------------- Commons credit -> museum
# Round 2 fix for the n=45 gap: an item sourced via Wikimedia Commons (item.source == "Wikimedia
# Commons") never triggered _museum_record's title-keyed lookup even when the file's own Credit field
# names the holding museum and its accession number. Extract institution + accession from Commons
# extmetadata and follow it to that museum's own keyless API (Cleveland: direct accession lookup) or,
# generically, to Wikidata via P217 (inventory number) + P195 (collection) — never guessed, only
# accepted when both the accession AND the institution keyword match.
DOMAIN_INSTITUTIONS = {
    "clevelandart.org": "Cleveland Museum of Art",
    "metmuseum.org": "The Metropolitan Museum of Art",
    "nga.gov": "National Gallery of Art",
    "yale.edu": "Yale University Art Gallery",
    "terraamericanart.org": "Terra Foundation for American Art",
    "denverartmuseum.org": "Denver Art Museum",
    "si.edu": "Smithsonian",
    "artic.edu": "Art Institute of Chicago",
    "rijksmuseum.nl": "Rijksmuseum",
    "smk.dk": "SMK",
}
_ACCESSION_IN_TEXT_RE = re.compile(r"\b\d{2,4}\.\d[\w.\-]*\b")


def _find_institution_and_accession(text: str) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    for domain, name in DOMAIN_INSTITUTIONS.items():
        if domain in text:
            m = _ACCESSION_IN_TEXT_RE.search(text)
            return name, (m.group(0) if m else None)
    return None, None


async def _cleveland_by_accession(fx: Fetcher, accession: str) -> dict | None:
    body, err = await fx.get_json(CLEVELAND_SEARCH, {"accession_number": accession})
    data = (body or {}).get("data") or []
    if not data:
        return None
    d = data[0]
    out = {
        "api": "Cleveland Open Access API (by accession)",
        "url": f"https://openaccess-api.clevelandart.org/api/artworks/{d.get('id')}",
        "title": d.get("title"), "creators": [c.get("description") for c in (d.get("creators") or [])],
        "creation_date": d.get("creation_date"), "technique": d.get("technique"), "culture": d.get("culture"),
        "dimensions": d.get("measurements"),
    }
    if d.get("share_license_status") == "CC0":
        for k in ("description", "did_you_know"):
            if d.get(k):
                out[k] = re.sub(r"<[^>]+>", "", str(d[k])).strip()
    return out


async def _cleveland_extra_by_title(fx: Fetcher, title: str) -> dict | None:
    """Round 3 #5: Cleveland's own `description`/`did_you_know` curatorial text, CC0 — but only when
    THIS record's own `share_license_status` says so (some Cleveland records are not CC0), and this
    fresh title search actually landed on (near enough) the same object as the caller's title."""
    body, err = await fx.get_json(CLEVELAND_SEARCH, {"q": title, "limit": 1})
    data = (body or {}).get("data") or []
    if not data or data[0].get("share_license_status") != "CC0":
        return None
    d = data[0]
    if _title_similarity(title, d.get("title") or "") < _TITLE_SIM_THRESHOLD:
        return None
    out = {}
    for k in ("description", "did_you_know"):
        if d.get(k):
            out[k] = re.sub(r"<[^>]+>", "", str(d[k])).strip()
    if d.get("measurements"):
        out["dimensions"] = d["measurements"]
    return out or None


async def _aic_record(fx: Fetcher, title: str) -> dict | None:
    """Art Institute of Chicago's public API — same shape as _museum_record's dict so it flows through
    the one fact-emission loop. `description`/`short_description` are CC BY 4.0 per AIC's own
    license_text (measured 2026-09-25: "The `description` field ... is licensed under ... CC-By"), NOT
    CC0 like the rest of the record — kept out of `facts`, returned under `_checkonly` instead."""
    body, err = await fx.get_json(AIC_SEARCH, {
        "q": title, "limit": 1,
        "fields": "id,title,date_display,medium_display,artist_display,dimensions,description,short_description",
    })
    data = (body or {}).get("data") or []
    if not data:
        return None
    d = data[0]
    url = f"https://api.artic.edu/api/v1/artworks/{d.get('id')}"
    out = {"api": "Art Institute of Chicago API", "url": url, "title": d.get("title"),
           "artist_display": d.get("artist_display")}
    if d.get("date_display"):
        out["objectDate"] = d["date_display"]
    if d.get("medium_display"):
        out["medium"] = d["medium_display"]
    if d.get("dimensions"):
        out["dimensions"] = d["dimensions"]
    checkonly = []
    for k in ("description", "short_description"):
        if d.get(k):
            checkonly.append(re.sub(r"<[^>]+>", "", str(d[k])).strip())
    out["_checkonly"] = checkonly
    return out


# A run of capitalised words ending in an institution keyword, optionally followed by "of <Place>" (or a
# bare proper noun — "Museum Barberini" has no "of") — e.g. "National Gallery of Art" out of a Credit
# sentence that doesn't match a known DOMAIN_INSTITUTIONS entry (round 3 #3: current_repository from
# generic Commons Credit/Institution text). Round 13: NO period in the word-character classes — a
# footnote/sentence boundary ("...National Gallery of Art. Please see...") was previously bridged
# because `.` sat inside `[\w&.'-]`, so the run kept extending across the full stop into the next
# sentence ("National Gallery of Art. Please"). Dropping `.` here means a following sentence can never
# be absorbed, since the required `\s+` before each further word never gets past the intervening period.
_INSTITUTION_PHRASE_RE = re.compile(
    r"\b((?:[A-Z][\w&'-]*\s+){0,6}(?:Museum|Galler\w*|Librar\w*|Archive\w*|University|Institut\w*|"
    r"Foundation|Academy|Society)(?:\s+(?:of\s+)?[A-Z][\w&'-]*(?:\s+[A-Z][\w&'-]*){0,3})?)\b"
)
# Round 13: a bare single generic word ("Gallery", "Museum", "Collection"...) names no institution at
# all — reject it outright rather than shipping it as current_repository (Hay Wain: "Gallery" was really
# just the substring "Gallery" inside a gallerix.ru URL slug "National-Gallery-London-4"; Museum
# Barberini used to truncate to "Museum" before the optional-"of" fix above).
_GENERIC_SINGLE_WORD_INSTITUTIONS = {
    "gallery", "museum", "collection", "archive", "archives", "library", "libraries",
    "foundation", "academy", "society", "institute", "university", "center", "centre",
}
# Round 13: a credit that names a PUBLISHER/catalogue/exhibition rather than the holding institution
# ("published by the...", "A Catalogue Raisonné", "exhibition catalogue") — reject a phrase whose
# immediately preceding text reads that way, so we don't ship the publisher as current_repository.
_PUBLISHER_CONTEXT_RE = re.compile(
    r"\b(published by|publisher|catalogue raisonn\w*|catalog raisonn\w*|exhibition catalog\w*)\b", re.I,
)
# Round 13: "X via somesite.com" / "...photo collection at somesite.com" is a photo-agency/aggregator
# credit line ("Erich Lessing Culture and Fine Arts Archives via artsy.net"; "Vatican Museum Complete
# indexed photo collection at WorldHistoryPics.com") — the named phrase is a photo credit, never the
# holding institution, however institution-shaped it reads (Liberty Leading the People: "Fine Arts
# Archives").
_VIA_AGGREGATOR_RE = re.compile(
    r"\b(?:via|(?:photo\s+)?collection at)\s+[\w.-]+\.(?:com|net|org|ru|de|fr|uk)\b", re.I,
)
_BARE_URL_RE = re.compile(r"https?://\S+")
# Round 13: "Scanned from <author>: <title>, <Museum>, <year>, ISBN ..." — a book citation, where the
# named institution is the CATALOGUE's publisher, not the work's holder (The Beguiling of Merlin: "...
# Metropolitan Museum of Art, 1998, ISBN 0870998595" — real holder is Lady Lever Art Gallery).
_BOOK_CITATION_RE = re.compile(r"\bISBN\b", re.I)

# Round 8 #1: these are image AGGREGATORS/AGENCIES/ARCHIVES — they license or host a photo of a work,
# they never HOLD it. "Google Cultural Institute" in particular is institution-shaped enough to slip
# past _INSTITUTION_PHRASE_RE/looks_like_institution_label, which is exactly the bug (devils-bridge-
# st-gotthards-pass-0059, luncheon-of-the-boating-party-0020, farmyard-0098, boy-in-flowers…-0144 all
# had it as current_repository instead of the real holder).
_AGGREGATOR_BLOCKLIST_RE = re.compile(
    r"\b(Google (?:Cultural Institute|Art Project)|Bridgeman (?:Art Library|Images)|Wikimedia|"
    r"Wikimedia Commons|\bCommons\b|Flickr|Project Apollo Archive|Art Renewal Center|WikiArt|"
    r"Web Gallery of Art|Yorck Project|AMICA Library|Google Arts)\b", re.I,
)
# Trailing/leading junk a Commons Credit sentence leaves behind after the institution name itself —
# "Library of Congress Catalog", "...See the full record.", "'s Prints and Photographs Division".
_TRAILING_JUNK_RE = re.compile(
    r"(\.\s*See\b.*$|\s+Catalog\w*$|'s\s+Prints?\b.*$|\s+Digital\s+Image\w*$)", re.I,
)
_LEADING_JUNK_RE = re.compile(r"^(drawings?\s+in\s+the\s+|photographs?\s+in\s+the\s+)", re.I)


def is_aggregator_or_agency(name: str) -> bool:
    return bool(name) and bool(_AGGREGATOR_BLOCKLIST_RE.search(name))


def _clean_institution_text(name: str) -> str:
    name = _LEADING_JUNK_RE.sub("", name or "").strip()
    name = _TRAILING_JUNK_RE.sub("", name).strip()
    # Round 13: strip a trailing footnote-reference digit ("Courtauld Gallery1" -> "Courtauld Gallery"),
    # never a digit that's part of the institution's own name (none in this domain do that).
    name = re.sub(r"\d+$", "", name).strip()
    return name


def _extract_institution_phrase(text: str) -> str | None:
    if not text:
        return None
    text = _BARE_URL_RE.sub(" ", text)
    if _VIA_AGGREGATOR_RE.search(text):
        return None  # round 13: whole credit line is a photo-agency/aggregator credit
    if _BOOK_CITATION_RE.search(text):
        return None  # round 13: a book citation names its publisher, not the work's holder
    m = _INSTITUTION_PHRASE_RE.search(text)
    if not m:
        return None
    # Round 13: scope the publisher-context check to the CURRENT sentence only (back to the nearest
    # preceding full stop) — a footnote for something else entirely earlier in the same Credit string
    # ("1. J. Rewald ... A Catalogue Raisonné2. National Gallery of Art, ...") must not veto a real
    # institution named in its own following sentence.
    sentence_start = text.rfind(".", 0, m.start(1)) + 1
    window = text[sentence_start:m.start(1)]
    if _PUBLISHER_CONTEXT_RE.search(window):
        return None  # round 13: names a publisher/catalogue/exhibition, not the holder
    phrase = _clean_institution_text(m.group(1))
    if not phrase or is_aggregator_or_agency(phrase):
        return None
    if len(phrase.split()) == 1 and phrase.lower() in _GENERIC_SINGLE_WORD_INSTITUTIONS:
        return None  # round 13: a bare generic word names no institution
    return phrase


# ----------------------------------------------------------------------- museum-match verification
# Round 4 addendum: a museum-API TITLE SEARCH (Met/Cleveland/AIC by q=title) can return the wrong
# object entirely — mount-washington-0007 (Homer oil painting, 1869) matched AIC's record for
# "Partially gilded and painted blown mold glass" that merely shares words with the title. Because
# museum records sit at the TOP of the structured-field precedence, an unverified one is dangerous.
# Require normalised title similarity AND (a corroborating creator OR an explicit identifier — an
# accession/inventory number pulled off the Commons file itself, not a keyword search).
_TITLE_SIM_THRESHOLD = 0.5


def _title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a or ""), _norm(b or "")).ratio()


def _museum_record_creator_text(record: dict) -> str:
    return (record.get("artistDisplayName") or record.get("artist_display")
            or ", ".join(c for c in (record.get("creators") or []) if c) or "")


def museum_match_verified(item: dict, record: dict, *, explicit_id: bool = False) -> tuple[bool, str]:
    """(ok, reason). `explicit_id=True` for a lookup keyed by an accession/inventory number taken
    directly off the Commons file — those skip the creator-corroboration requirement, but never the
    title check (an explicit id can still be extracted from the wrong Credit line)."""
    sim = _title_similarity(item.get("title") or "", record.get("title") or "")
    if sim < _TITLE_SIM_THRESHOLD:
        return False, f"title similarity {sim:.2f} < {_TITLE_SIM_THRESHOLD} ({item.get('title')!r} vs {record.get('title')!r})"
    if explicit_id:
        return True, "explicit identifier + title match"
    record_creator = _museum_record_creator_text(record)
    cat_tokens, rec_tokens = _name_tokens(item.get("agent_name") or ""), _name_tokens(record_creator)
    if cat_tokens and rec_tokens and (cat_tokens & rec_tokens):
        return True, "title + creator match"
    return False, f"no corroborating creator (catalog {item.get('agent_name')!r} vs record {record_creator!r})"


async def _wikidata_by_accession(fx: Fetcher, accession: str, institution: str) -> str | None:
    """SPARQL: an item whose P217 (inventory number) matches AND whose P195 (collection) label
    contains the institution's first word — both must hold, so a common accession-number shape from a
    different museum can't false-match."""
    keyword = (institution.split() or [""])[0].lower()
    q = (
        'SELECT ?item WHERE { '
        f'?item wdt:P217 "{accession}" . '
        '?item wdt:P195 ?coll . ?coll rdfs:label ?collLabel . FILTER(LANG(?collLabel)="en") '
        f'FILTER(CONTAINS(LCASE(?collLabel), "{keyword}")) }} LIMIT 1'
    )
    body, err = await fx.get_json(WD_SPARQL, {"query": q, "format": "json"})
    rows = ((body or {}).get("results") or {}).get("bindings") or []
    return rows[0]["item"]["value"].rsplit("/", 1)[-1] if rows else None


async def resolve_via_commons_credit(fx: Fetcher, ext: dict) -> dict | None:
    """Returns {institution, accession, qid|None, museum_record|None} or None."""
    text = " ".join(filter(None, [ext.get("Credit", ""), ext.get("ImageDescription", "")]))
    institution, accession = _find_institution_and_accession(text)
    if not institution or not accession:
        return None
    museum_record = None
    if institution == "Cleveland Museum of Art":
        museum_record = await _cleveland_by_accession(fx, accession)
    qid = await _wikidata_by_accession(fx, accession, institution)
    if not museum_record and not qid:
        return None
    return {"institution": institution, "accession": accession, "qid": qid, "museum_record": museum_record}


# ----------------------------------------------------------------------- current_repository validity
# Never a bare unresolved QID, and never a P276 "location" (that property mixes holding institutions
# with depicted/creation PLACES — e.g. "Moon" for an Apollo photo). Only P195 collection labels that
# actually look like an institution are accepted; a museum-API-sourced institution name is trusted
# outright (it came from that institution's own record, not a generic Wikidata place property).
_BARE_QID_RE = re.compile(r"^Q\d+$")
_INSTITUTION_LABEL_RE = re.compile(
    r"\b(museum|gallery|librar|archive|university|institut|foundation|collection|academy|society|"
    r"cent(?:er|re))", re.I
)


def looks_like_institution_label(label: str) -> bool:
    if not label or _BARE_QID_RE.match(label.strip()):
        return False
    if is_aggregator_or_agency(label):
        return False
    return bool(_INSTITUTION_LABEL_RE.search(label))


# In-process label cache, keyed by QID, shared across the whole run — many items share the same
# creator/collection/genre entity, and a full-catalog run repeats the same handful of QIDs thousands
# of times. Populated only via the batched fetch below.
_LABEL_CACHE: dict[str, str] = {}


async def _labels_for_qids(fx: Fetcher, qids: list[str]) -> dict[str, str]:
    """Resolve many QIDs to English labels in as few requests as possible: wbgetentities accepts up
    to 50 pipe-separated ids per call. This is the fix for a real throughput problem found while
    running the full catalog — the original one-id-per-request _label_for_qid made a full-corpus run
    (~2,800 items x up to ~35 label lookups each) collapse under Wikidata's rate limiting."""
    todo = [q for q in dict.fromkeys(qids) if q and q not in _LABEL_CACHE]
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        body, _ = await fx.get_json(WD_API, {
            "action": "wbgetentities", "ids": "|".join(chunk), "props": "labels",
            "languages": "en", "format": "json",
        })
        entities = (body or {}).get("entities") or {}
        for q in chunk:
            ent = entities.get(q) or {}
            lab = ((ent.get("labels") or {}).get("en") or {}).get("value")
            _LABEL_CACHE[q] = lab or q
    return {q: _LABEL_CACHE.get(q, q) for q in qids}


# Round 3 #7: is P144/P1877/P629's target itself a work of art (painting, sculpture, print,
# photograph, …), or a theme/text/character it merely depicts or draws on (Old Testament, "Venus
# Pudica")? Judged by that entity's own P31 (instance of) labels — cheap keyword match, not a QID
# allowlist, so it generalises past the handful of classes anyone bothered to enumerate.
_ARTWORK_CLASS_RE = re.compile(
    r"\b(painting|sculpture|print|photograph|drawing|artwork|work of art|fresco|engraving|etching|"
    r"lithograph|woodcut|woodblock|mosaic|tapestry|illustration|statue|bronze|panel painting|"
    r"altarpiece|relief|bust|manuscript)\b", re.I,
)
_ARTWORK_ENTITY_CACHE: dict[str, bool] = {}


async def _is_artwork_entity(fx: Fetcher, qid: str) -> bool:
    if qid in _ARTWORK_ENTITY_CACHE:
        return _ARTWORK_ENTITY_CACHE[qid]
    body, err = await fx.get_json(WD_API, {
        "action": "wbgetentities", "ids": qid, "props": "claims", "format": "json",
    })
    result = False
    if body:
        ent = (body.get("entities") or {}).get(qid) or {}
        ids = []
        for c in ((ent.get("claims") or {}).get("P31") or [])[:5]:
            v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(v, dict) and v.get("id"):
                ids.append(v["id"])
        if ids:
            labels = await _labels_for_qids(fx, ids)
            result = any(_ARTWORK_CLASS_RE.search(labels.get(i, "") or "") for i in ids)
    _ARTWORK_ENTITY_CACHE[qid] = result
    return result


# ----------------------------------------------------------------------- Wikidata quantity (dimensions)
# Round 6 bug: P2048/P2049 (height/width) were shipped as the RAW Wikidata quantity `amount` string
# ("+1272") with its unit (mm/cm/in/m — a QID on the same datavalue) silently ignored, then blindly
# suffixed " cm" downstream — "+1272 x +1121 cm" on a small watercolour was really 127.2 x 112.1 cm
# (mm) or similar unit confusion. Wikidata's own default unit for P2048/P2049 when none is given is cm.
_WD_LENGTH_UNIT_TO_CM = {
    "Q174789": 0.1,     # millimetre
    "Q174728": 1.0,     # centimetre
    "Q11573": 100.0,    # metre (canonical item; P2048/P2049 usually cite this one)
    "Q7727": 100.0,     # metre (alternate item some data uses)
    "Q218593": 2.54,    # inch
}


def wikidata_quantity_to_cm(dv: dict) -> float | None:
    amount = dv.get("amount")
    if amount is None:
        return None
    try:
        val = float(str(amount).lstrip("+"))
    except ValueError:
        return None
    unit = dv.get("unit") or ""
    qid = unit.rsplit("/", 1)[-1] if unit else None
    factor = _WD_LENGTH_UNIT_TO_CM.get(qid, 1.0)  # unknown/missing unit -> Wikidata's own cm default
    return abs(val) * factor


# ----------------------------------------------------------------------- Wikidata date normalisation
# Round 3 bug: raw ISO dates ("1850-00-00", "1873-01-01") were leaking into date_display. Wikidata
# zero-fills month/day it doesn't actually know ("00"), so the real signal is the claim's own
# `precision` code, not the string shape. P1480 ("circa" qualifier) is honoured as a "c. " prefix.
_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December"]


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_wikidata_date(claim: dict, allow_day_precision: bool = False) -> str | None:
    """precision: 11=day, 10=month, 9=year, 8=decade, 7=century, <=6=millennium+. Day/month precision
    is collapsed to a year UNLESS allow_day_precision (photos/space imagery, where a specific day is
    both plausible and meaningful) — never for a generic artwork."""
    dv = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
    if not isinstance(dv, dict) or "time" not in dv:
        return None
    m = re.match(r"^([+-])(\d+)-(\d{2})-(\d{2})T", dv["time"])
    if not m:
        return None
    sign, y, mo, day = m.groups()
    year = int(y) * (-1 if sign == "-" else 1)
    precision = dv.get("precision", 9)
    circa = bool((claim.get("qualifiers") or {}).get("P1480"))
    prefix = "c. " if circa else ""

    if precision >= 10 and allow_day_precision and mo != "00":
        try:
            if precision >= 11 and day != "00":
                return f"{prefix}{int(day)} {_MONTH_NAMES[int(mo)]} {year}"
            return f"{prefix}{_MONTH_NAMES[int(mo)]} {year}"
        except (ValueError, IndexError):
            pass
    if precision >= 9:
        return f"{prefix}{year}"
    if precision == 8:
        return f"{prefix}{(year // 10) * 10}s"
    if precision == 7:
        century = (year - 1) // 100 + 1 if year > 0 else (year // 100)
        return f"{prefix}{_ordinal(century)} century"
    return f"{prefix}{year}"  # millennium or coarser — best effort


# Round 7 #2: a raw "YYYY-MM-DD" reaching date_display/creation_date (typically an
# existing_catalog_value straight from the pre-reground catalog) gets the same day/year rule as a
# Wikidata claim — a specific day is only meaningful for a photograph or space-imagery item.
_RAW_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _normalize_iso_date_string(value: str, collection: str | None) -> str:
    if not value:
        return value
    m = _RAW_ISO_DATE_RE.match(value.strip())
    if not m:
        return value
    year, month, day = m.groups()
    if catalog_spec.kind_for(collection or "") in ("photo", "space") and month != "00" and day != "00":
        try:
            return f"{int(day)} {_MONTH_NAMES[int(month)]} {int(year)}"
        except (ValueError, IndexError):
            pass
    return str(int(year))


async def _wikidata_full(fx: Fetcher, qid: str, allow_day_precision: bool = False) -> dict:
    body, err = await fx.get_json(WD_API, {
        "action": "wbgetentities", "ids": qid, "props": "claims|sitelinks", "languages": "en", "format": "json",
    })
    if not body:
        return {"fetch_error": err}
    ent = (body.get("entities") or {}).get(qid) or {}
    claims = ent.get("claims") or {}

    # First pass: collect every QID this item's claims reference (creator, collection, genre, …) so
    # they can be resolved to labels in one or two batched calls instead of one-per-value.
    need_ids: list[str] = []
    for prop in list(CLAIM_PROPS) + list(VERSION_PROPS):
        if prop == "P571":
            continue  # date-valued, not QID-valued — handled separately below
        for c in (claims.get(prop) or [])[:5]:
            v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(v, dict) and v.get("id"):
                need_ids.append(v["id"])
    labels = await _labels_for_qids(fx, need_ids) if need_ids else {}

    def _resolved_label(qid_val: str) -> str | None:
        """The batched label, or None if it never resolved to an English label — round 3: a fact must
        never ship a bare, unresolved QID to a writer."""
        lab = labels.get(qid_val)
        if not lab or _BARE_QID_RE.match(lab.strip()):
            return None
        return lab

    out = {}
    for prop, key in CLAIM_PROPS.items():
        vals = claims.get(prop) or []
        vlabels = []
        for c in vals[:5]:
            dv = c.get("mainsnak", {}).get("datavalue", {})
            v = dv.get("value")
            if prop == "P571":
                formatted = format_wikidata_date(c, allow_day_precision)
                if formatted:
                    vlabels.append(formatted)
            elif prop in ("P2048", "P2049") and isinstance(v, dict) and "amount" in v:
                cm = wikidata_quantity_to_cm(v)
                if cm is not None:
                    vlabels.append(f"{cm:.1f}")
            elif isinstance(v, dict) and v.get("id"):
                lab = _resolved_label(v["id"])
                if lab:
                    vlabels.append(lab)
            elif isinstance(v, dict) and "amount" in v:
                vlabels.append(v["amount"])
            elif isinstance(v, str):
                vlabels.append(v)
        if vlabels:
            out[key] = vlabels
    creator_qid = None
    for c in (claims.get("P170") or [])[:1]:
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and v.get("id"):
            creator_qid = v["id"]
    version = None
    based_on_theme = None
    for prop, relation in VERSION_PROPS.items():
        vals = claims.get(prop) or []
        if not vals:
            continue
        dv = vals[0].get("mainsnak", {}).get("datavalue", {}).get("value", {})
        if isinstance(dv, dict) and dv.get("id"):
            lab = _resolved_label(dv["id"])
            if lab:  # never ship an unresolved QID as a "version of" target either
                # Round 3 #7: P144/P1877/P629 often point at a THEME, not another physical artwork
                # (Old Testament, "Venus Pudica", Book of Judith) — only call it is_version_of when the
                # target is itself an instance/subclass of some kind of artwork.
                if await _is_artwork_entity(fx, dv["id"]):
                    version = {"relation": relation, "of_qid": dv["id"], "of_label": lab, "property": prop}
                else:
                    based_on_theme = {"relation": relation, "of_qid": dv["id"], "of_label": lab, "property": prop}
            break
    sitelinks = ent.get("sitelinks") or {}
    return {
        "claims": out, "enwiki_title": sitelinks.get("enwiki", {}).get("title"),
        "is_version_of": version, "based_on_theme": based_on_theme, "creator_qid": creator_qid,
    }


# ----------------------------------------------------------------------- extra facts (--extra-facts DIR)
# Cosmos rebuild 2026-09-27: 88 NASA/STScI/JPL/Chandra works gained their official release text (US
# government work -> public domain). Unlike the check-only CC BY-SA Commons/Wikipedia text elsewhere in
# this module, PDM-1.0 text is a directly-quotable/paraphrasable FACT, so it rides the normal facts
# bundle (nasa.release_text[.N] / nasa.release_url / nasa.credit) straight into the packet.
_NASA_RELEASE_CHUNK_MAX = 1200


def _split_release_text(text: str, max_len: int = _NASA_RELEASE_CHUNK_MAX) -> list[str]:
    """Whitespace-normalize, then split into <= max_len chunks at sentence boundaries (never mid-
    sentence) so nasa.release_text(.N) facts each read as complete prose."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_len:
        return [text] if text else []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    cur = ""
    for sent in sentences:
        candidate = f"{cur} {sent}".strip() if cur else sent
        if len(candidate) > max_len and cur:
            chunks.append(cur)
            cur = sent
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


# NASA Photojournal (photojournal.jpl.nasa.gov/catalog/PIAnnnnn) is a JS-rendered SPA: a plain fetch
# captures the site shell -- welcome banner, "related images" cards, footer -- not the release text
# (2026-10-03: the SDO Sun's "text" was 2.5k chars of that). A model must never see it as a source.
_BOILERPLATE_PHRASES = (
    "welcome you to photojournal", "welcome to the new photojournal", "welcome to photojournal",
    "stay up-to-date with the latest additions", "discover more topics from photojournal",
    "skip to main content", "enable javascript", "javascript is required", "javascript is disabled",
    "we use cookies", "accept all cookies",
)
_RELATED_CARD_RE = re.compile(r"(?m)^Description\s+\S")


def boilerplate_reason(text: str | None) -> str | None:
    """A clear reason when `text` is scraped site boilerplate rather than release text, else None.
    Two independent signals: known shell phrases, or a run of "Description ..." related-image cards."""
    t = re.sub(r"\s+", " ", text or "").lower()
    for phrase in _BOILERPLATE_PHRASES:
        if phrase in t:
            return f"site boilerplate (matched {phrase!r}) - page was JS-rendered, no release text captured"
    n_cards = len(_RELATED_CARD_RE.findall(text or ""))
    if n_cards >= 2:
        return (f"site boilerplate ({n_cards} related-image 'Description ...' cards) - "
                f"page was JS-rendered, no release text captured")
    return None


def load_extra_facts(path: str | Path | None, rejected: list | None = None) -> list[dict]:
    """--extra-facts DIR: one {n, title, release_url, text, credit_line} JSON per file. Order doesn't
    matter — matching (below) is keyed off release_url/title, not filename. Records whose text is scraped
    site boilerplate (see boilerplate_reason) are dropped HERE, before any packet exists; each is appended
    to `rejected` (when given) as {n, title, release_url, reason}."""
    if not path:
        return []
    d = Path(path).expanduser()
    if not d.is_dir():
        return []
    out = []
    for fp in sorted(d.glob("*.json")):
        try:
            data = json.loads(fp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("release_url") and data.get("text"):
            reason = boilerplate_reason(data.get("text"))
            if reason:
                logger.info(f"    · extra-facts {fp.name} refused: {reason}")
                if rejected is not None:
                    rejected.append({"n": data.get("n"), "title": data.get("title"),
                                     "release_url": data.get("release_url"), "reason": reason})
                continue
            out.append(data)
    return out


def index_extra_facts_by_catalog(extra_facts: list[dict], catalog_items: list[dict]) -> dict[int, dict]:
    """Match each extra-facts record to a catalog row's index: release_url == the row's
    attribution_url, falling back to normalized title. Returns {idx: extra_fact_record}."""
    by_url: dict[str, int] = {}
    by_title: dict[str, int] = {}
    for idx, item in enumerate(catalog_items):
        url = (item.get("attribution_url") or "").strip()
        if url:
            by_url.setdefault(url, idx)
        title = _norm(item.get("title") or "")
        if title:
            by_title.setdefault(title, idx)
    matched: dict[int, dict] = {}
    for rec in extra_facts:
        url = (rec.get("release_url") or "").strip()
        idx = by_url.get(url)
        if idx is None:
            idx = by_title.get(_norm(rec.get("title") or ""))
        if idx is not None:
            matched[idx] = rec
    return matched


_FREELY_QUOTABLE_LICENCES = {"PDM-1.0", "CC0-1.0"}
_COPY_RUN_WORDS = 8   # a narrative may not reproduce this many consecutive words of a paraphrase-only fact


_ESA_HOSTS = ("esahubble.org", "esawebb.org", "esa.int")


def _is_esa_release_url(url: str | None) -> bool:
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _ESA_HOSTS)


def _extra_fact_licence(rec: dict) -> tuple[str, bool]:
    """(licence id/text for the fact, paraphrase_only). A record without a licence is the original NASA/
    STScI case (PDM-1.0, quotable). PD/CC0 stay quotable. EVERYTHING else -- CC BY 4.0 (ADR-142: attribution
    + paraphrase, never copied wording), BY-SA/NC, or an unrecognised string -- is paraphrase-only: the
    pipeline only has "quotable" vs "not", so the safe mapping for anything not provably PD/CC0 is the latter."""
    raw = (rec.get("licence") or rec.get("license") or "").strip()
    if not raw:
        if _is_esa_release_url(rec.get("release_url")):   # real ESA records carry no licence field
            return "CC-BY-4.0", True
        return "PDM-1.0", False
    norm = normalize_license(raw)
    if norm in _FREELY_QUOTABLE_LICENCES:
        return norm, False
    return (norm or raw), True


def _extra_facts_for_item(rec: dict | None) -> list[dict]:
    """nasa.release_text[.N] / nasa.release_url / nasa.credit for one matched extra-facts record.
    Licence + source label come from the record (`licence`/`license`, `source_label`/`source`); absent ->
    PDM-1.0 / "NASA release". PD/CC0 text is a normal fact. Anything else (ESA CC BY 4.0, ...) is still a
    citable fact but flagged paraphrase_only, which the facts block shows the writer and
    validate_written_item enforces. The `nasa.*` keys are kept: clean_credits keys on nasa.credit."""
    if not rec or boilerplate_reason(rec.get("text")):
        return []
    licence, paraphrase_only = _extra_fact_licence(rec)
    label = (rec.get("source_label") or rec.get("source") or "").strip()
    if not label:
        label = ("ESA release" if _is_esa_release_url(rec.get("release_url")) else
                 "Agency release" if paraphrase_only else "NASA release")
    facts: list[dict] = []
    url = rec.get("release_url") or ""
    chunks = _split_release_text(rec.get("text") or "")
    if len(chunks) == 1:
        facts.append(_fact("nasa.release_text", chunks[0], label, url, licence))
    else:
        for i, chunk in enumerate(chunks, 1):
            facts.append(_fact(f"nasa.release_text.{i}", chunk, label, url, licence))
    if url:
        facts.append(_fact("nasa.release_url", url, label, url, licence))
    if rec.get("credit_line"):
        facts.append(_fact("nasa.credit", rec["credit_line"], label, url, licence))
    if paraphrase_only:
        for f in facts:
            if f["key"].startswith("nasa.release_text"):
                f["paraphrase_only"] = True
    return facts


# ----------------------------------------------------------------------- facts bundle
_MUSEUM_FACT_KEYS = ("objectDate", "medium", "culture", "classification", "creation_date",
                     "technique", "description", "did_you_know", "dimensions")


async def _apply_commons_structured_override(fx: Fetcher, item: dict, match: dict | None) -> dict | None:
    """Round 8 addendum (a): the Commons FILE's own structured data (P6243 digital-representation-of /
    P180 depicts) names a QID directly — that beats a search/title match every time, since a search
    can land on a plausible-sounding but wrong item (house-in-provence-0130 matched a Barnes painting
    by title while the file's own structured data links the real Indianapolis one). Only overrides a
    non-high-confidence match — a P18-exact match already IS this same signal."""
    source_url = item.get("source_url") or ""
    if not match or match.get("confidence") == "high" or "commons.wikimedia.org" not in source_url:
        return match
    filename = urllib.parse.unquote(urllib.parse.urlparse(source_url).path.rsplit("/", 1)[-1])
    structured_qid, note = await _commons_structured(fx, filename)
    if structured_qid and structured_qid != match["qid"]:
        return {"qid": structured_qid, "method": f"commons structured data overrides search match ({note})",
                "confidence": "high"}
    return match


async def build_facts_bundle(fx: Fetcher, item: dict, collection: str | None = None,
                              extra_fact: dict | None = None) -> dict:
    """Resolve identity + assemble the typed facts bundle for one catalog item. `extra_fact` is an
    optional matched --extra-facts record (see index_extra_facts_by_catalog) folded in as nasa.*
    facts."""
    allow_day_precision = catalog_spec.kind_for(collection or "") in ("photo", "space")
    match = await resolve_work(fx, item)
    match = await _apply_commons_structured_override(fx, item, match)
    facts: list[dict] = []
    check_only: list[str] = []
    conflicts: list[dict] = []
    is_version_of = None
    wikipedia_lead = None
    commons_description = None
    creator_qid = None
    creator_death_year = None

    if match:
        wd = await _wikidata_full(fx, match["qid"], allow_day_precision)
        claims = wd.get("claims") or {}
        wd_url = f"https://www.wikidata.org/wiki/{match['qid']}"
        for key, labels in claims.items():
            facts.append(_fact(f"wikidata.{key}", labels, "Wikidata", wd_url, "CC0"))
        is_version_of = wd.get("is_version_of")
        creator_qid = wd.get("creator_qid")
        # Round 7: computed once, up front, so the "does this date land after the artist died"
        # guard is available for ANY date_display (not just commons.DateTimeOriginal) — including an
        # existing_catalog_value fallback that's itself a leaked upload/scan date.
        if creator_qid:
            creator_death_year = await _creator_death_year(fx, creator_qid)
        if wd.get("based_on_theme"):
            bot = wd["based_on_theme"]
            facts.append(_fact("wikidata.based_on_theme", bot["of_label"], "Wikidata", wd_url, "CC0"))
        # Round 3 #8: a Wikipedia lead is only trustworthy off a HIGH-confidence identity match — the
        # search+creator-verify fallback (confidence "medium") landed on a wrong QID for at least one
        # sampled work and pulled in an unrelated article's lead as check-only text.
        if wd.get("enwiki_title") and match.get("confidence") == "high":
            lead = await _wikipedia_lead(fx, wd["enwiki_title"])
            if lead:
                check_only.append(lead)
                wikipedia_lead = lead

    if creator_death_year is None and item.get("agent_name"):
        # The WORK itself may have no confident match at all (so no creator_qid) while the catalog's
        # agent_name is still a real, identifiable artist — "Farmyard in Normandy" never matched, so
        # the date-after-death guard below would otherwise never see Monet's 1926 death year and would
        # wave through its bogus "2024-04-10" catalog date.
        creator_death_year = await _death_year_by_artist_name(fx, item["agent_name"])

    museum = await _museum_record(fx, item)
    if museum:
        ok, reason = museum_match_verified(item, museum)
        if not ok:
            logger.info(f"    · museum title-search match rejected for {item.get('title')!r}: {reason}")
            museum = None
    # Round 3 #5: museums' own CC0 curatorial text, where the API provides it. Cleveland's search
    # response already carries `description`/`did_you_know` (only trustworthy when that record's own
    # `share_license_status` is CC0 — some Cleveland records are not).
    if museum and item.get("source") == "Cleveland Museum of Art":
        extra = await _cleveland_extra_by_title(fx, item.get("title") or "")
        if extra:
            museum.update(extra)
    # AIC isn't covered by audit_placards._museum_record; add it here in the SAME shape so it flows
    # through the one fact-emission loop below. Its `description`/`short_description` fields are CC BY
    # 4.0 per AIC's own license_text (NOT CC0 like the rest of the record) — kept check-only, never a
    # directly-quotable fact.
    if not museum and item.get("source") == "Art Institute of Chicago":
        aic = await _aic_record(fx, item.get("title") or "")
        if aic:
            ok, reason = museum_match_verified(item, aic)
            if ok:
                museum = aic
                for txt in museum.pop("_checkonly", []):
                    check_only.append(txt)
            else:
                logger.info(f"    · AIC title-search match rejected for {item.get('title')!r}: {reason}")
    if museum:
        for key in _MUSEUM_FACT_KEYS:
            if museum.get(key):
                facts.append(_fact(f"museum.{key}", museum[key], museum["api"], museum["url"], "CC0"))
        for key in ("creators",):
            if museum.get(key):
                facts.append(_fact(f"museum.{key}", museum[key], museum["api"], museum["url"], "CC0"))
        # source-keyed lookup only fires when item.source IS the institution's exact name — in that
        # case it's trustworthy as the current repository outright.
        if item.get("source"):
            facts.append(_fact("museum.institution", item["source"], museum["api"], museum["url"], "CC0"))

    source_url = item.get("source_url") or ""
    ext = None
    commons_upload_year = None
    if "commons.wikimedia.org" in source_url:
        filename = urllib.parse.unquote(urllib.parse.urlparse(source_url).path.rsplit("/", 1)[-1])
        ext = await _commons_extmetadata(fx, filename)
        if ext:
            commons_upload_year = _extract_year(ext.get("DateTime") or "")
            for key, val in ext.items():
                if key == "ImageDescription":
                    check_only.append(val)
                    commons_description = val
                    continue
                if key == "DateTimeOriginal":
                    if looks_like_capture_timestamp(val, commons_upload_year, creator_death_year):
                        continue  # round 3 #2: drop — reads as a scan/upload timestamp, not a date
                    if looks_like_garbled_date(val):
                        continue  # round 3 #9: drop — not a clean date expression
                facts.append(_fact(f"commons.{key}", val, "Wikimedia Commons", source_url, "CC BY-SA"))
            # round 3 #3: a generic institution name in Credit/Institution text, when nothing more
            # authoritative names one — lowest precedence, applied in resolve_structured_fields.
            phrase = _extract_institution_phrase(" ".join(filter(None, [ext.get("Institution"), ext.get("Credit")])))
            if phrase:
                facts.append(_fact("commons.institution_credit", phrase, "Wikimedia Commons", source_url, "CC BY-SA"))

    # Round 2: when the item is Commons-sourced, also try the file's own Credit/accession to find the
    # museum's own record and/or a stronger identity match — fixes the "matched via P18 but its own
    # museum's medium/date were never fetched" gap (n=45: hanging-scroll vs. Cleveland's handscroll).
    if ext:
        credit_hit = await resolve_via_commons_credit(fx, ext)
        if credit_hit:
            mr = credit_hit.get("museum_record")
            if mr:
                ok, reason = museum_match_verified(item, mr, explicit_id=True)
                if not ok:
                    logger.info(f"    · accession-based museum match rejected for {item.get('title')!r}: {reason}")
                    mr = None
                    credit_hit["museum_record"] = None
            if mr:
                for key in _MUSEUM_FACT_KEYS:
                    if mr.get(key):
                        facts.append(_fact(f"museum2.{key}", mr[key], mr["api"], mr["url"], "CC0"))
                facts.append(_fact("museum2.institution", credit_hit["institution"], mr["api"], mr["url"], "CC0"))
            if credit_hit.get("qid") and not match:
                match = {
                    "qid": credit_hit["qid"],
                    "method": "commons credit institution+accession -> Wikidata P217/P195",
                    "confidence": "high",
                }
                wd = await _wikidata_full(fx, match["qid"], allow_day_precision)
                claims = wd.get("claims") or {}
                wd_url = f"https://www.wikidata.org/wiki/{match['qid']}"
                for key, labels in claims.items():
                    if not any(f["key"] == f"wikidata.{key}" for f in facts):
                        facts.append(_fact(f"wikidata.{key}", labels, "Wikidata", wd_url, "CC0"))
                is_version_of = is_version_of or wd.get("is_version_of")
                if wd.get("based_on_theme") and not any(f["key"] == "wikidata.based_on_theme" for f in facts):
                    facts.append(_fact("wikidata.based_on_theme", wd["based_on_theme"]["of_label"],
                                        "Wikidata", wd_url, "CC0"))

    facts.extend(_extra_facts_for_item(extra_fact))

    facts, conflicts = _detect_conflicts(facts)
    return {
        "match": match, "facts": facts, "conflicts": conflicts,
        "is_version_of": is_version_of, "check_only_texts": check_only,
        "wikipedia_lead": wikipedia_lead, "commons_description": commons_description,
        "creator_death_year": creator_death_year, "commons_upload_year": commons_upload_year,
    }


def _detect_conflicts(facts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Flag when two sources disagree on medium class or date by >25 yrs. Facts themselves are kept
    as-is (conflict resolution happens in resolve_structured_fields, not here)."""
    conflicts = []
    mediums = [f for f in facts if f["key"] in
               ("museum.medium", "museum2.medium", "commons.Medium", "wikidata.made_from_material")]
    buckets = {medium_bucket(" ".join(f["value"]) if isinstance(f["value"], list) else str(f["value"])) for f in mediums}
    buckets.discard(None)
    if len(buckets) > 1:
        conflicts.append({"field": "medium", "detail": f"conflicting medium classes: {sorted(buckets)}"})

    years = []
    date_keys = ("museum.objectDate", "museum.creation_date", "museum2.objectDate", "museum2.creation_date",
                 "wikidata.inception", "commons.DateTimeOriginal")
    for f in facts:
        if f["key"] in date_keys:
            v = f["value"][0] if isinstance(f["value"], list) else f["value"]
            y = _extract_year(str(v))
            if y and not looks_like_accession_number(str(v)):
                years.append((y, f["key"]))
    if years:
        span = max(y for y, _ in years) - min(y for y, _ in years)
        if span > 25:
            conflicts.append({"field": "date", "detail": f"date spread {span} yrs across {[k for _, k in years]}"})
    return facts, conflicts


_YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-2][0-9])\b")


def _extract_year(text: str) -> int | None:
    m = _YEAR_RE.search(text or "")
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------- identity mismatch (catalog integrity)
# Round 3 #10: catch a work whose IMAGE plausibly doesn't match its catalog title/artist — Commons'
# own ObjectName/Artist/Credit naming a different creator than the catalog (judith-i-0007: catalog
# Klimt, Commons says Jacopo Amigoni), or a print/engraving "after" a painter rather than the painting
# itself (shipwreck-0002: "William Miller after Turner"; battle-of-trafalgar-0020: Turner in the
# catalog, LOC calls it a "Popular Graphic Arts" engraving). This is a red flag for a human, not
# something the pipeline corrects on its own — surfaced via needs_review + a standalone report.
_AFTER_ARTIST_RE = re.compile(r"\bafter\s+[A-Z][\w.'’-]+(?:\s+[A-Z][\w.'’-]+)*")
_PRINT_MEDIUM_WORDS_RE = re.compile(
    r"\b(engraving|engraved|print|lithograph\w*|etching|woodcut|woodblock)\b", re.I,
)

# Round 8 addendum (e): Commons/museum text may qualify a bare master's name — "School of Raphael",
# "Workshop of Rembrandt", or (a Staatliche Museen zu Berlin convention) "Schule, Raffael" — meaning
# the actual attribution is weaker than the catalog's bare "Raphael"/"Rembrandt". Detected only to
# REFINE an already-agreeing name, never to introduce a name the catalog didn't already have.
# Round 13 (C): added "after"/"possibly" — "After Peter Paul Rubens", "After Raphael" (catalog title
# says "after Titian"), "Possibly Frans Hals" are the same kind of weakened attribution.
# Round 13b: the qualifier keyword is matched case-insensitively but the CAPTURED name never was — a
# bare `re.I` flag on the whole pattern makes `[A-Z]` match a lowercase letter too, so "After the bath
# by Edgar Degas" (a Commons FILE TITLE that happens to start with "After", not an attribution at all)
# captured "the bath by Edgar Degas" as if it were a qualified name. `(?i:...)` scopes case-insensitivity
# to the keyword alternation only; the capture group stays a real, case-sensitive proper-noun run.
_ATTRIBUTION_QUALIFIER_RE = re.compile(
    r"(?i:\b(school of|workshop of|circle of|studio of|attributed to|follower of|manner of|after|possibly))\s+"
    r"([A-Z][\w.’'\-]+(?:\s+[A-Z][\w.’'\-]+)*)"
)
_SCHULE_QUALIFIER_RE = re.compile(r"(?i:\bSchule),\s*([A-Z][\w.’'\-]+(?:\s+[A-Z][\w.’'\-]+)*)")


_NAME_PARTICLES = {"van", "der", "de", "von", "di", "le", "la", "den", "ter", "y", "af"}


def _name_tokens(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", (name or "").lower()) if w not in _NAME_PARTICLES}


def _names_plausibly_match(a: str, b: str) -> bool:
    """True unless neither name shares a single meaningful token with the other — tolerant of a
    mononym (Rembrandt van Rijn ~ "Rembrandt"), initials, and particle differences."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return True  # nothing to compare — never flag on absence
    return bool(ta & tb)


_QUALIFIER_PREFIX_STRIP_RE = re.compile(
    r"^(school of|workshop of|circle of|studio of|attributed to|follower of|manner of|after|possibly|"
    r"the|sir|dr\.?|mr\.?|mrs\.?)\s+",
    re.I,
)
_GENERATION_SUFFIX_RE = re.compile(r"\s+the\s+(elder|younger)\s*$", re.I)


def _is_different_person_same_surname(a: str, b: str) -> bool:
    """Round 13 (C): "Jan Hals" vs "Frans Hals", "Joan" vs "Willem Blaeu", "Henri-Joseph" vs
    "Pierre-Joseph Redouté" all share a surname (and sometimes a middle name) token, so plain
    `_names_plausibly_match` calls them a match — but they are different, real people, and must be
    flagged for manual review rather than silently confirmed/replaced. Detected only when both names
    give a real (non-initial) FIRST given-name token and those first tokens differ, with the surname
    (last token) agreeing — a hyphenated given name ("Henri-Joseph") tokenises to two words, so its
    first word is what's compared. A qualifier prefix ("Attributed to Winslow Homer") or "the Elder"/
    "the Younger" generation suffix is stripped first so it's never mistaken for a given name/surname;
    a multi-person credit string ("Jan Brueghel and Peter Paul Rubens", "NASA, ESA, ...") is skipped
    entirely — this check is only meaningful one identity at a time."""
    def _strip_qualifiers(s):
        s = _QUALIFIER_PREFIX_STRIP_RE.sub("", (s or "").strip())
        return _GENERATION_SUFFIX_RE.sub("", s)

    a, b = _strip_qualifiers(a), _strip_qualifiers(b)
    if re.search(r"\band\b|,", a, re.I) or re.search(r"\band\b|,", b, re.I):
        return False
    # `_norm` (module-level, from tools.audit_placards) folds accents/case so a pure spelling variant
    # ("Elisabeth Vigee Le Brun" vs "Élisabeth Vigée Le Brun") is never mistaken for a different
    # person — but it DELETES (not replaces) a hyphen, which would otherwise fuse "Jean-Baptiste" into
    # one token unlike the same name written "Jean Baptiste", so hyphens become spaces first.
    ta = [w for w in _norm(a.replace("-", " ")).split() if w not in _NAME_PARTICLES]
    tb = [w for w in _norm(b.replace("-", " ")).split() if w not in _NAME_PARTICLES]
    if len(ta) < 2 or len(tb) < 2:
        return False  # a mononym gives nothing to disagree on
    if ta[-1] != tb[-1]:
        return False  # different surname entirely — that's the ordinary disagreement path
    first_a, first_b = ta[0], tb[0]
    if len(first_a) <= 1 or len(first_b) <= 1:
        return False  # an initial is compatible with any given name
    given_a, given_b = {w.lower() for w in ta[:-1]}, {w.lower() for w in tb[:-1]}
    if first_a in given_b or first_b in given_a:
        return False  # a full birth name vs. common name ("Hilaire Germain Edgar Degas" ~ "Edgar
        # Degas") — one given name is a subset of the other's, not a different person
    return first_a != first_b


# Round 13b (item 2): a candidate agent_name pulled off museum.creators/wikidata.creator must never
# ship if it's obviously not a clean personal/organisation name — empty, a URL, a newline, an HTML
# entity remnant ("amp"), a photo-credit/caption fragment ("Drawn from nature", "Engraved"), or a
# library-catalog "Surname, Given" citation LIST (2+ such pairs chained together — a single "Surname,
# Given" is tolerated, since that's just one person's name in inverted form, but a run of them is a
# citation list masquerading as one string, e.g. "Cellarius, Andreas, Schenk, Peter, Valck, G. (Gerard),
# and Loon, J. Van"). In every case the caller keeps the existing catalog value instead.
_BAD_AGENT_NAME_RE = re.compile(
    r"https?://|\n|\bamp\b|drawn from nature|\bengraved\b", re.I,
)
_CITATION_PAIR_RE = re.compile(r"[A-Za-z'\-]+,\s*[A-Z]")


def _agent_name_is_suspect(name: str) -> bool:
    if not name or not name.strip():
        return True
    if _BAD_AGENT_NAME_RE.search(name):
        return True
    return len(_CITATION_PAIR_RE.findall(name)) >= 2


# ADR-142 / ADR-145: a CC BY 4.0 row's credit is EXACT as the licensor gave it, never rewritten. These are
# the fields that carry that attribution; every rewrite path below (the space-agency credit cleanup, header
# hygiene, land's reground-wins merge) must leave them verbatim on a CC BY row. PD/CC0 rows are unaffected.
# A wrong CC BY credit is corrected in the served row / staging, never through the pipeline.
CCBY_VERBATIM_FIELDS = ("credit_line", "agent_name", "license", "license_url", "license_basis",
                        "license_verdict", "license_verified", "attribution_url")


def is_cc_by_row(item: dict | None) -> bool:
    return requires_attribution(normalize_license((item or {}).get("license")))


def restore_cc_by_fields(item: dict, original: dict) -> list[str]:
    """If `original` is a CC BY row, put its CCBY_VERBATIM_FIELDS back into `item` (in place) and return
    the names that had been changed (empty when nothing was disturbed or the row isn't CC BY)."""
    if not is_cc_by_row(original):
        return []
    changed = []
    for f in CCBY_VERBATIM_FIELDS:
        if f in original:
            if item.get(f) != original[f]:
                changed.append(f)
            item[f] = original[f]
        elif f in item:
            changed.append(f)
            del item[f]
    return changed


# Round 13b (item 3): known space-agency full names/acronyms, matched in this order so the acronym list
# comes out in the credit's own first-occurrence order (never invented — only agencies actually named).
_SPACE_AGENCY_PATTERNS = [
    (re.compile(r"national aeronautics and space administration|\bnasa\b", re.I), "NASA"),
    (re.compile(r"european space agency|\besa\b", re.I), "ESA"),
    (re.compile(r"canadian space agency|\bcsa\b", re.I), "CSA"),
    (re.compile(r"space telescope science institute|\bstsci\b", re.I), "STScI"),
]


def _clean_space_agency_credit(text: str) -> str | None:
    """Returns a clean ", "-joined acronym list (first-occurrence order, deduplicated) when at least
    TWO distinct known space agencies are recognisable in `text`, else None — a single incidental
    acronym mention isn't treated as a "multi-agency credit" worth rewriting."""
    if not text:
        return None
    hits: list[tuple[int, str]] = []
    for pattern, acronym in _SPACE_AGENCY_PATTERNS:
        m = pattern.search(text)
        if m:
            hits.append((m.start(), acronym))
    if len(hits) < 2:
        return None
    hits.sort(key=lambda h: h[0])
    ordered: list[str] = []
    for _, acronym in hits:
        if acronym not in ordered:
            ordered.append(acronym)
    return ", ".join(ordered) if len(ordered) >= 2 else None


def detect_identity_mismatch(item: dict, facts: list[dict]) -> dict | None:
    facts_by_key = {f["key"]: f for f in facts}
    catalog_artist = item.get("agent_name") or ""
    evidence: list[str] = []

    def val_of(key):
        f = facts_by_key.get(key)
        if not f:
            return None
        v = f["value"]
        return str(v[0] if isinstance(v, list) else v)

    # Wikidata's creator only — commons.Artist is excluded here because it routinely names the
    # PHOTOGRAPHER of a 2D reproduction rather than the original painter (a well-known Commons
    # metadata quirk), which would otherwise false-positive constantly on well-attributed works.
    wd_creator = val_of("wikidata.creator")
    if wd_creator and catalog_artist and not _names_plausibly_match(catalog_artist, wd_creator):
        evidence.append(f"wikidata.creator={wd_creator!r} names a different creator than catalog "
                         f"agent_name={catalog_artist!r}")

    for label in ("commons.ObjectName", "commons.Credit", "commons.Artist"):
        text = val_of(label) or ""
        if not text:
            continue
        m = _AFTER_ARTIST_RE.search(text)
        if m:
            evidence.append(f"{label}={text!r} says {m.group(0)!r} — may be a reproduction/print "
                             f"after another artist's work, not the original")
        pm = _PRINT_MEDIUM_WORDS_RE.search(text)
        if pm and "oil" in (item.get("medium") or "").lower():
            evidence.append(f"{label}={text!r} names a print technique ({pm.group(0)!r}) but "
                             f"catalog medium is {item.get('medium')!r}")

    return {"evidence": evidence} if evidence else None


# Round 13 (B): collections where a work is routinely a PRINT with many separate impressions in many
# collections — Audubon plates (Pittsburgh scans), ukiyo-e (NDL/BnF/LOC scans), posters, illustration —
# plus any work whose medium itself names a print technique. Wikidata's collection/dimensions for these
# belong to whichever impression Wikidata's item happens to describe, not necessarily the SCANNED
# EXEMPLAR the placard is about ("now held by the Vanderbilt Museum of Art" over a Pittsburgh scan).
_MULTI_IMPRESSION_COLLECTIONS = {
    "audubon-birds-of-america", "ukiyo-e", "vintage-posters", "golden-age-illustration",
}
_PRINT_TECHNIQUE_RE = re.compile(
    r"\b(engraving|engraved|etching|etched|aquatint|lithograph\w*|woodblock\w*|woodcut\w*|print)\b", re.I,
)
# A print's exemplar date may legitimately differ a little from Wikidata's inception (edition years,
# posthumous printings) — only a >5yr gap is treated as a real disagreement (tighter than the general
# 25yr conflict threshold, because print exemplars/impressions vary far more than paintings do).
_PRINT_DATE_DISAGREEMENT_YEARS = 5


def _is_multi_impression_work(collection: str | None, medium: str | None) -> bool:
    if collection in _MULTI_IMPRESSION_COLLECTIONS:
        return True
    return bool(_PRINT_TECHNIQUE_RE.search(medium or ""))


# Collections where a genuinely large object is expected (a building, a monument, a wall map) — the
# physical_dimensions plausibility guard (round 6) doesn't apply here.
_LARGE_OBJECT_COLLECTIONS = {"cartography", "sculpture-antiquity", "cities-architecture", "ancient-egypt"}
_DIMENSION_MIN_CM, _DIMENSION_MAX_CM = 0.5, 1500.0
# Round 8 #3: a painting a few centimetres a side is implausible regardless of the general floor
# (garden-at-les-lauves-0157 "2.6 x 3.2 cm" — that's a thumbnail crop's dimensions, not the canvas).
_PAINTING_LIKE_BUCKETS = {"oil", "watercolor"}
_PAINTING_DIMENSION_MIN_CM = 5.0


def _dimension_plausible(cm: float, collection: str | None, medium: str | None = None) -> bool:
    if collection in _LARGE_OBJECT_COLLECTIONS:
        return True
    floor = _DIMENSION_MIN_CM
    if medium_bucket(medium or "") in _PAINTING_LIKE_BUCKETS:
        floor = _PAINTING_DIMENSION_MIN_CM
    return floor <= cm <= _DIMENSION_MAX_CM


# Round 8 addendum (c): an archive that holds only a PHOTOGRAPH of some other object (a mural, a
# building, a statue) is never that object's repository — mission-building-0035 (LoC) is the classic
# case. Only rejected when the resolved medium ISN'T itself photographic (LoC legitimately holds the
# photograph when the placarded object is the photograph itself).
_PHOTO_ARCHIVE_RE = re.compile(r"\bLibrary of Congress\b|\bNational Archives\b", re.I)
_PHOTOGRAPHIC_MEDIUM_RE = re.compile(
    r"\bphoto|\bnegative\b|glass plate|photochrom|halftone|gelatin silver|albumen\b", re.I,
)


def _is_photo_archive_of_a_nonphoto_object(candidate: str, medium: str | None) -> bool:
    if not candidate or not _PHOTO_ARCHIVE_RE.search(candidate):
        return False
    return not _PHOTOGRAPHIC_MEDIUM_RE.search(medium or "")


def _credit_is_wrong_national_gallery(candidate: str, facts_by_key: dict) -> bool:
    """Round 13: The Fighting Temeraire's own Commons Credit field literally says "National Gallery of
    Art" (Washington) though it hangs in the National Gallery, London — a real Commons data mistake, not
    a fact about a different institution. Reject the credit-derived candidate whenever Wikidata's own
    collection/location names the bare "National Gallery" (no "of Art")."""
    if candidate.strip().lower() != "national gallery of art":
        return False
    for key in ("wikidata.collection", "wikidata.location"):
        f = facts_by_key.get(key)
        if not f:
            continue
        v = f["value"][0] if isinstance(f["value"], list) else f["value"]
        if str(v).strip().lower() == "national gallery":
            return True
    return False


# ----------------------------------------------------------------------- structured fields (deterministic)
# Precedence: museum record (title-keyed) > museum record (Commons-credit-keyed) > Wikidata > Commons
# extmetadata > existing catalog value. current_repository is handled separately below (institution
# validity, never a bare QID or a P276 place).
_FIELD_SOURCES = {
    "medium": [("museum.medium", None), ("museum.technique", None), ("museum2.medium", None), ("museum2.technique", None), ("wikidata.made_from_material", None), ("commons.Medium", None)],
    "date_display": [("museum.objectDate", None), ("museum.creation_date", None), ("museum2.objectDate", None), ("museum2.creation_date", None), ("wikidata.inception", None), ("commons.DateTimeOriginal", None)],
    # agent_name deliberately NOT here — see the dedicated block below (round 5): it must never come
    # from commons.Artist, so it can't share the generic first_value() precedence-list shape.
}


def resolve_structured_fields(bundle: dict, existing_item: dict, collection: str | None = None) -> tuple[dict, bool, list[str]]:
    """Deterministically fill medium / date_display+creation_date / current_repository /
    physical_dimensions / agent_name from the facts bundle by source precedence. The model never
    touches these. Returns (fields, needs_review, notes)."""
    facts_by_key = {f["key"]: f for f in bundle["facts"]}
    fields: dict = {}
    notes: list[str] = []
    needs_review = bool(bundle.get("conflicts"))
    if needs_review:
        notes.extend(c["detail"] for c in bundle["conflicts"])

    mismatch = detect_identity_mismatch(existing_item, bundle["facts"])
    if mismatch:
        needs_review = True
        fields["identity_mismatch_suspected"] = True
        fields["identity_mismatch_evidence"] = mismatch["evidence"]
        notes.extend(f"identity_mismatch_suspected: {e}" for e in mismatch["evidence"])

    # Round 8 #2: a TITLE-ONLY Wikidata match (wbsearchentities + creator-verified — confidence
    # "medium", never P18/an explicit accession identifier) only corroborates creator/date. Different
    # physical impressions/casts of the "same" work share a Wikidata item family loosely at best —
    # bather-drying-herself-0232 matched a DIFFERENT Degas pastel than the one pictured;
    # redtailed-hawk-0063 pulled another impression's holder+dimensions entirely. Repository,
    # dimensions and medium from wikidata.* are excluded unless the match is confidence=="high".
    high_confidence_match = (bundle.get("match") or {}).get("confidence") == "high"
    date_conflict = any(c.get("field") == "date" for c in (bundle.get("conflicts") or []))

    def first_value(keys, exclude=()):
        for k, _ in keys:
            if k in exclude:
                continue
            f = facts_by_key.get(k)
            if not f:
                continue
            v = f["value"]
            v = v[0] if isinstance(v, list) else v
            v = str(v).strip()
            if not v:
                continue
            yield v, f["source"]

    medium_exclude = () if high_confidence_match else ("wikidata.made_from_material",)
    if not high_confidence_match and facts_by_key.get("wikidata.made_from_material"):
        notes.append("medium: ignored wikidata.made_from_material — title-only match, not exemplar-specific")

    # medium
    for v, src in first_value(_FIELD_SOURCES["medium"], exclude=medium_exclude):
        fields["medium"] = v
        fields["medium_source"] = src
        break
    else:
        if existing_item.get("medium"):
            fields["medium"] = existing_item["medium"]
            fields["medium_source"] = "existing_catalog_value"
            notes.append("medium: no retrieved fact, kept existing catalog value")

    # Round 13 (B): a print/multi-impression work's repository and dimensions must come only from the
    # SCANNED EXEMPLAR, never from a Wikidata item's collection/dimensions unless that's the exemplar's
    # own institution — so for these works, wikidata.collection/height/width are treated the same as a
    # title-only (medium-confidence) match even when the identity match itself came back "high".
    multi_impression = _is_multi_impression_work(collection, fields.get("medium") or existing_item.get("medium"))
    high_confidence_repo_dims = high_confidence_match and not multi_impression
    if multi_impression and high_confidence_match:
        notes.append("repository/dimensions: treated as title-only (not exemplar-specific) — "
                      "multi-impression work (print technique or a multi-impression collection)")

    # date — accession-year guard applied before acceptance. Round 8 addendum (b): once a "date"
    # conflict is flagged (sources disagree by >25yrs), Wikidata's inception is EXCLUDED rather than
    # winning on precedence order — toilers-of-the-sea-0125 shipped Wikidata's 1847 over Commons+
    # museum's agreeing 1873. On a genuine conflict: museum > commons > blank; never the outlier.
    # Round 13 (B): for a multi-impression work, a much tighter 5yr (not 25yr) gap between Wikidata's
    # inception and the exemplar's OWN Commons date already counts as a disagreement — the exemplar's
    # date wins and the item is flagged for review.
    def _fact_year(key):
        f = facts_by_key.get(key)
        if not f:
            return None
        v = f["value"]
        return _extract_year(str(v[0] if isinstance(v, list) else v))

    dropped_accession = False
    print_date_conflict = False
    if multi_impression:
        wd_year = _fact_year("wikidata.inception")
        exemplar_year = _fact_year("commons.DateTimeOriginal")
        if wd_year and exemplar_year and abs(wd_year - exemplar_year) > _PRINT_DATE_DISAGREEMENT_YEARS:
            print_date_conflict = True
            needs_review = True
            notes.append(f"date: multi-impression exemplar date {exemplar_year} disagrees with "
                         f"wikidata.inception {wd_year} by >{_PRINT_DATE_DISAGREEMENT_YEARS}yrs — "
                         f"preferring the exemplar's own date")
    date_exclude = ("wikidata.inception",) if (date_conflict or print_date_conflict) else ()
    if date_conflict and facts_by_key.get("wikidata.inception"):
        notes.append("date: ignored wikidata.inception — conflicts with museum/Commons dating, "
                     "preferring the more authoritative source instead of the outlier")
    for v, src in first_value(_FIELD_SOURCES["date_display"], exclude=date_exclude):
        if looks_like_accession_number(v):
            dropped_accession = True
            continue
        v = _normalize_iso_date_string(v, collection)
        fields["date_display"] = v
        fields["creation_date"] = v
        fields["date_source"] = src
        break
    else:
        existing_date = existing_item.get("creation_date")
        if existing_date and not looks_like_accession_number(existing_date):
            # Round 7: the ORIGINAL catalog's own date can itself be a leaked upload/scan timestamp
            # (e.g. "Farmyard in Normandy" catalogued "2024-04-10" — a Commons upload date, not a 19th
            # century painting's creation date). Same guard as commons.DateTimeOriginal, applied here
            # because this value skips that check entirely (it's the pre-existing catalog field, not
            # something we just fetched from Commons).
            if looks_like_capture_timestamp(existing_date, bundle.get("commons_upload_year"),
                                            bundle.get("creator_death_year")):
                notes.append(f"date: dropped existing_catalog_value {existing_date!r} — reads as an "
                             f"upload/post-mortem date, not a creation date")
                needs_review = True
            else:
                fields["date_display"] = _normalize_iso_date_string(
                    existing_item.get("date_display") or existing_date, collection)
                fields["creation_date"] = _normalize_iso_date_string(existing_date, collection)
                fields["date_source"] = "existing_catalog_value"
        elif existing_item.get("creation_date"):
            dropped_accession = True
    if dropped_accession:
        notes.append("date: dropped an accession/object-number-shaped candidate (not a creation date)")
        needs_review = needs_review or "date_display" not in fields

    # current repository — museum-API-sourced institution name trusted outright; a Wikidata P195
    # collection label only if it actually looks like an institution (never a bare QID, never a
    # place) AND the match is confidence=="high" (round 8 #2 — a title-only match's P195 belongs to
    # whichever exemplar Wikidata happened to pick, not necessarily the one pictured). Round 8 #1: an
    # aggregator/agency/archive-of-images (Google Cultural Institute, Bridgeman, Commons, Flickr…) is
    # never a real holder, however institution-shaped the phrase reads.
    for key in ("museum.institution", "museum2.institution"):
        f = facts_by_key.get(key)
        if f and str(f["value"]).strip() and not is_aggregator_or_agency(str(f["value"])):
            fields["current_repository"] = str(f["value"]).strip()
            break
    else:
        v = None
        if high_confidence_repo_dims:
            f = facts_by_key.get("wikidata.collection")
            if f:
                v = f["value"][0] if isinstance(f["value"], list) else f["value"]
        elif facts_by_key.get("wikidata.collection"):
            notes.append("current_repository: ignored wikidata.collection — "
                         + ("multi-impression work, not exemplar-specific"
                            if multi_impression else "title-only match, not exemplar-specific"))
        if v and looks_like_institution_label(str(v)) and not is_aggregator_or_agency(str(v)):
            fields["current_repository"] = str(v).strip()
        else:
            if v:
                notes.append(f"current_repository: rejected non-institution/unresolved candidate {v!r}")
            # round 3 #3 / round 8 #1: last resort — a generic institution name lifted from Commons
            # Credit/Institution text, only when nothing more authoritative named one, it isn't an
            # aggregator/agency, and (round 8 addendum c) it isn't a photo archive holding only a
            # PHOTOGRAPH of some other object (a mural/building) rather than the object itself.
            cf = facts_by_key.get("commons.institution_credit")
            if cf:
                candidate = str(cf["value"]).strip()
                if looks_like_institution_label(candidate) and not is_aggregator_or_agency(candidate):
                    if _is_photo_archive_of_a_nonphoto_object(candidate, fields.get("medium")):
                        notes.append(f"current_repository: rejected {candidate!r} — an archive of the "
                                     f"PHOTOGRAPH, not the depicted (non-photographic) object")
                    elif _credit_is_wrong_national_gallery(candidate, facts_by_key):
                        # Round 13: Commons Credit text routinely says "National Gallery of Art"
                        # (Washington) even for works actually held by "National Gallery" (London) — a
                        # known Commons metadata mistake, not a different institution. Never ship it
                        # when Wikidata's own collection/location names the plain "National Gallery".
                        notes.append(f"current_repository: rejected Commons Credit {candidate!r} — "
                                     f"Wikidata names plain 'National Gallery' (London); never mapped "
                                     f"to National Gallery of Art (Washington)")
                        needs_review = True
                    else:
                        fields["current_repository"] = candidate
                        fields["current_repository_source"] = "Commons Credit text"

    # agent name — round 5 bug fix: this MUST come only from the museum record's own creator field or
    # Wikidata P170, never from commons.Artist (routinely the photographer/uploader — "Didier
    # Descouens", "Elke Wetzig" — or even the holding INSTITUTION, e.g. "Rijksmuseum" on 100+ Hiroshige
    # prints; 133 packets had a structured artist disagreeing with the catalog for exactly this
    # reason). commons.Artist stays a fact for writers to see but is never used to set this field.
    #
    # Round 13b: co-creator joining (round 13 C) is REMOVED — reviewed against static/catalog and found
    # to be joining Wikidata's multiple, often ALTERNATIVE/disputed P170 attributions as if they were
    # collaborators ("Pieter Claesz, Clara Peeters, and Floris van Dyck"; "Rembrandt, Jan Lievens, and
    # Jan Gillisz van Vliet") and, via commons.Artist, real garbage ("Cellarius, Andreas, Schenk, Peter,
    # Valck, G. (Gerard), and Loon, J. Van", "Bibliographisches Institut (Leipzig, Germany) and Haeckel,
    # Ernst"). Back to the PRINCIPAL creator only — genuine collaborations are curated by hand instead
    # (see --agent-overrides / ~/pieria-img/curation/agent_overrides.json).
    def _detect_attribution_qualifier(catalog_artist: str) -> str | None:
        """Round 13b: the qualified name must be EXACTLY (accent/case-insensitive) the catalog's own
        principal artist — never a fuzzy/partial match, so trailing title words or unrelated text can
        never leak in ("After the bath by Edgar Degas" is a Commons FILE TITLE, not a qualifier — see
        the regex fix above; this equality check is the second line of defence)."""
        if not catalog_artist:
            return None
        norm_catalog = _norm(catalog_artist)
        for key in ("commons.ObjectName", "commons.Artist", "commons.Credit"):
            f = facts_by_key.get(key)
            if not f:
                continue
            text = str(f["value"][0] if isinstance(f["value"], list) else f["value"])
            m = _ATTRIBUTION_QUALIFIER_RE.search(text)
            qualifier = "school of"
            if m:
                qualifier, name = m.group(1).lower(), m.group(2)
            else:
                m2 = _SCHULE_QUALIFIER_RE.search(text)
                if not m2:
                    continue
                name = m2.group(1)
            if _norm(name) == norm_catalog:
                return f"{qualifier.capitalize()} {name.strip()}"
        return None

    agent_val, agent_src = None, None
    for key in ("museum.creators", "wikidata.creator"):
        f = facts_by_key.get(key)
        if not f:
            continue
        v = f["value"]
        v = str(v[0] if isinstance(v, list) else v).strip()
        if v and not _agent_name_is_suspect(v):
            agent_val, agent_src = v, f["source"]
            break

    if agent_val:
        catalog_artist = (existing_item.get("agent_name") or "").strip()
        if not catalog_artist or catalog_artist.lower() in ("unknown artist", "unknown"):
            # nothing to disagree with — a real name where the catalog had none is a genuine gain.
            fields["agent_name_confirmed"] = agent_val
            fields["agent_name_source"] = agent_src
        else:
            # Round 13 (C): a shared surname with a DIFFERENT given name ("Jan Hals" vs catalog
            # "Frans Hals") must never auto-confirm/replace — it's a different, real person.
            different_person = _is_different_person_same_surname(catalog_artist, agent_val)
            genuine_match = (not different_person) and _names_plausibly_match(catalog_artist, agent_val)
            if genuine_match:
                # confirmed, not changed — keep the catalog's own spelling/form (e.g. "Rembrandt van
                # Rijn"), don't replace it with a shorter/differently-formatted museum/Wikidata string —
                # UNLESS Commons/museum text qualifies it ("School of Raphael" — round 8 addendum e),
                # which is a real weakening of the attribution the bare confirmed name would hide.
                qualified = _detect_attribution_qualifier(catalog_artist)
                if qualified:
                    fields["agent_name_confirmed"] = qualified
                    fields["agent_name_source"] = "Commons attribution qualifier"
                    needs_review = True
                    notes.append(f"agent_name: qualified attribution detected — {qualified!r} "
                                 f"(catalog said {catalog_artist!r})")
                else:
                    fields["agent_name_confirmed"] = catalog_artist
                    fields["agent_name_source"] = f"existing_catalog_value (confirmed by {agent_src})"
            else:
                # disagreement — never overwrite; flag for a human instead of shipping a guess.
                needs_review = True
                fields["agent_name_disagreement"] = agent_val
                reason = (" (same surname, different given name)" if different_person else "")
                notes.append(f"agent_name: {agent_src} says {agent_val!r}{reason}, catalog says "
                             f"{catalog_artist!r} — kept catalog value, needs_review")

    # Round 13b (item 3): a multi-agency space-image credit ("Image: National Aeronautics and Space
    # Administration ... European space agency ... Canadian Space Agency ... Space Telescope Science
    # Institute ...") is common in the cosmos collection and often IS the raw catalog agent_name,
    # newlines/URLs and all. Clean it to a short acronym list whenever >=2 known agencies are
    # recognisable in the EXISTING catalog value — never invents an agency that isn't actually named,
    # and never touches a value the block above already resolved.
    if "agent_name_confirmed" not in fields and "agent_name_disagreement" not in fields:
        cleaned = _clean_space_agency_credit(existing_item.get("agent_name") or "")
        if cleaned:
            fields["agent_name_confirmed"] = cleaned
            fields["agent_name_source"] = "space-agency credit cleanup"
            notes.append(f"agent_name: cleaned multi-agency credit to {cleaned!r}")

    # physical dimensions — round 6: prefer a museum record's own dimensions text (e.g. Cleveland's/
    # AIC's own measurements, already unit-correct) over a computed Wikidata value. The Wikidata path
    # was shipping raw quantity amounts with the unit silently ignored ("+1272 x +1121 cm" on a small
    # watercolour); wikidata_quantity_to_cm() now converts mm/in/m -> cm before this point, but a bad
    # source value or unit could still slip through, so a plausibility guard drops anything outside a
    # sane range for a hand-held object (0.5-1500 cm per side) unless the collection is one where a
    # genuinely large object is expected (building/monument/wall map).
    got_museum_dims = False
    for key in ("museum.dimensions", "museum2.dimensions"):
        f = facts_by_key.get(key)
        if f and str(f["value"]).strip():
            fields["physical_dimensions"] = str(f["value"]).strip()
            fields["physical_dimensions_source"] = f["source"]
            got_museum_dims = True
            break
    if not got_museum_dims and not high_confidence_repo_dims:
        if facts_by_key.get("wikidata.height") or facts_by_key.get("wikidata.width"):
            notes.append("physical_dimensions: ignored wikidata height/width — "
                         + ("multi-impression work, not exemplar-specific"
                            if multi_impression else "title-only match, not exemplar-specific"))
    elif not got_museum_dims:
        h = facts_by_key.get("wikidata.height")
        w = facts_by_key.get("wikidata.width")
        if h or w:
            def _to_float(f):
                try:
                    return float(f["value"][0]) if f else None
                except (TypeError, ValueError):
                    return None
            hv, wv = _to_float(h), _to_float(w)
            sides = [x for x in (hv, wv) if x is not None]
            if sides and all(_dimension_plausible(x, collection, fields.get("medium")) for x in sides):
                parts = [f"{x:.1f}" for x in (hv, wv) if x is not None]
                fields["physical_dimensions"] = " x ".join(parts) + " cm"
                fields["physical_dimensions_source"] = "Wikidata"
            elif sides:
                notes.append(f"physical_dimensions: dropped implausible Wikidata value(s) {sides} cm "
                              f"(guard: painting floor 5cm, else 0.5-1500cm unless building/monument/map)")

    if is_cc_by_row(existing_item):
        # ADR-142: a CC BY credit is exact as given — the agent_name is never confirmed, qualified or
        # "cleaned" (the space-agency cleanup would turn "ESA/Webb, NASA & CSA, A. Leroy" into "ESA, NASA, CSA").
        for k in ("agent_name_confirmed", "agent_name_source", "agent_name_disagreement"):
            fields.pop(k, None)
        notes.append("agent_name: CC BY row — credit kept verbatim (ADR-142)")

    return fields, needs_review, notes


# ----------------------------------------------------------------------- narrative (model, grounded only)
_NARRATIVE_PROMPT = """You are writing a museum wall placard. Use ONLY the facts listed below — never \
add a claim (name, date, place, event, subject detail) that is not directly supported by them. If the \
facts are thin, write a SHORTER, more general placard about what is visibly depicted, the maker, and the \
date rather than inventing anything. You may use the "background text (context only)" ONLY to avoid \
stating something false — never as a source of new claims or phrasing; do not copy its wording. Facts \
marked PARAPHRASE ONLY (CC BY text) may support claims but must be restated in your own words.
{version_note}
Facts (each has a fact_key you must cite):
{facts_block}

Structured fields (already fixed, do not contradict them):
{structured_block}

Background text (context only — verification, not a source of new claims):
{context_block}

Return ONLY a JSON object:
{{"description_narrative": "2-3 plain-English sentences", "tags": "5-8 comma-separated lowercase keywords",
"claims": [{{"text": "one factual claim from the narrative", "fact_keys": ["wikidata.inception", ...]}}]}}
Every sentence containing a checkable fact must have a matching entry in "claims" whose fact_keys point \
at the facts above that support it."""


def _facts_block(bundle: dict) -> str:
    lines = []
    for f in bundle["facts"]:
        v = f["value"]
        v = ", ".join(v) if isinstance(v, list) else v
        note = "; PARAPHRASE ONLY - never copy its wording" if f.get("paraphrase_only") else ""
        lines.append(f"- [{f['key']}] {v} (source: {f['source']}, {f['licence']}{note})")
    return "\n".join(lines) or "(none retrieved)"


def _structured_block(fields: dict) -> str:
    keys = ("medium", "date_display", "current_repository", "physical_dimensions")
    return "\n".join(f"- {k}: {fields[k]}" for k in keys if fields.get(k)) or "(none)"


def build_narrative_prompt(bundle: dict, fields: dict, offending_claim: dict | None = None) -> str:
    version_note = ""
    if bundle.get("is_version_of"):
        v = bundle["is_version_of"]
        version_note = (f"\nIMPORTANT: this object is {v['relation']} \"{v['of_label']}\" — say PLAINLY "
                         f"that it is a copy/cast/replica/version, do not describe it as the original.")
    prompt = _NARRATIVE_PROMPT.format(
        version_note=version_note,
        facts_block=_facts_block(bundle),
        structured_block=_structured_block(fields),
        context_block="\n".join(bundle.get("check_only_texts") or [])[:1200] or "(none)",
    )
    if offending_claim:
        prompt += (f"\n\nYour previous draft included this claim, which is NOT supported by the facts "
                   f"above: \"{offending_claim.get('text')}\". Remove it or replace it with one the facts "
                   f"actually support.")
    return prompt


def check_claims(claims: list[dict], bundle: dict) -> tuple[bool, dict | None]:
    """Every claim must cite >=1 real fact key, and the claim's specifics (years/numbers/proper nouns
    it names) must actually appear in the text of the facts it cites. Returns (ok, offending_claim)."""
    facts_by_key = {f["key"]: f for f in bundle["facts"]}
    for claim in claims or []:
        keys = claim.get("fact_keys") or []
        real_keys = [k for k in keys if k in facts_by_key]
        if not real_keys:
            return False, claim
        cited_text = " ".join(
            (", ".join(facts_by_key[k]["value"]) if isinstance(facts_by_key[k]["value"], list) else str(facts_by_key[k]["value"]))
            for k in real_keys
        ).lower()
        numbers = re.findall(r"\b\d{3,4}\b", claim.get("text") or "")
        for n in numbers:
            if n not in cited_text:
                return False, claim
    return True, None


def _template_placard_grounded(item: dict, fields: dict, bundle: dict) -> dict:
    """Deterministic fallback, using only the grounded structured fields — never invents."""
    t, a = item.get("title", "Untitled"), fields.get("agent_name_confirmed") or item.get("agent_name", "")
    d = fields.get("date_display") or item.get("date_display") or item.get("creation_date") or ""
    m = fields.get("medium") or item.get("medium") or ""
    repo = fields.get("current_repository") or item.get("source", "a public collection")
    s1 = t + (f" by {a}" if a and a != "Unknown Artist" else "") + (f" ({d})" if d else "") + "."
    version_note = ""
    if bundle.get("is_version_of"):
        v = bundle["is_version_of"]
        version_note = f" This is {v['relation']} \"{v['of_label']}\"."
    s2 = (f"{m}. " if m else "") + f"Held by {repo}.{version_note}"
    item = dict(item)
    item["description_narrative"] = (s1 + " " + s2).strip()
    if not item.get("tags"):
        words = [w.lower() for w in re.split(r"[\s,]+", t) if len(w) > 3][:5]
        item["tags"] = ", ".join(words)
    return item


async def generate_narrative(bundle: dict, fields: dict, model_sem: asyncio.Semaphore) -> tuple[dict | None, bool]:
    """Returns (parsed_json_or_None, used_fallback). Regenerates once on a failed claim check."""
    offending = None
    for attempt in range(2):
        prompt = build_narrative_prompt(bundle, fields, offending)
        async with model_sem:
            try:
                text = await asyncio.to_thread(
                    ai_client.chat, "vision", [{"role": "user", "content": prompt}],
                    json_mode=True, cfg=_narrative_cfg(),
                )
                data = ai_client.parse_json(text)
            except Exception as e:
                logger.info(f"    · model call failed ({e})")
                return None, True
        ok, offending = check_claims(data.get("claims"), bundle)
        if ok:
            return data, False
    return None, True


# ----------------------------------------------------------------------- per-item pipeline
async def process_item(fx: Fetcher, model_sem: asyncio.Semaphore, item: dict, collection: str,
                        extra_fact: dict | None = None) -> dict:
    bundle = await build_facts_bundle(fx, item, extra_fact=extra_fact)
    fields, needs_review, notes = resolve_structured_fields(bundle, item, collection)

    new_item = dict(item)
    for key in ("medium", "date_display", "creation_date", "current_repository", "physical_dimensions"):
        if fields.get(key):
            new_item[key] = fields[key]

    data, fell_back = await generate_narrative(bundle, fields, model_sem)
    if data and not fell_back:
        new_item["description_narrative"] = data.get("description_narrative") or new_item.get("description_narrative")
        if data.get("tags"):
            new_item["tags"] = data["tags"]
    else:
        new_item = _template_placard_grounded(new_item, fields, bundle)

    return {
        "item": new_item,
        "stats": {
            "collection": collection, "title": item.get("title"),
            "matched": bool(bundle["match"]), "n_facts": len(bundle["facts"]),
            "needs_review": needs_review, "notes": notes,
            "fallback_to_template": fell_back, "is_version_of": bundle.get("is_version_of"),
        },
        "bundle": bundle,
    }


# ----------------------------------------------------------------------- packets (inputs for the writer)
# Round 2: no API model — Claude subagents write the narratives. This tool's job stops at preparing a
# self-contained "writing packet" per item and, later, validating/importing what comes back.
def item_key(idx: int, title: str) -> str:
    return f"{_slug(title) or 'untitled'}-{idx:04d}"


_manifest_cache: dict[str, dict[str, str]] = {}


def _manifest_index(collection: str) -> dict[str, str]:
    """art-pack/_Library filename -> title, from that collection's signed manifest — read individually,
    never a recursive copy of the pack tree. Keyed by filename (not title) so the resolver below can
    verify a build_pack-derived filename actually landed in the pack, and separately sanity-check the
    manifest's own title against the catalog's."""
    if collection in _manifest_cache:
        return _manifest_cache[collection]
    out: dict[str, str] = {}
    mf = ART_PACK_MANIFESTS / f"{collection}.json"
    if mf.exists():
        try:
            data = json.loads(mf.read_text())
            for it in data.get("items", []):
                lf = (it.get("image") or {}).get("local_file")
                title = it.get("title")
                if lf:
                    out[lf] = title
        except Exception as e:
            logger.info(f"    · manifest read failed for {collection}: {e}")
    _manifest_cache[collection] = out
    return out


# Round 10: the bug this fixes — the OLD _manifest_local_file_map mapped pack masters by _norm(title)
# ONLY, and _norm strips leading articles + last-wins on collision, so every same-titled entry in a
# collection (Whistler "The Artist in His Studio" vs Sargent "An Artist in His Studio"; 3x "The
# Flowers"; 4x "Bathers"; two "Penitent Magdalene"s) got the SAME master — wrong previews, placards
# written from the wrong image, false duplicate-image groups. Fixed by resolving by catalog INDEX via
# build_pack's own deterministic filename derivation instead: a title can collide, an index cannot.
_expected_masters_cache: dict[str, list[str | None]] | None = None
title_mismatches: list[dict] = []  # {collection, idx, catalog_title, manifest_title, filename} — surfaced, never rejected
_pack_resolve_stats = {"resolved": 0, "unresolved": 0}


def _get_expected_masters() -> dict[str, list[str | None]]:
    global _expected_masters_cache
    if _expected_masters_cache is None:
        from tools.build_pack import compute_expected_masters_indexed
        # No collections_filter: the dedup (shared source_url -> one filename) is global across every
        # collection, exactly like a real build_pack run over the whole catalog — a filtered call here
        # could name a different (later-claiming) filename for a shared work.
        _expected_masters_cache = compute_expected_masters_indexed()
    return _expected_masters_cache


def _pack_master_for(collection: str, idx: int, title: str | None = None) -> str | None:
    """Resolve the served-catalog item at (collection, idx) to its pack master filename, via
    build_pack's own deterministic derivation — never by title (see round-10 note above). Accepted only
    if the filename appears in that collection's signed manifest AND exists in ART_PACK_LIBRARY;
    otherwise None (callers fall back to the URL preview). `title`, when given, drives a sanity check —
    a mismatch against the manifest's own title is logged + recorded in `title_mismatches`, never
    rejected (a pin can legitimately retitle a slot)."""
    rows = _get_expected_masters().get(collection) or []
    filename = rows[idx] if 0 <= idx < len(rows) else None
    if not filename:
        _pack_resolve_stats["unresolved"] += 1
        return None
    manifest_title = _manifest_index(collection).get(filename)
    if manifest_title is None or not (ART_PACK_LIBRARY / filename).exists():
        _pack_resolve_stats["unresolved"] += 1
        return None
    if title and _norm(manifest_title or "") != _norm(title):
        logger.info(f"    · title mismatch {collection}[{idx}]: catalog={title!r} manifest={manifest_title!r}")
        title_mismatches.append({
            "collection": collection, "idx": idx, "catalog_title": title,
            "manifest_title": manifest_title, "filename": filename,
        })
    _pack_resolve_stats["resolved"] += 1
    return filename


def build_preview_from_master(collection: str, item: dict, key: str, idx: int) -> Path | None:
    """Downscale from the local pack master if this work has one. Reads ONE file directly — never
    lists/copies the (16GB) _Library tree."""
    out_path = PREVIEWS_DIR / collection / f"{key}.jpg"
    if out_path.exists():
        return out_path
    local_file = _pack_master_for(collection, idx, item.get("title"))
    if not local_file:
        return None
    src = ART_PACK_LIBRARY / local_file
    if not src.exists():
        return None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((PREVIEW_MAX_PX, PREVIEW_MAX_PX), Image.Resampling.LANCZOS)
            im.save(out_path, format="JPEG", quality=85)
        return out_path
    except Exception as e:
        logger.info(f"    · preview from master failed for {item.get('title')}: {e}")
        return None


async def build_preview_from_url(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                                  collection: str, item: dict, key: str) -> Path | None:
    """Fallback when there's no local master: fetch the catalog's own thumbnail/preview URL."""
    out_path = PREVIEWS_DIR / collection / f"{key}.jpg"
    if out_path.exists():
        return out_path
    url = item.get("thumbnail_url") or item.get("source_url")
    if not url:
        return None
    async with sem:
        try:
            r = await client.get(url, timeout=30.0, follow_redirects=True)
            if r.status_code != 200:
                return None
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(io.BytesIO(r.content)) as im:
                im = im.convert("RGB")
                im.thumbnail((PREVIEW_MAX_PX, PREVIEW_MAX_PX), Image.Resampling.LANCZOS)
                im.save(out_path, format="JPEG", quality=85)
            return out_path
        except Exception as e:
            logger.info(f"    · preview fetch failed for {item.get('title')}: {e}")
            return None


_CATALOG_PACKET_FIELDS = (
    "title", "agent_name", "agent_role", "date_display", "creation_date", "medium",
    "cultural_context", "tags", "source", "source_url", "license", "credit_line",
)


def build_packet(key: str, collection: str, item: dict, bundle: dict, fields: dict,
                  needs_review: bool, preview_path: Path | None) -> dict:
    return {
        "key": key, "collection": collection, "title": item.get("title"),
        "catalog": {k: item.get(k) for k in _CATALOG_PACKET_FIELDS},
        "structured": fields,
        "facts": bundle["facts"],
        "check_only": {
            "wikipedia_lead": bundle.get("wikipedia_lead"),
            "commons_description": bundle.get("commons_description"),
        },
        "is_version_of": bundle.get("is_version_of"),
        "needs_review": needs_review,
        "identity_mismatch_suspected": bool(fields.get("identity_mismatch_suspected")),
        "identity_mismatch_evidence": fields.get("identity_mismatch_evidence") or [],
        "image": str(preview_path) if preview_path else None,
    }


def merge_packet_index(existing: dict[str, str], new_entries: dict[str, str],
                        filtered_collection: str | None) -> dict[str, str]:
    """--mode packets --collection X only builds packets for X, so folding its {key: collection}
    entries straight into packets_index.json would silently drop every other collection's entries —
    which then made a later full import silently skip everything but X (bit us 2026-09-27). An
    unfiltered run already covers every collection and can just replace outright; a filtered run
    instead drops X's OLD entries (keys can change — a row's title/idx changed) and folds in the
    fresh ones, keeping every other collection's entries untouched."""
    if not filtered_collection:
        return dict(new_entries)
    merged = {k: v for k, v in existing.items() if v != filtered_collection}
    merged.update(new_entries)
    return merged


def build_batches(entries: list[dict], batch_size: int = 100) -> list[list[dict]]:
    """Group packet entries into ~batch_size chunks, collection-coherent where possible: sort by
    collection first so a chunk boundary only splits a collection when it doesn't divide evenly."""
    ordered = sorted(entries, key=lambda e: (e["collection"], e["key"]))
    return [ordered[i:i + batch_size] for i in range(0, len(ordered), batch_size)]


async def run_packets(*, only_sample: bool, limit: int | None, collection: str | None, batch_size: int = 100,
                       extra_facts_dir: str | Path | None = None):
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    PACKETS_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    catalog = load_catalog()
    targets: list[tuple[str, int]] = []
    if only_sample:
        sample = json.loads((AUDIT_DIR / "sample.json").read_text())
        targets = [(r["collection"], r["idx"]) for r in sample]
    else:
        colls = [collection] if collection else list(catalog.keys())
        for c in colls:
            for i in range(len(catalog[c])):
                targets.append((c, i))
    if limit:
        targets = targets[:limit]

    extra_rejected: list[dict] = []
    extra_facts = load_extra_facts(extra_facts_dir, rejected=extra_rejected)
    extra_by_coll: dict[str, dict[int, dict]] = {}
    if extra_facts:
        for c in {c for c, _ in targets}:
            extra_by_coll[c] = index_extra_facts_by_catalog(extra_facts, catalog[c])

    sem = asyncio.Semaphore(4)
    report = {"collections": {}, "matched": 0, "needs_review_count": 0, "total": 0, "with_preview": 0,
              "with_cc0_curatorial": 0, "facts_total": 0}
    packet_index: dict[str, str] = {}
    entries: list[dict] = []
    mismatches: list[dict] = []

    _CC0_CURATORIAL_KEYS = ("museum.description", "museum.did_you_know",
                            "museum2.description", "museum2.did_you_know")

    async with httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True) as client:
        fx = Fetcher(client, sem)

        async def one(coll, idx):
            item = catalog[coll][idx]
            key = item_key(idx, item.get("title") or "")
            fact_path = FACTS_DIR / coll / f"{idx:04d}.json"
            fact_path.parent.mkdir(parents=True, exist_ok=True)
            packet_path = PACKETS_DIR / coll / f"{key}.json"
            extra_fact = extra_by_coll.get(coll, {}).get(idx)
            bundle = None
            if fact_path.exists() and packet_path.exists():
                bundle = json.loads(fact_path.read_text())  # resume: skip re-fetching
                # ...unless this run supplies release text the cached bundle predates.
                if extra_fact and not any(f["key"].startswith("nasa.") for f in bundle["facts"]):
                    bundle = None
            if bundle is None:
                bundle = await build_facts_bundle(fx, item, coll, extra_fact=extra_fact)
                fact_path.write_text(json.dumps(bundle, indent=2, default=str))
            fields, needs_review, notes = resolve_structured_fields(bundle, item, coll)

            preview = build_preview_from_master(coll, item, key, idx)
            if not preview:
                preview = await build_preview_from_url(client, sem, coll, item, key)

            packet = build_packet(key, coll, item, bundle, fields, needs_review, preview)
            packet_path.parent.mkdir(parents=True, exist_ok=True)
            packet_path.write_text(json.dumps(packet, indent=1, ensure_ascii=False, default=str))
            has_cc0 = any(f["key"] in _CC0_CURATORIAL_KEYS for f in bundle["facts"])
            mismatch = None
            if fields.get("identity_mismatch_suspected"):
                mismatch = {"key": key, "collection": coll, "title": item.get("title"),
                            "agent_name": item.get("agent_name"), "medium": item.get("medium"),
                            "evidence": fields.get("identity_mismatch_evidence") or []}
            return (coll, key, packet_path, bool(bundle.get("match")), needs_review, bool(preview),
                    has_cc0, len(bundle["facts"]), mismatch)

        results = await asyncio.gather(*(one(c, i) for c, i in targets))

    for coll, key, packet_path, matched, needs_review, has_preview, has_cc0, n_facts, mismatch in results:
        cstat = report["collections"].setdefault(
            coll, {"total": 0, "matched": 0, "needs_review": 0, "with_preview": 0,
                   "with_cc0_curatorial": 0, "facts_total": 0})
        cstat["total"] += 1
        cstat["matched"] += int(matched)
        cstat["needs_review"] += int(needs_review)
        cstat["with_preview"] += int(has_preview)
        cstat["with_cc0_curatorial"] += int(has_cc0)
        cstat["facts_total"] += n_facts
        report["total"] += 1
        report["matched"] += int(matched)
        report["needs_review_count"] += int(needs_review)
        report["with_preview"] += int(has_preview)
        report["with_cc0_curatorial"] += int(has_cc0)
        report["facts_total"] += n_facts
        rel = str(packet_path.relative_to(REGROUND_DIR))
        packet_index[key] = coll
        entries.append({"key": key, "collection": coll, "packet": rel})
        if mismatch:
            mismatches.append(mismatch)

    for c, cstat in report["collections"].items():
        cstat["facts_per_item"] = round(cstat["facts_total"] / cstat["total"], 2) if cstat["total"] else 0
    report["facts_per_item"] = round(report["facts_total"] / report["total"], 2) if report["total"] else 0

    if extra_rejected:
        report["extra_facts_rejected"] = extra_rejected
    if extra_facts:
        matched_ns = {rec["n"] for idx_map in extra_by_coll.values() for rec in idx_map.values()}
        report["extra_facts_total"] = len(extra_facts)
        report["extra_facts_matched"] = len(matched_ns)
        report["extra_facts_unmatched"] = sorted(rec["n"] for rec in extra_facts if rec["n"] not in matched_ns)

    existing_index: dict[str, str] = {}
    if PACKET_INDEX_PATH.exists():
        try:
            existing_index = json.loads(PACKET_INDEX_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            existing_index = {}
    packet_index = merge_packet_index(existing_index, packet_index, collection if not only_sample else None)
    PACKET_INDEX_PATH.write_text(json.dumps(packet_index, indent=1))
    for i, batch in enumerate(build_batches(entries, batch_size), 1):
        (BATCHES_DIR / f"batch_{i:02d}.json").write_text(json.dumps(batch, indent=1))
    report["n_batches"] = len(build_batches(entries, batch_size))
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    IDENTITY_MISMATCHES_PATH.write_text(json.dumps(
        {"count": len(mismatches), "items": mismatches}, indent=1, ensure_ascii=False))
    return report


# ----------------------------------------------------------------------- import (validate + land the writer's output)
_VISUAL_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "with", "is", "are", "was", "were",
    "this", "it", "its", "his", "her", "their", "by", "from", "to", "as", "shows", "depicts",
}


_POSSESSIVE_RE = re.compile(r"'s?$", re.I)


def _possessive_stem(word: str) -> str:
    """"Nebula's" -> "nebula", "Stars'" -> "stars" (lower-cased). The possessive marker is not part of the
    proper noun, so the noun is checked against the title/facts, not the inflected form."""
    return _POSSESSIVE_RE.sub("", word).lower()


def _visual_claim_ok(text: str, title: str, facts: list[dict]) -> tuple[bool, str | None]:
    """A visual:true claim may describe only what is visibly depicted — no names/dates/places/events.
    Reject a number, or a capitalised word that isn't part of the title or a fact value."""
    if re.search(r"\d", text or ""):
        return False, "visual claim contains a number/year"
    allowed_words = set()
    for w in re.findall(r"[A-Za-z']+", title or ""):
        allowed_words.add(w.lower())
        allowed_words.add(_possessive_stem(w))
    for f in facts:
        v = f["value"]
        vals = v if isinstance(v, list) else [v]
        for val in vals:
            for w in re.findall(r"[A-Za-z']+", str(val)):
                allowed_words.add(w.lower())
                allowed_words.add(_possessive_stem(w))
    for m in re.finditer(r"\b[A-Z][a-zA-Z']*\b", text or ""):
        word = m.group(0)
        if word.lower() in _VISUAL_STOPWORDS:
            continue
        if (re.match(r"^[A-Z][a-z']*$", word) and word.lower() not in allowed_words
                and _possessive_stem(word) not in allowed_words):
            # A capitalised word that isn't sentence-initial reads as a proper noun. Sentence-initial
            # capitals — the claim's first word, or the first word after . ! ? (claims can hold two
            # sentences) — are exempt.
            if not re.search(r"(^|[.!?][\"'”’)]*\s+)[\"'“‘(]*$", text[:m.start()]):
                return False, f"visual claim names a proper noun not in the title/facts: {word!r}"
    return True, None


def _copied_run(narrative: str, source: str, n: int = _COPY_RUN_WORDS) -> str | None:
    """The first n-word run of `narrative` that also appears verbatim (case/punctuation-insensitive) in
    `source`, else None."""
    def words(t):
        return re.findall(r"[a-z0-9']+", t.lower().replace("\u2019", "'"))
    nw, sw = words(narrative), words(source)
    if len(nw) < n or len(sw) < n:
        return None
    grams = {tuple(sw[i:i + n]) for i in range(len(sw) - n + 1)}
    for i in range(len(nw) - n + 1):
        if tuple(nw[i:i + n]) in grams:
            return " ".join(nw[i:i + n])
    return None


def validate_written_item(written: dict, packet: dict) -> tuple[bool, list[str]]:
    """Returns (passed, reasons). reasons is non-empty iff not passed."""
    reasons = []
    facts = packet.get("facts") or []
    facts_by_key = {f["key"]: f for f in facts}
    for claim in written.get("claims") or []:
        text = claim.get("text") or ""
        if claim.get("visual"):
            ok, reason = _visual_claim_ok(text, packet.get("title") or "", facts)
            if not ok:
                reasons.append(f"{reason}: {text!r}")
            continue
        keys = claim.get("fact_keys") or []
        real_keys = [k for k in keys if k in facts_by_key]
        if not real_keys:
            reasons.append(f"claim cites no real fact_keys: {text!r}")
            continue
        cited_text = " ".join(
            (", ".join(facts_by_key[k]["value"]) if isinstance(facts_by_key[k]["value"], list) else str(facts_by_key[k]["value"]))
            for k in real_keys
        ).lower()
        for n in re.findall(r"\b\d{3,4}\b", text):
            if n not in cited_text:
                reasons.append(f"claim year/number {n!r} not found in its cited facts: {text!r}")
    if not (written.get("description_narrative") or "").strip():
        reasons.append("empty description_narrative")
    for f in facts:
        if f.get("paraphrase_only") and isinstance(f.get("value"), str):
            run = _copied_run(written.get("description_narrative") or "", f["value"])
            if run:
                reasons.append(f"narrative copies {_COPY_RUN_WORDS}+ consecutive words of paraphrase-only "
                               f"fact {f['key']} ({f.get('licence')}): {run!r}")
    return (not reasons), reasons


# ----------------------------------------------------------------------- field corrections (round 4)
# The writers can see the actual image; the pipeline can't. A structured field whose ONLY source is
# `existing_catalog_value` (the old, unverified catalog — never museum/Wikidata) may be flatly wrong
# (storm-coming-0125: catalog says "Oil on canvas", the image is a charcoal drawing and Commons' own
# Credit says "Drawing, Storm Coming"). A writer may propose a correction, grounded in a fact just like
# any other claim; it's accepted only if it's grounded AND the field it's replacing was never actually
# verified in the first place.
_CORRECTABLE_FIELD_SOURCE_KEY = {"medium": "medium_source", "date_display": "date_source"}
_ALL_STRUCTURED_FIELDS = ("medium", "date_display", "current_repository", "physical_dimensions")
_QID_IN_URL_RE = re.compile(r"/(Q\d+)$")

# ----------------------------------------------------------------------- curated overrides (--curated)
# A human already hand-curated these (collection, idx) pairs' listed fields directly in the served
# catalog (static/catalog) — e.g. ~/pieria-img/curation/applied.json. Every field the import can
# actually touch (header/structured fields) is PROTECTED against every downstream override path
# (packet structured fill, a writer's field_correction, identity_mismatch blanking, medium_doubtful
# blanking, header hygiene): whatever the pipeline would have written is discarded in favour of the
# served-catalog value it started from, and the collision is recorded in
# import_report["curated_conflicts"]. Narrative/tags/image/licence fields are never protected — they
# either come from the writers (narrative/tags) or are never touched by run_import at all (image/licence).
_TOUCHABLE_STRUCTURED_FIELDS = {
    "title", "agent_name", "agent_role", "date_display", "creation_date", "medium",
    "current_repository", "physical_dimensions",
}
_FIELD_SOURCE_KEY = {
    "medium": "medium_source", "date_display": "date_source", "creation_date": "date_source",
    "current_repository": "current_repository_source", "physical_dimensions": "physical_dimensions_source",
    "agent_name": "agent_name_source",
}


def load_curated_protections(path: str | Path | None) -> dict[tuple[str, int], set[str]]:
    """--curated PATH (default None -> {}): {(collection, idx): {protected field names}}, restricted
    to fields run_import can actually touch — a curated changed_fields entry naming an image/licence
    field (aspect_crops, credit_line, license*, source*, thumbnail_url, resolution_tier, delivered_edge,
    focal_point) is simply not in this set, since the import never writes those anyway."""
    if not path:
        return {}
    records = json.loads(Path(path).read_text())
    out: dict[tuple[str, int], set[str]] = {}
    for rec in records:
        coll, idx = rec.get("collection"), rec.get("idx")
        if coll is None or idx is None:
            continue
        fields = {f for f in (rec.get("changed_fields") or []) if f in _TOUCHABLE_STRUCTURED_FIELDS}
        if fields:
            out[(coll, idx)] = fields
    return out


# Round 13b (item 4): a small, hand-curated set of genuine collaborations (Wikidata's multiple P170
# values are usually alternative/disputed attributions, not collaborators, so round 13b removed
# automatic co-creator joining — see resolve_structured_fields). Entries are keyed by the PRE-DROP
# index (the packet key's own `-NNNN` — same indexing item_key()/the packets/ tree use), and applied
# only after the title is verified to still match, so a later re-curation of the underlying catalog
# can never silently misapply an override written against a different work.
def load_agent_overrides(path: str | Path | None) -> dict[tuple[str, int], dict]:
    """--agent-overrides PATH (default None -> {}): {(collection, pre_idx): {"title", "agent_name"}}."""
    if not path:
        return {}
    records = json.loads(Path(path).read_text())
    out: dict[tuple[str, int], dict] = {}
    for rec in records:
        coll, idx = rec.get("collection"), rec.get("pre_idx")
        if coll is None or idx is None or not rec.get("agent_name"):
            continue
        out[(coll, idx)] = {"title": rec.get("title") or "", "agent_name": rec["agent_name"]}
    return out


def _extract_match_qids(packet: dict) -> list[str]:
    """Every distinct Wikidata QID backing this packet's facts (there's normally exactly one — the
    resolved match — but a Commons-credit-derived match can add a second)."""
    qids: list[str] = []
    for f in packet.get("facts") or []:
        if f.get("source") == "Wikidata" and f.get("source_url"):
            m = _QID_IN_URL_RE.search(f["source_url"])
            if m and m.group(1) not in qids:
                qids.append(m.group(1))
    return qids


def validate_field_correction(correction: dict, packet: dict) -> tuple[bool, str | None]:
    value = str((correction or {}).get("value") or "").strip()
    if not value:
        return False, "empty value"
    facts_by_key = {f["key"]: f for f in packet.get("facts") or []}
    keys = (correction or {}).get("fact_keys") or []
    real_keys = [k for k in keys if k in facts_by_key]
    if not real_keys:
        return False, "cites no real fact_keys"
    cited_text = " ".join(
        (", ".join(facts_by_key[k]["value"]) if isinstance(facts_by_key[k]["value"], list) else str(facts_by_key[k]["value"]))
        for k in real_keys
    ).lower()
    for n in re.findall(r"\b\d{3,4}\b", value):
        if n not in cited_text:
            return False, f"number {n!r} in the correction is not found in its cited facts"
    return True, None


def apply_field_corrections(written: dict, packet: dict, base: dict) -> tuple[dict, dict]:
    """Returns (applied: {field: value}, rejected: {field: reason})."""
    applied, rejected = {}, {}
    for field, correction in (written.get("field_corrections") or {}).items():
        source_key = _CORRECTABLE_FIELD_SOURCE_KEY.get(field)
        if not source_key:
            rejected[field] = "not a correctable field"
            continue
        if packet["structured"].get(source_key) != "existing_catalog_value":
            rejected[field] = f"current value is sourced from {packet['structured'].get(source_key)!r}, " \
                               f"not existing_catalog_value — corrections only override unverified fields"
            continue
        ok, reason = validate_field_correction(correction, packet)
        if not ok:
            rejected[field] = reason
            continue
        value = str(correction["value"]).strip()
        base[field] = value
        if field == "date_display":
            base["creation_date"] = value
        applied[field] = value
    return applied, rejected


# ----------------------------------------------------------------------- header hygiene (round 7)
# NASA/space-agency Commons credit fields leak straight into the pre-reground catalog's agent_name
# unmangled — a multi-line credit blob ending in Commons' own "featured picture … nominate it"
# boilerplate (Ring Nebula), or "Image: \n\nNational Aeronautics and Space Administration (a U.S.
# federal government agency; https://…)" (Stephan's Quintet). This is landing-step sanitisation:
# deterministic, no model, applied to whatever ends up in the header regardless of its source.
_CREDIT_PREFIX_RE = re.compile(r"^\s*(image|credit)\s*:\s*", re.I)
_URL_RE = re.compile(r"https?://\S+")
_PAREN_WITH_URL_RE = re.compile(r"\([^()]*https?://[^()]*\)")
_ORG_ALIASES = [
    (re.compile(r"National Aeronautics and Space Administration", re.I), "NASA"),
    (re.compile(r"European Space Agency", re.I), "ESA"),
    (re.compile(r"Canadian Space Agency", re.I), "CSA"),
    (re.compile(r"Space Telescope Science Institute", re.I), "STScI"),
    (re.compile(r"European Southern Observatory", re.I), "ESO"),
    (re.compile(r"Jet Propulsion Laboratory", re.I), "JPL"),
    (re.compile(r"California Institute of Technology", re.I), "Caltech"),
]
_ORG_TOKEN_RE = re.compile(r"\b(NASA|ESA|STScI|ESO|JPL|Caltech|JAXA|Roscosmos|CSA|Hubble)\b")
_COMMONS_BOILERPLATE_RES = [
    re.compile(r"this is a featured picture on wikimedia commons[^.]*\.?", re.I),
    re.compile(r"if you have an image of (?:a )?similar or higher quality[^.]*\.?", re.I),
    re.compile(r"\bnominate it\b[^.]*\.?", re.I),
    re.compile(r"\bfeatured pictures?\b\s*(?:\([^)]*\))?", re.I),
]


# Round 8 #4: a credit blob can clean down to a stub too short or too empty of meaning to be a name.
_AGENT_STOPWORDS_ONLY_RE = re.compile(
    r"^(?:and|or|the|a|an|by|of|in|with|,|;|\.|-|\s)+$", re.I,
)


def _looks_like_garbage_agent_name(value: str) -> bool:
    v = (value or "").strip()
    if len(v) < 3:
        return True
    return bool(_AGENT_STOPWORDS_ONLY_RE.match(v))


def normalize_credit_text(text: str, max_len: int = 80, shorten_to_orgs: bool = True) -> str:
    """Strip Image:/Credit: prefixes, URLs, parenthetical URL asides, and Commons "featured
    picture"/"nominate it" boilerplate; collapse to the first real paragraph; if still too long,
    reduce to the leading organisation names (agent_name only — never for a title)."""
    if not text:
        return text
    t = text.strip()
    t = _CREDIT_PREFIX_RE.sub("", t)
    # Boilerplate commonly follows a blank line after the real credit — keep only the first paragraph.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]
    t = paragraphs[0] if paragraphs else t
    for alias_re, repl in _ORG_ALIASES:
        t = alias_re.sub(repl, t)
    t = _PAREN_WITH_URL_RE.sub("", t)
    t = _URL_RE.sub("", t)
    for pat in _COMMONS_BOILERPLATE_RES:
        t = pat.sub("", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+,", ",", t)          # a removed "(url)" often leaves "word , word"
    t = re.sub(r",\s*,", ",", t)         # two removed asides back-to-back -> double comma
    t = re.sub(r"\s+", " ", t).strip()
    # Trailing-punctuation cleanup (a dangling ", " or "; " left by a removed aside) only applies to
    # credit text, and NEVER eats a trailing period — round 7 first tried stripping "." here too and
    # it silently ate a legitimate abbreviation ("Univ. of Ariz." -> "Univ. of Ariz", helix-nebula-
    # 0012); a title/institution name can also legitimately end in one ("Jr.").
    if shorten_to_orgs:
        t = t.strip(" ,;-")
    if shorten_to_orgs and len(t) > max_len:
        orgs = list(dict.fromkeys(_ORG_TOKEN_RE.findall(t)))
        if orgs:
            t = ", ".join(orgs)
        else:
            t = t[:max_len].rsplit(" ", 1)[0].strip() + "…"
    return t


# ----------------------------------------------------------------------- input normalisation
def normalize_flags(raw_flags) -> tuple[list[str], str | None, int]:
    """Normalise a writer row's raw `flags` field into (flags, evidence, dropped_count).
    Each entry is normally a plain string flag, but a writer may instead submit an object
    like {"key": "identity_mismatch", "evidence": "..."} — normalise those to their "key"
    and keep any "evidence" text (joined if more than one object flag carries it). Entries
    that are neither a string nor an object with a usable string "key" are dropped and
    counted rather than silently ignored."""
    flags: list[str] = []
    evidence_parts: list[str] = []
    dropped = 0
    for f in raw_flags or []:
        if isinstance(f, str):
            key = f
        elif isinstance(f, dict):
            key = f.get("key")
            if not isinstance(key, str) or not key:
                dropped += 1
                continue
            ev = f.get("evidence")
            if isinstance(ev, str) and ev.strip():
                evidence_parts.append(ev.strip())
        else:
            dropped += 1
            continue
        if key not in flags:
            flags.append(key)
    evidence = " | ".join(evidence_parts) if evidence_parts else None
    return flags, evidence, dropped


def normalize_tags(written_tags, existing_tags) -> tuple[str | None, bool]:
    """Normalise a writer's submitted `tags` (a comma-separated string OR a list — writers
    submit both shapes) into the catalog's stored representation: a comma-separated string,
    de-duplicated case-insensitively (first occurrence wins), matching the existing tags'
    casing convention (lowercased only if the existing tags are already all-lowercase).
    Returns (normalized_string, rejected); rejected=True (normalized_string is None) means
    the submission produced an empty tag set and the caller should keep the existing value."""
    if isinstance(written_tags, str):
        raw = written_tags.split(",")
    elif isinstance(written_tags, list):
        raw = written_tags
    else:
        return None, True
    tags: list[str] = []
    seen: set[str] = set()
    for t in raw:
        t = str(t).strip()
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(t)
    if not tags:
        return None, True
    if isinstance(existing_tags, list):
        existing = [str(s).strip() for s in existing_tags if str(s).strip()]
    else:
        existing = [s.strip() for s in (existing_tags or "").split(",") if s.strip()]
    if existing and all(s == s.lower() for s in existing):
        tags = [t.lower() for t in tags]
    return ", ".join(tags), False


def run_import(batch_glob: str = "batch_*.jsonl", curated_path: str | Path | None = None,
               agent_overrides_path: str | Path | None = None):
    catalog = load_catalog()
    curated_protections = load_curated_protections(curated_path)
    agent_overrides = load_agent_overrides(agent_overrides_path)
    packet_index = json.loads(PACKET_INDEX_PATH.read_text()) if PACKET_INDEX_PATH.exists() else {}
    written_lines: dict[str, dict] = {}
    flags_dropped_total = 0
    for f in sorted(WRITTEN_DIR.glob(batch_glob)):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("key"):
                flags, evidence, dropped = normalize_flags(row.get("flags"))
                row["flags"] = flags
                row["_flag_evidence"] = evidence
                flags_dropped_total += dropped
                written_lines[row["key"]] = row

    import_report = {"passed": 0, "failed": 0, "no_submission": 0, "items": [], "flagged": [],
                      "existing_catalog_value_counts": {}, "identity_suspect_fields_dropped": [],
                      "medium_blanked": [], "header_hygiene_changes": [],
                      "flags_dropped": flags_dropped_total, "tags_rejected": [], "curated_conflicts": [],
                      "agent_overrides_applied": [], "agent_overrides_title_mismatch": []}
    by_collection_new: dict[str, dict[int, dict]] = {}

    for key, coll in packet_index.items():
        packet_path = PACKETS_DIR / coll / f"{key}.json"
        if not packet_path.exists():
            continue
        packet = json.loads(packet_path.read_text())
        idx = int(key.rsplit("-", 1)[-1])
        base = dict(catalog[coll][idx])

        protected_fields = set(curated_protections.get((coll, idx)) or set())

        # Round 13b (item 4): a manual agent_name override, applied only once its title is verified
        # against the CURRENT catalog value — a title that no longer matches means the underlying
        # catalog entry moved/changed since the override was written, so it's skipped, never guessed.
        override = agent_overrides.get((coll, idx))
        if override:
            if _norm(override["title"]) == _norm(base.get("title") or "") and is_cc_by_row(base):
                import_report.setdefault("cc_by_override_refused", []).append(
                    {"key": key, "collection": coll, "field": "agent_name"})
            elif _norm(override["title"]) == _norm(base.get("title") or ""):
                base["agent_name"] = override["agent_name"]
                protected_fields = protected_fields | {"agent_name"}
                import_report["agent_overrides_applied"].append({
                    "key": key, "collection": coll, "agent_name": override["agent_name"],
                })
            else:
                import_report["agent_overrides_title_mismatch"].append({
                    "key": key, "collection": coll, "override_title": override["title"],
                    "catalog_title": base.get("title"),
                })

        curated_snapshot = {f: base.get(f) for f in protected_fields}

        def _protect(field: str, by: str) -> None:
            """If `field` is curated-protected and this run just changed it, record the collision
            in import_report["curated_conflicts"] and restore the served-catalog value immediately —
            so every later step in this loop iteration sees (and can't further disturb) the curated
            value, not just the final write."""
            if field not in protected_fields:
                return
            curated_value = curated_snapshot[field]
            if base.get(field) != curated_value:
                import_report["curated_conflicts"].append({
                    "key": key, "collection": coll, "field": field,
                    "curated": curated_value, "would_have_been": base.get(field), "by": by,
                })
                base[field] = curated_value

        for f in ("medium", "date_display", "creation_date", "current_repository", "physical_dimensions"):
            if packet["structured"].get(f):
                base[f] = packet["structured"][f]
                _protect(f, f"structured:{packet['structured'].get(_FIELD_SOURCE_KEY.get(f, ''), 'unknown')}")
        # Round 5: agent_name_confirmed is only ever set (see resolve_structured_fields) when it's a
        # genuine gain (catalog had none) or agrees with the catalog's own spelling — a disagreement
        # is recorded as agent_name_disagreement + needs_review and NEVER reaches this point, so this
        # assignment can never write a bogus name.
        if packet["structured"].get("agent_name_confirmed"):
            base["agent_name"] = packet["structured"]["agent_name_confirmed"]
            _protect("agent_name", f"structured:{packet['structured'].get('agent_name_source', 'unknown')}")

        # Round 4 #3: how much of the catalog's structure is still just the old, unverified value —
        # counted for every packet regardless of whether a writer has submitted for it yet.
        for field, source_key in _CORRECTABLE_FIELD_SOURCE_KEY.items():
            if packet["structured"].get(source_key) == "existing_catalog_value":
                import_report["existing_catalog_value_counts"][field] = \
                    import_report["existing_catalog_value_counts"].get(field, 0) + 1

        written = written_lines.get(key)
        if not written:
            import_report["no_submission"] += 1
            import_report["items"].append({"key": key, "collection": coll, "status": "no_submission"})
        else:
            # Round 3 #6: a writer may report a catalog-integrity problem independent of pass/fail —
            # the image plausibly not matching the title (pilot found campfire-adirondacks-0015 shows a
            # hunter by tree roots; bermuda-settlers-0110 shows boars). This never blocks the narrative.
            flags = written.get("flags") or []
            flag_evidence = written.get("_flag_evidence")
            integrity = [f for f in flags if f in ("image_title_mismatch", "identity_mismatch")]
            if integrity:
                import_report["flagged"].append({"key": key, "collection": coll, "flags": integrity,
                                                 "title": written.get("title") or packet.get("title"),
                                                 "flag_evidence": flag_evidence})

            # Round 4 #1: a writer-proposed correction to a structured field, grounded in a cited
            # fact, accepted ONLY where the field's current value was never actually verified
            # (existing_catalog_value) — never lets a writer override a museum/Wikidata-sourced value.
            applied, rejected = apply_field_corrections(written, packet, base)
            if applied or rejected:
                import_report["items_with_corrections"] = import_report.get("items_with_corrections", 0) + 1
            for field in applied:
                _protect(field, "field_correction")
                if field == "date_display":
                    _protect("creation_date", "field_correction")

            # Round 4 addendum 2: a confirmed identity mismatch means the MATCH itself is suspect, not
            # just one field — hermit-thrush-0004's medium/date/repository all came from the wrong
            # Wikidata entity (Q64582791). Drop every structured field regardless of its source
            # (matched Wikidata/museum/Commons-of-the-match, or existing_catalog_value) unless an
            # accepted correction backs it; ship title + catalog artist only. Superset of round 4 #2
            # (which only dropped existing_catalog_value-sourced fields).
            blanked = []
            suspect_qids = []
            if "identity_mismatch" in flags:
                suspect_qids = _extract_match_qids(packet)
                for field in _ALL_STRUCTURED_FIELDS:
                    if field in applied:
                        continue
                    if base.get(field):
                        base[field] = ""
                        _protect(field, "identity_mismatch")
                        if field == "date_display":
                            base["creation_date"] = ""
                            _protect("creation_date", "identity_mismatch")
                        if not base.get(field):  # not restored by curated protection above
                            blanked.append(field)
                if blanked:
                    import_report["identity_suspect_fields_dropped"].append({
                        "key": key, "collection": coll, "fields_dropped": blanked, "match_qids": suspect_qids,
                    })

            # Round 5 addition: `medium_doubtful` — the image visibly contradicts an unverified medium
            # but no fact states the right one (Toulouse-Lautrec crayon/gouache sketches catalogued
            # "Oil on canvas"). Blank rather than guess; only when nothing already corrected it.
            if "medium_doubtful" in flags and "medium" not in applied \
                    and packet["structured"].get("medium_source") == "existing_catalog_value":
                base["medium"] = ""
                _protect("medium", "medium_doubtful")
                if not base.get("medium"):  # not restored by curated protection above
                    import_report["medium_blanked"].append({"key": key, "collection": coll})

            ok, reasons = validate_written_item(written, packet)
            if ok:
                base["description_narrative"] = written["description_narrative"]
                if written.get("tags"):
                    new_tags, tags_rejected = normalize_tags(written["tags"], base.get("tags"))
                    if tags_rejected:
                        import_report["tags_rejected"].append({"key": key, "collection": coll})
                    else:
                        base["tags"] = new_tags
                import_report["passed"] += 1
                import_report["items"].append({"key": key, "collection": coll, "status": "passed", "flags": flags,
                                                "flag_evidence": flag_evidence,
                                                "corrections_applied": applied, "corrections_rejected": rejected,
                                                "blanked_fields": blanked})
            else:
                import_report["failed"] += 1
                import_report["items"].append({"key": key, "collection": coll, "status": "failed", "reasons": reasons,
                                                "flags": flags, "flag_evidence": flag_evidence,
                                                "corrections_applied": applied, "corrections_rejected": rejected,
                                                "blanked_fields": blanked})
                # failing items are NOT silently templated — narrative/tags stay as the existing
                # catalog value; only the deterministic structured fields (+ any applied corrections /
                # blanking above) are applied.

        # Round 7 #1: header hygiene — applied unconditionally (even with no writer submission yet),
        # since the pollution is in the pre-reground catalog value itself, not something a writer
        # introduced. agent_name may be shortened to leading org names if still too long; title never is.
        for field, shorten in (("agent_name", True), ("current_repository", False), ("title", False)):
            if field == "agent_name" and is_cc_by_row(base):
                continue  # ADR-142: a CC BY credit is never "cleaned" (the restore below would undo it anyway)
            old_val = base.get(field)
            new_val = normalize_credit_text(old_val, shorten_to_orgs=shorten) if old_val else old_val
            if field == "agent_name" and new_val and _looks_like_garbage_agent_name(new_val):
                # Round 8 #4: a credit blob can clean down to a meaningless fragment (cosmic-reef-0020
                # "and" — a leftover conjunction from "NASA, ESA, and STScI"). Round 13b: never ship
                # that as an EMPTY agent_name either — a blank header field is worse than the original
                # unhygienic value, so leave the field untouched (keep old_val) instead of blanking it.
                new_val = old_val
            if new_val != old_val:
                base[field] = new_val
                _protect(field, "hygiene")
                if base.get(field) != old_val:  # not restored by curated protection above
                    import_report["header_hygiene_changes"].append({
                        "key": key, "collection": coll, "field": field, "old": old_val, "new": new_val,
                    })

        # ADR-142/ADR-145 belt-and-braces: whatever the steps above did, a CC BY row leaves with its
        # credit/licence/attribution fields exactly as the catalog had them.
        disturbed = restore_cc_by_fields(base, catalog[coll][idx])
        if disturbed:
            import_report.setdefault("cc_by_restored", []).append(
                {"key": key, "collection": coll, "fields": disturbed})

        by_collection_new.setdefault(coll, {})[idx] = base

    OUT_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    for coll, touched in by_collection_new.items():
        full = list(catalog[coll])
        for idx, item in touched.items():
            full[idx] = item
        (OUT_CATALOG_DIR / f"{coll}.json").write_text(json.dumps({"items": full}, indent=1, ensure_ascii=False))

    IMPORT_REPORT_PATH.write_text(json.dumps(import_report, indent=2))
    return import_report


# ----------------------------------------------------------------------- run
async def run(*, only_sample: bool, limit: int | None, collection: str | None,
              extra_facts_dir: str | Path | None = None):
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    catalog = load_catalog()
    targets: list[tuple[str, int]] = []
    if only_sample:
        sample = json.loads((AUDIT_DIR / "sample.json").read_text())
        targets = [(r["collection"], r["idx"]) for r in sample]
    else:
        colls = [collection] if collection else list(catalog.keys())
        for c in colls:
            for i in range(len(catalog[c])):
                targets.append((c, i))
    if limit:
        targets = targets[:limit]

    extra_facts = load_extra_facts(extra_facts_dir)
    extra_by_coll: dict[str, dict[int, dict]] = {}
    if extra_facts:
        for c in {c for c, _ in targets}:
            extra_by_coll[c] = index_extra_facts_by_catalog(extra_facts, catalog[c])

    sem = asyncio.Semaphore(4)
    model_sem = asyncio.Semaphore(8)
    report = {"collections": {}, "fallback_count": 0, "needs_review_count": 0, "matched": 0, "total": 0,
              "model_calls": 0}
    by_collection: dict[str, list] = {}

    async with httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True) as client:
        fx = Fetcher(client, sem)

        async def one(coll, idx):
            item = catalog[coll][idx]
            fact_path = FACTS_DIR / coll / f"{idx:04d}.json"
            fact_path.parent.mkdir(parents=True, exist_ok=True)
            extra_fact = extra_by_coll.get(coll, {}).get(idx)
            result = await process_item(fx, model_sem, item, coll, extra_fact=extra_fact)
            fact_path.write_text(json.dumps(result["bundle"], indent=2, default=str))
            return coll, result

        results = await asyncio.gather(*(one(c, i) for c, i in targets))

    for coll, result in results:
        by_collection.setdefault(coll, []).append(result["item"])
        st = result["stats"]
        cstat = report["collections"].setdefault(coll, {"total": 0, "matched": 0, "needs_review": 0, "fallback": 0, "facts_sum": 0})
        cstat["total"] += 1
        cstat["matched"] += int(st["matched"])
        cstat["needs_review"] += int(st["needs_review"])
        cstat["fallback"] += int(st["fallback_to_template"])
        cstat["facts_sum"] += st["n_facts"]
        report["total"] += 1
        report["matched"] += int(st["matched"])
        report["needs_review_count"] += int(st["needs_review"])
        report["fallback_count"] += int(st["fallback_to_template"])
        if not st["fallback_to_template"]:
            report["model_calls"] += 1

    for coll, items in by_collection.items():
        # Merge regenerated items back over the full collection (only-sample / --limit runs only touch
        # a subset; unresolved indices are written out unchanged so the file stays a valid full catalog).
        full = list(catalog[coll])
        touched = {idx: item for (c, idx), item in zip(targets, [r["item"] for _, r in results]) if c == coll}
        for idx, item in touched.items():
            full[idx] = item
        (OUT_CATALOG_DIR / f"{coll}.json").write_text(json.dumps({"items": full}, indent=1, ensure_ascii=False))

    REPORT_PATH.write_text(json.dumps(report, indent=2))
    return report


# ----------------------------------------------------------------------- museum-match recheck (round 4)
def _identity_suspect_keys_from_import_report() -> set[str]:
    """Best-effort, backward compatible: if import_report.json exists and names keys whose identity
    match was flagged suspect, force those into the recheck even if their resolved fields happen not
    to differ (addendum 2 — "include those QIDs in the match-verification re-check")."""
    if not IMPORT_REPORT_PATH.exists():
        return set()
    try:
        rep = json.loads(IMPORT_REPORT_PATH.read_text())
    except Exception:
        return set()
    return {e["key"] for e in (rep.get("identity_suspect_fields_dropped") or []) if e.get("key")}


async def run_recheck_museum_matches(*, limit: int | None = None, force_keys: set[str] | None = None) -> dict:
    """Re-resolve every item's facts bundle under the new museum_match_verified() gate and rewrite
    ONLY the fact+packet files whose resolved structured fields actually changed — everything else,
    including reground/written/, is left untouched. Lists the changed keys so writers know which ones
    to redo. `force_keys` (default: auto-loaded from import_report.json's identity-mismatch-flagged
    keys) are always reported even when unchanged, since their underlying Wikidata QID is suspect for
    reasons this pass can't detect on its own (a writer's visual confirmation, not a title mismatch)."""
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    PACKETS_DIR.mkdir(parents=True, exist_ok=True)
    packet_index = json.loads(PACKET_INDEX_PATH.read_text()) if PACKET_INDEX_PATH.exists() else {}
    catalog = load_catalog()
    items_to_check = list(packet_index.items())
    if limit:
        items_to_check = items_to_check[:limit]
    force_keys = force_keys if force_keys is not None else _identity_suspect_keys_from_import_report()

    sem = asyncio.Semaphore(4)

    async with httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True) as client:
        fx = Fetcher(client, sem)

        async def one(key, coll):
            idx = int(key.rsplit("-", 1)[-1])
            item = catalog[coll][idx]
            packet_path = PACKETS_DIR / coll / f"{key}.json"
            fact_path = FACTS_DIR / coll / f"{idx:04d}.json"
            old_packet = json.loads(packet_path.read_text()) if packet_path.exists() else {}
            old_structured = old_packet.get("structured") or {}
            old_qids = _extract_match_qids(old_packet)

            new_bundle = await build_facts_bundle(fx, item, coll)
            new_fields, needs_review, notes = resolve_structured_fields(new_bundle, item, coll)
            forced = key in force_keys
            if new_fields == old_structured and not forced:
                return None  # unchanged — nothing to rewrite

            fact_path.parent.mkdir(parents=True, exist_ok=True)
            fact_path.write_text(json.dumps(new_bundle, indent=2, default=str))
            preview = build_preview_from_master(coll, item, key, idx)
            if not preview:
                preview = await build_preview_from_url(client, sem, coll, item, key)
            new_packet = build_packet(key, coll, item, new_bundle, new_fields, needs_review, preview)
            packet_path.parent.mkdir(parents=True, exist_ok=True)
            packet_path.write_text(json.dumps(new_packet, indent=1, ensure_ascii=False, default=str))
            return {
                "key": key, "collection": coll, "title": item.get("title"), "forced_identity_suspect": forced,
                "old_medium": old_structured.get("medium"), "old_medium_source": old_structured.get("medium_source"),
                "new_medium": new_fields.get("medium"), "new_medium_source": new_fields.get("medium_source"),
                "old_date_display": old_structured.get("date_display"), "old_date_source": old_structured.get("date_source"),
                "new_date_display": new_fields.get("date_display"), "new_date_source": new_fields.get("date_source"),
                "old_match_qids": old_qids, "new_match_qids": _extract_match_qids(new_packet),
            }

        results = await asyncio.gather(*(one(k, c) for k, c in items_to_check))

    changed = [r for r in results if r]
    report = {"checked": len(items_to_check), "changed": len(changed),
              "forced_identity_suspect_count": len(force_keys), "items": changed}
    MUSEUM_MATCH_CHANGES_PATH.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    return report


# ----------------------------------------------------------------------- duplicate-image detection (round 4)
def run_duplicate_images() -> dict:
    """Hash every item's local pack master (preferred) or its preview (fallback) and group keys that
    share a hash — writers found byte-identical images filed under different catalog entries with
    different attributions (garden-wall-0052 vs -0160)."""
    packet_index = json.loads(PACKET_INDEX_PATH.read_text()) if PACKET_INDEX_PATH.exists() else {}
    catalog = load_catalog()
    by_hash: dict[str, list[dict]] = {}
    for key, coll in packet_index.items():
        idx = int(key.rsplit("-", 1)[-1])
        items = catalog.get(coll) or []
        item = items[idx] if idx < len(items) else {}
        local_file = _pack_master_for(coll, idx, item.get("title"))
        digest = None
        if local_file:
            src = ART_PACK_LIBRARY / local_file
            if src.exists():
                digest = hashlib.sha256(src.read_bytes()).hexdigest()
        if digest is None:
            preview = PREVIEWS_DIR / coll / f"{key}.jpg"
            if preview.exists():
                digest = hashlib.sha256(preview.read_bytes()).hexdigest()
        if digest is None:
            continue
        by_hash.setdefault(digest, []).append({
            "key": key, "collection": coll, "title": item.get("title"), "agent_name": item.get("agent_name"),
        })
    groups = [v for v in by_hash.values() if len(v) > 1]
    result = {"n_duplicate_groups": len(groups), "groups": groups}
    DUPLICATE_IMAGES_PATH.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    return result


# ----------------------------------------------------------------------- deferred drops (--mode apply-drops)
def run_apply_drops(drops_path: str | Path | None = None) -> dict:
    """Apply a set of previously-approved-but-deferred removals to OUT_CATALOG_DIR (the IMPORT's
    output — this never touches static/catalog directly). Removing an item shifts every later index
    in its collection, which is exactly why these were deferred rather than applied one at a time
    during curation review; so every drop is verified — title, and agent_name when the record names
    one — against the CURRENT item at its key's idx BEFORE anything is written, across every
    collection in this call, and a single mismatch aborts the whole call with nothing written. Only
    once every drop in every collection has verified does it remove them, per collection, in
    descending index order (so an earlier deletion in the same collection never invalidates a later
    one's already-verified index)."""
    drops_path = Path(drops_path) if drops_path else DEFAULT_DROPS_PATH
    drops = json.loads(Path(drops_path).read_text())

    by_collection: dict[str, list[dict]] = {}
    for d in drops:
        coll = d["collection"]
        idx = int(d["key"].rsplit("-", 1)[-1])
        by_collection.setdefault(coll, []).append({**d, "idx": idx})

    # Pass 1: load + verify every drop in every collection. Nothing is written or mutated here, so a
    # failure anywhere aborts with the catalog untouched.
    loaded: dict[str, tuple[Path, list]] = {}
    for coll, items in by_collection.items():
        path = OUT_CATALOG_DIR / f"{coll}.json"
        data = json.loads(path.read_text())
        full = data["items"]
        for d in items:
            idx = d["idx"]
            if idx >= len(full):
                raise ValueError(f"apply-drops abort: {d['key']!r} — idx {idx} out of range for "
                                 f"{coll!r} (has {len(full)} items); nothing written")
            actual = full[idx]
            if (actual.get("title") or "") != (d.get("title") or ""):
                raise ValueError(f"apply-drops abort: {d['key']!r} — title mismatch at {coll}[{idx}]: "
                                 f"expected {d.get('title')!r}, found {actual.get('title')!r}; nothing written")
            expected_agent = d.get("agent_name")
            if expected_agent and (actual.get("agent_name") or "") != expected_agent:
                raise ValueError(f"apply-drops abort: {d['key']!r} — agent_name mismatch at {coll}[{idx}]: "
                                 f"expected {expected_agent!r}, found {actual.get('agent_name')!r}; nothing written")
        loaded[coll] = (path, full)

    # Pass 2: every drop in every collection verified — now remove + write, matching OUT_CATALOG_DIR's
    # own existing format exactly (indent=1, ensure_ascii=False, {"items": [...]} — see run_import).
    report = {"dropped": [], "collections": {}}
    for coll, items in by_collection.items():
        path, full = loaded[coll]
        before = len(full)
        for d in sorted(items, key=lambda d: d["idx"], reverse=True):
            del full[d["idx"]]
            report["dropped"].append({"key": d["key"], "collection": coll, "title": d.get("title")})
        report["collections"][coll] = {"before": before, "after": len(full)}
        path.write_text(json.dumps({"items": full}, indent=1, ensure_ascii=False))

    DROPS_REPORT_PATH.write_text(json.dumps(report, indent=2))
    return report


# ----------------------------------------------------------------------- land re-grounded catalog (--mode land)
# Fields whose change is worth counting in the --dry-run summary — narrative/model-touched fields plus
# the two structured fields the writers most often correct, and title (a change here is a mismatch, not
# an expected edit — see the assertion below, but still worth surfacing in the per-field tally).
LAND_CHANGE_FIELDS = [
    "description_narrative", "tags", "medium", "date_display", "current_repository", "agent_name", "title",
]


def _static_collections(static_dir: Path) -> dict[str, dict]:
    """{collection: parsed static/catalog/<collection>.json} for every file there that is an actual
    served COLLECTION — a dict carrying an "items" key — excluding index.json / _pack_pins.json /
    _pending_artic.json, which live in the same directory but aren't collections."""
    out = {}
    for f in sorted(static_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and "items" in data:
            out[f.stem] = data
    return out


def _merge_landed_item(static_item: dict, reground_item: dict) -> dict:
    """The reground item's VALUES win throughout, but key ORDER follows the static item's for every
    key the two share; any key reground adds that static never had (e.g. current_repository) is
    appended at the end, in reground's own order."""
    merged = {}
    for key in static_item:
        if key in reground_item:
            merged[key] = reground_item[key]
    for key in reground_item:
        if key not in static_item:
            merged[key] = reground_item[key]
    restore_cc_by_fields(merged, static_item)   # ADR-142: the served CC BY credit is the truth, not reground's
    return merged


def run_land(*, static_dir: Path | None = None, drops_path: str | Path | None = None,
             dry_run: bool = False, collections: list[str] | None = None) -> dict:
    """Land OUT_CATALOG_DIR's re-grounded items into the served static/catalog/<collection>.json files,
    preserving each file's own top-level keys/order and each item's own key order (new keys appended).
    Reground is index-aligned to static EXCEPT the deferred drops already removed from reground's
    output, so item i there is matched back to static's pre_drop_index(i, drops for that collection).

    Refuses (writes nothing at all) if any collection's reground item count != static count minus that
    collection's drops, or if any static collection has no reground file. A title mismatch between a
    matched pair does NOT abort the run (static already carries curated titles, so a mismatch should be
    rare) — it's asserted, collected, and reported instead.

    `collections` (--collections cosmos): a SCOPED re-land. Only the listed collections are considered —
    every other collection's reground/static file is ignored and its served file is never rewritten, so
    it stays byte-identical. Within a listed collection, reground rows beyond static's count (minus
    drops) are APPENDED as new rows (new works added after the one-shot full land); fewer reground rows
    than expected still refuses. Without it the strict one-shot behaviour (exact counts) is unchanged.
    """
    static_dir = Path(static_dir) if static_dir is not None else (CATALOG_SRC_DIR or STATIC_CATALOG_DIR)
    drops_path = Path(drops_path) if drops_path is not None else DEFAULT_DROPS_PATH
    drops_map = load_deferred_drops(drops_path) if drops_path.exists() else {}

    static_by_coll = _static_collections(static_dir)
    reground_files = {f.stem: f for f in sorted(OUT_CATALOG_DIR.glob("*.json"))}

    scoped = bool(collections)
    if scoped:
        wanted = list(dict.fromkeys(collections))
        unknown = sorted(c for c in wanted if c not in static_by_coll or c not in reground_files)
        if unknown:
            return {"refused": True, "dry_run": dry_run, "collections": unknown,
                    "reason": "--collections names a collection with no static file or no reground file"}
        static_by_coll = {c: static_by_coll[c] for c in wanted}
        reground_files = {c: reground_files[c] for c in wanted}

    missing_reground = sorted(c for c in static_by_coll if c not in reground_files)
    if missing_reground:
        return {"refused": True, "dry_run": dry_run,
                "reason": "static collection(s) with no reground file", "collections": missing_reground}

    missing_static = sorted(c for c in reground_files if c not in static_by_coll)
    if missing_static:
        return {"refused": True, "dry_run": dry_run,
                "reason": "reground collection(s) with no static file", "collections": missing_static}

    # Pass 1: load every collection, verify counts. A single mismatch refuses the WHOLE call.
    loaded: dict[str, tuple[dict, list, list, list[int]]] = {}
    count_mismatches = []
    for coll, reground_path in reground_files.items():
        static_data = static_by_coll[coll]
        static_items = static_data["items"]
        reground_data = json.loads(reground_path.read_text())
        reground_items = reground_data["items"] if isinstance(reground_data, dict) else reground_data
        dropped = drops_map.get(coll, [])
        expected = len(static_items) - len(dropped)
        if len(reground_items) < expected or (len(reground_items) != expected and not scoped):
            count_mismatches.append({"collection": coll, "reground_count": len(reground_items),
                                      "static_count": len(static_items), "drops": len(dropped),
                                      "expected": expected})
            continue
        loaded[coll] = (static_data, static_items, reground_items, dropped)

    if count_mismatches:
        return {"refused": True, "dry_run": dry_run, "reason": "item count mismatch",
                "collections": count_mismatches}

    # Pass 2: every collection verified — build the merged output. Nothing written yet.
    report = {"refused": False, "dry_run": dry_run, "collections": {}, "title_mismatches": [], "cc_by_refused": []}
    plan: dict[str, tuple[Path, dict]] = {}
    for coll, (static_data, static_items, reground_items, dropped) in loaded.items():
        new_items = list(static_items)
        changed = dict.fromkeys(LAND_CHANGE_FIELDS, 0)
        for i, r_item in enumerate(reground_items[:len(static_items) - len(dropped)]):
            pre_idx = pre_drop_index(i, dropped)
            s_item = static_items[pre_idx]
            if (is_cc_by_row(s_item) or is_cc_by_row(r_item)) and r_item.get("source_url") != s_item.get("source_url"):
                # Index-paired to a DIFFERENT work (reordered catalog): never move a CC BY credit onto it, or off it.
                report["cc_by_refused"].append({"collection": coll, "pre_idx": pre_idx,
                                                "static_title": s_item.get("title"),
                                                "reground_title": r_item.get("title")})
                continue
            if (r_item.get("title") or "") != (s_item.get("title") or ""):
                report["title_mismatches"].append({
                    "collection": coll, "pre_idx": pre_idx,
                    "static_title": s_item.get("title"), "reground_title": r_item.get("title"),
                })
            for field in LAND_CHANGE_FIELDS:
                if s_item.get(field) != r_item.get(field):
                    changed[field] += 1
            new_items[pre_idx] = _merge_landed_item(s_item, r_item)
        n_matched = len(static_items) - len(dropped)
        appended = reground_items[n_matched:]   # only non-empty in a scoped re-land (count check above)
        # The deferred drops are approved removals: they must leave the served catalog too, not just
        # the import output (they were kept here by mistake on the first landing, 2026-09-27).
        dropped_set = set(dropped)
        new_items = [it for j, it in enumerate(new_items) if j not in dropped_set]
        new_items.extend(appended)

        new_data = dict(static_data)
        new_data["items"] = new_items
        plan[coll] = (static_dir / f"{coll}.json", new_data)
        report["collections"][coll] = {
            "items_before": len(static_items), "items_after": len(new_items), "changed": changed,
            "appended": len(appended),
        }

    if dry_run:
        for coll in sorted(plan):
            stat = report["collections"][coll]
            changed_str = ", ".join(f"{k}={v}" for k, v in stat["changed"].items() if v)
            print(f"{coll}: before={stat['items_before']} after={stat['items_after']} "
                  f"changed=[{changed_str}]")
        if report["title_mismatches"]:
            print(f"title mismatches: {len(report['title_mismatches'])}")
            for m in report["title_mismatches"]:
                print(f"  {m['collection']}[{m['pre_idx']}]: {m['static_title']!r} != {m['reground_title']!r}")
        return report

    for coll, (path, new_data) in plan.items():
        path.write_text(json.dumps(new_data, indent=1, ensure_ascii=False))

    # A landing that adds/removes rows must keep index.json's per-collection `count` true. Only landed
    # collections whose count actually changed are touched; nothing else in index.json changes.
    index_path = static_dir / "index.json"
    if plan and index_path.exists():
        index = json.loads(index_path.read_text())
        changed_idx = False
        for c in index.get("collections", []):
            if c.get("id") in plan and c.get("count") != len(plan[c["id"]][1]["items"]):
                c["count"] = len(plan[c["id"]][1]["items"])
                changed_idx = True
        if changed_idx:
            index_path.write_text(json.dumps(index, indent=1, ensure_ascii=False))
            report["index_counts_updated"] = True

    return report


# ----------------------------------------------------------------------- blank/near-uniform masters (round 10)
def _edge_flat_band_frac(im) -> tuple[float, str]:
    """A TRUNCATED pack master (partial real image, remainder filled with flat black/white) can look
    unremarkable in whole-image mean/std/modal stats (they blend the real content with the fill) — the
    McCandless spacewalk master that caught this had a noisy top third and a flat black bottom two
    thirds, mean 32 / std 49 / modal_frac 0.67, none of which trip near_uniform. Instead: walk inward
    from each of the four edges and measure the longest run of rows/columns that are each internally
    flat (max-min<=2) AND agree with each other (within ±2 of the first flat row/col's value) — a run
    anchored at an edge is a fill band, not coincidental local flatness in the middle of a photo.
    Returns (band_frac, edge) for whichever edge has the largest band, band_frac in [0, 1]."""
    w, h = im.size
    px = list(im.getdata())

    def row_minmax(y):
        vals = px[y * w:(y + 1) * w]
        return min(vals), max(vals)

    def col_minmax(x):
        vals = px[x::w]
        return min(vals), max(vals)

    def run_len(get, order):
        ref = None
        count = 0
        for i in order:
            lo, hi = get(i)
            if hi - lo > 2:
                break
            val = (lo + hi) / 2
            if ref is None:
                ref = val
            elif abs(val - ref) > 2:
                break
            count += 1
        return count

    bands = {
        "bottom": run_len(row_minmax, range(h - 1, -1, -1)) / h,
        "top": run_len(row_minmax, range(h)) / h,
        "left": run_len(col_minmax, range(w)) / w,
        "right": run_len(col_minmax, range(w - 1, -1, -1)) / w,
    }
    edge = max(bands, key=bands.get)
    return bands[edge], edge


def _image_grayscale_stats(path: Path, max_edge: int = 512) -> dict:
    """Open ONE file, decode as cheaply as possible at <=max_edge (Image.draft hints the JPEG decoder
    to downscale during decode; thumbnail() then finishes it), and return grayscale mean/stddev/modal
    fraction from the pixel histogram (no numpy dependency), plus the edge-anchored flat-band fraction
    (see _edge_flat_band_frac) for truncated-master detection."""
    with Image.open(path) as im:
        im.draft("L", (max_edge, max_edge))  # no-op / ignored for non-JPEG; safe either way
        im = im.convert("L")
        im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        hist = im.histogram()
        n = im.width * im.height
        if n == 0:
            return {"mean": 0.0, "std": 0.0, "modal_frac": 0.0, "band_frac": 0.0, "band_edge": None}
        mean = sum(i * c for i, c in enumerate(hist)) / n
        var = sum(((i - mean) ** 2) * c for i, c in enumerate(hist)) / n
        std = var ** 0.5
        modal_idx = max(range(256), key=lambda i: hist[i])
        lo, hi = max(0, modal_idx - 8), min(255, modal_idx + 8)
        modal_frac = sum(hist[lo:hi + 1]) / n
        band_frac, band_edge = _edge_flat_band_frac(im)
        return {"mean": mean, "std": std, "modal_frac": modal_frac,
                "band_frac": band_frac, "band_edge": band_edge}


def _blank_master_tags(stats: dict) -> list[str]:
    tags = []
    if stats["std"] < 6 or stats["modal_frac"] > 0.95:
        tags.append("near_uniform")
    if stats["mean"] > 245:
        tags.append("very_bright")
    if stats["mean"] < 10:
        tags.append("very_dark")
    if stats.get("band_frac", 0.0) >= 0.08:
        tags.append("truncated_band")
    return tags


def run_blank_masters(limit: int | None = None, collection: str | None = None,
                       max_workers: int = 4) -> dict:
    """For every served-catalog item with a resolved pack master (see _pack_master_for), open the
    master, downscale to <=512px, and flag near-uniform / very-bright / very-dark images — writers'
    placards were being grounded from masters that were blank or near-blank (e.g. an all-white scan)
    with no signal that anything was wrong. Tags, not a verdict: a legitimately dark deep-space photo
    (e.g. a McCandless spaceflight EVA shot) is expected to trip very_dark."""
    catalog = load_catalog()
    colls = [collection] if collection else list(catalog.keys())
    targets = [(c, i, catalog[c][i]) for c in colls for i in range(len(catalog[c]))]
    if limit:
        targets = targets[:limit]

    resolved: list[tuple[str, int, dict, str]] = []
    unresolved = 0
    for coll, idx, item in targets:
        filename = _pack_master_for(coll, idx, item.get("title"))
        if filename:
            resolved.append((coll, idx, item, filename))
        else:
            unresolved += 1

    def check(entry: tuple[str, int, dict, str]) -> dict | None:
        coll, idx, item, filename = entry
        try:
            stats = _image_grayscale_stats(ART_PACK_LIBRARY / filename)
        except Exception as e:
            logger.info(f"    · blank-master check failed for {coll}[{idx}] {filename}: {e}")
            return None
        tags = _blank_master_tags(stats)
        if not tags:
            return None
        return {
            "collection": coll, "idx": idx, "key": item_key(idx, item.get("title") or ""),
            "title": item.get("title"), "file": filename,
            "mean": stats["mean"], "std": stats["std"], "modal_frac": stats["modal_frac"],
            "band_frac": stats["band_frac"], "edge": stats["band_edge"], "tags": tags,
        }

    flagged: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for result in ex.map(check, resolved):
            if result:
                flagged.append(result)

    # most-suspicious first: whichever of "concentrated on one grey value" (near_uniform) or "large
    # edge-anchored flat band" (truncated_band) is more extreme, tie-broken by lowest spread.
    flagged.sort(key=lambda f: (-max(f["modal_frac"], f["band_frac"]), f["std"]))

    result = {"checked": len(resolved), "unresolved": unresolved, "flagged": flagged}
    BLANK_MASTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLANK_MASTERS_PATH.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    logger.info(f"blank-masters: checked {len(resolved)}, unresolved {unresolved}, flagged {len(flagged)}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["facts", "packets", "import", "recheck-museum", "duplicate-images",
                                        "blank-masters", "apply-drops", "land"],
                     default="packets",
                     help="facts: legacy single-pass (facts+template/model, round 1). "
                          "packets: write facts + writing packets + preview images + batches "
                          "(round 2, no API model — Claude subagents write the narratives). "
                          "import: read reground/written/batch_NN.jsonl, validate, land the catalog. "
                          "recheck-museum: re-verify museum matches (round 4), rewriting only the "
                          "fact+packet files whose structured fields changed; never touches written/. "
                          "duplicate-images: hash every item's master/preview and group shared hashes. "
                          "blank-masters: flag near-uniform/very-bright/very-dark resolved pack masters. "
                          "apply-drops: remove approved, previously-deferred drops from the IMPORT's "
                          "OUT_CATALOG_DIR (never static/catalog), verifying title/agent_name first. "
                          "land: write OUT_CATALOG_DIR's re-grounded items into static/catalog, "
                          "preserving static's top-level + item key order; use --dry-run to preview.")
    ap.add_argument("--only-sample", action="store_true", help="only the 150 audit-sample works")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--collection", default=None)
    ap.add_argument("--batch-size", type=int, default=100)
    ap.add_argument("--workdir", default=None,
                     help="override REGROUND_DIR for this run only (default: $REGROUND_DIR or "
                          "~/pieria-img/reground — never /tmp, which a reboot can wipe)")
    ap.add_argument("--curated", default=None,
                     help="import mode only: path to a hand-curated corrections file (applied.json "
                          "shape: [{collection, idx, changed_fields}]) whose listed header/structured "
                          "fields are protected from every downstream import override and restored to "
                          "the served-catalog value if disturbed (see import_report['curated_conflicts'])")
    ap.add_argument("--drops", default=None,
                     help="apply-drops/land mode: path to the approved deferred-drops file "
                          "(default: ~/pieria-img/curation/deferred_drops.json)")
    ap.add_argument("--catalog-dir", default=None,
                     help="catalog directory (<collection>.json files) every mode reads and --mode land "
                          "writes (default: the served static/catalog)")
    ap.add_argument("--collections", nargs="+", default=None,
                     help="land mode only: re-land just these collections (space/comma separated), "
                          "appending reground rows static doesn't have yet; every other collection "
                          "file is left untouched")
    ap.add_argument("--dry-run", action="store_true",
                     help="land mode only: print the per-collection summary and write nothing")
    ap.add_argument("--extra-facts", default=None,
                     help="facts/packets mode: a directory of {n, title, release_url, text, "
                          "credit_line} JSON records (e.g. NASA/STScI/JPL release text) matched to "
                          "catalog rows by release_url==attribution_url (fallback: normalized title) "
                          "and folded into that row's facts bundle as nasa.release_text[.N]/"
                          "nasa.release_url/nasa.credit")
    ap.add_argument("--agent-overrides", default=None,
                     help="import mode only: path to a hand-curated agent_name overrides file "
                          "(agent_overrides.json shape: [{collection, pre_idx, title, agent_name}]) — "
                          "genuine multi-creator collaborations round 13 stopped inferring automatically; "
                          "applied only once each entry's title is verified against the catalog, and "
                          "protected the same way --curated fields are (import_report["
                          "'agent_overrides_applied'/'agent_overrides_title_mismatch'])")
    args = ap.parse_args()

    if args.workdir:
        set_workdir(Path(args.workdir).expanduser())
    if args.catalog_dir:
        set_catalog_dir(args.catalog_dir)

    if args.mode == "import":
        report = run_import(curated_path=args.curated, agent_overrides_path=args.agent_overrides)
    elif args.mode == "apply-drops":
        report = run_apply_drops(drops_path=args.drops)
    elif args.mode == "land":
        colls = [c for part in (args.collections or []) for c in part.split(",") if c] or None
        report = run_land(drops_path=args.drops, dry_run=args.dry_run, collections=colls)
    elif args.mode == "packets":
        report = asyncio.run(run_packets(
            only_sample=args.only_sample, limit=args.limit, collection=args.collection,
            batch_size=args.batch_size, extra_facts_dir=args.extra_facts,
        ))
    elif args.mode == "recheck-museum":
        report = asyncio.run(run_recheck_museum_matches(limit=args.limit))
    elif args.mode == "duplicate-images":
        report = run_duplicate_images()
    elif args.mode == "blank-masters":
        report = run_blank_masters(limit=args.limit, collection=args.collection)
    else:
        report = asyncio.run(run(only_sample=args.only_sample, limit=args.limit, collection=args.collection,
                                  extra_facts_dir=args.extra_facts))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
