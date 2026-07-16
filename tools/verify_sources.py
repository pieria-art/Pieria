#!/usr/bin/env python3
"""Verify every image source Screen Docent depends on still resolves to a real image.

A deterministic health-check (NOT an AI agent) over three scopes: the factory seed, the bundled
catalog, and subscribed Manifest v2 feeds. External hosts rot silently — Wikimedia tightened its
thumbnail-width whitelist and a fresh box booted with an empty library before anyone noticed. This
is the regime that catches the next one. Runs on a weekly CI cron and on demand.

    python -m tools.verify_sources                        # all scopes
    python -m tools.verify_sources --scope seed,catalog   # CI scope (no DB needed)
    python -m tools.verify_sources --scope subscriptions  # local/admin (reads the app DB)
    python -m tools.verify_sources --limit 50 --json

Exit code is non-zero if any URL fails, so it drives a CI job / cron failure.

Mirrors the display-true gates in tools/build_catalog.py (_get / _source_ok / _thumb_is_real_image),
re-implemented locally to keep this tool dependency-light (build_catalog pulls ai_client/dotenv).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from config import SD_USER_AGENT
from scout import MIN_DISPLAY_EDGE, _wm_throttle

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_FILE = REPO_ROOT / "static" / "factory_seed.json"
CATALOG_DIR = REPO_ROOT / "static" / "catalog"

WIKIMEDIA_HOSTS = ("commons.wikimedia.org", "upload.wikimedia.org")
MAX_CONCURRENCY = 4       # for non-Wikimedia hosts; Wikimedia is serialized via _wm_throttle
POLITENESS_DELAY = 0.3    # seconds between non-Wikimedia requests


@dataclass
class UrlCheck:
    origin: str        # seed | catalog | subscription
    collection: str    # collection id / subscription label
    title: str
    kind: str          # source | thumbnail | cover | manifest
    url: str
    error: str | None = None   # pre-network failure (bad manifest, SSRF-blocked) — recorded, not fetched


@dataclass
class CheckResult:
    uc: UrlCheck
    ok: bool
    detail: str = ""


def _is_wikimedia(url: str) -> bool:
    return any(h in url for h in WIKIMEDIA_HOSTS)


# --------------------------------------------------------------- URL collection (no network)

def collect_seed(seed_file: Path = SEED_FILE) -> list[UrlCheck]:
    out: list[UrlCheck] = []
    for it in json.loads(Path(seed_file).read_text()):
        title = it.get("title", "?")
        if it.get("source_url"):
            out.append(UrlCheck("seed", "seed", title, "source", it["source_url"]))
        if it.get("thumbnail_url"):
            out.append(UrlCheck("seed", "seed", title, "thumbnail", it["thumbnail_url"]))
    return out


def collect_catalog(catalog_dir: Path = CATALOG_DIR) -> list[UrlCheck]:
    out: list[UrlCheck] = []
    catalog_dir = Path(catalog_dir)
    index = json.loads((catalog_dir / "index.json").read_text())
    for col in index.get("collections", []):
        cid = col.get("id", "?")
        if col.get("cover_thumbnail"):
            out.append(UrlCheck("catalog", cid, col.get("title", cid), "cover", col["cover_thumbnail"]))
        cfile = catalog_dir / f"{cid}.json"
        if not cfile.exists():
            continue
        data = json.loads(cfile.read_text())
        items = data.get("items") if isinstance(data, dict) else data
        for it in items or []:
            title = it.get("title", "?")
            if it.get("source_url"):
                out.append(UrlCheck("catalog", cid, title, "source", it["source_url"]))
            if it.get("thumbnail_url"):
                out.append(UrlCheck("catalog", cid, title, "thumbnail", it["thumbnail_url"]))
    return out


def collect_subscriptions(db) -> list[UrlCheck]:
    """Enabled subscriptions: validate the cached manifest, then emit (SSRF-guarded) asset URLs.
    Lazy imports so the seed/catalog scopes don't need the DB / federation modules."""
    from federation import _assert_public_url
    from manifest_validator import validate_manifest
    from models import SubscriptionModel

    out: list[UrlCheck] = []
    for s in db.query(SubscriptionModel).filter(SubscriptionModel.enabled == True).all():  # noqa: E712
        label = getattr(s, "title", None) or getattr(s, "collection_id", None) or f"sub-{s.id}"
        if not s.cached_manifest:
            out.append(UrlCheck("subscription", label, "(manifest)", "manifest", "", error="no cached manifest"))
            continue
        try:
            manifest = json.loads(s.cached_manifest)
        except Exception as e:
            out.append(UrlCheck("subscription", label, "(manifest)", "manifest", "", error=f"invalid JSON: {e}"))
            continue
        errs = validate_manifest(manifest)
        if errs:
            out.append(UrlCheck("subscription", label, "(manifest)", "manifest", "", error=f"invalid manifest: {errs[0]}"))
            continue
        for it in manifest.get("items", []):
            img = it.get("image") or {}
            title = it.get("title", "?")
            for kind, url in (("source", img.get("full_url")),
                              ("thumbnail", img.get("thumbnail_url") or img.get("full_url"))):
                if not url:
                    continue
                try:
                    _assert_public_url(url)
                except Exception as e:
                    out.append(UrlCheck("subscription", label, title, kind, url, error=f"SSRF guard: {e}"))
                    continue
                out.append(UrlCheck("subscription", label, title, kind, url))
    return out


# --------------------------------------------------------------- per-URL checks

# Transient transport failures (timeouts, connection resets, protocol hiccups) get retried with
# backoff rather than recorded as a dead source — a slow/throttling host (notably Wikimedia's
# on-the-fly thumbnail generation, which is slow on first hit then cached) must not read as rot.
# Genuine rot still fails: 404/403/deleted/not-an-image are none of these and return immediately.
_TRANSIENT_EXC = httpx.TransportError


async def _get(client, url, **kw):
    """GET resilient to 429 (Retry-After), transient transport errors, and 5xx — each retried with
    backoff (mirrors tools/build_catalog.py:_get). Permanent 4xx (other than 429) return at once, so
    real source-rot still surfaces; only flaky timeouts/throttling are absorbed."""
    r = None
    for attempt in range(5):
        try:
            r = await client.get(url, **kw)
        except _TRANSIENT_EXC:
            if attempt == 4:
                raise                                  # persistent → let check_url record it as failed
            await asyncio.sleep(min(2.0 * (attempt + 1), 30.0))
            continue
        code = getattr(r, "status_code", None)
        if code == 429:
            try:
                wait = float(r.headers.get("retry-after", "") or 0)
            except ValueError:
                wait = 0.0
            await asyncio.sleep(min(wait or 2.0 * (attempt + 1), 30.0))
            continue
        if code in (500, 502, 503, 504) and attempt < 4:  # transient server-side → back off and retry
            await asyncio.sleep(min(2.0 * (attempt + 1), 30.0))
            continue
        return r
    return r


async def _check_is_image(client, url, *, ranged: bool = False) -> tuple[bool, str]:
    """200/206 + real raster content-type (not svg). When not ranged, decode-verify the bytes."""
    headers = {"Range": "bytes=0-4095"} if ranged else None
    r = await _get(client, url, timeout=30.0, follow_redirects=True, headers=headers)
    code = getattr(r, "status_code", None)
    if code not in (200, 206):
        return False, f"HTTP {code}"
    ct = (r.headers.get("content-type") or "").lower()
    if not ct.startswith("image/") or "svg" in ct:
        return False, f"content-type {ct or '?'}"
    if not ranged:
        try:
            Image.open(BytesIO(r.content)).verify()
        except Exception:
            return False, "not a decodable image"
    return True, f"HTTP {code} {ct}"


async def _check_source_sized(client, url) -> tuple[bool, str]:
    """Non-Wikimedia source: header-bytes dimension read, gated at >= MIN_DISPLAY_EDGE.
    Wikimedia FilePath is pre-gated at resolve time, so those take the cheap _check_is_image path."""
    r = await _get(client, url, timeout=60.0, follow_redirects=True, headers={"Range": "bytes=0-262143"})
    code = getattr(r, "status_code", None)
    if code not in (200, 206):
        return False, f"HTTP {code}"
    ct = (r.headers.get("content-type") or "").lower()
    if not ct.startswith("image/") or "svg" in ct:
        return False, f"content-type {ct or '?'}"
    try:
        w, h = Image.open(BytesIO(r.content)).size
    except Exception:
        r2 = await _get(client, url, timeout=90.0, follow_redirects=True)
        if getattr(r2, "status_code", None) != 200:
            return False, f"HTTP {getattr(r2, 'status_code', None)} (full)"
        w, h = Image.open(BytesIO(r2.content)).size
    if max(w, h) < MIN_DISPLAY_EDGE:
        return False, f"{w}x{h} < {MIN_DISPLAY_EDGE}px gate"
    return True, f"{w}x{h}"


async def check_url(client, uc: UrlCheck) -> CheckResult:
    if uc.error:                       # pre-network failure (bad manifest / SSRF) — record as-is
        return CheckResult(uc, False, uc.error)
    try:
        if uc.kind == "source" and not _is_wikimedia(uc.url):
            ok, detail = await _check_source_sized(client, uc.url)
        elif uc.kind == "source":      # Wikimedia source: pre-gated; cheap ranged content-type check
            ok, detail = await _check_is_image(client, uc.url, ranged=True)
        else:                          # thumbnail / cover
            ok, detail = await _check_is_image(client, uc.url)
        return CheckResult(uc, ok, detail)
    except Exception as e:
        return CheckResult(uc, False, f"error: {e.__class__.__name__}: {e}")


async def run_checks(checks: list[UrlCheck], *, client=None) -> list[CheckResult]:
    if not checks:
        return []
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    own = client is None
    if own:
        client = httpx.AsyncClient(headers={"User-Agent": SD_USER_AGENT}, follow_redirects=True)

    async def one(uc: UrlCheck) -> CheckResult:
        async with sem:
            if uc.url and _is_wikimedia(uc.url):
                await _wm_throttle()          # serialize Wikimedia to its polite interval
            res = await check_url(client, uc)
            if uc.url and not _is_wikimedia(uc.url):
                await asyncio.sleep(POLITENESS_DELAY)
            return res

    try:
        # group by host so per-host bursts cluster (Wikimedia ends up serialized regardless)
        ordered = sorted(checks, key=lambda c: c.url)
        return list(await asyncio.gather(*(one(uc) for uc in ordered)))
    finally:
        if own:
            await client.aclose()


# A rot-detector must red on ROT (a source that stopped resolving), not on a host's transient mood.
# Transient = timeout / 429 / 5xx / no-response AFTER _get already retried with backoff — most often
# Wikimedia throttling an expensive on-the-fly thumbnail render, which is fine once cached. We tolerate
# a small number of these; a spike (systemic outage, or a block like the AIC-Cloudflare event) still reds.
TRANSIENT_TOLERANCE_FRAC = 0.005    # tolerate transient blips up to 0.5% of all checks…
TRANSIENT_TOLERANCE_MIN = 3         # …but always allow at least this many
_TRANSIENT_EXC_NAMES = ("Timeout", "ConnectError", "ReadError", "RemoteProtocolError",
                        "NetworkError", "TransportError", "PoolTimeout")


def _is_transient(detail: str) -> bool:
    """True for retry-exhausted timeout/429/5xx/no-response — NOT for 404/403/wrong-type/too-small."""
    d = detail or ""
    if d.startswith("HTTP "):
        tok = (d.split() + [""])[1]
        return tok in {"429", "500", "502", "503", "504", "None"}
    if d.startswith("error: "):
        return any(name in d for name in _TRANSIENT_EXC_NAMES)
    return False


def report(results: list[CheckResult], *, strict: bool = False) -> tuple[str, int]:
    fails = [r for r in results if not r.ok]
    hard = [r for r in fails if not _is_transient(r.detail)]
    transient = [r for r in fails if _is_transient(r.detail)]
    total = len(results)
    tol = 0 if strict else max(TRANSIENT_TOLERANCE_MIN, int(total * TRANSIENT_TOLERANCE_FRAC))

    lines: list[str] = []
    if hard:
        lines.append(f"\n{len(hard)} HARD FAILURE(S) — likely source rot:")
        for r in hard:
            lines.append(f'  FAIL [{r.uc.origin}/{r.uc.collection}] "{r.uc.title}" ({r.uc.kind}) — {r.detail}')
            if r.uc.url:
                lines.append(f"        {r.uc.url}")
    if transient:
        over = strict or len(transient) > tol
        lines.append(f"\n{len(transient)} TRANSIENT failure(s) (timeout/429/5xx, retry-exhausted) "
                     f"— tolerance {tol}{' [EXCEEDED]' if over else ''}:")
        for r in transient:
            lines.append(f'  {"FAIL" if over else "warn"} [{r.uc.origin}/{r.uc.collection}] '
                         f'"{r.uc.title}" — {r.detail}')

    total_c = Counter(r.uc.origin for r in results)
    okc = Counter(r.uc.origin for r in results if r.ok)
    lines.append("")
    for origin in sorted(total_c):
        lines.append(f"  {origin}: {okc[origin]}/{total_c[origin]} ok")
    lines.append(f"\nchecked {total} urls — {total - len(fails)} passed, "
                 f"{len(hard)} hard-fail, {len(transient)} transient (tolerance {tol})")
    code = 1 if (hard or len(transient) > tol) else 0
    if code == 0 and transient:
        lines.append("PASS — transient blips within tolerance, not treated as rot.")
    return "\n".join(lines), code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scope", default="all",
                    help="all | comma list of: seed, catalog, subscriptions")
    ap.add_argument("--limit", type=int, default=0, help="check at most N URLs (smoke runs)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON results")
    ap.add_argument("--strict", action="store_true",
                    help="fail on ANY failure incl. transient blips (zero tolerance)")
    args = ap.parse_args(argv)

    scopes = ({"seed", "catalog", "subscriptions"} if args.scope == "all"
              else {s.strip() for s in args.scope.split(",") if s.strip()})

    checks: list[UrlCheck] = []
    if "seed" in scopes:
        checks += collect_seed()
    if "catalog" in scopes:
        checks += collect_catalog()
    if "subscriptions" in scopes:
        from database import SessionLocal
        db = SessionLocal()
        try:
            checks += collect_subscriptions(db)
        finally:
            db.close()

    if args.limit:
        checks = checks[:args.limit]

    results = asyncio.run(run_checks(checks))
    text, code = report(results, strict=args.strict)

    if args.json:
        print(json.dumps([{"origin": r.uc.origin, "collection": r.uc.collection, "title": r.uc.title,
                           "kind": r.uc.kind, "url": r.uc.url, "ok": r.ok, "detail": r.detail}
                          for r in results], indent=2))
    else:
        print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
