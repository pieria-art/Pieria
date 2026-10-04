"""Every pieria.conf reader must agree with bash `.`-sourcing on inline comments and quoting (1.1).

The conf is shell. `KEY=value   # note` sources as `value`; sd-eink / sd-conf / eink_client used to
return `value   # note`. These tests run the REAL bash as the oracle for each form the conf uses.
"""
import importlib.machinery
import importlib.util
import pathlib
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
import eink_client  # noqa: E402

_BIN = _ROOT / "deploy" / "appliance" / "bin"


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


sc = _load("sd_conf_cmt", _BIN / "sd-conf")
se = _load("sd_eink_cmt", _BIN / "sd-eink")

# (line, what bash yields)
CASES = [
    ("K=plain", "plain"),
    ("K=value   # note", "value"),
    ("K=value # note", "value"),
    ("K=value\t# note", "value"),
    ("K=value#nospace", "value#nospace"),
    ("K=   # only a comment", ""),
    ("K=", ""),
    ("K=a/b:c.d  # path-ish", "a/b:c.d"),
    ('K="quoted"', "quoted"),
    ('K="has # inside"', "has # inside"),
    ('K="has # inside"   # and a comment', "has # inside"),
    ("K='single # inside'", "single # inside"),
    ("K='single # inside'  # trailing", "single # inside"),
    ('K="q" # c', "q"),
]


def _bash_source(text, var):
    r = subprocess.run(["bash", "-c", '. "$1"; printf %s "${!2}"', "x", "/dev/stdin", var],
                       input=text, capture_output=True, text=True, timeout=10)
    return r.stdout


@pytest.mark.parametrize("line,expected", CASES)
def test_bash_is_the_oracle(line, expected):
    assert _bash_source(line + "\n", "K") == expected  # guards the table itself


@pytest.mark.parametrize("line,expected", CASES)
def test_all_readers_match_bash(line, expected, tmp_path, monkeypatch):
    assert eink_client.parse_conf_text(line)["K"] == expected
    assert sc.get_key(line + "\n", "K") == expected
    conf = tmp_path / "c.conf"
    conf.write_text(line + "\n")
    monkeypatch.delenv("K", raising=False)
    se._load_conf(str(conf))
    assert se.os.environ["K"] == expected
    monkeypatch.delenv("K", raising=False)


def test_the_two_parser_copies_are_identical():
    for line, _ in CASES:
        rest = line.partition("=")[2]
        assert sc.split_conf_value(rest) == eink_client.split_conf_value(rest)


def test_export_view_strips_comments():
    text = 'WATCHDOG=enforce   # self-heal\nTIMEZONE="America/Chicago" # tz\n'
    assert sc.export_view(text) == {"WATCHDOG": "enforce", "TIMEZONE": "America/Chicago"}


def test_set_keys_preserves_trailing_comment():
    text = '# head\nWATCHDOG=observe   # self-heal mode\nROTATE="90"  # portrait\nOTHER=1\n'
    out = sc.set_keys(text, {"WATCHDOG": "enforce", "ROTATE": "270"})
    assert out == "# head\nWATCHDOG=enforce   # self-heal mode\nROTATE=270  # portrait\nOTHER=1\n"


def test_set_keys_preserves_comment_on_empty_value():
    out = sc.set_keys("ROTATE=   # landscape\nX=1\n", {"ROTATE": "90"})
    assert out == "ROTATE=90   # landscape\nX=1\n"


def test_set_keys_without_comment_unchanged_and_result_sources_correctly():
    assert sc.set_keys("WATCHDOG=observe\n", {"WATCHDOG": "off"}) == "WATCHDOG=off\n"
    out = sc.set_keys("WATCHDOG=observe # c\n", {"WATCHDOG": "off"})
    assert _bash_source(out, "WATCHDOG") == "off"
