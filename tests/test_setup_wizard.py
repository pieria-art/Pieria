"""First-run setup wizard (R1-F1).

sd_setup is the stdlib wizard that writes screen-docent.conf on first boot. These lock the pure conf
logic (validation, conf bytes, orientation mapping) and prove the safety contract that makes the
in-situ dry-run trustworthy: a dry-run commit writes only the preview file and NEVER the real boot conf.
"""
import importlib.util
import json
import pathlib
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

# sd_setup lives under the appliance deploy tree, not on the default path — load it by file path.
_SD_SETUP_PATH = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "appliance" / "setup" / "sd_setup.py"
_spec = importlib.util.spec_from_file_location("sd_setup", _SD_SETUP_PATH)
sd_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd_setup)


# --- pure logic ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Living Room", "living_room"),
    ("Den TV!! 2", "den_tv_2"),
    ("  --Hall--  ", "hall"),
    ("", ""),
])
def test_sanitize_display_id(raw, expected):
    assert sd_setup.sanitize_display_id(raw) == expected


def test_validate_accepts_good_fields():
    assert sd_setup.validate_fields(
        {"server_url": "http://192.168.1.50:8000", "display_id": "den", "orientation": "90"}) == {}


def test_validate_reports_each_bad_field():
    errs = sd_setup.validate_fields({"server_url": "not-a-url", "display_id": "!!!", "orientation": "sideways"})
    assert set(errs) == {"server_url", "display_id", "orientation"}


def test_validate_wifi_is_optional():
    # No SSID (wired box) is fine.
    assert sd_setup.validate_fields(
        {"server_url": "http://localhost:8000", "display_id": "wall", "orientation": "landscape"}) == {}


def test_build_conf_maps_orientation_and_sanitizes():
    conf = sd_setup.build_conf(
        {"server_url": "http://localhost:8000", "display_id": "Den TV", "orientation": "270"},
        all_in_one=True)
    assert "DISPLAY_ID=den_tv" in conf
    assert "ROTATE=270" in conf
    assert "ALL_IN_ONE=1" in conf
    assert "SERVER_URL=http://localhost:8000" in conf


def test_build_conf_landscape_leaves_rotate_blank():
    conf = sd_setup.build_conf(
        {"server_url": "http://localhost:8000", "display_id": "wall", "orientation": "landscape"})
    assert "ROTATE=\n" in conf
    assert "ALL_IN_ONE=0" in conf


def test_resolve_boot_conf_path_targets_the_conf_file():
    assert sd_setup.resolve_boot_conf_path().name == "screen-docent.conf"


# --- dry-run safety contract (integration) ------------------------------------

@pytest.fixture
def server(tmp_path):
    """A wizard server in dry-run, with the boot conf pointed at a path that must stay untouched."""
    boot_conf = tmp_path / "boot" / "screen-docent.conf"
    cfg = sd_setup.SetupConfig(dry_run=True, all_in_one=False, boot_conf=boot_conf, output="HDMI-A-1")
    cfg.preview_path = tmp_path / "preview" / "screen-docent.conf"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), sd_setup.make_handler(cfg))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    yield f"http://127.0.0.1:{port}", cfg
    httpd.shutdown()


def _post(base, path, payload):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:  # noqa: S310 — localhost test server
        return r.status, json.loads(r.read())


def _get(base, path):
    with urllib.request.urlopen(base + path) as r:  # noqa: S310
        return r.status, r.read()


def test_dry_run_commit_writes_preview_only_never_boot_conf(server):
    base, cfg = server
    status, data = _post(base, "/api/commit", {
        "server_url": "http://localhost:8000", "display_id": "Den TV", "orientation": "90",
        "wifi_ssid": "HomeNet", "wifi_pass": "secret",
    })
    assert status == 200
    assert data["dry_run"] is True
    assert "DISPLAY_ID=den_tv" in data["conf"]
    assert data["would_join_wifi"] == "HomeNet"
    # The safety contract: preview written, real boot conf NEVER created.
    assert cfg.preview_path.exists()
    assert "DISPLAY_ID=den_tv" in cfg.preview_path.read_text()
    assert not cfg.boot_conf.exists()


def test_commit_rejects_invalid_fields(server):
    base, _ = server
    req = urllib.request.Request(base + "/api/commit",
                                 data=json.dumps({"server_url": "bad", "display_id": "", "orientation": "x"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req)  # noqa: S310
        assert False, "expected 422"
    except urllib.error.HTTPError as e:
        assert e.code == 422
        assert "errors" in json.loads(e.read())


def test_captive_probe_redirects_to_wizard(server):
    base, _ = server
    # urllib follows redirects; a 302 -> "/" lands on the wizard HTML (200).
    status, body = _get(base, "/generate_204")
    assert status == 200
    assert b"Set up your display" in body


def test_mode_endpoint_reports_dry_run(server):
    base, _ = server
    status, body = _get(base, "/api/mode")
    assert json.loads(body)["dry_run"] is True
