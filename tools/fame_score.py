"""Fame-score enrichment for the bundled catalog (offline maintainer tool — NOT runtime).

Scores each work's cultural fame / recognizability 0-100. That single number drives three things
(ADR-038): the browse sort ("Start Here" surfaces the famous works first), the affinity-weighted
playback shuffle (seeded from this at pre-seed time), and a synthesized **Greatest Hits** collection
— the zero-choice out-of-box default rotation.

Follows the tools/backfill_focal_* pattern: a **Claude Code Sonnet fan-out** does the judgment in-IDE
and bakes results here. This deliberately does NOT use the app's ai_client / LiteLLM gateway — fame is
a build-time dev enrichment (Sonnet knows "Mona Lisa"=99, an obscure study=20 from title+artist alone,
no pixels needed), so there is no runtime model call and no app spend.

    python -m tools.fame_score --emit worklist.json     # dump [{source_url,title,artist,collection}] (deduped)
    #   ...Sonnet scores each -> scores.json: [{"source_url": "...", "featured_rank": 0-100}]
    python -m tools.fame_score --bake scores.json        # write featured_rank into static/catalog/*.json
    python -m tools.fame_score --bake scores.json --top 40 --dry-run   # preview Greatest Hits, write nothing
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.catalog_spec import PAINTERLY_KINDS, kind_for

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = REPO_ROOT / "static" / "catalog"
INDEX_FILE = CATALOG_DIR / "index.json"
# CURATION-v2 (ADR-039): the out-of-box first-glimpse is a paintings-only "Masterpieces" set, replacing
# the old top-fame "Greatest Hits" (which led with sculptures/photos/posters/space imagery).
MASTERPIECES_ID = "masterpieces"
MASTERPIECES_TITLE = "Masterpieces"
MASTERPIECES_DESC = "The most iconic paintings of all time — start here."
LEGACY_GH_ID = "greatest-hits"   # retired synthesized collection; cleaned up on bake


def _catalog_files() -> list[Path]:
    """Every real collection file (skip `_`-prefixed, index.json, and the synthesized first-glimpse —
    both the current Masterpieces and the retired Greatest Hits, so neither is scored as a source)."""
    skip = ("index.json", f"{MASTERPIECES_ID}.json", f"{LEGACY_GH_ID}.json")
    return sorted(f for f in CATALOG_DIR.glob("*.json")
                  if not f.name.startswith("_") and f.name not in skip)


# --------------------------------------------------------------------------- emit
def emit(out_path: Path) -> int:
    """Flatten the catalog to a deduped worklist (one row per unique source_url) for Sonnet to score."""
    seen: set[str] = set()
    work: list[dict] = []
    for f in _catalog_files():
        d = json.loads(f.read_text())
        for it in d.get("items", []):
            su = it.get("source_url")
            if not su or su in seen:
                continue
            seen.add(su)
            work.append({
                "source_url": su,
                "title": it.get("title", ""),
                "artist": it.get("agent_name", ""),
                "collection": d.get("id", f.stem),
            })
    out_path.write_text(json.dumps(work, indent=1, ensure_ascii=False))
    print(f"emitted {len(work)} unique works -> {out_path}")
    return 0


# --------------------------------------------------------------------------- bake
def _load_scores(scores_path: Path) -> dict[str, int]:
    raw = json.loads(scores_path.read_text())
    rows = raw.get("scores", raw) if isinstance(raw, dict) else raw
    scores: dict[str, int] = {}
    for r in rows:
        su, rank = r.get("source_url"), r.get("featured_rank")
        if su is None or rank is None:
            continue
        scores[su] = max(0, min(100, int(round(float(rank)))))
    return scores


def bake(scores_path: Path, top: int, dry_run: bool) -> int:
    scores = _load_scores(scores_path)
    print(f"loaded {len(scores)} scores from {scores_path}")

    updated = missing = 0
    # Greatest Hits candidates are deduped by WORK, not source_url: the same painting can appear in
    # several collections under different Commons files (A11) — we don't want it twice in Greatest Hits.
    candidates: dict[tuple, dict] = {}   # (title, artist) -> best-ranked item for that work
    for f in _catalog_files():
        d = json.loads(f.read_text())
        # Masterpieces is paintings-only: a collection's `kind` (from catalog_spec) gates first-glimpse
        # eligibility, so sculpture/photo/poster/space/artifact collections never feed the candidate pool.
        painterly = kind_for(d.get("id", f.stem)) in PAINTERLY_KINDS
        changed = False
        for it in d.get("items", []):
            su = it.get("source_url")
            if su in scores:
                if it.get("featured_rank") != scores[su]:
                    it["featured_rank"] = scores[su]
                    changed = True
                updated += 1
            else:
                missing += 1
            # Masterpieces candidacy uses the item's EFFECTIVE rank (this batch's score if present, else
            # the already-baked featured_rank), so a partial re-score still builds the first-glimpse from
            # the whole scored catalog rather than only the works in this scores file.
            rank = it.get("featured_rank")
            if painterly and rank is not None:
                # normalize the title (drop parenthetical/series suffixes like "(…nami ura), from the
                # series …") so two museum records of the SAME work collapse to one Masterpieces entry.
                norm_title = it.get("title", "").split("(")[0].split(",")[0].strip().lower()
                key = (norm_title, (it.get("agent_name") or "").strip().lower())
                prev = candidates.get(key)
                if prev is None or rank > prev.get("featured_rank", -1):
                    candidates[key] = it
        if changed and not dry_run:
            f.write_text(json.dumps(d, indent=1, ensure_ascii=False))

    top_items = sorted(candidates.values(), key=lambda it: it.get("featured_rank", 0), reverse=True)[:top]
    print(f"featured_rank written: {updated} items updated, {missing} items had no score")
    print(f"\nMasterpieces (paintings-only, top {len(top_items)}):")
    for it in top_items[:12]:
        print(f"  {it.get('featured_rank', 0):3}  {it.get('title', '?')[:44]:44} {it.get('agent_name', '')[:24]}")
    if len(top_items) > 12:
        print(f"  … +{len(top_items) - 12} more")

    if dry_run:
        print("\n[dry-run] no files written")
        return 0

    # Synthesize the Masterpieces collection file (build_pack + pre-seed consume it like any collection;
    # items are dup-by-source_url of their home collections, which the downstream dedup handles).
    mp = {"id": MASTERPIECES_ID, "title": MASTERPIECES_TITLE, "description": MASTERPIECES_DESC,
          "source": "Screen Docent", "license": "Public Domain", "items": top_items}
    (CATALOG_DIR / f"{MASTERPIECES_ID}.json").write_text(json.dumps(mp, indent=1, ensure_ascii=False))

    # Retire the old Greatest Hits synthesized collection if present (renamed to Masterpieces).
    legacy = CATALOG_DIR / f"{LEGACY_GH_ID}.json"
    if legacy.exists():
        legacy.unlink()

    # Register Masterpieces in index.json (front of the list so browse shows it first); drop any stale
    # Masterpieces/Greatest Hits entry first.
    index = json.loads(INDEX_FILE.read_text())
    cols = [c for c in index.get("collections", []) if c.get("id") not in (MASTERPIECES_ID, LEGACY_GH_ID)]
    cover = top_items[0].get("thumbnail_url", "") if top_items else ""
    cols.insert(0, {"id": MASTERPIECES_ID, "title": MASTERPIECES_TITLE, "description": MASTERPIECES_DESC,
                    "source": "Screen Docent", "license": "Public Domain",
                    "count": len(top_items), "cover_thumbnail": cover})
    index["collections"] = cols
    INDEX_FILE.write_text(json.dumps(index, indent=1, ensure_ascii=False))
    print(f"\nwrote static/catalog/{MASTERPIECES_ID}.json ({len(top_items)} items) + registered in index.json"
          + ("; retired greatest-hits.json" if not legacy.exists() else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit", type=Path, metavar="OUT", help="dump the worklist for Sonnet to score")
    g.add_argument("--bake", type=Path, metavar="SCORES", help="write featured_rank + build Greatest Hits")
    ap.add_argument("--top", type=int, default=40, help="Greatest Hits size (default 40)")
    ap.add_argument("--dry-run", action="store_true", help="with --bake: preview, write nothing")
    args = ap.parse_args()

    if args.emit:
        return emit(args.emit)
    return bake(args.bake, top=args.top, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
