"""Offline tests for the seed migration + the source verifier (no network).

Covers the highest-risk correctness detail (filename round-tripping / no double-encoding), the URL
collectors for all three scopes, and the pass/fail classification in check_url. HTTP is mocked with
a tiny fake client (same shape as tests/test_download.py); throttling/sleeps are bypassed.
"""
import asyncio
import io
import json

from PIL import Image

import federation
from models import SubscriptionModel
from tools import migrate_seed_urls as mig
from tools import verify_sources as vs


def _img_bytes(size=(2200, 1700), fmt="PNG"):
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format=fmt)
    return buf.getvalue()


# --------------------------------------------------------------- migration: filename round-trip

def test_extract_plain_filename():
    url = ("https://upload.wikimedia.org/wikipedia/commons/thumb/e/ea/"
           "Van_Gogh_-_Starry_Night.jpg/2000px-Van_Gogh_-_Starry_Night.jpg")
    assert mig._commons_filename_from_thumb(url) == "Van_Gogh_-_Starry_Night.jpg"


def test_extract_decodes_encoded_filename():
    # %2C (comma) must come back DECODED so the builder re-encodes exactly once.
    url = ("https://upload.wikimedia.org/wikipedia/commons/thumb/7/7d/"
           "A_Sunday%2C_1884.jpg/2000px-A_Sunday%2C_1884.jpg")
    assert mig._commons_filename_from_thumb(url) == "A_Sunday,_1884.jpg"


def test_extract_non_thumb_returns_none():
    assert mig._commons_filename_from_thumb("https://commons.wikimedia.org/wiki/Special:FilePath/x.jpg?width=600") is None
    assert mig._commons_filename_from_thumb("https://artic.edu/iiif/2/abc/full/max/0/default.jpg") is None
    assert mig._commons_filename_from_thumb("") is None


def test_rebuild_item_urls_single_encoding():
    item = {"source_url": ("https://upload.wikimedia.org/wikipedia/commons/thumb/7/7d/"
                           "A_Sunday%2C_1884.jpg/2000px-A_Sunday%2C_1884.jpg"),
            "thumbnail_url": ("https://upload.wikimedia.org/wikipedia/commons/thumb/7/7d/"
                              "A_Sunday%2C_1884.jpg/500px-A_Sunday%2C_1884.jpg")}
    source, thumb = mig.rebuild_item_urls(item)
    assert source == "https://commons.wikimedia.org/wiki/Special:FilePath/A_Sunday%2C_1884.jpg?width=3840"
    assert thumb == "https://commons.wikimedia.org/wiki/Special:FilePath/A_Sunday%2C_1884.jpg?width=600"
    assert "%25" not in source and "%25" not in thumb  # single-encoded


def test_rebuild_item_urls_unmatchable_returns_none():
    assert mig.rebuild_item_urls({"source_url": "https://x/y.jpg", "thumbnail_url": ""}) is None


# --------------------------------------------------------------- collectors (no network)

def test_collect_seed(tmp_path):
    f = tmp_path / "seed.json"
    f.write_text(json.dumps([{"title": "T", "source_url": "https://x/s.jpg", "thumbnail_url": "https://x/t.jpg"}]))
    checks = vs.collect_seed(f)
    assert [c.kind for c in checks] == ["source", "thumbnail"]
    assert all(c.origin == "seed" for c in checks)


def test_collect_catalog(tmp_path):
    (tmp_path / "index.json").write_text(json.dumps(
        {"collections": [{"id": "c1", "title": "C1", "cover_thumbnail": "https://x/cover.jpg"}]}))
    (tmp_path / "c1.json").write_text(json.dumps(
        {"items": [{"title": "A", "source_url": "https://x/s.jpg", "thumbnail_url": "https://x/t.jpg"}]}))
    checks = vs.collect_catalog(tmp_path)
    kinds = sorted(c.kind for c in checks)
    assert kinds == ["cover", "source", "thumbnail"]


def test_collect_subscriptions(testing_session, monkeypatch):
    monkeypatch.setattr(federation, "_assert_public_url", lambda url: None)  # skip real SSRF DNS checks
    valid = {
        "manifest_version": 2, "id": "janes", "title": "Jane's",
        "items": [{"id": "a1", "title": "Sunrise", "artist": "Monet",
                   "image": {"full_url": "https://cdn.jane.test/a.jpg",
                             "thumbnail_url": "https://cdn.jane.test/a-t.jpg", "license": "CC0-1.0"}}],
    }
    invalid = {"manifest_version": 2, "id": "x"}  # missing title + items
    testing_session.add(SubscriptionModel(url="https://jane.test/m.json", title="Jane's",
                                           enabled=True, cached_manifest=json.dumps(valid)))
    testing_session.add(SubscriptionModel(url="https://bad.test/m.json", title="Bad",
                                           enabled=True, cached_manifest=json.dumps(invalid)))
    testing_session.commit()

    checks = vs.collect_subscriptions(testing_session)
    good = [c for c in checks if c.collection == "Jane's"]
    bad = [c for c in checks if c.collection == "Bad"]
    assert sorted(c.kind for c in good) == ["source", "thumbnail"]
    assert len(bad) == 1 and bad[0].kind == "manifest" and bad[0].error  # invalid manifest -> recorded, not crashed


# --------------------------------------------------------------- check_url classification

class _Resp:
    def __init__(self, status_code=200, content_type="image/jpeg", content=b""):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.content = content


class _Client:
    def __init__(self, resp):
        self._resp = resp

    async def get(self, url, **kw):
        return self._resp


def _check(uc, resp):
    return asyncio.run(vs.check_url(_Client(resp), uc))


def _uc(kind="source", url="https://artic.edu/iiif/2/abc/full/max/0/default.jpg", error=None):
    return vs.UrlCheck("catalog", "c1", "A", kind, url, error=error)


def test_source_ok_when_large_image():
    r = _check(_uc("source"), _Resp(200, "image/jpeg", _img_bytes((2200, 1700))))
    assert r.ok, r.detail


def test_source_fails_400():
    assert not _check(_uc("source"), _Resp(400, "text/html", b"blocked")).ok


def test_source_fails_below_gate():
    r = _check(_uc("source"), _Resp(200, "image/jpeg", _img_bytes((1000, 800))))
    assert not r.ok and "gate" in r.detail


def test_thumbnail_does_not_gate_on_size():
    # same small image is fine as a thumbnail (no resolution gate)
    assert _check(_uc("thumbnail"), _Resp(200, "image/jpeg", _img_bytes((1000, 800)))).ok


def test_thumbnail_fails_html():
    assert not _check(_uc("thumbnail"), _Resp(200, "text/html", b"<html>")).ok


def test_thumbnail_fails_svg():
    assert not _check(_uc("thumbnail"), _Resp(200, "image/svg+xml", b"<svg/>")).ok


def test_wikimedia_source_uses_cheap_check():
    # Wikimedia source is pre-gated -> ranged content-type check only (no PIL decode of partial bytes)
    uc = _uc("source", url="https://commons.wikimedia.org/wiki/Special:FilePath/x.jpg?width=3840")
    assert _check(uc, _Resp(206, "image/jpeg", b"\xff\xd8partial")).ok


def test_pre_error_recorded_without_network():
    r = _check(_uc("manifest", url="", error="invalid manifest"), _Resp(500))
    assert not r.ok and r.detail == "invalid manifest"


# --------------------------------------------------------------- aggregation / exit code

def test_report_exit_code_and_text():
    ok = vs.CheckResult(_uc("source"), True, "2200x1700")
    bad = vs.CheckResult(_uc("thumbnail", url="https://artic.edu/x.jpg"), False, "HTTP 403")
    text, code = vs.report([ok, bad])
    assert code == 1 and "FAIL" in text and "HTTP 403" in text
    text2, code2 = vs.report([ok])
    assert code2 == 0 and "1 passed" in text2


def test_run_checks_dedupes_identical_urls(monkeypatch):
    # same URL referenced by two different catalog items (F7) -> one network call, two results
    async def _noop():
        return None

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(vs, "_wm_throttle", _noop)
    monkeypatch.setattr(vs.asyncio, "sleep", _nosleep)

    calls = []

    class _CountingClient:
        async def get(self, url, **kw):
            calls.append(url)
            return _Resp(200, "image/jpeg", _img_bytes())

    shared = "https://x/shared.jpg"
    checks = [vs.UrlCheck("catalog", "c1", "A", "source", shared),
             vs.UrlCheck("catalog", "c2", "B", "source", shared)]
    results = asyncio.run(vs.run_checks(checks, client=_CountingClient(), budget_seconds=None))
    assert len(calls) == 1                      # deduped: one network call for the shared URL
    assert len(results) == 2                     # but every catalog item still gets a result
    assert all(r.ok for r in results)
    assert {r.uc.collection for r in results} == {"c1", "c2"}


def test_run_checks_budget_exhausted_marks_unchecked(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(vs, "_wm_throttle", _noop)
    checks = [_uc("source", url="https://x/a.jpg"), _uc("thumbnail", url="https://x/b.jpg")]
    # budget=0 -> the very first wait() round times out before anything can finish
    results = asyncio.run(vs.run_checks(checks, client=_Client(_Resp(200)), budget_seconds=0.0))
    assert len(results) == 2
    assert all(r.unchecked and not r.ok for r in results)


def test_report_unchecked_not_counted_as_failure_or_transient():
    ok = vs.CheckResult(_uc("source"), True, "2200x1700")
    unchecked = vs.CheckResult(_uc("thumbnail", url="https://x/u.jpg"),
                               False, "budget exhausted before this URL was reached", unchecked=True)
    text, code = vs.report([ok, unchecked])
    assert code == 0
    assert "1 unchecked" in text
    assert "0 hard-fail" in text and "0 transient" in text
    assert "UNCHECKED" in text


def test_report_min_coverage_fails_run():
    ok = vs.CheckResult(_uc("source"), True, "ok")
    unchecked = vs.CheckResult(_uc("thumbnail", url="https://x/u.jpg"), False, "budget", unchecked=True)
    text, code = vs.report([ok, unchecked], min_coverage=0.9)   # coverage is 1/2 = 50%
    assert code == 1 and "min-coverage" in text
    text2, code2 = vs.report([ok, unchecked], min_coverage=0.4)
    assert code2 == 0


def test_run_checks_integration(monkeypatch):
    async def _noop():
        return None

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(vs, "_wm_throttle", _noop)
    monkeypatch.setattr(vs.asyncio, "sleep", _nosleep)
    checks = [_uc("source", url="https://artic.edu/a.jpg"), _uc("thumbnail", url="https://artic.edu/b.jpg")]
    results = asyncio.run(vs.run_checks(checks, client=_Client(_Resp(403, "text/html", b"blocked"))))
    assert len(results) == 2 and all(not r.ok for r in results)
