# common.sh — shared helpers for the first-run setup path. SOURCED, never executed.
# Used by sd-setup-pre (runs before NetworkManager) and sd-setup-boot (runs after it). Both need the
# same answer to "is this box configured?", so the rule lives here once rather than drifting in two
# copies. Installed to /usr/local/share/screen-docent/setup/common.sh by install.sh.

SD_SETUP_DIR=/usr/local/share/screen-docent/setup
# The NetworkManager drop-in that hands wlan0 to hostapd for the setup AP. Written by sd-setup-pre
# BEFORE NM starts; removed the moment the box is configured. See ADR-056.
SD_DROPIN=/etc/NetworkManager/conf.d/99-screen-docent-setup.conf

# Resolve the boot-partition conf path: honour an explicit argument, else probe the usual mounts.
sd_resolve_conf() {
  local c="${1:-}"
  if [ -n "$c" ] && [ -e "$c" ]; then echo "$c"; return; fi
  local d
  for d in /boot/firmware /boot; do
    if [ -e "$d/screen-docent.conf" ]; then echo "$d/screen-docent.conf"; return; fi
  done
  echo "${c:-/boot/firmware/screen-docent.conf}"
}

# Configured = the conf exists AND carries non-placeholder SERVER_URL + DISPLAY_ID. The shipped example
# uses DISPLAY_ID=living_room / SERVER_URL=http://192.168.1.50:8000 — treat those as "not set yet".
sd_is_configured() {
  local CONF="${1:-}" SERVER_URL="" DISPLAY_ID=""
  [ -r "$CONF" ] || return 1
  # shellcheck disable=SC1090
  . "$CONF" 2>/dev/null || return 1
  [ -n "${SERVER_URL:-}" ] && [ -n "${DISPLAY_ID:-}" ] || return 1
  [ "${DISPLAY_ID}" = "living_room" ] && [ "${SERVER_URL}" = "http://192.168.1.50:8000" ] && return 1
  return 0
}
