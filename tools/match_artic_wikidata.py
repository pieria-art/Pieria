"""Dry-run coverage probe: how many artic-sourced catalog items have a Wikimedia mirror?

Read-only. Touches only Wikidata's public SPARQL endpoint (one query) — no catalog writes, no
artic requests, no Cloudflare poking. Answers the "verify coverage first" question before we commit
to a re-source: for every Art-Institute-of-Chicago item in static/catalog/*.json, is there a
Wikidata artwork in the AIC collection (P195 = Q239303) that carries a Commons image (P18)?

P18 hands us the image already in Special:FilePath form — the exact rot-proof URL the rest of the
app uses — so a match is directly usable later. We report a hit rate plus an eyeball-able sample,
and flag ambiguous titles (e.g. several "Water Lilies") rather than silently claiming a match.

    python -m tools.match_artic_wikidata            # summary + samples
    python -m tools.match_artic_wikidata --json out.json   # also dump the full match map
"""
import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

CATALOG_DIR = Path(__file__).resolve().parent.parent / "static" / "catalog"
AIC_QID = "Q239303"  # Art Institute of Chicago
SPARQL = "https://query.wikidata.org/sparql"
UA = "Pieria/1.0 (https://github.com/AiwendilInTheWoods/Pieria; jmyost@gmail.com) coverage-probe"

# Pull every AIC-collection artwork on Wikidata that has an image; P18 is returned as a
# Special:FilePath URL. OPTIONAL creator/inventory help disambiguate duplicate titles.
QUERY = f"""
SELECT ?item ?itemLabel ?creatorLabel ?inv ?image WHERE {{
  ?item wdt:P195 wd:{AIC_QID} .
  ?item wdt:P18 ?image .
  OPTIONAL {{ ?item wdt:P217 ?inv . }}
  OPTIONAL {{ ?item wdt:P170 ?creator . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def _norm(s: str) -> str:
    """Loose title key: strip accents/punctuation, lowercase, collapse spaces, drop leading article."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("—", " ").replace("–", " ").replace("-", " ")
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    s = re.sub(r"^(the|a|an)\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _surname(name: str) -> str:
    return _norm((name or "").split()[-1]) if name else ""


def collect_artic_items():
    items = []
    for f in sorted(CATALOG_DIR.glob("*.json")):
        if f.name == "index.json":
            continue
        data = json.loads(f.read_text())
        for it in data.get("items", []):
            if "artic.edu" in it.get("source_url", ""):
                items.append({
                    "collection": data.get("id", f.stem),
                    "title": it.get("title", ""),
                    "artist": it.get("agent_name", ""),
                    "date": it.get("creation_date") or it.get("date_display") or "",
                    "source_url": it.get("source_url", ""),
                })
    return items


def fetch_wikidata():
    url = f"{SPARQL}?{urllib.parse.urlencode({'query': QUERY, 'format': 'json'})}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310 (trusted, fixed host)
        rows = json.load(resp)["results"]["bindings"]
    by_title = defaultdict(list)
    for r in rows:
        title = r.get("itemLabel", {}).get("value", "")
        by_title[_norm(title)].append({
            "qid": r["item"]["value"].rsplit("/", 1)[-1],
            "label": title,
            "creator": r.get("creatorLabel", {}).get("value", ""),
            "inv": r.get("inv", {}).get("value", ""),
            "image": r["image"]["value"],  # already Special:FilePath form
        })
    return by_title


def match(items, by_title):
    hits, ambiguous, misses = [], [], []
    for it in items:
        cands = by_title.get(_norm(it["title"]), [])
        if not cands:
            misses.append(it)
            continue
        surname = _surname(it["artist"])
        narrowed = [c for c in cands if surname and surname in _norm(c["creator"])] or cands
        if len(narrowed) == 1:
            hits.append({**it, "match": narrowed[0]})
        else:
            ambiguous.append({**it, "candidates": narrowed})
    return hits, ambiguous, misses


COMMONS_API = "https://commons.wikimedia.org/w/api.php"


def commons_search(title: str, artist: str):
    """Direct Commons file search (lifts the Wikidata-AIC-tagged floor toward the true ceiling).

    Returns the top File: result's Special:FilePath URL, or None. Needs human eyeball later — a hit
    means 'a plausibly-matching file exists,' not a verified identity. Polite: one request, fixed host.
    """
    q = f'{artist} {title}'.strip()
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrnamespace": "6", "gsrsearch": q, "gsrlimit": "1", "prop": "imageinfo",
        "iiprop": "url", "iiurlwidth": "100",
    }
    url = f"{COMMONS_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted, fixed host)
            pages = json.load(resp).get("query", {}).get("pages", {})
    except Exception:
        return None
    for p in pages.values():
        fname = p.get("title", "").split(":", 1)[-1]
        if fname:
            return f"http://commons.wikimedia.org/wiki/Special:FilePath/{urllib.parse.quote(fname)}"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="dump full match map to PATH")
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--deep", action="store_true",
                    help="also Commons-search the misses to estimate the true reachable ceiling")
    args = ap.parse_args()

    items = collect_artic_items()
    print(f"artic items in catalog: {len(items)}", file=sys.stderr)
    print("querying Wikidata for AIC works with Commons images...", file=sys.stderr)
    t0 = time.monotonic()
    by_title = fetch_wikidata()
    n_wd = sum(len(v) for v in by_title.values())
    print(f"  {n_wd} AIC artworks-with-image on Wikidata ({time.monotonic()-t0:.1f}s)\n", file=sys.stderr)

    hits, ambiguous, misses = match(items, by_title)
    total = len(items)
    print("=" * 60)
    print(f"  CLEAN MATCH (title+artist, unique):  {len(hits):>3} / {total}  ({len(hits)/total:.0%})")
    print(f"  AMBIGUOUS (title match, >1 work):    {len(ambiguous):>3} / {total}")
    print(f"  NO MATCH:                            {len(misses):>3} / {total}")
    print(f"  ----  reachable upper bound:         {len(hits)+len(ambiguous):>3} / {total}  "
          f"({(len(hits)+len(ambiguous))/total:.0%})")
    print("=" * 60)

    print(f"\n--- sample clean matches (first {args.samples}) ---")
    for h in hits[:args.samples]:
        print(f"  ✓ {h['artist']} — {h['title']!r}\n      → {h['match']['image']}")
    if args.deep and (misses or ambiguous):
        probe = misses + ambiguous
        print(f"\n--- deep pass: Commons-searching {len(probe)} unmatched (≈{len(probe)*0.35:.0f}s) ---",
              file=sys.stderr)
        recovered = []
        for i, m in enumerate(probe):
            hit = commons_search(m["title"], m["artist"])
            if hit:
                recovered.append({**m, "commons_search": hit})
            time.sleep(0.3)  # politeness, fixed host
            if (i + 1) % 40 == 0:
                print(f"    ...{i+1}/{len(probe)}", file=sys.stderr)
        print("=" * 60)
        print(f"  + recovered via Commons search:      {len(recovered):>3} / {len(probe)} probed")
        print(f"  ====  TRUE reachable ceiling:        {len(hits)+len(recovered):>3} / {total}  "
              f"({(len(hits)+len(recovered))/total:.0%})  (needs eyeball)")
        print("=" * 60)
        print(f"\n--- sample recovered (first {args.samples}) ---")
        for r in recovered[:args.samples]:
            print(f"  ? {r['artist']} — {r['title']!r}\n      → {r['commons_search']}")
        misses = [m for m in misses if not any(
            r["title"] == m["title"] and r["artist"] == m["artist"] for r in recovered)]

    if misses:
        print(f"\n--- still no match (first {args.samples}) ---")
        for m in misses[:args.samples]:
            print(f"  ✗ [{m['collection']}] {m['artist']} — {m['title']!r}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"hits": hits, "ambiguous": ambiguous, "misses": misses}, indent=2, ensure_ascii=False))
        print(f"\nwrote {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
