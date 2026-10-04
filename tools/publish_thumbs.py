"""Self-hosted catalog thumbnails (ADR-148): `thumbs/<sha256(source_url)>.jpg` + `thumbs/index.json`.

The bundled catalog used to hotlink Commons/NASA (`Special:FilePath?width=600`, 1-3.5 MB NASA PNGs). This
tool produces ONE small (~600px long edge, JPEG q82, progressive, EXIF/ICC stripped, sRGB) thumbnail per
catalog item, to be uploaded to R2 beside the packs (`https://packs.curwe.ai/thumbs/<name>.jpg`), and can
rewrite `static/catalog/*.json` to point at them (original kept as `thumbnail_source_url`).

Source of each thumbnail, in order (no network unless `--fetch-remote`):
  1. the pack's existing `_catalog_thumbs/<sha1[:12]>.jpg` (found by source_url hash, or via the pack
     manifests' `origin_url`/title for works whose pack master was pinned to another host);
  2. derived from the local pack master under `<pack>/_Library/` (READ-ONLY, streamed one at a time);
  3. only with `--fetch-remote`: the item's remote thumbnail via build_pack's downloader.

    python -m tools.publish_thumbs --pack ./art-pack --out ./art-pack-dist/thumbs
    python -m tools.publish_thumbs --pack ./art-pack --out ./art-pack-dist/thumbs --rewrite-catalog

Never copies the pack. Upload (Josh): see docs/how-to-publish.md "Catalog thumbnails".
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

from config import PACK_REGISTRY_URL

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = REPO_ROOT / "static" / "catalog"

THUMB_MAX_EDGE = 600
THUMB_QUALITY = 82
THUMBS_PREFIX = "thumbs"
_SKIP_JSON = {"index.json"}

_SRGB = ImageCms.createProfile("sRGB")


def default_base_url() -> str:
    """`https://packs.curwe.ai` — the registry base the app already uses (config.PACK_REGISTRY_URL)."""
    return PACK_REGISTRY_URL.rsplit("/", 1)[0]


def thumb_key(source_url: str) -> str:
    """Deterministic object name stem: sha256 hex of the item's source_url."""
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def thumb_name(source_url: str) -> str:
    return f"{thumb_key(source_url)}.jpg"


def thumb_url(base_url: str, source_url: str) -> str:
    return f"{base_url.rstrip('/')}/{THUMBS_PREFIX}/{thumb_name(source_url)}"


# --------------------------------------------------------------------------- encoding
def derive_thumbnail(src) -> bytes:
    """Image (path or bytes) -> ~600px JPEG q82 progressive, sRGB, no EXIF/ICC. Never upscales."""
    from tools import build_pack  # lazy: heavy imports

    fp = BytesIO(src) if isinstance(src, (bytes, bytearray)) else src
    with Image.open(fp) as img:
        icc = img.info.get("icc_profile")
        if img.format == "JPEG":
            img.draft("RGB", (THUMB_MAX_EDGE * 2, THUMB_MAX_EDGE * 2))  # fast decode of huge masters
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = build_pack._to_rgb8(img)
        if icc:
            try:
                img = ImageCms.profileToProfile(img, ImageCms.ImageCmsProfile(BytesIO(icc)), _SRGB,
                                                outputMode="RGB")
            except Exception:  # noqa: BLE001 — a broken profile must not fail the thumb
                pass
        img.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=THUMB_QUALITY, progressive=True, optimize=True)  # no exif/icc
        return buf.getvalue()


# --------------------------------------------------------------------------- inventory
def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def load_catalog_items(catalog_dir: Path) -> list[dict]:
    """[{source_url, thumbnail_url, title, collection}] for every bundled catalog item."""
    out = []
    for f in sorted(catalog_dir.glob("*.json")):
        if f.name in _SKIP_JSON or f.name.startswith("_"):
            continue
        col = json.loads(f.read_text())
        for it in col.get("items", []):
            if it.get("source_url"):
                out.append({"source_url": it["source_url"],
                            "thumbnail_url": it.get("thumbnail_source_url") or it.get("thumbnail_url"),
                            "title": it.get("title"), "collection": col.get("id") or f.stem})
    return out


def load_pack_index(pack: Path) -> dict:
    """origin_url -> (thumb file, master file); (collection, title) -> same; plus pack-only rows."""
    by_origin, by_title, rows = {}, {}, []
    mdir = pack / "_manifests"
    if not mdir.is_dir():
        return {"by_origin": by_origin, "by_title": by_title, "rows": rows}
    for mf in sorted(mdir.glob("*.json")):
        m = json.loads(mf.read_text())
        cid = m.get("id") or mf.stem
        for it in m.get("items", []):
            img = it.get("image") or {}
            th = (img.get("thumbnail_url") or "").rsplit("/", 1)[-1] or None
            ref = {"thumb": th, "master": img.get("local_file"), "origin": img.get("origin_url"),
                   "collection": cid, "title": it.get("title")}
            rows.append(ref)
            if ref["origin"]:
                by_origin.setdefault(ref["origin"], ref)
            by_title.setdefault((cid, _norm(it.get("title"))), ref)
    return {"by_origin": by_origin, "by_title": by_title, "rows": rows}


def _find_local(pack: Path, idx: dict, item: dict) -> tuple[Path, str] | None:
    """(path, kind) of the best local source for a catalog item, or None."""
    from tools import build_pack

    su = item["source_url"]
    ref = idx["by_origin"].get(su) or idx["by_title"].get((item.get("collection"), _norm(item.get("title"))))
    cands = [build_pack.thumb_filename(su)]
    if ref and ref.get("thumb"):
        cands.append(ref["thumb"])
    for c in cands:
        p = pack / "_catalog_thumbs" / c
        if p.is_file():
            return p, "pack-thumb"
    if ref and ref.get("master"):
        p = pack / "_Library" / ref["master"]
        if p.is_file():
            return p, "master"
    return None


# --------------------------------------------------------------------------- build
def build_thumbs(catalog_dir: Path, pack: Path, out: Path, *, fetch_remote: bool = False,
                 include_pack_only: bool = True) -> dict:
    """Write out/<name>.jpg for every item + out/index.json. Returns the index dict (with a transient
    `_failed` list). Streams one image at a time; reads the pack read-only."""
    out.mkdir(parents=True, exist_ok=True)
    items = load_catalog_items(catalog_dir)
    idx = load_pack_index(pack)
    known = {i["source_url"] for i in items}
    if include_pack_only:  # works that live only in a pack (parked/pinned) still get a thumbnail
        for r in idx["rows"]:
            if r["origin"] and r["origin"] not in known:
                known.add(r["origin"])
                items.append({"source_url": r["origin"], "thumbnail_url": None, "title": r["title"],
                              "collection": r["collection"]})
    entries, failed, remote_needed = {}, [], []
    for it in items:
        su = it["source_url"]
        if su in entries:
            continue
        found = _find_local(pack, idx, it)
        if found is None:
            if fetch_remote and it.get("thumbnail_url"):
                remote_needed.append(it)
            else:
                failed.append(su)
            continue
        try:
            data = derive_thumbnail(found[0])
        except Exception as e:  # noqa: BLE001
            failed.append(su)
            print(f"  x {su}: {e}", file=sys.stderr)
            continue
        entries[su] = _write(out, su, data, found[1])
    if remote_needed:
        entries_r, failed_r = asyncio.run(_fetch_all(remote_needed, out))
        entries.update(entries_r)
        failed.extend(failed_r)
    index = {"version": 1, "items": dict(sorted(entries.items()))}
    (out / "index.json").write_text(json.dumps(index, indent=1, ensure_ascii=False))
    index["_failed"] = failed
    return index


def _write(out: Path, su: str, data: bytes, origin: str) -> dict:
    name = thumb_name(su)
    (out / name).write_bytes(data)
    return {"path": f"{THUMBS_PREFIX}/{name}", "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "origin": origin}


async def _fetch_all(items: list[dict], out: Path):
    import httpx

    from config import SD_USER_AGENT
    from tools import build_pack

    entries, failed = {}, []
    async with httpx.AsyncClient(headers={"User-Agent": SD_USER_AGENT}) as client:
        for it in items:  # sequential: the downloader already throttles per host
            su = it["source_url"]
            raw = await build_pack._fetch_bytes(client, it["thumbnail_url"])
            try:
                entries[su] = _write(out, su, derive_thumbnail(raw), "remote")
            except Exception:  # noqa: BLE001 — None bytes / undecodable
                failed.append(su)
    return entries, failed


# --------------------------------------------------------------------------- catalog rewrite
def rewrite_catalog(catalog_dir: Path, index_items: dict, base_url: str) -> dict:
    """Point each item's thumbnail_url at R2, keeping the original as `thumbnail_source_url`. Only items
    with an entry in the thumbs index are rewritten (a missing thumb keeps its working hotlink).
    Idempotent: the provenance URL is never overwritten once set."""
    changed = total = 0
    for f in sorted(catalog_dir.glob("*.json")):
        if f.name in _SKIP_JSON or f.name.startswith("_"):
            continue
        col = json.loads(f.read_text())
        for it in col.get("items", []):
            total += 1
            su = it.get("source_url")
            if su not in index_items:
                continue
            if "thumbnail_source_url" not in it:
                it["thumbnail_source_url"] = it.get("thumbnail_url")
            new = thumb_url(base_url, su)
            if it.get("thumbnail_url") != new:
                it["thumbnail_url"] = new
                changed += 1
        f.write_text(json.dumps(col, indent=1, ensure_ascii=False))
    ip = catalog_dir / "index.json"
    if ip.exists():
        idx = json.loads(ip.read_text())
        for c in idx.get("collections", []):
            cf = catalog_dir / f"{c['id']}.json"
            if not cf.exists():
                continue
            items = json.loads(cf.read_text()).get("items") or []
            if items and items[0].get("source_url") in index_items:
                c.setdefault("cover_thumbnail_source", c.get("cover_thumbnail"))
                c["cover_thumbnail"] = items[0]["thumbnail_url"]
        ip.write_text(json.dumps(idx, indent=1, ensure_ascii=False))
    return {"items": total, "rewritten": changed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pack", type=Path, default=Path("art-pack"), help="built art-pack (read-only)")
    ap.add_argument("--out", type=Path, default=Path("art-pack-dist/thumbs"), help="output thumbs dir")
    ap.add_argument("--catalog-dir", type=Path, default=CATALOG_DIR)
    ap.add_argument("--base-url", default=default_base_url(), help="public base (default: pack registry host)")
    ap.add_argument("--fetch-remote", action="store_true", help="LAST RESORT: download missing originals")
    ap.add_argument("--rewrite-catalog", action="store_true", help="rewrite static/catalog/*.json to R2 URLs")
    a = ap.parse_args(argv)
    index = build_thumbs(a.catalog_dir, a.pack, a.out, fetch_remote=a.fetch_remote)
    failed = index.pop("_failed")
    n = len(index["items"])
    size = sum(e["bytes"] for e in index["items"].values())
    print(f"thumbs: {n} written, {size / 1e6:.1f} MB -> {a.out}; {len(failed)} without a source")
    for su in failed[:20]:
        print(f"  ! no source: {su}")
    if a.rewrite_catalog:
        print("catalog:", rewrite_catalog(a.catalog_dir, index["items"], a.base_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
