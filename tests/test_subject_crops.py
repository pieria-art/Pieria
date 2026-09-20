"""Unit tests for tools.subject_crops — the ADR-087 subject-filling aspect-crop deriver.

Synthetic plates stand in for real Audubon masters: a paper background, a drawn "ink" subject
blob, and (where needed) a caption block below a genuine blank gap. Real masters are NOT touched
here (that's the maintainer's own review pass over the real corpus, done separately) — these tests
are about the algorithm's contract: it finds the blob, cuts the caption, returns an exact-aspect
box, respects the coverage floor, is (approximately) minimal-area, gates correctly, and is
deterministic.
"""
import hashlib
import json

import numpy as np
from PIL import Image, ImageDraw

from tools import subject_crops as sc

PAPER = (250, 248, 240)
INK = (40, 110, 50)
DUST = (35, 90, 45)


def _plate(w, h, subject, caption=None, paper=PAPER, ink=INK):
    """subject/caption are (x0,y0,x1,y1) fractions of (w,h); caption=None omits it."""
    im = Image.new("RGB", (w, h), paper)
    d = ImageDraw.Draw(im)
    sx0, sy0, sx1, sy1 = subject
    d.ellipse([sx0 * w, sy0 * h, sx1 * w, sy1 * h], fill=ink)
    if caption is not None:
        cx0, cy0, cx1, cy1 = caption
        d.rectangle([cx0 * w, cy0 * h, cx1 * w, cy1 * h], fill=(25, 25, 25))
    return im


def _master(lib, source_url, im: Image.Image):
    name = f"col__t__{hashlib.sha1(source_url.encode()).hexdigest()[:8]}.jpg"
    im.save(lib / name, "JPEG", quality=95)
    return lib / name


# --------------------------------------------------------------------------- ink_mask

def test_ink_mask_finds_the_subject_blob_and_ignores_paper():
    im = _plate(300, 400, (0.2, 0.15, 0.8, 0.45))
    mask = sc.ink_mask(im, px=300)
    h, w = mask.shape
    # centre of the blob is ink
    assert mask[int(0.30 * h), int(0.50 * w)]
    # a corner (pure paper) is not
    assert not mask[2, 2]
    # roughly plausible overall ink fraction (blob is well inside the canvas)
    assert 0.05 < mask.mean() < 0.5


def test_ink_mask_drops_small_dust_specks():
    im = _plate(300, 400, (0.2, 0.15, 0.8, 0.45))
    d = ImageDraw.Draw(im)
    # a 2x2px fleck, far from the subject -- smaller than MIN_COMPONENT_FRACTION of the canvas
    d.rectangle([5, 5, 7, 7], fill=DUST)
    mask = sc.ink_mask(im, px=300)
    h, w = mask.shape
    assert not mask[6 * h // 400, 6 * w // 300]


def test_ink_mask_keeps_a_component_at_the_area_floor():
    # a blob sized comfortably ABOVE MIN_COMPONENT_FRACTION must survive.
    im = _plate(300, 400, (0.45, 0.45, 0.55, 0.55))
    mask = sc.ink_mask(im, px=300)
    assert mask.sum() > 0


# --------------------------------------------------------------------------- plate_bbox

def test_plate_bbox_matches_subject_extent():
    im = _plate(300, 400, (0.2, 0.15, 0.8, 0.45))
    mask = sc.ink_mask(im, px=300)
    box = sc.plate_bbox(mask)
    assert box is not None
    x0, y0, x1, y1 = box
    h, w = mask.shape
    assert abs(x0 / w - 0.2) < 0.05
    assert abs(x1 / w - 0.8) < 0.05
    assert abs(y0 / h - 0.15) < 0.05
    assert abs(y1 / h - 0.45) < 0.05


def test_plate_bbox_is_none_on_blank_canvas():
    im = Image.new("RGB", (100, 100), PAPER)
    mask = sc.ink_mask(im, px=100)
    assert sc.plate_bbox(mask) is None


# --------------------------------------------------------------------------- caption_band

def test_caption_band_cuts_a_dense_caption_below_a_genuine_gap():
    """Regression guard for the real bug: a caption's title line can be as dense as (or denser
    than) the subject -- the cut must come from the GAP, not from the caption's own density."""
    im = _plate(300, 400, (0.2, 0.10, 0.8, 0.55), caption=(0.30, 0.68, 0.70, 0.74))
    mask = sc.ink_mask(im, px=400)
    plate = sc.plate_bbox(mask)
    cut = sc.caption_band(mask, plate)
    assert cut is not None
    h, _w = mask.shape
    # cut sits between the subject's bottom (~0.55) and the caption's top (~0.68)
    assert 0.55 < cut / h < 0.68
    subject_mask = mask.copy()
    subject_mask[cut:, :] = False
    # none of the caption ink survives into the subject mask
    caption_row = int(0.70 * h)
    assert not subject_mask[caption_row, :].any()
    # the subject itself is untouched
    assert subject_mask[int(0.30 * h), :].any()


def test_caption_band_returns_none_when_subject_reaches_the_bottom():
    # no gap between the subject and the plate's bottom edge -> nothing to cut.
    im = _plate(300, 400, (0.2, 0.50, 0.8, 0.95))
    mask = sc.ink_mask(im, px=300)
    plate = sc.plate_bbox(mask)
    assert sc.caption_band(mask, plate) is None


def test_caption_band_ignores_a_sparse_top_region_far_from_the_bottom():
    """Regression guard: a full-bleed scenic plate can have a faint region near its own TOP (e.g.
    sky). Scanning for a caption gap must not wander that far from the bottom edge and mistake it
    for the subject/caption boundary — this collapsed the subject mask on a real master."""
    im = Image.new("RGB", (300, 400), PAPER)
    d = ImageDraw.Draw(im)
    # faint band across the TOP tenth (barely-there ink) -- NOT a caption.
    for y in range(2, 20):
        d.line([(0, y), (299, y)], fill=(246, 244, 236))
    # then a large, dense, edge-to-edge "scene" filling most of the rest of the canvas -- no gap
    # anywhere near the bottom.
    d.rectangle([0, 40, 299, 390], fill=INK)
    mask = sc.ink_mask(im, px=300)
    plate = sc.plate_bbox(mask)
    cut = sc.caption_band(mask, plate)
    # either no cut at all, or a cut that stays within the bottom search window -- never one that
    # wipes out the dense body of the scene.
    if cut is not None:
        h, _w = mask.shape
        subject_mask = mask.copy()
        subject_mask[cut:, :] = False
        assert subject_mask.sum() > 0.5 * mask.sum()


# --------------------------------------------------------------------------- subject_box

def _brute_force_minimal_box(mask, target_aspect, source_aspect, coverage, steps=120):
    """Reference implementation: linear (not bisected) scan over box heights, exhaustive
    per-scale position search via the same integral-image machinery. Used only to check
    `subject_box`'s minimal-area property on small synthetic masks."""
    h, w = mask.shape
    total = float(mask.sum())
    r = target_aspect / source_aspect
    hi = min(1.0, 1.0 / r)
    ii = sc._integral_image(mask)
    best_area = None
    for i in range(1, steps + 1):
        bh = hi * i / steps
        h_px = max(1, min(h, int(round(bh * h))))
        w_px = max(1, min(w, int(round(bh * r * w))))
        best_sum, _ = sc._best_position(ii, h_px, w_px, h, w, stride=1)
        if total > 0 and best_sum / total >= coverage:
            area = (w_px / w) * (h_px / h)
            if best_area is None or area < best_area:
                best_area = area
            break  # linear scan ascending in size -> first hit is the (near-)minimal one
    if best_area is None:
        return (hi * r) * hi  # the largest inscribed box -- the fallback case
    return best_area


def test_subject_box_exact_aspect_across_source_aspects():
    rng = np.random.default_rng(0)
    mask = np.zeros((200, 160), dtype=bool)
    mask[60:140, 40:120] = True
    for source_aspect in (1.0, 0.75, 1.333, 0.5326):
        for key in sc.ASPECT_CROP_KEYS:
            target = sc._target_ratio(key)
            box = sc.subject_box(mask, target, source_aspect, coverage=0.8)
            x0, y0, x1, y1 = box
            assert 0.0 <= x0 < x1 <= 1.0
            assert 0.0 <= y0 < y1 <= 1.0
            real_aspect = source_aspect * (x1 - x0) / (y1 - y0)
            assert abs(real_aspect - target) / target < sc.ASPECT_INEXACT_TOL
    del rng


def test_subject_box_meets_or_exceeds_the_coverage_floor_when_reachable():
    mask = np.zeros((200, 200), dtype=bool)
    mask[80:120, 80:120] = True  # a small centred square -- easily fits any aspect
    for key in sc.ASPECT_CROP_KEYS:
        target = sc._target_ratio(key)
        box = sc.subject_box(mask, target, 1.0, coverage=0.9)
        cov, _area, _paper = sc._box_stats(mask, mask, box)
        assert cov >= 0.9 - 1e-6


def test_subject_box_falls_back_to_largest_inscribed_when_unreachable():
    """Two blobs in opposite corners: no box of a narrow aspect can enclose both at high coverage,
    so subject_box must fall back to the largest box that fits the frame."""
    mask = np.zeros((200, 200), dtype=bool)
    mask[5:25, 5:25] = True
    mask[175:195, 175:195] = True
    target = 16 / 9
    box = sc.subject_box(mask, target, 1.0, coverage=0.95)
    x0, y0, x1, y1 = box
    r = target / 1.0
    hi = min(1.0, 1.0 / r)
    # the box is (approximately) the largest one this aspect can be
    assert (y1 - y0) >= hi - 0.02


def test_subject_box_is_approximately_minimal_area():
    mask = np.zeros((120, 100), dtype=bool)
    mask[40:80, 30:70] = True
    for key in sc.ASPECT_CROP_KEYS:
        target = sc._target_ratio(key)
        box = sc.subject_box(mask, target, 1.0, coverage=0.85)
        area = (box[2] - box[0]) * (box[3] - box[1])
        ref_area = _brute_force_minimal_box(mask, target, 1.0, coverage=0.85, steps=120)
        # generous tolerance: brute force uses a coarse linear scan, subject_box a fine bisection.
        assert area <= ref_area + 0.05


def test_subject_box_handles_empty_mask_without_raising():
    mask = np.zeros((50, 50), dtype=bool)
    box = sc.subject_box(mask, 4 / 3, 1.0, coverage=0.9)
    x0, y0, x1, y1 = box
    assert 0.0 <= x0 < x1 <= 1.0
    assert 0.0 <= y0 < y1 <= 1.0


# --------------------------------------------------------------------------- derive determinism

def test_derive_is_deterministic(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    im = _plate(300, 400, (0.2, 0.10, 0.8, 0.55), caption=(0.30, 0.68, 0.70, 0.74))
    p = _master(lib, "https://x/a.jpg", im)
    r1 = sc.derive(p)
    r2 = sc.derive(p)
    assert r1["aspect_crops"] == r2["aspect_crops"]
    assert r1["diag"] == r2["diag"]


def test_derive_returns_all_four_keys_with_exact_aspects(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    im = _plate(300, 400, (0.15, 0.15, 0.85, 0.55), caption=(0.30, 0.68, 0.70, 0.74))
    p = _master(lib, "https://x/b.jpg", im)
    r = sc.derive(p)
    assert set(r["aspect_crops"].keys()) == set(sc.ASPECT_CROP_KEYS)
    sa = r["source_aspect"]
    for key, box in r["aspect_crops"].items():
        target = sc._target_ratio(key)
        x0, y0, x1, y1 = box
        real_aspect = sa * (x1 - x0) / (y1 - y0)
        assert abs(real_aspect - target) / target < sc.ASPECT_INEXACT_TOL


# --------------------------------------------------------------------------- gate table

FULL_BOX_4_3 = [0.1, 0.1, 0.9, 0.7]  # exact 4:3 at source_aspect=1.0: bw=0.8, bh=0.6, bw/bh==4/3


def test_gate_rejects_malformed_box():
    reason, flags = sc.gate_subject_box([0.5, 0.5, 0.3], 1.0, 4 / 3,
                                         coverage=0.9, area=0.5, paper_fraction=0.2)
    assert reason == "malformed"
    assert flags == []


def test_gate_rejects_out_of_bounds_box():
    reason, flags = sc.gate_subject_box([-0.1, 0.1, 0.5, 0.6], 1.0, 4 / 3,
                                         coverage=0.9, area=0.5, paper_fraction=0.2)
    assert reason == "out_of_bounds"


def test_gate_rejects_aspect_inexact_box():
    reason, flags = sc.gate_subject_box([0.1, 0.1, 0.9, 0.9], 1.0, 4 / 3,
                                         coverage=0.9, area=0.5, paper_fraction=0.2)
    assert reason == "aspect_inexact"


def test_gate_never_rejects_on_coverage_area_or_paper_alone():
    """The design decision (measured against the real corpus): a box that's already the best
    available for a fixed exact aspect is never hard-rejected just for having low coverage, small
    area, or lots of paper -- only structural defects reject. Flags still fire."""
    reason, flags = sc.gate_subject_box(FULL_BOX_4_3, 1.0, 4 / 3,
                                         coverage=0.01, area=0.001, paper_fraction=0.999,
                                         ink_fraction=0.001)
    assert reason is None
    assert "low_coverage" in flags
    assert "small_area" in flags
    assert "high_paper" in flags
    assert "near_empty_plate" in flags


def test_gate_accepts_clean_box_with_no_flags():
    reason, flags = sc.gate_subject_box(FULL_BOX_4_3, 1.0, 4 / 3,
                                         coverage=0.92, area=0.4, paper_fraction=0.3,
                                         ink_fraction=0.2, caption_cut=0.7)
    assert reason is None
    assert flags == []


def test_gate_caption_cut_none_is_never_flagged():
    """caption_cut is accepted but deliberately not turned into a flag -- see gate_subject_box's
    docstring: most real captions never register as ink at the working resolution at all, which
    is the SAFE default, not a problem worth a human's time on 400+ review rows."""
    reason, flags = sc.gate_subject_box(FULL_BOX_4_3, 1.0, 4 / 3,
                                         coverage=0.92, area=0.4, paper_fraction=0.3,
                                         ink_fraction=0.2, caption_cut=None)
    assert reason is None
    assert flags == []


# --------------------------------------------------------------------------- CLI plumbing

def test_build_worklist_matches_masters_by_hash(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    cat = tmp_path / "static" / "catalog"
    cat.mkdir(parents=True)
    su = "https://x/c.jpg"
    _master(lib, su, _plate(300, 400, (0.2, 0.2, 0.8, 0.6)))
    (cat / "col.json").write_text(json.dumps({
        "id": "col",
        "items": [{"title": "C", "source_url": su, "aspect_crops": {"4:3": [0, 0, 1, 1]}}],
    }))
    items = sc._build_worklist([str(cat)], ["col"], lib)
    assert len(items) == 1
    assert items[0]["source_url"] == su
    assert items[0]["old_aspect_crops"] == {"4:3": [0, 0, 1, 1]}


def test_process_item_reports_error_for_unreadable_master(tmp_path):
    bogus = tmp_path / "bogus.jpg"
    bogus.write_bytes(b"not a jpeg")
    payload = {"source_url": "https://x/d.jpg", "collection": "col", "title": "D",
               "master": str(bogus), "coverage": sc.COVERAGE_DEFAULT}
    result = sc._process_item(payload)
    assert "error" in result
    assert result["flags"] == ["error"]
    assert result["aspect_crops"] == {}


def test_build_summary_computes_median_area_and_fullwidth_fraction():
    results = [
        {"source_url": "u1", "title": "MacGillivray's Finch", "flags": [],
         "aspect_crops": {"4:3": [0.0, 0.2, 1.0, 0.8], "16:9": [0.0, 0.3, 1.0, 0.7],
                           "9:16": [0.2, 0.0, 0.8, 1.0], "3:4": [0.2, 0.0, 0.8, 1.0]},
         "diag": {"boxes": {"4:3": {"coverage": 0.9, "area": 0.6, "paper_fraction": 0.5},
                             "16:9": {"coverage": 0.9, "area": 0.7, "paper_fraction": 0.5},
                             "9:16": {"coverage": 0.9, "area": 0.6, "paper_fraction": 0.5},
                             "3:4": {"coverage": 0.9, "area": 0.6, "paper_fraction": 0.5}}}},
        {"source_url": "u2", "title": "Other Bird", "flags": ["4:3:flag:low_coverage"],
         "aspect_crops": {"4:3": [0.3, 0.3, 0.7, 0.7]},
         "diag": {"boxes": {"4:3": {"coverage": 0.5, "area": 0.16, "paper_fraction": 0.3}}}},
    ]
    summary = sc.build_summary(results)
    assert summary["n_items"] == 2
    assert summary["median_4_3_area"] == round((0.6 + 0.16) / 2, 4)
    assert summary["fullwidth_4_3_fraction"] == 0.5  # only u1's 4:3 box is full-width
    assert summary["finch_4_3_box"] == [0.0, 0.2, 1.0, 0.8]
    assert summary["review_list_size"] == 1
