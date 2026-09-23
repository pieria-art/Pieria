"""sd-mailbox — the ONE way any root-run appliance script touches data/appliance/ (or data/) (ADR-119).

Threat model: the app container (uid 1000) bind-mounts only ./data, so it cannot replace
$REPO_ROOT/data itself, but it CAN replace anything INSIDE it — including turning data/appliance into
a symlink to an arbitrary directory. Every subcommand here must refuse closed against that (and
against a planted file-level symlink at a given name) rather than follow it, and must never touch the
outside target.
"""

import os
import pathlib
import subprocess

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "appliance" / "bin" / "sd-mailbox"


def mbox(repo_root, *args, input_bytes: bytes | None = None, **kw):
    return subprocess.run([str(_BIN), "--repo-root", str(repo_root), *args],
                          input=input_bytes, capture_output=True, **kw)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    return root


# --- basic operations, real (non-symlinked) directory ---------------------------------------------

def test_write_creates_the_appliance_dir_if_missing(repo):
    r = mbox(repo, "write", "foo.txt", input_bytes=b"hello")
    assert r.returncode == 0, r.stderr
    assert (repo / "data" / "appliance" / "foo.txt").read_bytes() == b"hello"


def test_write_sets_mode(repo):
    mbox(repo, "write", "foo.txt", "--mode", "0640", input_bytes=b"x")
    mode = (repo / "data" / "appliance" / "foo.txt").stat().st_mode & 0o777
    assert mode == 0o640


@pytest.mark.skipif(os.geteuid() != 0, reason="owner change only observable as root")
def test_write_sets_owner_when_running_as_root(repo):
    mbox(repo, "write", "foo.txt", "--owner", "1000:1000", input_bytes=b"x")
    st = (repo / "data" / "appliance" / "foo.txt").stat()
    assert (st.st_uid, st.st_gid) == (1000, 1000)


def test_append_creates_then_appends(repo):
    mbox(repo, "append", "log.txt", input_bytes=b"one\n")
    mbox(repo, "append", "log.txt", input_bytes=b"two\n")
    assert (repo / "data" / "appliance" / "log.txt").read_text() == "one\ntwo\n"


def test_read_returns_the_content(repo):
    (repo / "data" / "appliance").mkdir()
    (repo / "data" / "appliance" / "foo.txt").write_bytes(b"hi there")
    r = mbox(repo, "read", "foo.txt")
    assert r.returncode == 0
    assert r.stdout == b"hi there"


def test_read_of_a_missing_file_fails_closed(repo):
    r = mbox(repo, "read", "nope.txt")
    assert r.returncode != 0


def test_rm_removes_the_file(repo):
    (repo / "data" / "appliance").mkdir()
    (repo / "data" / "appliance" / "foo.txt").write_bytes(b"x")
    r = mbox(repo, "rm", "foo.txt")
    assert r.returncode == 0
    assert not (repo / "data" / "appliance" / "foo.txt").exists()


def test_rm_of_a_missing_file_is_a_quiet_no_op(repo):
    r = mbox(repo, "rm", "nope.txt")
    assert r.returncode == 0


def test_copy_in_reads_a_root_private_source(repo, tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    r = mbox(repo, "copy-in", str(src), "dest.bin", "--mode", "0600")
    assert r.returncode == 0, r.stderr
    out = repo / "data" / "appliance" / "dest.bin"
    assert out.read_bytes() == b"payload"
    assert out.stat().st_mode & 0o777 == 0o600


def test_chown_dir_creates_the_dir_if_missing(repo):
    r = mbox(repo, "chown-dir")
    assert r.returncode == 0, r.stderr
    assert (repo / "data" / "appliance").is_dir()


def test_missing_appliance_dir_is_created_on_write(repo):
    assert not (repo / "data" / "appliance").exists()
    mbox(repo, "write", "foo.txt", input_bytes=b"x")
    assert (repo / "data" / "appliance").is_dir()
    assert not (repo / "data" / "appliance").is_symlink()


# --- directory-level symlink attack: data/appliance -> outside dir ---------------------------------

@pytest.fixture
def hijacked_repo(tmp_path):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "data" / "appliance").symlink_to(outside)
    return root, outside


@pytest.mark.parametrize("args,kw", [
    (("write", "pwn.txt"), {"input_bytes": b"x"}),
    (("append", "pwn.txt"), {"input_bytes": b"x"}),
    (("read", "pwn.txt"), {}),
    (("rm", "pwn.txt"), {}),
    (("chown-dir",), {}),
])
def test_every_subcommand_refuses_a_symlinked_appliance_dir(hijacked_repo, args, kw):
    root, outside = hijacked_repo
    r = mbox(root, *args, **kw)
    assert r.returncode != 0
    assert list(outside.iterdir()) == []          # nothing was ever written into the real target
    assert (root / "data" / "appliance").is_symlink()   # never replaced


def test_copy_in_refuses_a_symlinked_appliance_dir(hijacked_repo, tmp_path):
    root, outside = hijacked_repo
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    r = mbox(root, "copy-in", str(src), "pwn.bin")
    assert r.returncode != 0
    assert list(outside.iterdir()) == []
    assert (root / "data" / "appliance").is_symlink()


# --- file-level symlink attack: a planted symlink AT a given name ----------------------------------

def test_write_refuses_a_planted_file_symlink(repo, tmp_path):
    (repo / "data" / "appliance").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch")
    (repo / "data" / "appliance" / "victim.txt").symlink_to(outside)

    mbox(repo, "write", "victim.txt", input_bytes=b"pwned")
    # write's atomic rename REPLACES the directory entry (symlink or not) rather than writing through
    # it — the outside target is untouched either way, which is the property that matters here.
    assert outside.read_text() == "do not touch"


def test_append_refuses_a_planted_file_symlink(repo, tmp_path):
    (repo / "data" / "appliance").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch")
    (repo / "data" / "appliance" / "victim.txt").symlink_to(outside)

    r = mbox(repo, "append", "victim.txt", input_bytes=b"pwned")
    assert r.returncode != 0
    assert outside.read_text() == "do not touch"
    assert (repo / "data" / "appliance" / "victim.txt").is_symlink()


def test_read_refuses_a_planted_file_symlink(repo, tmp_path):
    (repo / "data" / "appliance").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (repo / "data" / "appliance" / "victim.txt").symlink_to(outside)

    r = mbox(repo, "read", "victim.txt")
    assert r.returncode != 0
    assert b"secret" not in r.stdout


# --- name validation --------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["../evil", "a/b", "", "..", "."])
def test_unsafe_names_are_refused(repo, name):
    r = mbox(repo, "write", name, input_bytes=b"x")
    assert r.returncode != 0


def test_dir_data_operates_on_data_root_not_appliance(repo):
    r = mbox(repo, "--dir", "data", "write", "artwork.db", "--mode", "0644", input_bytes=b"dbdata")
    assert r.returncode == 0, r.stderr
    assert (repo / "data" / "artwork.db").read_bytes() == b"dbdata"
    assert not (repo / "data" / "appliance" / "artwork.db").exists()


# --- stat -------------------------------------------------------------------------------------------

def test_stat_reports_a_regular_file(repo):
    mbox(repo, "write", "foo.txt", "--mode", "0640", input_bytes=b"hello")
    r = mbox(repo, "stat", "foo.txt")
    assert r.returncode == 0, r.stderr
    mtime, size, kind, mode, uid, gid = r.stdout.decode().split()
    assert size == "5"
    assert kind == "file"
    assert mode == "0640"


def test_stat_of_a_missing_name_fails_closed(repo):
    r = mbox(repo, "stat", "nope.txt")
    assert r.returncode != 0


def test_stat_reports_symlink_without_following_it(repo, tmp_path):
    (repo / "data" / "appliance").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (repo / "data" / "appliance" / "link.txt").symlink_to(outside)
    r = mbox(repo, "stat", "link.txt")
    assert r.returncode == 0, r.stderr
    assert r.stdout.decode().split()[2] == "symlink"


# --- FIFO / non-regular-file refusal (item 5) --------------------------------------------------------

def test_read_refuses_a_fifo_promptly(repo):
    (repo / "data" / "appliance").mkdir()
    fifo = repo / "data" / "appliance" / "status.json"
    os.mkfifo(fifo)
    r = mbox(repo, "read", "status.json", timeout=10)
    assert r.returncode != 0


def test_append_refuses_a_fifo(repo):
    (repo / "data" / "appliance").mkdir()
    fifo = repo / "data" / "appliance" / "log.txt"
    os.mkfifo(fifo)
    r = mbox(repo, "append", "log.txt", input_bytes=b"x", timeout=10)
    assert r.returncode != 0


# --- oversize refusal (item 5) ------------------------------------------------------------------------

def test_read_refuses_a_file_over_the_cap(repo):
    (repo / "data" / "appliance").mkdir()
    big = repo / "data" / "appliance" / "big.bin"
    big.write_bytes(b"x" * 2048)
    r = mbox(repo, "read", "big.bin", "--max", "1024")
    assert r.returncode != 0


def test_read_allows_a_file_under_the_cap(repo):
    (repo / "data" / "appliance").mkdir()
    small = repo / "data" / "appliance" / "small.bin"
    small.write_bytes(b"x" * 512)
    r = mbox(repo, "read", "small.bin", "--max", "1024")
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"x" * 512


def test_copy_in_refuses_an_oversize_source(repo, tmp_path):
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 2048)
    r = mbox(repo, "copy-in", str(src), "dest.bin", "--max", "1024")
    assert r.returncode != 0
    assert not (repo / "data" / "appliance" / "dest.bin").exists()
