"""Placard ACCURACY AUDIT — evidence collection step only (judging happens elsewhere).

The ~2,800 pack placards are written by tools/build_catalog.py `enrich_item`: an LLM given only
title/artist/date/medium/source, no retrieval. This tool draws a reproducible stratified sample,
resolves each work to Wikidata (best-first, never guessing), and writes compact third-party evidence
(Wikidata claims, a museum's own keyless record when available, the enwiki lead) per work — plus a
sanity check that the two locally-installed packs' placard text still matches the served catalog.

Read-only against the repo; writes only under the scratchpad audit/ dir. Polite to upstream APIs:
concurrency <=4, retry+backoff on 429/5xx, disk cache so reruns are free and never bake a transient
error into a verdict (fetch_error is retried on rerun, never cached as a result).

    python -m tools.audit_placards                  # full run (sample -> resolve -> evidence)
    python -m tools.audit_placards --sample-only     # just (re)write audit/sample.json
    python -m tools.audit_placards --limit 10        # smoke test on the first N sampled works
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sys
import unicodedata
import urllib.parse
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / "static" / "catalog"
MANIFEST_DIR = ROOT / "Artwork" / "_manifests"
# Round 9: NEVER /tmp — a laptop reboot wipes tmpfs, and it took the entire cache + facts + packets +
# regenerated catalog with it (the writers' narratives survived only because they'd been backed up
# outside it by hand). Durable by default, outside the repo; still overridable per-run via env.
AUDIT_DIR = Path(os.environ.get("AUDIT_DIR", str(Path.home() / "pieria-img" / "audit")))
EVIDENCE_DIR = AUDIT_DIR / "evidence"
CACHE_DIR = AUDIT_DIR / "cache"

SEED = 20260925
TOTAL_SAMPLE = 150
FAMOUS_COLLECTIONS = [
    "masterpieces", "impressionism", "post-impressionism", "renaissance", "dutch-golden-age",
]
FAMOUS_TARGET = 25
OTHER_TARGET = TOTAL_SAMPLE - FAMOUS_TARGET  # 125
OTHER_FLOOR = 3

UA = "Pieria-PlacardAudit/1.0 (https://github.com/pieria-art/Pieria; placard accuracy audit)"

WD_API = "https://www.wikidata.org/w/api.php"
WD_SPARQL = "https://query.wikidata.org/sparql"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WP_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
MET_SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1/search"
MET_OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{}"
CLEVELAND_SEARCH = "https://openaccess-api.clevelandart.org/api/artworks/"

# Evidence properties we resolve to English labels.
CLAIM_PROPS = {
    "P170": "creator", "P571": "inception", "P186": "made_from_material", "P136": "genre",
    "P135": "movement", "P195": "collection", "P276": "location", "P180": "depicts",
    "P1071": "location_of_creation", "P2048": "height", "P2049": "width",
}


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


def _slug(title: str) -> str:
    s = _norm(title).replace(" ", "-")
    return re.sub(r"-+", "-", s)


# --------------------------------------------------------------------------- catalog loading
def load_catalog() -> dict[str, list[dict]]:
    out = {}
    for f in sorted(CATALOG_DIR.glob("*.json")):
        if f.name == "index.json" or f.name.startswith("_"):
            continue
        data = json.loads(f.read_text())
        out[f.stem] = data.get("items", [])
    return out


# --------------------------------------------------------------------------- sampling
def build_sample(catalog: dict[str, list[dict]]) -> list[dict]:
    rng = random.Random(SEED)
    sample = []
    n = 0

    def take(coll, idxs):
        nonlocal n
        for i in idxs:
            n += 1
            it = catalog[coll][i]
            sample.append({
                "n": n, "collection": coll, "idx": i,
                "title": it.get("title"), "agent_name": it.get("agent_name"),
            })

    # -- famous: 25 across 5 collections, weighted by size, floor 1
    fam_sizes = {c: len(catalog[c]) for c in FAMOUS_COLLECTIONS}
    fam_alloc = _allocate(fam_sizes, FAMOUS_TARGET, floor=1, rng=rng)
    for c, k in fam_alloc.items():
        idxs = rng.sample(range(len(catalog[c])), k)
        take(c, idxs)

    # -- other: 125 across all non-famous collections, floor 3, rest proportional
    other_colls = [c for c in catalog if c not in FAMOUS_COLLECTIONS]
    other_sizes = {c: len(catalog[c]) for c in other_colls}
    other_alloc = _allocate(other_sizes, OTHER_TARGET, floor=OTHER_FLOOR, rng=rng)
    for c, k in other_alloc.items():
        idxs = rng.sample(range(len(catalog[c])), k)
        take(c, idxs)

    # deterministic mixing for later batch split (famous + obscure interleaved)
    rng.shuffle(sample)
    for i, row in enumerate(sample, 1):
        row["batch"] = (i - 1) % 3 + 1
    sample.sort(key=lambda r: r["n"])
    return sample


def _allocate(sizes: dict[str, int], target: int, floor: int, rng: random.Random) -> dict[str, int]:
    """Floor per collection (capped by its size), then distribute the remainder proportional to
    remaining size using largest-remainder rounding, never exceeding a collection's own size."""
    alloc = {c: min(floor, sz) for c, sz in sizes.items()}
    remaining_budget = target - sum(alloc.values())
    capacity = {c: sizes[c] - alloc[c] for c in sizes}
    total_capacity = sum(capacity.values())
    if remaining_budget <= 0 or total_capacity <= 0:
        return alloc
    remaining_budget = min(remaining_budget, total_capacity)
    exact = {c: remaining_budget * capacity[c] / total_capacity for c in sizes}
    base = {c: min(int(exact[c]), capacity[c]) for c in sizes}
    for c in sizes:
        alloc[c] += base[c]
    left = remaining_budget - sum(base.values())
    fracs = sorted(
        ((exact[c] - base[c], c) for c in sizes if alloc[c] < sizes[c]),
        key=lambda t: (-t[0], t[1]),
    )
    i = 0
    while left > 0 and fracs:
        _, c = fracs[i % len(fracs)]
        if alloc[c] < sizes[c]:
            alloc[c] += 1
            left -= 1
        i += 1
        if i > 10000:
            break
    return alloc


# --------------------------------------------------------------------------- HTTP cache + fetch
def _cache_key(method: str, url: str, params: dict | None, data) -> Path:
    raw = json.dumps([method, url, params or {}, data], sort_keys=True, default=str)
    h = hashlib.sha256(raw.encode()).hexdigest()
    return CACHE_DIR / f"{h}.json"


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, sem: asyncio.Semaphore):
        self.client = client
        self.sem = sem
        self.stats = {"http_ok": 0, "http_cached": 0, "http_error": 0}

    async def get_json(self, url, params=None) -> tuple[dict | list | None, str | None]:
        return await self._req("GET", url, params, None, want_json=True)

    async def get_text(self, url, params=None) -> tuple[str | None, str | None]:
        return await self._req("GET", url, params, None, want_json=False)

    async def post_json(self, url, data) -> tuple[dict | list | None, str | None]:
        return await self._req("POST", url, None, data, want_json=True)

    async def _req(self, method, url, params, data, want_json):
        ck = _cache_key(method, url, params, data)
        if ck.exists():
            self.stats["http_cached"] += 1
            cached = json.loads(ck.read_text())
            return cached["body"], None
        async with self.sem:
            err = None
            for attempt in range(5):
                try:
                    if method == "GET":
                        r = await self.client.get(url, params=params, timeout=30.0)
                    else:
                        r = await self.client.post(url, data=data, timeout=30.0)
                    if r.status_code == 200:
                        body = r.json() if want_json else r.text
                        ck.write_text(json.dumps({"body": body}))
                        self.stats["http_ok"] += 1
                        return body, None
                    if r.status_code in (429, 500, 502, 503, 504):
                        wait = 2.0 * (attempt + 1)
                        try:
                            wait = max(wait, float(r.headers.get("retry-after", 0) or 0))
                        except ValueError:
                            pass
                        await asyncio.sleep(min(wait, 20.0))
                        err = f"http_{r.status_code}"
                        continue
                    err = f"http_{r.status_code}"
                    break
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    err = f"transport_error:{type(e).__name__}"
                    await asyncio.sleep(min(2.0 * (attempt + 1), 20.0))
            self.stats["http_error"] += 1
            return None, err or "unknown_error"


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


# --------------------------------------------------------------------------- evidence assembly
async def _wikidata_claims(fx: Fetcher, qid: str) -> dict:
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
    sitelinks = ent.get("sitelinks") or {}
    enwiki = sitelinks.get("enwiki", {}).get("title")
    return {"claims": out, "enwiki_title": enwiki}


async def _wikipedia_lead(fx: Fetcher, enwiki_title: str) -> str | None:
    body, err = await fx.get_json(WP_SUMMARY.format(urllib.parse.quote(enwiki_title)))
    if not body:
        return None
    extract = body.get("extract") or ""
    return extract[:1500] if extract else None


async def _museum_record(fx: Fetcher, item: dict) -> dict | None:
    source = item.get("source") or ""
    title = item.get("title") or ""
    if source == "The Metropolitan Museum of Art":
        body, err = await fx.get_json(MET_SEARCH, {"q": title})
        if not body or not body.get("objectIDs"):
            return None
        obj_id = body["objectIDs"][0]
        obj, err2 = await fx.get_json(MET_OBJECT.format(obj_id))
        if not obj:
            return None
        return {
            "api": "Met Collection API", "url": MET_OBJECT.format(obj_id),
            "title": obj.get("title"), "artistDisplayName": obj.get("artistDisplayName"),
            "objectDate": obj.get("objectDate"), "medium": obj.get("medium"),
            "culture": obj.get("culture"), "classification": obj.get("classification"),
        }
    if source == "Cleveland Museum of Art":
        body, err = await fx.get_json(CLEVELAND_SEARCH, {"q": title, "limit": 1})
        data = (body or {}).get("data") or []
        if not data:
            return None
        d = data[0]
        return {
            "api": "Cleveland Open Access API", "url": f"https://openaccess-api.clevelandart.org/api/artworks/{d.get('id')}",
            "title": d.get("title"), "creators": [c.get("description") for c in (d.get("creators") or [])],
            "creation_date": d.get("creation_date"), "technique": d.get("technique"), "culture": d.get("culture"),
        }
    return None  # Rijksmuseum/SMK need an API key (D006-style: no key at rest here) -> skip


async def build_evidence(fx: Fetcher, row: dict, item: dict) -> dict:
    placard = {
        "title": item.get("title"), "agent_name": item.get("agent_name"), "agent_role": item.get("agent_role"),
        "creation_date": item.get("creation_date"), "date_display": item.get("date_display"),
        "medium": item.get("medium"), "cultural_context": item.get("cultural_context"),
        "description_narrative": item.get("description_narrative"), "tags": item.get("tags"),
        "source": item.get("source"), "source_url": item.get("source_url"),
    }
    match = await resolve_work(fx, item)
    evidence = {"wikidata": None, "museum": None, "wikipedia_lead": None, "urls": []}
    if match:
        wd = await _wikidata_claims(fx, match["qid"])
        evidence["wikidata"] = wd.get("claims") if "claims" in wd else None
        if wd.get("fetch_error"):
            evidence["wikidata_fetch_error"] = wd["fetch_error"]
        evidence["urls"].append(f"https://www.wikidata.org/wiki/{match['qid']}")
        if wd.get("enwiki_title"):
            lead = await _wikipedia_lead(fx, wd["enwiki_title"])
            evidence["wikipedia_lead"] = lead
            evidence["urls"].append(f"https://en.wikipedia.org/wiki/{urllib.parse.quote(wd['enwiki_title'])}")
    museum = await _museum_record(fx, item)
    if museum:
        evidence["museum"] = museum
        evidence["urls"].append(museum["url"])
    if item.get("source_url"):
        evidence["urls"].append(item["source_url"])
    return {
        "n": row["n"], "collection": row["collection"],
        "placard": placard, "match": match, "evidence": evidence,
    }


# --------------------------------------------------------------------------- pack consistency check
def check_pack_consistency() -> dict:
    results = {}
    for manifest_name in ("masterpieces", "golden-age-illustration"):
        mf = MANIFEST_DIR / f"{manifest_name}.json"
        if not mf.exists():
            results[manifest_name] = {"error": "manifest not found"}
            continue
        # Match against THIS collection's own catalog file only — matching against a global,
        # cross-collection title index caused false "mismatches" on titles that recur in more than
        # one collection (e.g. "The Apparition" exists in both golden-age-illustration and
        # symbolism-romance, with different artists entirely).
        cf = CATALOG_DIR / f"{manifest_name}.json"
        catalog_by_title: dict[str, list] = {}
        if cf.exists():
            cdata = json.loads(cf.read_text())
            for it in cdata.get("items", []):
                catalog_by_title.setdefault(_norm(it.get("title", "")), []).append(it)
        mdata = json.loads(mf.read_text())
        mismatches = []
        ambiguous_titles = []
        checked = 0
        for it in mdata.get("items", []):
            key = _norm(it.get("title", ""))
            cands = catalog_by_title.get(key) or []
            if len(cands) > 1:
                # Same title appears >1x in this collection (e.g. "The Flowers" x3) — title-only
                # matching can't disambiguate which catalog row a manifest item came from (no shared
                # id field), so skip rather than risk a false "mismatch" against the wrong row.
                ambiguous_titles.append(it.get("title"))
                continue
            cat = cands[0] if cands else None
            if not cat:
                continue
            checked += 1
            pairs = [
                ("placard", "description_narrative", it.get("placard"), cat.get("description_narrative")),
                ("artist", "agent_name", it.get("artist"), cat.get("agent_name")),
                ("artist_role", "agent_role", it.get("artist_role"), cat.get("agent_role")),
                ("culture", "cultural_context", it.get("culture"), cat.get("cultural_context")),
                ("date", "date_display", it.get("date"), cat.get("date_display")),
                ("medium", "medium", it.get("medium"), cat.get("medium")),
            ]
            for pack_field, cat_field, pv, cv in pairs:
                if pv != cv:
                    mismatches.append({
                        "title": it.get("title"), "pack_field": pack_field, "catalog_field": cat_field,
                        "pack_value": pv, "catalog_value": cv,
                    })
            # tags: pack is a list, catalog is comma-joined
            ptags = it.get("tags") or []
            cat_tags_raw = cat.get("tags") or []
            if isinstance(cat_tags_raw, str):
                ctags = [t.strip() for t in cat_tags_raw.split(",") if t.strip()]
            else:
                ctags = list(cat_tags_raw)
            if ptags != ctags:
                mismatches.append({
                    "title": it.get("title"), "pack_field": "tags", "catalog_field": "tags",
                    "pack_value": ptags, "catalog_value": ctags,
                })
        results[manifest_name] = {
            "checked": checked, "mismatches": mismatches, "ambiguous_titles_skipped": ambiguous_titles,
        }
    return results


# --------------------------------------------------------------------------- main
async def run(limit: int | None):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    catalog = load_catalog()
    sample = build_sample(catalog)
    (AUDIT_DIR / "sample.json").write_text(json.dumps(sample, indent=2))

    pack_check = check_pack_consistency()
    (AUDIT_DIR / "pack_consistency.json").write_text(json.dumps(pack_check, indent=2))

    rows = sample[:limit] if limit else sample
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True) as client:
        fx = Fetcher(client, sem)

        async def one(row):
            item = catalog[row["collection"]][row["idx"]]
            try:
                ev = await build_evidence(fx, row, item)
            except Exception as e:
                ev = {
                    "n": row["n"], "collection": row["collection"],
                    "placard": {"title": item.get("title")}, "match": None,
                    "evidence": {"fetch_error": f"{type(e).__name__}: {e}"},
                }
            (EVIDENCE_DIR / f"{row['n']:03d}.json").write_text(json.dumps(ev, indent=2))
            return ev

        results = await asyncio.gather(*(one(r) for r in rows))

    # -- summary
    by_collection = {}
    by_method = {}
    unmatched_reasons = {}
    with_evidence = 0
    for ev in results:
        c = ev["collection"]
        by_collection.setdefault(c, {"total": 0, "matched": 0})
        by_collection[c]["total"] += 1
        m = ev.get("match")
        if m:
            by_collection[c]["matched"] += 1
            by_method[m["method"]] = by_method.get(m["method"], 0) + 1
        else:
            reason = (ev.get("evidence") or {}).get("wikidata_fetch_error") or "no_confident_match"
            unmatched_reasons[reason] = unmatched_reasons.get(reason, 0) + 1
        has_ev = bool((ev.get("evidence") or {}).get("wikidata")) or bool((ev.get("evidence") or {}).get("museum")) or bool((ev.get("evidence") or {}).get("wikipedia_lead"))
        if has_ev:
            with_evidence += 1

    summary = {
        "sampled": len(rows), "matched": sum(1 for e in results if e.get("match")),
        "with_evidence": with_evidence,
        "by_collection": by_collection, "by_method": by_method,
        "unmatched_reasons": unmatched_reasons,
        "pack_consistency": {k: {"checked": v.get("checked"), "mismatches": len(v.get("mismatches", []))} for k, v in pack_check.items()},
        "http_stats": fx.stats,
    }
    (AUDIT_DIR / "collect_summary.json").write_text(json.dumps(summary, indent=2))

    # -- batch index files (already-shuffled sample -> stable 3-way split)
    for b in (1, 2, 3):
        batch_rows = [r for r in rows if r["batch"] == b]
        batch = [{"n": r["n"], "collection": r["collection"], "file": f"evidence/{r['n']:03d}.json"} for r in batch_rows]
        (AUDIT_DIR / f"batch_{b}.json").write_text(json.dumps(batch, indent=2))

    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-only", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if args.sample_only:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        catalog = load_catalog()
        sample = build_sample(catalog)
        (AUDIT_DIR / "sample.json").write_text(json.dumps(sample, indent=2))
        print(f"wrote {len(sample)} sample rows -> {AUDIT_DIR / 'sample.json'}")
        return

    summary = asyncio.run(run(args.limit))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    sys.exit(main())
