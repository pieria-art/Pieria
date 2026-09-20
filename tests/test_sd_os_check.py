"""sd-os-check — parsing apt's plan, and deciding whether it needs a reboot (ADR-119).

Everything here is the pure half: the CLI talks to apt, which does not exist on the dev box and must
never be reached from a test.
"""

import importlib.machinery
import importlib.util
import json
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "appliance" / "bin" / "sd-os-check"
_loader = importlib.machinery.SourceFileLoader("sd_os_check", str(_PATH))
_spec = importlib.util.spec_from_loader("sd_os_check", _loader)
oc = importlib.util.module_from_spec(_spec)
_loader.exec_module(oc)


# A real `apt-get -s full-upgrade` tail from a Raspberry Pi OS box: an epoch version, a plain
# upgrade, a brand-new package with no [from] part, a removal, and the Conf noise.
SIMULATION = """NOTE: This is only a simulation!
      apt-get needs root privileges for real execution.
Inst chromium-browser [1:149.0.7827.1-rpt1] (1:150.0.7890.2-rpt1 rpi-distro:stable [arm64])
Inst libc6 [2.36-9+rpt2+deb12u7] (2.36-9+rpt2+deb12u8 Raspbian:stable [arm64])
Inst rpi-eeprom [21.1] (22.3 Raspbian:stable [all])
Inst libnewdependency (1.2.3 Debian:12/stable [arm64])
Remv obsolete-thing [1.0-2]
Conf chromium-browser (1:150.0.7890.2-rpt1 rpi-distro:stable [arm64])
Conf libc6 (2.36-9+rpt2+deb12u8 Raspbian:stable [arm64])
"""


def test_parse_reads_every_inst_line_including_an_epoch_version():
    parsed = oc.parse_simulation(SIMULATION)
    names = [p["name"] for p in parsed["packages"]]
    assert names == ["chromium-browser", "libc6", "rpi-eeprom", "libnewdependency"]
    chromium = parsed["packages"][0]
    assert chromium["from"] == "1:149.0.7827.1-rpt1"
    assert chromium["to"] == "1:150.0.7890.2-rpt1"


def test_a_brand_new_package_has_no_from_version():
    parsed = oc.parse_simulation(SIMULATION)
    new = [p for p in parsed["packages"] if p["name"] == "libnewdependency"][0]
    assert new["from"] == ""
    assert new["to"] == "1.2.3"


def test_removals_are_collected_separately():
    assert oc.parse_simulation(SIMULATION)["removals"] == ["obsolete-thing"]


def test_conf_lines_are_ignored():
    # apt emits one Conf per package it would configure; counting them roughly doubles every number.
    assert len(oc.parse_simulation(SIMULATION)["packages"]) == 4


def test_parse_of_an_empty_or_up_to_date_plan():
    assert oc.parse_simulation("")["packages"] == []
    assert oc.parse_simulation("Reading package lists...\n0 upgraded, 0 newly installed.\n")["packages"] == []


# --- reboot verdict ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,needs", [
    ("linux-image-6.6.20-v8", True),
    ("linux-headers-rpi-v8", True),
    ("raspberrypi-kernel", True),
    ("raspi-firmware", True),
    ("rpi-eeprom", True),
    ("firmware-brcm80211", True),
    ("libc6", True),
    ("systemd", True),
    ("udev", True),
    ("dbus", True),
    ("containerd.io", True),
    ("docker-ce", True),
    ("docker.io", True),
    ("seatd", True),
    ("libseat1", True),
    # A new browser is picked up by a kiosk RELAUNCH, which update-system does anyway — far cheaper
    # than a reboot, and ADR-118's whole problem was a wedged browser, not a stale kernel.
    ("chromium-browser", False),
    ("chromium", False),
    ("cage", False),
    ("base-files", False),
    ("libc-bin", False),          # not libc6 itself
    ("python3-systemd", False),   # must anchor at the start
])
def test_reboot_table(name, needs):
    assert oc.reboot_likely([{"name": name}])[0] is needs


def test_reboot_reasons_are_named_not_just_flagged():
    ok, reasons = oc.reboot_likely([{"name": "chromium-browser"}, {"name": "rpi-eeprom"},
                                    {"name": "containerd.io"}])
    assert ok is True
    assert reasons == ["rpi-eeprom", "containerd.io"]


def test_nothing_pending_means_no_reboot():
    assert oc.reboot_likely([]) == (False, [])


def test_reboot_likely_accepts_bare_names_too():
    assert oc.reboot_likely(["rpi-eeprom"])[0] is True


# --- the report -------------------------------------------------------------------------------

def test_report_shape():
    report = oc.build_report(oc.parse_simulation(SIMULATION), duration_s=2.34)
    assert report["count"] == 4
    assert report["removals"] == ["obsolete-thing"]
    assert report["truncated"] is False
    assert report["reboot_likely"] is True
    assert set(report["reboot_reasons"]) == {"libc6", "rpi-eeprom"}
    assert report["error"] == ""
    assert report["duration_s"] == 2.3
    assert report["checked_at"].endswith("Z")


def test_a_long_list_is_capped_but_the_count_is_honest():
    parsed = {"packages": [{"name": f"pkg{i}", "from": "1", "to": "2"} for i in range(120)],
              "removals": []}
    report = oc.build_report(parsed)
    assert report["count"] == 120          # what the box actually faces
    assert len(report["packages"]) == 50   # what we bother listing
    assert report["truncated"] is True


def test_an_error_report_still_has_every_field():
    report = oc.build_report({"packages": [], "removals": []}, error="apt-get not found")
    assert report["error"] == "apt-get not found"
    assert report["count"] == 0 and report["reboot_likely"] is False


# --- the host path, without ever touching apt ---------------------------------------------------

def test_the_check_bows_out_while_an_update_is_running(tmp_path, monkeypatch):
    appliance = tmp_path / "repo" / "data" / "appliance"
    appliance.mkdir(parents=True)
    (appliance / "status.json").write_text('{"state": "running", "action": "update-system"}')
    monkeypatch.setenv("SD_LOCK_FILE", str(tmp_path / "lock"))
    assert oc.main([str(tmp_path / "repo")]) == 0
    report = json.loads((appliance / "os-updates.json").read_text())
    assert report["error"] == "update in progress"


def test_an_unrelated_running_action_does_not_block_the_check(tmp_path):
    appliance = tmp_path / "repo" / "data" / "appliance"
    appliance.mkdir(parents=True)
    (appliance / "status.json").write_text('{"state": "running", "action": "set-timezone"}')
    assert oc._update_in_progress(appliance) is False


def test_a_missing_apt_is_reported_not_raised(tmp_path, monkeypatch):
    appliance = tmp_path / "repo" / "data" / "appliance"
    appliance.mkdir(parents=True)
    monkeypatch.setenv("SD_LOCK_FILE", str(tmp_path / "lock"))
    monkeypatch.setattr(oc, "_run_apt", lambda args: (_ for _ in ()).throw(FileNotFoundError()))
    assert oc.main([str(tmp_path / "repo")]) == 0
    assert "apt-get not found" in json.loads((appliance / "os-updates.json").read_text())["error"]


def test_print_reboot_writes_the_verdict_to_stdout(tmp_path, monkeypatch, capsys):
    appliance = tmp_path / "repo" / "data" / "appliance"
    appliance.mkdir(parents=True)
    monkeypatch.setenv("SD_LOCK_FILE", str(tmp_path / "lock"))

    class _Fake:
        returncode = 0
        stdout = SIMULATION
        stderr = ""
    monkeypatch.setattr(oc, "_run_apt", lambda args: _Fake())
    assert oc.main([str(tmp_path / "repo"), "--no-update", "--print-reboot"]) == 0
    assert capsys.readouterr().out.strip() == "1"


def test_the_report_is_world_readable_for_the_container(tmp_path, monkeypatch):
    appliance = tmp_path / "repo" / "data" / "appliance"
    appliance.mkdir(parents=True)
    oc.write_report(appliance, oc.build_report({"packages": [], "removals": []}))
    assert oct((appliance / "os-updates.json").stat().st_mode)[-3:] == "644"
