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


def test_host_metrics_symlink_target_is_left_untouched(box, tmp_path):
    # L6: data/appliance/ is writable by the unprivileged container — a chmod-by-path after the
    # rename-over would be a TOCTOU window for a symlink replanted at host_metrics.json.
    root, conf = box
    outside = tmp_path / "outside.json"
    outside.write_text('{"do":"not touch"}')
    outside.chmod(0o600)
    before_mode = outside.stat().st_mode
    before_text = outside.read_text()

    out = root / "data" / "appliance" / "host_metrics.json"
    out.symlink_to(outside)

    assert _run(root, conf).returncode == 0
    assert outside.read_text() == before_text
    assert outside.stat().st_mode == before_mode
    assert not out.is_symlink()   # rename replaced the link with a real file
    assert "throttled" in json.loads(out.read_text())


def test_conf_json_symlink_target_is_left_untouched(box, tmp_path):
    # L6/item 2: conf.json used to be written by `sd-conf export --out PATH` (mkstemp in the
    # container-writable dir + chown/chmod BY PATH) — a symlink planted at that name would have been
    # followed. Now sd-conf streams to stdout (`--out -`) and sd-metrics places it via sd-mailbox,
    # whose atomic rename replaces the directory entry rather than writing through it.
    root, conf = box
    outside = tmp_path / "outside.json"
    outside.write_text('{"do":"not touch"}')
    outside.chmod(0o600)
    before_mode = outside.stat().st_mode
    before_text = outside.read_text()

    out = root / "data" / "appliance" / "conf.json"
    out.symlink_to(outside)
    # Force staleness (conf newer than the symlinked "mirror") so the export actually fires — a fresh
    # symlink would otherwise look up-to-date and the write would never be attempted at all.
    os.utime(conf, (outside.stat().st_mtime + 10, outside.stat().st_mtime + 10))

    assert _run(root, conf).returncode == 0
    assert outside.read_text() == before_text
    assert outside.stat().st_mode == before_mode
    assert not out.is_symlink()   # rename replaced the link with a real file
    assert json.loads(out.read_text())["values"]["DISPLAY_ID"] == "living_room"


def test_staleness_is_decided_via_sd_mailbox_stat_not_a_by_path_stat(box):
    # item 2: the old check was `[ ! -f "$CONF_JSON" ] || [ "$CONF" -nt "$CONF_JSON" ]` — BY PATH,
    # so it would follow a symlinked mirror to decide freshness. Prove the new check still does its
    # job across repeated ticks: unchanged conf -> no rewrite, changed (newer) conf -> rewrite.
    root, conf = box
    _run(root, conf)
    out = root / "data" / "appliance" / "conf.json"
    mid = out.stat().st_mtime_ns
    _run(root, conf)   # conf unchanged -> mirror left alone
    assert out.stat().st_mtime_ns == mid
    conf.write_text(CONF.replace("observe", "enforce"))
    os.utime(conf, (out.stat().st_mtime + 10, out.stat().st_mtime + 10))
    _run(root, conf)
    assert json.loads(out.read_text())["values"]["WATCHDOG"] == "enforce"


def test_a_symlinked_appliance_dir_is_refused_and_the_outside_dir_untouched(tmp_path):
    """The container can replace data/appliance ITSELF with a symlink (e.g. to /etc) — every
    read/write/chown sd-metrics does there now goes through sd-mailbox, which opens data/ then
    "appliance" with O_NOFOLLOW. A symlinked appliance dir must make the script write NOTHING into
    the outside target, and must not crash."""
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "data" / "appliance").symlink_to(outside)
    conf = tmp_path / "pieria.conf"
    conf.write_text(CONF)

    assert _run(root, conf).returncode == 0
    assert not (outside / "host_metrics.json").exists()
    assert (root / "data" / "appliance").is_symlink()
