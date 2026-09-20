"""sd-conf — the conf validator/writer both gates of the ADR-119 bridge run through.

Loaded by path (the SourceFileLoader pattern from tests/test_watchdog_advance.py) because the host
helpers are extension-less executables, not importable modules.
"""

import importlib.machinery
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PATH = _ROOT / "deploy" / "appliance" / "bin" / "sd-conf"


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


sc = _load("sd_conf_under_test", _PATH)
wiz = _load("sd_setup_under_test", _ROOT / "deploy" / "appliance" / "setup" / "sd_setup.py")


SAMPLE = """# Pieria — Appliance configuration
# a comment that must survive

SERVER_URL=http://localhost:8000
DISPLAY_ID=living_room
ROTATE=
WATCHDOG=observe
GEMINI_API_KEY=sk-secret-value
EINK_ENABLED=1
"""


# --- set_keys: ADR-059 #1, the wizard bug that deleted EINK_*/WATCHDOG must not recur -------------

def test_set_keys_replaces_in_place_and_preserves_everything_else():
    out = sc.set_keys(SAMPLE, {"WATCHDOG": "enforce"})
    assert "WATCHDOG=enforce" in out
    assert "WATCHDOG=observe" not in out
    # comments, blank line, ordering, and every untouched key survive
    assert out.splitlines()[0] == "# Pieria — Appliance configuration"
    assert "# a comment that must survive" in out
    assert "EINK_ENABLED=1" in out
    assert "GEMINI_API_KEY=sk-secret-value" in out
    assert out.index("SERVER_URL") < out.index("DISPLAY_ID") < out.index("ROTATE")


def test_set_keys_appends_a_key_that_was_absent():
    out = sc.set_keys(SAMPLE, {"OS_UPDATE_SCHEDULE": "weekly"})
    assert out.rstrip().endswith("OS_UPDATE_SCHEDULE=weekly")
    assert "EINK_ENABLED=1" in out


def test_set_keys_drops_a_later_duplicate_that_would_win_when_sourced():
    # `.`-sourcing takes the LAST assignment, so leaving a duplicate silently undoes the edit.
    text = "ROTATE=\nOUTPUT=HDMI-A-1\nROTATE=180\n"
    out = sc.set_keys(text, {"ROTATE": "90"})
    assert out == "ROTATE=90\nOUTPUT=HDMI-A-1\n"


def test_set_keys_on_an_empty_conf_writes_the_keys():
    assert sc.set_keys("", {"TIMEZONE": "America/Chicago"}) == "TIMEZONE=America/Chicago\n"


def test_set_keys_ignores_a_commented_out_key():
    out = sc.set_keys("#ROTATE=270\nROTATE=\n", {"ROTATE": "90"})
    assert out == "#ROTATE=270\nROTATE=90\n"


def test_get_key_reads_the_first_assignment():
    assert sc.get_key(SAMPLE, "DISPLAY_ID") == "living_room"
    assert sc.get_key(SAMPLE, "ROTATE") == ""
    assert sc.get_key(SAMPLE, "NOPE") is None


# --- SAFE_VALUE_RE: the load-bearing control (the conf is `.`-sourced as shell) -------------------

@pytest.mark.parametrize("bad", [
    "x; rm -rf /", "`reboot`", "$(reboot)", "a b", "a\nB=c", "a'b", 'a"b', "a|b", "a&b", "a>b", "a*b",
    "${HOME}", "a\\b",
])
def test_safe_value_re_rejects_shell_metacharacters(bad):
    assert not sc.SAFE_VALUE_RE.match(bad)


@pytest.mark.parametrize("good", ["", "America/Chicago", "90", "living_room", "http://host:8000",
                                  "observe", "03:30", "a-b.c_d+e@f,g"])
def test_safe_value_re_accepts_what_the_conf_legitimately_holds(good):
    assert sc.SAFE_VALUE_RE.match(good)


def test_validate_rejects_an_injection_even_for_a_known_key():
    assert sc.validate("TIMEZONE", "America/Chicago; rm -rf /")


# --- the validator table --------------------------------------------------------------------------

@pytest.mark.parametrize("key,value,ok", [
    ("TIMEZONE", "America/Chicago", True),
    ("TIMEZONE", "Not/AZone", False),
    ("TIMEZONE", "", False),
    ("ROTATE", "", True), ("ROTATE", "90", True), ("ROTATE", "180", True), ("ROTATE", "270", True),
    ("ROTATE", "45", False), ("ROTATE", "landscape", False),
    ("EINK_ORIENTATION", "", True), ("EINK_ORIENTATION", "portrait", True),
    ("EINK_ORIENTATION", "landscape", False),
    ("DISPLAY_ID", "living_room", True), ("DISPLAY_ID", "Living Room", False), ("DISPLAY_ID", "", False),
    ("WATCHDOG", "observe", True), ("WATCHDOG", "enforce", True), ("WATCHDOG", "off", True),
    ("WATCHDOG", "on", False),
    ("OS_UPDATE_SCHEDULE", "off", True), ("OS_UPDATE_SCHEDULE", "weekly", True),
    ("OS_UPDATE_SCHEDULE", "daily", False),
    ("OS_UPDATE_TIME", "03:00", True), ("OS_UPDATE_TIME", "23:59", True),
    ("OS_UPDATE_TIME", "24:00", False), ("OS_UPDATE_TIME", "3:00", False),
    ("SERVER_URL", "http://192.168.1.50:8000", True), ("SERVER_URL", "ftp://x", False),
    ("HOSTNAME", "anything", False),        # ADR-083: hostname is NOT writable through this bridge
    ("GEMINI_API_KEY", "sk-x", False),      # never writable through the bridge
])
def test_validator_table(key, value, ok):
    assert (sc.validate(key, value) is None) is ok


def test_validate_rejects_an_overlong_value():
    assert sc.validate("DISPLAY_ID", "a" * 65)


# --- parity with the wizard -------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["Living Room!", "  HALLWAY  ", "kitchen", "--weird--", "a b c",
                                 "Ω unicode Ω", "", "___"])
def test_sanitize_display_id_matches_the_wizard_byte_for_byte(raw):
    assert sc.sanitize_display_id(raw) == wiz.sanitize_display_id(raw)


def test_orientations_mirror_the_wizard():
    assert sc.ORIENTATIONS == {k: v[0] for k, v in wiz.ORIENTATIONS.items()}


# --- export: the non-secret mirror -----------------------------------------------------------------

def test_export_view_never_leaks_the_api_key():
    view = sc.export_view(SAMPLE)
    assert view["DISPLAY_ID"] == "living_room"
    assert view["WATCHDOG"] == "observe"
    assert view["EINK_ENABLED"] == "1"
    assert not any("KEY" in k for k in view)
    assert "sk-secret-value" not in json.dumps(view)


# --- CLI round trip ---------------------------------------------------------------------------------

def _cli(*args, conf=None):
    cmd = [sys.executable, str(_PATH)]
    if conf:
        cmd += ["--conf", str(conf)]
    return subprocess.run(cmd + list(args), capture_output=True, text=True)


def test_cli_set_get_round_trip(tmp_path):
    conf = tmp_path / "pieria.conf"
    conf.write_text(SAMPLE)
    assert _cli("set", "ROTATE=90", "EINK_ORIENTATION=portrait", conf=conf).returncode == 0
    assert conf.read_text().count("ROTATE=") == 1
    assert _cli("get", "ROTATE", conf=conf).stdout.strip() == "90"
    assert _cli("get", "EINK_ORIENTATION", conf=conf).stdout.strip() == "portrait"
    assert "GEMINI_API_KEY=sk-secret-value" in conf.read_text()


def test_cli_set_validates_every_pair_before_writing_any(tmp_path):
    conf = tmp_path / "pieria.conf"
    conf.write_text(SAMPLE)
    before = conf.read_text()
    # ROTATE is fine, the second pair is not — the conf must be untouched, not half-applied.
    r = _cli("set", "ROTATE=90", "WATCHDOG=; rm -rf /", conf=conf)
    assert r.returncode == 2
    assert conf.read_text() == before


def test_cli_validate_exit_codes(tmp_path):
    assert _cli("validate", "WATCHDOG", "enforce").returncode == 0
    assert _cli("validate", "WATCHDOG", "banana").returncode == 2


def test_cli_export_writes_a_conf_json_without_secrets(tmp_path):
    conf = tmp_path / "pieria.conf"
    conf.write_text(SAMPLE)
    out = tmp_path / "conf.json"
    assert _cli("export", "--out", str(out), conf=conf).returncode == 0
    data = json.loads(out.read_text())
    assert data["values"]["DISPLAY_ID"] == "living_room"
    assert data["conf_path"] == str(conf)
    assert "exported_at" in data and "os_upgrade_timer" in data
    assert "sk-secret-value" not in out.read_text()


def test_write_atomic_leaves_no_temp_files_behind(tmp_path):
    conf = tmp_path / "pieria.conf"
    sc.write_atomic(conf, "A=1\n")
    assert conf.read_text() == "A=1\n"
    assert [p.name for p in tmp_path.iterdir()] == ["pieria.conf"]
