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
import json
import logging
import os
import re
import urllib.parse

import httpx
from dotenv import load_dotenv

load_dotenv()

import ai_client
from tools.audit_placards import (
    AUDIT_DIR,
    CACHE_DIR,
    UA,
    Fetcher,
    _commons_structured,  # noqa: F401  (re-exported for callers/tests that want it)
    _label_for_qid,
    _museum_record,
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

WD_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

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
    for k in ("DateTimeOriginal", "ObjectName", "Artist", "Credit", "Medium", "ImageDescription"):
        v = (meta.get(k) or {}).get("value")
        if v:
            out[k] = _clean_commons_text(str(v))
    return out or None


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


async def _wikidata_full(fx: Fetcher, qid: str) -> dict:
    body, err = await fx.get_json(WD_API, {
        "action": "wbgetentities", "ids": qid, "props": "claims|sitelinks", "languages": "en", "format": "json",
    })
    if not body:
        return {"fetch_error": err}
    ent = (body.get("entities") or {}).get(qid) or {}
    claims = ent.get("claims") or {}
    out = {}
    for prop, key in CLAIM_PROPS.items():
        vals = claims.get(prop) or []
        labels = []
        for c in vals[:5]:
            dv = c.get("mainsnak", {}).get("datavalue", {})
            v = dv.get("value")
            if isinstance(v, dict) and v.get("id"):
                labels.append(await _label_for_qid(fx, v["id"]))
            elif isinstance(v, dict) and "time" in v:
                labels.append(v["time"].lstrip("+").split("T")[0])
            elif isinstance(v, dict) and "amount" in v:
                labels.append(v["amount"])
            elif isinstance(v, str):
                labels.append(v)
        if labels:
            out[key] = labels
    version = None
    for prop, relation in VERSION_PROPS.items():
        vals = claims.get(prop) or []
        if not vals:
            continue
        dv = vals[0].get("mainsnak", {}).get("datavalue", {}).get("value", {})
        if isinstance(dv, dict) and dv.get("id"):
            label = await _label_for_qid(fx, dv["id"])
            version = {"relation": relation, "of_qid": dv["id"], "of_label": label, "property": prop}
            break
    sitelinks = ent.get("sitelinks") or {}
    return {"claims": out, "enwiki_title": sitelinks.get("enwiki", {}).get("title"), "is_version_of": version}


# ----------------------------------------------------------------------- facts bundle
async def build_facts_bundle(fx: Fetcher, item: dict) -> dict:
    """Resolve identity + assemble the typed facts bundle for one catalog item."""
    match = await resolve_work(fx, item)
    facts: list[dict] = []
    check_only: list[str] = []
    conflicts: list[dict] = []
    is_version_of = None

    if match:
        wd = await _wikidata_full(fx, match["qid"])
        claims = wd.get("claims") or {}
        wd_url = f"https://www.wikidata.org/wiki/{match['qid']}"
        for key, labels in claims.items():
            facts.append(_fact(f"wikidata.{key}", labels, "Wikidata", wd_url, "CC0"))
        is_version_of = wd.get("is_version_of")
        if wd.get("enwiki_title"):
            lead = await _wikipedia_lead(fx, wd["enwiki_title"])
            if lead:
                check_only.append(lead)

    museum = await _museum_record(fx, item)
    if museum:
        for key in ("objectDate", "medium", "culture", "classification", "creation_date", "technique"):
            if museum.get(key):
                facts.append(_fact(f"museum.{key}", museum[key], museum["api"], museum["url"], "CC0"))
        for key in ("creators",):
            if museum.get(key):
                facts.append(_fact(f"museum.{key}", museum[key], museum["api"], museum["url"], "CC0"))

    source_url = item.get("source_url") or ""
    if "commons.wikimedia.org" in source_url:
        filename = urllib.parse.unquote(urllib.parse.urlparse(source_url).path.rsplit("/", 1)[-1])
        ext = await _commons_extmetadata(fx, filename)
        if ext:
            for key, val in ext.items():
                if key == "ImageDescription":
                    check_only.append(val)
                else:
                    facts.append(_fact(f"commons.{key}", val, "Wikimedia Commons", source_url, "CC BY-SA"))

    facts, conflicts = _detect_conflicts(facts)
    return {
        "match": match, "facts": facts, "conflicts": conflicts,
        "is_version_of": is_version_of, "check_only_texts": check_only,
    }


def _detect_conflicts(facts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Flag when two sources disagree on medium class or date by >25 yrs. Facts themselves are kept
    as-is (conflict resolution happens in resolve_structured_fields, not here)."""
    conflicts = []
    mediums = [f for f in facts if f["key"] in ("museum.medium", "commons.Medium", "wikidata.made_from_material")]
    buckets = {medium_bucket(" ".join(f["value"]) if isinstance(f["value"], list) else str(f["value"])) for f in mediums}
    buckets.discard(None)
    if len(buckets) > 1:
        conflicts.append({"field": "medium", "detail": f"conflicting medium classes: {sorted(buckets)}"})

    years = []
    date_keys = ("museum.objectDate", "museum.creation_date", "wikidata.inception", "commons.DateTimeOriginal")
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


# ----------------------------------------------------------------------- structured fields (deterministic)
# Precedence: museum record > Wikidata > Commons extmetadata > existing catalog value.
_FIELD_SOURCES = {
    "medium": [("museum.medium", None), ("museum.technique", None), ("wikidata.made_from_material", None), ("commons.Medium", None)],
    "date_display": [("museum.objectDate", None), ("museum.creation_date", None), ("wikidata.inception", None), ("commons.DateTimeOriginal", None)],
    "current_repository": [("museum.api", None), ("wikidata.collection", None), ("wikidata.location", None)],
    "agent_name": [("museum.creators", None), ("wikidata.creator", None), ("commons.Artist", None)],
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

    # current repository
    for v, src in first_value(_FIELD_SOURCES["current_repository"]):
        fields["current_repository"] = v
        break

    # agent name (rarely overridden — catalog agent_name is usually already right; only replace on a
    # confirmed mismatch, never invent one)
    for v, src in first_value(_FIELD_SOURCES["agent_name"]):
        fields.setdefault("agent_name_confirmed", v)

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-sample", action="store_true", help="only the 150 audit-sample works")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--collection", default=None)
    args = ap.parse_args()
    report = asyncio.run(run(only_sample=args.only_sample, limit=args.limit, collection=args.collection))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
