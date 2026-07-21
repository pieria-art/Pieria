#!/usr/bin/env bash
# Screen Docent — Appliance provisioner
#
# Turns a fresh Raspberry Pi OS Lite (64-bit, Bookworm) install into a kiosk
# that boots straight into the Screen Docent Canvas display, fullscreen, with
# no browser chrome and no Fully Kiosk. Idempotent — safe to re-run.
#
# Default: display-only (thin client → server elsewhere). Optionally also runs
# the server on THIS box (all-in-one) when ALL_IN_ONE=1 is set in the config.
#
# Usage:  sudo deploy/appliance/install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root:  sudo $0" >&2
  exit 1
fi

KIOSK_USER="${KIOSK_USER:-kiosk}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN_SRC="$HERE/bin"
UNIT_SRC="$HERE/systemd"
SETUP_SRC="$HERE/setup"
CONF_EXAMPLE="$HERE/config/screen-docent.conf.example"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# Read an existing appliance config if one is already present (e.g. placed on
# the boot partition before first boot) so we honor ALL_IN_ONE / GEMINI_API_KEY / EINK_ENABLED.
ALL_IN_ONE=0
GEMINI_API_KEY=""
EINK_ENABLED=0
for d in /boot/firmware /boot /etc; do
  if [ -r "$d/screen-docent.conf" ]; then
    # shellcheck disable=SC1090
    . "$d/screen-docent.conf"
    break
  fi
done

echo "==> Installing packages (cage, seatd, chromium, curl, avahi, wlr-randr)"
export DEBIAN_FRONTEND=noninteractive
apt-get update
# chromium-browser is the Raspberry Pi OS package; plain `chromium` on others.
# avahi-daemon powers <hostname>.local (mDNS) so users reach the box by name, not IP.
# wlr-randr applies the ROTATE= display rotation for portrait/rotated panels.
# cec-utils provides cec-client, used by sd-quiet-hours to power the TV off/on over HDMI-CEC
# (Night & Quiet Hours). Non-fatal if unavailable — the Canvas software blackout still applies.
apt-get install -y --no-install-recommends cage seatd curl avahi-daemon wlr-randr cec-utils \
  || { echo "package install failed" >&2; exit 1; }
if ! apt-get install -y --no-install-recommends chromium-browser; then
  apt-get install -y --no-install-recommends chromium
fi

echo "==> Enabling seatd"
systemctl enable --now seatd || true

echo "==> Enabling avahi-daemon (mDNS / <hostname>.local discovery)"
systemctl enable --now avahi-daemon || true

echo "==> Creating kiosk user: $KIOSK_USER"
if ! id "$KIOSK_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$KIOSK_USER"
fi
# Group access wlroots/cage wants for GPU, input, and seat management.
for g in video render input seat tty; do
  if getent group "$g" >/dev/null 2>&1; then
    usermod -aG "$g" "$KIOSK_USER" || true
  fi
done

echo "==> Installing launch scripts to /usr/local/bin"
install -m 0755 "$BIN_SRC/sd-kiosk-launch"   /usr/local/bin/sd-kiosk-launch
install -m 0755 "$BIN_SRC/sd-wait-for-server" /usr/local/bin/sd-wait-for-server
install -m 0755 "$BIN_SRC/sd-rotate-keep"    /usr/local/bin/sd-rotate-keep
install -m 0755 "$BIN_SRC/sd-metrics"        /usr/local/bin/sd-metrics
install -m 0755 "$BIN_SRC/sd-quiet-hours"    /usr/local/bin/sd-quiet-hours
install -m 0755 "$BIN_SRC/sd-watchdog"       /usr/local/bin/sd-watchdog
install -m 0755 "$BIN_SRC/sd-setup-boot"     /usr/local/bin/sd-setup-boot
install -m 0755 "$BIN_SRC/sd-setup-pre"      /usr/local/bin/sd-setup-pre
install -m 0755 "$BIN_SRC/sd-net-recover"    /usr/local/bin/sd-net-recover
install -m 0755 "$SETUP_SRC/sd_setup.py"     /usr/local/bin/sd-setup
install -m 0755 "$BIN_SRC/sd-update"         /usr/local/bin/sd-update
install -m 0755 "$BIN_SRC/sd-eink"           /usr/local/bin/sd-eink
install -m 0755 "$BIN_SRC/sd-image-prep"     /usr/local/bin/sd-image-prep

echo "==> Enabling a persistent (but size-capped) journal"
# An appliance that fails at a customer's house is debugged from its PREVIOUS boot — a first-run setup
# that failed, a kiosk that crashed and rebooted. Raspberry Pi OS ships a volatile journal, so all of
# that vanishes on reboot: after the 2026-07-21 setup-mode test the failing boot's logs were simply
# gone (ADR-056). Cap it hard — this is an SD card, and unbounded logging is how you wear one out.
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/screen-docent.conf <<'EOF'
# Screen Docent — keep logs across reboots so a failed boot can be diagnosed after the fact,
# but bound the size: this is flash, not a server disk.
[Journal]
Storage=persistent
SystemMaxUse=64M
SystemMaxFileSize=8M
MaxRetentionSec=1month
EOF
install -d -m 2755 -g systemd-journal /var/log/journal 2>/dev/null || install -d /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null || true
systemctl restart systemd-journald 2>/dev/null || true
echo "    journal is persistent, capped at 64M."

echo "==> Installing boot splash (shows the admin URL while the server starts)"
install -d /usr/local/share/screen-docent
install -m 0644 "$HERE/share/sd-splash.html" /usr/local/share/screen-docent/sd-splash.html
# The shipped placeholder conf — sd-image-prep falls back to this when resetting a box to setup mode.
install -m 0644 "$CONF_EXAMPLE" /usr/local/share/screen-docent/screen-docent.conf.example

echo "==> Installing udev rule (suppress the HDMI-CEC phantom pointer / stray cursor)"
install -m 0644 "$HERE/udev/99-screen-docent-no-cec-pointer.rules" \
  /etc/udev/rules.d/99-screen-docent-no-cec-pointer.rules
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=input --action=change 2>/dev/null || true

echo "==> Configuring tty1 autologin for $KIOSK_USER"
install -d /etc/systemd/system/getty@tty1.service.d
sed "s/__KIOSK_USER__/$KIOSK_USER/g" "$UNIT_SRC/autologin.conf" \
  > /etc/systemd/system/getty@tty1.service.d/autologin.conf

echo "==> Installing login hook (launches kiosk on tty1)"
PROFILE="/home/$KIOSK_USER/.bash_profile"
if ! grep -q "Screen Docent kiosk launch" "$PROFILE" 2>/dev/null; then
  cat >> "$PROFILE" <<'EOF'

# Screen Docent kiosk launch
if [ -z "${DISPLAY:-}" ] && [ "$(tty)" = "/dev/tty1" ]; then
  exec sd-kiosk-launch
fi
EOF
fi
chown "$KIOSK_USER:$KIOSK_USER" "$PROFILE"

echo "==> Seeding config on the boot partition"
BOOT_CONF=""
for d in /boot/firmware /boot; do
  if [ -d "$d" ]; then BOOT_CONF="$d/screen-docent.conf"; break; fi
done
[ -z "$BOOT_CONF" ] && BOOT_CONF="/boot/firmware/screen-docent.conf"
if [ ! -e "$BOOT_CONF" ]; then
  install -m 0644 "$CONF_EXAMPLE" "$BOOT_CONF"
  echo "    Wrote $BOOT_CONF  (edit SERVER_URL and DISPLAY_ID before reboot)"
else
  echo "    $BOOT_CONF already exists — leaving it untouched"
fi

echo "==> Installing first-run setup wizard (assets only — enabled on the .img, NOT on this box)"
# The wizard writes screen-docent.conf on a fresh flash so a non-technical user never edits files.
# We install the assets everywhere but keep sd-setup.service DISABLED here: enabling it is the .img
# build's job (a flashed card boots into setup once, then never again). hostapd/dnsmasq power the
# Docent-Setup AP but are kept disabled so they never fight a working box's network.
install -d /usr/local/share/screen-docent/setup
install -m 0644 "$SETUP_SRC/common.sh"    /usr/local/share/screen-docent/setup/common.sh
install -m 0644 "$SETUP_SRC/hostapd.conf" /usr/local/share/screen-docent/setup/hostapd.conf
install -m 0644 "$SETUP_SRC/dnsmasq.conf" /usr/local/share/screen-docent/setup/dnsmasq.conf
apt-get install -y --no-install-recommends hostapd dnsmasq iw \
  || echo "    (hostapd/dnsmasq unavailable — the setup AP won't come up, but a pre-seeded conf still works)"
systemctl disable --now hostapd dnsmasq 2>/dev/null || true
sed "s#__BOOT_CONF__#$BOOT_CONF#g" "$UNIT_SRC/sd-setup.service" > /etc/systemd/system/sd-setup.service
# sd-setup-pre is ENABLED everywhere, unlike sd-setup.service. It is inert on a configured box (it only
# removes a stale drop-in) and refuses to unmanage wlan0 unless sd-setup.service is enabled — and being
# always-on is exactly what makes the setup-mode radio hand-off self-healing (ADR-056).
sed "s#__BOOT_CONF__#$BOOT_CONF#g" "$UNIT_SRC/sd-setup-pre.service" > /etc/systemd/system/sd-setup-pre.service
systemctl daemon-reload
systemctl enable sd-setup-pre.service 2>/dev/null \
  || echo "    (could not enable sd-setup-pre.service)"
echo "    sd-setup.service installed but left DISABLED (the .img build enables it for out-of-box setup)."
echo "    sd-setup-pre.service installed and ENABLED (safe on a configured box; self-heals the drop-in)."
# The anti-brick backstop: re-opens the wizard if a CONFIGURED box can't get online (ADR-057). Enabled
# everywhere — on an unconfigured box it exits immediately, since sd-setup-boot owns that case.
sed "s#__BOOT_CONF__#$BOOT_CONF#g" "$UNIT_SRC/sd-net-recover.service" > /etc/systemd/system/sd-net-recover.service
systemctl daemon-reload
systemctl enable sd-net-recover.service 2>/dev/null || echo "    (could not enable sd-net-recover.service)"
echo "    sd-net-recover.service installed and ENABLED (anti-brick: re-opens setup if never online)."

echo "==> Disabling console blanking"
for CMDLINE in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
  if [ -f "$CMDLINE" ]; then
    grep -q "consoleblank=0" "$CMDLINE" || sed -i 's/[[:space:]]*$/ consoleblank=0/' "$CMDLINE"
    break
  fi
done

if [ "${ALL_IN_ONE:-0}" = "1" ]; then
  echo "==> All-in-one mode: provisioning the Screen Docent server on this box"
  OVERRIDE="$HERE/compose/docker-compose.appliance.yml"

  if ! command -v docker >/dev/null 2>&1; then
    echo "    Installing Docker (official convenience script)..."
    curl -fsSL https://get.docker.com | sh
  fi
  systemctl enable --now docker || true
  usermod -aG docker "$KIOSK_USER" || true

  # The base compose mounts env_file: .env — write it (with the Gemini key if given).
  if [ -n "${GEMINI_API_KEY:-}" ]; then
    ( umask 077; printf 'GEMINI_API_KEY=%s\n' "$GEMINI_API_KEY" > "$REPO_ROOT/.env" )
    echo "    Wrote $REPO_ROOT/.env"
  else
    echo "    WARNING: GEMINI_API_KEY not set in config — AI features will be unavailable" >&2
    [ -f "$REPO_ROOT/.env" ] || ( umask 077; : > "$REPO_ROOT/.env" )
  fi

  # The image runs as non-root (uid 1000, Phase 1 C1). The bind-mounted data/ + Artwork/ MUST be owned
  # by 1000 or the container can't write the DB — migrations fail and every DB endpoint 500s (a box first
  # set up under the OLD root container leaves these root-owned; this reconciles it, idempotently, BEFORE
  # the container boots so the very first migration can write).
  mkdir -p "$REPO_ROOT/data" "$REPO_ROOT/Artwork"
  chown -R 1000:1000 "$REPO_ROOT/data" "$REPO_ROOT/Artwork" || true

  echo "    Building & starting the stack (first run downloads + builds; be patient)..."
  ( cd "$REPO_ROOT" && docker compose -f docker-compose.yml -f "$OVERRIDE" up -d --build )
  echo "    Server is starting on http://localhost:8000 (restart: unless-stopped survives reboot)"

  echo "==> Installing host metrics timer (Device Health throttle/under-voltage reading)"
  sed "s#__REPO_ROOT__#$REPO_ROOT#g" "$UNIT_SRC/sd-metrics.service" > /etc/systemd/system/sd-metrics.service
  install -m 0644 "$UNIT_SRC/sd-metrics.timer" /etc/systemd/system/sd-metrics.timer
  systemctl daemon-reload
  systemctl enable --now sd-metrics.timer || true

  echo "==> Installing quiet-hours HDMI-CEC panel power timer (Night & Quiet Hours)"
  sed "s#__BOOT_CONF__#$BOOT_CONF#g" "$UNIT_SRC/sd-quiet-hours.service" > /etc/systemd/system/sd-quiet-hours.service
  install -m 0644 "$UNIT_SRC/sd-quiet-hours.timer" /etc/systemd/system/sd-quiet-hours.timer
  systemctl daemon-reload
  systemctl enable --now sd-quiet-hours.timer || true

  echo "==> Installing kiosk/server watchdog (self-heal; ships in observe/log-only mode)"
  sed -e "s#__BOOT_CONF__#$BOOT_CONF#g" -e "s#__REPO_ROOT__#$REPO_ROOT#g" \
    "$UNIT_SRC/sd-watchdog.service" > /etc/systemd/system/sd-watchdog.service
  install -m 0644 "$UNIT_SRC/sd-watchdog.timer" /etc/systemd/system/sd-watchdog.timer
  systemctl daemon-reload
  # Safe to enable: WATCHDOG defaults to 'observe' (logs, never acts) until you set enforce in the conf.
  systemctl enable --now sd-watchdog.timer || true

  echo "==> Advertising the server over mDNS (friendly name in network browsers)"
  install -d /etc/avahi/services
  install -m 0644 "$HERE/avahi/screen-docent.service" /etc/avahi/services/screen-docent.service

  echo "==> Installing GUI update bridge (Admin -> Devices -> Maintenance)"
  sed "s#__REPO_ROOT__#$REPO_ROOT#g" "$UNIT_SRC/sd-update.path"    > /etc/systemd/system/sd-update.path
  sed "s#__REPO_ROOT__#$REPO_ROOT#g" "$UNIT_SRC/sd-update.service" > /etc/systemd/system/sd-update.service
  systemctl daemon-reload
  systemctl enable --now sd-update.path || true
fi

if [ "${EINK_ENABLED:-0}" = "1" ]; then
  echo "==> E-ink panel enabled (Track B): installing sd-eink host client + deps"
  # eink_client.py is stdlib+Pillow+httpx only (no app import) and runs host-side even with no local
  # container (satellite mode) — install it ALONGSIDE sd-eink so its own-directory sys.path trick finds
  # it (see the script's header comment). Works whether or not ALL_IN_ONE is set.
  install -m 0644 "$REPO_ROOT/eink_client.py" /usr/local/bin/eink_client.py

  echo "    Installing host Python deps (python3-pil via apt; httpx + inky[rpi] via pip)"
  apt-get install -y --no-install-recommends python3-pip python3-pil \
    || echo "    WARNING: python3-pip/python3-pil install failed — sd-eink may not run" >&2
  # Bookworm's system Python is externally-managed (PEP 668); --break-system-packages is the
  # documented escape hatch for a host-level script install like this one (not a packaged app venv).
  pip3 install --break-system-packages --no-cache-dir httpx "inky[rpi]" \
    || echo "    WARNING: pip install of httpx/inky failed — sd-eink may not run (Pi-gated: verify on the bench Pi)" >&2

  sed "s#__BOOT_CONF__#$BOOT_CONF#g" "$UNIT_SRC/sd-eink.service" > /etc/systemd/system/sd-eink.service
  systemctl daemon-reload
  systemctl enable --now sd-eink || true
fi

echo "==> Finalizing"
systemctl set-default multi-user.target   # boot to console; autologin does the rest
systemctl daemon-reload

cat <<EOF

Done.

  1. Edit the config:   $BOOT_CONF
       - SERVER_URL  -> your Screen Docent server (e.g. http://192.168.1.50:8000)
       - DISPLAY_ID  -> a unique name for this screen
  2. Reboot:            sudo reboot

The Pi will boot straight into the fullscreen display. If the server isn't
reachable yet the screen stays black and paints automatically once it is.

For ALL-IN-ONE (server on this box): set ALL_IN_ONE=1, GEMINI_API_KEY=...,
and SERVER_URL=http://localhost:8000 in the config, then re-run this script.
EOF
