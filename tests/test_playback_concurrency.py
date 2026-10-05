"""Regression (ADR-148 race fix): /next-image + the e-ink pull run in the threadpool, so requests for one
display interleave inside a worker. Real SQLite FILE database, one session per thread (the shared
in-memory test session would hide every race)."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, func, text
from sqlalchemy.orm import sessionmaker

import core.playback as playback
from database import Base
from models import ActiveDisplayModel, ArtworkModel, PlaylistModel, SettingsModel

N = 16


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(bind=engine)
    f = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with f() as db:
        pl = PlaylistModel(name="Gallery")
        arts = [ArtworkModel(filename=f"a{i}.jpg", status="approved") for i in range(5)]
        db.add_all([pl, *arts]); db.commit()
        for a in arts:
            pl.artworks.append(a)
        db.commit()
    yield f
    engine.dispose()


def _run_parallel(fn, n=N):
    barrier = threading.Barrier(n)

    def _go(i):
        barrier.wait()
        return fn(i)
    with ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(_go, range(n)))   # re-raises the first failure


def test_parallel_next_image_fresh_displays_all_succeed(factory):
    def _pull(i):
        with factory() as db:
            return playback.select_next_image_sync("Gallery", None, f"nx{i}", 1, db)
    out = _run_parallel(_pull)
    assert len(out) == N and all(o["metadata"]["id"] for o in out)
    with factory() as db:
        assert db.query(SettingsModel).filter(SettingsModel.setting_key.like("last_playlist:%")).count() == N


def test_parallel_next_image_same_display_one_last_playlist_row(factory):
    def _pull(i):
        with factory() as db:
            return playback.select_next_image_sync("Gallery", False, "shared", 1, db)
    _run_parallel(_pull)
    with factory() as db:
        assert db.query(SettingsModel).filter(SettingsModel.setting_key == "last_playlist:shared").count() == 1
        # serialized per display: N sequential advances over 5 items, none lost to a read-modify-write race
        idx = db.execute(text(
            "select last_sequential_index from display_playback_sessions where display_id='shared'")).scalar()
        assert idx == (N - 1) % 5   # default cursor -1, N serialized advances over 5 items


def test_show_next_is_consumed_exactly_once(factory):
    with factory() as db:
        art_id = db.query(ArtworkModel).first().id
        playback.set_pending_show(db, "d1", art_id)

    def _take(i):
        with factory() as db:
            return playback._take_pending_show(db, "d1")
    got = [r for r in _run_parallel(_take) if r is not None]
    assert len(got) == 1


def test_parallel_touch_active_display_never_loses_the_touch(factory):
    def _touch(i):
        with factory() as db:
            playback.touch_active_display(db, "eink-1", refresh_s=60 + i)
    _run_parallel(_touch)
    with factory() as db:
        assert db.query(func.count(ActiveDisplayModel.display_id)).scalar() == 1
        assert db.query(ActiveDisplayModel).one().kind == "eink"
        assert db.query(SettingsModel).filter(
            SettingsModel.setting_key == f"{playback._REFRESH_S_PREFIX}eink-1").count() == 1
