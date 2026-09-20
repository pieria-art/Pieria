"""sd-os-schedule — the weekly timer's two refusals (ADR-119).

It never upgrades anything itself: it drops the same request.json the GUI button writes, so the work
flows through sd-update's update-system arm with identical logging. What matters here is when it
declines to write that request at all, because this is the one path that reboots a box unattended.
"""

import json
import os
import pathlib
import subprocess

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "appliance" / "bin" / "sd-os-schedule"


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "repo"
    (root / "data" / "appliance").mkdir(parents=True)
    conf = tmp_path / "pieria.conf"
    return root, conf


def _run(root, conf):
    # `logger` is absent in many containers; keep the shim so the script's own path is what's tested.
    shim = pathlib.Path(root).parent / "bin"
    shim.mkdir(exist_ok=True)
    (shim / "logger").write_text("#!/bin/sh\nexit 0\n")
    (shim / "logger").chmod(0o755)
    return subprocess.run(["bash", str(_BIN), str(conf), str(root)], capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"})


def test_an_off_schedule_queues_nothing(box):
    root, conf = box
    conf.write_text("OS_UPDATE_SCHEDULE=off\n")
    assert _run(root, conf).returncode == 0
    assert not (root / "data" / "appliance" / "request.json").exists()


def test_a_conf_with_no_schedule_key_at_all_queues_nothing(box):
    root, conf = box
    conf.write_text("DISPLAY_ID=living_room\n")
    _run(root, conf)
    assert not (root / "data" / "appliance" / "request.json").exists()


def test_weekly_queues_an_update_system_request_marked_as_scheduled(box):
    root, conf = box
    conf.write_text("OS_UPDATE_SCHEDULE=weekly\n")
    assert _run(root, conf).returncode == 0
    appliance = root / "data" / "appliance"
    req = json.loads((appliance / "request.json").read_text())
    assert req["action"] == "update-system"
    assert req["source"] == "schedule"          # so the UI can say why an unasked-for upgrade is running
    status = json.loads((appliance / "status.json").read_text())
    assert status["state"] == "queued"
    assert status["nonce"] == req["nonce"]
    assert "schedule" in status["message"]


def test_the_root_written_files_are_readable_by_the_container(box):
    root, conf = box
    conf.write_text("OS_UPDATE_SCHEDULE=weekly\n")
    _run(root, conf)
    appliance = root / "data" / "appliance"
    for name in ("request.json", "status.json"):
        assert oct((appliance / name).stat().st_mode)[-3:] == "644"


@pytest.mark.parametrize("state", ["queued", "running"])
def test_it_skips_the_week_rather_than_stacking_on_an_action_in_flight(box, state):
    root, conf = box
    conf.write_text("OS_UPDATE_SCHEDULE=weekly\n")
    appliance = root / "data" / "appliance"
    (appliance / "status.json").write_text(json.dumps({"state": state, "action": "update-app"}))
    _run(root, conf)
    assert not (appliance / "request.json").exists()
    assert json.loads((appliance / "status.json").read_text())["action"] == "update-app"


def test_a_finished_action_does_not_block_the_schedule(box):
    root, conf = box
    conf.write_text("OS_UPDATE_SCHEDULE=weekly\n")
    appliance = root / "data" / "appliance"
    (appliance / "status.json").write_text(json.dumps({"state": "done", "action": "update-app"}))
    _run(root, conf)
    assert (appliance / "request.json").exists()


def test_the_timer_is_not_persistent(box):
    # A missed Sunday must be SKIPPED, never replayed at the next power-on — which would upgrade and
    # reboot the moment someone switched their art frame on.
    unit = _BIN.parent.parent / "systemd" / "sd-os-upgrade.timer"
    assert "Persistent=false" in unit.read_text()
