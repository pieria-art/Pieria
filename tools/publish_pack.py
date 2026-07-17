"""Slice a built art-pack into per-collection modular packs + a registry (ADR-040 #4, ADR-038 R2 host).

The build (`tools/build_pack.py`) produces one big `./art-pack` (all 28 collections, deduped masters +
thumbs + one signed Manifest v2 per collection). This tool slices that into **per-collection artifacts**
so the appliance can bake a small **Core** set into the `.img` and let users **pull the rest by category**
on demand — "a pack is a pack" (ADR-040 #4): official theme packs and future publisher packs share the
same manifest + download + install flow.

For each collection it writes a self-contained mini-pack (its own `_Library/` + `_catalog_thumbs/` +
`_manifests/<id>.json` + a single-collection `pack-index.json`), tars it, and records
`{id, title, category, item_count, bytes, sha256, download, cover, core}` in **`packs.json`** — the
registry the "browse & download packs" UI reads. Masters shared across collections (a work in
Masterpieces *and* its home collection) are copied into each artifact so every download is
self-contained; the installer dedups by source_url on the device, so a re-downloaded master is harmless.

Each collection also gets a small **cover** image (`covers/<id>.jpg`) — the #1 fame-ranked work's
thumbnail (manifests are rank-sorted, so `items[0]`) — so the browse grid shows real art, never a blank
tile. `--covers-only` regenerates just the covers + `packs.json` cover fields against an existing dist,
WITHOUT re-taring (a cheap R2 cover refresh: upload `packs.json` + `covers/`, the big tars are untouched).

    python -m tools.publish_pack --pack ./art-pack --out ./art-pack-dist
    python -m tools.publish_pack --core masterpieces,impressionism,post-impressionism
    python -m tools.publish_pack --pack ./art-pack --out ./art-pack-dist --covers-only

Upload to Cloudflare R2 is a separate step (ADR-038 §5: publish-only token → Infisical, device curls a
public URL). This tool stays offline/deterministic; `--upload` is a documented seam, not implemented here.
"""
import argparse
import hashlib
import json
import shutil
import tarfile
from pathlib import Path

# Category for the browse UI = the collection's kind (painting/print/photo/…); Masterpieces is the overlay.
try:
    from tools.catalog_spec import COLLECTION_KIND
except Exception:  # noqa: BLE001 — catalog_spec is maintainer-only; degrade to "mixed" if unavailable
    COLLECTION_KIND = {}

# Default Core set baked into the .img (ADR-040 #4b: "Masterpieces + essential paintings") — small so the
# image flashes fast (gramps-test + giftable). Override with --core. Everything else is an on-demand pull.
DEFAULT_CORE = ("masterpieces", "impressionism", "post-impressionism")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _thumb_name(thumbnail_url: str | None) -> str | None:
    """`pack:_catalog_thumbs/<hash>.jpg` (or a bare name) -> `<hash>.jpg`."""
    if not thumbnail_url:
        return None
    return thumbnail_url.rsplit("/", 1)[-1] or None


def _category(cid: str) -> str:
    return COLLECTION_KIND.get(cid) or ("featured" if cid == "masterpieces" else "mixed")


def _cover_candidates(pack: Path, cid: str) -> list[tuple[str, Path]]:
    """Ordered (thumb_name, src_path) for a collection's items that have a thumbnail on disk. Manifests
    are rank-sorted (build_pack `_emit_v2_manifests`), so index 0 is the #1 fame-ranked work."""
    mpath = pack / "_manifests" / f"{cid}.json"
    if not mpath.exists():
        return []
    manifest = json.loads(mpath.read_text())
    out = []
    for item in manifest.get("items", []):
        tn = _thumb_name((item.get("image") or {}).get("thumbnail_url"))
        if not tn:
            continue
        src = pack / "_catalog_thumbs" / tn
        if src.exists():
            out.append((tn, src))
    return out


def _pick_cover(cid: str, candidates: list[tuple[str, Path]],
                master_thumb: str | None) -> tuple[str, Path] | None:
    """The cover = the top fame-ranked work's thumbnail, EXCEPT: a non-masterpieces collection whose #1
    work is Masterpieces' cover too (e.g. Renaissance's Mona Lisa) falls back to its #2 work, so tiles
    don't all show the same image. Keeps item[0] if there's no alternative."""
    if not candidates:
        return None
    chosen = candidates[0]
    if cid != "masterpieces" and master_thumb and chosen[0] == master_thumb and len(candidates) > 1:
        chosen = candidates[1]
    return chosen


def emit_covers(pack: Path, out: Path, rows: list[dict]) -> None:
    """Copy each collection's cover thumbnail to `out/covers/<id>.jpg` and set `row["cover"]`. Idempotent;
    tar-free (safe for `--covers-only`). A collection with no on-disk thumbnail gets `cover: None`."""
    covers_dir = out / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    cands = {r["id"]: _cover_candidates(pack, r["id"]) for r in rows}
    master = cands.get("masterpieces") or []
    master_thumb = master[0][0] if master else None
    for r in rows:
        chosen = _pick_cover(r["id"], cands.get(r["id"]) or [], master_thumb)
        if chosen is None:
            r["cover"] = None
            print(f"  ~ {r['id']}: no cover thumbnail available")
            continue
        shutil.copy2(chosen[1], covers_dir / f"{r['id']}.jpg")
        r["cover"] = f"covers/{r['id']}.jpg"


def slice_collection(pack: Path, col: dict, out: Path) -> dict | None:
    """Write one collection's self-contained mini-pack under `out/<id>/`, tar it, and return its registry
    row. Returns None (skips) if the manifest is missing/unreadable."""
    cid = col["id"]
    mpath = pack / col.get("manifest", f"_manifests/{cid}.json")
    if not mpath.exists():
        print(f"  x {cid}: missing manifest {mpath.name} — skipped")
        return None
    manifest = json.loads(mpath.read_text())

    stage = out / cid
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "_Library").mkdir(parents=True)
    (stage / "_catalog_thumbs").mkdir(parents=True)
    (stage / "_manifests").mkdir(parents=True)

    master_bytes = 0
    missing = 0
    for item in manifest.get("items", []):
        img = item.get("image") or {}
        lf = img.get("local_file")
        if lf:
            src = pack / "_Library" / lf
            if src.exists():
                dst = stage / "_Library" / lf
                shutil.copy2(src, dst)
                master_bytes += dst.stat().st_size
            else:
                missing += 1
        tn = _thumb_name(img.get("thumbnail_url"))
        if tn:
            tsrc = pack / "_catalog_thumbs" / tn
            if tsrc.exists():
                shutil.copy2(tsrc, stage / "_catalog_thumbs" / tn)

    # The mini-pack is itself a valid single-collection pack (installs via install_pack_subscriptions OR
    # the runtime append path) — carry the manifest + a one-entry pack-index so it stands alone.
    shutil.copy2(mpath, stage / "_manifests" / f"{cid}.json")
    (stage / "pack-index.json").write_text(json.dumps({
        "pack_version": "2",
        "publisher": manifest.get("publisher"),
        "collections": [{"id": cid, "title": manifest.get("title") or cid,
                         "manifest": f"_manifests/{cid}.json",
                         "item_count": len(manifest.get("items", [])), "default": False}],
    }, indent=1, ensure_ascii=False))

    tar_path = out / f"{cid}.tar"
    with tarfile.open(tar_path, "w") as tf:
        tf.add(stage, arcname=cid)
    shutil.rmtree(stage)  # keep only the tarball (the downloadable artifact)

    if missing:
        print(f"  ~ {cid}: {missing} item(s) had no on-disk master (skipped)")
    row = {
        "id": cid,
        "title": manifest.get("title") or cid,
        "category": _category(cid),
        "item_count": len(manifest.get("items", [])),
        "bytes": tar_path.stat().st_size,
        "master_bytes": master_bytes,
        "sha256": _sha256(tar_path),
        "download": tar_path.name,
        "trust": None,  # set by the device from the signed manifest at install (assess_trust); informational here
    }
    print(f"  ✓ {cid:<26} {row['item_count']:>4} works  {row['bytes']/1e6:7.1f} MB  [{row['category']}]")
    return row


def publish(pack: Path, out: Path, core: set[str], only: set[str] | None = None) -> dict:
    index = json.loads((pack / "pack-index.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for col in index.get("collections", []):
        if only and col["id"] not in only:
            continue
        row = slice_collection(pack, col, out)
        if row is None:
            continue
        row["core"] = row["id"] in core
        rows.append(row)

    emit_covers(pack, out, rows)  # cover art per collection -> covers/<id>.jpg + row["cover"]
    core_ids = sorted(r["id"] for r in rows if r["core"])
    ids = {r["id"] for r in rows}
    # The OOB first-glimpse: what a fresh install pulls from R2 and sets as the default playlist.
    default_id = ("masterpieces" if "masterpieces" in ids
                  else (core_ids[0] if core_ids else (rows[0]["id"] if rows else None)))
    registry = {
        "registry_version": "1",
        "publisher": index.get("publisher"),
        "public_key": index.get("public_key"),
        "core": core_ids,
        "default": default_id,
        "collections": rows,
    }
    (out / "packs.json").write_text(json.dumps(registry, indent=1, ensure_ascii=False))
    return registry


def publish_covers_only(pack: Path, out: Path) -> dict:
    """Regenerate `covers/` + patch `cover` into every row of an EXISTING `packs.json`, without re-taring.
    A cheap R2 cover refresh: only `packs.json` + `covers/` change; the (large) per-collection tars stand."""
    reg_path = out / "packs.json"
    if not reg_path.exists():
        raise SystemExit(f"--covers-only needs an existing {reg_path}; run a full publish first")
    registry = json.loads(reg_path.read_text())
    emit_covers(pack, out, registry.get("collections", []))
    reg_path.write_text(json.dumps(registry, indent=1, ensure_ascii=False))
    return registry


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", type=Path, default=Path("./art-pack"), help="built pack dir (has pack-index.json)")
    ap.add_argument("--out", type=Path, default=Path("./art-pack-dist"), help="output dir for per-collection tars + packs.json")
    ap.add_argument("--core", default=",".join(DEFAULT_CORE),
                    help="comma list of collection ids baked into the .img (the rest are on-demand pulls)")
    ap.add_argument("--collections", default=None,
                    help="comma list of collection ids to slice (default: all). Incremental re-publish.")
    ap.add_argument("--covers-only", action="store_true",
                    help="regenerate covers/ + patch cover fields into an existing packs.json WITHOUT "
                         "re-taring (cheap R2 cover refresh: upload packs.json + covers/, tars untouched)")
    args = ap.parse_args()

    if args.covers_only:
        reg = publish_covers_only(args.pack, args.out)
        n = sum(1 for r in reg["collections"] if r.get("cover"))
        print("\n=== COVERS-ONLY ===")
        print(f"covers refreshed: {n}/{len(reg['collections'])} collections -> {args.out}/covers/ + packs.json")
        print(f"Next: upload {args.out}/packs.json + {args.out}/covers/ to R2 (the tars are unchanged).")
        return 0

    if not (args.pack / "pack-index.json").exists():
        print(f"FAIL: no pack-index.json under {args.pack}")
        return 1
    core = {c.strip() for c in args.core.split(",") if c.strip()}
    only = {c.strip() for c in args.collections.split(",") if c.strip()} if args.collections else None

    reg = publish(args.pack, args.out, core, only)
    core_rows = [r for r in reg["collections"] if r["core"]]
    core_mb = sum(r["bytes"] for r in core_rows) / 1e6
    total_mb = sum(r["bytes"] for r in reg["collections"]) / 1e6
    print("\n=== PUBLISH SUMMARY ===")
    print(f"collections:  {len(reg['collections'])}  ->  {args.out}/packs.json")
    print(f"CORE (.img):  {len(core_rows)} pack(s), {core_mb:.0f} MB  [{', '.join(reg['core']) or '(none)'}]")
    print(f"on-demand:    {len(reg['collections']) - len(core_rows)} pack(s), {total_mb - core_mb:.0f} MB")
    print(f"total:        {total_mb:.0f} MB across {len(reg['collections'])} downloadable packs")
    print(f"\nNext: upload {args.out}/ to Cloudflare R2 (ADR-038 §5); serve packs.json + the tars behind curwe.ai.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
