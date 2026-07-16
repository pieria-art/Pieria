"""
tools/clean_inject.py — inject Sonnet-cleaned works into a served collection (maintainer tool).

The third stage of the ADR-040 grab → Sonnet-clean → inject pipeline. greedy_grab.py produces raw
candidates; a per-master Sonnet clean agent turns them into placard-grade items (canonical English
title, series disambiguation, real narrative, junk dropped); this stage dedups the cleaned items
against the target collection and appends the survivors, keeping static/catalog/ in the exact shape
build_catalog.py writes (so the app serves them identically to resolved picks).

    python -m tools.clean_inject --collection ukiyo-e --items scratch/hiroshige_clean.json
    python -m tools.clean_inject -k impressionism -i clean.json --dry-run   # report, don't write

Dedup key = the Commons filename embedded in source_url (Special:FilePath/<fname>?width=…), which
is stable across the width variants a pick and a grab carry. Injected items intentionally omit
featured_rank / focal_point — those are backfilled later (fame_score.py, focal tools), not at inject.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = REPO_ROOT / "static" / "catalog"

# The fields a served catalog item must carry (matches build_catalog output). The optional
# ranking/focal fields are added by later backfill passes, so they are NOT required here.
REQUIRED = (
    "title", "agent_name", "creation_date", "cultural_context", "medium",
    "date_display", "description_narrative", "tags", "source", "license",
    "source_url", "thumbnail_url",
)

_FILEPATH_RE = re.compile(r"/Special:FilePath/([^?]+)")


def _fname_key(source_url: str) -> str:
    """Normalized Commons filename from a source_url, for dedup. Non-Commons urls key on the url."""
    m = _FILEPATH_RE.search(source_url or "")
    if not m:
        return (source_url or "").strip().lower()
    return unquote(m.group(1)).strip().lower()


def _validate(items: list) -> list:
    """Return a list of (index, missing_fields) problems; empty = all valid."""
    problems = []
    for i, it in enumerate(items):
        missing = [f for f in REQUIRED if not str(it.get(f) or "").strip()]
        if missing:
            problems.append((i, missing))
    return problems


def inject(collection: str, cleaned: list, *, dry_run: bool) -> dict:
    col_path = CATALOG_DIR / f"{collection}.json"
    if not col_path.exists():
        sys.exit(f"error: collection file not found: {col_path}")
    col = json.loads(col_path.read_text())
    items = col["items"]

    existing_keys = {_fname_key(it.get("source_url", "")) for it in items}
    added, skipped_dupe, batch_keys = [], 0, set()
    for it in cleaned:
        key = _fname_key(it.get("source_url", ""))
        if key in existing_keys or key in batch_keys:
            skipped_dupe += 1
            continue
        batch_keys.add(key)
        added.append(it)

    report = {
        "collection": collection,
        "before": len(items),
        "cleaned_in": len(cleaned),
        "skipped_dupe": skipped_dupe,
        "added": len(added),
        "after": len(items) + len(added),
    }
    if dry_run or not added:
        return report

    col["items"] = items + added
    col_path.write_text(json.dumps(col, indent=1, ensure_ascii=False))

    # Update the collection's count in index.json (leave cover_thumbnail + ordering + other cols).
    idx_path = CATALOG_DIR / "index.json"
    index = json.loads(idx_path.read_text())
    for c in index["collections"]:
        if c["id"] == collection:
            c["count"] = len(col["items"])
            break
    idx_path.write_text(json.dumps(index, indent=1, ensure_ascii=False))
    return report


def main():
    ap = argparse.ArgumentParser(description="Inject Sonnet-cleaned works into a served collection (ADR-040).")
    ap.add_argument("-k", "--collection", required=True, help="target collection id (e.g. ukiyo-e)")
    ap.add_argument("-i", "--items", required=True, help="cleaned items JSON (list of placard dicts)")
    ap.add_argument("--dry-run", action="store_true", help="report what would inject; write nothing")
    ap.add_argument("--allow-invalid", action="store_true", help="skip the required-field validation gate")
    args = ap.parse_args()

    cleaned = json.loads(Path(args.items).read_text())
    if not isinstance(cleaned, list):
        sys.exit("error: --items must be a JSON list of item dicts")

    problems = _validate(cleaned)
    if problems and not args.allow_invalid:
        for i, missing in problems[:20]:
            title = cleaned[i].get("title", "?")
            print(f"  item {i} ({title!r}) missing: {', '.join(missing)}", file=sys.stderr)
        sys.exit(f"error: {len(problems)} cleaned item(s) missing required fields "
                 f"(fix the clean stage, or --allow-invalid to force)")

    report = inject(args.collection, cleaned, dry_run=args.dry_run)
    tag = "DRY-RUN" if args.dry_run else "INJECTED"
    print(f"[{tag}] {report['collection']}: {report['before']} → {report['after']} "
          f"(+{report['added']} added, {report['skipped_dupe']} dupes skipped, "
          f"{report['cleaned_in']} cleaned in)", file=sys.stderr)


if __name__ == "__main__":
    main()
