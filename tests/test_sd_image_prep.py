"""sd-image-prep's `wipe_secrets` step (the `--full` capture-time secret sweep).

`sd-image-prep --full` is genuinely destructive (require_root, wipes SSH host keys, locks every human
login, disables sshd, resets the boot hostname, ...) and its `repo_root()` helper has no test override —
it shells out to `systemctl show` or globs `/home/*/Pieria` / `/root/Pieria`. None of that is safe or
even possible to drive from a dev-box test, so this suite does NOT invoke the CLI (`--enable-setup` /
`--full`) at all. Instead it sources the real script (so `wipe_secrets` is the actual function body, not
a reimplementation) into a throwaway `bash -c` subprocess, overrides `repo_root` to point at a tmp
directory, and calls `wipe_secrets` directly.

Sourcing quirk this works around: with no positional args, sourcing the script re-runs its own trailing
`case "${1:-}" in ... "" ) sed -n '2,30p' "$0" ...` branch (the --help text) as a side effect of
sourcing — under the script's own `set -euo pipefail` a failure there would abort the whole subprocess.
Passing the script's own path as the subprocess's argv[0] (so `$0` is a real, readable file inside the
script) keeps that branch a no-op instead of a hard failure.
"""

import pathlib
import sqlite3
import subprocess

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BIN = _ROOT / "deploy" / "appliance" / "bin" / "sd-image-prep"


def _make_settings_db(path: pathlib.Path, *, ai_api_key: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (setting_key TEXT, setting_value TEXT)")
    if ai_api_key:
        conn.execute("INSERT INTO settings (setting_key, setting_value) VALUES ('ai_api_key', ?)",
                     (ai_api_key,))
    conn.commit()
    conn.close()


def _run_wipe_secrets(fake_root: pathlib.Path):
    script = 'source "$BIN"; repo_root() { printf "%s" "$FAKE_ROOT"; }; wipe_secrets'
    env = {"BIN": str(_BIN), "FAKE_ROOT": str(fake_root), "PATH": "/usr/bin:/bin"}
    return subprocess.run(["bash", "-c", script, str(_BIN)],
                          capture_output=True, text=True, env=env, timeout=30)


def test_stray_pre_update_snapshot_with_a_stored_key_is_deleted_and_prep_proceeds(tmp_path):
    root = tmp_path / "repo"
    _make_settings_db(root / "data" / "appliance" / "pre-update.db", ai_api_key="sk-leaked-in-snapshot")
    _make_settings_db(root / "data" / "artwork.db")  # the real DB: clean, no stored key

    r = _run_wipe_secrets(root)

    assert r.returncode == 0, r.stderr
    assert not (root / "data" / "appliance" / "pre-update.db").exists()
    assert "removed" in r.stdout and "pre-update.db" in r.stdout
    assert "carries no stored API keys" in r.stdout


def test_artwork_db_with_a_stored_key_refuses(tmp_path):
    root = tmp_path / "repo"
    _make_settings_db(root / "data" / "artwork.db", ai_api_key="sk-still-in-the-real-db")

    r = _run_wipe_secrets(root)

    assert r.returncode == 1
    assert "REFUSING TO CONTINUE" in r.stderr
    assert (root / "data" / "artwork.db").exists()  # refused, not silently deleted


def _run_wipe_kiosk_chromium_profile(fake_home: pathlib.Path):
    # Same override trick as _run_wipe_secrets: source the real script so wipe_kiosk_chromium_profile
    # is the actual function body, but redefine _kiosk_home so it points at a tmp dir instead of
    # shelling out to getent for a real "kiosk" system user.
    # systemctl/pkill/sleep are stubbed: the real ones would stop THIS host's tty1 / signal a real user
    # (or raise a polkit prompt) if the suite ever ran as root.
    script = ('source "$BIN"; _kiosk_home() { printf "%s" "$FAKE_HOME"; }; '
              'systemctl() { :; }; pkill() { :; }; sleep() { :; }; wipe_kiosk_chromium_profile')
    env = {"BIN": str(_BIN), "FAKE_HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
    return subprocess.run(["bash", "-c", script, str(_BIN)],
                          capture_output=True, text=True, env=env, timeout=30)


def test_full_removes_planted_kiosk_chromium_profile(tmp_path):
    home = tmp_path / "home" / "kiosk"
    profile = home / ".config" / "chromium"
    profile.mkdir(parents=True)
    (profile / "SingletonLock").symlink_to("pieria-1234")
    (profile / "Default").mkdir()

    r = _run_wipe_kiosk_chromium_profile(home)

    assert r.returncode == 0, r.stderr
    assert not profile.exists()
    assert "removed kiosk Chromium profile" in r.stdout


def test_full_refuses_when_profile_dir_itself_is_a_symlink(tmp_path):
    home = tmp_path / "home" / "kiosk"
    (home / ".config").mkdir(parents=True)
    real_target = tmp_path / "elsewhere"
    real_target.mkdir()
    (home / ".config" / "chromium").symlink_to(real_target)

    r = _run_wipe_kiosk_chromium_profile(home)

    assert r.returncode == 0, r.stderr
    assert "WARNING" in r.stderr and "symlink" in r.stderr
    assert (home / ".config" / "chromium").is_symlink()  # left alone, not followed
    assert real_target.exists()  # the real dir it points to is untouched
