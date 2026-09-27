"""One-shot: normalize `license` free text -> a core.licensing id in every served catalog file AND
static/catalog/_pack_pins.json, and fill `license_url` from that id (Stage A #2 of
.ai/spec_ccby_attribution.md, ADR-142).

Computes every row's normalized id FIRST; if any row fails to normalize (share-alike/non-commercial/
no-derivatives/unrecognized), nothing is written — the run STOPS and lists every offending row (as of
2026-09-27 all 2,840 served rows + 135 pin rows are PD, so this should never trip in practice).

Preserves each file's exact format: json.dumps(indent=1, ensure_ascii=False), and each file's own
trailing-newline state (most static/catalog/*.json have none; _pack_pins.json ends with one).

    python -m tools.migrate_catalog_licenses            # apply
    python -m tools.migrate_catalog_licenses --dry-run   # report only, never writes
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.licensing import LICENSE_URLS, normalize_license  # noqa: E402

CATALOG_DIR = REPO_ROOT / "static" / "catalog"
PINS_FILE = CATALOG_DIR / "_pack_pins.json"


def _catalog_files() -> list[Path]:
    # Mirrors tools/audit_licenses.py:load_items' file-selection rule.
    return sorted(f for f in CATALOG_DIR.glob("*.json") if not f.name.startswith("_") and "index" not in f.name)


def _write(path: Path, data: dict) -> None:
    had_nl = path.read_bytes().endswith(b"\n")
    text = json.dumps(data, indent=1, ensure_ascii=False)
    path.write_text(text + "\n" if had_nl else text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report counts/failures only, never writes")
    args = ap.parse_args()

    counts: Counter[str] = Counter()
    failures: list[tuple[str, str, object]] = []
    catalog_docs: list[tuple[Path, dict]] = []

    for f in _catalog_files():
        d = json.loads(f.read_text())
        for it in d.get("items", []):
            lic_id = normalize_license(it.get("license"))
            if lic_id is None:
                failures.append((f.name, it.get("title", "?"), it.get("license")))
            else:
                counts[lic_id] += 1
        catalog_docs.append((f, d))

    pins = json.loads(PINS_FILE.read_text())
    for cid, items in pins.get("collections", {}).items():
        for it in items:
            lic_id = normalize_license(it.get("license"))
            if lic_id is None:
                failures.append((f"_pack_pins.json[{cid}]", it.get("title", "?"), it.get("license")))
            else:
                counts[lic_id] += 1

    if failures:
        print(f"STOP: {len(failures)} row(s) failed to normalize a license — NOTHING WRITTEN:", file=sys.stderr)
        for fname, title, raw in failures[:80]:
            print(f"  x [{fname}] {title!r}: license={raw!r}", file=sys.stderr)
        if len(failures) > 80:
            print(f"  … and {len(failures) - 80} more", file=sys.stderr)
        return 1

    print("=== LICENSE NORMALIZATION (computed) ===")
    for lic_id, n in counts.most_common():
        print(f"  {n:5}  {lic_id}")
    print(f"total: {sum(counts.values())} rows")

    if args.dry_run:
        print("\n--dry-run: no files written")
        return 0

    files_changed = 0
    for f, d in catalog_docs:
        changed = False
        for it in d.get("items", []):
            lic_id = normalize_license(it.get("license"))
            if it.get("license") != lic_id:
                it["license"] = lic_id
                changed = True
            url = LICENSE_URLS[lic_id]
            if it.get("license_url") != url:
                it["license_url"] = url
                changed = True
        if changed:
            _write(f, d)
            files_changed += 1

    pins_changed = False
    for cid, items in pins.get("collections", {}).items():
        for it in items:
            lic_id = normalize_license(it.get("license"))
            if it.get("license") != lic_id:
                it["license"] = lic_id
                pins_changed = True
            url = LICENSE_URLS[lic_id]
            if it.get("license_url") != url:
                it["license_url"] = url
                pins_changed = True
    if pins_changed:
        _write(PINS_FILE, pins)
        files_changed += 1

    print(f"\nwrote {files_changed} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
