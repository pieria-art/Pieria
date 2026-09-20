"""sd-support-bundle — the redaction that makes the bundle safe to send us (ADR-119).

The bundle exists because a shipped box has no shell (ADR-064), which means the user is the one who
mails it to us or pastes it into an issue. Anything secret in it is therefore published. The
redaction is a shell FUNCTION precisely so it can be driven directly here, rather than being an
inline sed nobody ever exercises.
"""

import pathlib
import subprocess

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
