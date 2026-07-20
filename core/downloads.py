"""SSRF-safe robust image downloader shared by the boot seed, discovery-approve, and catalog-add
paths — the one downloader those routes share, plus its focal-point parsing companion.
"""

import asyncio
from pathlib import Path

import httpx
from fastapi import HTTPException
from PIL import Image

import federation
from config import LIBRARY_DIR, SD_USER_AGENT
from epaper import ASPECT_CROP_KEYS, normalize_crop_box


async def _download_image_to_library(source_url: str, *, filename: str,
                                     retries: int = 3) -> tuple[Path, str, int, int]:
    """Robustly download a remote image into LIBRARY_DIR — the one downloader the seed, discovery,
    and catalog paths share. Sends a descriptive User-Agent (the default httpx UA is rejected by
    Wikimedia and others), follows redirects through SSRF-validated hops only (M1), retries 429s with
    escalating backoff, writes to a collision-safe unique filename, and validates the bytes are a real
    image (a bad download is
    deleted, never left in the library). Returns (dest_path, safe_filename, width, height); raises
    HTTPException on download or validation failure."""
    safe_name = "".join(x for x in filename if x.isalnum() or x in "_-.")
    if not safe_name.lower().endswith(".jpg"):
        safe_name += ".jpg"
    stem = safe_name[:-4]
    dest_path = LIBRARY_DIR / safe_name
    n = 1
    while dest_path.exists():
        safe_name = f"{stem}_{n}.jpg"; dest_path = LIBRARY_DIR / safe_name; n += 1

    resp = None
    # M1: follow redirects MANUALLY and SSRF-validate every hop. httpx's own follow_redirects=True
    # would bounce a 3xx to an internal host (127.0.0.1, router admin, cloud metadata) that the
    # caller's initial pre-check never saw. We can't simply refuse redirects — Wikimedia's
    # Special:FilePath (most of the catalog + seed) legitimately 302s to the real image — so we
    # follow, but only to validated public hosts.
    async with httpx.AsyncClient(headers={"User-Agent": SD_USER_AGENT}) as client:
        for attempt in range(retries):
            url = source_url
            for _hop in range(6):
                resp = await client.get(url, timeout=45.0, follow_redirects=False)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                loc = resp.headers.get("location")
                if not loc:
                    break
                url = str(resp.url.join(loc))   # resolve relative redirects against the current URL
                try:
                    await asyncio.to_thread(federation._assert_public_url, url)   # C2: DNS off the loop
                except federation.FederationError as e:
                    raise HTTPException(502, detail=f"Image redirected to a blocked host ({e}).")
            if resp.status_code == 429:
                await asyncio.sleep(3 * (attempt + 1))
                continue
            break
    if not resp or resp.status_code != 200:
        raise HTTPException(502, detail=f"Could not download image (HTTP {resp.status_code if resp else 'none'}).")

    with open(dest_path, "wb") as f:
        f.write(resp.content)
    try:
        with Image.open(dest_path) as img:
            w, h = img.size
    except Exception:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(502, detail="Downloaded file was not a valid image.")
    return dest_path, safe_name, w, h


def _focal_xy(item: dict, default: tuple = (0.5, 0.5)) -> tuple:
    """Parse a flat 'focal_point': [x, y] (normalized 0..1) from a catalog/seed item — the Ken Burns
    / crop framing anchor baked by tools/backfill_focal_*. Absent or malformed ⇒ centered default."""
    fp = item.get("focal_point")
    if isinstance(fp, (list, tuple)) and len(fp) == 2:
        try:
            return min(1.0, max(0.0, float(fp[0]))), min(1.0, max(0.0, float(fp[1])))
        except (TypeError, ValueError):
            pass
    return default


def _aspect_crops(item: dict) -> dict | None:
    """Parse 'aspect_crops': {"16:9":[x0,y0,x1,y1], ...} (normalized 0..1) from a catalog/manifest
    item — the per-shape "how do I best fill THIS screen" boxes (epaper.ASPECT_CROP_KEYS), a twin to
    _focal_xy for a different question. Absent/not-a-dict/no valid boxes ⇒ None (renderer falls back
    to the focal cover). Unlike the offline derivation tool, this is the read path: one malformed box
    must not void the others, so each key is validated independently and only good ones kept.

    epaper.normalize_crop_box() deliberately returns None for BOTH a malformed box and a valid
    near-full-frame one (its "already fills the frame, no-op" convention) — those two cases must be
    told apart here, since a full-frame crop is a meaningful "no crop, use it all" answer for this
    key, not something to silently drop."""
    raw = item.get("aspect_crops")
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for key, box in raw.items():
        if key not in ASPECT_CROP_KEYS:
            continue
        normalized = normalize_crop_box(box)
        if normalized is not None:
            out[key] = list(normalized)
            continue
        if isinstance(box, (list, tuple)) and len(box) == 4:
            try:
                x0, y0, x1, y1 = (float(v) for v in box)
            except (TypeError, ValueError):
                continue
            if 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0:
                out[key] = [0.0, 0.0, 1.0, 1.0]   # near-full-frame: "use the whole thing", not invalid
    return out or None
