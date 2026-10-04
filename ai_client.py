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
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from PIL import Image

from database import SessionLocal
from models import SettingsModel

logger = logging.getLogger("artwork-display-api.ai_client")

# -----------------------------------------------------------------------------
# Provider presets — base_url + a curated, known-good model list per provider.
# Mirrored (loosely) by the admin UI so users get sensible defaults per provider.
#
# These lists are HAND-MAINTAINED and therefore go stale — the 2026-07-25 UAT caught them a full
# generation behind, which ages the product on day one. POST-LAUNCH: fetch each provider's own model
# list at runtime (all of them expose one; the OpenAI-compatible shape is `GET {base_url}/models`) and
# filter hard — vision-capable only, no image-GENERATION models, no dated snapshots, no deprecated
# entries — falling back to the hardcoded list below when the fetch fails or the box is offline. The
# filter is the load-bearing part: an unfiltered list would offer models that can't do enrichment.
# -----------------------------------------------------------------------------
PRESETS = {
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        # All GA as of 2026-07. 3.6-flash is the current stable default; flash-lite is the cheap tier.
        # NB the "-image" Gemini models are image GENERATION — not what enrichment needs, which is
        # vision *input*. Don't add them here.
        "models": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"],
        "key_url": "https://aistudio.google.com/apikey",
        "json_mode": True,
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        # 5.6 family is current (terra = balanced, sol = high-capability, luna = cost-optimised).
        # gpt-5.4 kept as a known-good fallback: enrichment needs VISION input, and if a 5.6 variant
        # ever ships without it the user needs somewhere to land that isn't "nothing works".
        "models": ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.4"],
        "key_url": "https://platform.openai.com/api-keys",
        "json_mode": True,
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "base_url": "https://api.anthropic.com/v1",
        # Sonnet 5.5 leads: near-Opus quality on this workload at a fraction of Opus pricing, so it's
        # the right default for placard writing. Opus 5.5 for the hardest cases, Haiku 4.5 for cheap.
        # Saved settings holding an older id (claude-sonnet-5 ...) keep working — never rewritten.
        "models": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5-20251001"],
        # Anthropic's OpenAI-compatible layer wants an explicit output cap.
        "max_tokens": 8192,
        "key_url": "https://console.anthropic.com/settings/keys",
        # Anthropic's OpenAI-compat layer does not reliably honour response_format;
        # rely on prompt + tolerant fence-stripping instead.
        "json_mode": False,
    },
    "openrouter": {
        "label": "OpenRouter (one-click sign-in)",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            "google/gemini-3.6-flash",
            "openai/gpt-5.6-terra",
            "anthropic/claude-sonnet-5",
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

# Built-in defaults (Claude Sonnet), used for a FRESH install — nothing saved, no legacy env key.
DEFAULT_PROVIDER = "anthropic"
DEFAULT_BASE_URL = PRESETS["anthropic"]["base_url"]
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_FAST_MODEL = "claude-haiku-4-5-20251001"

# Legacy default: before 1.1 an unset provider meant Gemini, and the README quick-start tells users to
# put GEMINI_API_KEY in .env WITHOUT ever saving a provider. Those installs must keep resolving to
# Gemini — flipping them to Anthropic would silently send a Google key to the wrong host.
LEGACY_PROVIDER = "gemini"
LEGACY_MODEL = "gemini-3.5-flash"

# Keys we persist in the SettingsModel KV table.
AI_SETTING_KEYS = (
    "ai_provider",
    "ai_base_url",
    "ai_api_key",
    "ai_model",
    "ai_model_fast",
    "ai_temperature",
)


# Last-failure record. Separate from AI_SETTING_KEYS: these are health, not config, and must never
# be echoed back into the settings form.
AI_HEALTH_KEYS = ("ai_last_error", "ai_last_error_at")

# Some providers echo the offending credential back in their error body. Never persist that — this
# record is read straight into the admin UI.
_KEYISH = __import__("re").compile(r"\b(?:sk-|AIza|ghp_|xox[baprs]-)[A-Za-z0-9_\-]{8,}\b")


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


# Anthropic removed the sampling parameters from Claude Opus 4.7 onward and across the 5-family:
# sending `temperature` to them is a hard 400, not a warning. The Temperature box in Admin → AI Engine
# is provider-agnostic, so a user who sets it and then picks a current Claude model would break every
# enrichment — and (before the health record above) would have been told nothing about why.
_NO_TEMPERATURE = __import__("re").compile(
    r"claude-(?:opus-(?:4-7|4-8|5)|sonnet-5|fable-5|mythos-5)", __import__("re").I
)


def rejects_temperature(model: str) -> bool:
    """True for models that 400 on `temperature` rather than ignoring it."""
    return bool(model and _NO_TEMPERATURE.search(model))


def _write_health(pairs: dict) -> None:
    """Upsert health rows on a PRIVATE session.

    Callers record a failure from inside an `except` that has already rolled their own session back;
    reusing it would either lose the write or resurrect the rolled-back work. Best-effort by design —
    failing to record why enrichment failed must never itself break a serve.
    """
    db = SessionLocal()
    try:
        for k, v in pairs.items():
            row = db.query(SettingsModel).filter(SettingsModel.setting_key == k).first()
            if row:
                row.setting_value = v
            else:
                db.add(SettingsModel(setting_key=k, setting_value=v))
        db.commit()
    except Exception as e:
        logger.error(f"[AI] could not record health state: {e}")
        db.rollback()
    finally:
        db.close()


def record_failure(detail: str) -> None:
    """Remember why the last model call failed, so the UI can say so instead of showing empty metadata.

    `has_key` only proves a key EXISTS. A key that is invalid, expired, revoked or over quota passes
    every configuration check and then fails at call time — which is exactly the case that used to
    produce an artwork with a null title and no explanation anywhere but the server log.
    """
    _write_health({
        "ai_last_error": _KEYISH.sub("<redacted>", str(detail))[:300],
        "ai_last_error_at": datetime.now(UTC).isoformat(),
    })


def clear_failure() -> None:
    """Called after a successful model call — the engine is demonstrably healthy again."""
    _write_health({"ai_last_error": "", "ai_last_error_at": ""})


def get_failure() -> dict:
    """{detail, at} for the last failure, or empty strings when the engine is healthy."""
    db = SessionLocal()
    try:
        rows = {
            r.setting_key: r.setting_value
            for r in db.query(SettingsModel).filter(SettingsModel.setting_key.in_(AI_HEALTH_KEYS)).all()
        }
        return {"detail": rows.get("ai_last_error") or "", "at": rows.get("ai_last_error_at") or ""}
    except Exception:
        return {"detail": "", "at": ""}
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

    env_key = os.getenv("GEMINI_API_KEY") or ""
    provider = s.get("ai_provider")
    legacy = False
    if not provider:
        # Unset provider: a legacy (pre-1.1) install if anything Gemini-era is present; else fresh.
        legacy = bool(env_key or s.get("ai_api_key") or s.get("ai_model"))
        provider = LEGACY_PROVIDER if legacy else DEFAULT_PROVIDER
    base_url = (s.get("ai_base_url") or "").rstrip("/")
    if not base_url:
        base_url = PRESETS.get(provider, {}).get("base_url", DEFAULT_BASE_URL).rstrip("/")
    # The env key is a GEMINI key — never hand it to any other provider's host.
    api_key = s.get("ai_api_key") or (env_key if provider == "gemini" else "")
    model = s.get("ai_model") or (LEGACY_MODEL if legacy else DEFAULT_MODEL)
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

    cap = PRESETS.get(cfg.get("provider"), {}).get("max_tokens")
    if cap:
        payload["max_tokens"] = cap

    t = temperature if temperature is not None else cfg.get("temperature")
    if t is not None and not rejects_temperature(model):
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
        # L4: the upstream body can carry request-identifying detail (or, previously, an unredacted
        # key — see the _KEYISH scrub above) — log it server-side only; callers get status + a generic
        # message, not up to 300 chars of someone else's response body.
        logger.warning(f"Model API error {resp.status_code}: {_KEYISH.sub('<redacted>', resp.text[:300])}")
        raise AIConfigError(f"Model API error {resp.status_code}: the model endpoint returned an error.")

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        logger.warning(f"Unexpected model response: {_KEYISH.sub('<redacted>', resp.text[:300])}")
        raise AIConfigError("Unexpected model response: the model endpoint returned an unreadable reply.") from e


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


# -----------------------------------------------------------------------------
# Runtime model lists (ADR-078 post-launch plan)
# -----------------------------------------------------------------------------
# Ask the provider what it serves, then filter HARD: an unfiltered list offers embeddings, TTS,
# image-generation and text-only models that fail at enrichment time. Where a provider exposes
# capability metadata (OpenRouter modalities, Anthropic capabilities) we use it; elsewhere an explicit
# per-provider allowlist pattern stands in. The key is only ever sent to the provider's own base_url,
# and redirects are NOT followed (a 3xx is an error). PRESETS stay the offline fallback.
MODELS_CACHE_PREFIX = "ai_models_cache_"   # + provider; deliberately NOT in AI_SETTING_KEYS
_MAX_FETCHED = 150

_re = __import__("re")
_NON_VISION_OR_GEN = _re.compile(
    r"embed|tts|whisper|audio|speech|transcri|realtime|moderation|dall-e|imagen|image|veo|sora|"
    r"-live|rerank|guard|aqa|robotics|computer-use|search-preview|instruct$|davinci|babbage|"
    r"gpt-3\.5|gpt-4-|codex",
    _re.I,
)
_DATED = _re.compile(r"(-\d{4}-\d{2}-\d{2}|-\d{8}|-\d{2}-\d{4}|-\d{3}|-\d{4})$")
_ALLOW = {
    "openai": _re.compile(r"^(gpt-(?:4o|4\.1|4\.5|[5-9])|o[3-9])", _re.I),
    "anthropic": _re.compile(r"^claude-(?:sonnet|opus|haiku|fable|mythos)-\d", _re.I),
    "gemini": _re.compile(r"^gemini-\d", _re.I),
    "ollama": _re.compile(
        r"llava|vision|vl(?:[:\-]|$)|minicpm-v|moondream|gemma3|gemma4|llama4|pixtral|mistral-small3", _re.I
    ),
}


def _collapse_dated(ids: list) -> list:
    """Drop `X-2025-10-01` style snapshots when the undated alias `X` is in the list."""
    have = set(ids)
    out = []
    for i in ids:
        m = _DATED.search(i)
        if m and i[: m.start()] in have:
            continue
        out.append(i)
    return out


def _filter_models(provider: str, payload) -> list:
    """Parse a provider's /models reply into a filtered, de-duplicated, sorted id list."""
    entries = None
    if isinstance(payload, dict):
        entries = payload.get("data") or payload.get("models")
    ids = []
    for e in entries or []:
        if not isinstance(e, dict) or not (e.get("id") or e.get("name")):
            continue
        mid = str(e.get("id") or e.get("name"))
        if mid.startswith("models/"):
            mid = mid[len("models/"):]
        if _NON_VISION_OR_GEN.search(mid):
            continue
        allow = _ALLOW.get(provider)
        if allow and not allow.search(mid):
            continue
        if provider == "openrouter":
            arch = e.get("architecture") or {}
            if "image" not in (arch.get("input_modalities") or []):
                continue
            if set(arch.get("output_modalities") or ["text"]) != {"text"}:
                continue            # image/audio GENERATION
            if e.get("expiration_date"):
                continue            # deprecated
        caps = e.get("capabilities")
        if provider == "anthropic" and isinstance(caps, dict):
            if not (caps.get("image_input") or {}).get("supported", True):
                continue
        if e.get("deprecated") is True:
            continue
        ids.append(mid)
    return _collapse_dated(sorted(set(ids)))[:_MAX_FETCHED]


def fetch_provider_models(provider: str, base_url: str, api_key: str, timeout: float = 15.0) -> list:
    """GET the provider's own model list and filter it. Raises AIConfigError on any failure.

    Never logs or echoes the key; error text is generic.
    """
    base = (base_url or "").rstrip("/")
    if not base:
        raise AIConfigError("A base URL is required to list models.")
    if not api_key and not PRESETS.get(provider, {}).get("key_optional"):
        raise AIConfigError("An API key is required to list models.")
    headers = {}
    if provider == "anthropic":
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    params = {"limit": 1000} if provider == "anthropic" else None
    try:
        resp = _http_client.get(
            f"{base}/models", headers=headers, params=params, timeout=timeout, follow_redirects=False
        )
    except httpx.HTTPError as e:
        raise AIConfigError(f"Could not reach the model endpoint: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise AIConfigError(f"The provider's model list returned HTTP {resp.status_code}.")
    try:
        models = _filter_models(provider, resp.json())
    except ValueError as e:
        raise AIConfigError("The provider's model list was unreadable.") from e
    if not models:
        raise AIConfigError("The provider's model list had no usable vision models.")
    return models
