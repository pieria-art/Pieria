# common.sh — shared helpers for the first-run setup path. SOURCED, never executed.
# Used by sd-setup-pre (before NetworkManager), sd-setup-boot (first-run wizard) and sd-net-recover
# (re-opens the wizard when a CONFIGURED box can't get online). All three need the same answers to
# "is this box configured?" and "how do I take/return the radio", so that logic lives here once rather
# than drifting in three copies — the is_configured duplication is what made ADR-054 hard to reason
# about. Installed to /usr/local/share/screen-docent/setup/common.sh by install.sh.

SD_SETUP_DIR=/usr/local/share/screen-docent/setup
# The NetworkManager drop-in that hands wlan0 to hostapd. Written by sd-setup-pre BEFORE NM starts on a
# setup boot (that ordering is the whole point — see ADR-056), and at runtime by sd-net-recover, where
# there is no race to lose because NM has long since settled.
SD_DROPIN=/etc/NetworkManager/conf.d/99-screen-docent-setup.conf
SD_AP_ADDR=10.0.0.1/24

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

# Does wlan0 currently hold an IPv4 address? This — NOT internet reachability — is our definition of
# "online". An all-in-one box on a router with no WAN is perfectly healthy: it serves its own art. We
# only want to intervene when the device has no network at all.
sd_have_ip() {
  local dev="${1:-wlan0}"
  [ -n "$(ip -4 -br addr show "$dev" 2>/dev/null | awk '{print $3}')" ]
}

# Bring up the Docent-Setup AP + captive DNS/DHCP. $1 = log file (never /dev/null — ADR-056).
# Idempotent with respect to the drop-in, so it is safe on both the setup-boot path (where sd-setup-pre
# already wrote it) and the recovery path (where it must be created on the fly).
sd_start_ap() {
  local LOG="${1:-/var/log/sd-setup-ap.log}"
  rfkill unblock wlan 2>>"$LOG" || true
  install -d /etc/NetworkManager/conf.d
  cat > "$SD_DROPIN" <<'EOF'
# Screen Docent setup mode — wlan0 is handed to hostapd for the Docent-Setup AP.
[keyfile]
unmanaged-devices=interface-name:wlan0
EOF
  nmcli general reload 2>>"$LOG" || true
  nmcli device disconnect wlan0 2>>"$LOG" || true
  nmcli device set wlan0 managed no 2>>"$LOG" || true
  # wpa_supplicant holds a claim on the radio INDEPENDENTLY of NetworkManager — unmanaging wlan0 in NM
  # does not release it. Stop it too, or hostapd can be refused the interface.
  systemctl stop wpa_supplicant.service 2>>"$LOG" || true
  # Bounce the link: the radio may be parked on a 5 GHz channel from a prior association, and the AP is
  # 2.4 GHz ch6.
  ip link set wlan0 down 2>>"$LOG" || true
  ip link set wlan0 up 2>>"$LOG" || true
  ip addr flush dev wlan0 2>>"$LOG" || true
  ip addr add "$SD_AP_ADDR" dev wlan0 2>>"$LOG" || true
  # hostapd.service ships MASKED on Bookworm, so `systemctl start hostapd` could only ever fail — run
  # the binary, and LOG its errors rather than discarding them.
  hostapd -B -f "$LOG" "$SD_SETUP_DIR/hostapd.conf" \
    || echo "sd: hostapd failed to start — see $LOG" >&2
  sleep 3
  if pgrep -f "hostapd.*$SD_SETUP_DIR" >/dev/null 2>&1; then
    echo "sd: AP up — $(iw dev wlan0 info 2>/dev/null | tr '\n' ' ')"
  else
    echo "sd: AP DID NOT START. Last 20 lines of $LOG:" >&2
    tail -20 "$LOG" >&2 2>/dev/null || true
    return 1
  fi
  dnsmasq --conf-file="$SD_SETUP_DIR/dnsmasq.conf" 2>>"$LOG" \
    || echo "sd: dnsmasq failed to start — see $LOG" >&2
}

# Tear the AP down and give wlan0 back to NetworkManager. Safe to call when no AP is running.
# $1 = optional NM profile to explicitly reactivate (recovery path re-joins the saved Wi-Fi).
sd_stop_ap() {
  local PROFILE="${1:-}"
  # Do NOTHING unless an AP is actually up. This used to tear down unconditionally, so merely stopping
  # sd-net-recover on a HEALTHY box ran `ip addr flush dev wlan0` and knocked it off the network — it
  # took the bench Pi offline and out of reach (2026-07-21). Teardown must be safe by construction, not
  # by every caller remembering to check.
  if ! pgrep -f "hostapd.*$SD_SETUP_DIR" >/dev/null 2>&1 && [ ! -e "$SD_DROPIN" ]; then
    return 0
  fi
  # Match only OUR processes — a bare `pkill dnsmasq` would kill an unrelated system resolver.
  pkill -f "dnsmasq.*$SD_SETUP_DIR" 2>/dev/null || true
  pkill -f "hostapd.*$SD_SETUP_DIR" 2>/dev/null || true
  ip addr flush dev wlan0 2>/dev/null || true
  rm -f "$SD_DROPIN"
  systemctl start wpa_supplicant.service 2>/dev/null || true
  nmcli general reload 2>/dev/null || true
  nmcli device set wlan0 managed yes 2>/dev/null || true
  # Reactivate the saved Wi-Fi — but WAIT for NM to actually take wlan0 back first, and pin the
  # interface. Firing immediately after `managed yes` failed outright: the device was still
  # transitioning, so nmcli picked eth0 and reported "mismatching interface name". The box recovered
  # anyway, but only because NM's own autoconnect fired 3s later — meaning this call was decorative and
  # would have silently done nothing for a profile without autoconnect (observed 2026-07-21, ADR-057).
  if [ -n "$PROFILE" ]; then
    local i state
    for i in $(seq 1 20); do
      state="$(nmcli -t -f DEVICE,STATE device status 2>/dev/null | awk -F: '$1=="wlan0"{print $2}')"
      case "$state" in
        disconnected|connected|connecting*) break ;;
      esac
      sleep 1
    done
    nmcli con up "$PROFILE" ifname wlan0 2>/dev/null || true
  fi
  return 0
}
