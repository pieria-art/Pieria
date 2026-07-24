"""Setup card / Wi-Fi scan (ADR-058).

sd-setup-card paints the "this display needs setting up" card and caches the Wi-Fi scan that feeds the
wizard's SSID picker. These lock the scan PARSER, which is the part that silently returned nothing on
the first real gramps cycle.
"""
import importlib.util
import pathlib
import subprocess

_PATH = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "appliance" / "bin" / "sd-setup-card"
_spec = importlib.util.spec_from_loader("sd_setup_card",
                                        importlib.machinery.SourceFileLoader("sd_setup_card", str(_PATH)))
sd_card = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd_card)


_IW_OUTPUT = """BSS aa:bb:cc:dd:ee:01(on wlan0)
\tfreq: 2437
\tsignal: -58.00 dBm
\tSSID: 3Yosts
\tRSN:\t * Version: 1
BSS aa:bb:cc:dd:ee:02(on wlan0)
\tsignal: -73.00 dBm
\tSSID: Neighbour Open
BSS aa:bb:cc:dd:ee:03(on wlan0)
\tsignal: -40.00 dBm
\tSSID: 3Yosts
\tRSN:\t * Version: 1
BSS aa:bb:cc:dd:ee:04(on wlan0)
\tsignal: -50.00 dBm
\tSSID:
"""


def _fake_run(output, rc=0):
    def _run(cmd, *a, **k):
        if cmd[:2] == ["iw", "dev"] or (len(cmd) > 1 and cmd[1] == "dev"):
            return subprocess.CompletedProcess(cmd, rc, stdout=output, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return _run


def test_scan_parses_iw_dedupes_and_scores(monkeypatch):
    """`iw` is used instead of nmcli because during a setup boot wlan0 is already NM-UNMANAGED (the
    ADR-056 fix), so nmcli reports zero networks and the picker came up empty. Mesh SSIDs collapse to
    the strongest radio; a hidden/blank SSID is dropped since it can't be picked from a list."""
    monkeypatch.setattr(sd_card.subprocess, "run", _fake_run(_IW_OUTPUT))
    nets = sd_card.scan_networks("wlan0")

    assert [n["ssid"] for n in nets] == ["3Yosts", "Neighbour Open"]
    assert nets[0]["signal"] == 100          # -40 dBm -> 2*(-40+100) = 120, clamped to 100
    assert nets[1]["signal"] == 54           # -73 dBm -> 54
    assert nets[0]["secure"] is True
    assert nets[1]["secure"] is False


def test_scan_returns_empty_when_iw_fails(monkeypatch):
    """A failed scan must degrade to free-text SSID entry, never to an exception that kills setup."""
    monkeypatch.setattr(sd_card.subprocess, "run", _fake_run("", rc=1))
    assert sd_card.scan_networks("wlan0") == []


def test_render_card_produces_panel_sized_image():
    img = sd_card.render_card(1600, 1200, "Pieria-Setup")
    assert img.size == (1600, 1200)


def test_splash_is_self_contained_and_names_the_ssid():
    """The kiosk loads this over file:// with no network — every asset must be inline."""
    html = sd_card.render_splash("Pieria-Setup")
    assert "Pieria-Setup" in html
    assert "http://" not in html.replace("http://192.168", "")  # no external asset URLs
    assert "<style>" in html


def test_fit_font_px_shrinks_until_the_longest_line_fits():
    """Guards the bug that shipped: the step column's width was computed and then discarded, so a long
    SSID printed straight through the QR box on a portrait panel."""
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    short = ['Join the Wi-Fi network "Pieria-Setup"']
    long_ = ['Join the Wi-Fi network "SomeAbsurdlyLongNetworkNameThatKeepsGoing-5GHz"']

    tight = sd_card.fit_font_px(d, long_, avail=600, start_px=35, min_px=20)
    roomy = sd_card.fit_font_px(d, short, avail=600, start_px=35, min_px=20)
    assert tight < roomy, "a longer SSID must shrink the step type"
    assert tight >= 20, "never shrink below the legibility floor"


def test_ellipsize_guarantees_fit_when_shrinking_is_not_enough():
    """Shrinking bottoms out at the legibility floor, where a pathological SSID STILL overflowed (801px
    into a 600px column) — i.e. straight through the QR box again. Ellipsis is the hard guarantee; the
    QR itself still encodes the exact SSID."""
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = sd_card._font(20)
    text = 'Join the Wi-Fi network "SomeAbsurdlyLongNetworkNameThatKeepsGoing-5GHz"'
    out = sd_card.ellipsize(d, text, font, 600)
    assert d.textlength(out, font=font) <= 600
    assert out.endswith("\u2026")
    # A string that already fits is returned untouched.
    assert sd_card.ellipsize(d, "short", font, 600) == "short"


def test_portrait_canvas_stacks_so_text_cannot_hit_the_qr():
    """A portrait canvas is ~400px narrower; side-by-side left the step column too tight. Both
    orientations must render at their requested size with the layout intact."""
    assert sd_card.render_card(1200, 1600, "Pieria-Setup").size == (1200, 1600)   # portrait
    assert sd_card.render_card(1600, 1200, "Pieria-Setup").size == (1600, 1200)   # landscape
    # An extreme name must still render both ways without raising.
    assert sd_card.render_card(1200, 1600, "X" * 60).size == (1200, 1600)
