"""sd-watchdog — the escalation table + boot-loop guards (ADR-121).

There is no cage/chromium/docker/systemd on the dev box, and there must not be: this suite drives the
REAL script with a PATH of shims that record their argv instead of doing anything, plus a couple of
plain state files (`SD_WATCHDOG_STATE` for the consecutive-fails counter, `SD_UPTIME_FILE` standing in
for /proc/uptime) so a test controls exactly what the watchdog thinks it's looking at. python3 is left
UNSHIMMED (the paint probe and sd-watchdog-advance both pipe through it) — `SD_WATCHDOG_HELPER` is
pointed at a nonexistent path so the live/advance probes stay their pass-by-default 1/1 and the test
doesn't need a real HTTP server for sd-watchdog-advance's own urllib calls.

Background (2026-09-20 bench incident): a wedged-page fault (server reachable, kiosk alive, but the
picture stuck) escalated all the way to reboot, six times in 50 minutes, each reboot killing a running
`update-system` apt download. The fix: a browser-level fault caps at relaunch-kiosk and then gives up
(observes) rather than ever reboot or restart the container; only the server being genuinely
unreachable can reach those rungs; the reboot cap widened to 3-per-6h and a 10-minute post-boot grace
was added; and an appliance action in flight (sd-update's own status.json) pauses the watchdog outright.
"""

import json
import os
import pathlib
import subprocess
import textwrap
import time
from datetime import UTC, datetime, timedelta

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BIN = _ROOT / "deploy" / "appliance" / "bin" / "sd-watchdog"

SHIMMED = ["curl", "pgrep", "systemctl", "docker", "logger"]


class Harness:
    def __init__(self, tmp_path):
        self.root = tmp_path / "repo"
        self.dir = self.root / "data" / "appliance"
        self.dir.mkdir(parents=True)
        self.conf = tmp_path / "pieria.conf"
        self.conf.write_text(
            "SERVER_URL=http://localhost:8000\nDISPLAY_ID=living_room\nWATCHDOG=enforce\n"
        )
        self.calls = tmp_path / "calls.log"
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        self.state = tmp_path / "fails.state"
        self.state_display = tmp_path / "fails-display.state"
        self.reboots = self.dir / "watchdog-reboots.log"
        self.uptime = tmp_path / "uptime"
        self.uptime.write_text("999999.0 0.0\n")     # long-booted by default
        self.server_ok_file = tmp_path / "server_ok"
        self.server_ok_file.write_text("0")           # curl exit code for the logo.svg probe
        self.displays_json = tmp_path / "displays.json"
        self.displays_json.write_text("[]")
        self.pgrep_ok_file = tmp_path / "pgrep_ok"
        self.pgrep_ok_file.write_text("1")
        self.eink_active_file = tmp_path / "eink_active"
        self.eink_active_file.write_text("1")
        # Default DRM glob: a fake sysfs with one CONNECTED connector, so existing (non-eink) tests
        # keep their today's-behaviour path (has_kiosk=1) unless a test points this elsewhere.
        self.drm_dir = tmp_path / "drm"
        (self.drm_dir / "card1-HDMI-A-1").mkdir(parents=True)
        (self.drm_dir / "card1-HDMI-A-1" / "status").write_text("connected")
        for name in SHIMMED:
            self._shim(name)

    def _shim(self, name):
        bodies = {
            "curl": textwrap.dedent(f"""\
                #!/bin/sh
                printf "%s\\n" "curl $*" >> "$SD_CALLS"
                for a in "$@"; do url="$a"; done
                case "$url" in
                  */logo.svg) exit "$(cat "{self.server_ok_file}")" ;;
                  */api/remote/displays) cat "{self.displays_json}"; exit 0 ;;
                  *) exit 0 ;;
                esac
                """),
            "pgrep": textwrap.dedent(f"""\
                #!/bin/sh
                printf "%s\\n" "pgrep $*" >> "$SD_CALLS"
                [ "$(cat "{self.pgrep_ok_file}")" = "1" ] && exit 0 || exit 1
                """),
            "systemctl": textwrap.dedent(f"""\
                #!/bin/sh
                printf "%s\\n" "systemctl $*" >> "$SD_CALLS"
                case "$*" in
                  "is-active --quiet sd-eink")
                    [ "$(cat "{self.eink_active_file}")" = "1" ] && exit 0 || exit 3 ;;
                  *) exit 0 ;;
                esac
                """),
            "docker": '#!/bin/sh\nprintf "%s\\n" "docker $*" >> "$SD_CALLS"\nexit 0\n',
            "logger": '#!/bin/sh\nprintf "%s\\n" "logger $*" >> "$SD_CALLS"\nexit 0\n',
        }
        p = self.bindir / name
        p.write_text(bodies[name])
        p.chmod(0o755)

    # --- scenario controls ---------------------------------------------------
    def set_server_ok(self, ok):
        self.server_ok_file.write_text("0" if ok else "1")

    def set_kiosk_ok(self, ok):
        self.pgrep_ok_file.write_text("1" if ok else "0")

    def set_paint_ok(self, ok, display_id="living_room"):
        art = {"id": 1} if ok else None
        self.displays_json.write_text(json.dumps([{"display_id": display_id, "artwork": art}]))

    def set_eink_active(self, ok):
        self.eink_active_file.write_text("1" if ok else "0")

    def set_connector(self, connected):
        """True: one HDMI connector reads connected. False: it reads disconnected (still present —
        distinct from the connector glob matching nothing at all, see `no_drm`)."""
        (self.drm_dir / "card1-HDMI-A-1" / "status").write_text(
            "connected" if connected else "disconnected"
        )

    def no_drm(self):
        """Simulate /sys/class/drm not existing at all (dev box / CI) — glob matches nothing."""
        self.drm_dir = self.root / "no-such-drm-dir"

    def enable_eink(self, enabled=True):
        lines = [l for l in self.conf.read_text().splitlines() if not l.startswith("EINK_ENABLED=")]
        lines.append(f"EINK_ENABLED={'1' if enabled else '0'}")
        self.conf.write_text("\n".join(lines) + "\n")

    def write_appliance_status(self, state, action="update-system", age_seconds=0):
        ts = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
        (self.dir / "status.json").write_text(json.dumps(
            {"state": state, "action": action, "updated_at": ts, "message": "", "log_tail": []}
        ))

    def run(self, mode="enforce", **env):
        lines = [l for l in self.conf.read_text().splitlines() if not l.startswith("WATCHDOG=")]
        lines.append(f"WATCHDOG={mode}")
        self.conf.write_text("\n".join(lines) + "\n")
        e = {
            **os.environ,
            "PATH": f"{self.bindir}:{os.environ['PATH']}",
            "SD_CALLS": str(self.calls),
            "SD_WATCHDOG_STATE": str(self.state),
            "SD_WATCHDOG_STATE_DISPLAY": str(self.state_display),
            "SD_UPTIME_FILE": str(self.uptime),
            "SD_WATCHDOG_HELPER": str(self.bindir / "no-such-helper"),
            "SD_DRM_STATUS_GLOB": str(self.drm_dir / "*" / "status"),
        }
        e.update(env)
        return subprocess.run(
            ["bash", str(_BIN), str(self.conf), str(self.root)],
            capture_output=True, text=True, env=e,
        )

    def tick(self, mode="enforce", **env):
        r = self.run(mode=mode, **env)
        assert r.returncode == 0, r.stderr
        return self.status

    # --- observations ---------------------------------------------------------
    @property
    def status(self):
        return json.loads((self.dir / "watchdog.json").read_text())

    @property
    def called(self):
        return self.calls.read_text() if self.calls.exists() else ""

    @property
    def fails(self):
        try:
            return int(self.state.read_text())
        except (FileNotFoundError, ValueError):
            return 0

    @property
    def fails_display(self):
        try:
            return int(self.state_display.read_text())
        except (FileNotFoundError, ValueError):
            return 0


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


# --- healthy is a true no-op --------------------------------------------------------------------

def test_healthy_takes_no_action_and_calls_nothing_but_the_probes(h):
    h.set_server_ok(True)
    h.set_kiosk_ok(True)
    h.set_paint_ok(True)
    st = h.tick()
    assert st["action"] == "none"
    assert "systemctl restart" not in h.called
    assert "docker" not in h.called
    assert "systemctl reboot" not in h.called


# --- a browser-level fault never escalates past relaunch-kiosk ---------------------------------

def test_paint_fault_relaunches_at_3_4_5_then_gives_up_and_never_reboots(h):
    h.set_server_ok(True)
    h.set_kiosk_ok(True)
    h.set_paint_ok(False)

    actions = [h.tick()["action"] for _ in range(7)]

    assert actions == [
        "none", "none",
        "relaunch-kiosk", "relaunch-kiosk", "relaunch-kiosk",
        "give-up", "give-up",
    ]
    assert h.called.count("systemctl restart getty@tty1") == 3
    assert "docker" not in h.called
    assert "systemctl reboot" not in h.called
    assert "giving up on kiosk relaunch; not escalating" in h.status["message"]


def test_kiosk_dead_with_server_up_also_caps_at_relaunch_kiosk(h):
    """Same rule for the plain cage/chromium-not-running probe, not just the paint probe."""
    h.set_server_ok(True)
    h.set_kiosk_ok(False)
    h.set_paint_ok(True)

    for _ in range(5):
        st = h.tick()
    assert st["action"] == "relaunch-kiosk"

    st = h.tick()
    assert st["action"] == "give-up"
    assert "docker" not in h.called
    assert "systemctl reboot" not in h.called


def test_observe_mode_never_calls_anything_for_a_paint_fault(h):
    h.set_server_ok(True)
    h.set_kiosk_ok(True)
    h.set_paint_ok(False)

    actions = [h.tick(mode="observe")["action"] for _ in range(6)]

    assert actions[2:5] == ["observe:relaunch-kiosk"] * 3
    assert actions[5] == "give-up"          # giving up is a real stop, not an "observe:" label
    # Probing still runs in observe mode (curl/pgrep) — it's ENFORCEMENT that never fires.
    assert "systemctl restart" not in h.called
    assert "docker" not in h.called
    assert "systemctl reboot" not in h.called


# --- server down escalates through restart-container to reboot ---------------------------------

def test_server_down_escalates_restart_container_then_reboot(h):
    h.set_server_ok(False)
    h.set_kiosk_ok(True)

    actions = [h.tick()["action"] for _ in range(6)]

    assert actions == [
        "none", "none",
        "restart-container", "restart-container", "restart-container",
        "reboot",
    ]
    assert h.called.count("docker compose") == 3
    assert "systemctl reboot" in h.called
    assert h.reboots.exists() and h.reboots.read_text().strip() != ""


def test_reboot_is_refused_within_10_minutes_of_boot(h):
    h.set_server_ok(False)
    h.uptime.write_text("300.0 0.0\n")   # 5 minutes since boot
    for _ in range(5):
        h.tick()
    st = h.tick()

    assert st["action"] == "reboot"          # the DECISION still shows reboot...
    assert "systemctl reboot" not in h.called  # ...but it was never actually carried out
    assert "within 600s of boot" in h.called   # logger call recorded the specific reason


def test_reboot_cap_refuses_a_fourth_reboot_within_the_window(h):
    h.set_server_ok(False)
    epoch = int(time.time())
    h.reboots.write_text("\n".join(str(epoch - i) for i in (10, 20, 30)) + "\n")
    h.state.write_text("5")   # one tick away from the reboot rung

    st = h.tick()

    assert st["action"] == "reboot"
    assert "systemctl reboot" not in h.called   # capped: already rebooted 3x in the window


# --- the appliance action-in-flight pause ---------------------------------------------------------

def test_a_running_appliance_action_pauses_the_watchdog_entirely(h):
    h.set_server_ok(False)   # would otherwise be unhealthy
    h.write_appliance_status("running", action="update-system", age_seconds=60)

    r = h.run()
    assert r.returncode == 0
    assert h.status["action"] == "paused: update-system in flight"
    assert h.called == ""                 # no probe, no perform() call — nothing ran at all
    assert not h.state.exists()           # the fail counter was never touched


def test_a_queued_appliance_action_also_pauses(h):
    h.write_appliance_status("queued", action="reboot", age_seconds=5)
    h.run()
    assert h.status["action"] == "paused: reboot in flight"


def test_a_stale_in_flight_status_does_not_pause(h):
    """30 minutes is long enough for a real full-upgrade; past that, treat it as abandoned and probe
    normally rather than disabling self-heal forever."""
    h.set_server_ok(True)
    h.set_kiosk_ok(True)
    h.set_paint_ok(True)
    h.write_appliance_status("running", action="update-system", age_seconds=1900)

    h.run()
    assert h.status["action"] == "none"   # ran the real probes instead of pausing
    assert "curl" in h.called


def test_a_done_appliance_action_does_not_pause(h):
    h.set_server_ok(True)
    h.set_kiosk_ok(True)
    h.set_paint_ok(True)
    h.write_appliance_status("done", action="update-system", age_seconds=5)

    h.run()
    assert h.status["action"] == "none"
    assert "curl" in h.called


# --- e-ink-only appliance (no HDMI kiosk) -------------------------------------------------------
# The bug: kiosk_ok requires cage+chromium, which never run on an e-ink-only box, so `healthy` could
# never be 1 and `fails` pinned above ESCALATE_AFTER forever — the first server blip then jumped
# straight to reboot instead of trying restart-container first.

def test_eink_only_box_stays_healthy_with_server_up(h):
    h.enable_eink()
    h.set_connector(False)          # no HDMI attached
    h.set_server_ok(True)
    h.set_eink_active(True)

    st = h.tick()

    assert st["action"] == "none"
    assert st["display"] == "eink"
    assert st["kiosk_ok"] == 1 and st["paint_ok"] == 1 and st["live_ok"] == 1 and st["advance_ok"] == 1
    assert h.fails == 0
    assert "systemctl restart" not in h.called
    assert "docker" not in h.called
    assert "systemctl reboot" not in h.called


def test_eink_only_box_with_server_down_escalates_restart_container_before_reboot(h):
    """The actual regression: server_ok probing is untouched by e-ink detection, so a server-down
    box must still climb restart-container (fails 3-5) before reboot (fails 6+) — never straight to
    reboot the way the pinned-fails bug caused."""
    h.enable_eink()
    h.set_connector(False)
    h.set_server_ok(False)
    h.set_eink_active(True)

    actions = [h.tick()["action"] for _ in range(6)]

    assert actions == [
        "none", "none",
        "restart-container", "restart-container", "restart-container",
        "reboot",
    ]
    assert h.called.count("docker compose") == 3
    assert "systemctl reboot" in h.called


def test_eink_only_box_never_relaunches_kiosk_or_gives_up(h):
    """sd-eink dead (server up, eink liveness fails) must never pick relaunch-kiosk/give-up — neither
    rung applies without a browser. No fix is wired for this case; it stays "none" and unhealthy."""
    h.enable_eink()
    h.set_connector(False)
    h.set_server_ok(True)
    h.set_eink_active(False)

    actions = [h.tick()["action"] for _ in range(8)]

    assert set(actions) == {"none"}
    assert "systemctl restart getty" not in h.called
    assert "docker" not in h.called
    assert "systemctl reboot" not in h.called


# --- Gap 1: the two escalation ladders must not share a counter --------------------------------
# sd-eink is deliberately `disabled` during every bench panel session (it holds the SPI/GPIO lines),
# so "server up, sd-eink down" is not hypothetical. Before the fix this pinned the single shared
# `fails` counter above ESCALATE_AFTER with no remedy, so the NEXT unrelated server blip jumped
# straight to reboot, skipping restart-container — the exact ADR-121 regression, just narrower.

def test_eink_liveness_fault_never_inflates_the_server_side_counter(h):
    h.enable_eink()
    h.set_connector(False)
    h.set_server_ok(True)
    h.set_eink_active(False)   # sd-eink down, server fine — climbs fails_display, never fails_server

    for _ in range(10):
        h.tick()
    assert h.fails == 0             # server-side counter untouched
    assert h.fails_display >= 10    # display-side counter free to climb — nothing consumes it (no
                                     # kiosk to relaunch), but it must not leak into the server ladder

    # The server now blips down for the first time: must still climb restart-container (fails 3-5)
    # before reboot (fails 6+), starting fresh at 0 — NOT jump straight to reboot because fails_server
    # was pinned by the (unrelated) e-ink fault above.
    h.set_server_ok(False)
    actions = [h.tick()["action"] for _ in range(6)]
    assert actions == [
        "none", "none",
        "restart-container", "restart-container", "restart-container",
        "reboot",
    ]


def test_hdmi_box_behaviour_is_unchanged_by_no_eink(h):
    """A plain HDMI kiosk box (EINK_ENABLED unset/0) never runs the e-ink probe and must behave
    exactly as before this work — the primary regression risk. Same scenario as
    test_kiosk_dead_with_server_up_also_caps_at_relaunch_kiosk, restated with the DRM connector
    explicitly connected to prove that path doesn't change anything for a non-eink box."""
    h.set_connector(True)
    h.set_server_ok(True)
    h.set_kiosk_ok(False)
    h.set_paint_ok(True)

    for _ in range(5):
        st = h.tick()
    assert st["action"] == "relaunch-kiosk"
    assert st["display"] == "kiosk"

    st = h.tick()
    assert st["action"] == "give-up"
    assert "docker" not in h.called
    assert "systemctl reboot" not in h.called


# --- dual box: HDMI kiosk AND e-ink panel present together (Gap 2) -----------------------------
# EINK_ENABLED=1 + a CONNECTED connector is a real, expressible configuration (the bench conf has
# ALL_IN_ONE=1, EINK_ENABLED=1 AND OUTPUT=HDMI-A-1 simultaneously) — both surfaces must be probed
# independently and both must be able to fail without masking each other.

def test_dual_box_reports_both_and_probes_both_surfaces(h):
    h.enable_eink()
    h.set_connector(True)      # HDMI present
    h.set_server_ok(True)
    h.set_kiosk_ok(True)
    h.set_paint_ok(True)
    h.set_eink_active(True)    # sd-eink present and running

    st = h.tick()

    assert st["display"] == "both"
    assert st["action"] == "none"
    assert st["eink_live_ok"] == 1
    assert h.fails == 0 and h.fails_display == 0


def test_dual_box_dead_sd_eink_is_no_longer_invisible(h):
    """Before the fix, a dual box's eink_only=0 classification meant it was probed purely as a kiosk
    box and a dead sd-eink was never observed at all. Now the kiosk surface is healthy but the e-ink
    surface is dead: display_ok must go unhealthy and it must show up in eink_live_ok, still capping
    at relaunch-kiosk/give-up (no distinct e-ink remedy exists) rather than ever reaching the server
    ladder (restart-container/reboot)."""
    h.enable_eink()
    h.set_connector(True)
    h.set_server_ok(True)
    h.set_kiosk_ok(True)
    h.set_paint_ok(True)
    h.set_eink_active(False)   # sd-eink dead

    actions = [h.tick()["action"] for _ in range(6)]

    assert actions == [
        "none", "none",
        "relaunch-kiosk", "relaunch-kiosk", "relaunch-kiosk",
        "give-up",
    ]
    assert h.status["eink_live_ok"] == 0
    assert "docker" not in h.called
    assert "systemctl reboot" not in h.called


def test_missing_sys_class_drm_falls_back_to_kiosk_behaviour(h):
    """No /sys/class/drm at all (dev box / CI): has_kiosk falls back to 1 (assume a kiosk is
    expected — today's behaviour) even with EINK_ENABLED=1, which independently sets has_eink=1 —
    so this box reports "both", not "eink"."""
    h.enable_eink()
    h.no_drm()
    h.set_server_ok(True)
    h.set_kiosk_ok(False)
    h.set_paint_ok(True)

    for _ in range(5):
        st = h.tick()
    assert st["action"] == "relaunch-kiosk"
    assert st["display"] == "both"
