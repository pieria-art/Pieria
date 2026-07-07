"""
Samsung Frame TV push output (Track: Integrations).

Renders the currently-selected artwork to a full-colour TV-resolution JPEG and pushes it into a
Samsung Frame TV's Art Mode over the LAN (Samsung's unofficial art WebSocket API via `samsungtvws`).
This is a third output target alongside the browser Canvas and the e-ink pull API.

Design note (no hardware here): the TV is hidden behind the small `FrameClient` interface so the only
hardware-dependent code is the thin `SamsungFrameClient` wrapper. Everything else — config, render,
push orchestration (dedupe / delete-old / persist), and the scheduler tick — is exercised in tests via
`FakeFrameClient` and an injected selector. See memory `no-frame-tv-for-testing`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

from database import SessionLocal
from epaper import render_fullcolor
from models import SettingsModel

logger = logging.getLogger("artwork-display-api.frame")

# Token persists the TV pairing (first connect triggers a TV-side authorize prompt) across restarts.
# ./data is the app's persisted volume (same place as the SQLite DB).
DEFAULT_TOKEN_FILE = Path("data") / "frame_tv_token.json"

FRAME_SETTING_KEYS = (
    "frame_enabled", "frame_host", "frame_port", "frame_playlist",
    "frame_interval_sec", "frame_width", "frame_height", "frame_matte", "frame_display_id",
    # internal state written by the pusher:
    "frame_last_content_id", "frame_last_artwork_id", "frame_last_push_at",
)

_DEFAULTS = {
    "frame_enabled": "false",
    "frame_host": "",
    "frame_port": "8001",
    "frame_playlist": "",
    "frame_interval_sec": "900",
    "frame_width": "3840",
    "frame_height": "2160",
    "frame_matte": "none",
    "frame_display_id": "frame-tv",
}

# select_fn(playlist_name) -> (image_path, artwork_id, focal) or None, where focal is the normalized
# (x, y) framing anchor. Injected by app.py so this module never imports app (avoids a circular
# import) and stays unit-testable.
SelectFn = Callable[[str], Awaitable[Optional[Tuple[Path, int, Tuple[float, float]]]]]

# --------------------------------------------------------------------------- config

_cache: dict = {"data": None, "ts": 0.0}
_CACHE_TTL = 30.0


def _read_rows() -> dict:
    db = SessionLocal()
    try:
        return {
            s.setting_key: s.setting_value
            for s in db.query(SettingsModel).filter(SettingsModel.setting_key.in_(FRAME_SETTING_KEYS)).all()
        }
    finally:
        db.close()


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def _int(v, fallback: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return fallback


def get_frame_config(force: bool = False) -> dict:
    """Resolve Frame settings (DB rows over defaults), 30s TTL cache like ai_client.get_ai_config."""
    now = time.monotonic()
    if not force and _cache["data"] is not None and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["data"]
    rows = _read_rows()

    def val(k):
        return rows.get(k, _DEFAULTS.get(k, ""))

    cfg = {
        "enabled": _truthy(val("frame_enabled")),
        "host": val("frame_host").strip(),
        "port": _int(val("frame_port"), 8001),
        "playlist": val("frame_playlist").strip(),
        "interval_sec": max(60, _int(val("frame_interval_sec"), 900)),
        "width": _int(val("frame_width"), 3840),
        "height": _int(val("frame_height"), 2160),
        "matte": (val("frame_matte").strip() or "none"),
        "display_id": (val("frame_display_id").strip() or "frame-tv"),
        "last_content_id": rows.get("frame_last_content_id") or None,
        "last_artwork_id": _int(rows.get("frame_last_artwork_id"), 0) or None,
        "last_push_at": float(rows.get("frame_last_push_at") or 0.0) or None,
    }
    _cache["data"] = cfg
    _cache["ts"] = now
    return cfg


def invalidate_frame_cache() -> None:
    _cache["data"] = None
    _cache["ts"] = 0.0


def _persist_state(content_id: str, artwork_id: int) -> None:
    """Record what we last pushed so the next cycle can dedupe + delete the prior upload."""
    db = SessionLocal()
    try:
        updates = {
            "frame_last_content_id": content_id,
            "frame_last_artwork_id": str(artwork_id),
            "frame_last_push_at": repr(time.time()),
        }
        for k, v in updates.items():
            row = db.query(SettingsModel).filter(SettingsModel.setting_key == k).first()
            if row:
                row.setting_value = v
            else:
                db.add(SettingsModel(setting_key=k, setting_value=v))
        db.commit()
    finally:
        db.close()
    invalidate_frame_cache()


# --------------------------------------------------------------------------- TV abstraction

class FrameClient(ABC):
    """Minimal interface over a Frame TV's Art Mode, so the pusher is hardware-agnostic + testable."""

    @abstractmethod
    async def push(self, image: bytes, file_type: str, matte: str) -> str:
        """Upload an image; return its content_id."""

    @abstractmethod
    async def show(self, content_id: str) -> None:
        """Select the uploaded image as the current art."""

    @abstractmethod
    async def ensure_artmode(self) -> None:
        """Make sure the TV is in Art Mode."""

    @abstractmethod
    async def delete(self, content_id: str) -> None:
        """Remove a previously-uploaded image (housekeeping)."""

    @abstractmethod
    async def test(self) -> dict:
        """Light connectivity check; raises on failure."""


class SamsungFrameClient(FrameClient):
    """Thin wrapper over the synchronous `samsungtvws` library, called via asyncio.to_thread.
    THE ONLY HARDWARE-DEPENDENT CODE. One connection is reused across the calls within a single push
    cycle; the scheduler builds a fresh client each tick (see _run_one_cycle), so there is no
    long-lived socket to reset. `tv_factory` is injectable purely so tests can verify the mapping
    without the real library or a TV."""

    def __init__(self, host: str, port: int = 8001, token_file: Optional[str] = None, tv_factory=None):
        self.host = host
        self.port = port
        self.token_file = token_file or str(DEFAULT_TOKEN_FILE)
        self._tv_factory = tv_factory or self._default_factory
        self._tv = None

    def _default_factory(self):
        from samsungtvws import SamsungTVWS  # lazy: optional dep, only needed when enabled
        DEFAULT_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        return SamsungTVWS(host=self.host, port=self.port, token_file=self.token_file)

    def _conn(self):
        if self._tv is None:
            self._tv = self._tv_factory()
        return self._tv

    async def push(self, image: bytes, file_type: str, matte: str) -> str:
        return await asyncio.to_thread(
            lambda: self._conn().art().upload(image, file_type=file_type, matte=matte)
        )

    async def show(self, content_id: str) -> None:
        await asyncio.to_thread(lambda: self._conn().art().select_image(content_id, show=True))

    async def ensure_artmode(self) -> None:
        await asyncio.to_thread(lambda: self._conn().art().set_artmode(True))

    async def delete(self, content_id: str) -> None:
        await asyncio.to_thread(lambda: self._conn().art().delete(content_id))

    async def test(self) -> dict:
        return await asyncio.to_thread(lambda: {"artmode": self._conn().art().get_artmode()})


class FakeFrameClient(FrameClient):
    """In-memory FrameClient for tests: records calls, returns deterministic content_ids."""

    def __init__(self, fail_on: Optional[str] = None):
        self.calls: list = []
        self.uploaded: list = []
        self.deleted: list = []
        self.artmode = False
        self._n = 0
        self._fail_on = fail_on  # method name to raise in, to exercise error paths

    def _maybe_fail(self, name):
        if self._fail_on == name:
            raise RuntimeError(f"simulated failure in {name}")

    async def push(self, image: bytes, file_type: str, matte: str) -> str:
        self._maybe_fail("push")
        self._n += 1
        cid = f"MY-F{self._n:04d}"
        self.calls.append(("push", file_type, matte, len(image)))
        self.uploaded.append(cid)
        return cid

    async def show(self, content_id: str) -> None:
        self._maybe_fail("show")
        self.calls.append(("show", content_id))

    async def ensure_artmode(self) -> None:
        self._maybe_fail("artmode")
        self.artmode = True
        self.calls.append(("artmode",))

    async def delete(self, content_id: str) -> None:
        self._maybe_fail("delete")
        self.deleted.append(content_id)
        self.calls.append(("delete", content_id))

    async def test(self) -> dict:
        self._maybe_fail("test")
        return {"ok": True}


def _default_client_factory(cfg: dict) -> FrameClient:
    return SamsungFrameClient(cfg["host"], cfg["port"], token_file=str(DEFAULT_TOKEN_FILE))


# --------------------------------------------------------------------------- orchestration

async def push_once(
    cfg: dict,
    select_fn: SelectFn,
    client: FrameClient,
    *,
    force: bool = False,
    persist: Optional[Callable[[str, int], None]] = None,
) -> dict:
    """Render + push the current artwork to the Frame. Returns a status dict; never raises for
    expected conditions (no artwork, unchanged). Caller decides how to surface client errors."""
    sel = await select_fn(cfg.get("playlist", ""))
    if not sel:
        return {"status": "skipped", "reason": "no artwork available"}
    path, artwork_id, focal = sel

    if not force and artwork_id == cfg.get("last_artwork_id") and cfg.get("last_content_id"):
        return {"status": "unchanged", "artwork_id": artwork_id}

    image = await asyncio.to_thread(
        render_fullcolor, Path(path), cfg["width"], cfg["height"], "cover", focal, 90
    )
    content_id = await client.push(image, "jpg", cfg.get("matte", "none"))
    await client.show(content_id)
    await client.ensure_artmode()

    old = cfg.get("last_content_id")
    if old and old != content_id:
        try:
            await client.delete(old)
        except Exception as e:  # housekeeping only — don't fail the push
            logger.warning(f"[Frame] could not delete prior upload {old}: {e}")

    (persist or _persist_state)(content_id, artwork_id)
    return {"status": "pushed", "content_id": content_id, "artwork_id": artwork_id, "bytes": len(image)}


async def run_test_push(select_fn: SelectFn, client_factory=_default_client_factory) -> dict:
    """One-shot 'Test / Push now' for the settings endpoint. Returns a structured result instead of
    raising, so the GUI can show a clean message with or without a TV present."""
    cfg = get_frame_config(force=True)
    if not cfg["host"]:
        return {"status": "error", "reason": "No Frame TV host/IP configured."}
    try:
        client = client_factory(cfg)
        return await push_once(cfg, select_fn, client, force=True)
    except Exception as e:
        logger.warning(f"[Frame] test push failed: {e}")
        return {"status": "error", "reason": str(e) or e.__class__.__name__}


async def _run_one_cycle(select_fn: SelectFn, client_factory=_default_client_factory) -> dict:
    """A single scheduler tick (factored out so it's testable without the infinite loop)."""
    cfg = get_frame_config(force=True)
    if not cfg["enabled"] or not cfg["host"]:
        return {"status": "idle"}
    client = client_factory(cfg)
    return await push_once(cfg, select_fn, client)


async def frame_push_loop(select_fn: SelectFn, client_factory=_default_client_factory) -> None:
    """Periodic pusher. Started once (leader worker only) from app.py's lifespan."""
    logger.info("[Frame] push loop started")
    while True:
        delay = 900
        try:
            delay = get_frame_config(force=True)["interval_sec"]
            res = await _run_one_cycle(select_fn, client_factory)
            if res.get("status") not in ("idle", "unchanged"):
                logger.info(f"[Frame] cycle: {res}")
        except Exception:
            logger.error(f"[Frame] loop error: {traceback.format_exc()}")
            delay = max(60, delay)
        await asyncio.sleep(delay)
