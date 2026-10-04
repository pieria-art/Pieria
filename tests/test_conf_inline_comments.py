"""Every pieria.conf reader must agree with bash `.`-sourcing on inline comments and quoting (1.1).

The conf is shell. `KEY=value   # note` sources as `value`; sd-eink / sd-conf / eink_client used to
return `value   # note`. These tests run the REAL bash as the oracle for each form the conf uses.
"""
import importlib.machinery
import importlib.util
import inspect
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
    ('K="a"b', "ab"),
    ('K="90"#x', "90#x"),
    ("K='a'b # c", "ab"),
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


def _src(fn):
    return inspect.getsource(fn)


def test_the_parser_copies_are_identical_source():
    assert _src(sc.split_conf_value) == _src(eink_client.split_conf_value)


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


# --- the setup wizard's preserved lines ---------------------------------------------------------
wiz = _load("sd_setup_cmt", _ROOT / "deploy" / "appliance" / "setup" / "sd_setup.py")


def test_wizard_parser_copy_is_identical_source():
    assert _src(wiz.split_conf_value) == _src(eink_client.split_conf_value)


def test_wizard_preserves_commented_line_with_its_comment():
    out = wiz._preserved_lines("WATCHDOG=enforce  # self-heal\nEINK_ENABLED=1\n")
    assert out == ["WATCHDOG=enforce  # self-heal", "EINK_ENABLED=1"]


def test_wizard_still_rejects_unsafe_value_even_with_a_comment(capsys):
    assert wiz._preserved_lines("X=a;rm -rf /  # c\nY=$(id) # c\n") == []
    assert "dropping unsafe" in capsys.readouterr().err


# --- reviewer regressions: text glued to a closing quote must never survive a rewrite ---------------

def test_vertical_whitespace_does_not_split_like_bash():
    # \v, \f and NBSP are not word separators in bash; only space/tab are.
    for ws in ("\x0b", "\x0c", "\xa0"):
        line = f"K=a{ws}# c"
        assert eink_client.split_conf_value(line.partition("=")[2])[0] == f"a{ws}# c"


def test_set_keys_cleans_a_glued_hash_payload():
    out = sc.set_keys('ROTATE="90"#;echo PWNED\n', {"ROTATE": "180"})
    assert out == "ROTATE=180\n"
    assert _bash_source(out, "ROTATE") == "180"


def test_glued_payload_after_unquoted_value_is_not_a_comment_either():
    out = sc.set_keys("ROTATE=90#;echo PWNED\n", {"ROTATE": "180"})
    assert "PWNED" not in out


def test_set_keys_drops_non_comment_trailing_text():
    assert sc.set_keys('ROTATE="90" junk\n', {"ROTATE": "180"}) == "ROTATE=180\n"


def test_wizard_refuses_glued_hash_payload(capsys):
    assert wiz._preserved_lines('K="ok"#;echo PWNED\n') == []
    assert "dropping unsafe" in capsys.readouterr().err


def test_wizard_refuses_non_comment_trailing_text(capsys):
    assert wiz._preserved_lines('K="ok" ;echo PWNED\n') == []
    assert "dropping unsafe" in capsys.readouterr().err


def test_sd_conf_get_of_glued_value_fails_validation_not_silently_90():
    assert sc.get_key('ROTATE="90"#x\n', "ROTATE") == "90#x"
    assert sc.validate("ROTATE", "90#x") is not None


# --- line breaks: bash splits on \n only ----------------------------------------------------------

@pytest.mark.parametrize("ch", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
def test_set_keys_refuses_a_conf_with_exotic_line_breaks(ch):
    text = f"# note{ch}echo PWNED\nROTATE=90\n"
    with pytest.raises(ValueError):
        sc.set_keys(text, {"ROTATE": "180"})


def test_cli_set_fails_closed_and_leaves_the_file(tmp_path):
    conf = tmp_path / "c.conf"
    original = "# note\x0becho PWNED\nROTATE=90\n"
    conf.write_text(original)
    r = subprocess.run([sys.executable, str(_BIN / "sd-conf"), "--conf", str(conf), "set", "ROTATE=180"],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "refusing to rewrite" in r.stderr
    assert conf.read_text() == original
    assert "PWNED" not in _bash_source(conf.read_text(), "ROTATE") + "x"


def test_readers_treat_exotic_breaks_as_part_of_the_line_like_bash():
    text = "# note\x0cROTATE=999\nROTATE=90\n"
    assert _bash_source(text, "ROTATE") == "90"
    assert sc.get_key(text, "ROTATE") == "90"
    assert eink_client.parse_conf_text(text)["ROTATE"] == "90"
    assert wiz._preserved_lines(text.replace("ROTATE", "EINK_ENABLED")) == ["EINK_ENABLED=90"]


def test_set_keys_normal_file_roundtrip_unchanged_shape():
    assert sc.set_keys("A=1\n\nB=2\n", {"B": "3"}) == "A=1\n\nB=3\n"
    assert sc.set_keys("", {"B": "3"}) == "B=3\n"
