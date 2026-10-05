"""Federation — safely fetch, validate, and cache subscribed Manifest v2 collections.

Security posture (we index pointers, never host bytes; the user chose the URL):
- http(s) only; an **SSRF guard** allowlists only globally-routable host IPs (`ipaddress.is_global`,
  which also excludes CGNAT 100.64.0.0/10) plus explicit multicast/reserved refusals (no reaching
  internal services like cloud metadata, localhost, or link-local).
- **redirects disabled** (a 3xx could bounce past the SSRF check to an internal host).
- **size cap + content-type/JSON check + timeout** (no zip-bombs / HTML / hangs).
- **strict Manifest v2 validation** before anything is cached or shown.
- a **host/publisher blocklist** the fetch honors (revocation).

Untrusted manifest strings are escaped at *render* time (the /art page + browse UI), not here.
"""

import asyncio
import base64
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from core import safe_http
from manifest_validator import validate_manifest
from models import SubscriptionModel

logger = logging.getLogger("artwork-display-api.federation")

MAX_MANIFEST_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_ITEMS = 5000
FETCH_TIMEOUT = 20.0

# Revocation/blocklist the fetcher honors (v1 = static; later a synced list).
BLOCKED_HOSTS: set[str] = set()
BLOCKED_PUBLISHERS: set[str] = set()


def _load_trusted_keys() -> dict:
    """Curated registry: publisher.id -> base64 Ed25519 public key. A signed manifest is promoted to
    'verified' only when its publisher key matches an entry here (else a valid self-signed feed stays
    'community' / trust-on-first-use). v1 = a hand-curated static file."""
    path = Path(__file__).parent / "registry" / "trusted_publishers.json"
    try:
        return (json.loads(path.read_text()) or {}).get("publishers", {})
    except (OSError, ValueError):
        return {}


TRUSTED_KEYS = _load_trusted_keys()


class FederationError(Exception):
    """Any reason a manifest URL was rejected (network, safety, or schema)."""


def canonical_bytes(manifest: dict) -> bytes:
    """Deterministic bytes a manifest is signed over: the object minus `signature`, JSON with sorted
    keys + compact separators + UTF-8. Signer and verifier must agree on exactly this."""
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_signature(manifest: dict) -> bool:
    """True iff the manifest carries a `signature` + `publisher.public_key` and the Ed25519 signature
    validates over the canonical bytes (i.e. untampered, signed by that key)."""
    key_b64 = (manifest.get("publisher") or {}).get("public_key")
    sig_b64 = manifest.get("signature")
    if not key_b64 or not sig_b64:
        return False
    try:
        VerifyKey(base64.b64decode(key_b64)).verify(canonical_bytes(manifest), base64.b64decode(sig_b64))
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def assess_trust(manifest: dict, trusted_keys: dict | None = None) -> str:
    """Trust tier for a manifest: 'verified' (signed + key matches the curated registry for that
    publisher) or 'community' (unsigned, or validly self-signed but not registry-trusted)."""
    keys = TRUSTED_KEYS if trusted_keys is None else trusted_keys
    pub = manifest.get("publisher") or {}
    if not manifest.get("signature") or not verify_signature(manifest):
        return "community"
    if pub.get("id") and keys.get(pub["id"]) == pub.get("public_key"):
        return "verified"
    return "community"


def _assert_public_url(url: str) -> None:
    """Reject non-http(s), blocked hosts, and any host that resolves to a non-public IP (SSRF)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FederationError("URL must be http or https")
    host = parsed.hostname
    if not host:
        raise FederationError("URL has no host")
    if host in BLOCKED_HOSTS:
        raise FederationError("host is on the blocklist")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # Early, friendly refusal only. The AUTHORITATIVE check is core.safe_http's pinned connect (M2):
    # this resolution is NOT what httpx dials, so it can never be the only guard.
    try:
        safe_http.resolve_public(host, port)
    except safe_http.UnsafeAddress as e:
        raise FederationError(str(e)) from e


async def fetch_manifest(url: str) -> dict:
    """Safely fetch + validate a Manifest v2 collection from `url`. Raises FederationError."""
    await asyncio.to_thread(_assert_public_url, url)   # C2: getaddrinfo is blocking — keep it off the loop
    try:
        async with safe_http.safe_async_client(headers={"User-Agent": "Pieria-Federation/1.0"}) as client:
            # follow_redirects=False on purpose — a redirect could bypass the SSRF pre-check.
            async with client.stream("GET", url, timeout=FETCH_TIMEOUT, follow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    raise FederationError("URL redirects; subscribe to the final URL directly")
                if resp.status_code != 200:
                    raise FederationError(f"HTTP {resp.status_code}")
                if "html" in resp.headers.get("content-type", "").lower():
                    raise FederationError("URL returned HTML, not a JSON manifest")
                chunks, total = [], 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_MANIFEST_BYTES:
                        raise FederationError("manifest exceeds the size cap")
                    chunks.append(chunk)
        raw = b"".join(chunks)
    except httpx.HTTPError as e:
        raise FederationError(f"fetch failed: {e}") from e

    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise FederationError(f"invalid JSON: {e}") from e

    errors = validate_manifest(obj)
    if errors:
        raise FederationError("invalid Manifest v2: " + "; ".join(errors[:5]))
    if len(obj.get("items", [])) > MAX_ITEMS:
        raise FederationError(f"manifest has too many items (>{MAX_ITEMS})")
    pub_id = (obj.get("publisher") or {}).get("id")
    if pub_id and pub_id in BLOCKED_PUBLISHERS:
        raise FederationError("publisher is on the blocklist")
    # A present-but-invalid signature means tampered/corrupt — reject outright. (Unsigned is fine;
    # it just stays in the 'community' tier.)
    if obj.get("signature") and not verify_signature(obj):
        raise FederationError("manifest signature is invalid (tampered, or wrong key)")
    return obj


def manifest_item_to_catalog(item: dict, default_license: str | None = None) -> dict:
    """Map a Manifest v2 item to the catalog item shape the browse UI + add flow already expect.

    Two asset modes: a remote item carries `image.full_url` (the app fetches it on add); a first-party
    pack item carries `image.local_file` (bytes already on disk under _Library/). For a local item the
    `source_url` is a stable non-http `pack:<file>` sentinel — it dedups + drives the `added` flag like
    any URL, but never gets fetched: the add path branches on `local_file` to reference it in place.

    `default_license` is the manifest's own top-level `default_license` (ADR-142 Stage B): when an item
    omits `image.license` (every item shares the manifest default — manifest_validator's
    `has_default_license` gate), pass the manifest's value here so it still reaches the catalog row
    instead of persisting as a bare None."""
    img = item.get("image") or {}
    tags = item.get("tags")
    local = img.get("local_file")
    source_url = f"pack:{local}" if local else img.get("full_url")
    return {
        "title": item.get("title"),
        "agent_name": item.get("artist"),
        "agent_role": item.get("artist_role"),
        "creation_date": item.get("creation_date"),
        "date_display": item.get("date"),
        "medium": item.get("medium"),
        "cultural_context": item.get("culture"),
        "description_narrative": item.get("placard"),
        "series": item.get("series"),
        "resolution_tier": item.get("resolution_tier"),
        "tags": ",".join(tags) if isinstance(tags, list) else (tags or ""),
        "source": img.get("rights_holder") or "",
        "license": img.get("license") or default_license,
        "license_url": img.get("license_url"),
        "attribution": img.get("attribution"),
        "attribution_url": img.get("attribution_url"),
        "origin_url": img.get("origin_url"),
        "source_url": source_url,
        "local_file": local,
        "thumbnail_url": img.get("thumbnail_url") or (f"pack:{local}" if local else img.get("full_url")),
        "focal_point": img.get("focal_point"),
        "aspect_crops": img.get("aspect_crops"),
    }


def manifest_key(manifest: dict) -> str | None:
    """The publisher key a manifest is validly signed with, else None (unsigned). fetch_manifest has
    already rejected a present-but-invalid signature, so a key here is a proven signing key."""
    if manifest.get("signature") and verify_signature(manifest):
        return (manifest.get("publisher") or {}).get("public_key")
    return None


def key_fingerprint(key_b64: str | None) -> str | None:
    """Short, human-comparable fingerprint of a base64 public key (sha256, first 16 hex, grouped)."""
    if not key_b64:
        return None
    try:
        raw = base64.b64decode(key_b64)
    except (ValueError, TypeError):
        raw = key_b64.encode("utf-8", "replace")
    h = hashlib.sha256(raw).hexdigest()[:16]
    return " ".join(h[i:i + 4] for i in range(0, 16, 4))


async def sync_subscription(db, sub: SubscriptionModel, retrust: bool = False) -> SubscriptionModel:
    """Re-fetch a subscription's manifest, validate it, and cache it on the row. Records status; a
    failed sync keeps the previous cached manifest (graceful degradation).

    L5 / ADR-148 key pinning (SSH-style TOFU): the first valid signing key is pinned. A later sync whose
    key CHANGED, or whose signature was REMOVED, is NOT applied — key_status='changed', the new key is
    parked in pending_public_key, and the last good cached manifest keeps being served until the user
    re-trusts. A registry-verified publisher follows the registry instead (our record) and is flagged
    'rotated' (pending_public_key then holds the previous pin). `retrust=True` (user action) accepts the
    pending key — but only if the feed still presents exactly that key."""
    try:
        manifest = await fetch_manifest(sub.url)
    except FederationError as e:
        return record_sync_error(db, sub, e)
    return apply_synced_manifest(db, sub, manifest, retrust)


def record_sync_error(db, sub: SubscriptionModel, e: Exception) -> SubscriptionModel:
    """The failed-fetch half of sync_subscription (sync DB work only — callers off the event loop
    run this in a threadpool with their own short-lived session)."""
    sub.last_status = f"error: {e}"
    sub.last_synced = datetime.now(UTC)
    db.commit()
    return sub


def apply_synced_manifest(db, sub: SubscriptionModel, manifest: dict,
                          retrust: bool = False) -> SubscriptionModel:
    """The apply half of sync_subscription (key pinning + cache the manifest). Pure sync DB work,
    split out so routers can `await fetch_manifest` on the loop and apply in a threadpool."""
    trust = assess_trust(manifest)
    new_key = manifest_key(manifest)
    pinned = sub.pinned_public_key
    if pinned and new_key != pinned:
        if trust == "verified":
            logger.warning("subscription %s: registry key rotated; following the registry", sub.id)
            sub.key_status = "rotated"
            sub.pending_public_key = pinned          # the previous pin, shown in the UI
            sub.pinned_public_key = new_key
        elif retrust and sub.key_status == "changed" and new_key == sub.pending_public_key:
            logger.info("subscription %s: publisher key re-trusted by user", sub.id)
            sub.pinned_public_key = new_key
            sub.key_status, sub.pending_public_key = "ok", None
        else:
            logger.warning("subscription %s: publisher key %s — manifest NOT applied",
                           sub.id, "REMOVED (unsigned)" if new_key is None else "CHANGED")
            sub.key_status = "changed"
            sub.pending_public_key = new_key
            sub.last_status = "blocked: publisher key changed — review before trusting"
            sub.last_synced = datetime.now(UTC)
            db.commit()
            return sub
    elif pinned:
        if sub.key_status == "changed":              # feed went back to the pinned key
            sub.key_status, sub.pending_public_key = "ok", None
    elif new_key:
        logger.info("subscription %s: pinning publisher key on first signed sync", sub.id)
        sub.pinned_public_key = new_key

    pub = manifest.get("publisher") or {}
    sub.collection_id = manifest.get("id")
    sub.title = manifest.get("title")
    sub.publisher_id = pub.get("id")
    sub.publisher_name = pub.get("name")
    sub.publisher_url = pub.get("url")
    sub.trust = trust   # re-evaluate the tier on every sync
    sub.cached_manifest = json.dumps(manifest)
    sub.item_count = len(manifest.get("items", []))
    sub.last_status = "ok"
    sub.last_synced = datetime.now(UTC)
    db.commit()
    return sub
