#!/usr/bin/env python3
"""Screen Docent — first-run setup wizard (R1-F1).

A tiny, dependency-free (Python stdlib only) web wizard that collects Wi-Fi + server/display config on
first boot and writes the appliance `screen-docent.conf`, so a non-technical user never touches SSH or
hand-edits a file. On a freshly flashed Pi it runs behind a `Docent-Setup` Wi-Fi hotspot + captive
portal (see sd-setup-boot); here it is just the HTTP brain.

Two modes:
  * live      — writes the real boot-partition conf, joins Wi-Fi (nmcli), tears down the AP, reboots.
  * --dry-run — writes a PREVIEW conf to a temp dir and shows the exact bytes it *would* write; never
                touches the real conf, Wi-Fi, or reboots. Safe to run on a working Pi in-situ:
                    python3 sd_setup.py --dry-run --port 8080
                then open http://<pi>:8080 from a phone/laptop on the same network.

The wizard NEVER touches Artwork/ or the database — it only ever writes screen-docent.conf.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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


def sanitize_display_id(raw: str) -> str:
    """Lowercase, collapse anything that isn't [a-z0-9_-] to a single underscore, and trim leading/
    trailing separators so an id never starts or ends with a stray '-' or '_'."""
    return _DISPLAY_ID_RE.sub("_", (raw or "").strip().lower()).strip("_-")


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

    return errors


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
                "WAIT_TIMEOUT", "ALL_IN_ONE", "GEMINI_API_KEY", "EINK_ORIENTATION"}


def _preserved_lines(existing: str) -> list:
    """Settings from an existing conf that the wizard must NOT clobber.

    The wizard emits a fixed key set, so a re-run silently DELETED everything else — EINK_ENABLED,
    EINK_SATURATION, EINK_MIN_INTERVAL, WATCHDOG. An e-ink box that went through setup (or the ADR-057
    recovery wizard) came back with its panel unconfigured and its watchdog reset, with nothing to
    indicate why. Found before it could bite on the bench, 2026-07-21.
    """
    out = []
    for raw in (existing or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key and key not in _WIZARD_KEYS:
            out.append(f"{key}={line.split('=', 1)[1].strip()}")
    return out


def build_conf(fields: dict, all_in_one: bool = False, existing: str = "") -> str:
    """Render the screen-docent.conf text from validated wizard fields.

    Only kiosk variables land here (SERVER_URL/DISPLAY_ID/ROTATE/OUTPUT/…). Wi-Fi credentials are NOT
    written to this FAT boot file — they go to NetworkManager via nmcli at commit time. GEMINI_API_KEY
    is left blank (a separate, optional step for all-in-one AI). Any OTHER key already present in
    `existing` is carried through untouched — see _preserved_lines.
    """
    rotate = ORIENTATIONS.get(fields.get("orientation", "landscape"), ("", ""))[0]
    display_id = sanitize_display_id(fields.get("display_id", "")) or "display"
    server_url = (fields.get("server_url") or "").strip() or "http://localhost:8000"
    output = (fields.get("output") or "HDMI-A-1").strip()
    return (
        "# Screen Docent — Appliance configuration\n"
        "# Written by the first-run setup wizard. Safe to edit on the SD card's boot partition.\n"
        f"SERVER_URL={server_url}\n"
        f"DISPLAY_ID={display_id}\n"
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
    return (firmware if firmware.is_dir() else Path("/boot")) / "screen-docent.conf"


# --- HTTP server --------------------------------------------------------------

# OS connectivity-check URLs. A captive portal answers these with a redirect so the "Sign in to
# network" sheet pops on iOS/Android/Windows. (Only reachable via the AP's DNS catch-all on a real
# first boot; harmless in dry-run.)
_CAPTIVE_PROBES = {
    "/generate_204", "/gen_204", "/hotspot-detect.html", "/library/test/success.html",
    "/ncsi.txt", "/connecttest.txt", "/redirect", "/canonical.html", "/success.txt",
}


class SetupConfig:
    """Runtime knobs shared with the request handler."""
    def __init__(self, dry_run: bool, all_in_one: bool, boot_conf: Path, output: str,
                 recovery: str = ""):
        self.dry_run = dry_run
        self.all_in_one = all_in_one
        self.boot_conf = boot_conf
        self.output = output
        # Non-empty when sd-net-recover re-opened the wizard because a CONFIGURED box could not get
        # online (usually a mistyped Wi-Fi password). Shown as a banner so the user understands this is
        # a second attempt, not a fresh setup — otherwise the box silently looks like it reset itself.
        self.recovery = recovery
        self.preview_path = Path("/tmp/sd-setup-preview/screen-docent.conf")
        self._revert_timer: threading.Timer | None = None


def _preview_on_eink(orientation: str) -> bool:
    """Repaint the e-ink setup card in `orientation`, in the background. Returns whether we launched it.

    Best-effort and fire-and-forget: a full Spectra 6 refresh is ~9s and the HTTP response must not wait
    on it. Absent panel / absent script simply means False and the caller falls back to its old message.
    """
    card = shutil.which("sd-setup-card") or "/usr/local/bin/sd-setup-card"
    if not Path(card).exists():
        return False
    try:
        subprocess.Popen([card, "--ssid", "Docent-Setup", "--orientation", orientation],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


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

    if not shutil.which("wlr-randr") or not os.environ.get("WAYLAND_DISPLAY"):
        if eink:
            return {"mode": "eink", "message": "Repainting the e-ink panel in that orientation — "
                                               "it takes about 10 seconds."}
        return {"mode": "unavailable",
                "message": "Live preview runs on the display itself (the Pi kiosk). Your choice is "
                           "recorded and written to the config."}
    try:
        subprocess.run(["wlr-randr", "--output", output, "--transform", transform],
                       check=True, capture_output=True, timeout=10)
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        return {"mode": "error", "message": f"Could not rotate: {e}"}

    # Cancel any prior pending revert, then arm a new one.
    if cfg._revert_timer:
        cfg._revert_timer.cancel()

    def _revert():
        try:
            subprocess.run(["wlr-randr", "--output", output, "--transform", "normal"],
                           check=False, capture_output=True, timeout=10)
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
            sys.stderr.write(f"sd-setup: {self.address_string()} {fmt % args}\n")

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
                self._send(200, WIZARD_HTML, "text/html; charset=utf-8")
            elif path == "/api/networks":
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
                self._json(200, _apply_rotation(cfg.output, body.get("orientation", "landscape"),
                                                int(body.get("revert_after", 30)), cfg))
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


def _join_wifi(ssid: str, password: str) -> None:
    """Persist the chosen Wi-Fi to NetworkManager so the post-commit reboot auto-joins it. We SAVE the
    profile (autoconnect) rather than activate it now: wlan0 is currently held by the setup AP (hostapd,
    NM-unmanaged), and activating would tear the AP down mid-commit — killing the phone's connection
    before it ever sees the success page. Leaving setup mode on reboot lets NM auto-connect the saved
    profile. Live-mode only. (bench-day finding 2026-07-19)"""
    con = f"docent-{ssid}"
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
_SETUP_DROPIN = Path("/etc/NetworkManager/conf.d/99-screen-docent-setup.conf")


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
    ap = argparse.ArgumentParser(description="Screen Docent first-run setup wizard")
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
    print(f"Screen Docent setup wizard — {mode} — http://0.0.0.0:{args.port}  (conf target: {boot_conf})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


# --- wizard page (self-contained; no external assets so it works behind the captive portal) ----------
WIZARD_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Set up your Screen Docent</title>
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
      run Screen Docent on another machine.</div>

    <label>Server address</label>
    <input type="text" id="server_url" value="http://localhost:8000">
    <div class="err" id="err-server_url"></div>
    <div class="hint">Where Screen Docent runs. If this box runs the server too, keep localhost.</div>

    <label>Name this display</label>
    <input type="text" id="display_id" placeholder="living_room">
    <div class="err" id="err-display_id"></div>
    <div class="hint">Appears in the phone remote. Letters, numbers, - or _.</div>

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
async function loadNetworks() {
  const pick = $('wifi_pick'), manual = $('wifi_ssid');
  let nets = [];
  try { nets = (await fetch('/api/networks').then(r=>r.json())).networks || []; } catch(e){}
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

function fields() {
  return {
    wifi_ssid: $('wifi_ssid').value, wifi_pass: $('wifi_pass').value,
    all_in_one: $('all_in_one').value,
    server_url: $('server_url').value, display_id: $('display_id').value,
    orientation: $('orientation').value,
  };
}
function clearErrors(){ ['server_url','display_id','orientation'].forEach(f=>{ const e=$('err-'+f); e.style.display='none'; }); }
function showErrors(errs){ clearErrors(); for(const [f,m] of Object.entries(errs)){ const e=$('err-'+f); if(e){ e.textContent=m; e.style.display='block'; } } }

$('try-rotate').onclick = async () => {
  $('rotate-status').textContent = 'Applying…';
  const r = await fetch('/api/orientation', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({orientation: $('orientation').value})}).then(r=>r.json());
  $('rotate-status').textContent = r.message || '';
};

$('continue').onclick = async () => {
  const r = await fetch('/api/validate', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify(fields())});
  const data = await r.json();
  if (data.errors && Object.keys(data.errors).length) { showErrors(data.errors); return; }
  clearErrors();
  $('conf-preview').textContent = data.conf;
  const f = fields();
  $('confirm-summary').textContent = `Display “${f.display_id}” → ${f.server_url}` +
    (f.wifi_ssid ? ` · Wi-Fi “${f.wifi_ssid}”` : ' · wired network');
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
loadNetworks();
wireWifiExtras();
</script>
</body></html>"""


if __name__ == "__main__":
    main()
