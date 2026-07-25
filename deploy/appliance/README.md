# Pieria — Raspberry Pi Appliance

Turn a cheap Raspberry Pi into a self-contained art frame that boots straight
into the Pieria display: **fullscreen, no browser chrome, no Fully Kiosk,
survives reboots, never sleeps.** This is the recommended way to drive a TV or
monitor — point it at your Pieria server and forget it.

> **Scope:** *display-only* — the Pi is a thin client that connects to a
> Pieria server running elsewhere (e.g. your MS-01). **This is the recommended
> production setup, not just a first cut:** the display device only runs the
> Chromium kiosk, so it stays cheap, cool (~5–7 W), and small enough to tuck
> behind the panel. Running the server *on the same Pi* ("all-in-one") is also
> supported (see [below](#all-in-one-mode-server--display-on-one-box)).

---

## What you need

- A Raspberry Pi (Pi 4 or Pi 5 recommended; Pi 3 works but is slower) + power + HDMI.
- A microSD card flashed with **Raspberry Pi OS Lite, 64-bit (Bookworm)** using
  Raspberry Pi Imager. In the Imager's settings, enable SSH and set a user so you
  can log in once to run the installer.
- A running Pieria server reachable on your LAN (the box running
  `docker compose up` — see the repo root [`README.md`](../../README.md)).
  *(Not needed for all-in-one mode, which runs the server on the Pi itself.)*

## Flash-day checklist

Boxed Pi → running kiosk in minutes:

1. **Raspberry Pi Imager** → choose **Raspberry Pi OS Lite (64-bit)**.
2. Click the gear / **Edit Settings** *before* writing and set:
   - **Hostname** (e.g. `pieria-living-room`) — also becomes your address: `pieria-living-room.local`.
   - **Enable SSH** (password or public key)
   - **Username + password** (your one-time login to run the installer)
   - ⚠️ **Wi-Fi SSID + password + country** — **the single most important step.** This is the
     **only** place Wi-Fi gets configured; it is *not* on the boot partition and cannot be fixed
     later without a keyboard+monitor. If you skip it, the Pi boots dark and silent.
   - **Locale / timezone**
3. Write the card and boot the Pi — no keyboard or monitor required.
4. *(Optional, and required to pre-enable all-in-one)* drop a `pieria.conf`
   onto the boot partition now.
5. `ssh <user>@<hostname>.local`, then follow **Install** below.

> **Finding the box on your network (no IP hunting):** because you set a Hostname in step 2, you reach
> it by name — SSH as `<hostname>.local`, and (for all-in-one) open the admin at
> **`http://<hostname>.local:8000/admin`** (e.g. `http://pieria-living-room.local:8000/admin`). The
> installer makes sure the `avahi-daemon` (mDNS) that powers `.local` is running. If your client
> doesn't do mDNS (some Android devices), find the Pi's DHCP address in your router's client list, or
> run `ping <hostname>.local` from a Mac/PC to resolve it.

> **Troubleshooting — Pi booted dark / `ssh …local` won't resolve:** almost always the Wi-Fi step (2)
> was missed or mistyped. Re-flash with **Edit Settings**, or attach a keyboard + monitor and run
> `sudo nmtui` to join Wi-Fi. (The kiosk also shows nothing until it can reach its server.)

## Install

SSH into the freshly-booted Pi, then:

```bash
git clone https://github.com/pieria-art/Pieria.git
sudo Pieria/deploy/appliance/install.sh
```

The installer:

- installs `cage` (a minimal single-app Wayland kiosk compositor), Chromium, and `seatd`;
- creates a `kiosk` user and logs it in automatically on **tty1**;
- installs the launcher to `/usr/local/bin` and a login hook that runs it;
- writes a config file to the SD card's **boot partition** so you can edit it
  from any computer;
- disables console blanking.

## Configure

Edit `pieria.conf` on the **boot partition** (it appears as a small FAT
volume named `bootfs`/`boot` when you put the SD card in any computer — no SSH
needed). On the Pi itself it lives at `/boot/firmware/pieria.conf`.

```ini
SERVER_URL=http://192.168.1.50:8000   # your Pieria server
DISPLAY_ID=living_room                # unique name shown in the mobile remote
MODE=                                 # optional: ken-burns|static-crop|contain-matte
CYCLE_TIME=                           # optional: seconds per image
WAIT_TIMEOUT=0                        # 0 = wait forever for the server at boot
ROTATE=                               # portrait panel? 90 or 270 (blank = landscape)
OUTPUT=HDMI-A-1                       # which HDMI port ROTATE applies to
```

> **Portrait (or upside-down) displays:** set `ROTATE=90` (or `270` if it comes up
> upside-down; `180` flips a landscape panel). The rotation is done by the compositor,
> so the picture stays pixel-exact — for a commercial panel like a Samsung QMR, also set
> the **TV's own** orientation to **Landscape/Normal** and Picture Size to **"Just Scan"**
> so it passes the HDMI signal 1:1 instead of stretching it. (The TV's built-in "portrait"
> mode is for its internal player and will deform an external source.)
>
> The rotation is **re-asserted automatically** (`sd-rotate-keep`), so it survives the
> TV powering off and on — e.g. a built-in sleep timer. Without that, an HDMI sink
> dropping and re-hotplugging on wake resets wlroots to landscape until the next reboot.

Then `sudo reboot`. The Pi comes up fullscreen on the Canvas. If the server
isn't reachable yet, the screen stays black and paints automatically once it is.

---

## First-run setup wizard (R1-F1)

Instead of editing `pieria.conf` by hand, a freshly flashed card can configure itself from a
phone. On first boot (no valid conf yet), `sd-setup-boot` brings up an open **`Pieria-Setup`** Wi-Fi AP
with a captive portal; you join it, a setup page opens, you enter Wi-Fi + server + display name +
orientation, confirm "does this look right?", and the Pi writes the conf, joins your network, and
reboots into the gallery. No SSH, no SD-card editing.

- **Enabled on the pre-baked `.img`, not by `install.sh`.** `install.sh` installs the wizard assets but
  leaves `sd-setup.service` **disabled** (and `hostapd`/`dnsmasq` disabled) so it never disturbs a
  working box. The image-build step enables `sd-setup.service`.
- **The wizard only ever writes `pieria.conf`** — it never touches `Artwork/` or the database.
- **Baking the image itself:** see **[`docs/image-build.md`](../../docs/image-build.md)** — the full
  provision → verify → sysprep → capture → gramps-test checklist, plus the traps that have already
  cost real time (wrong flavour, trixie vs Bookworm, unarmed host keys, shipped `authorized_keys`).

**Test it non-destructively on a working Pi (no flash, no changes):**

```
python3 ~/Pieria/deploy/appliance/setup/sd_setup.py --dry-run --port 8080
```

Then open `http://<pi-ip>:8080` from a phone or laptop on the same network and walk the wizard. In
`--dry-run` it skips the AP, `nmcli`, and the reboot, writes the conf only to `/tmp/sd-setup-preview/`,
and shows you the exact bytes it *would* write to the boot partition. Your Wi-Fi, config, art, and
display are untouched. (Orientation preview is simulated unless you opt into the live 30 s auto-revert.)

> **Pi-gated:** the AP bring-up / captive portal / Wi-Fi hand-off and the `.img` build pipeline are only
> fully validated on real hardware with a flash cycle. The wizard's form + conf-writer logic is verified
> off-Pi via `--dry-run` (and the pytest suite).

---

## All-in-one mode (server + display on one box)

For a single-frame setup with **no separate server**, the appliance can also run the Pieria
server on the same Pi. In `pieria.conf` set:

```ini
ALL_IN_ONE=1
SERVER_URL=http://localhost:8000
GEMINI_API_KEY=your-key-here   # optional — see note below
```

> **`GEMINI_API_KEY` here is just an optional pre-seed.** The primary way to connect a model is the
> in-app panel (**Admin → ⚙ Settings → 🧠 AI Engine**), which supports Gemini/OpenAI/Anthropic/
> OpenRouter/local and **overrides** this value when set. Leave it blank to configure everything from
> the GUI after first boot.

then run `sudo install.sh` (re-run it if you flip this on after the first install). The installer
installs Docker, writes the server's `.env`, and brings up the stack with
[`compose/docker-compose.appliance.yml`](compose/docker-compose.appliance.yml) — which trims Uvicorn
from 4 workers to **2** so the server and Chromium kiosk share the Pi's RAM comfortably. The stack
uses `restart: unless-stopped`, so it returns on every reboot, and `sd-wait-for-server` holds the
kiosk until it answers.

**Notes:** use a **Pi 5 (8 GB)** for all-in-one — the server's Pillow/Docker work wants the headroom
(display-only is fine on a Pi 4). First boot is slow (Docker build + the ~500 MB factory-seed
download). The Gemini key lives on the FAT boot partition — acceptable for a home box, but be aware
anyone with the SD card can read it.

---

## E-ink panel (Track B, optional)

For a Pimoroni **Inky Impression 13.3" (Spectra 6)** panel wired to this box's GPIO header, set in
`pieria.conf`:

```ini
EINK_ENABLED=1
EINK_MIN_INTERVAL=900     # cadence floor (s) — e-ink art is contemplative
EINK_SATURATION=0.5       # Inky saturation 0-1 (bench-tune against the real panel)
EINK_ORIENTATION=         # blank = landscape 1600x1200 | portrait = 1200x1600
```

then re-run `sudo install.sh`. This works in **either** topology: all-in-one (`SERVER_URL=http://localhost:8000`,
alongside the server container on this same box) or **satellite/client-only** — no local container at
all, `SERVER_URL` pointed at a remote hub (another Pieria box on the LAN). `sd-eink` runs
host-side (not in Docker) because GPIO/SPI aren't reachable from the non-root app container; it polls
`GET /display/<DISPLAY_ID>/current.png`, change-detects on the response's `ETag` so an unchanged frame
never triggers a panel refresh, and never repaints during Night/Quiet Hours (e-ink holds its image at
zero power, so "quiet" means *stop refreshing*, not blank the panel).

Smoke-test with no panel attached: `EINK_DRY_RUN=1 sd-eink /path/to/pieria.conf` (or `--dry-run`)
swaps in an in-memory fake and logs "would paint" instead of touching hardware.

---

## GUI maintenance & updates (all-in-one)

In all-in-one mode the admin gains a **🩺 Devices** tab (host health) and an **Appliance Maintenance**
card that can **Update App** (git pull `origin/main` + `docker compose up -d --build`), **Update
Scripts** (re-run `install.sh`), and **Reboot** — no SSH.

**How the privilege boundary is kept.** The web app runs in an **unprivileged container** and never
gains host access. A GUI action only writes `data/appliance/request.json` into the bind-mounted data
dir. A root **systemd path unit** (`sd-update.path`) notices the file and runs the host helper
`sd-update`, which performs a **fixed, whitelisted action** (the requested string is matched in a
`case`, never evaluated), writes progress to `data/appliance/status.json` for the GUI to poll, and
deletes the request. The container stays non-root; only the oneshot helper has host privilege.

> **Trust assumption.** Anything that can write `data/appliance/request.json` (the app, or anyone
> with write access to the data dir) can trigger a root-level pull/rebuild/reboot. The nonce is
> anti-stale-replay, not authentication. The bridge is enabled **only** when the all-in-one compose
> sets `SD_APPLIANCE_MODE=all-in-one`, and `update-app` is pinned to `git reset --hard origin/main`
> (no arbitrary ref/URL) — which **discards any local edits on the Pi**. The appliance is not an edit
> host, so that's intended; do bench work on a clone, not the deployed box.

---

## How it works (and a design note)

The kiosk is launched from the **autologin user's login shell on tty1**, not from
a standalone systemd service. `cage`/wlroots needs a real **logind seat**
(`seat0`); a plain service doesn't get one and would fail to find a seat. An
autologin session is the reliable, well-trodden pattern, so the "systemd" piece
here is just a `getty@tty1` autologin drop-in. `sd-kiosk-launch` then waits for
the server, runs Chromium inside `cage`, and relaunches the session if it dies.

Because `cage` has **no screensaver or idle logic**, the screen never blanks —
so the appliance does **not** need the hidden looping `<video>` "sleep defeater"
in `static/index.html`. (That element stays in the app for non-appliance displays
like Fire TV / bring-your-own-browser; it's simply inert here.)

## Files

| File | Role |
|------|------|
| `install.sh` | Idempotent provisioner (run with `sudo`). |
| `bin/sd-kiosk-launch` | Reads the config, builds the URL, runs `cage` + Chromium, relaunches on exit. |
| `bin/sd-wait-for-server` | Polls the server so Chromium never lands on an error page at cold boot. |
| `bin/sd-rotate-keep` | Re-asserts the `ROTATE` transform on every display power-cycle/hotplug so portrait survives the TV's sleep timer (only runs when `ROTATE` is set). |
| `bin/sd-metrics` | (all-in-one) Writes the Pi `vcgencmd` throttle/under-voltage reading into `data/appliance/` for the Device Health console; run by `sd-metrics.timer` every 30 s. |
| `bin/sd-quiet-hours` | (all-in-one) Powers the TV off/on over HDMI-CEC to match the app's Night & Quiet Hours schedule (only when `quiet_mode=cec`, on-transition only); run by `sd-quiet-hours.timer` every 60 s. Needs `cec-utils`; the Canvas software blackout is the fallback. |
| `setup/sd_setup.py` | First-run wizard web server (stdlib only). Installed as `/usr/local/bin/sd-setup`. Serves the setup form + captive-portal redirects; `--dry-run` for a safe in-situ test. |
| `bin/sd-watchdog` | (all-in-one) Self-heal: probes server + kiosk; on sustained failure escalates relaunch-kiosk → restart-container → reboot (with a boot-loop cap). Ships in `WATCHDOG=observe` (logs only) until you set `enforce`; run by `sd-watchdog.timer` every 60 s. |
| `bin/sd-setup-boot` | First-boot gate: if unconfigured, brings up the `Pieria-Setup` AP + captive portal and runs the wizard; else no-ops. Run by `sd-setup.service` (enabled on the `.img` only). |
| `setup/{hostapd,dnsmasq}.conf` | The `Pieria-Setup` access point + DNS catch-all that make the captive portal fire. Used only while `sd-setup-boot` runs. |
| `bin/sd-setup-pre` | Runs before the wizard to hand `wlan0` to `hostapd` (marks it NetworkManager-unmanaged) and to clear a stale drop-in afterwards. Enabled everywhere; inert on a configured box (ADR-056). |
| `bin/sd-net-recover` | Anti-brick backstop: re-opens the setup wizard if a *configured* box can never get online (e.g. the Wi-Fi password was mistyped). Enabled everywhere (ADR-057). |
| `bin/sd-setup-card` | Renders the e-ink first-run setup card — instructions plus a `WIFI:` join QR — locally with PIL, so an unconfigured box looks *waiting* rather than *working* (ADR-058). |
| `bin/sd-timesync-wait` | Bounded wait for NTP before the app touches the network. A flashed `.img` boots on `fake-hwclock`'s build-day timestamp, and a clock in the past fails TLS as "certificate is not yet valid" — which reads as "registry unreachable" (ADR-062). Always exits 0, so it can delay the stack but never block it. |
| `bin/sd-image-prep` | Sysprep for the `.img` bake: `--enable-setup` (light, reversible) or `--full` (destructive — wipes identity/Wi-Fi/logs, re-arms host keys, removes `authorized_keys`, stamps the clock floor). See `docs/image-build.md`. |
| `bin/sd-update` | (all-in-one) Root host helper for GUI updates — whitelisted update-app / update-scripts / reboot; triggered by `sd-update.path`. |
| `bin/sd-eink` | (optional, `EINK_ENABLED=1`) Track B e-ink client: polls `/display/<id>/current.png` and blits it to a Pimoroni Inky Impression Spectra 6 panel over SPI. Long-running (not timer-driven); works all-in-one or as a satellite pointed at a remote hub. `--dry-run`/`EINK_DRY_RUN=1` smoke-tests it with no panel attached. |
| `share/sd-splash.html` | Boot splash shown while the server starts, displaying the admin URL / `<hostname>.local` / IP; self-redirects to the canvas once the server answers. |
| `systemd/autologin.conf` | `getty@tty1` drop-in enabling kiosk-user autologin. |
| `systemd/sd-metrics.{service,timer}` | (all-in-one) Periodic host-metrics writer for Device Health. |
| `systemd/sd-quiet-hours.{service,timer}` | (all-in-one) Polls the quiet-hours schedule and drives HDMI-CEC panel power. |
| `systemd/sd-watchdog.{service,timer}` | (all-in-one) Runs the self-heal watchdog every 60 s (observe mode by default). |
| `systemd/sd-setup.service` | Runs `sd-setup-boot` on first boot. Installed disabled; enabled only on the pre-baked `.img`. |
| `systemd/sd-setup-pre.service` | Runs `sd-setup-pre`. Enabled everywhere — being always-on is what makes the setup-mode radio hand-off self-healing. |
| `systemd/sd-net-recover.service` | Runs `sd-net-recover`. Enabled everywhere (anti-brick). |
| `systemd/sd-app.service` | (all-in-one) Brings the compose stack up at boot. Exists because `restart: unless-stopped` only revives a container that *already exists* — a freshly flashed card has none, so without this it boots with no app, ever (ADR-060). |
| `systemd/sd-timesync-wait.service` | (all-in-one) Runs `sd-timesync-wait` after `network-online`, ordered before `sd-app`. Deliberately not the stock `systemd-time-wait-sync.service`, which waits *forever* inside `sysinit.target` and would hang the entire boot on a box with no internet. |
| `systemd/sd-update.{path,service}` | (all-in-one) Watches for GUI update requests and runs `sd-update`. |
| `systemd/sd-eink.service` | (optional, `EINK_ENABLED=1`) Long-running unit running `sd-eink` (`Restart=always`; the poll/sleep cadence lives inside the client, not a timer). |
| `udev/99-pieria-no-cec-pointer.rules` | Ignores the HDMI-CEC phantom pointer so no stray cursor shows on the display. |
| `avahi/pieria.service` | (all-in-one) Advertises the server over mDNS with a friendly name. |
| `config/pieria.conf.example` | Template seeded to the boot partition. |
| `compose/docker-compose.appliance.yml` | All-in-one override (Uvicorn 4→2 workers, `SD_APPLIANCE_MODE=all-in-one`) merged over the root compose. |

---

## To verify on real hardware

This is packaging, so validation is "does a fresh device just work":

1. Flash Pi OS Lite, run the installer, set the config, reboot **with no keyboard attached**.
2. Confirm it lands fullscreen on the Canvas (`/?display=<id>`) with **no browser
   chrome**, **no manual URL entry**, and **no Fully Kiosk** involved.
3. Leave it past the normal blank/sleep timeout — confirm the screen stays awake.
4. Power-cycle — confirm it boots straight back into the display unattended.

## Known unknowns to confirm on the target image

These are intentionally flagged rather than guessed; verify on the Pi OS build you use:

- **Chromium package/binary name** — `chromium-browser` on Raspberry Pi OS; `chromium`
  elsewhere. The installer tries both; `sd-kiosk-launch` resolves whichever exists.
- **Chromium Wayland flags** — `--enable-features=UseOzonePlatform --ozone-platform=wayland`
  are set in `sd-kiosk-launch`. Some Chromium builds under `cage` auto-detect Wayland and
  need neither; if Chromium fails to start, try removing them.
- **Seat access** — with an autologin logind session, `cage` should find `seat0` without the
  `seat` group; `seatd` is installed as a fallback. If `cage` reports "could not create
  backend"/no seat, confirm `seatd` is running and the user's groups.
- **Boot partition path** — `/boot/firmware/` on Bookworm, `/boot/` on older images. The
  installer and launcher handle both.

## Why not just use the display's built-in browser?

Some smart displays can point their own browser at the server and skip the Pi entirely — *if* that
browser is current enough to render the Canvas app. In practice many aren't: the **Samsung QMR's
Tizen MagicINFO URL Launcher is too old** and fails to render the app (which is exactly why this
appliance exists). When the built-in browser can't do it, this thin-client Pi is the fix.
Pieria's **e-ink image endpoint** also lets such limited panels show art by fetching a
server-rendered *image* instead of running the app — see the main README's e-ink section.

## Fallback: X11 instead of cage

On a Pi or image where `cage`/Wayland Chromium misbehaves, the classic X11 recipe works:
`xserver-xorg xinit openbox unclutter`, a `.xinitrc` that runs `xset s off -dpms` then
`chromium-browser --kiosk <url>`, started via `startx` from the same tty1 login hook. Not
shipped in v1 to keep one clean path; documented here as the escape hatch.
