"""`sd-image-prep --full` empties data/appliance/ itself, through sd-mailbox (ADR-131).

Same harness as test_sd_image_prep.py: source the real script, override repo_root, call the function.
The real sd-mailbox runs (it needs no root); only the repo root is faked.
"""
import os
import pathlib
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BIN = _ROOT / "deploy" / "appliance" / "bin" / "sd-image-prep"
_MAILBOX = _ROOT / "deploy" / "appliance" / "bin" / "sd-mailbox"


def _run_clear(fake_root, mailbox=None):
    script = 'source "$BIN"; repo_root() { printf "%s" "$FAKE_ROOT"; }; clear_appliance_status'
    env = {"BIN": str(_BIN), "FAKE_ROOT": str(fake_root), "PATH": "/usr/bin:/bin",
           "SD_MAILBOX_BIN": str(mailbox or _MAILBOX)}
    return subprocess.run(["bash", "-c", script, str(_BIN)],
                          capture_output=True, text=True, env=env, timeout=30)


def _mailbox(*args):
    return subprocess.run([sys.executable, str(_MAILBOX), *args], capture_output=True, text=True, timeout=30)


def test_full_clears_status_files_and_leaves_the_rest_of_data(tmp_path):
    app = tmp_path / "data" / "appliance"
    app.mkdir(parents=True)
    for n in ("conf.json", "watchdog.json", "metrics.json", ".hidden.tmp"):
        (app / n).write_text("x")
    (tmp_path / "data" / "keep.txt").write_text("keep")

    r = _run_clear(tmp_path)

    assert r.returncode == 0, r.stderr
    assert list(app.iterdir()) == []
    assert app.is_dir()
    assert (tmp_path / "data" / "keep.txt").exists()   # only data/appliance, never data/
    assert "cleared data/appliance/ (4 file(s))" in r.stdout


def test_clear_unlinks_symlinks_without_following_and_leaves_subdirs(tmp_path):
    app = tmp_path / "data" / "appliance"
    app.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (app / "planted").symlink_to(victim)
    (app / "subdir").mkdir()
    (app / "subdir" / "inner").write_text("x")

    r = _mailbox("--repo-root", str(tmp_path), "clear")

    assert r.returncode == 0, r.stderr
    assert victim.read_text() == "precious"
    assert not os.path.lexists(app / "planted")
    assert (app / "subdir" / "inner").exists()
    assert "leaving directory" in r.stderr


def test_clear_refuses_a_symlinked_appliance_dir(tmp_path):
    (tmp_path / "data").mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "f").write_text("x")
    (tmp_path / "data" / "appliance").symlink_to(target)

    r = _mailbox("--repo-root", str(tmp_path), "clear")

    assert r.returncode != 0
    assert (target / "f").exists()


def test_full_aborts_if_mailbox_fails(tmp_path):
    (tmp_path / "data").mkdir()
    bad = tmp_path / "bad-mailbox"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)

    r = _run_clear(tmp_path, mailbox=bad)

    assert r.returncode == 1
    assert "refusing to capture" in r.stderr


def _run_with_systemctl_log(body, tmp_path, **env_extra):
    log = tmp_path / "sysctl.log"
    script = ('source "$BIN"; repo_root() { printf "%s" "$FAKE_ROOT"; }; '
              'systemctl() { echo "$*" >> "$LOG"; }; ' + body)
    env = {"BIN": str(_BIN), "FAKE_ROOT": str(tmp_path), "LOG": str(log), "PATH": "/usr/bin:/bin",
           "SD_MAILBOX_BIN": str(_MAILBOX), **env_extra}
    r = subprocess.run(["bash", "-c", script, str(_BIN)], capture_output=True, text=True, env=env, timeout=30)
    return r, (log.read_text() if log.exists() else "")


def test_declare_capabilities_keeps_trailing_comment(tmp_path):
    conf = tmp_path / "pieria.conf"
    conf.write_text("A=1\nEINK_ENABLED=0   # panel installed?\nB=2\n")
    r, _ = _run_with_systemctl_log('BOOT_CONF="$CONF"; declare_capabilities', tmp_path, CONF=str(conf))
    assert r.returncode == 0, r.stderr
    # fake systemctl returns 0 for is-enabled -> want=1
    assert conf.read_text() == "A=1\nEINK_ENABLED=1   # panel installed?\nB=2\n"


def test_declare_capabilities_plain_empty_and_quoted_values(tmp_path):
    for before, after in (("EINK_ENABLED=0\n", "EINK_ENABLED=1\n"),
                          ("EINK_ENABLED=  # c\n", "EINK_ENABLED=1  # c\n"),
                          ('EINK_ENABLED="0" # c\n', "EINK_ENABLED=1 # c\n")):
        conf = tmp_path / "c.conf"
        conf.write_text(before)
        r, _ = _run_with_systemctl_log('BOOT_CONF="$CONF"; declare_capabilities', tmp_path, CONF=str(conf))
        assert r.returncode == 0, r.stderr
        assert conf.read_text() == after
