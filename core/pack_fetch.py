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
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import httpx

import federation
from config import SD_USER_AGENT
from core import lifespan

# A single collection artifact is bounded (the whole 28-collection pack is ~15 GB); cap a download well
# above the largest single collection so a hostile/oversized artifact can't fill the disk.
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB
_CHUNK = 1024 * 1024


async def _guard(url: str) -> None:
    """Block private/loopback/link-local targets before fetching (reuses federation's SSRF guard)."""
    await asyncio.to_thread(federation._assert_public_url, url)


async def fetch_registry(client: httpx.AsyncClient, registry_url: str) -> dict:
    """GET the packs.json registry (the list of downloadable collections + their categories/sizes/sha256)."""
    await _guard(registry_url)
    r = await client.get(registry_url, timeout=30)
    r.raise_for_status()
    return r.json()


async def _download_verified(client: httpx.AsyncClient, url: str, dest: Path, sha256: str | None) -> None:
    """Stream `url` → `dest`, enforcing the size cap and (if given) the expected sha256. Raises on mismatch
    so a corrupt/tampered artifact is never installed."""
    await _guard(url)
    h = hashlib.sha256()
    total = 0
    async with client.stream("GET", url, timeout=httpx.Timeout(30.0, read=90.0)) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            async for chunk in r.aiter_bytes(_CHUNK):
                total += len(chunk)
                if total > MAX_ARTIFACT_BYTES:
                    raise ValueError(f"artifact exceeds {MAX_ARTIFACT_BYTES} byte cap")
                h.update(chunk)
                f.write(chunk)
    if sha256 and h.hexdigest() != sha256:
        raise ValueError(f"sha256 mismatch: expected {sha256[:12]}…, got {h.hexdigest()[:12]}…")


def _extract_collection(tar_path: Path, cid: str, artwork_root: Path) -> bool:
    """Extract a collection artifact and MERGE its masters + manifest into ARTWORK_ROOT (what append-install
    reads). Uses tarfile's `data` filter (blocks path traversal / absolute paths). Returns True if the
    collection's manifest landed."""
    # Stage INSIDE artwork_root so the final renames stay on one filesystem — ARTWORK_ROOT is a bind
    # mount, so a temp dir on the container's overlay fs would make Path.replace() a cross-device error
    # (Errno 18). `_`-prefixed so the boot filesystem-sync never mistakes it for a collection dir.
    with tempfile.TemporaryDirectory(dir=artwork_root, prefix="_dl") as tmp:
        tmpd = Path(tmp)
        with tarfile.open(tar_path) as tf:
            tf.extractall(tmpd, filter="data")
        inner = tmpd / cid
        if not inner.is_dir():
            # tolerate an unexpected top-level dir name — take the sole child
            children = [c for c in tmpd.iterdir() if c.is_dir()]
            inner = children[0] if len(children) == 1 else inner
        man = inner / "_manifests" / f"{cid}.json"
        if not man.exists():
            return False
        (artwork_root / "_Library").mkdir(parents=True, exist_ok=True)
        (artwork_root / "_manifests").mkdir(parents=True, exist_ok=True)
        (artwork_root / "_catalog_thumbs").mkdir(parents=True, exist_ok=True)
        for sub, dst in (("_Library", "_Library"), ("_catalog_thumbs", "_catalog_thumbs")):
            srcdir = inner / sub
            if srcdir.is_dir():
                for f in srcdir.iterdir():
                    f.replace(artwork_root / dst / f.name)
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
    return httpx.AsyncClient(headers={"User-Agent": SD_USER_AGENT}, follow_redirects=True)
