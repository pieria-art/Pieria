"""sd-watchdog-advance — the watchdog's page-liveness + picture-advance probes (2026-09-20).

Background: the prod Pi held one frame for a week while every existing probe (server up, cage+chromium
up, artwork non-null, WebSocket open) passed. decide() is the pure rule set the shell watchdog calls.
"""

import importlib.machinery
import importlib.util
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "appliance" / "bin" / "sd-watchdog-advance"
_loader = importlib.machinery.SourceFileLoader("sd_watchdog_advance", str(_PATH))
_spec = importlib.util.spec_from_loader("sd_watchdog_advance", _loader)
wa = importlib.util.module_from_spec(_spec)
_loader.exec_module(wa)

T0 = 1_000_000.0
PLAYLISTS = [{"name": "Summer", "display_time": 1200, "artworks": [{"id": i} for i in range(68)]},
             {"name": "Solo", "display_time": 30, "artworks": [{"id": 1}]}]


def _disp(art_id=78, live=True, playlist="Summer", **extra):
    row = {"display_id": "living_room", "playlist": playlist, "live": live,
           "artwork": {"id": art_id} if art_id is not None else None}
    row.update(extra)
    return [row]


def test_unregistered_display_is_not_this_probes_problem():
    live, adv, state, _ = wa.decide([], PLAYLISTS, "living_room", None, T0)
    assert (live, adv, state) == (1, 1, None)


def test_a_silent_page_is_not_live():
    live, adv, _, _ = wa.decide(_disp(live=False), PLAYLISTS, "living_room", None, T0)
    assert live == 0 and adv == 1          # first sighting of this artwork: not stale yet


def test_pre_heartbeat_server_without_live_key_is_given_the_benefit_of_the_doubt():
    rows = _disp(); del rows[0]["live"]
    live, _, _, _ = wa.decide(rows, PLAYLISTS, "living_room", None, T0)
    assert live == 1


def test_first_sighting_starts_the_clock():
    _, adv, state, _ = wa.decide(_disp(78), PLAYLISTS, "living_room", None, T0)
    assert adv == 1 and state == {"artwork_id": 78, "since": T0}


def test_a_changed_artwork_resets_the_clock():
    st = {"artwork_id": 78, "since": T0 - 99_999}
    _, adv, state, _ = wa.decide(_disp(65), PLAYLISTS, "living_room", st, T0)
    assert adv == 1 and state == {"artwork_id": 65, "since": T0}


def test_same_artwork_within_three_display_times_is_fine():
    st = {"artwork_id": 78, "since": T0}
    _, adv, state, _ = wa.decide(_disp(78), PLAYLISTS, "living_room", st, T0 + 3 * 1200 + 119)
    assert adv == 1 and state["since"] == T0            # clock keeps its ORIGINAL start


def test_same_artwork_past_the_limit_is_stale():
    # The real incident: id 78 sat from 2026-09-13 to 09-20 on a 20-minute playlist.
    st = {"artwork_id": 78, "since": T0}
    _, adv, _, reason = wa.decide(_disp(78), PLAYLISTS, "living_room", st, T0 + 3 * 1200 + 121)
    assert adv == 0 and "78" in reason


def test_single_work_playlist_can_never_be_stale():
    st = {"artwork_id": 1, "since": T0}
    _, adv, _, _ = wa.decide(_disp(1, playlist="Solo"), PLAYLISTS, "living_room", st, T0 + 7 * 86400)
    assert adv == 1


def test_unknown_playlist_falls_back_to_the_slowest_known_cadence():
    st = {"artwork_id": 5, "since": T0}
    _, adv, _, _ = wa.decide(_disp(5, playlist="Renamed"), PLAYLISTS, "living_room", st, T0 + 3 * 1200 + 100)
    assert adv == 1
    _, adv, _, _ = wa.decide(_disp(5, playlist="Renamed"), PLAYLISTS, "living_room", st, T0 + 3 * 1200 + 200)
    assert adv == 0


def test_no_artwork_yet_is_the_paint_probes_case():
    _, adv, state, _ = wa.decide(_disp(None), PLAYLISTS, "living_room", None, T0)
    assert adv == 1 and state is None


@pytest.mark.parametrize("bad", [None, [], [{"display_id": "x"}], "garbage"])
def test_malformed_displays_never_trip(bad):
    displays = bad if isinstance(bad, list) else []
    live, adv, _, _ = wa.decide(displays, PLAYLISTS, "living_room", None, T0)
    assert (live, adv) == (1, 1)
