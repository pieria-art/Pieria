"""Settings API — museum API keys, AI engine config (+ OpenRouter OAuth), Samsung Frame TV push,
remote catalog source, default playlist, and the Night & Quiet Hours display schedule.
"""

import asyncio
import json
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import ai_client
import frame_push
from core.playback import _frame_select
from core.settings_util import (
    _HHMM_RE,
    SCHEDULE_SETTING_KEY,
    _catalog_remote_base,
    _fetch_remote_json,
    _load_schedule,
    _upsert_setting,
)
from database import get_db
from models import PlaylistModel, SettingsModel

router = APIRouter()


# -----------------------------------------------------------------------------
# Default playlist (which playlist a freshly-loaded display falls back to)
# -----------------------------------------------------------------------------

class DefaultPlaylistPayload(BaseModel):
    default_playlist: Optional[str] = None


@router.get("/api/settings/default-playlist")
async def get_default_playlist(db: Session = Depends(get_db)):
    row = db.query(SettingsModel).filter(SettingsModel.setting_key == "default_playlist").first()
    return {"default_playlist": row.setting_value if row else None}


@router.post("/api/settings/default-playlist")
async def set_default_playlist(payload: DefaultPlaylistPayload, db: Session = Depends(get_db)):
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


@router.get("/api/settings/display-schedule")
async def get_display_schedule(db: Session = Depends(get_db)):
    return _load_schedule(db)


@router.post("/api/settings/display-schedule")
async def set_display_schedule(payload: DisplaySchedulePayload, db: Session = Depends(get_db)):
    """Merge the given fields over the current schedule, validate, and persist as JSON."""
    merged = _load_schedule(db)
    for k, v in payload.model_dump(exclude_none=True).items():
        merged[k] = v
    # Validate ranges/formats so a bad value can't wedge the resolver or the Canvas overlay.
    for bkey in ("day_brightness", "night_brightness"):
        if not (0.1 <= float(merged[bkey]) <= 1.0):
            raise HTTPException(400, detail=f"{bkey} must be between 0.1 and 1.0")
    if not (0.0 <= float(merged["night_warmth"]) <= 1.0):
        raise HTTPException(400, detail="night_warmth must be between 0.0 and 1.0")
    for tkey in ("evening_start", "night_start", "morning_start", "day_start", "quiet_start", "quiet_end"):
        if not _HHMM_RE.match(str(merged[tkey])):
            raise HTTPException(400, detail=f"{tkey} must be HH:MM")
    if merged["quiet_mode"] not in ("cec", "blackout"):
        raise HTTPException(400, detail="quiet_mode must be 'cec' or 'blackout'")
    _upsert_setting(db, SCHEDULE_SETTING_KEY, json.dumps(merged))
    db.commit()
    return merged


# -----------------------------------------------------------------------------
# Museum API keys
# -----------------------------------------------------------------------------

@router.get("/api/settings/keys")
async def get_api_keys(db: Session = Depends(get_db)):
    """Returns a map of which API keys are unlocked."""
    settings = db.query(SettingsModel).all()
    # Check for presence of keys
    return {
        "harvard": any(s.setting_key == "harvard_api_key" for s in settings),
        "smithsonian": any(s.setting_key == "smithsonian_api_key" for s in settings),
        "europeana": any(s.setting_key == "europeana_api_key" for s in settings)
    }


@router.post("/api/settings/keys/{source}")
async def verify_and_save_api_key(source: str, payload: dict, db: Session = Depends(get_db)):
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

    setting = db.query(SettingsModel).filter(SettingsModel.setting_key == db_key).first()
    if setting:
        setting.setting_value = key
    else:
        setting = SettingsModel(setting_key=db_key, setting_value=key)
        db.add(setting)
    db.commit()
    return {"status": "success", "source": source}


# -----------------------------------------------------------------------------
# AI Engine (model provider configuration)
# -----------------------------------------------------------------------------

@router.get("/api/settings/ai")
async def get_ai_settings(db: Session = Depends(get_db)):
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
        "provider": rows.get("ai_provider", ai_client.DEFAULT_PROVIDER),
        "base_url": rows.get("ai_base_url", ""),
        "model": rows.get("ai_model", ""),
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


class AISettingsPayload(BaseModel):
    model_config = {"protected_namespaces": ()}  # allow "model"/"model_fast" field names
    provider: str
    base_url: Optional[str] = ""
    api_key: Optional[str] = None  # blank/omitted ⇒ keep the existing stored key
    model: str
    model_fast: Optional[str] = ""
    temperature: Optional[str] = ""


@router.post("/api/settings/ai")
async def save_ai_settings(payload: AISettingsPayload, db: Session = Depends(get_db)):
    """Validates a candidate AI config against the live endpoint, then persists it."""
    provider = payload.provider
    if provider not in ai_client.PRESETS:
        raise HTTPException(400, f"Unknown provider: {provider}")
    if not payload.model:
        raise HTTPException(400, "A model name is required.")

    base_url = (payload.base_url or ai_client.PRESETS[provider]["base_url"]).rstrip("/")
    existing = db.query(SettingsModel).filter(SettingsModel.setting_key == "ai_api_key").first()
    api_key = (
        (payload.api_key or "").strip()
        or (existing.setting_value if existing else "")
        or os.getenv("GEMINI_API_KEY", "")
    )
    key_optional = ai_client.PRESETS[provider].get("key_optional", False)
    if not api_key and not key_optional:
        raise HTTPException(400, "An API key is required for this provider.")

    # Validate against the live endpoint before persisting (mirrors the museum-key flow).
    try:
        await asyncio.to_thread(ai_client.validate_config, provider, base_url, api_key, payload.model)
    except Exception as e:
        raise HTTPException(401, detail=f"Validation failed: {str(e)}")

    _upsert_setting(db, "ai_provider", provider)
    _upsert_setting(db, "ai_base_url", base_url)
    if api_key:
        _upsert_setting(db, "ai_api_key", api_key)
    _upsert_setting(db, "ai_model", payload.model)
    _upsert_setting(db, "ai_model_fast", (payload.model_fast or "").strip())
    _upsert_setting(db, "ai_temperature", (payload.temperature or "").strip())
    db.commit()
    ai_client.invalidate_config_cache()
    # We just proved this config works against the live endpoint, so any recorded failure describes the
    # OLD config. Leaving it would show "Auto-analysis failed: ..." to someone who has just fixed the
    # problem, until the next successful enrichment happened to clear it.
    ai_client.clear_failure()
    return {"status": "success", "provider": provider, "model": payload.model}


@router.get("/api/settings/ai/oauth/start")
async def ai_oauth_start(callback_url: str, challenge: str):
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
async def ai_oauth_exchange(payload: OAuthExchangePayload, db: Session = Depends(get_db)):
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
    _upsert_setting(db, "ai_provider", provider)
    _upsert_setting(db, "ai_base_url", ai_client.PRESETS[provider]["base_url"])
    _upsert_setting(db, "ai_api_key", key)
    if not db.query(SettingsModel).filter(SettingsModel.setting_key == "ai_model").first():
        _upsert_setting(db, "ai_model", ai_client.PRESETS[provider]["models"][0])
    db.commit()
    ai_client.invalidate_config_cache()
    return {"status": "success", "provider": provider}


# ---------------------------------------------------------------------------
# Samsung Frame TV push (Integrations)
# ---------------------------------------------------------------------------
# _frame_select (the selector shared with the Frame pusher's push loop) lives in
# core/playback.py — see that module's docstring for why.

@router.get("/api/settings/frame")
async def get_frame_settings(db: Session = Depends(get_db)):
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
async def save_frame_settings(payload: FrameSettingsPayload, db: Session = Depends(get_db)):
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
async def test_frame_push(db: Session = Depends(get_db)):
    """One-shot 'Test / Push now'. Returns a structured result (never 500s) so the GUI can show a
    clean message with or without a TV present."""
    return await frame_push.run_test_push(_frame_select)


class CatalogSourcePayload(BaseModel):
    catalog_url: Optional[str] = ""


@router.get("/api/settings/catalog")
async def get_catalog_source(db: Session = Depends(get_db)):
    """Current remote catalog base URL (empty ⇒ serving the bundled catalog)."""
    base = await _catalog_remote_base(db)
    return {"catalog_url": base or "", "using_remote": bool(base)}


@router.post("/api/settings/catalog")
async def save_catalog_source(payload: CatalogSourcePayload, db: Session = Depends(get_db)):
    """Set or clear the remote catalog base URL — a static host serving `index.json` + per-collection
    files (no server required). Validation is advisory: we test-fetch `index.json` and report the
    collection count, but still persist a currently-unreachable URL (the runtime fetch falls back to
    bundled on any failure) so the GUI can warn rather than block. An empty value reverts to bundled."""
    url = (payload.catalog_url or "").strip().rstrip("/")
    if not url:
        row = db.query(SettingsModel).filter(SettingsModel.setting_key == "catalog_url").first()
        if row:
            db.delete(row); db.commit()
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

    _upsert_setting(db, "catalog_url", url)
    db.commit()
    result = {"status": "success", "catalog_url": url, "using_remote": True}
    if warning:
        result["warning"] = warning
    else:
        result["message"] = f"Connected — {collections} collection(s) found."
    return result
