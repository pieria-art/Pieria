"""install.sh's ADR-083 legacy-conf migration + orphan cleanup (ADR-148).

install.sh provisions a whole host, so it cannot run here. The migration block is self-contained
(`_conf_is_placeholder` plus the `for d in ...` loop up to the flavour read), so this slices exactly that
text out of the REAL script, points its three directories at a tmp dir, and runs it under bash —
a rename of the block's contract fails here, not on a living-room Pi.
"""

import pathlib
import subprocess

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = (_ROOT / "deploy" / "appliance" / "install.sh").read_text()

_START = "_conf_is_placeholder() {"
_END = "for d in /boot/firmware /boot /etc; do\n  if [ -r"
CONFIGURED = "SERVER_URL=http://localhost:8000\nDISPLAY_ID=hall_tv\n"
PLACEHOLDER = "SERVER_URL=http://192.168.1.50:8000\nDISPLAY_ID=living_room\n"


def _block(d):
    block = _SRC[_SRC.index(_START):_SRC.index(_END)]
    return block.replace("for d in /boot/firmware /boot /etc; do", f"for d in {d}; do")


def _run(d):
    return subprocess.run(["bash", "-c", _block(d)], capture_output=True, text=True)


def test_install_sh_parses():
    assert subprocess.run(["bash", "-n", str(_ROOT / "deploy/appliance/install.sh")]).returncode == 0


def test_legacy_conf_is_migrated_then_renamed_not_deleted(tmp_path):
    (tmp_path / "screen-docent.conf").write_text(CONFIGURED)
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "pieria.conf").read_text() == CONFIGURED
    assert not (tmp_path / "screen-docent.conf").exists()
    assert (tmp_path / "screen-docent.conf.migrated").read_text() == CONFIGURED
    assert "MIGRATED" in r.stdout and "renamed" in r.stdout


def test_a_placeholder_pieria_conf_is_replaced_by_the_configured_legacy(tmp_path):
    (tmp_path / "screen-docent.conf").write_text(CONFIGURED)
    (tmp_path / "pieria.conf").write_text(PLACEHOLDER)
    _run(tmp_path)
    assert (tmp_path / "pieria.conf").read_text() == CONFIGURED
    assert (tmp_path / "screen-docent.conf.migrated").exists()


def test_second_run_is_a_no_op(tmp_path):
    (tmp_path / "screen-docent.conf").write_text(CONFIGURED)
    _run(tmp_path)
    r = _run(tmp_path)
    assert r.returncode == 0 and r.stdout == ""
    assert (tmp_path / "screen-docent.conf.migrated").read_text() == CONFIGURED


def test_an_unverified_new_conf_leaves_the_legacy_file_alone(tmp_path):
    # A placeholder pieria.conf and a placeholder legacy: nothing was migrated, so nothing is orphaned.
    (tmp_path / "screen-docent.conf").write_text(PLACEHOLDER)
    (tmp_path / "pieria.conf").write_text(PLACEHOLDER)
    _run(tmp_path)
    assert (tmp_path / "screen-docent.conf").exists()
    assert not (tmp_path / "screen-docent.conf.migrated").exists()


def test_a_pieria_conf_that_does_not_parse_leaves_the_legacy_file_alone(tmp_path):
    (tmp_path / "screen-docent.conf").write_text(CONFIGURED)
    (tmp_path / "pieria.conf").write_text("SERVER_URL=http://x:1\nDISPLAY_ID=a\nif then fi ((\n")
    _run(tmp_path)
    assert (tmp_path / "screen-docent.conf").exists()


def test_an_existing_migrated_file_is_never_overwritten(tmp_path):
    (tmp_path / "screen-docent.conf").write_text(CONFIGURED)
    (tmp_path / "screen-docent.conf.migrated").write_text("OLDER\n")
    (tmp_path / "pieria.conf").write_text(CONFIGURED)
    r = _run(tmp_path)
    assert (tmp_path / "screen-docent.conf.migrated").read_text() == "OLDER\n"
    assert (tmp_path / "screen-docent.conf").exists() and "already exists" in r.stderr
