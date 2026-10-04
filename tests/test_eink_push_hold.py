"""eink_push holds the watchdog off (the /run/pieria-eink-hold flag) for the duration of a push.

Flag path is overridden with SD_EINK_HOLD_FLAG — the same override sd-watchdog honours.
"""
import sys
import types

import pytest
from PIL import Image

from tools.eink import eink_push


@pytest.fixture
def flag(tmp_path, monkeypatch):
    p = tmp_path / "hold"
    monkeypatch.setenv("SD_EINK_HOLD_FLAG", str(p))
    return p


def _fake_inky(monkeypatch, on_show=None):
    class Panel:
        resolution = (8, 6)

        def set_image(self, img):
            pass

        def show(self):
            if on_show:
                on_show()

    auto_mod = types.ModuleType("inky.auto")
    auto_mod.auto = lambda: Panel()
    monkeypatch.setitem(sys.modules, "inky", types.ModuleType("inky"))
    monkeypatch.setitem(sys.modules, "inky.auto", auto_mod)


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "x.png"
    Image.new("RGB", (8, 6)).save(p)
    return p


def test_flag_exists_during_push_and_is_gone_after(flag, png, monkeypatch):
    seen = []
    _fake_inky(monkeypatch, on_show=lambda: seen.append(flag.exists()))
    eink_push.push(png)
    assert seen == [True]
    assert not flag.exists()


def test_flag_removed_when_the_push_errors(flag, png, monkeypatch):
    def boom():
        raise RuntimeError("spi fault")

    _fake_inky(monkeypatch, on_show=boom)
    with pytest.raises(RuntimeError):
        eink_push.push(png)
    assert not flag.exists()


def test_flag_removed_on_keyboard_interrupt(flag, png, monkeypatch):
    def ctrl_c():
        raise KeyboardInterrupt

    _fake_inky(monkeypatch, on_show=ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        eink_push.push(png)
    assert not flag.exists()


def test_flag_removed_when_inky_is_missing(flag, png, monkeypatch):
    monkeypatch.setitem(sys.modules, "inky", None)   # import -> ImportError
    monkeypatch.setitem(sys.modules, "inky.auto", None)
    with pytest.raises(SystemExit):
        eink_push.push(png)
    assert not flag.exists()


def test_someone_elses_hold_is_never_cleared(flag, png, monkeypatch):
    flag.write_text("bench session")
    seen = []
    _fake_inky(monkeypatch, on_show=lambda: seen.append(flag.exists()))
    eink_push.push(png)
    assert seen == [True]
    assert flag.read_text() == "bench session"   # untouched, content and existence


def test_someone_elses_hold_survives_an_error_too(flag, png, monkeypatch):
    flag.write_text("bench session")

    def boom():
        raise RuntimeError("x")

    _fake_inky(monkeypatch, on_show=boom)
    with pytest.raises(RuntimeError):
        eink_push.push(png)
    assert flag.exists()


def test_unwritable_flag_dir_warns_but_still_pushes(tmp_path, png, monkeypatch, capsys):
    monkeypatch.setenv("SD_EINK_HOLD_FLAG", str(tmp_path / "no-such-dir" / "hold"))
    pushed = []
    _fake_inky(monkeypatch, on_show=lambda: pushed.append(1))
    eink_push.push(png)
    assert pushed == [1]
    assert "could not create" in capsys.readouterr().err
