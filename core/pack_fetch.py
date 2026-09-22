"""Runtime fetch + install of modular packs (ADR-040 #4 "browse & download packs"; ADR-038 R2 host).

The device reads a pack REGISTRY (`packs.json` — the output of `tools/publish_pack`, hosted on Cloudflare
R2 behind curwe.ai) and lets the user pull additional collections BY CATEGORY on demand. This is the
consumer side of the modular-pack story:

  fetch registry → download a collection's artifact → verify **sha256** (integrity) → extract into
  ARTWORK_ROOT → **append-install** it (`install_downloaded_collection`, no re-seed of the baked Core).

The manifest inside each artifact is Ed25519-signed, so trust (`verified`/`community`) is assessed at
install — sha256 guards the bytes in transit, the signature guards the manifest's authenticity. The R2
URL is public (no secret on the device, ADR-038 §5); URLs pass the federation SSRF guard before any fetch.
"""
import asyncio
import hashlib
import json
import logging
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import httpx

from config import SD_USER_AGENT
from core import lifespan
from core.downloads import guarded_stream

logger = logging.getLogger(__name__)

# A single collection artifact is bounded (the whole 28-collection pack is ~15 GB); cap a download well
# above the largest single collection so a hostile/oversized artifact can't fill the disk.
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB
_CHUNK = 1024 * 1024

# Extraction bomb guards, checked BEFORE extractall. tools/publish_pack.py emits plain (uncompressed)
# .tar artifacts, so opening with "r:" (uncompressed-only) rejects a gzip/bz2/xz-compressed artifact
# outright — a compression bomb can't expand past the on-disk download cap that way. Cap the sum of
# member sizes at the same bound as the download itself (an uncompressed tar's members can't exceed
# the bytes already on disk, but this also catches a crafted tar whose header sizes lie) and cap the
# member count so a huge number of tiny entries can't exhaust inodes/memory during extraction.
MAX_EXTRACTED_BYTES = MAX_ARTIFACT_BYTES
MAX_MEMBERS = 100_000

# N3: only these are ever merged from a pack's _Library/_catalog_thumbs into ARTWORK_ROOT, which is
# served as static /media (StaticFiles sets Content-Type from the extension — an unallowlisted file,
# e.g. .html, would be served with that type and execute as first-party stored XSS). Checked against
# tools/publish_pack.py's actual output (always .jpg today) and the real art-pack-dist/*.tar artifacts;
# .jpeg/.png/.webp are included as the formats the catalog itself already treats as images elsewhere.
_MERGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# The R2 pack host sits behind a Cloudflare rate-limit (ADR-038: a burst of `.tar` requests per IP →
# HTTP 429 + a ~10s block). A device mid-install must back off and retry, not fail the whole collection
# on a transient throttle — so 429/503 are retried with escalating backoff (matches core.downloads).
_RETRY_STATUS = {429, 503}
_MAX_ATTEMPTS = 4
_MAX_BACKOFF = 30.0  # cap so a hostile Retry-After can't stall an install indefinitely


def _backoff(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before the next attempt: honor a `Retry-After` header (Cloudflare sends one with
    the rate-limit block) when present and sane, else escalating backoff. Capped at _MAX_BACKOFF."""
    hdr = resp.headers.get("Retry-After")
    if hdr:
        try:
            return min(_MAX_BACKOFF, max(0.0, float(hdr)))
        except ValueError:
            pass  # HTTP-date form (rare from CF) — fall through to backoff
    return min(_MAX_BACKOFF, 3.0 * attempt)


async def fetch_registry(client: httpx.AsyncClient, registry_url: str) -> dict:
    """GET the packs.json registry (the list of downloadable collections + their categories/sizes/sha256).
    Retries a rate-limited (429/503) host with backoff before giving up. N5: redirects are followed
    manually (guarded_stream), SSRF-validating every hop — httpx's own follow_redirects=True would trust
    a redirect blindly, including one a hostile registry hands back."""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        async with guarded_stream(client, "GET", registry_url, timeout=30) as r:
            if r.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_backoff(r, attempt))
                continue
            r.raise_for_status()
            await r.aread()
            return r.json()


async def _download_verified(client: httpx.AsyncClient, url: str, dest: Path, sha256: str | None) -> None:
    """Stream `url` → `dest`, enforcing the size cap and the expected sha256. Raises on mismatch so a
    corrupt/tampered artifact is never installed. A rate-limited (429/503) host is retried with backoff
    — the throttle is detected on the response status before any bytes hit disk, so a retry restarts the
    download cleanly (fresh hash, truncated file). N5: redirects are followed manually (guarded_stream),
    SSRF-validating every hop.

    N4: a registry entry with no `sha256` is an ERROR, not "skip verification" — sha256 is the one
    control doing all the integrity work today (tools/publish_pack.py always emits it; a missing field
    only happens on a malformed or hostile registry)."""
    if not sha256:
        raise ValueError("registry entry has no sha256 — refusing to install an unverifiable artifact")
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        h = hashlib.sha256()
        total = 0
        async with guarded_stream(client, "GET", url, timeout=httpx.Timeout(30.0, read=90.0)) as r:
            if r.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_backoff(r, attempt))
                continue
            r.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(_CHUNK):
                    total += len(chunk)
                    if total > MAX_ARTIFACT_BYTES:
                        raise ValueError(f"artifact exceeds {MAX_ARTIFACT_BYTES} byte cap")
                    h.update(chunk)
                    f.write(chunk)
        if h.hexdigest() != sha256:
            raise ValueError(f"sha256 mismatch: expected {sha256[:12]}…, got {h.hexdigest()[:12]}…")
        return


def _extract_collection(tar_path: Path, cid: str, artwork_root: Path) -> bool:
    """Extract a collection artifact and MERGE its masters + manifest into ARTWORK_ROOT (what append-install
    reads). Uses tarfile's `data` filter (blocks path traversal / absolute paths). Returns True if the
    collection's manifest landed."""
    # Stage INSIDE artwork_root so the final renames stay on one filesystem — ARTWORK_ROOT is a bind
    # mount, so a temp dir on the container's overlay fs would make Path.replace() a cross-device error
    # (Errno 18). `_`-prefixed so the boot filesystem-sync never mistakes it for a collection dir.
    with tempfile.TemporaryDirectory(dir=artwork_root, prefix="_dl") as tmp:
        tmpd = Path(tmp)
        # "r:" refuses a compressed (gzip/bz2/xz) artifact outright — publish_pack emits plain .tar, and
        # disallowing compression means a member's claimed size can't diverge from bytes actually read
        # off disk, closing the compression-bomb class before extractall ever runs.
        with tarfile.open(tar_path, "r:") as tf:
            members = tf.getmembers()
            if len(members) > MAX_MEMBERS:
                raise ValueError(f"artifact has {len(members)} members, over the {MAX_MEMBERS} cap")
            total = sum(m.size for m in members)
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError(f"artifact extracts to {total} bytes, over the {MAX_EXTRACTED_BYTES} cap")
            tf.extractall(tmpd, members=members, filter="data")
        inner = tmpd / cid
        if not inner.is_dir():
            # tolerate an unexpected top-level dir name — take the sole child
            children = [c for c in tmpd.iterdir() if c.is_dir()]
            inner = children[0] if len(children) == 1 else inner
        man = inner / "_manifests" / f"{cid}.json"
        if not man.exists():
            return False
        # N1: gate BEFORE merging anything into artwork_root. Extraction above lands only in the
        # tempdir (deleted on context exit); nothing has touched artwork_root yet, so a refused pack
        # leaves shared masters / the baked Core manifest untouched.
        manifest = json.loads(man.read_text())
        if manifest.get("id") not in (None, cid):
            raise ValueError(f"pack refused: manifest id {manifest.get('id')!r} does not match {cid!r}")
        reason = lifespan.manifest_refusal_reason(manifest, cid, require_verified=True)
        if reason is not None:
            logger.error(f"[PackFetch] {reason}")
            raise ValueError(f"pack refused: {reason}")
        (artwork_root / "_Library").mkdir(parents=True, exist_ok=True)
        (artwork_root / "_manifests").mkdir(parents=True, exist_ok=True)
        (artwork_root / "_catalog_thumbs").mkdir(parents=True, exist_ok=True)
        for sub, dst in (("_Library", "_Library"), ("_catalog_thumbs", "_catalog_thumbs")):
            srcdir = inner / sub
            if srcdir.is_dir():
                for f in srcdir.iterdir():
                    if f.suffix.lower() not in _MERGE_EXTS:
                        continue
                    target = artwork_root / dst / f.name
                    if target.exists():
                        # Never clobber an existing master/thumb (a different collection's file, or a
                        # user's own upload sharing the name) — the installer dedups by filename
                        # downstream (core/lifespan.py:_install_collection), so skipping is correct.
                        continue
                    f.replace(target)
        man.replace(artwork_root / "_manifests" / f"{cid}.json")
    return True


async def install_collection_from_registry(db, client: httpx.AsyncClient, registry_url: str,
                                           collection_id: str) -> dict:
    """The full on-demand flow for one collection: registry → download+verify → extract → append-install.
    Returns {ok, collection, trust, installed, error?}. Idempotent (re-running is a no-op via the installer).
    `<artifact>` URLs resolve relative to the registry URL, so the whole pack site can move hosts freely."""
    result = {"ok": False, "collection": collection_id, "trust": None, "installed": False}
    try:
        registry = await fetch_registry(client, registry_url)
        entry = next((c for c in registry.get("collections", []) if c.get("id") == collection_id), None)
        if entry is None:
            result["error"] = f"{collection_id!r} not in registry"
            return result

        art_url = urljoin(registry_url, entry["download"])
        artwork_root = lifespan.ARTWORK_ROOT
        with tempfile.TemporaryDirectory(dir=artwork_root.parent) as tmp:
            tar_path = Path(tmp) / f"{collection_id}.tar"
            await _download_verified(client, art_url, tar_path, entry.get("sha256"))
            if not _extract_collection(tar_path, collection_id, artwork_root):
                result["error"] = "artifact missing the collection manifest"
                return result

        installed = await asyncio.to_thread(lifespan.install_downloaded_collection, db, collection_id)
        result["installed"] = installed
        if installed:
            sub = _installed_sub(db, collection_id)
            result["trust"] = getattr(sub, "trust", None)
            result["ok"] = True
        else:
            result["error"] = "manifest invalid at install"
    except Exception as e:  # noqa: BLE001 — surface any failure to the caller/UI, never crash the app
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def _installed_sub(db, cid: str):
    from models import SubscriptionModel
    return db.query(SubscriptionModel).filter(SubscriptionModel.url == f"pack:{cid}").first()


def new_client() -> httpx.AsyncClient:
    # N5: redirects are followed manually and SSRF-validated per hop (guarded_stream) — the client
    # itself must never auto-follow, or a bypass is one httpx.AsyncClient.get() call away.
    return httpx.AsyncClient(headers={"User-Agent": SD_USER_AGENT}, follow_redirects=False)
