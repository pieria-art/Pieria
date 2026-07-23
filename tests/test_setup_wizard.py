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


def test_orientation_preview_degrades_gracefully_without_wlr_randr(monkeypatch):
    """On anything but the wlroots kiosk (dev laptop, or before the kiosk starts) wlr-randr is absent —
    the preview must report 'unavailable' + record the choice, never surface a raw errno as an error."""
    monkeypatch.setattr(sd_setup.shutil, "which", lambda _: None)  # simulate wlr-randr not installed
    cfg = sd_setup.SetupConfig(dry_run=True, all_in_one=False,
                               boot_conf=pathlib.Path("/tmp/none"), output="HDMI-A-1")
    result = sd_setup._apply_rotation("HDMI-A-1", "90", 30, cfg)
    assert result["mode"] == "unavailable"
    assert "error" not in result


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


def test_live_commit_writes_boot_conf_0644(tmp_path, monkeypatch):
    """The live commit path writes the boot conf world-readable (0644), deterministically — the FAT
    boot partition is read from any computer, so the mode must not depend on the process umask. Wi-Fi
    join + reboot are stubbed so no hardware is touched. Pre-create the conf 0600 to prove the explicit
    chmod overrides an existing restrictive mode (write_text alone would leave 0600).

    EVERY host-touching call on the live path must be stubbed here. Leaving `_release_wlan0` unstubbed
    made this test shell out to the developer's own `nmcli general reload`, which pops a polkit
    authentication dialog on the desktop mid-run (caught 2026-07-21)."""
    import stat

    monkeypatch.setattr(sd_setup, "_join_wifi", lambda *a, **k: None)
    monkeypatch.setattr(sd_setup, "_schedule_reboot", lambda: None)
    monkeypatch.setattr(sd_setup, "_release_wlan0", lambda: None)
    boot_conf = tmp_path / "boot" / "screen-docent.conf"
    boot_conf.parent.mkdir(parents=True)
    boot_conf.write_text("stale")            # pre-existing file...
    boot_conf.chmod(0o600)                    # ...with restrictive perms the commit must override

    cfg = sd_setup.SetupConfig(dry_run=False, all_in_one=True, boot_conf=boot_conf, output="HDMI-A-1")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), sd_setup.make_handler(cfg))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        status, data = _post(f"http://127.0.0.1:{httpd.server_address[1]}", "/api/commit", {
            "server_url": "http://localhost:8000", "display_id": "wall", "orientation": "landscape",
        })
    finally:
        httpd.shutdown()

    assert status == 200 and data["committed"] is True
    assert "DISPLAY_ID=wall" in boot_conf.read_text()
    assert stat.S_IMODE(boot_conf.stat().st_mode) == 0o644


def test_live_commit_releases_wlan0_after_saving_wifi_and_before_reboot(tmp_path, monkeypatch):
    """The setup AP holds wlan0 via a NetworkManager `unmanaged-devices` drop-in. That drop-in MUST be
    removed on commit: `_join_wifi` only SAVES the profile and relies on NM auto-connecting it after the
    reboot, so if wlan0 is still unmanaged the appliance finishes setup and then silently never joins
    Wi-Fi — a bricked box for a non-technical user. Order matters too: release must come AFTER the
    profile is saved (so nothing races the save) and BEFORE the reboot is scheduled (ADR-056)."""
    calls: list[str] = []
    monkeypatch.setattr(sd_setup, "_join_wifi", lambda *a, **k: calls.append("join_wifi"))
    monkeypatch.setattr(sd_setup, "_release_wlan0", lambda: calls.append("release_wlan0"))
    monkeypatch.setattr(sd_setup, "_schedule_reboot", lambda: calls.append("reboot"))

    boot_conf = tmp_path / "boot" / "screen-docent.conf"
    boot_conf.parent.mkdir(parents=True)
    cfg = sd_setup.SetupConfig(dry_run=False, all_in_one=True, boot_conf=boot_conf, output="HDMI-A-1")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), sd_setup.make_handler(cfg))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        status, _ = _post(f"http://127.0.0.1:{httpd.server_address[1]}", "/api/commit", {
            "server_url": "http://localhost:8000", "display_id": "wall", "orientation": "landscape",
            "wifi_ssid": "HomeNet", "wifi_pass": "hunter2",
        })
    finally:
        httpd.shutdown()

    assert status == 200
    assert calls == ["join_wifi", "release_wlan0", "reboot"]


def test_dry_run_commit_never_releases_wlan0(server, monkeypatch):
    """A dry run must not touch the host's network stack at all — no drop-in removal, no `nmcli`.
    (Unstubbed, this is what popped a polkit dialog on the developer's desktop.)"""
    called = []
    monkeypatch.setattr(sd_setup, "_release_wlan0", lambda: called.append(1))
    base, _ = server
    status, data = _post(base, "/api/commit", {
        "server_url": "http://localhost:8000", "display_id": "wall", "orientation": "landscape",
    })
    assert status == 200 and data["dry_run"] is True
    assert called == []


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


def test_mode_exposes_recovery_banner_text(tmp_path):
    """sd-net-recover re-opens this same wizard on a box that failed to get online. /api/mode carries
    the reason so the page can explain itself — without it the box looks like it spontaneously reset
    to factory setup, which is more alarming than the original failure (ADR-057)."""
    boot_conf = tmp_path / "boot" / "screen-docent.conf"
    msg = "This display couldn't join your Wi-Fi."
    cfg = sd_setup.SetupConfig(dry_run=True, all_in_one=False, boot_conf=boot_conf,
                               output="HDMI-A-1", recovery=msg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), sd_setup.make_handler(cfg))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/mode") as r:
            body = json.loads(r.read())
    finally:
        httpd.shutdown()
    assert body["recovery"] == msg


def test_mode_recovery_defaults_empty(server):
    """A normal first-run setup must NOT show a recovery banner."""
    base, _ = server
    with urllib.request.urlopen(base + "/api/mode") as r:
        assert json.loads(r.read())["recovery"] == ""


@pytest.mark.parametrize("raw,default,expected", [
    ("1", False, True), ("0", True, False), ("true", False, True), ("on", False, True),
    (None, True, True), (None, False, False), ("", True, True),
])
def test_pick_all_in_one(raw, default, expected):
    """ALL_IN_ONE used to come ONLY from the --all-in-one CLI flag, which sd-setup-boot never passes —
    so the flagship all-in-one .img could not write ALL_IN_ONE=1 from its own wizard. The form field now
    decides, falling back to the CLI default when absent."""
    fields = {} if raw is None else {"all_in_one": raw}
    assert sd_setup._pick_all_in_one(fields, default) is expected


def test_build_conf_honours_the_form_over_the_cli_default():
    conf = sd_setup.build_conf(
        {"server_url": "http://localhost:8000", "display_id": "wall",
         "orientation": "landscape", "all_in_one": "1"},
        all_in_one=False)          # CLI says no, the user said yes -> the user wins
    assert "ALL_IN_ONE=1" in conf


def test_scanned_networks_dedupes_meshes_and_sorts_by_signal(tmp_path, monkeypatch):
    """A mesh shows the same SSID once per radio (the bench saw 3Yosts five times). The picker must show
    each network ONCE, strongest first, or the list is unusable on exactly the setups most likely to
    have several access points."""
    cache = tmp_path / "networks.json"
    cache.write_text(json.dumps([
        {"ssid": "3Yosts", "signal": 52, "secure": True},
        {"ssid": "Neighbour", "signal": 61, "secure": True},
        {"ssid": "3Yosts", "signal": 74, "secure": True},
        {"ssid": "", "signal": 90, "secure": False},      # hidden/blank -> dropped
    ]))
    monkeypatch.setattr(sd_setup, "SCAN_CACHE", cache)
    nets = sd_setup._scanned_networks()
    # 3Yosts wins on its STRONGEST radio (74), not the 52 that happened to be listed first.
    assert [n["ssid"] for n in nets] == ["3Yosts", "Neighbour"]
    assert nets[0]["signal"] == 74


def test_scanned_networks_survives_a_missing_cache(tmp_path, monkeypatch):
    """No scan (AP already up, or scan failed) must degrade to free-text entry, never to an error."""
    monkeypatch.setattr(sd_setup, "SCAN_CACHE", tmp_path / "nope.json")
    assert sd_setup._scanned_networks() == []


def test_build_conf_preserves_settings_the_wizard_does_not_own():
    """The wizard emits a FIXED key set, so a re-run silently deleted everything else — an e-ink box
    that went through setup (or the ADR-057 recovery wizard) came back with EINK_ENABLED, saturation,
    cadence and WATCHDOG all gone, and nothing to say why."""
    existing = (
        "# comment\n"
        "SERVER_URL=http://old:8000\n"      # wizard-owned -> replaced
        "EINK_ENABLED=1\n"                  # not owned -> preserved
        "EINK_SATURATION=0.5\n"
        "EINK_MIN_INTERVAL=60\n"
        "WATCHDOG=observe\n"
    )
    conf = sd_setup.build_conf(
        {"server_url": "http://localhost:8000", "display_id": "wall", "orientation": "landscape"},
        all_in_one=True, existing=existing)
    assert "SERVER_URL=http://localhost:8000" in conf
    assert "SERVER_URL=http://old:8000" not in conf
    for kept in ("EINK_ENABLED=1", "EINK_SATURATION=0.5", "EINK_MIN_INTERVAL=60", "WATCHDOG=observe"):
        assert kept in conf, f"wizard clobbered {kept}"


@pytest.mark.parametrize("orientation,rotate,eink", [
    ("landscape", "", ""), ("90", "90", "portrait"), ("270", "270", "portrait"), ("180", "180", ""),
])
def test_orientation_reaches_both_hdmi_and_eink(orientation, rotate, eink):
    """One orientation choice must drive BOTH surfaces: ROTATE for wlroots/HDMI and EINK_ORIENTATION for
    the e-ink client, which reads its own variable and would otherwise stay landscape on a panel the
    user just told us is portrait. 180 is still a landscape panel, so it is NOT portrait for e-ink."""
    conf = sd_setup.build_conf(
        {"server_url": "http://localhost:8000", "display_id": "wall", "orientation": orientation})
    assert f"ROTATE={rotate}\n" in conf
    assert f"EINK_ORIENTATION={eink}\n" in conf


def test_build_conf_first_boot_has_nothing_to_preserve():
    conf = sd_setup.build_conf(
        {"server_url": "http://localhost:8000", "display_id": "wall", "orientation": "landscape"})
    assert "EINK_ENABLED" not in conf


# --- hostname (ADR-070) -------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Living Room", "living-room"),        # spaces -> hyphen, the headline case
    ("Den_TV", "den-tv"),                  # underscores -> hyphens (DNS labels can't hold _)
    ("  --Hall--  ", "hall"),              # trim stray separators
    ("Kids' Room!!", "kids-room"),         # drop punctuation, collapse
    ("café", "caf"),                       # non-ascii dropped, remainder valid
    ("", ""),                              # nothing usable -> blank (caller keeps baked default)
    ("---", ""),                           # only separators -> blank, NOT a leading/trailing hyphen
    ("a" * 80, "a" * 63),                  # clamp to a 63-char DNS label
])
def test_derive_hostname(raw, expected):
    assert sd_setup.derive_hostname(raw) == expected


@pytest.mark.parametrize("name,ok", [
    ("living-room", True), ("docent-4f9a", True), ("a", True),
    ("-lead", False), ("trail-", False), ("Up_per", False), ("has space", False), ("", False),
])
def test_valid_hostname(name, ok):
    assert sd_setup.valid_hostname(name) is ok


def test_resolve_hostname_explicit_beats_derived():
    # An advanced user opened the edit field and typed a real box name — it wins over the display name.
    assert sd_setup.resolve_hostname({"display_id": "Living Room", "hostname": "docent-hub"}) == "docent-hub"


def test_resolve_hostname_falls_back_to_display_name():
    # Gramps never touches the field -> the hostname follows the display name.
    assert sd_setup.resolve_hostname({"display_id": "Living Room", "hostname": ""}) == "living-room"


def test_resolve_hostname_ignores_an_invalid_explicit_entry():
    # Garbage typed into the advanced field must not become the hostname; derive instead.
    assert sd_setup.resolve_hostname({"display_id": "Kitchen", "hostname": "-bad-"}) == "kitchen"


def test_build_conf_writes_the_derived_hostname():
    conf = sd_setup.build_conf(
        {"server_url": "http://localhost:8000", "display_id": "Living Room", "orientation": "landscape"})
    assert "HOSTNAME=living-room\n" in conf


def test_validate_rejects_a_bad_explicit_hostname_but_not_a_blank_one():
    base = {"server_url": "http://localhost:8000", "display_id": "wall", "orientation": "landscape"}
    assert "hostname" not in sd_setup.validate_fields(base)                       # blank is fine
    assert "hostname" not in sd_setup.validate_fields({**base, "hostname": "hub"})  # valid is fine
    assert "hostname" in sd_setup.validate_fields({**base, "hostname": "-nope-"})   # garbage is caught
