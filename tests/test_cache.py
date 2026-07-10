"""Cache correctness (Phase 2): the thumbnail lru is mtime-keyed (A4) and the catalog JSON memo hands
back isolated copies (A2), so an in-place file replace is never served stale and a caller mutating a
returned catalog dict can't corrupt the shared cache."""

import json
import os

from PIL import Image

import app as app_module
import core.media as core_media


def test_optimized_image_busts_on_mtime_change(tmp_path):
    p = tmp_path / "x.jpg"
    Image.new("RGB", (800, 600), (10, 20, 30)).save(p, "JPEG")
    os.utime(p, (1000, 1000))
    before = core_media.get_optimized_image(p, (400, 400), 70)

    # Replace the file in place with a clearly different image + a newer mtime.
    Image.new("RGB", (800, 600), (200, 100, 50)).save(p, "JPEG")
    os.utime(p, (2000, 2000))
    after = core_media.get_optimized_image(p, (400, 400), 70)

    assert before != after   # mtime participates in the lru key → no stale thumbnail (A4)


def test_read_local_json_isolates_callers(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"collections": [{"id": "a"}]}))
    first = app_module._read_local_json(p)
    first["collections"].append({"id": "INJECTED"})   # mutate the returned object

    second = app_module._read_local_json(p)            # served from the mtime cache, but deep-copied
    assert [c["id"] for c in second["collections"]] == ["a"]   # caller mutation didn't leak (A2)
