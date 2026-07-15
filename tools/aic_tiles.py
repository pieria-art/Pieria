"""Art Institute of Chicago native-image fetch via IIIF deep-zoom tile stitching.

WHY: AIC's IIIF image host (www.artic.edu/iiif/2/) sits behind a Cloudflare managed challenge that
403s `full/max` (the full-native request) for scripts, and even the sizes it does serve are capped to
a small derivative (~3000px). BUT the deep-zoom viewer's per-tile **region** requests
(`<x,y,w,h>/full/0/default.jpg`) are small and pass Cloudflare unblocked at native resolution. So we
reassemble the true native master tile-by-tile — the one scriptable path to AIC's full-res CC0 art.
See [[aic-iiif-3000px-cap]]. Used at pack-assembly time (build_pack), never at runtime.
"""
from __future__ import annotations

import asyncio
from io import BytesIO

import httpx
from PIL import Image

IIIF_BASE = "https://www.artic.edu/iiif/2"
_AIC_IIIF_MARK = "/iiif/2/"
_AIC_HOSTS = ("www.artic.edu", "artic.edu")
TILE = 1024


def is_aic_iiif(url: str) -> bool:
    """True for an AIC IIIF image URL (any size/region variant)."""
    u = (url or "").lower()
    return _AIC_IIIF_MARK in u and any(h in u for h in _AIC_HOSTS)


def image_id_of(url: str) -> str | None:
    """Extract the IIIF image id from any AIC IIIF URL (…/iiif/2/<image_id>/<region>/<size>/…)."""
    if _AIC_IIIF_MARK not in (url or ""):
        return None
    tail = url.split(_AIC_IIIF_MARK, 1)[1]
    iid = tail.split("/", 1)[0].strip()
    return iid or None


async def _native_dims(client: httpx.AsyncClient, image_id: str) -> tuple[int, int] | None:
    """Native pixel dimensions from the (ungated) IIIF info.json."""
    try:
        r = await client.get(f"{IIIF_BASE}/{image_id}/info.json", timeout=30)
        if r.status_code != 200:
            return None
        j = r.json()
        w, h = int(j.get("width") or 0), int(j.get("height") or 0)
        return (w, h) if w and h else None
    except (httpx.HTTPError, ValueError):
        return None


async def _tile(client, sem, image_id, x, y, w, h, retries=4):
    url = f"{IIIF_BASE}/{image_id}/{x},{y},{w},{h}/full/0/default.jpg"
    async with sem:
        for attempt in range(retries):
            try:
                r = await client.get(url, timeout=30)
            except httpx.HTTPError:
                await asyncio.sleep(1.5 * (attempt + 1)); continue
            if r.status_code == 429:
                await asyncio.sleep(2.0 * (attempt + 1)); continue
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                return x, y, r.content
            return x, y, None   # a hard non-200 (e.g. 403) — don't spin
    return x, y, None


async def stitch_native(client: httpx.AsyncClient, image_id: str, w: int, h: int,
                        *, tile: int = TILE, concurrency: int = 6) -> Image.Image | None:
    """Assemble the full native master from `tile`-sized region requests. Returns a PIL image, or None
    if ANY tile fails (we never ship a holey master)."""
    canvas = Image.new("RGB", (w, h))
    sem = asyncio.Semaphore(concurrency)
    coords = [(x, y, min(tile, w - x), min(tile, h - y))
              for y in range(0, h, tile) for x in range(0, w, tile)]
    results = await asyncio.gather(*[_tile(client, sem, image_id, x, y, tw, th) for x, y, tw, th in coords])
    for x, y, content in results:
        if content is None:
            return None
        try:
            canvas.paste(Image.open(BytesIO(content)), (x, y))
        except OSError:
            return None
    return canvas


async def fetch_native_bytes(client: httpx.AsyncClient, url: str, *, quality: int = 95,
                             concurrency: int = 6) -> bytes | None:
    """Given any AIC IIIF URL, stitch the native master and return progressive-JPEG bytes (uncapped —
    the caller applies its own display cap). None if the id can't be resolved or a tile fails."""
    image_id = image_id_of(url)
    if not image_id:
        return None
    dims = await _native_dims(client, image_id)
    if not dims:
        return None
    img = await stitch_native(client, image_id, dims[0], dims[1], concurrency=concurrency)
    if img is None:
        return None
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, progressive=True)
    return buf.getvalue()
