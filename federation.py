"""Federation — safely fetch, validate, and cache subscribed Manifest v2 collections.

Security posture (we index pointers, never host bytes; the user chose the URL):
- http(s) only; an **SSRF guard** blocks hosts that resolve to private/loopback/link-local/reserved
  addresses (no reaching internal services like cloud metadata or localhost).
- **redirects disabled** (a 3xx could bounce past the SSRF check to an internal host).
- **size cap + content-type/JSON check + timeout** (no zip-bombs / HTML / hangs).
- **strict Manifest v2 validation** before anything is cached or shown.
- a **host/publisher blocklist** the fetch honors (revocation).

Untrusted manifest strings are escaped at *render* time (the /art page + browse UI), not here.
"""

import asyncio
import base64
import ipaddress
import json
import logging
import socket
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from models import SubscriptionModel
from tools.manifest_validator import validate_manifest

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
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FederationError(f"cannot resolve host: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise FederationError(f"host resolves to a non-public address ({ip}) — blocked")


async def fetch_manifest(url: str) -> dict:
    """Safely fetch + validate a Manifest v2 collection from `url`. Raises FederationError."""
    await asyncio.to_thread(_assert_public_url, url)   # C2: getaddrinfo is blocking — keep it off the loop
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "ScreenDocent-Federation/1.0"}) as client:
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


def manifest_item_to_catalog(item: dict) -> dict:
    """Map a Manifest v2 item to the catalog item shape the browse UI + add flow already expect."""
    img = item.get("image") or {}
    tags = item.get("tags")
    return {
        "title": item.get("title"),
        "agent_name": item.get("artist"),
        "agent_role": item.get("artist_role"),
        "creation_date": item.get("creation_date"),
        "date_display": item.get("date"),
        "medium": item.get("medium"),
        "cultural_context": item.get("culture"),
        "description_narrative": item.get("placard"),
        "tags": ",".join(tags) if isinstance(tags, list) else (tags or ""),
        "source": img.get("rights_holder") or "",
        "license": img.get("license"),
        "source_url": img.get("full_url"),
        "thumbnail_url": img.get("thumbnail_url") or img.get("full_url"),
        "focal_point": img.get("focal_point"),
    }


async def sync_subscription(db, sub: SubscriptionModel) -> SubscriptionModel:
    """Re-fetch a subscription's manifest, validate it, and cache it on the row. Records status; a
    failed sync keeps the previous cached manifest (graceful degradation)."""
    try:
        manifest = await fetch_manifest(sub.url)
    except FederationError as e:
        sub.last_status = f"error: {e}"
        sub.last_synced = datetime.now(UTC)
        db.commit()
        return sub

    pub = manifest.get("publisher") or {}
    sub.collection_id = manifest.get("id")
    sub.title = manifest.get("title")
    sub.publisher_id = pub.get("id")
    sub.publisher_name = pub.get("name")
    sub.publisher_url = pub.get("url")
    sub.trust = assess_trust(manifest)   # re-evaluate the tier on every sync
    sub.cached_manifest = json.dumps(manifest)
    sub.item_count = len(manifest.get("items", []))
    sub.last_status = "ok"
    sub.last_synced = datetime.now(UTC)
    db.commit()
    return sub
