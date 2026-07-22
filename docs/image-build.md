# Building a distributable Screen Docent `.img`

How to bake a golden master that a stranger can flash, boot, and set up from a phone.

The first master (2026-07-21, ADR-060) proved the cycle works, but it was a snapshot of a bench box
that had been live-patched for twelve hours and it carried the maintainer's `authorized_keys`. That
one is a **dev artifact**. A shippable image is `install.sh` on a **fresh Raspberry Pi OS**, and this
document is the checklist for producing one.

The whole reason it's a checklist: every failure on the first cycle was *something still doing the old
thing while we believed it had changed* (ADR-059). Each phase below therefore ends with a **verify**
step that reads state back rather than assuming the previous command worked.

---

## 0. What you need

| | |
|---|---|
| **OS image** | `2025-05-13-raspios-bookworm-arm64-lite` — **Bookworm, not trixie.** Raspberry Pi Imager now defaults to trixie (python 3.13), which the e-ink stack does not support (ADR-053). Pick the older release explicitly under "Raspberry Pi OS (other)". |
| **Cards** | Two, ideally. One is workable (capture-then-flash-back, ADR-060) but leaves no fallback if the capture is bad. |
| **Reader** | A USB adapter. Note it presents as `/dev/sd*` — the same namespace as the laptop's own NVMe. **Confirm the device node before every destructive command.** |
| **Laptop space** | ~65 GB free: the raw `dd` is the full card size before `pishrink` shrinks it. |
| **Tools** | `pishrink`, `xz`, and a Gemini API key if you want AI features baked in. |

---

## 1. Flash the base OS

Use Raspberry Pi Imager with OS customisation **on**: set the hostname, create the `pi` user, and
configure Wi-Fi + SSH so the box is reachable for provisioning. All of it is wiped again in step 4 —
it exists only so you can drive the build.

> Don't skip the customisation and plan to "just plug in a keyboard." Provisioning needs the network
> anyway (apt, Docker, the R2 pack), so the box has to be online regardless.

**Verify:** `ssh pi@<host>.local` and `cat /etc/os-release` — expect `bookworm`, and
`python3 -V` should report **3.11.x**.

---

## 2. Provision

```bash
git clone https://github.com/<you>/Screen-Docent.git
cd Screen-Docent
sudo ALL_IN_ONE=1 EINK_ENABLED=1 GEMINI_API_KEY=... deploy/appliance/install.sh
```

**The flavour variables are the single most expensive thing to get wrong.** `install.sh` reads them
from `screen-docent.conf`, which does not exist on a fresh box — so without the env overrides it
provisions a **thin client**: no Docker, no `sd-app.service`, no clock gate. A card baked that way can
never become all-in-one no matter what the first-run wizard writes, because the machinery was never
installed. The script prints the flavour it chose in its first line; **read that line.**

Drop `EINK_ENABLED=1` if this image is for HDMI panels only.

**Verify:** the run ends with an `==> Installed state` block read back from systemd. Every unit must
say `enabled`, except `sd-setup.service`, which is correctly `disabled` at this stage — step 4 enables
it. Anything marked `<-- NOT ENABLED` must be fixed before you continue; a missing unit here is a card
that boots into nothing.

---

## 3. Prove the box works, then empty it

Configure `/boot/firmware/screen-docent.conf` for real (`SERVER_URL=http://localhost:8000`,
`ALL_IN_ONE=1`, a `DISPLAY_ID`, `ROTATE`/`EINK_ORIENTATION` to taste), reboot, and confirm on glass:
art on HDMI, art on the e-ink panel, `http://<host>.local:8000` serving the admin UI.

Then **empty it**, so first boot exercises the real out-of-box path rather than shipping a
pre-populated library:

```bash
cd ~/Screen-Docent
docker compose -f docker-compose.yml -f deploy/appliance/compose/docker-compose.appliance.yml down
sudo rm -rf data/screen_docent.db Artwork/*
```

Leaving art baked in is a legitimate alternative (a "lean Core" image — faster to first paint, much
bigger download). Decide deliberately; don't let it happen by accident.

**Verify:** `du -sh Artwork/` is ~0, and the DB file is gone. The container image itself stays on the
card — that is what makes first boot fast.

---

## 4. Sysprep

```bash
sudo POWEROFF=1 sd-image-prep --full
```

This resets the boot conf to the placeholder (so first boot enters the wizard), enables
`sd-setup.service`, wipes saved Wi-Fi, wipes machine identity, re-arms SSH host-key regeneration,
removes every `authorized_keys`, cleans logs, and stamps the `fake-hwclock` floor.

- **`POWEROFF=1` is not optional in practice.** `--full` deletes the SSH host keys, destroying its own
  remote access — without it your only remaining option is pulling the plug, which images a dirty ext4
  journal into every unit you ever flash.
- **Never pass `KEEP_AUTHORIZED_KEYS=1` for a distributable image.** It ships your key to every unit in
  the world. It exists only for private dev masters.

**Verify:** read the command's own output before it powers off — it names the host-key unit it armed,
the count of `authorized_keys` files removed, and the clock floor it stamped. If it warns that it
could not arm host-key regeneration, **stop**: every flashed unit will refuse SSH forever.

---

## 5. Capture

With the card in the laptop reader, and after confirming the device node:

```bash
lsblk -o NAME,SIZE,MODEL,TRAN            # CONFIRM which /dev/sdX is the card
sudo dd if=/dev/sdX of=~/docent-img/docent-master.img bs=4M status=progress conv=fsync
sudo pishrink -Z ~/docent-img/docent-master.img ~/docent-img/docent-release.img.xz
```

`pishrink` shrinks the filesystem to its used size (~59 G → ~6 G), injects a first-boot auto-expand,
and runs `e2fsck -pf` on the way — which also cleans an unclean journal, though that is a safety net,
not a licence to pull the plug in step 4.

**Verify:** the shrunk image is a plausible size, and `xz -t` passes on the compressed file.

---

## 6. Flash a card and do the gramps test

Flash the release image to a card, put it in a Pi that has **never** been provisioned, and run the
whole thing as a stranger would: no SSH, no keyboard, phone only.

Expect: boot → auto-expand → first-run setup → e-ink setup card with the join QR → `Docent-Setup` AP →
captive portal → SSID picker → commit → reboot → joins Wi-Fi → `sd-app.service` creates the container
from nothing → migrations on an empty DB → first pack pulls from R2 → paints.

**Verify — and this is the actual gate:**

- Art on glass with **no intervention at any point**.
- `ssh pi@<host>.local` is refused for *your* key (proving no `authorized_keys` shipped) but the host
  presents its **own freshly generated** host key (proving regeneration was armed).
- `timedatectl` shows `NTPSynchronized=yes`, and `journalctl -u sd-timesync-wait` shows the gate ran.
- `journalctl -u sd-app` shows the container being **created**, not merely started.

---

## The traps, in one place

Each of these has already cost real time.

| Trap | What it looks like | Guard |
|---|---|---|
| Flavour not set on a fresh box | Finished card serves nothing; wizard's all-in-one choice does nothing | `install.sh` prints the flavour first and warns; pass `ALL_IN_ONE=1` |
| trixie instead of Bookworm | e-ink stack won't install (python 3.13) | Pick the OS release explicitly in Imager |
| Host-key regeneration not armed | Every flashed unit refuses SSH, permanently | `--full` reports which unit it armed; read it (ADR-060) |
| `authorized_keys` shipped | Every unit in the world trusts your laptop | `--full` removes it and prints the count |
| Plug pulled after `--full` | Dirty journal imaged into every unit | `POWEROFF=1` |
| No container in the master | Boots with no app, forever — `unless-stopped` can't create one | `sd-app.service` (ADR-060) |
| Stale clock on a months-old image | "Registry unreachable"; TLS fails as *not yet valid* | `sd-timesync-wait` + seed retry (ADR-061/062) |
| Wrong `/dev/sd*` | You overwrite your laptop | `lsblk` before every destructive command |

## Related

- **ADR-056** boot race · **ADR-057** anti-brick `sd-net-recover` · **ADR-058** setup state on both
  surfaces · **ADR-059** what the first real gramps cycle found · **ADR-060** the capture/flash cycle ·
  **ADR-061** out-of-box seed retry · **ADR-062** the first-boot clock.
- `deploy/appliance/README.md` — what each unit does.
