"""sd-support-bundle — the redaction that makes the bundle safe to send us (ADR-119).

The bundle exists because a shipped box has no shell (ADR-064), which means the user is the one who
mails it to us or pastes it into an issue. Anything secret in it is therefore published. The
redaction is a shell FUNCTION precisely so it can be driven directly here, rather than being an
inline sed nobody ever exercises.
"""

import os
import pathlib
import subprocess
import tarfile

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "appliance" / "bin" / "sd-support-bundle"


def redact(text: str) -> str:
    """Source the script as a library (it stops before collecting anything) and pipe through it."""
    r = subprocess.run(
        ["bash", "-c", f'SD_SUPPORT_BUNDLE_LIB=1 . "{_BIN}"; redact_conf'],
        input=text, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


# Deliberately un-secret-looking values: the point under test is the redaction, and a fixture that
# LOOKS like a real key trips the repo's own gitleaks pre-commit hook.
CONF = """# Pieria — Appliance configuration
SERVER_URL=http://localhost:8000
DISPLAY_ID=living_room
GEMINI_API_KEY=NOT-A-REAL-KEY-gemini-value
WIFI_PASSWORD=hunter2
SD_APPLIANCE_UPDATE_TOKEN=not-a-real-token
MUSEUM_API_SECRET=shhh
WPA_PSK=correcthorsebatterystaple
WATCHDOG=observe
TIMEZONE=America/Chicago
"""


@pytest.mark.parametrize("secret", ["NOT-A-REAL-KEY-gemini-value", "hunter2",
                                    "not-a-real-token", "shhh", "correcthorsebatterystaple"])
def test_no_secret_value_survives(secret):
    assert secret not in redact(CONF)


@pytest.mark.parametrize("key", ["GEMINI_API_KEY", "WIFI_PASSWORD", "SD_APPLIANCE_UPDATE_TOKEN",
                                 "MUSEUM_API_SECRET", "WPA_PSK"])
def test_the_key_name_is_kept_so_a_reader_can_see_it_is_set(key):
    assert f"{key}=<redacted>" in redact(CONF)


def test_ordinary_settings_are_untouched():
    out = redact(CONF)
    assert "SERVER_URL=http://localhost:8000" in out
    assert "DISPLAY_ID=living_room" in out
    assert "WATCHDOG=observe" in out
    assert "TIMEZONE=America/Chicago" in out
    assert out.splitlines()[0] == "# Pieria — Appliance configuration"


def test_a_leading_indent_does_not_smuggle_a_secret_through():
    assert "hunter2" not in redact("   WIFI_PASSWORD=hunter2\n")


def test_spaces_around_the_equals_do_not_smuggle_a_secret_through():
    assert "hunter2" not in redact("WIFI_PASSWORD = hunter2\n")


def test_an_empty_conf_is_fine():
    assert redact("") == ""


def test_the_script_never_asks_nmcli_for_secrets():
    # --show-secrets would print every saved Wi-Fi PSK in plain text. It appears only in the comment
    # that says so; no executable line may carry it.
    code = [ln for ln in _BIN.read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "--show-secrets" in ln]


def test_the_env_file_contributes_names_only():
    body = _BIN.read_text()
    assert 'sed -E \'s/=.*//\' "$REPO_ROOT/.env"' in body


# --- L6: data/appliance/ is writable by the unprivileged container ---------------------------------

def test_a_symlinked_bridge_state_file_is_skipped_not_followed(tmp_path):
    # watchdog.json (etc.) is copied into the bundle from data/appliance/ — if replaced with a
    # symlink, `cp -f` would follow it and put an arbitrary file's contents (read as root) into a
    # bundle the user may paste into a public issue.
    root = tmp_path / "repo"
    appliance = root / "data" / "appliance"
    appliance.mkdir(parents=True)
    outside = tmp_path / "outside_secret.json"
    outside.write_text("do not touch")
    outside.chmod(0o600)
    before_mode = outside.stat().st_mode
    before_text = outside.read_text()

    (appliance / "watchdog.json").symlink_to(outside)

    r = subprocess.run(["bash", str(_BIN), str(root)], capture_output=True, text=True,
                       env={**os.environ, "SD_CONF": str(tmp_path / "no-such-conf")})
    assert r.returncode == 0, r.stderr

    assert outside.read_text() == before_text
    assert outside.stat().st_mode == before_mode
    assert (appliance / "watchdog.json").is_symlink()   # skipped, never replaced

    tgz = appliance / "support-bundle.tar.gz"
    assert tgz.exists()
    with tarfile.open(tgz) as tf:
        names = tf.getnames()
        assert not any(n.endswith("appliance/watchdog.json") for n in names)
        contents = b"".join(tf.extractfile(n).read() for n in names if not tf.getmember(n).isdir())
    assert b"do not touch" not in contents


def test_the_bundle_itself_is_world_readable(tmp_path):
    root = tmp_path / "repo"
    (root / "data" / "appliance").mkdir(parents=True)
    r = subprocess.run(["bash", str(_BIN), str(root)], capture_output=True, text=True,
                       env={**os.environ, "SD_CONF": str(tmp_path / "no-such-conf")})
    assert r.returncode == 0, r.stderr
    out = root / "data" / "appliance" / "support-bundle.tar.gz"
    assert oct(out.stat().st_mode)[-3:] == "644"


def test_a_symlinked_appliance_dir_is_refused_and_the_outside_dir_untouched(tmp_path):
    """The container can replace data/appliance ITSELF with a symlink (e.g. to /etc). Both the
    bridge-state reads and the final tarball publish now go through sd-mailbox, which opens data/
    then "appliance" with O_NOFOLLOW — a symlinked appliance dir must leave the outside target
    untouched (no support-bundle.tar.gz written there), and the run must still complete."""
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "data" / "appliance").symlink_to(outside)

    r = subprocess.run(["bash", str(_BIN), str(root)], capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    assert not (outside / "support-bundle.tar.gz").exists()
    assert (root / "data" / "appliance").is_symlink()
