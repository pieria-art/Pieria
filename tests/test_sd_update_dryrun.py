"""sd-update under DRY_RUN — the host-side half of the appliance bridge (ADR-119).

There is no apt, no systemd and no docker on the dev box, and there must not be: this suite drives the
REAL script with `DRY_RUN=1`, a tmp conf via SD_CONF, and a PATH of shell shims that record their argv
instead of doing anything. So the dispatch, the whitelist, the host-side re-validation and the conf
writes are all exercised, while nothing can reach a real host command — if a call ever escapes the
shims, the assertion that its argv landed in calls.log is what fails.

Conf writes are deliberately NOT dry-run-skipped (sd-conf is pure file surgery), so the conf is the
observable effect; everything that touches the host is a `[dry-run] ...` line in the log.
"""

import json
import os
import pathlib
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BIN = _ROOT / "deploy" / "appliance" / "bin"

#: Every host command any arm may reach. A shim that is missing would fall through to the real binary,
#: which is exactly the accident this list exists to prevent.
SHIMMED = ["systemctl", "timedatectl", "docker", "apt-get", "git", "systemd-run", "wlr-randr",
           "flock", "dpkg", "logger"]

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


class Harness:
    def __init__(self, tmp_path):
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
        for name in SHIMMED:
            self._shim(name)
        # A box with no such tag is the realistic default, and it is what makes the update-app ref
        # re-validation testable: rev-parse must FAIL for an unknown tag.
        self._shim("git", 'case "$*" in *rev-parse*) exit 1 ;; esac\nexit 0')

    def _shim(self, name, body='exit 0'):
        p = self.bindir / name
        p.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> "$SD_CALLS"\n{body}\n')
        p.chmod(0o755)

    def stub(self, name, body):
        """Replace a sibling HELPER (not a PATH command) with a stub script."""
        p = self.bindir / name
        p.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> "$SD_CALLS"\n{body}\n')
        p.chmod(0o755)
        return p

    def request(self, action, **fields):
        (self.dir / "request.json").write_text(json.dumps({"action": action, "nonce": "n1", **fields}))

    def run(self, dry=True, **env):
        e = {**os.environ,
             "PATH": f"{self.bindir}:{os.environ['PATH']}",
             "SD_CALLS": str(self.calls),
             "SD_CONF": str(self.conf),
             "SD_SYSTEMD_DIR": str(self.systemd),
             "DRY_RUN": "1" if dry else "0"}
        e.update(env)
        return subprocess.run(["bash", str(_BIN / "sd-update"), str(self.root)],
                              capture_output=True, text=True, env=e)

    # --- observations -------------------------------------------------------
    @property
    def status(self):
        return json.loads((self.dir / "status.json").read_text())

    @property
    def conf_text(self):
        return self.conf.read_text()

    @property
    def log(self):
        f = self.dir / "last-update.log"
        return f.read_text() if f.exists() else ""

    @property
    def called(self):
        return self.calls.read_text() if self.calls.exists() else ""

    def conf_value(self, key):
        for line in self.conf_text.splitlines():
            if line.strip().startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
        return None

    @property
    def request_consumed(self):
        return not (self.dir / "request.json").exists()


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


# --- the whitelist is the gate --------------------------------------------------------------------

def test_an_unknown_action_is_refused_and_the_request_consumed(h):
    h.request("rm -rf /")
    r = h.run()
    assert r.returncode == 1
    assert h.status["state"] == "error"
    assert "unknown action" in h.status["message"]
    assert h.request_consumed
    assert h.called == ""          # nothing was executed before the whitelist rejected it


def test_no_request_is_a_silent_no_op(h):
    assert h.run().returncode == 0
    assert not (h.dir / "status.json").exists()


def test_the_conf_is_untouched_by_a_rejected_action(h):
    before = h.conf_text
    h.request("set-timezone", timezone="x; rm -rf /")
    h.run()
    assert h.conf_text == before


# --- the log survives the action ---------------------------------------------------------------

def test_the_log_is_persisted_world_readable_for_the_ui(h):
    h.request("update-scripts")
    h.run()
    log = h.dir / "last-update.log"
    assert log.exists()
    assert oct(log.stat().st_mode)[-3:] == "644"
    assert "install.sh" in log.read_text()


def test_status_json_is_world_readable(h):
    # Root writes it; uid 1000 in the container reads it (ADR-037).
    h.request("reboot")
    h.run()
    assert oct((h.dir / "status.json").stat().st_mode)[-3:] == "644"


# --- the existing arms still behave ---------------------------------------------------------------

def test_reboot_writes_its_status_and_consumes_the_request_before_rebooting(h):
    h.request("reboot")
    assert h.run().returncode == 0
    assert h.status == {**h.status, "state": "done", "message": "rebooting"}
    assert h.request_consumed
    assert "[dry-run] systemctl reboot" in h.log
    assert "systemctl reboot" not in h.called    # the shim was never reached


def test_update_app_without_a_ref_tracks_origin_main(h):
    h.request("update-app")
    h.run()
    assert h.status["state"] == "done"
    assert "[dry-run] git -c safe.directory" in h.log
    assert "reset --hard origin/main" in h.log
    assert "[dry-run] docker compose" in h.log and "up -d --build" in h.log
    assert "[dry-run] systemctl restart getty@tty1" in h.log


def test_update_app_refuses_a_ref_that_is_not_a_real_tag(h):
    # The endpoint's regex is the first gate; this is the authoritative one.
    h.request("update-app", ref="v9.9.9")
    assert h.run().returncode == 1
    assert "not a known tag" in h.status["message"]
    assert h.request_consumed
