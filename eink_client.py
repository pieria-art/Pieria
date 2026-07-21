"""
E-ink device client (Track B) — the poll-and-sleep bridge that pulls the current image from
`GET /display/{id}/current.{ext}` and blits it to a Pimoroni Inky Impression 13.3" Spectra 6 panel.

Runs HOST-SIDE (not in the container) via `deploy/appliance/bin/sd-eink` — GPIO/SPI aren't reachable
from the non-root app container (ADR-037), and this client must also run standalone in "satellite" mode
with no local Screen Docent container at all. So this module is deliberately standalone: only stdlib +
Pillow + httpx + (lazily) `inky`. No `database`/app imports — see `.ai/spec_eink_spectra6.md` §3.2/3.4.

Design note (no hardware here): mirrors `frame_push.py`'s shape — an `EinkClient` ABC + a real
`InkyClient` + a `FakeInkyClient` + a testable single-tick (`push_once` / `run_tick`) + a loop
(`run_loop`). Same "test the platform without hardware" posture as the Frame TV integration
(memory `no-frame-tv-for-testing`), applied to e-ink.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import httpx
from PIL import Image

logger = logging.getLogger("sd-eink")

DEFAULT_HEARTBEAT_FILE = "/run/sd-eink.state"

# fetch_fn(url) -> (content_bytes, headers_dict). Injected so push_once never calls httpx directly.
FetchFn = Callable[[str], Tuple[bytes, dict]]
# schedule_fn() -> schedule-state JSON dict (has a "quiet" bool).
ScheduleFn = Callable[[], dict]


# --------------------------------------------------------------------------- config

@dataclass
class EinkConfig:
    server_url: str
    display_id: str
    min_interval: int = 900
    saturation: float = 0.5
    orientation: str = ""   # "" = landscape (1600x1200) | "portrait" = rotate 90 (1200x1600)
    w: int = 1600
    h: int = 1200
    palette: str = "spectra6"
    fit: str = "cover"

    @classmethod
    def from_env(cls) -> EinkConfig:
        return cls(
            server_url=os.environ.get("SERVER_URL", "http://localhost:8000").rstrip("/"),
            display_id=os.environ.get("DISPLAY_ID", "default"),
            min_interval=_int_env("EINK_MIN_INTERVAL", 900),
            saturation=_float_env("EINK_SATURATION", 0.5),
            orientation=os.environ.get("EINK_ORIENTATION", "").strip(),
        )

    @property
    def render_size(self) -> tuple[int, int]:
        """The w,h to ASK the server for — swapped when the panel hangs portrait.

        The panel's buffer is always landscape (w x h native); `orientation=portrait` means it is
        physically rotated 90 degrees, so what the viewer sees is a h x w portrait canvas. The art must
        therefore be COMPOSED at h x w and rotated back to the native buffer at paint time — not
        composed landscape and rotated, which is what happened before this: the server framed for a
        1600x1200 landscape window and the client handed the 1200x1600 result to a 1600x1200 panel,
        so a portrait frame got both the wrong composition and a buffer the panel couldn't take.
        """
        return (self.h, self.w) if self.orientation == "portrait" else (self.w, self.h)

    @property
    def pull_url(self) -> str:
        w, h = self.render_size
        return (
            f"{self.server_url}/display/{self.display_id}/current.png"
            f"?w={w}&h={h}&palette={self.palette}&fit={self.fit}"
        )

    @property
    def schedule_url(self) -> str:
        return f"{self.server_url}/api/displays/{self.display_id}/schedule-state"


def _int_env(key: str, fallback: int) -> int:
    try:
        return int(str(os.environ.get(key, fallback)).strip())
    except (TypeError, ValueError):
        return fallback


def _float_env(key: str, fallback: float) -> float:
    try:
        return float(str(os.environ.get(key, fallback)).strip())
    except (TypeError, ValueError):
        return fallback


# --------------------------------------------------------------------------- panel abstraction

class EinkClient(ABC):
    """Minimal interface over an e-ink panel, so the pusher is hardware-agnostic + testable."""

    resolution: Optional[Tuple[int, int]] = None

    @abstractmethod
    def show(self, image: Image.Image, saturation: float) -> None:
        """Blit an image to the panel (blocking; a full refresh is ~20-35s)."""


class InkyClient(EinkClient):
    """Thin wrapper over the Pimoroni `inky` library. THE ONLY HARDWARE-DEPENDENT CODE.
    `inky_factory` is injectable purely so tests can verify the mapping without the real library or a
    panel; the default factory does the `inky` import INSIDE itself so importing this module on a
    laptop with no `inky` installed never fails."""

    def __init__(self, inky_factory=None):
        self._inky_factory = inky_factory or self._default_factory
        self._inky = None

    @staticmethod
    def _default_factory():
        from inky.auto import auto  # lazy: optional dep, only needed on real hardware
        return auto()

    def _panel(self):
        if self._inky is None:
            self._inky = self._inky_factory()
        return self._inky

    @property
    def resolution(self):
        return self._panel().resolution

    def show(self, image: Image.Image, saturation: float) -> None:
        panel = self._panel()
        panel.set_image(image, saturation=saturation)
        panel.show()


class FakeInkyClient(EinkClient):
    """In-memory EinkClient for tests: records calls, optionally raises to exercise error paths."""

    def __init__(self, fail_on: Optional[str] = None, resolution: Tuple[int, int] = (1600, 1200)):
        self.calls: list = []
        self.shown: list = []
        self.resolution = resolution
        self._fail_on = fail_on

    def show(self, image: Image.Image, saturation: float) -> None:
        if self._fail_on == "show":
            raise RuntimeError("simulated failure in show")
        self.calls.append(("show", image.size, saturation))
        self.shown.append((image.size, saturation))


# --------------------------------------------------------------------------- HTTP (real fetch/schedule)

def make_http_fetch_fn(timeout: float = 15.0) -> FetchFn:
    """Real FetchFn: GET the pull URL, return (body bytes, headers dict)."""

    def _fetch(url: str) -> Tuple[bytes, dict]:
        r = httpx.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content, dict(r.headers)

    return _fetch


def make_http_schedule_fn(cfg: EinkConfig, timeout: float = 8.0) -> ScheduleFn:
    """Real ScheduleFn: GET the display's schedule-state JSON."""

    def _schedule() -> dict:
        r = httpx.get(cfg.schedule_url, timeout=timeout)
        r.raise_for_status()
        return r.json()

    return _schedule


# --------------------------------------------------------------------------- orchestration

def push_once(
    cfg: EinkConfig,
    fetch_fn: FetchFn,
    client: EinkClient,
    *,
    last_etag: Optional[str] = None,
) -> dict:
    """Pull the current image and paint it if it changed. Returns a status dict; never raises for
    expected conditions (unchanged). A fetch or client.show error propagates to the caller
    (run_tick handles it) — matches frame_push's contract."""
    content, headers = fetch_fn(cfg.pull_url)

    etag = headers.get("ETag") or headers.get("etag")
    if not etag:
        # Content-hash fallback so dedupe still works if the server ever omits the header.
        etag = "sha256:" + hashlib.sha256(content).hexdigest()[:16]

    if last_etag is not None and etag == last_etag:
        return {"status": "unchanged", "etag": etag}

    image = Image.open(io.BytesIO(content))
    image.load()
    if cfg.orientation == "portrait":
        # The server composed this at (h, w) portrait; rotate it back onto the panel's native
        # landscape buffer. Net effect: the panel gets its normal w x h, the viewer sees portrait art.
        image = image.rotate(90, expand=True)

    client.show(image, cfg.saturation)

    refresh_after = headers.get("X-Refresh-After") or headers.get("x-refresh-after")
    try:
        refresh_after = int(refresh_after)
    except (TypeError, ValueError):
        refresh_after = cfg.min_interval

    return {"status": "painted", "etag": etag, "refresh_after": refresh_after}


def _write_heartbeat() -> None:
    path = os.environ.get("EINK_HEARTBEAT_FILE", DEFAULT_HEARTBEAT_FILE)
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass  # e.g. no /run perms on a laptop/test box — non-fatal, watchdog-friendly only


def run_tick(
    cfg: EinkConfig,
    fetch_fn: FetchFn,
    client: EinkClient,
    schedule_fn: ScheduleFn,
    state: dict,
) -> dict:
    """A single scheduler tick (factored out so it's testable without the infinite loop). Never
    raises — errors are caught and reported in the returned status dict."""
    try:
        sched = schedule_fn()
    except Exception as e:
        logger.warning(f"[eink] schedule-state fetch failed, proceeding to paint: {e}")
        sched = {}

    if sched.get("quiet"):
        # E-ink holds its image at zero power — night/quiet means STOP refreshing, not blank.
        return {"status": "quiet", "sleep": cfg.min_interval}

    try:
        res = push_once(cfg, fetch_fn, client, last_etag=state.get("last_etag"))
    except Exception as e:
        logger.error(f"[eink] tick failed: {e}")
        return {"status": "error", "reason": str(e) or e.__class__.__name__,
                "sleep": max(60, cfg.min_interval)}

    res = dict(res)
    if res["status"] == "painted":
        state["last_etag"] = res["etag"]
        _write_heartbeat()
        res["sleep"] = max(res.get("refresh_after", cfg.min_interval), cfg.min_interval)
    else:  # unchanged
        res["sleep"] = cfg.min_interval
    return res


def run_loop(
    cfg: EinkConfig,
    fetch_fn: Optional[FetchFn] = None,
    client: Optional[EinkClient] = None,
    schedule_fn: Optional[ScheduleFn] = None,
) -> None:
    """Periodic pull-and-paint loop. Builds real httpx fetch/schedule fns and an InkyClient unless
    injected (tests / --dry-run inject fakes)."""
    fetch_fn = fetch_fn or make_http_fetch_fn()
    schedule_fn = schedule_fn or make_http_schedule_fn(cfg)
    client = client or InkyClient()

    logger.info(f"[eink] loop started (display={cfg.display_id}, server={cfg.server_url})")
    state: dict = {}
    while True:
        res = run_tick(cfg, fetch_fn, client, schedule_fn, state)
        if res.get("status") not in ("unchanged", "quiet"):
            logger.info(f"[eink] tick: {res}")
        time.sleep(max(1, res.get("sleep", cfg.min_interval)))
