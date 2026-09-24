# Backup & Restore

Pieria can back up everything on a device except the art itself, and restore it onto a fresh
install — the fast path back after a reflash (see the appliance image runbook,
[`docs/image-build.md`](image-build.md), for when you'd reflash in the first place).

From the admin dashboard: **Settings → 💾 Backup & Restore**.

## What's in a backup

- Your **library** — the masters for My Photos and anything you added/uploaded yourself.
- Your **settings** — playlists, the display schedule, subscriptions, AI Engine config, Frame TV
  config, catalog source, default playlist.
- Which **Art Packs** you have installed (by id, not their images — see below).
- On an appliance: a **few non-secret device settings** (time zone, screen orientation, self-heal
  watchdog mode, OS update schedule).

## What's never in a backup

- **API keys and your publisher signing key** — off by default. Tick **"Include API keys &
  publisher signing key"** to include them; you'll set a passphrase (12+ characters) and they're
  encrypted with it (PyNaCl argon2id + a secret box) before they ever touch the archive. **Without
  that passphrase the keys can't be recovered** — there's no backdoor, so write it down somewhere
  safe.
- **Art Pack images.** Pack art is re-downloaded after a restore instead of archived — it's already
  hosted, re-fetching it keeps backups small and avoids shipping someone else's copyrighted-adjacent
  bytes around in your backup file.
- `data/frame_tv_token.json`, `.env`, Wi-Fi credentials, and anything under `data/appliance/` — none
  of that is portable between devices.

## Restoring

Restore is a **full replace**: it overwrites this device's library and settings with the backup's,
not a merge. Steps:

1. **Settings → Backup & Restore → choose a `.tar` file.** It's uploaded and validated (checksums,
   schema version) before anything on the device changes — you'll see a summary (when it was made,
   how much it holds, whether it has encrypted keys) to confirm before continuing.
2. **Type RESTORE to confirm**, enter the backup's passphrase if it has encrypted keys (or choose
   "restore without keys").
3. **Restart the container** to apply it (`docker compose restart`, or on the appliance this
   happens automatically). The swap happens once, single-process, before the app starts serving —
   the same window Pieria already uses for schema migrations.
4. **Pack art re-downloads in the background** after the restart; a progress panel shows what's
   left, with a retry button for anything that failed (a flaky network, a pack pulled from the
   registry since).
5. On an appliance, an **"Apply device settings"** button appears if the backup carried device
   settings — it replays them through the normal Devices actions (time zone, orientation, etc.).

If the restored database fails to migrate (e.g. it's from a version too old for this app to bring
forward), the device automatically rolls back to what was running before the restore and boots
normally — a bad restore can't strand the box.

## The reflash path (ADR-138)

Flashing a fresh appliance image wipes the SD card. The sequence is: back up from the old install →
flash the new image → run the setup wizard → restore the backup from **Settings → Backup &
Restore**. Your library, playlists, and settings come back; pack art re-downloads itself.
