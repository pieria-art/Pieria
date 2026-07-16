"""
tools/greedy_grab.py — greedy Wikimedia Commons category harvester (maintainer build tool).

Doctrine (ADR-040 #1/#2a): grab an *entire* PD Commons category (per artist / collection),
filtered HARD at source — native long edge >= MIN_NATIVE_EDGE, public-domain, sane aspect,
bad-substring reject. Category-grab is cleaner for categorization than keyword search: a Commons
"Paintings by X" / "Utagawa Hiroshige" category is unambiguously X, so every kept file lands in the
right collection with no keyword cross-contamination (the failure mode ADR-039 cleaned up).

Recursive BFS through subcategories (Commons organises works by date / museum / series), dedup by
filename. The output is a *candidates* file for the Sonnet clean stage — raw Commons metadata is
multilingual and cruft-heavy, so heuristics can't produce placard-grade titles; a per-master Sonnet
agent does (see clean_inject.py for the inject stage that consumes the cleaned items).

    python -m tools.greedy_grab --category "Utagawa Hiroshige" --out scratch/grab_hiroshige.json
    python -m tools.greedy_grab -c "Paintings by Claude Monet" --min-edge 3840 --max-files 800

NOT part of the runtime image (belongs behind .dockerignore with the rest of tools/, ADR-040 #6).

⚠ An empty grab almost always means a WRONG category name, not "no works" — the tool prints the
resolved category + file/subcat counts as it walks so you can see the crawl is real. Verify the
category exists (it logs "category not found") before concluding a master has no PD works.
See memory [[greedy-grab-category-gotcha]].
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

# Reuse the canonical Commons helpers — same PD gate + FilePath URLs the live scout/resolver use,
# so a grabbed item is byte-identical in shape to a resolved pick.
from scout import _wikimedia_filepath, _wm_is_pd

API_URL = "https://commons.wikimedia.org/w/api.php"
UA = "ScreenDocent/1.0 (offline art catalog build; jmyost@gmail.com)"

MIN_NATIVE_EDGE = 3840  # true-4K floor (ADR-039): pack-survivable long edge
MAX_ASPECT = 4.0        # reject ultra-panoramic handscrolls / banners (ADR-039 quality pass)

# Filename substrings that mark a file as NOT a clean full-work scan. Conservative on purpose — a
# false reject silently drops a real work, so keep this to unambiguous non-artwork markers.
BAD_SUBSTRINGS = (
    "(frame)", "with frame", "framed", "in frame",
    " detail", "(detail", "detail of", "detail)",
    " verso", "(verso", "reverse of", " recto ",
    "signature", "inscription", "colophon", "seal of",
    "diagram", "x-ray", "xray", "infrared", "raking light",
    "before restoration", "after restoration", "during restoration",
    "installation view", "in situ", "gallery view", "exhibition",
    "thumbnail",
)

# Polite Commons pacing (mirrors scout.WM_MIN_INTERVAL). One request at a time; this tool is
# sequential so a simple wall-clock spacer is enough.
_MIN_INTERVAL = 1.2
_last = [0.0]


def _throttle():
    wait = _MIN_INTERVAL - (time.monotonic() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.monotonic()


def _get(client: httpx.Client, params: dict) -> dict:
    """One Commons API read with throttle + light retry on transient transport/5xx errors.

    Uses POST — the API treats POST form params identically to GET for read queries, and a batch
    imageinfo request over 50 long (Japanese / Rijksmuseum) filenames overflows the URL length
    limit as a GET (HTTP 414). POST carries the titles in the body, no length cap.
    """
    params = {**params, "format": "json"}
    last_exc = None
    for attempt in range(4):
        _throttle()
        try:
            r = client.post(API_URL, data=params, timeout=30.0)
            if r.status_code >= 500:
                raise httpx.HTTPStatusError("5xx", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last_exc = e
            time.sleep(1.5 * (attempt + 1))  # backoff — a transient blip must not abort the crawl
    raise RuntimeError(f"Commons API failed after retries: {last_exc}")


def _category_members(client: httpx.Client, category: str):
    """Yield (files, subcats) for one category, following cmcontinue pagination."""
    files, subcats = [], []
    cont = {}
    while True:
        data = _get(client, {
            "action": "query", "list": "categorymembers",
            "cmtitle": f"Category:{category}", "cmtype": "file|subcat",
            "cmlimit": "500", **cont,
        })
        for m in data.get("query", {}).get("categorymembers", []):
            (files if m["ns"] == 6 else subcats).append(m["title"])
        cont = data.get("continue", {})
        if not cont:
            break
    return files, subcats


def _imageinfo(client: httpx.Client, titles):
    """Batch imageinfo (size + url + extmetadata) for up to 50 File: titles at a time."""
    out = {}
    for i in range(0, len(titles), 50):
        batch = titles[i:i + 50]
        data = _get(client, {
            "action": "query", "titles": "|".join(batch),
            "prop": "imageinfo", "iiprop": "size|url|extmetadata",
        })
        for page in data.get("query", {}).get("pages", {}).values():
            ii = (page.get("imageinfo") or [{}])[0]
            if ii:
                out[page["title"]] = ii
    return out


def _bad_name(fname: str) -> bool:
    low = fname.lower()
    return any(b in low for b in BAD_SUBSTRINGS)


def grab(category: str, *, min_edge: int, max_files: int, max_depth: int) -> list:
    seen_cats, seen_files = set(), set()
    kept, stats = [], {"files_seen": 0, "cat_reject": 0, "small": 0, "not_pd": 0, "aspect": 0, "badname": 0}
    with httpx.Client(headers={"User-Agent": UA}) as client:
        # Confirm the root category exists before crawling (the empty-grab gotcha guard).
        root_files, root_subs = _category_members(client, category)
        if not root_files and not root_subs:
            print(f"⚠ category not found or empty: Category:{category}", file=sys.stderr)
            return []
        print(f"root Category:{category} → {len(root_files)} files, {len(root_subs)} subcats", file=sys.stderr)

        # BFS the category tree.
        frontier = [(category, root_files, root_subs, 0)]
        seen_cats.add(category)
        pending_titles = []
        while frontier:
            cat, files, subs, depth = frontier.pop(0)
            for f in files:
                if f not in seen_files:
                    seen_files.add(f)
                    pending_titles.append(f)
            if depth < max_depth:
                for sc in subs:
                    name = sc.split("Category:", 1)[-1]
                    if name not in seen_cats:
                        seen_cats.add(name)
                        cf, cs = _category_members(client, name)
                        print(f"  ↳ {name} → {len(cf)} files, {len(cs)} subcats", file=sys.stderr)
                        frontier.append((name, cf, cs, depth + 1))
            if len(pending_titles) >= max_files:
                break

        pending_titles = pending_titles[:max_files]
        print(f"resolving imageinfo for {len(pending_titles)} unique files…", file=sys.stderr)
        info = _imageinfo(client, pending_titles)

    for title, ii in info.items():
        stats["files_seen"] += 1
        fname = title.split("File:", 1)[-1]
        w, h = ii.get("width") or 0, ii.get("height") or 0
        if max(w, h) < min_edge:
            stats["small"] += 1
            continue
        if not _wm_is_pd(ii.get("extmetadata") or {}):
            stats["not_pd"] += 1
            continue
        lo, hi = sorted((w, h))
        if lo == 0 or hi / lo > MAX_ASPECT:
            stats["aspect"] += 1
            continue
        if _bad_name(fname):
            stats["badname"] += 1
            continue
        em = ii.get("extmetadata") or {}
        kept.append({
            "fname": fname,
            "width": w, "height": h,
            "source_url": _wikimedia_filepath(fname, min_edge),
            "thumbnail_url": _wikimedia_filepath(fname, 600),
            "raw_title": (em.get("ObjectName") or {}).get("value") or fname.rsplit(".", 1)[0],
            "raw_date": (em.get("DateTimeOriginal") or em.get("DateTime") or {}).get("value", ""),
            "raw_artist": (em.get("Artist") or {}).get("value", ""),
            "license_short": (em.get("LicenseShortName") or {}).get("value", ""),
        })

    kept.sort(key=lambda x: x["fname"])
    print(f"KEPT {len(kept)} / {stats['files_seen']} resolved  "
          f"(small {stats['small']}, not-PD {stats['not_pd']}, aspect {stats['aspect']}, "
          f"badname {stats['badname']})", file=sys.stderr)
    return kept


def main():
    ap = argparse.ArgumentParser(description="Greedy Commons category harvester (ADR-040).")
    ap.add_argument("-c", "--category", required=True, help="Commons category name (no 'Category:' prefix)")
    ap.add_argument("-o", "--out", required=True, help="output candidates JSON path")
    ap.add_argument("--min-edge", type=int, default=MIN_NATIVE_EDGE, help=f"native long-edge floor (default {MIN_NATIVE_EDGE})")
    ap.add_argument("--max-files", type=int, default=800, help="cap on unique files to resolve (default 800)")
    ap.add_argument("--max-depth", type=int, default=3, help="subcategory BFS depth (default 3)")
    args = ap.parse_args()

    kept = grab(args.category, min_edge=args.min_edge, max_files=args.max_files, max_depth=args.max_depth)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(kept, indent=1, ensure_ascii=False))
    print(f"→ wrote {len(kept)} candidates to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
