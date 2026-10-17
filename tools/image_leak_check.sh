#!/usr/bin/env bash
# image_leak_check.sh — offline credential/identity gate for a captured Pieria master image.
#
# Usage:  sudo tools/image_leak_check.sh [--scrub] <raw.img>      (run BEFORE pishrink)
#
# WHY IT EXISTS: sd-image-prep --full scrubs the LIVE system, but NetworkManager rewrites its state
# (seen-bssids, timestamps, leases) during shutdown, AFTER the scrub. Only the powered-off card shows what
# actually shipped, so this is the authoritative gate before an image is distributed. It also catches
# anything sysprep missed (cloud-init seed, netplan Wi-Fi PSKs, password hashes, authorized_keys, .env).
#
# Read-only by default (losetup -r, mount -o ro) — that mode FAILs any image not yet scrubbed (a live
# journal always exists), so use it to re-check an already-scrubbed image. --scrub mounts read-write and
# removes what the box rewrote during shutdown (NM state, shell history, logs), then checks. It never prints secret values — only paths and key names.
# Exit 0 + "LEAK CHECK: PASS" or exit 1 + "LEAK CHECK: FAIL (n)".
set -euo pipefail

SCRUB=0
if [ "${1:-}" = "--scrub" ]; then SCRUB=1; shift; fi
IMG="${1:-}"

if [ "$(id -u)" -ne 0 ]; then echo "Please run as root:  sudo $0 $*" >&2; exit 1; fi
if [ -z "$IMG" ] || [ ! -f "$IMG" ]; then
  echo "Usage: sudo $0 [--scrub] <raw.img>   (argument must be a regular file)" >&2; exit 1
fi

WORK="$(mktemp -d)"
LOOP=""
cleanup() {
  set +e
  mountpoint -q "$WORK/boot" && umount "$WORK/boot"
  mountpoint -q "$WORK/root" && umount "$WORK/root"
  [ -n "$LOOP" ] && losetup -d "$LOOP"
  rmdir "$WORK/boot" "$WORK/root" "$WORK" 2>/dev/null
}
trap cleanup EXIT

if [ "$SCRUB" = 1 ]; then
  LOOP="$(losetup -P --show -f "$IMG")"; MOPT="rw"
else
  LOOP="$(losetup -r -P --show -f "$IMG")"; MOPT="ro"
fi
mkdir -p "$WORK/boot" "$WORK/root"
mount -o "$MOPT" "${LOOP}p1" "$WORK/boot"
mount -o "$MOPT" "${LOOP}p2" "$WORK/root"
B="$WORK/boot"; R="$WORK/root"

# Absolute symlinks inside the image would resolve to THIS laptop's files (truncate/grep the host).
for d in var/log var/lib/NetworkManager home root; do
  if [ -L "$R/$d" ]; then echo "REFUSING: $d is a symlink inside the image" >&2; exit 1; fi
done

FAILS=0
fail() { echo "  LEAK: $*"; FAILS=$((FAILS + 1)); }
rel() { echo "${1#"$WORK"/}"; }
nonempty() { [ -s "$1" ]; }

if [ "$SCRUB" = 1 ]; then
  for f in "$R"/var/lib/NetworkManager/{seen-bssids,timestamps,secret_key,*.lease,*.leases,internal-*}; do
    if [ -e "$f" ]; then rm -f "$f"; echo "  scrubbed: $(rel "$f")"; fi
  done
  # The operator's shell rewrites its history on SIGHUP at poweroff.
  for f in "$R"/home/*/.bash_history "$R"/root/.bash_history; do
    [ -L "$f" ] && continue
    if [ -s "$f" ]; then : > "$f"; echo "  truncated: $(rel "$f")"; fi
  done
  # NM/wpa_supplicant log the SSID/BSSID at shutdown, after clean_logs ran.
  if [ -d "$R/var/log/journal" ]; then
    find "$R/var/log/journal" -mindepth 1 -delete 2>/dev/null || true
    echo "  emptied: var/log/journal"
  fi
  if [ -d "$R/var/log" ]; then
    find "$R/var/log" -type f \( -name '*.gz' -o -name '*.xz' -o -name '*.1' -o -name '*.old' \) -delete
    find "$R/var/log" -type f -exec truncate -s0 {} +
    echo "  truncated regular files under var/log (rotated *.gz/*.xz/*.1/*.old deleted)"
  fi
fi

# --- boot partition ---
for f in user-data network-config meta-data firstrun.sh userconf.txt wpa_supplicant.conf; do
  [ -e "$B/$f" ] && fail "boot/$f present"
done

# --- rootfs: network ---
if [ -d "$R/etc/netplan" ]; then
  for f in "$R"/etc/netplan/*; do
    [ -e "$f" ] || continue
    keys=$(grep -oE '^\s*(psk|password|hashed_passwd|passwd|ssh_authorized_keys)' "$f" 2>/dev/null | tr -d ' \t' | sort -u | paste -sd, - || true)
    fail "$(rel "$f") present${keys:+ (keys: $keys)}"
  done
fi
if [ -d "$R/etc/NetworkManager/system-connections" ]; then
  for f in "$R"/etc/NetworkManager/system-connections/*; do
    [ -e "$f" ] && fail "$(rel "$f") present (saved connection)"
  done
fi
for f in "$R"/etc/wpa_supplicant/*.conf; do
  [ -e "$f" ] || continue
  grep -qE 'psk=' "$f" 2>/dev/null && fail "$(rel "$f") contains psk="
done
for f in "$R"/var/lib/NetworkManager/{seen-bssids,timestamps,secret_key,*.lease,*.leases,internal-*}; do
  [ -e "$f" ] && fail "$(rel "$f") present (NM state; use --scrub)"
done

# --- rootfs: users / keys ---
for f in "$R"/home/*/.ssh/authorized_keys "$R"/home/*/authorized_keys "$R"/root/.ssh/authorized_keys "$R"/root/authorized_keys; do
  [ -e "$f" ] && fail "$(rel "$f") present"
done
if [ -f "$R/etc/passwd" ] && [ -f "$R/etc/shadow" ]; then
  while IFS=: read -r user _ uid _ _ _ _; do
    [ "$uid" -ge 1000 ] 2>/dev/null || continue
    [ "$uid" -ge 65534 ] && continue
    pw=$(awk -F: -v u="$user" '$1==u{print $2}' "$R/etc/shadow")
    case "$pw" in
      '!*'|'*'|'!') ;;
      *) fail "etc/shadow: user '$user' has a real password hash" ;;
    esac
  done < "$R/etc/passwd"
fi

# --- rootfs: identity / state ---
for d in "$R"/var/lib/cloud/instance "$R"/var/lib/cloud/instances; do
  [ -e "$d" ] && fail "$(rel "$d") present (cloud-init instance state)"
done
for f in "$R/etc/sudoers.d/90-cloud-init-users" "$R/etc/ssh/sshd_config.d/50-cloud-init.conf"; do
  [ -e "$f" ] && fail "$(rel "$f") present (cloud-init generated)"
done
if [ -d "$R/var/log/journal" ]; then
  while IFS= read -r f; do fail "$(rel "$f") present (journal must be empty)"; done \
    < <(find "$R/var/log/journal" -type f)
fi
if [ -d "$R/var/log" ]; then
  while IFS= read -r f; do fail "$(rel "$f") mentions bssid/SSID"; done \
    < <(grep -rlaE 'bssid|SSID' "$R/var/log" 2>/dev/null || true)
fi
for f in "$R"/etc/ssh/ssh_host_*; do
  [ -e "$f" ] && fail "$(rel "$f") present (host keys must be absent)"
done
[ -e "$R/etc/systemd/system/ssh.service.d/10-pieria-hostkeys.conf" ] \
  || fail "etc/systemd/system/ssh.service.d/10-pieria-hostkeys.conf missing (no host-key generation: owners could never enable SSH)"
if [ -s "$R/etc/machine-id" ]; then
  mid=$(tr -d '[:space:]' < "$R/etc/machine-id")
  [ "$mid" != "uninitialized" ] && fail "etc/machine-id is set (must be empty or 'uninitialized')"
fi
for f in "$R"/home/*/.bash_history "$R"/root/.bash_history; do
  [ -e "$f" ] && nonempty "$f" && fail "$(rel "$f") is non-empty"
done
for f in "$R"/home/*/Pieria/.env "$R"/home/*/*/Pieria/.env; do
  [ -f "$f" ] || continue
  grep -qE '^\s*GEMINI_API_KEY=\S' "$f" 2>/dev/null && fail "$(rel "$f") has a non-empty GEMINI_API_KEY"
done

if [ "$FAILS" -eq 0 ]; then
  echo "LEAK CHECK: PASS"
else
  echo "LEAK CHECK: FAIL ($FAILS)"
  exit 1
fi
