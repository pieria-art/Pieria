"""
tools/clean_titles.py — normalize verbose / raw-source titles in the served + pack-source catalogs
(maintainer tool — NOT part of the runtime image).

Museum-metadata titles arrive with publication boilerplate, series attributions, processing-artifact
suffixes, and un-decoded HTML entities baked into the `title` field — fine for provenance, wrong for a
placard. This pass rewrites `title` to the display work-name and lifts the series attribution into a
structured `series` field (nothing is dropped — the series is preserved, just out of the title line).

Rules (idempotent; a second run is a no-op):
  1. HTML-entity decode          "Young 2 &amp; 3"                        → "Young 2 & 3"
  2. artifact suffix strip       "… Western Blue-bird (cropped)"          → "… Western Blue-bird"
  3. Audubon plate boilerplate   "The birds of America. [Livraison] 20,
                                  Mottled Owl … [i.e. Eastern Screech-Owl]
                                  … : [estampe] / Drawn from Nature …"     → "Eastern Screech-Owl"
  4. ukiyo-e "also known as"      "Under the Wave off Kanagawa (Kanagawa
                                  oki nami ura), also known as The Great
                                  Wave, from the series \"Thirty-Six …\""   → title "Under the Wave off
                                                                             Kanagawa" + series
  5. series tail lift            "Evening Cherry Blossoms at Gotenyama
                                  (from Famous Places in the Eastern
                                  Capital)"                                → title "…Gotenyama" + series
                                                                             "Famous Places in the …"
  6. numbered-plate OCR fix      "I. Arkansaw Flycatcher - 2. …"          → "1. Arkansaw Flycatcher - 2. …"

Operates on the SOURCE catalogs only — the served catalog the app ships (`static/catalog/`) and the pack
build input (`art-pack/_catalog/`). The signed pack manifests (`art-pack/_manifests/`) are BUILD OUTPUTS:
they regenerate + re-sign from `art-pack/_catalog/` on the next `tools.build_pack` run, so cleaning the
source is enough — do NOT hand-edit the manifests (it would invalidate their Ed25519 signature).

    python -m tools.clean_titles                       # dry-run: report every change, write nothing
    python -m tools.clean_titles --write               # apply to static/catalog/ + art-pack/_catalog/
    python -m tools.clean_titles --dir static/catalog  # limit to one tree
"""
import argparse
import html
import json
import re
from pathlib import Path

# Source catalog trees kept title-consistent (served + pack build input). Files whose basename starts
# with "_" (index.json aside) are internal/parked sets — skipped so we never touch e.g. _pending_artic.
DEFAULT_DIRS = ["static/catalog", "art-pack/_catalog"]

_ARTIFACT_SUFFIX = re.compile(r"\s*\((?:cropped|detail|full view|reproduction)\)\s*$", re.I)
_SERIES_TAIL = re.compile(r"\s*\(from (?P<series>[^()]+?)\)\s*$")
_AKA_SERIES = re.compile(
    r'^(?P<main>.+?)(?:\s*\([^)]*\))?,\s*also known as\s+.+?,\s*'
    r'from the series\s+"?(?P<series>.+?)"?\s*$'
)
_SERIES_OF = re.compile(r'^(?P<main>.+?),\s*from the series\s+"?(?P<series>.+?)"?\s*$')
_AUDUBON = re.compile(r'^The birds of America\.\s*\[Livraison\][^,]*,\s*(?P<body>.+)$', re.I)
_AUDUBON_IE = re.compile(r'\[i\.e\.\s*(?P<name>[^\]]+?)\]')
_PLATE_ROMAN_I = re.compile(r'^I\.\s')


def _audubon_species(body: str) -> str:
    """Extract the display species from an Audubon 'birds of America' plate body: prefer an editorial
    '[i.e. <corrected name>]', else the species named before the first plate-position clause."""
    core = re.split(r'\s+:\s+|\s+/\s+', body, maxsplit=1)[0]  # cut the ' : [estampe] / Drawn …' tail
    ie = _AUDUBON_IE.search(core)
    if ie:
        return ie.group("name").strip()
    # else the species is the lead token before the first ',' or ';' (plate/vulgo/position noise)
    return re.split(r'[,;]', core, maxsplit=1)[0].strip()


def clean_title(raw: str) -> tuple[str, str | None]:
    """Return (clean_title, series_or_None) for one raw title. Pure + idempotent."""
    t = html.unescape(raw).strip()
    series: str | None = None

    t = _ARTIFACT_SUFFIX.sub("", t).strip()

    m = _AUDUBON.match(t)
    if m:
        t = _audubon_species(m.group("body"))
    else:
        m = _AKA_SERIES.match(t)
        if m:
            t, series = m.group("main").strip(), m.group("series").strip()
        else:
            m = _SERIES_OF.match(t)
            if m:
                t, series = m.group("main").strip(), m.group("series").strip()
            else:
                m = _SERIES_TAIL.search(t)
                if m:
                    series = m.group("series").strip()
                    t = _SERIES_TAIL.sub("", t).strip()

    # OCR'd Roman "I." plate number → Arabic "1." — fires both for a multi-species list ("I. X - 2. Y")
    # and a solitary plate ("I. Mourning Warbler"). Skip when a later "II." is present, i.e. a genuine
    # Roman-numeral outline where the leading "I." is intentional, not an OCR'd "1.".
    if _PLATE_ROMAN_I.match(t) and "II." not in t:
        t = "1. " + t[3:]

    t = re.sub(r'\s+', ' ', t).strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':  # unwrap a fully-quoted title, never a lone quote
        t = t[1:-1].strip()
    return t, series


def _rewrite_item(it: dict) -> dict | None:
    """Return a new item dict with a cleaned title (+ lifted series), or None if nothing changes.
    `series` is inserted right after `title` so an applied diff is a one-line insert per touched item."""
    raw = it.get("title")
    if not isinstance(raw, str):
        return None
    new_title, series = clean_title(raw)
    if new_title == raw and (series is None or it.get("series") == series):
        return None
    out: dict = {}
    for k, v in it.items():
        out[k] = new_title if k == "title" else v
        if k == "title" and series and "series" not in it:
            out["series"] = series
    if series and "series" in it:      # already had a series key — refresh in place
        out["series"] = series
    return out


def process_file(path: Path, write: bool) -> list[tuple[str, str, str | None]]:
    """Clean one catalog file. Returns a list of (old_title, new_title, series) for changed items."""
    doc = json.loads(path.read_text())
    items = doc if isinstance(doc, list) else doc.get("items", [])
    changes = []
    for i, it in enumerate(items):
        new = _rewrite_item(it)
        if new is None:
            continue
        changes.append((it["title"], new["title"], new.get("series")))
        items[i] = new
    if changes and write:
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    return changes


def main() -> None:
    ap = argparse.ArgumentParser(description="Normalize verbose catalog titles (see module docstring).")
    ap.add_argument("--dir", action="append", dest="dirs",
                    help=f"catalog dir(s) to process (repeatable). Default: {DEFAULT_DIRS}")
    ap.add_argument("--write", action="store_true", help="apply changes (default: dry-run report only)")
    args = ap.parse_args()

    dirs = args.dirs or DEFAULT_DIRS
    total = 0
    for d in dirs:
        base = Path(d)
        if not base.is_dir():
            print(f"  (skip: {d} not found)")
            continue
        for path in sorted(base.glob("*.json")):
            if path.name == "index.json" or path.name.startswith("_"):
                continue  # internal/parked sets (e.g. _pending_artic) are not display surfaces
            changes = process_file(path, args.write)
            if not changes:
                continue
            total += len(changes)
            print(f"\n{path}  ({len(changes)} titles)")
            for old, new, series in changes:
                print(f"  - {old[:88]}")
                print(f"  + {new[:88]}" + (f"   [series: {series[:50]}]" if series else ""))

    verb = "rewrote" if args.write else "would rewrite"
    print(f"\n{verb} {total} titles across {len(dirs)} tree(s)." +
          ("" if args.write else "  (dry-run — re-run with --write to apply)"))


if __name__ == "__main__":
    main()
