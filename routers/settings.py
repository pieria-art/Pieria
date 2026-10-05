"""Settings API — museum API keys, AI engine config (+ OpenRouter OAuth), Samsung Frame TV push,
remote catalog source, default playlist, and the Night & Quiet Hours display schedule.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

import ai_client
import frame_push
from core import sun as sun_mod
from core.playback import _frame_select
from core.settings_util import (
    ScheduleError,
    _catalog_remote_base_sync,
    _fetch_remote_json,
    _load_schedule,
    _upsert_setting,
    apply_schedule_patch,
    schedule_changes,
)
from database import SessionLocal, get_db
from models import PlaylistModel, SettingsModel

router = APIRouter()


# -----------------------------------------------------------------------------
# Default playlist (which playlist a freshly-loaded display falls back to)
# -----------------------------------------------------------------------------

class DefaultPlaylistPayload(BaseModel):
    default_playlist: Optional[str] = None


@router.get("/api/settings/default-playlist")
def get_default_playlist(db: Session = Depends(get_db)):
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    return {"default_playlist": row.setting_value if row else None}


@router.post("/api/settings/default-playlist")
def set_default_playlist(payload: DefaultPlaylistPayload, db: Session = Depends(get_db)):
    """Pin the fallback playlist a display boots to when it has no last-played history (e.g. a brand-new
    wall display). Empty string clears it. Validated against existing playlists."""
    name = (payload.default_playlist or "").strip()
    if name and not db.query(PlaylistModel).filter(PlaylistModel.name == name).first():
        raise HTTPException(400, detail=f"No playlist named '{name}'")
    _upsert_setting(db, "default_playlist", name)
    db.commit()
    return {"default_playlist": name}


# --- R1-F2: Night & Quiet Hours (clock-driven brightness/warmth + quiet-hours panel power) ----------
# Gentle defaults, warm-shift ON, quiet-hours panel-off OFF (opt-in) so nothing blanks unexpectedly.
# One global schedule for v1; the resolver takes a display_id so per-display overrides can layer in later
# (dev-rule #4 hierarchy). The Canvas applies a GPU-cheap CSS overlay; the appliance drives HDMI-CEC.
# resolve_schedule_state / _parse_hhmm / _cyc_* / _HHMM_RE live in core/settings_util.py — shared with
# the `/api/displays/{id}/schedule-state` route, which stays in app.py (display domain, not settings).

class DisplaySchedulePayload(BaseModel):
    enabled: Optional[bool] = None
    day_brightness: Optional[float] = None
    night_brightness: Optional[float] = None
    night_warmth: Optional[float] = None
    evening_start: Optional[str] = None
    night_start: Optional[str] = None
    morning_start: Optional[str] = None
    day_start: Optional[str] = None
    quiet_enabled: Optional[bool] = None
    quiet_start: Optional[str] = None
    quiet_end: Optional[str] = None
    quiet_mode: Optional[str] = None
    mode: Optional[str] = None
    evening_offset_min: Optional[int] = None
    night_offset_min: Optional[int] = None
    morning_offset_min: Optional[int] = None
    day_offset_min: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


@router.get("/api/settings/display-schedule/sun")
def get_display_schedule_sun(db: Session = Depends(get_db)):
    """Where the sun times come from and today's computed sunrise/sunset (for the admin hint)."""
    s = _load_schedule(db)
    loc = sun_mod.resolve_location(s)
    out = {"zone": sun_mod.current_zone(), "location": None, "sunrise": None, "sunset": None}
    if loc:
        out["location"] = {"latitude": loc[0], "longitude": loc[1], "source": loc[2]}
        t = sun_mod.todays_sun(s, datetime.now().date())
        if t:
            out["sunrise"], out["sunset"] = sun_mod.fmt_hhmm(t["sunrise"]), sun_mod.fmt_hhmm(t["sunset"])
    return out


@router.get("/api/settings/display-schedule")
def get_display_schedule(db: Session = Depends(get_db)):
    return _load_schedule(db)


@router.post("/api/settings/display-schedule")
def set_display_schedule(payload: DisplaySchedulePayload, db: Session = Depends(get_db)):
    """Merge the given fields over the current schedule, validate, and persist as JSON."""
    try:
        return apply_schedule_patch(db, schedule_changes(payload))
    except ScheduleError as e:
        raise HTTPException(400, detail=str(e))


# -----------------------------------------------------------------------------
# Museum API keys
# -----------------------------------------------------------------------------

@router.get("/api/settings/keys")
def get_api_keys(db: Session = Depends(get_db)):
    """Returns a map of which API keys are unlocked."""
    settings = db.query(SettingsModel).all()
    # Check for presence of keys
    return {
        "harvard": any(s.setting_key == "harvard_api_key" for s in settings),
        "smithsonian": any(s.setting_key == "smithsonian_api_key" for s in settings),
        "europeana": any(s.setting_key == "europeana_api_key" for s in settings)
    }


@router.post("/api/settings/keys/{source}")
async def verify_and_save_api_key(source: str, payload: dict):
    """Validates an API key against the source museum backend and persists it."""
    key = payload.get("api_key")
    if not key: raise HTTPException(400, "api_key payload is required.")

    try:
        async with httpx.AsyncClient() as client:
            if source == "harvard":
                resp = await client.get(f"https://api.harvardartmuseums.org/object?apikey={key}&size=1", timeout=15)
                if resp.status_code != 200: raise Exception("Harvard API rejected the key.")
                db_key = "harvard_api_key"
            elif source == "smithsonian":
                resp = await client.get(f"https://api.si.edu/openaccess/api/v1.0/search?q=art&api_key={key}&rows=1", timeout=15)
                if resp.status_code != 200: raise Exception("Smithsonian API rejected the key.")
                db_key = "smithsonian_api_key"
            elif source == "europeana":
                resp = await client.get(f"https://api.europeana.eu/record/v2/search.json?wskey={key}&query=*&rows=1", timeout=15)
                if resp.status_code != 200: raise Exception("Europeana API rejected the key.")
                db_key = "europeana_api_key"
            else:
                raise HTTPException(400, f"Unsupported museum target: {source}")
    except HTTPException:
        raise   # A6: don't re-wrap a deliberate 400 (unsupported source) as a 401 "Validation Failed"
    except Exception as e:
        raise HTTPException(401, detail=f"Validation Failed: {str(e)}")

    def _save():
        # ADR-148: own short-lived session in a worker thread (the M6 pattern), not a request-scoped one.
        with SessionLocal() as sdb:
            setting = sdb.query(SettingsModel).filter(SettingsModel.setting_key == db_key).first()
            if setting:
                setting.setting_value = key
            else:
                sdb.add(SettingsModel(setting_key=db_key, setting_value=key))
            sdb.commit()

    await run_in_threadpool(_save)
    return {"status": "success", "source": source}


# -----------------------------------------------------------------------------
# AI Engine (model provider configuration)
# -----------------------------------------------------------------------------

@router.get("/api/settings/ai")
def get_ai_settings(db: Session = Depends(get_db)):
    """Returns the current AI engine config (never the raw key) + provider presets for the UI."""
    rows = {
        s.setting_key: s.setting_value
        for s in db.query(SettingsModel)
        .filter(SettingsModel.setting_key.in_(ai_client.AI_SETTING_KEYS))
        .all()
    }
    cfg = ai_client.get_ai_config(force=True)
    health = ai_client.get_failure()
    return {
        "provider": cfg["provider"],  # saved, else legacy-env Gemini, else the fresh default (Claude)
        "base_url": rows.get("ai_base_url", ""),
        "model": rows.get("ai_model", ""),
        "default_model": ai_client.DEFAULT_MODEL if cfg["provider"] == ai_client.DEFAULT_PROVIDER else "",
        "fetched_models": _load_model_caches(db),
        "model_fast": rows.get("ai_model_fast", ""),
        "temperature": rows.get("ai_temperature", ""),
        "has_key": cfg["configured"],
        "key_source": "db" if rows.get("ai_api_key") else ("env" if cfg["configured"] else "none"),
        "model_is_local": ai_client.is_local_base_url(cfg["base_url"]),
        # Health, not config: has_key only proves a key EXISTS. A bad/expired/over-quota key passes
        # every config check and fails at call time, which used to be invisible to the user.
        "last_error": health["detail"],
        "last_error_at": health["at"],
        "presets": {
            k: {
                "label": v["label"],
                "base_url": v["base_url"],
                "models": v["models"],
                "oauth": v.get("oauth", False),
                "key_optional": v.get("key_optional", False),
                "key_url": v.get("key_url", ""),
            }
            for k, v in ai_client.PRESETS.items()
        },
    }


def _load_model_caches(db: Session) -> dict:
    """{provider: {models, fetched_at, base_url}} for every cached runtime model list."""
    out = {}
    rows = db.query(SettingsModel).filter(
        SettingsModel.setting_key.like(f"{ai_client.MODELS_CACHE_PREFIX}%")
    ).all()
    for r in rows:
        try:
            entry = json.loads(r.setting_value or "")
            if isinstance(entry.get("models"), list) and entry["models"]:
                out[r.setting_key[len(ai_client.MODELS_CACHE_PREFIX):]] = entry
        except (ValueError, AttributeError):
            continue
    return out


class ModelsRefreshPayload(BaseModel):
    provider: str
    base_url: Optional[str] = ""
    api_key: Optional[str] = None  # typed-but-unsaved key; blank ⇒ use the saved one


@router.post("/api/settings/ai/models")
async def refresh_ai_models(payload: ModelsRefreshPayload):
    """Fetch the provider's own model list (hard-filtered), cache it, and return it.

    Never sends a key anywhere but the provider's own base URL: preset providers ignore any
    client-supplied base_url, and the SAVED key is only used when the saved provider (and, for
    ollama/custom, saved base URL) matches. On failure falls back to the cache, then the presets.
    """
    provider = payload.provider
    preset = ai_client.PRESETS.get(provider)
    if preset is None:
        raise HTTPException(400, f"Unknown provider: {provider}")
    def _read_cfg_and_caches():
        with SessionLocal() as sdb:   # ADR-148: short-lived session in a worker thread
            return ai_client.get_ai_config(force=True), _load_model_caches(sdb)

    cfg, caches = await run_in_threadpool(_read_cfg_and_caches)
    user_url_ok = provider in ("ollama", "custom")
    if user_url_ok:
        base_url = (payload.base_url or "").strip().rstrip("/") or (
            cfg["base_url"] if cfg["provider"] == provider else preset["base_url"]
        )
    else:
        base_url = preset["base_url"].rstrip("/")
    api_key = (payload.api_key or "").strip()
    if not api_key and cfg["provider"] == provider and cfg["base_url"].rstrip("/") == base_url:
        api_key = cfg["api_key"]  # saved key, or the legacy GEMINI_API_KEY env for gemini

    try:
        models = await asyncio.to_thread(ai_client.fetch_provider_models, provider, base_url, api_key)
    except ai_client.AIConfigError as e:
        cached = caches.get(provider)
        if cached and cached.get("base_url") == base_url:
            return {"provider": provider, "models": cached["models"], "source": "cache",
                    "fetched_at": cached.get("fetched_at", ""), "error": str(e)}
        return {"provider": provider, "models": preset["models"], "source": "preset",
                "fetched_at": "", "error": str(e)}

    fetched_at = datetime.now(UTC).isoformat()
    def _write_cache():
        with SessionLocal() as sdb:
            _upsert_setting(
                sdb, ai_client.MODELS_CACHE_PREFIX + provider,
                json.dumps({"models": models, "fetched_at": fetched_at, "base_url": base_url}),
            )
            sdb.commit()

    await run_in_threadpool(_write_cache)
    return {"provider": provider, "models": models, "source": "live", "fetched_at": fetched_at, "error": ""}


class AISettingsPayload(BaseModel):
    model_config = {"protected_namespaces": ()}  # allow "model"/"model_fast" field names
    provider: str
    base_url: Optional[str] = ""
    api_key: Optional[str] = None  # blank/omitted ⇒ keep the existing stored key
    model: str
    model_fast: Optional[str] = ""
    temperature: Optional[str] = ""


@router.post("/api/settings/ai")
async def save_ai_settings(payload: AISettingsPayload):
    """Validates a candidate AI config against the live endpoint, then persists it."""
    provider = payload.provider
    if provider not in ai_client.PRESETS:
        raise HTTPException(400, f"Unknown provider: {provider}")
    if not payload.model:
        raise HTTPException(400, "A model name is required.")

    base_url = (payload.base_url or ai_client.PRESETS[provider]["base_url"]).rstrip("/")
    # The stored key belongs to ONE (provider, base_url). Reusing it for any other target would hand a
    # credential to a host the user never gave it to (e.g. POST provider=custom, base_url=attacker).
    cfg = await run_in_threadpool(ai_client.get_ai_config, force=True)
    same_target = provider == cfg["provider"] and base_url == cfg["base_url"].rstrip("/")
    api_key = (payload.api_key or "").strip() or (cfg["api_key"] if same_target else "")
    key_optional = ai_client.PRESETS[provider].get("key_optional", False)
    if not api_key and not key_optional:
        raise HTTPException(400, "An API key is required for this provider.")

    # Validate against the live endpoint before persisting (mirrors the museum-key flow).
    try:
        await asyncio.to_thread(ai_client.validate_config, provider, base_url, api_key, payload.model)
    except Exception as e:
        raise HTTPException(401, detail=f"Validation failed: {str(e)}")

    def _persist():
        with SessionLocal() as sdb:   # ADR-148: short-lived session in a worker thread
            _upsert_setting(sdb, "ai_provider", provider)
            _upsert_setting(sdb, "ai_base_url", base_url)
            if api_key:
                _upsert_setting(sdb, "ai_api_key", api_key)
            elif not same_target:
                _upsert_setting(sdb, "ai_api_key", "")  # never leave the old target's key bound to the new one
            _upsert_setting(sdb, "ai_model", payload.model)
            _upsert_setting(sdb, "ai_model_fast", (payload.model_fast or "").strip())
            _upsert_setting(sdb, "ai_temperature", (payload.temperature or "").strip())
            sdb.commit()

    await run_in_threadpool(_persist)
    ai_client.invalidate_config_cache()
    # We just proved this config works against the live endpoint, so any recorded failure describes the
    # OLD config. Leaving it would show "Auto-analysis failed: ..." to someone who has just fixed the
    # problem, until the next successful enrichment happened to clear it.
    await run_in_threadpool(ai_client.clear_failure)   # writes the health rows via its own session
    return {"status": "success", "provider": provider, "model": payload.model}


@router.get("/api/settings/ai/oauth/start")
def ai_oauth_start(callback_url: str, challenge: str):
    """Assembles the OpenRouter authorization URL (PKCE). The client holds the code_verifier."""
    from urllib.parse import urlencode
    params = urlencode({
        "callback_url": callback_url,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return {"auth_url": f"https://openrouter.ai/auth?{params}"}


class OAuthExchangePayload(BaseModel):
    code: str
    verifier: str


@router.post("/api/settings/ai/oauth/exchange")
async def ai_oauth_exchange(payload: OAuthExchangePayload):
    """Exchanges an OpenRouter auth code (+ PKCE verifier) for an API key and saves it."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/auth/keys",
                json={
                    "code": payload.code,
                    "code_verifier": payload.verifier,
                    "code_challenge_method": "S256",
                },
                timeout=20,
            )
        if resp.status_code != 200:
            raise Exception(resp.text[:200])
        key = resp.json().get("key")
        if not key:
            raise Exception("No key returned by OpenRouter.")
    except Exception as e:
        raise HTTPException(401, detail=f"OAuth exchange failed: {str(e)}")

    provider = "openrouter"
    def _persist():
        with SessionLocal() as sdb:   # ADR-148: short-lived session in a worker thread
            _upsert_setting(sdb, "ai_provider", provider)
            _upsert_setting(sdb, "ai_base_url", ai_client.PRESETS[provider]["base_url"])
            _upsert_setting(sdb, "ai_api_key", key)
            if not sdb.query(SettingsModel).filter(SettingsModel.setting_key == "ai_model").first():
                _upsert_setting(sdb, "ai_model", ai_client.PRESETS[provider]["models"][0])
            sdb.commit()

    await run_in_threadpool(_persist)
    ai_client.invalidate_config_cache()
    return {"status": "success", "provider": provider}


# ---------------------------------------------------------------------------
# Samsung Frame TV push (Integrations)
# ---------------------------------------------------------------------------
# _frame_select (the selector shared with the Frame pusher's push loop) lives in
# core/playback.py — see that module's docstring for why.

@router.get("/api/settings/frame")
def get_frame_settings(db: Session = Depends(get_db)):
    """Current Frame TV config (+ last-push status) for the Settings panel."""
    cfg = frame_push.get_frame_config(force=True)
    return {
        "enabled": cfg["enabled"],
        "host": cfg["host"],
        "port": cfg["port"],
        "playlist": cfg["playlist"],
        "interval_sec": cfg["interval_sec"],
        "width": cfg["width"],
        "height": cfg["height"],
        "matte": cfg["matte"],
        "last_artwork_id": cfg["last_artwork_id"],
        "last_push_at": cfg["last_push_at"],
    }


class FrameSettingsPayload(BaseModel):
    enabled: bool = False
    host: Optional[str] = ""
    port: Optional[int] = 8001
    playlist: Optional[str] = ""
    interval_sec: Optional[int] = 900
    width: Optional[int] = 3840
    height: Optional[int] = 2160
    matte: Optional[str] = "none"


@router.post("/api/settings/frame")
def save_frame_settings(payload: FrameSettingsPayload, db: Session = Depends(get_db)):
    """Persist Frame TV settings. Takes effect on the next push cycle (config cache invalidated)."""
    if payload.enabled and not (payload.host or "").strip():
        raise HTTPException(400, "A Frame TV host/IP is required to enable pushing.")
    _upsert_setting(db, "frame_enabled", "true" if payload.enabled else "false")
    _upsert_setting(db, "frame_host", (payload.host or "").strip())
    _upsert_setting(db, "frame_port", str(payload.port or 8001))
    _upsert_setting(db, "frame_playlist", (payload.playlist or "").strip())
    _upsert_setting(db, "frame_interval_sec", str(max(60, payload.interval_sec or 900)))
    _upsert_setting(db, "frame_width", str(payload.width or 3840))
    _upsert_setting(db, "frame_height", str(payload.height or 2160))
    _upsert_setting(db, "frame_matte", (payload.matte or "none").strip())
    db.commit()
    frame_push.invalidate_frame_cache()
    return {"status": "success"}


@router.post("/api/settings/frame/test")
async def test_frame_push():
    """One-shot 'Test / Push now'. Returns a structured result (never 500s) so the GUI can show a
    clean message with or without a TV present."""
    return await frame_push.run_test_push(_frame_select)


class CatalogSourcePayload(BaseModel):
    catalog_url: Optional[str] = ""


@router.get("/api/settings/catalog")
def get_catalog_source(db: Session = Depends(get_db)):
    """Current remote catalog base URL (empty ⇒ serving the bundled catalog)."""
    base = _catalog_remote_base_sync(db)
    return {"catalog_url": base or "", "using_remote": bool(base)}


@router.post("/api/settings/catalog")
async def save_catalog_source(payload: CatalogSourcePayload):
    """Set or clear the remote catalog base URL — a static host serving `index.json` + per-collection
    files (no server required). Validation is advisory: we test-fetch `index.json` and report the
    collection count, but still persist a currently-unreachable URL (the runtime fetch falls back to
    bundled on any failure) so the GUI can warn rather than block. An empty value reverts to bundled."""
    url = (payload.catalog_url or "").strip().rstrip("/")
    if not url:
        def _clear():
            with SessionLocal() as sdb:   # ADR-148: short-lived session in a worker thread
                row = sdb.query(SettingsModel).filter(SettingsModel.setting_key == "catalog_url").first()
                if row:
                    sdb.delete(row); sdb.commit()

        await run_in_threadpool(_clear)
        return {"status": "success", "catalog_url": "", "using_remote": False,
                "message": "Reverted to the bundled catalog."}
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Catalog URL must start with http:// or https://")

    warning = None
    collections = 0
    try:
        index = await _fetch_remote_json(url, "index.json")
        if not isinstance(index, dict) or "collections" not in index:
            raise HTTPException(400, "Reached the URL, but it doesn't look like a catalog index "
                                     "(no 'collections' key). Point at the base path that serves "
                                     "index.json.")
        collections = len(index.get("collections") or [])
    except HTTPException:
        raise
    except Exception as e:
        warning = (f"Saved, but couldn't reach {url}/index.json right now ({e}). The app will keep "
                   f"using the bundled catalog until it becomes reachable.")

    def _persist():
        with SessionLocal() as sdb:
            _upsert_setting(sdb, "catalog_url", url)
            sdb.commit()

    await run_in_threadpool(_persist)
    result = {"status": "success", "catalog_url": url, "using_remote": True}
    if warning:
        result["warning"] = warning
    else:
        result["message"] = f"Connected — {collections} collection(s) found."
    return result
