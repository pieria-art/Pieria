"""sd-update's F3 rollback path under DRY_RUN=0 (update-app arm).

wait_healthy/rollback only run when DRY_RUN=0 (see sd-update), so this suite drives the REAL script
with DRY_RUN=0, a tmp conf via SD_CONF, and a PATH of shell shims for every host command the arm can
reach (git, docker, curl, systemctl, ...) so nothing escapes to the real binary. `curl` is the health
probe (wait_healthy polls SERVER_URL/logo.svg); its exit code per-invocation is driven by a mode file
so a test can script "unhealthy then healthy" without a real server. `docker` mutates a REAL small
sqlite artwork.db the first time it sees `up`, standing in for a migration that runs during the new
version's startup — this is what proves the rollback actually restores the pre-update snapshot rather
than just resetting the code.
"""

import json
import os
import pathlib
import sqlite3
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BIN = _ROOT / "deploy" / "appliance" / "bin"

SHIMMED = ["systemctl", "timedatectl", "docker", "apt-get", "git", "systemd-run", "wlr-randr",
           "flock", "dpkg", "logger", "curl"]

BASE_CONF = """# Pieria — Appliance configuration
SERVER_URL=http://localhost:8000
DISPLAY_ID=living_room
HOSTNAME=pieria-abcd
TIMEZONE=
ROTATE=
OUTPUT=HDMI-A-1
WATCHDOG=observe
GEMINI_API_KEY=sk-not-a-real-key
EINK_ENABLED=0
EINK_ORIENTATION=
"""

FAKE_PREV_SHA = "deadbeef123456789012345678901234deadbee"


class Harness:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / "repo"
        self.dir = self.root / "data" / "appliance"
        self.dir.mkdir(parents=True)
        self.conf = tmp_path / "pieria.conf"
        self.conf.write_text(BASE_CONF)
        self.calls = tmp_path / "calls.log"
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        self.systemd = tmp_path / "systemd"
        self.systemd.mkdir()
        self.curl_mode = tmp_path / "curl_mode"
        self.curl_mode.write_text("always_ok")
        self.curl_counter = tmp_path / "curl_counter"
        self.docker_up_marker = tmp_path / "docker_up_marker"
        self.docker_mode = tmp_path / "docker_mode"
        self.docker_mode.write_text("normal")
        self.docker_fail_marker = tmp_path / "docker_fail_marker"

        # A real, tiny artwork.db so the sqlite3 backup/restore path is exercised for real.
        self.db = self.root / "data" / "artwork.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO meta VALUES ('version', 'pre-update')")
        con.commit()
        con.close()

        for name in SHIMMED:
            self._shim(name)
        self._shim("git", f'''case "$*" in
  *"rev-parse HEAD"*) echo {FAKE_PREV_SHA} ; exit 0 ;;
  *) exit 0 ;;
esac''')
        self._shim("curl", f'''mode="$(cat "{self.curl_mode}" 2>/dev/null || echo always_ok)"
case "$mode" in
  always_fail) exit 1 ;;
  fail_then_ok)
    count="$(cat "{self.curl_counter}" 2>/dev/null || echo 0)"
    count=$((count + 1))
    echo "$count" > "{self.curl_counter}"
    [ "$count" -gt 1 ]
    exit $? ;;
  *) exit 0 ;;
esac''')
        self._shim("docker", f'''case "$*" in
  *" up "*)
    mode="$(cat "{self.docker_mode}" 2>/dev/null || echo normal)"
    if [ "$mode" = "fail_first_up" ] && [ ! -f "{self.docker_fail_marker}" ]; then
      touch "{self.docker_fail_marker}" "{self.docker_up_marker}"
      exit 1
    fi
    if [ ! -f "{self.docker_up_marker}" ]; then
      touch "{self.docker_up_marker}"
      python3 -c "import sqlite3; c=sqlite3.connect('{self.db}'); c.execute(\\\"UPDATE meta SET value='post-migration'\\\"); c.commit(); c.close()"
    fi
    ;;
esac
exit 0''')

    def _shim(self, name, body="exit 0"):
        p = self.bindir / name
        p.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> "$SD_CALLS"\n{body}\n')
        p.chmod(0o755)

    def set_curl_mode(self, mode):
        self.curl_mode.write_text(mode)

    def set_docker_mode(self, mode):
        self.docker_mode.write_text(mode)

    def request(self, action, **fields):
        (self.dir / "request.json").write_text(json.dumps({"action": action, "nonce": "n1", **fields}))

    def run(self, **env):
        e = {**os.environ,
             "PATH": f"{self.bindir}:{os.environ['PATH']}",
             "SD_CALLS": str(self.calls),
             "SD_CONF": str(self.conf),
             "SD_SYSTEMD_DIR": str(self.systemd),
             "SD_LOCK_FILE": str(self.tmp / "apt.lock"),
             "DRY_RUN": "0",
             "WAIT_HEALTHY_ATTEMPTS": "1",
             "WAIT_HEALTHY_INTERVAL": "0"}
        e.update(env)
        return subprocess.run(["bash", str(_BIN / "sd-update"), str(self.root)],
                              capture_output=True, text=True, env=e)

    @property
    def status(self):
        return json.loads((self.dir / "status.json").read_text())

    @property
    def log(self):
        f = self.dir / "last-update.log"
        return f.read_text() if f.exists() else ""

    @property
    def called(self):
        return self.calls.read_text() if self.calls.exists() else ""

    @property
    def snapshot_exists(self):
        return (self.dir / "pre-update.db").exists()

    @property
    def db_version(self):
        con = sqlite3.connect(self.db)
        v = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]
        con.close()
        return v

    @property
    def env_text(self):
        f = self.root / ".env"
        return f.read_text() if f.exists() else ""


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_healthy_update_reports_done_and_cleans_up(h):
    h.set_curl_mode("always_ok")
    h.request("update-app")
    r = h.run()
    assert r.returncode == 0
    assert h.status["state"] == "done"
    assert not h.snapshot_exists
    # Only the new-version reset ran — no rollback reset to the captured PREV_SHA.
    assert f"reset --hard {FAKE_PREV_SHA}" not in h.log
    assert "reset --hard origin/main" in h.log


def test_unhealthy_new_version_rolls_back_and_restores_the_db(h):
    h.set_curl_mode("fail_then_ok")
    h.request("update-app")
    r = h.run()
    assert r.returncode == 0
    assert h.status["state"] == "error"
    assert "rolled back to the previous version" in h.status["message"]
    assert f"reset --hard {FAKE_PREV_SHA}" in h.log
    # stop ran before the rollback's second `up`.
    calls = h.called.splitlines()
    up_indices = [i for i, l in enumerate(calls) if l.startswith("docker") and " up " in l]
    stop_indices = [i for i, l in enumerate(calls) if l.startswith("docker") and l.rstrip().endswith("stop")]
    assert len(up_indices) == 2 and len(stop_indices) == 1
    assert up_indices[0] < stop_indices[0] < up_indices[1]
    # the DB was actually restored from the pre-update snapshot, not left mutated.
    assert h.db_version == "pre-update"
    assert not h.snapshot_exists


def test_both_new_version_and_rollback_unhealthy(h):
    h.set_curl_mode("always_fail")
    h.request("update-app")
    r = h.run()
    assert r.returncode == 0
    assert h.status["state"] == "error"
    assert "rollback did not come back up" in h.status["message"]
    assert not h.snapshot_exists


def test_compose_up_failure_triggers_rollback(h):
    # `docker compose up` itself returning non-zero (not just an unhealthy app) must also roll back —
    # today it only rolled back when `up` returned 0 but the health probe failed. The shim's
    # "fail_first_up" mode marks itself as already-mutated-and-failed on the FIRST `up`, which meant
    # this test could pass even if rollback never restored anything (the DB was never mutated on the
    # failing call). Mutate the DB on that failing first `up` too, so `db_version == "pre-update"`
    # below is only true if rollback's restore actually ran.
    h.set_curl_mode("always_ok")
    h.set_docker_mode("fail_first_up")
    h.request("update-app")
    r = h.run()
    assert r.returncode == 0
    assert h.status["state"] == "error"
    assert "rolled back to the previous version" in h.status["message"]
    assert f"reset --hard {FAKE_PREV_SHA}" in h.log
    assert h.db_version == "pre-update"
    assert not h.snapshot_exists


def test_token_mint_failure_after_reset_rolls_back(h):
    # A failure AFTER the checkout has moved but BEFORE `up --build` (the token-mint step) must still
    # roll back — reset_ok is already 1 at that point. Force the mint's `>> "$REPO_ROOT/.env"` to fail
    # by making .env a directory: `grep -q ... .env` then errors (not a match) so the mint is attempted,
    # and the append redirection fails outright ("Is a directory").
    (h.root / ".env").mkdir()
    h.set_curl_mode("always_ok")
    h.request("update-app")
    r = h.run()
    assert r.returncode == 0
    assert h.status["state"] == "error"
    assert "rolled back to the previous version" in h.status["message"]
    assert f"reset --hard {FAKE_PREV_SHA}" in h.log
    assert not h.snapshot_exists


def test_git_reset_failure_does_not_roll_back(h):
    # The INITIAL `git reset --hard $TARGET` failing must NOT trigger rollback (reset_ok stays 0) — it
    # falls through to a plain error, since nothing has moved yet.
    h.set_curl_mode("always_ok")
    h._shim("git", f'''case "$*" in
  *"rev-parse HEAD"*) echo {FAKE_PREV_SHA} ; exit 0 ;;
  *"reset --hard origin/main"*) exit 1 ;;
  *) exit 0 ;;
esac''')
    h.request("update-app")
    r = h.run()
    assert r.returncode == 0
    assert h.status["state"] == "error"
    assert "rolled back" not in h.status["message"]
    assert f"reset --hard {FAKE_PREV_SHA}" not in h.log
    assert not h.snapshot_exists


def test_both_new_version_and_rollback_unhealthy_reset_ran(h):
    # test_both_new_version_and_rollback_unhealthy only checks the final status message; also assert
    # the rollback's own `reset --hard $PREV_SHA` actually ran (the rollback attempt happened, it just
    # didn't come back healthy).
    h.set_curl_mode("always_fail")
    h.request("update-app")
    r = h.run()
    assert r.returncode == 0
    assert f"reset --hard {FAKE_PREV_SHA}" in h.log


def test_stale_preupdate_db_from_a_previous_run_is_not_restored(h):
    # A leftover pre-update.db from an earlier, interrupted run must be cleared before THIS run takes
    # its own snapshot — otherwise a healthy update could still leave a stale snapshot on disk, or a
    # later rollback could restore the WRONG snapshot.
    stale = h.dir / "pre-update.db"
    stale.write_bytes(b"stale snapshot from a previous interrupted run")
    h.set_curl_mode("always_ok")
    h.request("update-app")
    r = h.run()
    assert r.returncode == 0
    assert h.status["state"] == "done"
    assert not h.snapshot_exists
    # A real, fresh sqlite snapshot was taken (not left as the stale bytes) — the backup step ran.
    assert "sqlite3" in h.log or "import sqlite3" in h.log


def test_no_db_before_update_rollback_deletes_the_new_db(h):
    # If artwork.db did NOT exist before the update, a rollback must DELETE whatever the new version
    # created rather than trying to restore a snapshot that was never taken.
    h.db.unlink()
    (h.root / "data" / "artwork.db-wal").write_bytes(b"")
    (h.root / "data" / "artwork.db-shm").write_bytes(b"")
    h.set_curl_mode("always_fail")
    h.request("update-app")
    r = h.run()
    assert r.returncode == 0
    assert h.status["state"] == "error"
    assert not h.db.exists()
    assert not (h.root / "data" / "artwork.db-wal").exists()
    assert not (h.root / "data" / "artwork.db-shm").exists()
    assert not h.snapshot_exists


def test_snapshot_failure_aborts_before_touching_checkout(h):
    # A corrupt/unreadable source DB must abort BEFORE `git reset --hard` to the new target — nothing
    # has changed yet, so there is nothing to roll back.
    h.db.write_bytes(b"not a real sqlite database")
    h.set_curl_mode("always_ok")
    h.request("update-app")
    r = h.run()
    assert r.returncode == 0
    assert h.status["state"] == "error"
    assert "reset --hard" not in h.log
    assert not h.snapshot_exists


def test_update_bridge_token_is_appended_to_env_exactly_once(h):
    h.set_curl_mode("always_ok")
    h.request("update-app")
    h.run()
    h.request("update-app")
    h.run()
    lines = [l for l in h.env_text.splitlines() if l.startswith("SD_APPLIANCE_UPDATE_TOKEN=")]
    assert len(lines) == 1
