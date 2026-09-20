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
             "SD_LOCK_FILE": str(self.root.parent / "apt.lock"),
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


# --- settings arms (ADR-119) ---------------------------------------------------------------------

def test_set_timezone_writes_the_conf_sets_the_host_and_restarts_the_container(h):
    h.request("set-timezone", timezone="America/Chicago")
    h.run()
    assert h.status["state"] == "done"
    assert h.conf_value("TIMEZONE") == "America/Chicago"
    assert "[dry-run] timedatectl set-timezone America/Chicago" in h.log
    # ADR-118: /etc/localtime is a bind mount resolved at container START.
    assert "[dry-run] docker compose" in h.log and "restart" in h.log


def test_the_host_re_validates_the_timezone_and_leaves_the_conf_byte_identical(h):
    before = h.conf_text
    h.request("set-timezone", timezone="America/Chicago; rm -rf /")
    h.run()
    assert h.status["state"] == "error"
    assert h.conf_text == before
    # conf_export legitimately probes `timedatectl show`; what must never have run is the setter.
    assert "timedatectl set-timezone" not in h.called


def test_set_orientation_90_writes_both_keys(h):
    h.request("set-orientation", orientation="90")
    h.run()
    assert h.conf_value("ROTATE") == "90"
    assert h.conf_value("EINK_ORIENTATION") == "portrait"       # ADR-059 #2
    assert "[dry-run] systemctl restart getty@tty1" in h.log


def test_set_orientation_180_is_still_a_landscape_panel(h):
    h.request("set-orientation", orientation="180")
    h.run()
    assert h.conf_value("ROTATE") == "180"
    assert h.conf_value("EINK_ORIENTATION") == ""


def test_set_orientation_landscape_blanks_rotate(h):
    h.conf.write_text(h.conf_text.replace("ROTATE=", "ROTATE=270"))
    h.request("set-orientation", orientation="landscape")
    h.run()
    assert h.conf_value("ROTATE") == ""
    assert h.conf_value("EINK_ORIENTATION") == ""


def test_set_orientation_rejects_a_value_the_endpoint_would_never_send(h):
    before = h.conf_text
    h.request("set-orientation", orientation="45")
    assert h.run().returncode == 1
    assert h.status["state"] == "error"
    assert h.conf_text == before


def test_set_orientation_cancels_a_pending_preview_revert(h):
    h.request("set-orientation", orientation="90")
    h.run()
    assert "[dry-run] systemctl stop sd-orientation-revert.service" in h.log


def test_setting_the_orientation_preserves_every_other_key(h):
    h.request("set-orientation", orientation="90")
    h.run()
    assert "GEMINI_API_KEY=sk-not-a-real-key" in h.conf_text
    assert "WATCHDOG=observe" in h.conf_text
    assert h.conf_text.splitlines()[0].startswith("# Pieria")


def test_preview_orientation_arms_a_revert_to_the_CURRENT_conf_orientation(h):
    # Already mounted portrait: an expired preview must go back to 270, not to landscape.
    h.conf.write_text(h.conf_text.replace("ROTATE=", "ROTATE=270"))
    h.request("preview-orientation", orientation="90")
    h.run()
    assert "[dry-run] systemctl stop sd-orientation-revert.service" in h.log
    assert "sd-rotate-now apply 90" in h.log
    assert "--on-active=30 --unit=sd-orientation-revert --collect" in h.log
    assert "sd-rotate-now revert 270" in h.log
    assert "reverts in 30 seconds" in h.status["message"]


def test_preview_orientation_reverts_to_landscape_when_the_conf_is_blank(h):
    h.request("preview-orientation", orientation="90")
    h.run()
    assert "sd-rotate-now revert landscape" in h.log


def test_preview_orientation_does_not_touch_the_conf(h):
    before = h.conf_text
    h.request("preview-orientation", orientation="90")
    h.run()
    assert h.conf_text == before


def test_set_display_name_renames_and_relaunches_but_never_touches_the_hostname(h):
    h.request("set-display-name", display_id="new_name")
    h.run()
    assert h.conf_value("DISPLAY_ID") == "new_name"
    assert h.conf_value("HOSTNAME") == "pieria-abcd"      # ADR-083
    assert "[dry-run] systemctl restart getty@tty1" in h.log


def test_a_display_rename_restarts_the_eink_client_only_when_one_is_enabled(h):
    h.request("set-display-name", display_id="new_name")
    h.run()
    assert "systemctl restart sd-eink" not in h.log

    h2_conf = h.conf_text.replace("EINK_ENABLED=0", "EINK_ENABLED=1")
    h.conf.write_text(h2_conf)
    h.request("set-display-name", display_id="other_name")
    h.run()
    assert "[dry-run] systemctl restart sd-eink" in h.log


def test_set_watchdog_writes_the_conf_and_restarts_nothing(h):
    h.request("set-watchdog", watchdog="enforce")
    h.run()
    assert h.conf_value("WATCHDOG") == "enforce"
    assert "systemctl" not in h.log       # the timer re-sources the conf on its next tick


def test_set_watchdog_rejects_a_mode_that_is_not_one_of_the_three(h):
    h.request("set-watchdog", watchdog="enforce; reboot")
    h.run()
    assert h.status["state"] == "error"
    assert h.conf_value("WATCHDOG") == "observe"


def test_reopen_setup_reboots_after_re_arming_the_wizard(h):
    h.request("reopen-setup")
    assert h.run().returncode == 0
    assert h.status["state"] == "done"
    assert "setup wizard" in h.status["message"]
    assert h.request_consumed
    assert "sd-image-prep --enable-setup" in h.log
    assert "[dry-run] systemctl reboot" in h.log
    assert "systemctl reboot" not in h.called


def test_every_settings_arm_exports_conf_json_for_the_ui(h):
    h.request("set-watchdog", watchdog="off")
    h.run()
    exported = json.loads((h.dir / "conf.json").read_text())
    assert exported["values"]["WATCHDOG"] == "off"
    assert not any("KEY" in k for k in exported["values"])    # never the Gemini key


# --- action arms (ADR-119) -----------------------------------------------------------------------

def test_relaunch_kiosk_restarts_the_login_session(h):
    h.request("relaunch-kiosk")
    h.run()
    assert h.status["state"] == "done"
    assert "[dry-run] systemctl restart getty@tty1" in h.log


def test_restart_app_also_relaunches_the_kiosk(h):
    # A reloaded container leaves the Canvas connected with a stalled advance timer, so the picture
    # would sit frozen until the next cycle.
    h.request("restart-app")
    h.run()
    assert "[dry-run] docker compose" in h.log and "restart" in h.log
    assert "[dry-run] systemctl restart getty@tty1" in h.log


def test_poweroff_writes_its_status_and_consumes_the_request_first(h):
    h.request("poweroff")
    assert h.run().returncode == 0
    assert h.status["state"] == "done"
    assert h.status["message"] == "powering off"
    assert h.request_consumed
    assert "[dry-run] systemctl poweroff" in h.log
    assert "systemctl poweroff" not in h.called


def test_support_bundle_runs_the_collector(h):
    h.request("support-bundle")
    h.run()
    assert h.status["state"] == "done"
    assert "sd-support-bundle" in h.log
    assert "download" in h.status["message"]


# --- OS updates (ADR-119) --------------------------------------------------------------------

def _os_check_stub(h, reboot="0"):
    """Stand in for sd-os-check. The real one shells out to apt, which must never happen here."""
    return h.stub("sd-os-check", f'case "$*" in *--print-reboot*) echo {reboot} ;; esac\nexit 0')


def test_check_os_updates_just_runs_the_checker(h):
    stub = _os_check_stub(h)
    h.request("check-os-updates")
    h.run(SD_OS_CHECK_BIN=str(stub))
    assert h.status["state"] == "done"
    assert "sd-os-check" in h.log


def test_update_system_reboots_when_the_plan_says_so(h):
    stub = _os_check_stub(h, reboot="1")
    h.request("update-system")
    assert h.run(SD_OS_CHECK_BIN=str(stub)).returncode == 0
    assert h.status["state"] == "done"
    assert "rebooting" in h.status["message"]
    assert h.request_consumed
    assert "[dry-run] systemctl reboot" in h.log
    assert "systemctl reboot" not in h.called       # never actually reached


def test_update_system_relaunches_the_kiosk_when_no_reboot_is_needed(h):
    # Chromium may still have been upgraded under a running kiosk.
    stub = _os_check_stub(h, reboot="0")
    h.request("update-system")
    h.run(SD_OS_CHECK_BIN=str(stub))
    assert h.status["state"] == "done"
    assert "rebooting" not in h.status["message"]
    assert "[dry-run] systemctl restart getty@tty1" in h.log


def test_update_system_decides_the_reboot_question_before_upgrading(h):
    # Afterwards the simulation is empty, so asking later would always answer "no".
    stub = _os_check_stub(h, reboot="1")
    h.request("update-system")
    h.run(SD_OS_CHECK_BIN=str(stub))
    log = h.log
    assert log.index("reboot_likely=1") < log.index("full-upgrade")


def test_update_system_runs_the_whole_apt_sequence(h):
    stub = _os_check_stub(h)
    h.request("update-system")
    h.run(SD_OS_CHECK_BIN=str(stub))
    log = h.log
    for fragment in ("apt-get -y", "DPkg::Lock::Timeout=300", "--force-confold",
                     "update", "full-upgrade", "autoremove --purge", "apt-get clean"):
        assert fragment in log, fragment
    # The stack is brought back explicitly: a containerd upgrade restarts the daemon under us.
    assert "[dry-run] docker compose" in log and "up -d" in log


def test_update_system_refreshes_the_count_afterwards(h):
    # Otherwise the UI would keep showing the pre-upgrade number until 04:30 tomorrow.
    stub = _os_check_stub(h)
    h.request("update-system")
    h.run(SD_OS_CHECK_BIN=str(stub))
    log = h.log
    # The pre-upgrade decision is recorded, and the post-upgrade re-check ran after the compose bring-up.
    assert "reboot_likely=" in log
    assert log.index("up -d") < log.index("sd-os-check")
    assert "--no-update" in log


def test_update_system_takes_the_shared_apt_lock(h):
    stub = _os_check_stub(h)
    h.request("update-system")
    h.run(SD_OS_CHECK_BIN=str(stub))
    assert "flock -w 600" in h.called


def test_update_system_bows_out_when_the_lock_is_held(h):
    stub = _os_check_stub(h)
    h.stub("flock", "exit 1")
    h.request("update-system")
    assert h.run(SD_OS_CHECK_BIN=str(stub)).returncode == 1
    assert h.status["state"] == "error"
    assert "still running" in h.status["message"]
    assert "apt-get" not in h.called
