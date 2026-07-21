"""Unit tests for tools.derive_aspect_crops — the aspect-crop emit/bake maintainer tool."""
import hashlib
import json

from PIL import Image

from tools import derive_aspect_crops as dac

# A well-formed set of four boxes whose own aspect ratio exactly matches the key it is filed under.
FULL_SET = {
    "16:9": [0.0, 0.1, 1.0, 0.6625],
    "9:16": [0.2, 0.0, 0.7625, 1.0],
    "4:3": [0.0, 0.05, 1.0, 0.8],
    "3:4": [0.1, 0.0, 0.85, 1.0],
}


def _master(lib, source_url, w, h):
    """Write a master JPEG named with build_pack's stable `__<hash8>.jpg` suffix."""
    name = f"col__some-title__{hashlib.sha1(source_url.encode()).hexdigest()[:8]}.jpg"
    Image.new("RGB", (w, h), "red").save(lib / name, "JPEG")


def _catalog(base, cid, items):
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{cid}.json").write_text(json.dumps({"id": cid, "items": items}))


# --------------------------------------------------------------------------- _validate_item

def test_validate_item_accepts_well_formed_set():
    # FULL_SET's boxes are computed assuming a square (1.0-aspect) source.
    out, reason = dac._validate_item(FULL_SET, 1.0)
    assert reason is None
    assert set(out.keys()) == {"16:9", "9:16", "4:3", "3:4"}


def test_mismatched_box_is_corrected_by_snap_with_warning():
    """A box that's badly wrong for its key is no longer outright REJECTED (the old
    'aspect_mismatch' behavior) — bake now SNAPS it to the exact target aspect, preserving the box's
    centre (compositional intent). The correction shows up as a `snap_moved_far` warning instead."""
    bad = dict(FULL_SET)
    bad["9:16"] = FULL_SET["16:9"]  # a 16:9-shaped box filed under the 9:16 key
    diag: dict = {}
    out, reason = dac._validate_item(bad, 1.0, diag=diag)
    assert reason is None
    x0, y0, x1, y1 = out["9:16"]
    real_aspect = 1.0 * (x1 - x0) / (y1 - y0)
    assert abs(real_aspect - 9 / 16) / (9 / 16) < dac.ASPECT_INEXACT_TOL
    assert "snap_moved_far" in diag["9:16"]["warnings"]


def test_near_full_frame_stores_explicit_box():
    out, reason = dac._validate_item({"16:9": [0.0, 0.0, 1.0, 1.0]}, 1.0)
    assert reason is None
    assert out == {"16:9": [0.0, 0.0, 1.0, 1.0]}


def test_malformed_box_is_rejected_not_full_frame():
    out, reason = dac._validate_item({"4:3": [0.5, 0.5, 0.3, 0.9]}, 1.0)  # x0 > x1
    assert out is None
    assert reason == "malformed"


def test_validate_item_accounts_for_source_aspect_anisotropy():
    """Normalized [0,1] box coords are ANISOTROPIC: a box's real on-screen ratio is
    source_aspect * raw_ratio, not the raw ratio itself. `snap()` must fold `source_aspect` into its
    target-ratio solve the same way `_gate_box`'s aspect check does (real_aspect = source_aspect *
    bw/bh) — if snap instead solved for the RAW ratio, the corrected box would fail the
    aspect_inexact gate below, so this doubles as a regression guard on that wiring.

    On a non-square source (300x400, aspect 0.75): a box whose raw ratio is pre-compensated (so real
    ratio == 16/9 already) should barely move; a box with raw ratio exactly 16/9 (real ratio actually
    4:3, wrong once source aspect applies) must still be corrected to real ratio == 16/9, not left at
    its (wrong) raw ratio."""
    non_square_aspect = 300 / 400  # 0.75

    correct_16_9_box = [0.0, 0.2, 1.0, 0.621875]  # raw ratio 64/27; * 0.75 == 16/9 exactly
    out, reason = dac._validate_item({"16:9": correct_16_9_box}, non_square_aspect)
    assert reason is None
    assert out["16:9"] == [0.0, 0.2, 1.0, 0.6219]  # already exact -> snap barely moves it

    naive_box = [0.0, 0.1, 1.0, 0.6625]  # raw ratio exactly 16/9 — wrong once source aspect applies
    out2, reason2 = dac._validate_item({"16:9": naive_box}, non_square_aspect)
    assert reason2 is None
    x0, y0, x1, y1 = out2["16:9"]
    real_aspect = non_square_aspect * (x1 - x0) / (y1 - y0)
    assert abs(real_aspect - 16 / 9) / (16 / 9) < dac.ASPECT_INEXACT_TOL


# --------------------------------------------------------------------------- snap

def test_snap_produces_exact_real_aspect_across_source_aspects():
    """`snap` must hit the target real aspect exactly (source_aspect * bw/bh == target) regardless of
    how extreme the source shape is, and must always stay in-bounds. Includes an extreme 11.9:1
    handscroll and a 0.26 pillar print (near-degenerate sources) alongside ordinary ones."""
    cases = [1.0, 0.75, 1.3333, 11.9, 0.26, 2.5]
    targets = [16 / 9, 9 / 16, 4 / 3, 3 / 4]
    start_box = [0.15, 0.35, 0.55, 0.7]  # arbitrary, deliberately off-aspect for every target below
    for source_aspect in cases:
        for target in targets:
            x0, y0, x1, y1 = dac.snap(start_box, source_aspect, target)
            assert 0.0 <= x0 < x1 <= 1.0
            assert 0.0 <= y0 < y1 <= 1.0
            real_aspect = source_aspect * (x1 - x0) / (y1 - y0)
            # Same tolerance the downstream aspect_inexact gate uses — 4dp rounding on a small box can
            # cost a bit of precision, but never enough to trip the gate that's meant to catch this.
            assert abs(real_aspect - target) / target < dac.ASPECT_INEXACT_TOL


def test_snap_handles_degenerate_and_out_of_range_input():
    """Inverted coords and coords outside [0,1] must still clamp+order into an in-bounds box rather
    than raise or return garbage — `snap` is expected to run over ~11,428 agent-authored boxes
    unattended. (In production `snap` only ever sees a box `normalize_crop_box` already accepted, so
    this is a belt-and-suspenders check on the ported function itself, not the pipeline's own input.)
    A box collapsed to a single point has no defined size, so only bounds — not non-zero area — are
    guaranteed for that extreme."""
    for box in ([1.4, -0.3, -0.2, 0.9], [2.0, 2.0, 2.0, 2.0], [-1.0, -1.0, 0.0, 0.0]):
        x0, y0, x1, y1 = dac.snap(box, 0.75, 16 / 9)
        assert 0.0 <= x0 <= x1 <= 1.0
        assert 0.0 <= y0 <= y1 <= 1.0


def test_snap_is_idempotent():
    """Re-snapping an already-exact box should barely move it — the correction is meant to converge,
    not oscillate, so a box a human already hand-verified doesn't visibly drift on a re-bake."""
    for source_aspect, target in ((1.0, 16 / 9), (0.75, 4 / 3), (11.9, 9 / 16)):
        box = [0.1, 0.2, 0.6, 0.75]
        once = dac.snap(box, source_aspect, target)
        twice = dac.snap(once, source_aspect, target)
        assert max(abs(a - b) for a, b in zip(once, twice)) < 1e-3


# --------------------------------------------------------------------------- not_maximal gate

def test_not_maximal_rejects_a_small_centred_box():
    # Correct 16:9 aspect (source_aspect=1.0) but only 0.2 wide -> throws away most of the frame.
    # A gate rejection drops only the offending KEY (see `_validate_item`'s two-tier atomicity); with
    # only one key supplied here, dropping it leaves nothing usable -> item-level "empty", and the
    # actual gate verdict shows up per-key in `diag`.
    small = {"16:9": [0.4, 0.44375, 0.6, 0.55625]}
    diag: dict = {}
    out, reason = dac._validate_item(small, 1.0, diag=diag)
    assert out is None
    assert reason == "empty"
    assert diag["16:9"]["reject"] == "not_maximal"


def test_maximal_box_passes():
    out, reason = dac._validate_item({"16:9": FULL_SET["16:9"]}, 1.0)
    assert reason is None


def test_near_maximal_box_passes_with_warning():
    # max(w,h) == 0.95 -> inside the [0.90, 0.98) warn band, not a rejection.
    box = [0.025, 0.23281, 0.975, 0.76719]  # 16:9 real aspect at source_aspect=1.0, w=0.95
    diag: dict = {}
    out, reason = dac._validate_item({"16:9": box}, 1.0, diag=diag)
    assert reason is None
    assert "near_maximal" in diag["16:9"]["warnings"]


# --------------------------------------------------------------------------- focal_outside gate

def test_focal_outside_rejects_excluded_focal():
    # As with not_maximal, a gate rejection drops only the KEY -- with a single key supplied that
    # leaves the item "empty"; the actual verdict is in `diag`.
    diag: dict = {}
    out, reason = dac._validate_item({"16:9": FULL_SET["16:9"]}, 1.0, focal_point=(0.5, 0.05), diag=diag)
    assert out is None
    assert reason == "empty"
    assert diag["16:9"]["reject"] == "focal_outside"


def test_focal_outside_rejects_focal_near_edge():
    # box y-range is [0.1, 0.6625] (height 0.5625); put the focal 2% in from the top edge.
    fy = 0.1 + 0.02 * 0.5625
    diag: dict = {}
    out, reason = dac._validate_item({"16:9": FULL_SET["16:9"]}, 1.0, focal_point=(0.5, fy), diag=diag)
    assert out is None
    assert reason == "empty"
    assert diag["16:9"]["reject"] == "focal_outside"


def test_focal_comfortably_inside_passes():
    out, reason = dac._validate_item({"16:9": FULL_SET["16:9"]}, 1.0, focal_point=(0.5, 0.4))
    assert reason is None


def test_focal_check_skipped_when_item_has_no_focal_point():
    out, reason = dac._validate_item({"16:9": FULL_SET["16:9"]}, 1.0, focal_point=None)
    assert reason is None


def test_focal_axis_pinned_at_0_5_sentinel_is_skipped():
    """focal_x/y == 0.5 is a documented SENTINEL for "no single clear subject" (agents.py's
    FOCAL_POINT_INSTRUCTION), not a measurement — an axis pinned there carries no compositional
    signal and must not gate a legitimately off-centre crop on that axis. Measured on a random
    20-work sample: every focal_outside violation the un-fixed (whole-point) gate flagged was on an
    axis whose focal was exactly 0.5, biased toward landscapes composed toward one side."""
    # box y-range is [0.1, 0.6625]; focal_y sits right at the (would-be) violating edge, but
    # focal_y == 0.5 means the Y axis carries no signal and must be skipped entirely.
    out, reason = dac._validate_item({"16:9": FULL_SET["16:9"]}, 1.0, focal_point=(0.15, 0.5))
    assert reason is None

    # Meaningful (non-0.5) axis is still checked: "3:4" box has x-range [0.1, 0.85] -> a focal_x of
    # 0.02 sits well outside it, and must still be rejected.
    box = FULL_SET["3:4"]  # [0.1, 0.0, 0.85, 1.0]
    diag2: dict = {}
    out2, reason2 = dac._validate_item({"3:4": box}, 1.0, focal_point=(0.02, 0.5), diag=diag2)
    assert out2 is None
    assert reason2 == "empty"
    assert diag2["3:4"]["reject"] == "focal_outside"


def test_focal_both_axes_0_5_skips_both():
    out, reason = dac._validate_item({"16:9": FULL_SET["16:9"]}, 1.0, focal_point=(0.5, 0.5))
    assert reason is None


# --------------------------------------------------------------------------- bake atomicity

def test_bake_atomicity_one_bad_box_voids_the_whole_item(tmp_path):
    pack = tmp_path / "art-pack"
    lib = pack / "_Library"
    lib.mkdir(parents=True)
    _master(lib, "https://x/a.jpg", 1000, 1000)  # square, matches FULL_SET's assumption
    cat = tmp_path / "static" / "catalog"
    _catalog(cat, "col", [{"title": "A", "source_url": "https://x/a.jpg"}])
    bad = dict(FULL_SET)
    bad["4:3"] = [0.5, 0.5, 0.3, 0.9]  # malformed
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps([{"source_url": "https://x/a.jpg", "aspect_crops": bad}]))

    dac.bake(results_path, [str(cat)], pack, write=True)

    doc = json.loads((cat / "col.json").read_text())
    # NONE of the four boxes were applied — not even the three good ones.
    assert "aspect_crops" not in doc["items"][0]


def test_bake_no_master_is_rejected_distinctly(tmp_path):
    pack = tmp_path / "art-pack"
    (pack / "_Library").mkdir(parents=True)  # empty — no master for the item below
    cat = tmp_path / "static" / "catalog"
    _catalog(cat, "col", [{"title": "A", "source_url": "https://x/a.jpg"}])
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps([{"source_url": "https://x/a.jpg", "aspect_crops": FULL_SET}]))

    dac.bake(results_path, [str(cat)], pack, write=True)

    doc = json.loads((cat / "col.json").read_text())
    assert "aspect_crops" not in doc["items"][0]


def test_bake_not_maximal_gate_drops_only_that_key(tmp_path):
    """A QUALITY gate rejection is NOT atomic like `malformed` — it drops only the offending key and
    keeps the item's other, perfectly good boxes (the renderer already falls back to a focal cover for
    a missing key, so this is a strict improvement over discarding three good crops for one bad one)."""
    pack = tmp_path / "art-pack"
    lib = pack / "_Library"
    lib.mkdir(parents=True)
    _master(lib, "https://x/a.jpg", 1000, 1000)
    cat = tmp_path / "static" / "catalog"
    _catalog(cat, "col", [{"title": "A", "source_url": "https://x/a.jpg"}])
    partial = dict(FULL_SET)
    partial["4:3"] = [0.4, 0.44375, 0.6, 0.55625]  # correct 4:3 aspect, but only 0.2 wide -> not_maximal
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps([{"source_url": "https://x/a.jpg", "aspect_crops": partial}]))

    dac.bake(results_path, [str(cat)], pack, write=True)

    doc = json.loads((cat / "col.json").read_text())
    ac = doc["items"][0]["aspect_crops"]
    assert "4:3" not in ac
    assert set(ac.keys()) == {"16:9", "9:16", "3:4"}  # the three good siblings survive


def test_bake_focal_outside_gate_drops_only_that_key(tmp_path):
    pack = tmp_path / "art-pack"
    lib = pack / "_Library"
    lib.mkdir(parents=True)
    _master(lib, "https://x/a.jpg", 1000, 1000)
    cat = tmp_path / "static" / "catalog"
    _catalog(cat, "col", [{"title": "A", "source_url": "https://x/a.jpg", "focal_point": [0.3, 0.6]}])
    boxes = {
        "16:9": [0.0, 0.1, 1.0, 0.6625],
        "9:16": [0.4375, 0.0, 1.0, 1.0],   # excludes focal_x=0.3 -> focal_outside
        "4:3": [0.0, 0.05, 1.0, 0.8],
        "3:4": [0.1, 0.0, 0.85, 1.0],
    }
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps([{"source_url": "https://x/a.jpg", "aspect_crops": boxes}]))

    dac.bake(results_path, [str(cat)], pack, write=True)

    doc = json.loads((cat / "col.json").read_text())
    ac = doc["items"][0]["aspect_crops"]
    assert "9:16" not in ac
    assert set(ac.keys()) == {"16:9", "4:3", "3:4"}


# --------------------------------------------------------------------------- --report / --fail-under

def test_report_writes_per_item_qa_json(tmp_path):
    pack = tmp_path / "art-pack"
    lib = pack / "_Library"
    lib.mkdir(parents=True)
    _master(lib, "https://x/a.jpg", 1000, 1000)
    cat = tmp_path / "static" / "catalog"
    _catalog(cat, "col", [{"title": "A", "source_url": "https://x/a.jpg"}])
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(
        [{"source_url": "https://x/a.jpg", "title": "A", "aspect_crops": FULL_SET}]))
    report_path = tmp_path / "report.json"

    dac.bake(results_path, [str(cat)], pack, write=False, report_path=report_path)

    report = json.loads(report_path.read_text())
    assert len(report) == 1
    assert report[0]["source_url"] == "https://x/a.jpg"
    assert report[0]["valid"] is True
    assert report[0]["reject_reason"] is None
    assert set(report[0]["keys"].keys()) == {"16:9", "9:16", "4:3", "3:4"}


def test_fail_under_returns_nonzero_and_skips_write_when_below_threshold(tmp_path):
    pack = tmp_path / "art-pack"
    lib = pack / "_Library"
    lib.mkdir(parents=True)
    _master(lib, "https://x/a.jpg", 1000, 1000)
    cat = tmp_path / "static" / "catalog"
    _catalog(cat, "col", [{"title": "A", "source_url": "https://x/a.jpg"}])
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(
        [{"source_url": "https://x/a.jpg", "aspect_crops": {"4:3": [0.5, 0.5, 0.3, 0.9]}}]))  # malformed

    rc = dac.bake(results_path, [str(cat)], pack, write=True, fail_under=50.0)

    assert rc == 1
    doc = json.loads((cat / "col.json").read_text())
    assert "aspect_crops" not in doc["items"][0]  # refused to write on gate failure


def test_fail_under_returns_zero_when_at_or_above_threshold(tmp_path):
    pack = tmp_path / "art-pack"
    lib = pack / "_Library"
    lib.mkdir(parents=True)
    _master(lib, "https://x/a.jpg", 1000, 1000)
    cat = tmp_path / "static" / "catalog"
    _catalog(cat, "col", [{"title": "A", "source_url": "https://x/a.jpg"}])
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps([{"source_url": "https://x/a.jpg", "aspect_crops": FULL_SET}]))

    rc = dac.bake(results_path, [str(cat)], pack, write=True, fail_under=50.0)

    assert rc == 0
    doc = json.loads((cat / "col.json").read_text())
    assert doc["items"][0]["aspect_crops"] == FULL_SET


# --------------------------------------------------------------------------- bake idempotency

def test_bake_idempotent(tmp_path, capsys):
    pack = tmp_path / "art-pack"
    lib = pack / "_Library"
    lib.mkdir(parents=True)
    _master(lib, "https://x/a.jpg", 1000, 1000)
    cat = tmp_path / "static" / "catalog"
    _catalog(cat, "col", [{"title": "A", "source_url": "https://x/a.jpg"}])
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps([{"source_url": "https://x/a.jpg", "aspect_crops": FULL_SET}]))

    dac.bake(results_path, [str(cat)], pack, write=True)
    doc1 = json.loads((cat / "col.json").read_text())
    assert doc1["items"][0]["aspect_crops"] == FULL_SET

    capsys.readouterr()
    dac.bake(results_path, [str(cat)], pack, write=True)
    out = capsys.readouterr().out
    doc2 = json.loads((cat / "col.json").read_text())
    assert doc2 == doc1
    assert "0 item(s), 0 box(es)" in out


# --------------------------------------------------------------------------- emit skip-if-complete

def test_emit_skips_items_with_all_four_present(tmp_path):
    pack = tmp_path / "art-pack"
    lib = pack / "_Library"
    lib.mkdir(parents=True)
    _master(lib, "https://x/a.jpg", 4000, 3000)
    _master(lib, "https://x/b.jpg", 4000, 3000)
    cat = tmp_path / "static" / "catalog"
    _catalog(cat, "col", [
        {"title": "A", "source_url": "https://x/a.jpg", "aspect_crops": FULL_SET},   # already derived
        {"title": "B", "source_url": "https://x/b.jpg"},                              # still needs it
    ])
    out = tmp_path / "worklist.json"
    scratch = tmp_path / "scratch"

    rc = dac.emit(out, [str(cat)], pack, scratch, limit=None, collections=None)

    assert rc == 0
    worklist = json.loads(out.read_text())
    assert len(worklist) == 1
    assert worklist[0]["source_url"] == "https://x/b.jpg"
    assert worklist[0]["collection"] == "col"
    assert worklist[0]["master_size"] == [4000, 3000]
    downscaled = scratch / f"{dac._hash8('https://x/b.jpg')}.jpg"
    assert downscaled.exists()
    with Image.open(downscaled) as im:
        assert max(im.size) <= dac.DOWNSCALE_EDGE
