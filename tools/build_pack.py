"""Assemble a self-contained, offline "art-pack" (maintainer tool — NOT part of the runtime image).

ADR-038: Screen Docent moves from "link, don't own" (catalog JSON carries a `source_url`/
`thumbnail_url`, images download live at first-seed and at each catalog "Add") to shipping the
appliance with the curated collection **baked in**. This is the assembly step: it downloads every
catalog + factory-seed image once, caps it to display resolution, dedupes shared source files,
and writes a manifest a runtime consumer (built separately) reads to serve the collection with no
network at all.

Deterministic core only — NO AI. A separate fame-score enrichment pass and the runtime consumer
that reads pack-manifest.json are out of scope here; this tool only has to emit a manifest they can
depend on.

    python -m tools.build_pack --out ./art-pack                      # whole catalog
    python -m tools.build_pack --out ./art-pack --limit 20            # quick smoke run
    python -m tools.build_pack --out ./art-pack --collections impressionism,baroque
    python -m tools.build_pack --out /mnt/big/art-pack --concurrency 8

Resumable: a master already on disk (deterministic filename) is never re-downloaded, so a failed or
partial run can just be re-run to top up. A failed item (download error, SSRF-blocked redirect,
non-image response, decode failure) is logged and skipped — it never aborts the run.

Layout produced under --out:
    _Library/<filename>.jpg          canvas-ready <=7680px-long-edge progressive JPEG masters
    _catalog_thumbs/<hash>.jpg       ~600px-long-edge local thumbnails
    _catalog/<id>.json               verbatim copy of each static/catalog/<id>.json used
    pack-manifest.json               the manifest a runtime consumer depends on (see below)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import httpx
from PIL import Image, ImageOps

import federation
import publisher
from config import SD_USER_AGENT
from core.media import DISPLAY_MAX_EDGE, DISPLAY_QUALITY
from scout import _wm_throttle
from tools import aic_tiles

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("build-pack")

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_FILE = REPO_ROOT / "static" / "factory_seed.json"
CATALOG_DIR = REPO_ROOT / "static" / "catalog"

WIKIMEDIA_HOSTS = ("commons.wikimedia.org", "upload.wikimedia.org")
THUMB_MAX_EDGE = 600
THUMB_QUALITY = 85

# Per-collection native floor overrides (ADR-042): photochrom/architectural source ceilings sit just
# under true-4K, so cities-architecture relaxes to 3600. Everything else uses the global --min-edge.
COLLECTION_MIN_EDGE = {"cities-architecture": 3600}

# First-party publisher identity (ADR-044): the Core pack ships as this publisher's signed Manifest v2
# feeds — one per collection. Signed at build time with a key that never ships; the PUBLIC key lives in
# registry/trusted_publishers.json so assess_trust() promotes the pack to 'verified' on the appliance.
PACK_PUBLISHER = {"id": "screendocent", "name": "Screen Docent"}
# base64 Ed25519 private key (build secret, NEVER committed): CLI --signing-key or this env var.
SIGNING_KEY_ENV = "SD_PACK_SIGNING_KEY"

# Placard fields copied verbatim (in this order) from source item -> manifest item.
_PLACARD_FIELDS = (
    "title", "agent_name", "agent_role", "creation_date", "cultural_context", "medium", "kind",
    "date_display", "description_narrative", "tags", "source", "license", "needs_frame_crop",
)


# --------------------------------------------------------------------------- loading (no network)
def load_catalog_collections(collections_filter: set[str] | None) -> list[dict]:
    """Mirrors tools/audit_licenses.py:load_items' file-selection rule (skip `_`-prefixed / "index"
    files), but keeps each catalog file intact (id/title/description/items) instead of flattening —
    the manifest needs one entry per catalog file."""
    cols: list[dict] = []
    for f in sorted(CATALOG_DIR.glob("*.json")):
        if f.name.startswith("_") or "index" in f.name:
            continue
        d = json.loads(f.read_text())
        cid = d.get("id") or f.stem
        if collections_filter and cid not in collections_filter:
            continue
        d.setdefault("id", cid)
        cols.append(d)
    return cols


def load_seed_items() -> list[dict]:
    if not SEED_FILE.exists():
        return []
    data = json.loads(SEED_FILE.read_text())
    return data if isinstance(data, list) else (data.get("items") or [])


@dataclass
class WorkItem:
    kind: str            # "catalog" | "seed"
    collection_id: str    # catalog file's id, or "seed" for seed-only items
    item: dict


@dataclass
class Stats:
    master_downloaded: int = 0
    master_cached: int = 0
    master_failed: int = 0
    master_too_small: int = 0
    master_cropped: int = 0
    thumb_downloaded: int = 0
    thumb_cached: int = 0
    thumb_derived: int = 0
    thumb_failed: int = 0


@dataclass
class BuildState:
    pack_dir: Path
    client: httpx.AsyncClient
    sem: asyncio.Semaphore
    min_edge: int = 3840   # true-4K floor (CURATION-v2/ADR-039): native ≥4K fills a 4K panel 1:1 crisp;
    #                        Ken Burns zoom is capped per-work by native res (adaptive) so nothing softens
    stats: Stats = field(default_factory=Stats)
    url_to_master: dict[str, str] = field(default_factory=dict)
    url_to_thumb: dict[str, str] = field(default_factory=dict)
    url_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def lock_for(self, url: str) -> asyncio.Lock:
        lock = self.url_locks.get(url)
        if lock is None:
            lock = self.url_locks[url] = asyncio.Lock()
        return lock


# --------------------------------------------------------------------------- naming (deterministic)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _slugify(title: str) -> str:
    s = _SLUG_RE.sub("-", (title or "untitled").lower()).strip("-")
    return (s[:40] or "untitled")


def _sanitize(name: str) -> str:
    return _SAFE_CHARS.sub("", name)


def master_filename(collection_id: str, title: str, source_url: str) -> str:
    h = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:8]
    return _sanitize(f"{collection_id}__{_slugify(title)}__{h}.jpg")


def thumb_filename(source_url: str) -> str:
    return hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:12] + ".jpg"


# --------------------------------------------------------------------------- politeness
def _is_wikimedia_host(url: str) -> bool:
    return (urlparse(url).hostname or "") in WIKIMEDIA_HOSTS


def _pack_fetch_url(url: str) -> str:
    """The catalog stores Wikimedia `source_url`s capped at width=3840 (a live-serve convenience),
    which is *below* the pack's 4K floor — fetching them as-is would drop 86% of the catalog. For the
    pack we want native-max, so drop the width cap and fetch the ORIGINAL file. (Wikimedia caps
    on-the-fly thumbnail renders at 3840px — requesting width=5120/7680 still returns 3840 — so only
    the un-parameterised Special:FilePath original yields true native; `_cap_master` then downcaps it
    to DISPLAY_MAX_EDGE=7680.) Non-Wikimedia URLs (museum full/max originals) are already native-max and
    returned unchanged. See [[catalog-3840-vs-pack-5120]]."""
    if not _is_wikimedia_host(url) or "Special:FilePath" not in url:
        return url
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k != "width"]
    return urlunsplit(parts._replace(query=urlencode(q)))


async def _throttle_for(url: str) -> None:
    if _is_wikimedia_host(url):
        await _wm_throttle()
    else:
        await asyncio.sleep(0.3)


# --------------------------------------------------------------------------- SSRF-safe download
# MIRRORS core/downloads.py:_download_image_to_library's redirect/SSRF/429 handling exactly, but
# does NOT reuse it directly: that helper writes full-res bytes straight to LIBRARY_DIR with no size
# cap and a collision-rename scheme unrelated to ours. We need the bytes in memory so we can cap +
# re-encode before writing into the pack dir.

# Size-aware fetch (ADR-040 Step-5 caveat): native masters range from ~2 MB prints to ~100 MB+
# gigapixel paintings (Starry Night 44567px, The Scream .tif 73171px). A flat total-operation
# timeout starves the big ones — a legit 100 MB master can't arrive in 45 s. Instead we STREAM the
# body with a per-read timeout (each chunk must make progress, so a stalled socket still fails fast)
# and bound the total by BYTES, not seconds. FETCH_MAX_BYTES is the runaway/DoS ceiling; a master
# larger than this is skipped (logged), not downloaded forever.
FETCH_MAX_BYTES = 400 * 1024 * 1024  # 400 MB — comfortably above the largest real PD masters
# read=90s: max wait for the NEXT chunk (server-side render of a huge Wikimedia thumbnail can be slow
# to first byte); there is deliberately no total deadline — liveness + the byte cap bound the fetch.
FETCH_TIMEOUT = httpx.Timeout(connect=15.0, read=90.0, write=15.0, pool=15.0)


async def _fetch_bytes(client: httpx.AsyncClient, url: str, *, retries: int = 3) -> bytes | None:
    for attempt in range(retries):
        await _throttle_for(url)
        hop_url = url
        for _hop in range(6):
            try:
                async with client.stream("GET", hop_url, timeout=FETCH_TIMEOUT,
                                         follow_redirects=False) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            return None
                        hop_url = str(resp.url.join(loc))
                        try:
                            await asyncio.to_thread(federation._assert_public_url, hop_url)
                        except federation.FederationError as e:
                            logger.info(f"    x blocked redirect for {url} -> {hop_url}: {e}")
                            return None
                        continue  # next hop (context manager releases this connection)
                    if resp.status_code == 429:
                        break     # fall through to the 429 backoff below
                    if resp.status_code != 200:
                        logger.info(f"    x HTTP {resp.status_code} for {url}")
                        return None
                    ct = resp.headers.get("content-type", "").lower()
                    if not ct.startswith("image/"):
                        logger.info(f"    x not an image ({ct or 'unknown content-type'}) for {url}")
                        return None
                    chunks, total = [], 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > FETCH_MAX_BYTES:
                            logger.info(f"    x exceeds {FETCH_MAX_BYTES // (1024*1024)}MB cap for {url}")
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.HTTPError as e:
                logger.info(f"    x request failed for {url}: {e}")
                return None
        else:
            # ran out of redirect hops without a terminal response
            return None
        # reached only via the 429 `break`
        await asyncio.sleep(3 * (attempt + 1))
    logger.info(f"    x exhausted 429 retries for {url}")
    return None


# --------------------------------------------------------------------------- image processing
def _cap_master(raw: bytes) -> bytes | None:
    """The render_canvas_image capping recipe (core/media.py), applied once at build time instead of
    lazily at request time: exif-transpose, RGB, cap to DISPLAY_MAX_EDGE, progressive JPEG."""
    try:
        with Image.open(BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            if max(img.size) > DISPLAY_MAX_EDGE:
                img.thumbnail((DISPLAY_MAX_EDGE, DISPLAY_MAX_EDGE), Image.Resampling.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=DISPLAY_QUALITY, progressive=True)
            return buf.getvalue()
    except Exception as e:
        logger.info(f"    x could not decode/cap image: {e}")
        return None


def _apply_crop_box(raw: bytes, crop_box) -> bytes | None:
    """Crop the frame/mat/wall away from a master using a pre-baked normalized art rectangle
    crop_box=[x0,y0,x1,y1] (0..1). AI produces the box against the catalog thumbnail; because it is
    normalized it maps 1:1 onto the native master here (build_pack stays NO-AI). Returns re-encoded
    JPEG bytes, or None (caller keeps the uncropped raw) if the box is missing/degenerate/undecodable."""
    if not (isinstance(crop_box, (list, tuple)) and len(crop_box) == 4):
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in crop_box)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        return None
    # a near-full box is a no-op ("already clean" agents return [0,0,1,1]) — skip the re-encode
    if x0 <= 0.002 and y0 <= 0.002 and x1 >= 0.998 and y1 >= 0.998:
        return None
    try:
        with Image.open(BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            w, h = img.size
            left, upper = round(x0 * w), round(y0 * h)
            right, lower = round(x1 * w), round(y1 * h)
            if right - left < 1 or lower - upper < 1:
                return None
            cropped = img.crop((left, upper, right, lower))
            buf = BytesIO()
            cropped.save(buf, format="JPEG", quality=DISPLAY_QUALITY, progressive=True)
            return buf.getvalue()
    except Exception as e:
        logger.info(f"    x could not apply crop_box: {e}")
        return None


def _derive_thumbnail(raw: bytes) -> bytes | None:
    try:
        with Image.open(BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.Resampling.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=THUMB_QUALITY)
            return buf.getvalue()
    except Exception as e:
        logger.info(f"    x could not derive thumbnail: {e}")
        return None


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(data)
    tmp.rename(dest)


# --------------------------------------------------------------------------- per-URL dedup + fetch
async def ensure_master(state: BuildState, wi: WorkItem) -> str | None:
    """Download (or reuse a cached) capped master for this item's source_url. Dedup key is the
    source_url: the FIRST work item to claim a URL names the file; every later item sharing that
    URL (seed + a collection, or two collections) reuses the same filename — never downloaded twice."""
    su = wi.item.get("source_url")
    if not su:
        return None
    if su in state.url_to_master:
        return state.url_to_master[su]
    async with state.lock_for(su):
        if su in state.url_to_master:            # resolved while we waited for the lock
            return state.url_to_master[su]
        filename = master_filename(wi.collection_id, wi.item.get("title", "untitled"), su)
        dest = state.pack_dir / "_Library" / filename
        if dest.exists():                          # resumable: cheap re-runs skip completed work
            state.url_to_master[su] = filename
            state.stats.master_cached += 1
            return filename
        if aic_tiles.is_aic_iiif(su):
            # AIC blocks full/max but serves deep-zoom tiles — stitch the native master (self-throttled).
            raw = await aic_tiles.fetch_native_bytes(state.client, su, quality=DISPLAY_QUALITY)
        else:
            async with state.sem:
                raw = await _fetch_bytes(state.client, _pack_fetch_url(su))
        if raw is None:
            state.stats.master_failed += 1
            return None
        # Frame/mat/wall crop (ADR-043): consume the pre-baked normalized crop_box deterministically.
        # A cropped, hand-vetted famous work is kept even below the floor (frameless > absent), so the
        # crop bypasses the floor check below.
        cropped = False
        if wi.item.get("needs_frame_crop"):
            crop_bytes = _apply_crop_box(raw, wi.item.get("crop_box"))
            if crop_bytes is not None:
                raw = crop_bytes
                cropped = True
                state.stats.master_cropped += 1
        # 4K-friendly floor: a source smaller than this looks soft on a 4K/8K wall (esp. under Ken
        # Burns zoom). We never upscale — below the floor, the work is skipped from the pack.
        floor = COLLECTION_MIN_EDGE.get(wi.collection_id, state.min_edge)
        try:
            with Image.open(BytesIO(raw)) as probe:
                native_max = max(probe.size)
        except Exception:
            native_max = 0
        if native_max < floor and not cropped:
            state.stats.master_too_small += 1
            logger.info(f"    x below {floor}px 4K floor ({native_max}px): {su}")
            return None
        if cropped and native_max < floor:
            logger.info(f"    ~ cropped below {floor}px floor, kept ({native_max}px): {su}")
        capped = _cap_master(raw)
        if capped is None:
            state.stats.master_failed += 1
            return None
        _atomic_write(dest, capped)
        state.url_to_master[su] = filename
        state.stats.master_downloaded += 1
        return filename


async def ensure_thumbnail(state: BuildState, wi: WorkItem, master_name: str) -> str | None:
    su = wi.item.get("source_url")
    if not su:
        return None
    if su in state.url_to_thumb:
        return state.url_to_thumb[su]
    async with state.lock_for(f"thumb:{su}"):
        if su in state.url_to_thumb:
            return state.url_to_thumb[su]
        name = thumb_filename(su)
        dest = state.pack_dir / "_catalog_thumbs" / name
        if dest.exists():
            state.url_to_thumb[su] = name
            state.stats.thumb_cached += 1
            return name

        raw: bytes | None = None
        turl = wi.item.get("thumbnail_url")
        if turl:
            async with state.sem:
                raw = await _fetch_bytes(state.client, turl)

        derived_from_master = False
        if raw is None:
            master_path = state.pack_dir / "_Library" / master_name
            try:
                raw = master_path.read_bytes()
                derived_from_master = True
            except OSError:
                state.stats.thumb_failed += 1
                return None

        thumb_bytes = _derive_thumbnail(raw)
        if thumb_bytes is None:
            state.stats.thumb_failed += 1
            return None
        _atomic_write(dest, thumb_bytes)
        state.url_to_thumb[su] = name
        if derived_from_master:
            state.stats.thumb_derived += 1
        else:
            state.stats.thumb_downloaded += 1
        return name


def _manifest_item(item: dict, filename: str, thumbnail: str | None) -> dict:
    fp = item.get("focal_point")
    focal = fp if (isinstance(fp, (list, tuple)) and len(fp) == 2) else [0.5, 0.5]
    out = {
        "filename": filename,
        "thumbnail": thumbnail or "",
        "source_url": item.get("source_url", ""),
    }
    for k in _PLACARD_FIELDS:
        out[k] = item.get(k, "")
    out["focal_point"] = [float(focal[0]), float(focal[1])]
    out["featured_rank"] = item.get("featured_rank", 50)
    out["credit_line"] = item.get("credit_line") or ""
    return out


# --------------------------------------------------------------------------- Manifest v2 emit (ADR-044)
def _v2_row(mi: dict) -> dict:
    """Map ONE v1 manifest item (from `_manifest_item`) → a flat row `publisher.build_item` consumes.

    The asset is LOCAL: `local_file` names the pack master (resolved under _Library/ at install), no
    remote URL. `credit_line` → attribution, `source` → rights_holder. featured_rank is NOT carried —
    the collection's items are emitted rank-sorted, so install uses array order (per-collection = one
    manifest = one playlist). needs_frame_crop is NOT carried — the crop is already baked into the master.
    """
    focal = mi.get("focal_point") or [0.5, 0.5]
    return {
        "id": _slugify_v2(mi.get("title") or mi.get("filename") or "untitled"),
        "title": mi.get("title") or "Untitled",
        "artist": mi.get("agent_name") or None,
        "artist_role": mi.get("agent_role") or None,
        "date": mi.get("date_display") or None,
        "creation_date": mi.get("creation_date") or None,
        "medium": mi.get("medium") or None,
        "culture": mi.get("cultural_context") or None,
        "placard": mi.get("description_narrative") or None,
        "tags": mi.get("tags") or None,
        "local_file": mi.get("filename"),
        "thumbnail_url": f"pack:_catalog_thumbs/{mi['thumbnail']}" if mi.get("thumbnail") else None,
        "license": mi.get("license") or None,
        "attribution": mi.get("credit_line") or None,
        "rights_holder": mi.get("source") or None,
        "focal_x": focal[0], "focal_y": focal[1],
    }


def _slugify_v2(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "untitled"


def _load_signing_key(cli_path: str | None) -> str | None:
    """base64 Ed25519 private signing key from --signing-key <file> or $SD_PACK_SIGNING_KEY. Absent →
    unsigned manifests (valid, but only the 'community' tier until a key + registry entry exist)."""
    import os
    if cli_path:
        return Path(cli_path).read_text().strip()
    return (os.environ.get(SIGNING_KEY_ENV) or "").strip() or None


def _emit_v2_manifests(out: Path, manifest_collections: list[dict], *, signing_key: str | None,
                        generated_at: str | None) -> dict:
    """Write one signed Manifest v2 per collection to _manifests/<id>.json + a pack-index.json listing
    them (with the default rotation). Returns the pack index. Deterministic — NO AI, NO network."""
    (out / "_manifests").mkdir(parents=True, exist_ok=True)
    public_key = publisher.public_from_private(signing_key) if signing_key else None
    index_cols, signed_n, unsigned_n = [], 0, 0
    have_masterpieces = any(c["id"] == "masterpieces" for c in manifest_collections)
    for col in manifest_collections:
        cid = col["id"]
        # rank-sorted so install (array order) == fame order; stable sort keeps ties' original order.
        items = sorted(col["items"], key=lambda it: it.get("featured_rank", 50), reverse=True)
        rows = [_v2_row(mi) for mi in items]
        meta = {"id": cid, "title": col.get("title") or cid, "description": col.get("description") or "",
                "publisher": PACK_PUBLISHER}
        manifest, errors = publisher.assemble_and_validate(meta, rows, generated_at=generated_at)
        if errors:
            logger.warning(f"    x v2 manifest for {cid!r} invalid, writing unsigned draft: {errors[:3]}")
        elif signing_key:
            manifest = publisher.sign_manifest(manifest, signing_key, public_key)
        (out / "_manifests" / f"{cid}.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
        if manifest.get("signature"):
            signed_n += 1
        else:
            unsigned_n += 1
        index_cols.append({"id": cid, "title": meta["title"], "manifest": f"_manifests/{cid}.json",
                           "item_count": len(rows),
                           "default": (cid == "masterpieces") if have_masterpieces else (not index_cols)})
    pack_index = {"pack_version": "2", "publisher": PACK_PUBLISHER,
                  "public_key": public_key, "collections": index_cols}
    if generated_at:
        pack_index["generated_at"] = generated_at
    (out / "pack-index.json").write_text(json.dumps(pack_index, indent=1, ensure_ascii=False))
    logger.info(f"  v2: wrote {len(index_cols)} manifest(s) ({signed_n} signed, {unsigned_n} unsigned) + pack-index.json")
    return pack_index


async def process_item(state: BuildState, wi: WorkItem) -> dict | None:
    """Never raises — a bad item is logged and skipped so one dead source can't abort the run."""
    try:
        if not wi.item.get("source_url"):
            return None
        filename = await ensure_master(state, wi)
        if filename is None:
            return None
        thumbnail = await ensure_thumbnail(state, wi, filename)
        return _manifest_item(wi.item, filename, thumbnail)
    except Exception as e:
        logger.warning(f"    x unexpected error on '{wi.item.get('title', '?')}': {e}")
        state.stats.master_failed += 1
        return None


# --------------------------------------------------------------------------- reporting helpers
def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


# --------------------------------------------------------------------------- build
async def build(out: Path, *, scope: set[str], limit: int | None, collections_filter: set[str] | None,
                 concurrency: int, created: str | None, min_edge: int, signing_key: str | None = None) -> int:
    (out / "_Library").mkdir(parents=True, exist_ok=True)
    (out / "_catalog_thumbs").mkdir(parents=True, exist_ok=True)
    (out / "_catalog").mkdir(parents=True, exist_ok=True)

    collections = load_catalog_collections(collections_filter) if "catalog" in scope else []
    seed_items = load_seed_items() if "seed" in scope else []

    # Catalog items are queued first (they drive the manifest); seed items follow purely to warm
    # the dedup cache for any shared source_url and to bake seed art into the pack for first boot.
    # --limit caps the combined queue, so a quick test run stays small end-to-end.
    queue: list[WorkItem] = []
    for col in collections:
        for it in col.get("items", []):
            queue.append(WorkItem("catalog", col["id"], it))
    for it in seed_items:
        queue.append(WorkItem("seed", "seed", it))
    if limit:
        queue = queue[:limit]

    logger.info(f"queued {len(queue)} item(s) "
                f"({sum(1 for w in queue if w.kind == 'catalog')} catalog, "
                f"{sum(1 for w in queue if w.kind == 'seed')} seed)")

    async with httpx.AsyncClient(headers={"User-Agent": SD_USER_AGENT}) as client:
        state = BuildState(pack_dir=out, client=client, sem=asyncio.Semaphore(concurrency), min_edge=min_edge)
        results = await asyncio.gather(*(process_item(state, wi) for wi in queue))

    # Assemble the manifest: one entry per catalog file in scope, items in file-array order,
    # successfully-downloaded works only.
    by_collection: dict[str, list[dict]] = {}
    for wi, out_item in zip(queue, results):
        if wi.kind != "catalog" or out_item is None:
            continue
        by_collection.setdefault(wi.collection_id, []).append(out_item)

    manifest_collections = []
    for col in collections:
        cid = col["id"]
        items_out = by_collection.get(cid, [])
        manifest_collections.append({
            "id": cid,
            "title": col.get("title", ""),
            "description": col.get("description", ""),
            "items": items_out,
        })
        # Verbatim copy of the source catalog file, for any consumer that wants the original too.
        src = CATALOG_DIR / f"{cid}.json"
        if src.exists():
            shutil.copy2(src, out / "_catalog" / f"{cid}.json")

    manifest = {
        "version": "v1",
        "display_max_edge": DISPLAY_MAX_EDGE,
        "collections": manifest_collections,
    }
    if created:
        manifest["created"] = created
    (out / "pack-manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))

    # ADR-044: also emit signed Manifest v2 feeds (one per collection) + pack-index.json — the unified
    # ingestion path (first boot installs each as a verified local subscription). v1 kept during transition.
    _emit_v2_manifests(out, manifest_collections, signing_key=signing_key, generated_at=created)

    total_manifest_items = sum(len(c["items"]) for c in manifest_collections)
    st = state.stats
    print("\n=== BUILD PACK SUMMARY ===")
    print(f"masters:     {st.master_downloaded} downloaded, {st.master_cached} cached (skipped), "
          f"{st.master_too_small} below-{min_edge}px, {st.master_failed} failed")
    print(f"crops:       {st.master_cropped} frame/mat/wall crop(s) applied (needs_frame_crop + crop_box)")
    print(f"thumbnails:  {st.thumb_downloaded} downloaded, {st.thumb_cached} cached, "
          f"{st.thumb_derived} derived, {st.thumb_failed} failed")
    print(f"manifest:    {total_manifest_items} item(s) across {len(manifest_collections)} collection(s)")
    print(f"pack size:   {_human_size(_dir_size(out))}  ({out})")

    return 1 if (st.master_downloaded == 0 and st.master_cached == 0 and len(queue) > 0) else 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path, help="pack output directory")
    ap.add_argument("--scope", default="catalog",
                    help="comma list: catalog,seed. Default 'catalog' — seed works aren't represented in "
                         "the manifest (the masterpieces live in catalog collections + Greatest Hits), so "
                         "adding 'seed' only warms the dedup cache and would bake orphaned files.")
    ap.add_argument("--limit", type=int, default=None, help="cap total items queued (testing)")
    ap.add_argument("--collections", default=None, help="comma list of catalog ids to include")
    ap.add_argument("--concurrency", type=int, default=4, help="bounded concurrent downloads")
    ap.add_argument("--min-edge", type=int, default=3840,
                    help="native long-edge floor. Default 3840 (true 4K): fills a 4K panel 1:1 crisp. "
                         "Ken Burns zoom is capped per-work by native res (adaptive) rather than requiring "
                         "every work to exceed 4K, which starved the catalog (AIC etc. cap exports at 3000px).")
    ap.add_argument("--created", default=None,
                     help="fixed value written as manifest['created'] (omit for a stable, "
                          "timestamp-free manifest across reruns)")
    ap.add_argument("--signing-key", default=None,
                     help=f"path to a base64 Ed25519 private key file to sign the v2 manifests (ADR-044). "
                          f"Omit to read ${SIGNING_KEY_ENV}, or leave both unset for unsigned "
                          f"(community-tier) manifests. The verified Core pack needs the first-party key.")
    args = ap.parse_args()

    scope = {s.strip() for s in args.scope.split(",") if s.strip()}
    collections_filter = ({c.strip() for c in args.collections.split(",") if c.strip()}
                           if args.collections else None)
    signing_key = _load_signing_key(args.signing_key)

    return await build(args.out, scope=scope, limit=args.limit, collections_filter=collections_filter,
                        concurrency=args.concurrency, created=args.created, min_edge=args.min_edge,
                        signing_key=signing_key)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
