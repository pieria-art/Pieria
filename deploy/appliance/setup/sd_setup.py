#!/usr/bin/env python3
"""Pieria — first-run setup wizard (R1-F1).

A tiny, dependency-free (Python stdlib only) web wizard that collects Wi-Fi + server/display config on
first boot and writes the appliance `pieria.conf`, so a non-technical user never touches SSH or
hand-edits a file. On a freshly flashed Pi it runs behind a `Pieria-Setup` Wi-Fi hotspot + captive
portal (see sd-setup-boot); here it is just the HTTP brain.

Two modes:
  * live      — writes the real boot-partition conf, joins Wi-Fi (nmcli), tears down the AP, reboots.
  * --dry-run — writes a PREVIEW conf to a temp dir and shows the exact bytes it *would* write; never
                touches the real conf, Wi-Fi, or reboots. Safe to run on a working Pi in-situ:
                    python3 sd_setup.py --dry-run --port 8080
                then open http://<pi>:8080 from a phone/laptop on the same network.

The wizard NEVER touches Artwork/ or the database — it only ever writes pieria.conf.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- pure config logic (unit-tested) ------------------------------------------

# orientation choice -> (ROTATE value, human label). Landscape leaves ROTATE blank (the compositor
# default); 90/270 are the two portrait mounts; 180 flips a landscape panel.
ORIENTATIONS = {
    "landscape": ("", "Landscape"),
    "90": ("90", "Portrait (rotated 90°)"),
    "270": ("270", "Portrait (rotated 270°)"),
    "180": ("180", "Upside-down (180°)"),
}

_SERVER_URL_RE = re.compile(r"^https?://[^\s/]+(?::\d+)?(?:/.*)?$")
_DISPLAY_ID_RE = re.compile(r"[^a-z0-9_-]+")
_HOSTNAME_STRIP_RE = re.compile(r"[^a-z0-9-]+")
_HOSTNAME_VALID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def sanitize_display_id(raw: str) -> str:
    """Lowercase, collapse anything that isn't [a-z0-9_-] to a single underscore, and trim leading/
    trailing separators so an id never starts or ends with a stray '-' or '_'."""
    return _DISPLAY_ID_RE.sub("_", (raw or "").strip().lower()).strip("_-")


def derive_hostname(raw: str) -> str:
    """Turn a human display name into an RFC1123-safe hostname: lowercase, underscores→hyphens,
    anything else dropped, collapsed and trimmed to a valid DNS label (≤63 chars, no leading/trailing
    hyphen). "Living Room" → "living-room" → it answers at living-room.local.

    Returns "" when nothing usable survives, so the caller falls back to the unique baked default
    (pieria-XXXX) rather than shipping an invalid name."""
    s = _HOSTNAME_STRIP_RE.sub("-", (raw or "").strip().lower().replace("_", "-"))
    s = re.sub(r"-+", "-", s).strip("-")[:63].rstrip("-")
    return s if _HOSTNAME_VALID_RE.match(s) else ""


def valid_hostname(raw: str) -> bool:
    """Is `raw` already a valid single DNS label (what /etc/hostname wants)?"""
    return bool(_HOSTNAME_VALID_RE.match((raw or "").strip()))


def validate_fields(fields: dict) -> dict:
    """Return {field: error_message} for anything invalid — empty dict means the form is good.

    Wi-Fi is optional (a wired / already-connected box needs none); if an SSID is given the rest is
    accepted as-is (open networks have no password). SERVER_URL + DISPLAY_ID + orientation are required.
    """
    errors = {}
    url = (fields.get("server_url") or "").strip()
    if not url:
        errors["server_url"] = "Enter your server address (e.g. http://localhost:8000)."
    elif not _SERVER_URL_RE.match(url):
        errors["server_url"] = "Must look like http://host:port (e.g. http://192.168.1.50:8000)."

    if not sanitize_display_id(fields.get("display_id", "")):
        errors["display_id"] = "Give this display a name (letters, numbers, - or _)."

    if fields.get("orientation") not in ORIENTATIONS:
        errors["orientation"] = "Choose an orientation."

    # Timezone is OPTIONAL and normally auto-filled from the phone; only a non-blank value is checked.
    tz = (fields.get("timezone") or "").strip()
    if tz and not valid_timezone(tz):
        errors["timezone"] = "Use a zone name like America/Chicago or Europe/London."

    # Hostname is OPTIONAL: blank means "derive from the display name," and if that yields nothing the
    # box keeps its unique baked default. Only a non-empty, explicitly-typed value is validated — so an
    # advanced user who opens the edit affordance and types garbage gets told, while gramps who never
    # touches it is never bothered.
    hn = (fields.get("hostname") or "").strip()
    if hn and not valid_hostname(hn):
        errors["hostname"] = "Letters, numbers and hyphens only; can't start or end with a hyphen."

    return errors


def resolve_hostname(fields: dict) -> str:
    """The hostname this box should take: an explicit valid entry wins, else derive it from the display
    name, else "" (the caller leaves the unique baked default in place)."""
    explicit = (fields.get("hostname") or "").strip()
    if valid_hostname(explicit):
        return explicit
    return derive_hostname(fields.get("display_id", ""))


def _pick_all_in_one(fields: dict, default: bool) -> bool:
    """The wizard now ASKS whether this box runs the server. Fall back to the CLI/--all-in-one default
    when the field is absent (older clients, or a caller that already knows). Without this the flagship
    all-in-one .img could never write ALL_IN_ONE=1: the value came only from a CLI flag that
    sd-setup-boot does not pass."""
    raw = fields.get("all_in_one")
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


#: Keys the wizard OWNS — everything else in an existing conf is preserved verbatim.
_WIZARD_KEYS = {"SERVER_URL", "DISPLAY_ID", "MODE", "CYCLE_TIME", "ROTATE", "OUTPUT",
                "WAIT_TIMEOUT", "ALL_IN_ONE", "GEMINI_API_KEY", "EINK_ORIENTATION", "HOSTNAME",
                "TIMEZONE"}

#: The SAME injection control as sd-conf.SAFE_VALUE_RE (deliberately duplicated, not imported —
#: sd-conf is a separately-installed script and this file must stand alone). pieria.conf is
#: `.`-sourced as shell by several scripts, so an unvalidated preserved VALUE is not a bad setting,
#: it is code. No spaces, quotes, `$`, backticks, `;`, or newlines survive this as anything but a
#: literal (finding N7-Info, 2026-09-22).
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:+@,-]*$")
_PRESERVED_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: IANA zone names: Area/Location[/Sub], e.g. America/Chicago, America/Argentina/Buenos_Aires; also
#: bare "UTC". Shape check only — existence is verified against the OS zoneinfo where present.
_TIMEZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$")


def valid_timezone(tz: str) -> bool:
    """True for a zone name this box can set. The phone supplies it (Intl.DateTimeFormat), so garbage is
    rare — but the field is editable. When the OS zoneinfo is available the name must exist in it;
    without zoneinfo (a stripped test env) the shape check stands alone."""
    tz = (tz or "").strip()
    if not tz or len(tz) > 64 or not _TIMEZONE_RE.match(tz):
        return False
    try:
        from zoneinfo import available_timezones
        zones = available_timezones()
    except Exception:  # noqa: BLE001 — no tzdata → shape check only
        return True
    return tz in zones if zones else True


def resolve_timezone(fields: dict) -> str:
    """The TIMEZONE= value to write: the form's zone if valid, else blank (= leave the OS clock alone).
    Blank is deliberate for garbage — a wrong zone is worse than the default the user can still fix."""
    tz = (fields.get("timezone") or "").strip()
    return tz if valid_timezone(tz) else ""


def _preserved_lines(existing: str) -> list:
    """Settings from an existing conf that the wizard must NOT clobber.

    The wizard emits a fixed key set, so a re-run silently DELETED everything else — EINK_ENABLED,
    EINK_MIN_INTERVAL, WATCHDOG. An e-ink box that went through setup (or the ADR-057
    recovery wizard) came back with its panel unconfigured and its watchdog reset, with nothing to
    indicate why. Found before it could bite on the bench, 2026-07-21.
    """
    out = []
    for raw in (existing or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key or key in _WIZARD_KEYS:
            continue
        if not _PRESERVED_KEY_RE.match(key) or not _SAFE_VALUE_RE.match(value):
            # Only reachable by someone who can already write the FAT boot partition (ADR-011
            # physical-access risk) — but SAFE_VALUE_RE is the load-bearing control on a file that's
            # `.`-sourced as shell, so a foreign line must clear it too, not ride through verbatim.
            print(f"sd-setup: dropping unsafe preserved conf line for key {key!r} "
                  "(value did not pass SAFE_VALUE_RE)", file=sys.stderr)
            continue
        out.append(f"{key}={value}")
    return out


def build_conf(fields: dict, all_in_one: bool = False, existing: str = "") -> str:
    """Render the pieria.conf text from validated wizard fields.

    Only kiosk variables land here (SERVER_URL/DISPLAY_ID/ROTATE/OUTPUT/…). Wi-Fi credentials are NOT
    written to this FAT boot file — they go to NetworkManager via nmcli at commit time. GEMINI_API_KEY
    is left blank (a separate, optional step for all-in-one AI). Any OTHER key already present in
    `existing` is carried through untouched — see _preserved_lines.
    """
    rotate = ORIENTATIONS.get(fields.get("orientation", "landscape"), ("", ""))[0]
    display_id = sanitize_display_id(fields.get("display_id", "")) or "display"
    server_url = (fields.get("server_url") or "").strip() or "http://localhost:8000"
    output = (fields.get("output") or "HDMI-A-1").strip()
    hostname = resolve_hostname(fields)
    return (
        "# Pieria — Appliance configuration\n"
        "# Written by the first-run setup wizard. Safe to edit on the SD card's boot partition.\n"
        f"SERVER_URL={server_url}\n"
        f"DISPLAY_ID={display_id}\n"
        # The network name this box answers to (<HOSTNAME>.local). Applied at commit via hostnamectl;
        # recorded here so it survives a conf edit and the value is visible. Blank = keep the unique
        # baked default (pieria-XXXX).
        f"HOSTNAME={hostname}\n"
        # The house clock (IANA name). Night & Quiet Hours + CEC panel power follow it; applied at every
        # boot by sd-timesync-wait and at commit. Blank = leave the OS timezone alone.
        f"TIMEZONE={resolve_timezone(fields)}\n"
        "MODE=\n"
        "CYCLE_TIME=\n"
        f"ROTATE={rotate}\n"
        f"OUTPUT={output}\n"
        "WAIT_TIMEOUT=0\n"
        f"ALL_IN_ONE={'1' if _pick_all_in_one(fields, all_in_one) else '0'}\n"
        # The chosen orientation must reach BOTH surfaces. ROTATE drives wlroots/HDMI; the e-ink client
        # reads its own EINK_ORIENTATION and would otherwise stay landscape on a panel the user just
        # told us is portrait. Only 90/270 are portrait mounts — 180 is still a landscape panel.
        f"EINK_ORIENTATION={'portrait' if rotate in ('90', '270') else ''}\n"
        "GEMINI_API_KEY=\n"
        + ("".join(f"{line}\n" for line in _preserved_lines(existing)))
    )


#: Written by sd-setup-boot while wlan0 is still in STATION mode. Once hostapd owns the radio a scan is
#: impossible, so the list must be captured before the AP goes up and served from here.
SCAN_CACHE = Path("/run/sd-setup/networks.json")


def _scanned_networks() -> list:
    """Cached nearby networks, best signal first. Never raises: an unreadable or absent cache simply
    means the wizard falls back to a free-text SSID field."""
    try:
        data = json.loads(SCAN_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    best: dict = {}
    for n in data if isinstance(data, list) else []:
        ssid = (n.get("ssid") or "").strip()
        if not ssid:
            continue  # hidden/blank SSIDs can't be picked from a list
        signal = n.get("signal") or 0
        # A mesh advertises the same SSID once per radio/band. Keep the STRONGEST rather than whichever
        # came first: relying on nmcli's ordering would silently show a weak entry if that ever changed.
        if ssid not in best or signal > best[ssid]["signal"]:
            best[ssid] = {"ssid": ssid, "signal": signal, "secure": bool(n.get("secure"))}
    return sorted(best.values(), key=lambda n: -n["signal"])


def _read_existing(path: Path) -> str:
    """Current conf text, or empty on a first-ever boot. Never raises."""
    try:
        return path.read_text()
    except OSError:
        return ""


def resolve_boot_conf_path() -> Path:
    """Where the real conf lives — Bookworm moved it from /boot to /boot/firmware."""
    firmware = Path("/boot/firmware")
    try:
        return (firmware if firmware.is_dir() else Path("/boot")) / "pieria.conf"
    except OSError:   # F5: is_dir() raises on EACCES rather than returning False
        return Path("/boot") / "pieria.conf"


# --- HTTP server --------------------------------------------------------------

# OS connectivity-check URLs. A captive portal answers these with a redirect so the "Sign in to
# network" sheet pops on iOS/Android/Windows. (Only reachable via the AP's DNS catch-all on a real
# first boot; harmless in dry-run.)
_CAPTIVE_PROBES = {
    "/generate_204", "/gen_204", "/hotspot-detect.html", "/library/test/success.html",
    "/ncsi.txt", "/connecttest.txt", "/redirect", "/canonical.html", "/success.txt",
}

#: Served instead of the wizard when no setup PIN could be issued for this run (PIN_FILE missing or
#: malformed). Fail CLOSED rather than silently running the wizard with no PIN gate at all.
PIN_UNAVAILABLE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Setup unavailable</title>
<style>body{background:#0f172a;color:#f1f5f9;font-family:-apple-system,sans-serif;
padding:40px 20px;max-width:440px;margin:0 auto;}
h1{font-size:1.3rem;}</style></head>
<body><h1>Setup is temporarily unavailable</h1>
<p>This display couldn't generate a setup PIN, so the setup wizard can't run safely right now.</p>
<p>Please restart the display and reconnect to <b>Pieria-Setup</b>.</p>
</body></html>"""


#: Written by common.sh's sd_generate_setup_pin BEFORE sd-setup-card paints the splash/e-ink card and
#: BEFORE this wizard is launched — on both first boot (sd-setup-boot) and every recovery re-open
#: (sd-net-recover). /run is tmpfs: the PIN dies with the boot, never touches the SD card, and this
#: process (running as root) is the only reader (finding N7, 2026-09-22).
PIN_FILE = Path("/run/pieria-setup-pin")


def _read_pin() -> str | None:
    """The setup PIN generated for this run, or None on any read/shape problem. None means the caller
    FAILS CLOSED — serves an error page and refuses every PIN-gated request — rather than silently
    accepting them with no PIN check. Every launch path must create this file before it starts the
    wizard; a missing/malformed file is treated exactly like "no PIN issued"."""
    try:
        raw = PIN_FILE.read_text().strip()
    except OSError:
        return None
    return raw if re.fullmatch(r"[0-9]{6}", raw) else None


def _format_pin(pin: str) -> str:
    """"123456" -> "123 456" — easier to read off a screen and read back aloud."""
    return f"{pin[:3]} {pin[3:]}" if len(pin) == 6 else pin


class SetupConfig:
    """Runtime knobs shared with the request handler."""
    def __init__(self, dry_run: bool, all_in_one: bool, boot_conf: Path, output: str,
                 recovery: str = "", pin: str | None = None):
        self.dry_run = dry_run
        self.all_in_one = all_in_one
        self.boot_conf = boot_conf
        self.output = output
        # Non-empty when sd-net-recover re-opened the wizard because a CONFIGURED box could not get
        # online (usually a mistyped Wi-Fi password). Shown as a banner so the user understands this is
        # a second attempt, not a fresh setup — otherwise the box silently looks like it reset itself.
        self.recovery = recovery
        self.preview_path = Path("/tmp/sd-setup-preview/pieria.conf")
        self._revert_timer: threading.Timer | None = None
        # None (the default) means "read PIN_FILE now" — the production path. Tests pass an explicit
        # value so they don't depend on /run. None here (missing/unreadable file) is the fail-closed
        # state: every PIN-gated endpoint then refuses rather than accepting with no check.
        self.pin = pin if pin is not None else _read_pin()
        self._pin_fail_count = 0
        self._pin_lock_until = 0.0
        self._pin_lock = threading.Lock()

    def check_pin(self, candidate) -> tuple[bool, int, str]:
        """Constant-time PIN check shared by every PIN-gated endpoint (commit, network scan), with a
        5-attempt / 60s lockout shared across them too — a 6-digit PIN over an open AP must not be
        brute-forceable within the AP's own lifetime. Returns (ok, http_status, message)."""
        if self.pin is None:
            return False, 503, "Setup is unavailable — restart the display to get a new setup PIN."
        with self._pin_lock:
            now = time.monotonic()
            if now < self._pin_lock_until:
                remaining = int(self._pin_lock_until - now) + 1
                return False, 429, f"Too many attempts — try again in {remaining}s."
            ok = bool(candidate) and secrets.compare_digest(str(candidate), self.pin)
            if ok:
                self._pin_fail_count = 0
                return True, 200, ""
            self._pin_fail_count += 1
            if self._pin_fail_count >= 5:
                self._pin_lock_until = now + 60
                self._pin_fail_count = 0
                return False, 429, "Too many attempts — try again in 60s."
            return False, 401, "Incorrect PIN — check the display and try again."


def _preview_on_eink(orientation: str) -> bool:
    """Repaint the e-ink setup card in `orientation`, in the background. Returns whether we launched it.

    Best-effort and fire-and-forget: a full Spectra 6 refresh is ~9s and the HTTP response must not wait
    on it. Absent panel / absent script simply means False and the caller falls back to its old message.
    """
    card = shutil.which("sd-setup-card") or "/usr/local/bin/sd-setup-card"
    if not Path(card).exists():
        return False
    try:
        subprocess.Popen([card, "--ssid", "Pieria-Setup", "--orientation", orientation],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def _kiosk_wayland_env() -> dict | None:
    """Locate the kiosk compositor's Wayland socket so root can drive it.

    The wizard runs as ROOT from sd-setup.service, which has no Wayland session of its own — so
    `WAYLAND_DISPLAY` is unset by definition, and the live-rotate path below could never run in
    production. It only ever worked when someone ran the wizard by hand from inside a session, which is
    why the HDMI rotation preview silently did nothing on a real box (found 2026-07-22, Run 2).

    The compositor that owns the display is `cage`, running as the kiosk user, and its socket lives in
    that user's XDG runtime dir. Root can open it — it just has to be told where it is."""
    for sock in sorted(glob.glob("/run/user/*/wayland-*")):
        if sock.endswith(".lock"):
            continue
        return {"XDG_RUNTIME_DIR": os.path.dirname(sock),
                "WAYLAND_DISPLAY": os.path.basename(sock)}
    return None


def _enabled_outputs(env: dict) -> list:
    """Names of the outputs the compositor currently has enabled, in wlr-randr's own order."""
    try:
        out = subprocess.run(["wlr-randr"], capture_output=True, timeout=10,
                             env=env, text=True).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    names, current = [], None
    for line in out.splitlines():
        if line[:1].isalnum():
            current = line.split()[0]
        elif "Enabled: yes" in line and current:
            names.append(current)
    return names


def _resolve_output(output: str, env: dict) -> str:
    """Map a configured output name onto one that actually exists.

    A Pi 5 has two HDMI sockets and the shipped default is HDMI-A-1, so anyone who used the other one
    had every rotation target a non-existent output: wlr-randr fails and nothing rotates, silently.
    Seen on the bench 2026-07-22, where the monitor enumerated as HDMI-A-2 while the conf said
    HDMI-A-1. Which physical socket someone used is not something they should have to tell us."""
    have = _enabled_outputs(env)
    if not have or output in have:
        return output
    return have[0]


def _apply_rotation(output: str, orientation: str, revert_after: int, cfg: SetupConfig) -> dict:
    """Best-effort live rotate via wlr-randr (opt-in on the Pi), with an auto-revert so a wrong pick on
    a keyboard-less wall mount can't strand the display. Returns a status dict for the UI.

    Live rotation only works inside the wlroots kiosk session (Pi / all-in-one). Anywhere else — a dev
    laptop, or the wizard running before the kiosk starts — wlr-randr isn't present (or there's no
    Wayland session), so we record the choice and report it plainly instead of erroring."""
    transform = {"landscape": "normal", "90": "90", "180": "180", "270": "270"}.get(orientation, "normal")

    # E-ink first: wlr-randr only ever drove wlroots/HDMI, so on an e-ink box the preview button did
    # literally nothing (found mid-test, 2026-07-21). Repainting the setup card in the chosen
    # orientation IS the preview for that surface — the user watches the panel turn.
    eink = _preview_on_eink(orientation)

    # Prefer OUR OWN session if we somehow have one (hand-run wizard), else borrow the kiosk's.
    wl_env = ({"XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", ""),
               "WAYLAND_DISPLAY": os.environ["WAYLAND_DISPLAY"]}
              if os.environ.get("WAYLAND_DISPLAY") else _kiosk_wayland_env())

    if not shutil.which("wlr-randr") or wl_env is None:
        if eink:
            return {"mode": "eink", "message": "Repainting the e-ink panel in that orientation — "
                                               "it takes about 10 seconds."}
        return {"mode": "unavailable",
                "message": "Live preview runs on the display itself (the Pi kiosk). Your choice is "
                           "recorded and written to the config."}
    env = {**os.environ, **wl_env}
    output = _resolve_output(output, env)
    try:
        subprocess.run(["wlr-randr", "--output", output, "--transform", transform],
                       check=True, capture_output=True, timeout=10, env=env)
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        return {"mode": "error", "message": f"Could not rotate: {e}"}

    # Cancel any prior pending revert, then arm a new one.
    if cfg._revert_timer:
        cfg._revert_timer.cancel()

    def _revert():
        # Same borrowed session as the apply above — a revert that quietly fails would strand a
        # wall-mounted display in a wrong orientation, which is the exact thing the timer exists for.
        try:
            subprocess.run(["wlr-randr", "--output", output, "--transform", "normal"],
                           check=False, capture_output=True, timeout=10, env=env)
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

    cfg._revert_timer = threading.Timer(revert_after, _revert)
    cfg._revert_timer.daemon = True
    cfg._revert_timer.start()
    return {"mode": "applied", "revert_in": revert_after,
            "message": f"Applied — reverts in {revert_after}s unless you keep it."}


def make_handler(cfg: SetupConfig):
    class Handler(BaseHTTPRequestHandler):
        # Terse one-line request log. This used to be a silent `pass`, which meant a wizard served over
        # the captive portal left NO evidence it had been reached at all — the same blindness that made
        # the AP bug undiagnosable (ADR-056). Keep it quiet, but not invisible.
        def log_message(self, fmt, *args):  # noqa: D401
            # Defensive redaction: the PIN must never travel in a query string any more (it's a header
            # now, see /api/networks below), but a stray future caller or proxy log line could still put
            # `pin=` on the request line — scrub it here so the PIN never reaches the journal either way.
            line = re.sub(r"pin=[^&\s\"]*", "pin=REDACTED", fmt % args)
            sys.stderr.write(f"sd-setup: {self.address_string()} {line}\n")

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj))

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return {}

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                if cfg.pin is None:
                    self._send(503, PIN_UNAVAILABLE_HTML, "text/html; charset=utf-8")
                else:
                    self._send(200, WIZARD_HTML, "text/html; charset=utf-8")
            elif path == "/api/networks":
                # The scan leaks neighbouring SSIDs to anyone in radio range of the open AP — gate it
                # behind the same PIN as commit (N7). A phone that hasn't read the PIN off the screen
                # yet simply falls back to typing the SSID by hand; nothing else in the wizard needs it.
                # The PIN travels as a request HEADER, never a query string: a query string lands in
                # access logs and browser history verbatim (finding N7-1); a header does not.
                pin = self.headers.get("X-Setup-PIN", "")
                ok, status, err = cfg.check_pin(pin)
                if not ok:
                    self._json(status, {"error": err})
                    return
                self._json(200, {"networks": _scanned_networks()})
            elif path == "/api/mode":
                self._json(200, {"dry_run": cfg.dry_run, "all_in_one": cfg.all_in_one,
                                 "boot_conf": str(cfg.boot_conf), "recovery": cfg.recovery})
            elif path in _CAPTIVE_PROBES:
                # Trigger the OS captive-portal sheet: redirect the probe to our wizard.
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
            else:
                # DNS catch-all sends every host here; unknown paths bounce to the wizard.
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            body = self._body()
            if path == "/api/validate":
                errors = validate_fields(body)
                out = {"errors": errors}
                if not errors:
                    out["conf"] = build_conf(body, cfg.all_in_one, _read_existing(cfg.boot_conf))
                self._json(200 if not errors else 422, out)
            elif path == "/api/orientation":
                # Unauthenticated, this let a stranger in radio range of the open AP rotate someone's
                # screen sight-unseen (finding N7-2) — gate it behind the same PIN + shared lockout as
                # everything else. PIN travels in the POST body, same as /api/commit.
                ok, status, err = cfg.check_pin(body.get("pin"))
                if not ok:
                    self._json(status, {"error": err, "pin_required": True})
                    return
                try:
                    revert_after = int(body.get("revert_after", 30))
                except (TypeError, ValueError):
                    self._json(422, {"error": "revert_after must be a number of seconds."})
                    return
                # Clamp to what the UI actually offers (5..120s) — an unbounded value let a caller arm a
                # revert timer that fires instantly or never, either of which can strand the display.
                if not 5 <= revert_after <= 120:
                    self._json(422, {"error": "revert_after must be between 5 and 120 seconds."})
                    return
                self._json(200, _apply_rotation(cfg.output, body.get("orientation", "landscape"),
                                                revert_after, cfg))
            elif path == "/api/orientation/keep":
                if cfg._revert_timer:
                    cfg._revert_timer.cancel()
                self._json(200, {"kept": True})
            elif path == "/api/commit":
                self._commit(body)
            else:
                self._json(404, {"error": "not found"})

        def _commit(self, fields):
            errors = validate_fields(fields)
            if errors:
                self._json(422, {"errors": errors})
                return
            # PIN gate (N7): checked AFTER field validation (an obviously malformed form is rejected
            # without spending a PIN attempt) but BEFORE anything is written — wrong PIN means nothing
            # on disk changes, ever. Constant-time compare + shared lockout live in check_pin().
            ok, status, err = cfg.check_pin(fields.get("pin"))
            if not ok:
                self._json(status, {"error": err, "pin_required": True})
                return
            conf = build_conf(fields, cfg.all_in_one, _read_existing(cfg.boot_conf))
            ssid = (fields.get("wifi_ssid") or "").strip()

            if cfg.dry_run:
                cfg.preview_path.parent.mkdir(parents=True, exist_ok=True)
                cfg.preview_path.write_text(conf)
                self._json(200, {
                    "dry_run": True,
                    "conf": conf,
                    "preview_path": str(cfg.preview_path),
                    "would_write_to": str(cfg.boot_conf),
                    "would_join_wifi": ssid or None,
                    "would_reboot": True,
                    "message": "Dry run — nothing on this device was changed. Above is exactly what a "
                               "real first boot would write.",
                })
                return

            # --- live path (Pi-gated; runs on a real first boot) ---
            try:
                cfg.boot_conf.write_text(conf)
                # World-readable by design (FAT boot partition, read from any computer). Set it
                # explicitly so the mode is deterministic rather than inherited from the process umask.
                os.chmod(cfg.boot_conf, 0o644)
                _apply_hostname(resolve_hostname(fields))
                _apply_timezone(resolve_timezone(fields))
                if ssid:
                    _join_wifi(ssid, fields.get("wifi_pass", ""))
                _release_wlan0()
                _schedule_reboot()
                self._json(200, {"committed": True, "wrote_to": str(cfg.boot_conf),
                                 "joined_wifi": ssid or None, "rebooting": True,
                                 "message": "Saved. The display will restart into your gallery now."})
            except Exception as e:  # noqa: BLE001 — surface any commit failure to the user
                self._json(500, {"error": f"Commit failed: {e}"})

    return Handler


def _apply_hostname(hostname: str) -> None:
    """Set the box's network name at commit so it answers at <hostname>.local after the reboot.

    Best-effort and non-fatal by design: a hostname failure must never block a commit that already
    wrote the conf and joined Wi-Fi — the box would still work, just under its baked default name. Live-
    mode only (the caller skips this in dry-run). Blank hostname → leave the unique baked default alone.

    Updates the 127.0.1.1 line in /etc/hosts too: without it `sudo` warns 'unable to resolve host' and
    avahi can advertise a stale name. hostnamectl handles /etc/hostname; /etc/hosts it does not."""
    if not hostname or not valid_hostname(hostname):
        return
    try:
        subprocess.run(["hostnamectl", "set-hostname", hostname],
                       check=True, capture_output=True, timeout=15)
    except (subprocess.SubprocessError, FileNotFoundError):
        try:
            Path("/etc/hostname").write_text(hostname + "\n")
        except OSError:
            return  # can't set it at all — box keeps its current name, harmless
    try:
        hosts = Path("/etc/hosts")
        lines = hosts.read_text().splitlines()
        out, seen = [], False
        for ln in lines:
            if ln.split("#", 1)[0].strip().startswith("127.0.1.1"):
                out.append(f"127.0.1.1\t{hostname}")
                seen = True
            else:
                out.append(ln)
        if not seen:
            out.append(f"127.0.1.1\t{hostname}")
        hosts.write_text("\n".join(out) + "\n")
    except OSError:
        pass  # /etc/hostname is the one that actually matters; hosts is a courtesy


def _apply_timezone(tz: str) -> None:
    """Set the OS timezone at commit (best-effort, non-fatal — same contract as _apply_hostname). Blank
    = leave the OS default. sd-timesync-wait re-applies TIMEZONE= at every boot, so a failure here
    self-heals on the reboot that follows the commit."""
    if not tz or not valid_timezone(tz):
        return
    try:
        subprocess.run(["timedatectl", "set-timezone", tz], check=True, capture_output=True, timeout=15)
    except (subprocess.SubprocessError, FileNotFoundError):
        pass  # boot-time apply will retry; the conf already carries the value


def _join_wifi(ssid: str, password: str) -> None:
    """Persist the chosen Wi-Fi to NetworkManager so the post-commit reboot auto-joins it. We SAVE the
    profile (autoconnect) rather than activate it now: wlan0 is currently held by the setup AP (hostapd,
    NM-unmanaged), and activating would tear the AP down mid-commit — killing the phone's connection
    before it ever sees the success page. Leaving setup mode on reboot lets NM auto-connect the saved
    profile. Live-mode only. (bench-day finding 2026-07-19)"""
    con = f"pieria-{ssid}"
    # Idempotent: drop any stale profile of the same name from a prior run.
    subprocess.run(["nmcli", "connection", "delete", con], check=False, capture_output=True, timeout=15)
    subprocess.run(["nmcli", "connection", "add", "type", "wifi", "con-name", con,
                    "ifname", "wlan0", "ssid", ssid, "autoconnect", "yes"],
                   check=True, capture_output=True, timeout=30)
    if password:
        subprocess.run(["nmcli", "connection", "modify", con,
                        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password],
                       check=True, capture_output=True, timeout=30)


#: The setup-mode NetworkManager drop-in written by sd-setup-pre (keep in sync with setup/common.sh).
_SETUP_DROPIN = Path("/etc/NetworkManager/conf.d/99-pieria-setup.conf")


def _release_wlan0() -> None:
    """Give wlan0 back to NetworkManager before the post-commit reboot.

    While setup mode runs, wlan0 is marked unmanaged so hostapd can own the radio. That drop-in MUST NOT
    outlive setup: `_join_wifi` only SAVES the profile and relies on NM auto-connecting it after the
    reboot — with the radio still unmanaged, NM would never bring it up and the user would be left with a
    box that finished setup and then silently never joined Wi-Fi. sd-setup-pre also removes this on the
    next boot (the authoritative, self-healing path); this is the belt to that pair of braces.
    Live-mode only; best-effort by design — a failure here is still covered on the next boot.
    """
    try:
        _SETUP_DROPIN.unlink(missing_ok=True)
    except OSError:
        pass
    subprocess.run(["nmcli", "general", "reload"], check=False, capture_output=True, timeout=15)


def _schedule_reboot() -> None:
    """Reboot shortly after responding, so the user sees the success page first. Live-mode only."""
    subprocess.Popen(["sh", "-c", "sleep 3; systemctl reboot"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pieria first-run setup wizard")
    ap.add_argument("--dry-run", action="store_true",
                    help="Run the full wizard but change nothing (safe to run on a working Pi).")
    ap.add_argument("--port", type=int, default=80, help="Port to serve on (default 80; use 8080 for dry-run).")
    ap.add_argument("--all-in-one", action="store_true", help="Preselect ALL_IN_ONE=1 / localhost server.")
    ap.add_argument("--boot-conf", default="", help="Override the boot-partition conf path.")
    ap.add_argument("--output", default="HDMI-A-1", help="HDMI output for the live-rotate preview.")
    ap.add_argument("--recovery", default="",
                    help="Banner text shown when re-opened by sd-net-recover after a failed join.")
    args = ap.parse_args(argv)

    boot_conf = Path(args.boot_conf) if args.boot_conf else resolve_boot_conf_path()
    cfg = SetupConfig(dry_run=args.dry_run, all_in_one=args.all_in_one, boot_conf=boot_conf,
                      output=args.output, recovery=args.recovery)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(cfg))
    mode = "DRY RUN (nothing will be changed)" if args.dry_run else "LIVE"
    print(f"Pieria setup wizard — {mode} — http://0.0.0.0:{args.port}  (conf target: {boot_conf})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


# --- wizard page (self-contained; no external assets so it works behind the captive portal) ----------
WIZARD_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Set up your Pieria</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --border:#334155; --accent:#3b82f6; --text:#f1f5f9; --muted:#94a3b8; --danger:#ef4444; --ok:#34d399; }
  * { box-sizing: border-box; }
  body { margin:0; padding:20px; background:var(--bg); color:var(--text); font-family:'Inter',-apple-system,sans-serif; min-height:100vh; }
  .wrap { max-width:440px; margin:0 auto; }
  h1 { font-size:1.4rem; margin:8px 0 4px; }
  .sub { color:var(--muted); font-size:0.85rem; margin-bottom:18px; }
  .banner { background:#78350f; color:#fde68a; border:1px solid #b45309; border-radius:8px; padding:10px 12px; font-size:0.8rem; margin-bottom:18px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:18px; margin-bottom:16px; }
  label { display:block; font-size:0.72rem; text-transform:uppercase; letter-spacing:0.06rem; color:var(--muted); margin:14px 0 6px; }
  label:first-child { margin-top:0; }
  input[type=text], input[type=password], select { width:100%; background:var(--bg); border:1px solid var(--border); color:var(--text); padding:11px; border-radius:8px; font-size:1rem; outline:none; }
  input:focus, select:focus { border-color:var(--accent); }
  .err { color:var(--danger); font-size:0.75rem; margin-top:5px; display:none; }
  .hint { color:var(--muted); font-size:0.72rem; margin-top:5px; }
  button { background:var(--accent); color:white; border:none; padding:13px 18px; border-radius:9px; font-size:0.95rem; font-weight:600; cursor:pointer; width:100%; }
  button.secondary { background:transparent; border:1px solid var(--border); color:var(--text); }
  button:disabled { opacity:0.5; cursor:default; }
  .row { display:flex; gap:10px; }
  pre { background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:12px; font-size:0.75rem; overflow-x:auto; white-space:pre-wrap; word-break:break-word; color:#cbd5e1; }
  .ok { color:var(--ok); } .muted { color:var(--muted); font-size:0.8rem; }
  .hidden { display:none; }
</style>
</head><body>
<div class="wrap">
  <h1>Set up your display</h1>
  <div class="sub">A couple of details and your gallery is live. No apps, no accounts.</div>
  <div id="mode-banner" class="banner hidden"></div>

  <div class="card" id="form-card">
    <label>Setup PIN <span style="text-transform:none;letter-spacing:0;color:var(--muted)">(shown on your screen)</span></label>
    <input type="text" id="pin" inputmode="numeric" pattern="[0-9]*" autocomplete="one-time-code" maxlength="6" placeholder="123456">
    <div class="err" id="err-pin"></div>
    <div class="hint">Look at the display (HDMI or e-ink) you're setting up — it's showing a 6-digit PIN.</div>

    <label>Wi-Fi network <span style="text-transform:none;letter-spacing:0;color:var(--muted)">(skip if wired)</span></label>
    <select id="wifi_pick"><option value="">Scanning\u2026</option></select>
    <input type="text" id="wifi_ssid" class="hidden" placeholder="Your Wi-Fi name" autocomplete="off">
    <div class="hint" id="wifi_hint">Pick your network from the list \u2014 no typing, no typos.</div>
    <label>Wi-Fi password</label>
    <input type="password" id="wifi_pass" placeholder="Leave blank for an open network" autocomplete="off">
    <label style="display:flex;align-items:center;gap:8px;text-transform:none;letter-spacing:0;margin-top:8px;">
      <input type="checkbox" id="wifi_show" style="width:auto;"> Show password
    </label>

    <label>What does this box do?</label>
    <select id="all_in_one">
      <option value="1">It runs everything (server + display)</option>
      <option value="0">It's a display only \u2014 my server is elsewhere</option>
    </select>
    <div class="hint">Most people want one box that does both. Choose the second only if you already
      run Pieria on another machine.</div>

    <label>Server address</label>
    <input type="text" id="server_url" value="http://localhost:8000">
    <div class="err" id="err-server_url"></div>
    <div class="hint">Where Pieria runs. If this box runs the server too, keep localhost.</div>

    <label>Name this display</label>
    <input type="text" id="display_id" placeholder="living_room">
    <div class="err" id="err-display_id"></div>
    <div class="hint">Appears in the phone remote. Letters, numbers, - or _.</div>
    <div class="hint" id="host-line" style="margin-top:8px;">
      On your network as <b id="host-preview" class="ok">pieria</b>.local
      · <a href="#" id="host-edit" style="color:var(--accent);">change</a>
    </div>
    <div id="host-edit-row" class="hidden">
      <label>Network name (advanced)</label>
      <input type="text" id="hostname" placeholder="living-room" autocomplete="off">
      <div class="err" id="err-hostname"></div>
      <div class="hint">The <code>.local</code> address other devices use to reach this box. Most
        people can leave this — it follows the display name.</div>
    </div>

    <label>Orientation</label>
    <select id="orientation">
      <option value="landscape">Landscape (normal)</option>
      <option value="90">Portrait — rotated 90°</option>
      <option value="270">Portrait — rotated 270°</option>
      <option value="180">Upside-down (180°)</option>
    </select>
    <div class="err" id="err-orientation"></div>
    <div class="row" style="margin-top:8px;">
      <button class="secondary" id="try-rotate" type="button">Preview this rotation on the screen</button>
    </div>
    <div class="hint" id="rotate-status"></div>

    <label>Time zone</label>
    <input type="text" id="timezone" placeholder="America/Chicago" autocomplete="off">
    <div class="err" id="err-timezone"></div>
    <div class="hint">Filled in from this phone. Night &amp; Quiet Hours follow this clock.</div>

    <div style="margin-top:20px;"><button id="continue">Review &amp; finish →</button></div>
  </div>

  <div class="card hidden" id="confirm-card">
    <h1 style="font-size:1.1rem;">Does this look right?</h1>
    <div class="muted" id="confirm-summary"></div>
    <pre id="conf-preview"></pre>
    <div id="commit-result" class="muted" style="margin-bottom:12px;"></div>
    <div class="row">
      <button class="secondary" id="back" type="button">← Back</button>
      <button id="commit">Save &amp; start</button>
    </div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
let MODE = { dry_run:false };

// The phone knows the house's zone; the Pi (fresh image) does not. Pre-fill, leave it editable.
try { const tz = Intl.DateTimeFormat().resolvedOptions().timeZone; if (tz) $('timezone').value = tz; } catch (e) {}

async function loadMode() {
  try {
    MODE = await fetch('/api/mode').then(r=>r.json());
    if (MODE.dry_run) {
      const b = $('mode-banner'); b.classList.remove('hidden');
      b.textContent = '🔒 Dry run — nothing on this device will be changed. This is a safe preview.';
    } else if (MODE.recovery) {
      const b = $('mode-banner'); b.classList.remove('hidden');
      b.textContent = '\u26a0\ufe0f ' + MODE.recovery;
    }
    if (MODE.all_in_one) $('server_url').value = 'http://localhost:8000';
  } catch(e){}
}

// The radio can only scan in station mode, so sd-setup-boot scans BEFORE raising the AP and caches the
// result. Typing an SSID by hand is the single biggest source of a failed setup, so the list is the
// default and free text is the fallback (hidden networks, or an empty/failed scan).
async function loadNetworks(pin) {
  const pick = $('wifi_pick'), manual = $('wifi_ssid');
  let nets = [];
  // The scan is PIN-gated (it leaks neighbouring SSIDs to anyone in AP range) — without a complete PIN
  // yet we simply show no list and fall back to typing the SSID by hand, same as a failed/empty scan.
  if (pin && pin.length === 6) {
    try {
      // Header, not a query string — a query string lands in access logs and browser history verbatim.
      const r = await fetch('/api/networks', {headers: {'X-Setup-PIN': pin}});
      if (r.ok) nets = (await r.json()).networks || [];
    } catch(e){}
  }
  pick.innerHTML = '';
  const blank = document.createElement('option');
  blank.value = ''; blank.textContent = nets.length ? 'Choose your network\u2026' : 'No networks found';
  pick.appendChild(blank);
  nets.forEach(n => {
    const o = document.createElement('option');
    o.value = n.ssid;
    o.textContent = n.ssid + (n.secure ? '' : ' (open)') + (n.signal ? '  \u00b7 ' + n.signal + '%' : '');
    pick.appendChild(o);
  });
  const other = document.createElement('option');
  other.value = '__manual__'; other.textContent = 'Type it myself\u2026';
  pick.appendChild(other);

  pick.onchange = () => {
    const manualMode = pick.value === '__manual__';
    manual.classList.toggle('hidden', !manualMode);
    if (manualMode) { manual.value = ''; manual.focus(); } else { manual.value = pick.value; }
    $('wifi_hint').textContent = manualMode
      ? 'Enter the exact network name, including capitals.'
      : 'Pick your network from the list \u2014 no typing, no typos.';
  };
  if (!nets.length) { pick.value = '__manual__'; pick.onchange(); }
}

function wirePin() {
  $('pin').addEventListener('input', () => {
    const v = $('pin').value.replace(/\\D/g,'').slice(0,6);
    $('pin').value = v;
    if (v.length === 6) loadNetworks(v);
  });
}

function wireWifiExtras() {
  $('wifi_show').onchange = (e) => {
    $('wifi_pass').type = e.target.checked ? 'text' : 'password';
  };
  $('all_in_one').onchange = (e) => {
    // Keep the server address honest with the choice: an all-in-one box serves itself.
    if (e.target.value === '1') $('server_url').value = 'http://localhost:8000';
    else if ($('server_url').value === 'http://localhost:8000') $('server_url').value = 'http://';
  };
}

// Turn a display name into the hostname the box will answer to — MUST match derive_hostname() in the
// Python: lowercase, underscores→hyphens, drop the rest, collapse/trim, ≤63 chars. It's a preview only;
// the server re-derives authoritatively on commit, so a drift here is cosmetic, not a correctness bug.
function deriveHost(name){
  let s = (name||'').trim().toLowerCase().replace(/_/g,'-').replace(/[^a-z0-9-]+/g,'-');
  s = s.replace(/-+/g,'-').replace(/^-+|-+$/g,'').slice(0,63).replace(/-+$/,'');
  return s;
}
let hostEdited = false;   // once the user opens + types the advanced field, stop auto-following
function refreshHostPreview(){
  const derived = deriveHost($('display_id').value) || 'pieria';
  $('host-preview').textContent = hostEdited && $('hostname').value ? $('hostname').value : derived;
}
function wireHostname(){
  $('display_id').addEventListener('input', () => { if(!hostEdited) refreshHostPreview(); });
  $('host-edit').onclick = (e) => {
    e.preventDefault();
    $('host-edit-row').classList.remove('hidden');
    $('host-line').classList.add('hidden');
    if(!$('hostname').value) $('hostname').value = deriveHost($('display_id').value);
    $('hostname').focus();
    hostEdited = true;
  };
  $('hostname').addEventListener('input', refreshHostPreview);
}

function fields() {
  return {
    pin: $('pin').value,
    wifi_ssid: $('wifi_ssid').value, wifi_pass: $('wifi_pass').value,
    all_in_one: $('all_in_one').value,
    server_url: $('server_url').value, display_id: $('display_id').value,
    hostname: $('hostname').value,
    orientation: $('orientation').value,
    timezone: $('timezone').value,
  };
}
function clearErrors(){ ['pin','server_url','display_id','orientation','hostname','timezone'].forEach(f=>{ const e=$('err-'+f); e.style.display='none'; }); }
function showErrors(errs){ clearErrors(); for(const [f,m] of Object.entries(errs)){ const e=$('err-'+f); if(e){ e.textContent=m; e.style.display='block'; } } }

$('try-rotate').onclick = async () => {
  $('rotate-status').textContent = 'Applying…';
  const r = await fetch('/api/orientation', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({orientation: $('orientation').value, pin: $('pin').value})}).then(r=>r.json());
  $('rotate-status').textContent = r.message || r.error || '';
};

$('continue').onclick = async () => {
  const r = await fetch('/api/validate', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify(fields())});
  const data = await r.json();
  if (data.errors && Object.keys(data.errors).length) { showErrors(data.errors); return; }
  clearErrors();
  $('conf-preview').textContent = data.conf;
  const f = fields();
  const host = (f.hostname && f.hostname.trim()) || deriveHost(f.display_id) || 'pieria';
  $('confirm-summary').textContent = `Display “${f.display_id}” → ${f.server_url}` +
    (f.wifi_ssid ? ` · Wi-Fi “${f.wifi_ssid}”` : ' · wired network') +
    ` · reachable at ${host}.local`;
  $('form-card').classList.add('hidden');
  $('confirm-card').classList.remove('hidden');
  window.scrollTo(0,0);
};

$('back').onclick = () => { $('confirm-card').classList.add('hidden'); $('form-card').classList.remove('hidden'); };

$('commit').onclick = async () => {
  $('commit').disabled = true;
  $('commit-result').textContent = 'Saving…';
  const r = await fetch('/api/commit', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify(fields())});
  const data = await r.json();
  if (data.errors) { $('commit').disabled=false; $('commit-result').textContent='Please fix the form.'; return; }
  if (data.error) {
    $('commit').disabled = false;
    $('commit-result').textContent = data.error;
    if (data.pin_required) { $('err-pin').textContent = data.error; $('err-pin').style.display='block'; }
    return;
  }
  if (data.dry_run) {
    $('commit-result').innerHTML = '<span class="ok">✓ Dry run complete.</span> ' + data.message +
      '<br>Would write to: <code>' + data.would_write_to + '</code>' +
      (data.would_join_wifi ? '<br>Would join Wi-Fi: <code>' + data.would_join_wifi + '</code>' : '') +
      '<br>Preview saved at: <code>' + data.preview_path + '</code>';
    $('commit').textContent = 'Done (dry run)';
  } else {
    $('commit-result').innerHTML = '<span class="ok">✓ ' + (data.message||'Saved.') + '</span>';
    $('commit').textContent = 'Starting…';
  }
};

loadMode();
loadNetworks('');
wirePin();
wireWifiExtras();
wireHostname();
refreshHostPreview();
</script>
</body></html>"""


if __name__ == "__main__":
    main()
