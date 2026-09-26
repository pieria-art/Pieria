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

import httpx
from dotenv import load_dotenv

load_dotenv()

from pathlib import Path

from PIL import Image

import ai_client
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
    load_catalog,
    resolve_work,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("reground")

REGROUND_DIR = AUDIT_DIR.parent / "reground"
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

ROOT = Path(__file__).resolve().parent.parent
ART_PACK_LIBRARY = ROOT / "art-pack" / "_Library"
ART_PACK_MANIFESTS = ROOT / "art-pack" / "_manifests"

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
    return out or None


async def _aic_record(fx: Fetcher, title: str) -> dict | None:
    """Art Institute of Chicago's public API — same shape as _museum_record's dict so it flows through
    the one fact-emission loop. `description`/`short_description` are CC BY 4.0 per AIC's own
    license_text (measured 2026-09-25: "The `description` field ... is licensed under ... CC-By"), NOT
    CC0 like the rest of the record — kept out of `facts`, returned under `_checkonly` instead."""
    body, err = await fx.get_json(AIC_SEARCH, {
        "q": title, "limit": 1,
        "fields": "id,title,date_display,medium_display,artist_display,description,short_description",
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
    checkonly = []
    for k in ("description", "short_description"):
        if d.get(k):
            checkonly.append(re.sub(r"<[^>]+>", "", str(d[k])).strip())
    out["_checkonly"] = checkonly
    return out


# A run of capitalised words ending in an institution keyword, optionally followed by "of <Place>" —
# e.g. "National Gallery of Art" out of a Credit sentence that doesn't match a known DOMAIN_INSTITUTIONS
# entry (round 3 #3: current_repository from generic Commons Credit/Institution text).
_INSTITUTION_PHRASE_RE = re.compile(
    r"\b((?:[A-Z][\w&.'-]*\s+){0,6}(?:Museum|Galler\w*|Librar\w*|Archive\w*|University|Institut\w*|"
    r"Foundation|Academy|Society)(?:\s+of\s+[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})?)\b"
)


def _extract_institution_phrase(text: str) -> str | None:
    if not text:
        return None
    m = _INSTITUTION_PHRASE_RE.search(text)
    return m.group(1).strip() if m else None


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
    r"\b(museum|gallery|librar|archive|university|institut|foundation|collection|academy|society)", re.I
)


def looks_like_institution_label(label: str) -> bool:
    if not label or _BARE_QID_RE.match(label.strip()):
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


# ----------------------------------------------------------------------- facts bundle
_MUSEUM_FACT_KEYS = ("objectDate", "medium", "culture", "classification", "creation_date",
                     "technique", "description", "did_you_know")


async def build_facts_bundle(fx: Fetcher, item: dict, collection: str | None = None) -> dict:
    """Resolve identity + assemble the typed facts bundle for one catalog item."""
    allow_day_precision = catalog_spec.kind_for(collection or "") in ("photo", "space")
    match = await resolve_work(fx, item)
    facts: list[dict] = []
    check_only: list[str] = []
    conflicts: list[dict] = []
    is_version_of = None
    wikipedia_lead = None
    commons_description = None
    creator_qid = None

    if match:
        wd = await _wikidata_full(fx, match["qid"], allow_day_precision)
        claims = wd.get("claims") or {}
        wd_url = f"https://www.wikidata.org/wiki/{match['qid']}"
        for key, labels in claims.items():
            facts.append(_fact(f"wikidata.{key}", labels, "Wikidata", wd_url, "CC0"))
        is_version_of = wd.get("is_version_of")
        creator_qid = wd.get("creator_qid")
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
    if "commons.wikimedia.org" in source_url:
        filename = urllib.parse.unquote(urllib.parse.urlparse(source_url).path.rsplit("/", 1)[-1])
        ext = await _commons_extmetadata(fx, filename)
        if ext:
            upload_year = _extract_year(ext.get("DateTime") or "")
            death_year = None
            for key, val in ext.items():
                if key == "ImageDescription":
                    check_only.append(val)
                    commons_description = val
                    continue
                if key == "DateTimeOriginal":
                    if death_year is None and creator_qid:
                        death_year = await _creator_death_year(fx, creator_qid)
                    if looks_like_capture_timestamp(val, upload_year, death_year):
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

    facts, conflicts = _detect_conflicts(facts)
    return {
        "match": match, "facts": facts, "conflicts": conflicts,
        "is_version_of": is_version_of, "check_only_texts": check_only,
        "wikipedia_lead": wikipedia_lead, "commons_description": commons_description,
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


def resolve_structured_fields(bundle: dict, existing_item: dict) -> tuple[dict, bool, list[str]]:
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

    def first_value(keys):
        for k, _ in keys:
            f = facts_by_key.get(k)
            if not f:
                continue
            v = f["value"]
            v = v[0] if isinstance(v, list) else v
            v = str(v).strip()
            if not v:
                continue
            yield v, f["source"]

    # medium
    for v, src in first_value(_FIELD_SOURCES["medium"]):
        fields["medium"] = v
        fields["medium_source"] = src
        break
    else:
        if existing_item.get("medium"):
            fields["medium"] = existing_item["medium"]
            fields["medium_source"] = "existing_catalog_value"
            notes.append("medium: no retrieved fact, kept existing catalog value")

    # date — accession-year guard applied before acceptance
    dropped_accession = False
    for v, src in first_value(_FIELD_SOURCES["date_display"]):
        if looks_like_accession_number(v):
            dropped_accession = True
            continue
        fields["date_display"] = v
        fields["creation_date"] = v
        fields["date_source"] = src
        break
    else:
        if existing_item.get("creation_date") and not looks_like_accession_number(existing_item["creation_date"]):
            fields["date_display"] = existing_item.get("date_display") or existing_item["creation_date"]
            fields["creation_date"] = existing_item["creation_date"]
            fields["date_source"] = "existing_catalog_value"
        elif existing_item.get("creation_date"):
            dropped_accession = True
    if dropped_accession:
        notes.append("date: dropped an accession/object-number-shaped candidate (not a creation date)")
        needs_review = needs_review or "date_display" not in fields

    # current repository — museum-API-sourced institution name trusted outright; a Wikidata P195
    # collection label only if it actually looks like an institution (never a bare QID, never a place).
    for key in ("museum.institution", "museum2.institution"):
        f = facts_by_key.get(key)
        if f and str(f["value"]).strip():
            fields["current_repository"] = str(f["value"]).strip()
            break
    else:
        f = facts_by_key.get("wikidata.collection")
        v = None
        if f:
            v = f["value"][0] if isinstance(f["value"], list) else f["value"]
        if v and looks_like_institution_label(str(v)):
            fields["current_repository"] = str(v).strip()
        else:
            if v:
                notes.append(f"current_repository: rejected non-institution/unresolved candidate {v!r}")
            # round 3 #3: last resort — a generic institution name lifted from Commons Credit/
            # Institution text, only when nothing more authoritative named one.
            cf = facts_by_key.get("commons.institution_credit")
            if cf and looks_like_institution_label(str(cf["value"])):
                fields["current_repository"] = str(cf["value"]).strip()
                fields["current_repository_source"] = "Commons Credit text"

    # agent name — round 5 bug fix: this MUST come only from the museum record's own creator field or
    # Wikidata P170, never from commons.Artist (routinely the photographer/uploader — "Didier
    # Descouens", "Elke Wetzig" — or even the holding INSTITUTION, e.g. "Rijksmuseum" on 100+ Hiroshige
    # prints; 133 packets had a structured artist disagreeing with the catalog for exactly this
    # reason). commons.Artist stays a fact for writers to see but is never used to set this field.
    agent_val, agent_src = None, None
    for key in ("museum.creators", "wikidata.creator"):
        f = facts_by_key.get(key)
        if not f:
            continue
        v = f["value"]
        v = str(v[0] if isinstance(v, list) else v).strip()
        if v:
            agent_val, agent_src = v, f["source"]
            break
    if agent_val:
        catalog_artist = (existing_item.get("agent_name") or "").strip()
        if not catalog_artist or catalog_artist.lower() in ("unknown artist", "unknown"):
            # nothing to disagree with — a real name where the catalog had none is a genuine gain.
            fields["agent_name_confirmed"] = agent_val
            fields["agent_name_source"] = agent_src
        elif _names_plausibly_match(catalog_artist, agent_val):
            # confirmed, not changed — keep the catalog's own spelling/form (e.g. "Rembrandt van
            # Rijn"), don't replace it with a shorter/differently-formatted museum/Wikidata string.
            fields["agent_name_confirmed"] = catalog_artist
            fields["agent_name_source"] = f"existing_catalog_value (confirmed by {agent_src})"
        else:
            # disagreement — never overwrite; flag for a human instead of shipping a guess.
            needs_review = True
            fields["agent_name_disagreement"] = agent_val
            notes.append(f"agent_name: {agent_src} says {agent_val!r}, catalog says "
                         f"{catalog_artist!r} — kept catalog value, needs_review")

    # physical dimensions
    h = facts_by_key.get("wikidata.height")
    w = facts_by_key.get("wikidata.width")
    if h or w:
        hv = (h["value"][0] if h else None)
        wv = (w["value"][0] if w else None)
        fields["physical_dimensions"] = " x ".join(x for x in (hv, wv) if x) + " cm"

    return fields, needs_review, notes


# ----------------------------------------------------------------------- narrative (model, grounded only)
_NARRATIVE_PROMPT = """You are writing a museum wall placard. Use ONLY the facts listed below — never \
add a claim (name, date, place, event, subject detail) that is not directly supported by them. If the \
facts are thin, write a SHORTER, more general placard about what is visibly depicted, the maker, and the \
date rather than inventing anything. You may use the "background text (context only)" ONLY to avoid \
stating something false — never as a source of new claims or phrasing; do not copy its wording.
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
        lines.append(f"- [{f['key']}] {v} (source: {f['source']}, {f['licence']})")
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
async def process_item(fx: Fetcher, model_sem: asyncio.Semaphore, item: dict, collection: str) -> dict:
    bundle = await build_facts_bundle(fx, item)
    fields, needs_review, notes = resolve_structured_fields(bundle, item)

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


def _manifest_local_file_map(collection: str) -> dict[str, str]:
    """title (normalised) -> art-pack/_Library filename, from that collection's signed manifest —
    read individually, never a recursive copy of the pack tree."""
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
                if lf and title:
                    out[_norm(title)] = lf
        except Exception as e:
            logger.info(f"    · manifest read failed for {collection}: {e}")
    _manifest_cache[collection] = out
    return out


def build_preview_from_master(collection: str, item: dict, key: str) -> Path | None:
    """Downscale from the local pack master if this work has one. Reads ONE file directly — never
    lists/copies the (16GB) _Library tree."""
    out_path = PREVIEWS_DIR / collection / f"{key}.jpg"
    if out_path.exists():
        return out_path
    local_file = _manifest_local_file_map(collection).get(_norm(item.get("title") or ""))
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


def build_batches(entries: list[dict], batch_size: int = 100) -> list[list[dict]]:
    """Group packet entries into ~batch_size chunks, collection-coherent where possible: sort by
    collection first so a chunk boundary only splits a collection when it doesn't divide evenly."""
    ordered = sorted(entries, key=lambda e: (e["collection"], e["key"]))
    return [ordered[i:i + batch_size] for i in range(0, len(ordered), batch_size)]


async def run_packets(*, only_sample: bool, limit: int | None, collection: str | None, batch_size: int = 100):
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
            if fact_path.exists() and packet_path.exists():
                bundle = json.loads(fact_path.read_text())  # resume: skip re-fetching
            else:
                bundle = await build_facts_bundle(fx, item, coll)
                fact_path.write_text(json.dumps(bundle, indent=2, default=str))
            fields, needs_review, notes = resolve_structured_fields(bundle, item)

            preview = build_preview_from_master(coll, item, key)
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


def _visual_claim_ok(text: str, title: str, facts: list[dict]) -> tuple[bool, str | None]:
    """A visual:true claim may describe only what is visibly depicted — no names/dates/places/events.
    Reject a number, or a capitalised word that isn't part of the title or a fact value."""
    if re.search(r"\d", text or ""):
        return False, "visual claim contains a number/year"
    allowed_words = set()
    for w in re.findall(r"[A-Za-z']+", title or ""):
        allowed_words.add(w.lower())
    for f in facts:
        v = f["value"]
        vals = v if isinstance(v, list) else [v]
        for val in vals:
            for w in re.findall(r"[A-Za-z']+", str(val)):
                allowed_words.add(w.lower())
    for m in re.finditer(r"\b[A-Z][a-zA-Z']*\b", text or ""):
        word = m.group(0)
        if word.lower() in _VISUAL_STOPWORDS:
            continue
        if re.match(r"^[A-Z][a-z']*$", word) and word.lower() not in allowed_words:
            # A capitalised word that isn't sentence-initial reads as a proper noun. Sentence-initial
            # capitals — the claim's first word, or the first word after . ! ? (claims can hold two
            # sentences) — are exempt.
            if not re.search(r"(^|[.!?][\"'”’)]*\s+)[\"'“‘(]*$", text[:m.start()]):
                return False, f"visual claim names a proper noun not in the title/facts: {word!r}"
    return True, None


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


def run_import(batch_glob: str = "batch_*.jsonl"):
    catalog = load_catalog()
    packet_index = json.loads(PACKET_INDEX_PATH.read_text()) if PACKET_INDEX_PATH.exists() else {}
    written_lines: dict[str, dict] = {}
    for f in sorted(WRITTEN_DIR.glob(batch_glob)):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("key"):
                written_lines[row["key"]] = row

    import_report = {"passed": 0, "failed": 0, "no_submission": 0, "items": [], "flagged": [],
                      "existing_catalog_value_counts": {}, "identity_suspect_fields_dropped": [],
                      "medium_blanked": []}
    by_collection_new: dict[str, dict[int, dict]] = {}

    for key, coll in packet_index.items():
        packet_path = PACKETS_DIR / coll / f"{key}.json"
        if not packet_path.exists():
            continue
        packet = json.loads(packet_path.read_text())
        idx = int(key.rsplit("-", 1)[-1])
        base = dict(catalog[coll][idx])
        for f in ("medium", "date_display", "creation_date", "current_repository", "physical_dimensions"):
            if packet["structured"].get(f):
                base[f] = packet["structured"][f]
        # Round 5: agent_name_confirmed is only ever set (see resolve_structured_fields) when it's a
        # genuine gain (catalog had none) or agrees with the catalog's own spelling — a disagreement
        # is recorded as agent_name_disagreement + needs_review and NEVER reaches this point, so this
        # assignment can never write a bogus name.
        if packet["structured"].get("agent_name_confirmed"):
            base["agent_name"] = packet["structured"]["agent_name_confirmed"]

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
            integrity = [f for f in flags if f in ("image_title_mismatch", "identity_mismatch")]
            if integrity:
                import_report["flagged"].append({"key": key, "collection": coll, "flags": integrity,
                                                 "title": written.get("title") or packet.get("title")})

            # Round 4 #1: a writer-proposed correction to a structured field, grounded in a cited
            # fact, accepted ONLY where the field's current value was never actually verified
            # (existing_catalog_value) — never lets a writer override a museum/Wikidata-sourced value.
            applied, rejected = apply_field_corrections(written, packet, base)
            if applied or rejected:
                import_report["items_with_corrections"] = import_report.get("items_with_corrections", 0) + 1

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
                        if field == "date_display":
                            base["creation_date"] = ""
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
                import_report["medium_blanked"].append({"key": key, "collection": coll})

            ok, reasons = validate_written_item(written, packet)
            if ok:
                base["description_narrative"] = written["description_narrative"]
                if written.get("tags"):
                    base["tags"] = written["tags"]
                import_report["passed"] += 1
                import_report["items"].append({"key": key, "collection": coll, "status": "passed", "flags": flags,
                                                "corrections_applied": applied, "corrections_rejected": rejected,
                                                "blanked_fields": blanked})
            else:
                import_report["failed"] += 1
                import_report["items"].append({"key": key, "collection": coll, "status": "failed", "reasons": reasons,
                                                "flags": flags, "corrections_applied": applied,
                                                "corrections_rejected": rejected, "blanked_fields": blanked})
                # failing items are NOT silently templated — narrative/tags stay as the existing
                # catalog value; only the deterministic structured fields (+ any applied corrections /
                # blanking above) are applied.

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
async def run(*, only_sample: bool, limit: int | None, collection: str | None):
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
            result = await process_item(fx, model_sem, item, coll)
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
            new_fields, needs_review, notes = resolve_structured_fields(new_bundle, item)
            forced = key in force_keys
            if new_fields == old_structured and not forced:
                return None  # unchanged — nothing to rewrite

            fact_path.parent.mkdir(parents=True, exist_ok=True)
            fact_path.write_text(json.dumps(new_bundle, indent=2, default=str))
            preview = build_preview_from_master(coll, item, key)
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
        local_file = _manifest_local_file_map(coll).get(_norm(item.get("title") or ""))
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["facts", "packets", "import", "recheck-museum", "duplicate-images"],
                     default="packets",
                     help="facts: legacy single-pass (facts+template/model, round 1). "
                          "packets: write facts + writing packets + preview images + batches "
                          "(round 2, no API model — Claude subagents write the narratives). "
                          "import: read reground/written/batch_NN.jsonl, validate, land the catalog. "
                          "recheck-museum: re-verify museum matches (round 4), rewriting only the "
                          "fact+packet files whose structured fields changed; never touches written/. "
                          "duplicate-images: hash every item's master/preview and group shared hashes.")
    ap.add_argument("--only-sample", action="store_true", help="only the 150 audit-sample works")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--collection", default=None)
    ap.add_argument("--batch-size", type=int, default=100)
    args = ap.parse_args()

    if args.mode == "import":
        report = run_import()
    elif args.mode == "packets":
        report = asyncio.run(run_packets(
            only_sample=args.only_sample, limit=args.limit, collection=args.collection,
            batch_size=args.batch_size,
        ))
    elif args.mode == "recheck-museum":
        report = asyncio.run(run_recheck_museum_matches(limit=args.limit))
    elif args.mode == "duplicate-images":
        report = run_duplicate_images()
    else:
        report = asyncio.run(run(only_sample=args.only_sample, limit=args.limit, collection=args.collection))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
