"""Derivative-image rendering primitives, shared across every domain that serves or
creates an artwork image: the display feed (`display.jpg`), the admin/library grid
(thumbnail/preview), studio uploads, catalog adds, and the boot warm-sweep.
"""

import asyncio
import io
import logging
import os
from functools import lru_cache
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from PIL import Image, ImageOps

from config import ARTWORK_ROOT, LIBRARY_DIR

logger = logging.getLogger("artwork-display-api")


@lru_cache(maxsize=256)
def _optimized_image_cached(image_path: Path, size: tuple, quality: int, mtime: int) -> bytes:
    """Resize + JPEG-compress for web delivery. `mtime` participates only in the cache key (A4): a file
    replaced in place gets a fresh entry instead of serving stale bytes until process restart."""
    logger.info(f"[Image Processor] Optimizing: {image_path.name}")
    with Image.open(image_path) as img:
        if img.mode not in ("RGB", "L"):      # covers RGBA/P/LA/CMYK — "LA" used to crash the JPEG save
            img = img.convert("RGB")
        img.thumbnail(size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()


def get_optimized_image(image_path: Path, size: tuple, quality: int = 85) -> bytes:
    """mtime-keyed wrapper over the lru cache — see A4."""
    try:
        mtime = int(image_path.stat().st_mtime)
    except OSError:
        mtime = 0
    return _optimized_image_cached(image_path, size, quality, mtime)

# --- Canvas display image (resolution-capped) -------------------------------
# The Canvas <img> previously loaded the full-res original via /media. Museum
# originals can be 40–110 MB / 150+ MP — too big for a Pi-class browser to decode
# and GPU-texture (GL_MAX_TEXTURE_SIZE is commonly 8192), so the placard cycles
# while the image never paints. We serve a capped derivative instead; the full-res
# original stays on disk untouched (focal/crop quality unaffected). 7680 px long
# edge keeps ~4K detail even after a portrait→landscape cover-crop + Ken Burns
# zoom, while staying under the 8192 texture ceiling.
DISPLAY_MAX_EDGE = 7680
DISPLAY_QUALITY = 90
DERIVATIVES_DIR = ARTWORK_ROOT / "_derivatives"


def render_canvas_image(src: Path, art_id: int) -> bytes:
    """Resolution-capped, EXIF-baked JPEG for the Canvas; disk-cached per source mtime.

    Heavy (decode + LANCZOS downscale + encode of a 150 MP original) — call via
    run_in_threadpool so it never blocks the event loop. The derivative is written
    once and then served from disk on every later request; the cap is only applied
    when the source actually exceeds it (smaller originals are re-encoded as-is)."""
    DERIVATIVES_DIR.mkdir(exist_ok=True)
    mtime = int(src.stat().st_mtime)
    dst = DERIVATIVES_DIR / f"{art_id}-{mtime}-{DISPLAY_MAX_EDGE}.jpg"
    if dst.exists():
        return dst.read_bytes()
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)   # bake orientation — a re-encode drops the EXIF tag
        if img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > DISPLAY_MAX_EDGE:
            img.thumbnail((DISPLAY_MAX_EDGE, DISPLAY_MAX_EDGE), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=DISPLAY_QUALITY, progressive=True)
        data = buf.getvalue()
    # Prune stale derivatives for this artwork (an earlier crop/replace → new mtime).
    for old in DERIVATIVES_DIR.glob(f"{art_id}-*.jpg"):
        if old != dst:
            try: old.unlink()
            except OSError: pass
    # Atomic publish so a concurrent reader never sees a partial file. Per-writer tmp name (A3): the boot
    # warm sweep and a lazy /display.jpg render can target the same dst — a shared .tmp would let their
    # writes interleave before os.replace. os.replace is atomic, so last-writer-wins on identical bytes.
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)
    return data


def warm_canvas_cache_async(art_id: int, filename: str) -> None:
    """Fire-and-forget: pre-render the capped display derivative in the background so the Canvas never
    pays the one-time encode (up to several seconds for a 150 MP original) on first display. Best-effort;
    a missing loop or a bad file is swallowed (the lazy path + the boot sweep are the backstops)."""
    async def _run():
        try:
            await run_in_threadpool(render_canvas_image, LIBRARY_DIR / filename, art_id)
        except Exception as e:
            logger.warning(f"[Warm] could not pre-render display image for art {art_id}: {e}")
    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        pass  # no running loop (sync context) — the boot sweep will catch it
