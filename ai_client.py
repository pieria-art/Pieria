"""
Unified OpenAI-compatible AI client for Pieria.

Single source of truth for ALL model calls (vision enrichment + fast classification).
Every provider — Google Gemini, OpenAI, Anthropic, OpenRouter, and local servers such as
Ollama / LM Studio — is reached through one OpenAI-compatible ``/chat/completions`` endpoint,
so configuration collapses to three fields: base_url + api_key + model.

Configuration is read from the ``settings`` table (GUI-editable in Admin → AI Engine), falling
back to the ``GEMINI_API_KEY`` environment variable + Gemini defaults so existing ``.env``
deployments keep working with zero changes (now routed through Gemini's OpenAI-compat endpoint).
"""

import base64
import io
import ipaddress
import json
import logging
import os
import time
from urllib.parse import urlparse

import httpx
from PIL import Image

from database import SessionLocal
from models import SettingsModel

logger = logging.getLogger("artwork-display-api.ai_client")

# -----------------------------------------------------------------------------
# Provider presets — base_url + a curated, known-good model list per provider.
# Mirrored (loosely) by the admin UI so users get sensible defaults per provider.
# -----------------------------------------------------------------------------
PRESETS = {
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        # gemini-3.5-flash + 3.1-flash-lite are GA; 3.1-pro-preview is the latest Pro
        # currently exposed (swap to gemini-3.5-pro once it reaches GA).
        "models": ["gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-3.1-flash-lite"],
        "key_url": "https://aistudio.google.com/apikey",
        "json_mode": True,
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"],
        "key_url": "https://platform.openai.com/api-keys",
        "json_mode": True,
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "base_url": "https://api.anthropic.com/v1",
        "models": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"],
        "key_url": "https://console.anthropic.com/settings/keys",
        # Anthropic's OpenAI-compat layer does not reliably honour response_format;
        # rely on prompt + tolerant fence-stripping instead.
        "json_mode": False,
    },
    "openrouter": {
        "label": "OpenRouter (one-click sign-in)",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            "google/gemini-3.5-flash",
            "openai/gpt-5.4-mini",
            "anthropic/claude-haiku-4-5",
            "meta-llama/llama-3.2-90b-vision-instruct",
        ],
        "oauth": True,
        "key_url": "https://openrouter.ai/keys",
        "json_mode": True,
    },
    "ollama": {
        "label": "Local (Ollama / LM Studio)",
        # From inside Docker, localhost is the container — reach the host via host.docker.internal.
        "base_url": "http://host.docker.internal:11434/v1",
        "models": ["llama3.2-vision", "llava", "qwen2.5vl"],
        "key_optional": True,
        "json_mode": False,
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "base_url": "",
        "models": [],
        "json_mode": False,
    },
}

# Built-in defaults (Gemini), used when nothing is configured in the DB.
DEFAULT_PROVIDER = "gemini"
DEFAULT_BASE_URL = PRESETS["gemini"]["base_url"]
DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_FAST_MODEL = "gemini-3.1-flash-lite"

# Keys we persist in the SettingsModel KV table.
AI_SETTING_KEYS = (
    "ai_provider",
    "ai_base_url",
    "ai_api_key",
    "ai_model",
    "ai_model_fast",
    "ai_temperature",
)


class AIConfigError(RuntimeError):
    """Raised when the AI provider is unconfigured or the API call fails."""


# -----------------------------------------------------------------------------
# Config resolution (DB → env → defaults) with a short per-process TTL cache.
# The cache keeps hot paths (enrichment, classification) from hitting SQLite on
# every call, while a 30s TTL means a GUI save propagates to all workers quickly.
# -----------------------------------------------------------------------------
_cache = {"data": None, "ts": 0.0}
_CACHE_TTL = 30.0


def _read_settings_rows() -> dict:
    db = SessionLocal()
    try:
        rows = (
            db.query(SettingsModel)
            .filter(SettingsModel.setting_key.in_(AI_SETTING_KEYS))
            .all()
        )
        return {r.setting_key: r.setting_value for r in rows if r.setting_value}
    finally:
        db.close()


def get_ai_config(force: bool = False) -> dict:
    """Return the effective AI config: DB settings → GEMINI_API_KEY env → Gemini defaults."""
    now = time.monotonic()
    if not force and _cache["data"] is not None and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["data"]

    try:
        s = _read_settings_rows()
    except Exception as e:  # DB not ready / table missing — fall back to env.
        logger.warning(f"[AI] Could not read settings ({e}); using env/defaults.")
        s = {}

    provider = s.get("ai_provider") or DEFAULT_PROVIDER
    base_url = (s.get("ai_base_url") or "").rstrip("/")
    if not base_url:
        base_url = PRESETS.get(provider, {}).get("base_url", DEFAULT_BASE_URL).rstrip("/")
    api_key = s.get("ai_api_key") or os.getenv("GEMINI_API_KEY") or ""
    model = s.get("ai_model") or DEFAULT_MODEL
    model_fast = s.get("ai_model_fast") or model
    temp_raw = s.get("ai_temperature")
    try:
        temperature = float(temp_raw) if temp_raw not in (None, "") else None
    except (TypeError, ValueError):
        temperature = None

    cfg = {
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "model_fast": model_fast,
        "temperature": temperature,
        "configured": bool(api_key),
    }
    _cache["data"] = cfg
    _cache["ts"] = now
    return cfg


def invalidate_config_cache() -> None:
    """Force the next get_ai_config() to re-read the DB (call after a settings write)."""
    _cache["data"] = None
    _cache["ts"] = 0.0


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal", "0.0.0.0"}


def is_local_base_url(base_url: str) -> bool:
    """True when the model endpoint is on-device / on the LAN (Ollama, LM Studio,
    host.docker.internal, a private IP) — so images sent to it never leave the user's network.
    Lets the Studio show an honest privacy note for AI captioning instead of a blanket warning."""
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in _LOCAL_HOSTS or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def supports_json_mode(provider: str) -> bool:
    return bool(PRESETS.get(provider, {}).get("json_mode", False))


# -----------------------------------------------------------------------------
# Content-part + parsing helpers
# -----------------------------------------------------------------------------
def strip_json_fences(text: str) -> str:
    """Strip ```json ... ``` markdown fences so providers that ignore json-mode still parse."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return text


def parse_json(text: str):
    """Tolerant JSON parse: strips fences first."""
    return json.loads(strip_json_fences(text))


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(source, max_px: int = 2048, quality: int = 85) -> dict:
    """Build an OpenAI-style image_url content part (base64 data URI) from a path or bytes."""
    if isinstance(source, (bytes, bytearray)):
        img = Image.open(io.BytesIO(source))
    else:
        img = Image.open(source)
    with img:
        if img.mode not in ("RGB",):
            img = img.convert("RGB")
        img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


# -----------------------------------------------------------------------------
# The one model call everything goes through.
# -----------------------------------------------------------------------------
def _resolve_model(cfg: dict, role: str) -> str:
    return cfg["model_fast"] if role == "fast" else cfg["model"]


_http_client = httpx.Client()   # B5: pooled + thread-safe; chat() runs via asyncio.to_thread


def chat(
    role: str,
    messages: list,
    json_mode: bool = False,
    temperature=None,
    timeout: float = 90.0,
    cfg: dict = None,
) -> str:
    """
    Issue an OpenAI-compatible chat completion and return the assistant message text.

    role: "vision" → enrichment model; "fast" → classification model (falls back to the
          primary model when no fast override is set).
    cfg:  optional explicit config dict (used by the settings validator to test a candidate
          config before persisting); defaults to the live resolved config.
    """
    cfg = cfg or get_ai_config()
    if not cfg.get("api_key") and not PRESETS.get(cfg.get("provider"), {}).get("key_optional"):
        raise AIConfigError(
            "No AI model configured. Set one in Admin → AI Engine, or provide GEMINI_API_KEY."
        )

    model = _resolve_model(cfg, role)
    payload = {"model": model, "messages": messages}

    if json_mode and supports_json_mode(cfg.get("provider", "")):
        payload["response_format"] = {"type": "json_object"}

    t = temperature if temperature is not None else cfg.get("temperature")
    if t is not None:
        payload["temperature"] = t

    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    # OpenRouter attribution headers (harmless for other providers).
    headers["HTTP-Referer"] = "https://pieria.app"
    headers["X-Title"] = "Pieria"

    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
    # B5: reuse a pooled client (no fresh TLS handshake per call) + one bounded retry on transient
    # errors — matching the scout pattern, so a single 429/5xx blip doesn't silently fail an artwork's
    # enrichment (which happens in tight loops during batch_enrich).
    resp = None
    for attempt in range(2):
        try:
            resp = _http_client.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            if attempt == 0:
                time.sleep(1.5); continue
            raise AIConfigError(f"Could not reach the model endpoint: {e}") from e
        if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
            time.sleep(1.5); continue
        break

    if resp.status_code != 200:
        raise AIConfigError(f"Model API error {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        raise AIConfigError(f"Unexpected model response: {resp.text[:300]}") from e


def validate_config(provider: str, base_url: str, api_key: str, model: str) -> str:
    """
    Test a candidate config with a tiny live completion. Returns the model's reply text on
    success; raises AIConfigError on failure. Used by POST /api/settings/ai before persisting.
    """
    cfg = {
        "provider": provider,
        "base_url": (base_url or PRESETS.get(provider, {}).get("base_url", "")).rstrip("/"),
        "api_key": api_key or "",
        "model": model,
        "model_fast": model,
        "temperature": None,
    }
    if not cfg["base_url"]:
        raise AIConfigError("A base URL is required for this provider.")
    return chat(
        "vision",
        [{"role": "user", "content": "Reply with the single word: ok"}],
        timeout=25.0,
        cfg=cfg,
    )
