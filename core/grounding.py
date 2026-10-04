"""Grounded placards (ADR-140, ADR-148 "runtime grounding allowed").

The reusable core of the offline pack pipeline (`tools/reground_placards.py`, `tools/audit_placards.py`),
usable at runtime for works a user adds after install (Discover). Same contract as the offline tool:

* identity is RESOLVED, never guessed (museum accession / Commons file / title+creator -> Wikidata QID);
* structured fields (medium, date, repository, dimensions) come DETERMINISTICALLY from records
  (museum record > Wikidata), never from the model — a model value contradicting a record is overridden;
* the model writes prose from a facts bundle only, and its claims are validated against that bundle
  (cites real facts; years/numbers appear in the cited facts; no 8-word copy runs of paraphrase-only
  or check-only text — Wikipedia's lead is CC BY-SA, so it is context for fact-checking, never a source
  of wording);
* reject -> ONE retry with the violations listed -> else a minimal factual placard built from the records,
  never invented text.

Network: every call goes through `RuntimeFetcher` (descriptive UA, timeouts, 429/5xx backoff, a minimum
request interval). Failures never raise out of `ground_work`; the caller falls back to its old behaviour.
`ai_client.chat` is sync, so it runs via `asyncio.to_thread`; await `ground_work` from a background task
that owns its own session (rule 6).

The first part of this file is shared verbatim with the offline tools (moved here, re-imported there).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
import urllib.parse
from typing import Protocol

import httpx

import ai_client
from config import SD_USER_AGENT

logger = logging.getLogger("artwork-display-api.grounding")


class Fetcher(Protocol):
    """What the resolution code needs from an HTTP layer: the offline tools' cached Fetcher and the
    runtime `RuntimeFetcher` below both satisfy it. Each returns (body | None, error | None)."""
    async def get_json(self, url, params=None): ...
    async def get_text(self, url, params=None): ...


# =========================================================================================
# Shared with tools/audit_placards.py + tools/reground_placards.py (moved, not duplicated)
# =========================================================================================

# --------------------------------------------------------------------------- small helpers
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    s = re.sub(r"^(the|a|an)\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _surname(name: str) -> str:
    parts = (name or "").split()
    return _norm(parts[-1]) if parts else ""


# --------------------------------------------------------------------------- wikidata resolution
async def _label_for_qid(fx: Fetcher, qid: str) -> str:
    body, _ = await fx.get_json(WD_API, {
        "action": "wbgetentities", "ids": qid, "props": "labels", "languages": "en", "format": "json",
    })
    if not body:
        return qid
    ent = (body.get("entities") or {}).get(qid) or {}
    lab = ((ent.get("labels") or {}).get("en") or {}).get("value")
    return lab or qid


async def _sparql_p18(fx: Fetcher, filename: str) -> list[str]:
    # WDQS represents P18 (commonsMedia) as the Special:FilePath URI, not a bare string literal —
    # matching against that URI form is what actually hits the index (a literal-string FILTER times
    # out unindexed). Encoded exactly like the catalog's own source_url (spaces as %20).
    uri = f"http://commons.wikimedia.org/wiki/Special:FilePath/{urllib.parse.quote(filename)}"
    q = f"SELECT ?item WHERE {{ ?item wdt:P18 <{uri}> . }}"
    body, err = await fx.get_json(WD_SPARQL, {"query": q, "format": "json"})
    if not body:
        return []
    rows = body.get("results", {}).get("bindings", [])
    return [r["item"]["value"].rsplit("/", 1)[-1] for r in rows]


async def _commons_structured(fx: Fetcher, filename: str) -> tuple[str | None, str]:
    """Try the Commons file's structured data for P6243 (digital representation of), then P180
    (depicts). Returns (qid_or_None, method_note)."""
    body, err = await fx.get_json(COMMONS_API, {
        "action": "wbgetentities", "sites": "commonswiki", "titles": f"File:{filename}",
        "props": "claims", "format": "json",
    })
    if not body:
        return None, f"commons_fetch_error:{err}"
    entities = body.get("entities") or {}
    ent = next(iter(entities.values()), {}) if entities else {}
    claims = ent.get("claims") or {}
    for prop, note in (("P6243", "commons P6243 digital-representation-of"), ("P180", "commons P180 depicts")):
        vals = claims.get(prop) or []
        qids = []
        for c in vals:
            snak = c.get("mainsnak", {}).get("datavalue", {}).get("value", {})
            if isinstance(snak, dict) and snak.get("id"):
                qids.append(snak["id"])
        if len(qids) == 1:
            return qids[0], note
    return None, "commons_no_structured_match"


async def _search_and_verify(fx: Fetcher, title: str, artist: str) -> tuple[str | None, str, str]:
    """wbsearchentities on title; accept only a candidate whose P170 creator label matches the
    catalog's artist surname. Returns (qid_or_None, method, confidence)."""
    body, err = await fx.get_json(WD_API, {
        "action": "wbsearchentities", "search": title, "language": "en", "type": "item",
        "limit": 6, "format": "json",
    })
    if not body:
        return None, f"search_fetch_error:{err}", "none"
    cands = body.get("search") or []
    if not cands:
        return None, "search_no_candidates", "none"
    target_surname = _surname(artist)
    for c in cands:
        qid = c["id"]
        ebody, _ = await fx.get_json(WD_API, {
            "action": "wbgetentities", "ids": qid, "props": "claims|labels", "languages": "en", "format": "json",
        })
        if not ebody:
            continue
        ent = (ebody.get("entities") or {}).get(qid) or {}
        creators = (ent.get("claims") or {}).get("P170") or []
        for cl in creators:
            cqid = cl.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
            if not cqid:
                continue
            clabel = await _label_for_qid(fx, cqid)
            if target_surname and target_surname in _surname(clabel):
                return qid, "wbsearchentities + creator-verified", "medium"
    return None, "search_candidates_no_creator_match", "none"


async def resolve_work(fx: Fetcher, item: dict) -> dict:
    source_url = item.get("source_url") or ""
    title = item.get("title") or ""
    artist = item.get("agent_name") or ""

    if "commons.wikimedia.org" in source_url:
        parsed = urllib.parse.urlparse(source_url)
        raw_name = parsed.path.rsplit("/", 1)[-1]
        filename = urllib.parse.unquote(raw_name)
        qids = await _sparql_p18(fx, filename)
        if len(qids) == 1:
            return {"qid": qids[0], "method": "P18 exact commons-file match", "confidence": "high"}
        if len(qids) > 1:
            return {"qid": qids[0], "method": "P18 match (ambiguous, multiple items)", "confidence": "medium"}
        qid, note = await _commons_structured(fx, filename)
        if qid:
            return {"qid": qid, "method": note, "confidence": "high"}

    qid, method, conf = await _search_and_verify(fx, title, artist)
    if qid:
        return {"qid": qid, "method": method, "confidence": conf}
    return None


async def _wikipedia_lead(fx: Fetcher, enwiki_title: str) -> str | None:
    body, err = await fx.get_json(WP_SUMMARY.format(urllib.parse.quote(enwiki_title)))
    if not body:
        return None
    extract = body.get("extract") or ""
    return extract[:1500] if extract else None


WD_API = "https://www.wikidata.org/w/api.php"


WD_SPARQL = "https://query.wikidata.org/sparql"


COMMONS_API = "https://commons.wikimedia.org/w/api.php"


WP_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"


# ----------------------------------------------------------------------- accession-year guard
# Accession/object numbers look like "1940.116", "2019.34.1", "78.PA.1" — a leading 4-digit "year"
# segment followed by a dotted registrar suffix. A bare "1940" or "c. 1874" is NOT accession-shaped.
_ACCESSION_RE = re.compile(r"^\s*\d{2,4}\s*\.\s*\d+(\s*\.\s*\d+)*\s*$")


def looks_like_accession_number(value: str) -> bool:
    """True when `value` is shaped like a museum accession/object number rather than a date."""
    if not value:
        return False
    return bool(_ACCESSION_RE.match(str(value).strip()))


# ----------------------------------------------------------------------- fact record helpers
def _fact(key, value, source, source_url, licence):
    return {"key": key, "value": value, "source": source, "source_url": source_url, "licence": licence}


# ----------------------------------------------------------------------- current_repository validity
# Never a bare unresolved QID, and never a P276 "location" (that property mixes holding institutions
# with depicted/creation PLACES — e.g. "Moon" for an Apollo photo). Only P195 collection labels that
# actually look like an institution are accepted; a museum-API-sourced institution name is trusted
# outright (it came from that institution's own record, not a generic Wikidata place property).
_BARE_QID_RE = re.compile(r"^Q\d+$")


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


_COPY_RUN_WORDS = 8   # a narrative may not reproduce this many consecutive words of a paraphrase-only fact


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


# Wikidata properties consulted for the facts bundle. English-labelled claims only.
CLAIM_PROPS = {
    "P170": "creator", "P571": "inception", "P186": "made_from_material", "P136": "genre",
    "P135": "movement", "P195": "collection", "P276": "location", "P180": "depicts",
    "P2048": "height", "P2049": "width",
}


# "is a version of" signals — never guessed, only reported when Wikidata states one of these.
VERSION_PROPS = {"P1877": "after a work by", "P144": "based on", "P629": "edition or translation of"}


WD_API = "https://www.wikidata.org/w/api.php"


COMMONS_API = "https://commons.wikimedia.org/w/api.php"


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


# =========================================================================================
# Runtime-only: fetcher, museum-record normaliser, identity, facts, fields, prose, orchestration
# =========================================================================================

# Fixed hosts only (Wikidata / Commons / Wikipedia) — nothing here is a user-influenced URL, so the
# normal client is fine (core/safe_http is for federation-style third-party URLs, rule 14).
_SLEEP = asyncio.sleep          # indirection so tests don't really sleep
_MIN_INTERVAL_S = 0.3           # polite spacing between requests to the shared Wikimedia hosts
_MAX_ATTEMPTS = 3
_REQ_TIMEOUT_S = 15.0


class RuntimeFetcher:
    """Same `get_json/get_text` surface as tools.audit_placards.Fetcher (so the shared resolution code
    runs unchanged), minus the disk cache: descriptive UA, per-request timeout, bounded 429/5xx backoff
    (honours Retry-After, capped), a minimum interval between requests. `errors` counts requests that
    ultimately failed so the caller can tell "Wikidata is down" from "no match"."""

    def __init__(self, client: httpx.AsyncClient, *, min_interval: float | None = None,
                 max_attempts: int | None = None):
        self.client = client
        self.min_interval = _MIN_INTERVAL_S if min_interval is None else min_interval
        self.max_attempts = _MAX_ATTEMPTS if max_attempts is None else max_attempts
        self.errors = 0
        self.ok = 0
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def get_json(self, url, params=None):
        return await self._req(url, params, want_json=True)

    async def get_text(self, url, params=None):
        return await self._req(url, params, want_json=False)

    async def _req(self, url, params, want_json):
        err = "unknown_error"
        for attempt in range(self.max_attempts):
            async with self._lock:           # serialises + spaces requests: the rate limit
                wait = self.min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    await _SLEEP(wait)
                self._last = time.monotonic()
                try:
                    r = await self.client.get(url, params=params, timeout=_REQ_TIMEOUT_S)
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    r, err = None, f"transport_error:{type(e).__name__}"
            if r is None:
                await _SLEEP(min(2.0 * (attempt + 1), 10.0))
                continue
            if r.status_code == 200:
                try:
                    body = r.json() if want_json else r.text
                except ValueError:
                    self.errors += 1
                    return None, "bad_body"
                self.ok += 1
                return body, None
            err = f"http_{r.status_code}"
            if r.status_code in (429, 500, 502, 503, 504):
                backoff = 2.0 * (attempt + 1)
                try:
                    backoff = max(backoff, float(r.headers.get("retry-after", 0) or 0))
                except ValueError:
                    pass
                await _SLEEP(min(backoff, 10.0))
                continue
            break
        self.errors += 1
        return None, err


# ----------------------------------------------------------------------- museum record normaliser
# Discover stores the raw museum API JSON in `context_hints`; normalise the few sources whose
# accession number + structured fields we can read reliably. Free-text description fields are
# deliberately NOT lifted (licence varies by field; the prose comes from structured facts only).
def _first(*vals):
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def museum_record_from_hints(hints, source_api: str | None = None) -> dict | None:
    """Raw scout JSON (str or dict) -> {api, institution, inst_keyword, accession, title, artist, date,
    medium, dimensions, culture, url, licence} or None when the source/shape isn't recognised."""
    if isinstance(hints, str):
        try:
            hints = json.loads(hints)
        except ValueError:
            return None
    if not isinstance(hints, dict):
        return None
    src = (source_api or "").lower()
    if "metropolitan" in src or (not src and "accessionNumber" in hints):
        return {
            "api": "Met Collection API", "institution": "The Metropolitan Museum of Art",
            "inst_keyword": "metropolitan", "accession": hints.get("accessionNumber"),
            "title": hints.get("title"), "artist": hints.get("artistDisplayName"),
            "date": hints.get("objectDate"), "medium": hints.get("medium"),
            "dimensions": hints.get("dimensions"), "culture": hints.get("culture"),
            "url": hints.get("objectURL") or "https://www.metmuseum.org/art/collection", "licence": "CC0",
        }
    if "art institute" in src or (not src and "main_reference_number" in hints):
        return {
            "api": "Art Institute of Chicago API", "institution": "Art Institute of Chicago",
            "inst_keyword": "chicago", "accession": hints.get("main_reference_number"),
            "title": hints.get("title"), "artist": hints.get("artist_title"),
            "date": hints.get("date_display"), "medium": hints.get("medium_display"),
            "dimensions": hints.get("dimensions"), "culture": hints.get("place_of_origin"),
            "url": f"https://api.artic.edu/api/v1/artworks/{hints.get('id')}", "licence": "CC0",
        }
    if "cleveland" in src or (not src and "accession_number" in hints):
        creators = hints.get("creators") or []
        artist = (creators[0] or {}).get("description") if creators and isinstance(creators[0], dict) else None
        return {
            "api": "Cleveland Open Access API", "institution": "Cleveland Museum of Art",
            "inst_keyword": "cleveland", "accession": hints.get("accession_number"),
            "title": hints.get("title"), "artist": artist,
            "date": hints.get("creation_date"), "medium": hints.get("technique"),
            "dimensions": hints.get("measurements"), "culture": _first(*(hints.get("culture") or [None])),
            "url": hints.get("url") or f"https://openaccess-api.clevelandart.org/api/artworks/{hints.get('id')}",
            "licence": "CC0",
        }
    if "harvard" in src:
        people = hints.get("people") or []
        return {
            "api": "Harvard Art Museums API", "institution": "Harvard Art Museums",
            "inst_keyword": "harvard", "accession": hints.get("objectnumber"),
            "title": hints.get("title"),
            "artist": (people[0] or {}).get("name") if people and isinstance(people[0], dict) else None,
            "date": hints.get("dated"), "medium": hints.get("medium"), "dimensions": hints.get("dimensions"),
            "culture": hints.get("culture"), "url": hints.get("url") or "https://harvardartmuseums.org",
            "licence": "museum record",
        }
    return None


# ----------------------------------------------------------------------- identity
async def resolve_identity(fx, *, title: str, artist: str, rec: dict | None = None,
                           source_url: str | None = None) -> dict | None:
    """museum record -> Wikidata QID via accession/inventory number (P217, collection label must match
    the institution), else Commons file (P18 / structured data), else title + creator-verified search.
    Returns {qid, method, confidence} or None. Never guesses: every non-accession path is creator-checked
    inside `resolve_work`."""
    if rec and rec.get("accession") and rec.get("inst_keyword"):
        acc = re.sub(r'["\\\r\n]', "", str(rec["accession"])).strip()
        if acc:
            qid = await _wikidata_by_accession(fx, acc, rec["inst_keyword"])
            if qid:
                return {"qid": qid, "method": "museum accession -> Wikidata P217/P195", "confidence": "high"}
    return await resolve_work(fx, {"source_url": source_url or "", "title": title or "",
                                   "agent_name": artist or ""})


# ----------------------------------------------------------------------- facts bundle
_COMMONS_FACT_KEYS = ("LicenseShortName", "Artist", "Credit")   # short factual strings; description is check-only


async def _commons_file_meta(fx, filename: str) -> dict | None:
    body, _err = await fx.get_json(COMMONS_API, {
        "action": "query", "titles": f"File:{filename}", "prop": "imageinfo",
        "iiprop": "extmetadata", "format": "json",
    })
    pages = ((body or {}).get("query") or {}).get("pages") or {}
    page = next(iter(pages.values()), {}) if pages else {}
    meta = ((page.get("imageinfo") or [{}])[0]).get("extmetadata") or {}
    out = {}
    for k in _COMMONS_FACT_KEYS + ("ImageDescription",):
        v = (meta.get(k) or {}).get("value")
        if v:
            out[k] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(v))).strip()[:300]
    return out or None


def _surname_match(artist: str, labels) -> bool:
    s = _surname(artist)
    return bool(s) and any(s in _norm(str(lab)) for lab in labels)


async def build_runtime_bundle(fx, *, title: str, artist: str, rec: dict | None,
                               source_url: str | None) -> dict:
    """Typed facts bundle (same shape the offline tool's prompt/validators consume):
    {match, facts, is_version_of, check_only_texts, wikipedia_lead}."""
    match = await resolve_identity(fx, title=title, artist=artist, rec=rec, source_url=source_url)
    facts: list[dict] = []
    check_only: list[str] = []
    is_version_of = None
    wikipedia_lead = None

    if match:
        wd = await _wikidata_full(fx, match["qid"])
        claims = wd.get("claims") or {}
        wd_url = f"https://www.wikidata.org/wiki/{match['qid']}"
        # A creator-verified search / P18 hit that names someone else is a different work: drop it.
        if (match["confidence"] != "high" and artist and claims.get("creator")
                and not _surname_match(artist, claims["creator"])):
            logger.info("[grounding] identity rejected for %r: Wikidata creator %s != %r",
                        title, claims["creator"], artist)
            match, claims, wd = None, {}, {}
        for key, labels in claims.items():
            facts.append(_fact(f"wikidata.{key}", labels, "Wikidata", wd_url, "CC0"))
        if wd.get("based_on_theme"):
            facts.append(_fact("wikidata.based_on_theme", wd["based_on_theme"]["of_label"],
                               "Wikidata", wd_url, "CC0"))
        is_version_of = wd.get("is_version_of")
        # Wikipedia lead = CHECK-ONLY (CC BY-SA): only trusted off a high-confidence identity.
        if match and wd.get("enwiki_title") and match["confidence"] == "high":
            lead = await _wikipedia_lead(fx, wd["enwiki_title"])
            if lead:
                check_only.append(lead)
                wikipedia_lead = lead

    if rec:
        for key, val in (("objectDate", rec.get("date")), ("medium", rec.get("medium")),
                         ("dimensions", rec.get("dimensions")), ("culture", rec.get("culture"))):
            if val:
                facts.append(_fact(f"museum.{key}", str(val), rec["api"], rec["url"], rec.get("licence", "CC0")))
        if rec.get("artist"):
            facts.append(_fact("museum.creators", [str(rec["artist"])], rec["api"], rec["url"], rec.get("licence", "CC0")))
        facts.append(_fact("museum.institution", rec["institution"], rec["api"], rec["url"], rec.get("licence", "CC0")))

    if source_url and "commons.wikimedia.org" in source_url:
        filename = urllib.parse.unquote(urllib.parse.urlparse(source_url).path.rsplit("/", 1)[-1])
        meta = await _commons_file_meta(fx, filename)
        for key, val in (meta or {}).items():
            if key == "ImageDescription":
                check_only.append(val)          # CC BY-SA free text: context only
            else:
                facts.append(_fact(f"commons.{key}", val, "Wikimedia Commons", source_url, "CC BY-SA"))

    return {"match": match, "facts": facts, "is_version_of": is_version_of,
            "check_only_texts": check_only, "wikipedia_lead": wikipedia_lead}


# ----------------------------------------------------------------------- deterministic structured fields
_INSTITUTION_RE = re.compile(r"\b(museum|gallery|librar|archive|institute|collection|academy)", re.I)


def _fv(bundle: dict, key: str):
    for f in bundle["facts"]:
        if f["key"] == key:
            return f["value"]
    return None


def _one(v) -> str | None:
    if isinstance(v, list):
        v = v[0] if v else None
    v = str(v).strip() if v not in (None, "") else None
    return v or None


def resolve_fields(bundle: dict) -> dict:
    """Structured fields from RECORDS only (museum record > Wikidata). Absent -> key omitted, so the
    caller leaves the existing value; the model never supplies any of these (ADR-140). An accession
    number in a date slot is refused (the 1940.116 -> '1940' bug)."""
    out: dict = {}
    medium = _one(_fv(bundle, "museum.medium"))
    if not medium:
        mats = _fv(bundle, "wikidata.made_from_material")
        medium = ", ".join(mats) if isinstance(mats, list) and mats else None
    if medium:
        out["medium"] = medium
    date = _one(_fv(bundle, "museum.objectDate"))
    if date and looks_like_accession_number(date):
        date = None
    date = date or _one(_fv(bundle, "wikidata.inception"))
    if date:
        out["date_display"] = date
        out["creation_date"] = date
    repo = _one(_fv(bundle, "museum.institution"))
    if not repo:
        for cand in (_fv(bundle, "wikidata.collection") or []):
            if _INSTITUTION_RE.search(cand):
                repo = cand
                break
    if repo:
        out["current_repository"] = repo
    dims = _one(_fv(bundle, "museum.dimensions"))
    if not dims:
        h, w = _one(_fv(bundle, "wikidata.height")), _one(_fv(bundle, "wikidata.width"))
        dims = f"{h} × {w} cm" if h and w else None
    if dims:
        out["physical_dimensions"] = dims
    culture = _one(_fv(bundle, "museum.culture"))
    if culture:
        out["cultural_context"] = culture
    agent = _one(_fv(bundle, "museum.creators"))
    if not agent and (bundle.get("match") or {}).get("confidence") == "high":
        agent = _one(_fv(bundle, "wikidata.creator"))
    if agent:
        out["agent_name"] = agent
    return out


# ----------------------------------------------------------------------- prose validation
_YEAR_RE = re.compile(r"\b(?:1[0-9]{3}|20[0-9]{2})\b")


def _all_fact_text(bundle: dict, fields: dict, title: str) -> str:
    parts = [title or ""]
    for f in bundle["facts"]:
        v = f["value"]
        parts.append(", ".join(v) if isinstance(v, list) else str(v))
    parts.extend(str(v) for v in fields.values())
    return " ".join(parts).lower()


def validate_prose(data: dict, bundle: dict, fields: dict, title: str) -> list[str]:
    """Violations of the model's prose against the facts (empty list = accepted)."""
    if not isinstance(data, dict):
        return ["response is not a JSON object"]
    packet = {"facts": bundle["facts"], "title": title}
    _ok, reasons = validate_written_item(data, packet)
    reasons = list(reasons)
    narrative = data.get("description_narrative") or ""
    # Licence: CC BY-SA background text (Wikipedia lead, Commons description) is check-only.
    for txt in bundle.get("check_only_texts") or []:
        run = _copied_run(narrative, txt)
        if run:
            reasons.append(f"narrative copies {_COPY_RUN_WORDS}+ consecutive words of check-only "
                           f"background text (CC BY-SA): {run!r}")
    # A year in the prose must exist somewhere in the facts, even if the model didn't list it as a claim.
    known = _all_fact_text(bundle, fields, title)
    for y in _YEAR_RE.findall(narrative):
        if y not in known:
            reasons.append(f"year {y} in the narrative appears in none of the facts")
    # The prose may not name a different medium than the record's.
    rec_bucket = medium_bucket(fields.get("medium") or "")
    if rec_bucket:
        for name, pat in _MEDIUM_BUCKETS:
            if name != rec_bucket and pat.search(narrative) and not pat.search(fields["medium"]):
                reasons.append(f"narrative says {pat.pattern!r} but the record's medium is {fields['medium']!r}")
                break
    return reasons


# ----------------------------------------------------------------------- orchestration
async def _call_model(prompt: str, extra_parts: list | None) -> dict:
    parts = [ai_client.text_part(prompt)] + list(extra_parts or [])
    text = await asyncio.to_thread(
        ai_client.chat, "vision", [{"role": "user", "content": parts}], json_mode=True)
    data = ai_client.parse_json(text)
    if not isinstance(data, dict):
        raise ValueError("model reply is not a JSON object")
    return data


async def write_prose(bundle: dict, fields: dict, title: str, *, extra_parts: list | None = None,
                      prompt_suffix: str = "") -> tuple[dict | None, list[str]]:
    """Model writes the prose from the facts; validate; on rejection ONE retry with the violations listed.
    Returns (accepted_data | None, last_violations). A model/transport error returns (None, [...])."""
    base = build_narrative_prompt(bundle, fields) + (("\n" + prompt_suffix) if prompt_suffix else "")
    prompt, violations = base, []
    for attempt in range(2):
        try:
            data = await _call_model(prompt, extra_parts)
        except Exception as e:
            logger.warning("[grounding] prose model call failed: %s", e)
            return None, [f"model call failed: {e}"]
        violations = validate_prose(data, bundle, fields, title)
        if not violations:
            return data, []
        logger.info("[grounding] prose rejected (attempt %d): %s", attempt + 1, violations)
        prompt = (base + "\n\nYour previous draft was REJECTED for these problems — fix every one, "
                  "using only the facts above:\n" + "\n".join(f"- {v}" for v in violations))
    return None, violations


async def ground_work(*, title: str, artist: str | None, hints=None, source_api: str | None = None,
                      source_url: str | None = None, extra_parts: list | None = None,
                      prompt_suffix: str = "", client: httpx.AsyncClient | None = None) -> dict | None:
    """Ground one user-added museum work. Returns None when it can't be grounded (no museum record and no
    Wikidata identity, or the lookups failed with nothing to go on) — the caller then keeps today's
    behaviour. Never raises. Otherwise:
      {fields, description_narrative, tags, focal_point, used_fallback, violations, match, n_facts}."""
    own = client is None
    try:
        rec = museum_record_from_hints(hints, source_api)
        if own:
            client = httpx.AsyncClient(headers={"User-Agent": SD_USER_AGENT}, follow_redirects=True,
                                       timeout=_REQ_TIMEOUT_S)
        fx = RuntimeFetcher(client)
        bundle = await build_runtime_bundle(fx, title=title or "", artist=artist or "", rec=rec,
                                            source_url=source_url)
        if fx.errors:
            logger.warning("[grounding] %d lookup(s) failed for %r (Wikidata/Commons/Wikipedia down?)",
                           fx.errors, title)
        if not bundle["facts"]:
            logger.info("[grounding] nothing to ground %r on (no identity, no record) -> legacy path", title)
            return None
        fields = resolve_fields(bundle)
        data, violations = await write_prose(bundle, fields, title or "", extra_parts=extra_parts,
                                             prompt_suffix=prompt_suffix)
        item = {"title": title or "Untitled", "agent_name": fields.get("agent_name") or artist or "",
                "source": (rec or {}).get("institution")}
        if data:
            tags = data.get("tags")
            return {"fields": fields, "description_narrative": data["description_narrative"].strip(),
                    "tags": ", ".join(tags) if isinstance(tags, list) else (tags or None),
                    "focal_point": data.get("focal_point"), "used_fallback": False, "violations": [],
                    "match": bundle["match"], "n_facts": len(bundle["facts"])}
        minimal = _template_placard_grounded(item, fields, bundle)
        return {"fields": fields, "description_narrative": minimal["description_narrative"],
                "tags": minimal.get("tags") or None, "focal_point": None, "used_fallback": True,
                "violations": violations, "match": bundle["match"], "n_facts": len(bundle["facts"])}
    except Exception as e:                      # grounding must never hard-fail enrichment
        logger.warning("[grounding] failed for %r, using legacy enrichment: %s", title, e, exc_info=True)
        return None
    finally:
        if own and client is not None:
            await client.aclose()
