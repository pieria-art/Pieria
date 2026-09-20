"""sd-metrics — the conf mirror it keeps for the container (ADR-119).

The app cannot read /boot/firmware, so data/appliance/conf.json is the only way the admin UI knows
what the device's settings actually are. sd-update re-exports after every edit; this timer is what
makes a HAND edit (SD card in a laptop) show up instead of silently disagreeing with the UI.
"""

import json
import os
import pathlib
import subprocess

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "appliance" / "bin"

CONF = "DISPLAY_ID=living_room\nWATCHDOG=observe\nGEMINI_API_KEY=sk-not-a-real-key\n"


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "repo"
    (root / "data" / "appliance").mkdir(parents=True)
    conf = tmp_path / "pieria.conf"
    conf.write_text(CONF)
    return root, conf


def _run(root, conf):
    return subprocess.run(["bash", str(_BIN / "sd-metrics"), str(root), str(conf)],
                          capture_output=True, text=True, env={**os.environ, "PATH": os.environ["PATH"]})


def test_the_mirror_is_written_when_it_is_missing(box):
    root, conf = box
    assert _run(root, conf).returncode == 0
    data = json.loads((root / "data" / "appliance" / "conf.json").read_text())
    assert data["values"]["DISPLAY_ID"] == "living_room"


def test_the_mirror_never_carries_the_api_key(box):
    root, conf = box
    _run(root, conf)
    assert "sk-not-a-real-key" not in (root / "data" / "appliance" / "conf.json").read_text()


def test_a_hand_edited_conf_is_picked_up_on_the_next_tick(box):
    root, conf = box
    _run(root, conf)
    out = root / "data" / "appliance" / "conf.json"
    first = out.read_text()
    conf.write_text(CONF.replace("observe", "enforce"))
    os.utime(conf, (out.stat().st_mtime + 10, out.stat().st_mtime + 10))
    _run(root, conf)
    assert out.read_text() != first
    assert json.loads(out.read_text())["values"]["WATCHDOG"] == "enforce"


def test_an_up_to_date_mirror_is_left_alone(box):
    root, conf = box
    _run(root, conf)
    out = root / "data" / "appliance" / "conf.json"
    before = out.stat().st_mtime_ns
    _run(root, conf)
    assert out.stat().st_mtime_ns == before


def test_a_missing_conf_is_not_an_error(box):
    root, _ = box
    assert _run(root, "/nonexistent/pieria.conf").returncode == 0
