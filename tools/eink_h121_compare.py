"""
tools/eink_h121_compare.py — H-121 vs shipping vs the barycentric control, for a DIGITAL judgement.
(maintainer tool — NOT part of the runtime image)

WHY THIS EXISTS. ADR-120: the panel judged shipping over `eink_barycentric.dither` 6/6 on grain, while
naming barycentric's colour truer in 5/6 — the decomposition was right, the per-pixel IGN realisation
was what lost. H-121 registers two replacement realisations (`tools/eink_h121.py`, arms C and D). There
will be no panel shoot before 1.0 (Josh, 2026-09-21), so this renders all four variants side by side and
serves them locally for an on-screen judgement of COLOUR, TONE AND DIRECTION — not a substitute for the
ADR-084 panel protocol; the viewer says so on the page (ADR-120 is the proof that a panel and a digital
preview can disagree, at all).

FIVE VARIANTS, per work, all shown in the MEASURED ink colours (`eink_panel_model.ink_srgb8()`, ADR-117):
    A — shipping `epaper.render_for_epaper`, byte-identical, exactly as `eink_candidate.variant_a_index`.
    B — ADR-120's barycentric-with-per-pixel-IGN, the known 6/6 loser: included as the control that
        anchors the comparison, not as a candidate.
    C — H-121 arm 1: weight-residual diffusion (`eink_h121.dither_residual`).
    D — H-121 arm 2: clustered-dot mask realisation (`eink_h121.dither_clustered`).
    E — shipping's OWN algorithm (white-point tone LUT, gamma-space PIL Floyd-Steinberg) with ONLY the
        palette swapped to the measured inks (`variant_e_index`). Untried until now: ADR-117's blow-up
        was Floyd-Steinberg on the measured palette in LINEAR light; shipping dithers in GAMMA space.

FRAMING is production's, reused whole from `eink_candidate` (`frame_work`, `resolve_work`,
`variant_a_index`) — not re-implemented here.

    python -m tools.eink_h121_compare --work 9 --work 52 \
        --work Artwork/_Library/dutch-golden-age__the-night-watch__ff740524.jpg \
        --work Artwork/_Library/masterpieces__the-scream__867d895b.jpg \
        --work Artwork/_Library/american-art__american-gothic__bca4e65d.jpg \
        --out bench-eink/analysis/h121_compare
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import epaper as ep  # noqa: E402
from tools import eink_candidate as cand  # noqa: E402
from tools import eink_color as ec  # noqa: E402
from tools import eink_gamut as eg  # noqa: E402
from tools import eink_h121 as h121  # noqa: E402
from tools import eink_panel_model as pm  # noqa: E402

DEFAULT_W = cand.DEFAULT_W
DEFAULT_H = cand.DEFAULT_H

VARIANT_LABEL = {
    "A": "A — shipping (production today)",
    "B": "B — barycentric + per-pixel IGN (ADR-120's 6/6 loser; control)",
    "C": "C — H-121: weight-residual diffusion",
    "D": "D — H-121: clustered-dot mask",
    "E": "E — shipping's algorithm, measured palette (gamma-space FS, untried until now)",
    "EP": "E′ — E + black-point compensation to the measured black ink's own colour",
}


_MEASURED_PALETTE = [tuple(int(c) for c in row) for row in pm.ink_srgb8().tolist()]

# The measured BLACK ink's own colorimetry (ADR-116) — L*39.6, C*10.2, h335.4 — the target E' compensates
# TOWARD, as opposed to the derived NEUTRAL floor (L*49.64, `eink_gamut.neutral_floor()`) B/C/D inherit.
_BLACK_TARGET_LAB = ec.xyz_to_lab(pm.ink_xyz()[0:1], pm.media_white())[0]  # (L, a, b)


def _quantize_to_measured_palette(toned_rgb) -> np.ndarray:
    """PIL gamma-space Floyd-Steinberg against the measured inks, decoded to an (H,W) ink-index map.
    Shared by E and E' — the ONLY difference between them is what happens to `toned_rgb` before this.

    NOT `ep._flat_palette` for the unused 250 slots: it pads with (0,0,0), which is invisible for the
    vendor swatch (its own black IS (0,0,0)) but is a PHANTOM colour for the measured inks (measured
    black is (61,55,66) — real e-ink black reflects something, ADR-108). Left as (0,0,0), a very dark
    source pixel could dither to a padding slot that isn't any real ink at all — documented in
    `bench-eink/analysis/EPAPER_FLAT_PALETTE_PHANTOM.md`. Pad with a duplicate of a REAL ink instead,
    so PIL's nearest-colour search can never resolve off-palette.
    """
    pal_img = ep.Image.new("P", (1, 1))
    flat = []
    for rgb in _MEASURED_PALETTE:
        flat.extend(rgb)
    flat += list(_MEASURED_PALETTE[0]) * (256 - len(_MEASURED_PALETTE))
    pal_img.putpalette(flat)
    quantized = toned_rgb.quantize(palette=pal_img, dither=ep.Image.Dither.FLOYDSTEINBERG)
    arr = np.asarray(quantized.convert("RGB"), dtype=np.uint8)
    idx = np.full(arr.shape[:2], -1, dtype=np.int16)
    for i, rgb in enumerate(_MEASURED_PALETTE):
        idx[np.all(arr == np.array(rgb, dtype=np.uint8), axis=-1)] = i
    unmapped = int((idx < 0).sum())
    if unmapped:
        raise AssertionError(f"{unmapped} pixel(s) did not map to a measured ink")
    return idx.astype(np.uint8)


def variant_e_index(fitted_rgb) -> np.ndarray:
    """(H,W) ink indices for variant E: `epaper.render_for_epaper`'s EXACT `spectra6` algorithm —
    the white-point tone LUT (`epaper.SPECTRA6_WHITE_POINT`=0.75, `SPECTRA6_GAMMA`=1.0) then PIL
    Floyd-Steinberg IN GAMMA SPACE — with ONLY the palette table swapped: `pm.ink_srgb8()` (the
    MEASURED inks, ADR-116) instead of `epaper.SPECTRA6_DITHER_PALETTE` (the vendor swatch).

    Built here, not in `epaper.py` (which stays untouched), by calling its own tone/palette helpers —
    the same reuse discipline `eink_candidate.variant_a_index` already uses for the unmodified path.
    Untried until now: ADR-117 tried Floyd-Steinberg on the measured palette in LINEAR light and it
    blew up (unbounded error, a magenta band on Café Terrace's awning); shipping dithers in GAMMA
    space, a different regime that nobody has run against the measured inks before.
    """
    toned = fitted_rgb.point(list(ep._tone_lut(ep.SPECTRA6_WHITE_POINT, ep.SPECTRA6_GAMMA)) * 3)
    return _quantize_to_measured_palette(toned)


def _black_point_compensate(rgb8: np.ndarray) -> np.ndarray:
    """The ONLY difference between E and E': lift the source's black point onto the MEASURED BLACK
    INK's own Lab coordinates (L*39.6, C*10.2, h335.4 — ADR-116) instead of leaving it at source L*=0
    (E, crushes: Night Watch 95.9% black) or the derived NEUTRAL floor (B/C/D's L*49.64, measured
    8.4 dE00 mean on the S4 ladder — washed, wrong hue). This is the untried middle ADR-120's H-121
    follow-up asks for.

    Two moves, both anchored on the SOURCE pixel's own L* (before any lift), so the compensation
    strength is a property of how dark the pixel already was, not of anything downstream:
      1. LIGHTNESS: the same affine floor-lift construction `eink_gamut.neutral_floor`'s consumers use
         (source 0 -> the floor, source 100 -> 100), just anchored to the ink's own L*39.6 instead of
         the derived L*49.64 -- a materially lower, tighter floor, which is the whole reason this
         should wash less.
      2. CHROMA: the measured black is NOT neutral (a*+9.3, b*-4.2) -- lifting only L and leaving a*/b*
         alone would push a genuinely dark pixel toward a lightness the palette can produce, but with
         a hue the black ink cannot (it can only ever print its own chroma at that darkness). So each
         pixel's a*/b* is blended toward the ink's OWN a*/b*, with a weight that is 1 at source L*=0
         and 0 at source L* >= the target floor -- only pixels darker than the floor itself inherit the
         ink's chroma; anything already lighter than the floor is untouched on the chroma axis.
    """
    target_L, target_a, target_b = (float(v) for v in _BLACK_TARGET_LAB)
    lab = ec.xyz_to_lab(ec.srgb8_to_xyz(rgb8), pm.media_white())
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]

    L2 = target_L + L * (100.0 - target_L) / 100.0
    weight = np.clip((target_L - L) / target_L, 0.0, 1.0)
    a2 = a * (1.0 - weight) + target_a * weight
    b2 = b * (1.0 - weight) + target_b * weight

    xyz2 = ec.lab_to_xyz(np.stack([L2, a2, b2], axis=-1), pm.media_white())
    srgb2 = ec.linear_to_srgb(np.clip(ec.xyz_to_linear_rgb(xyz2), 0.0, None))
    return np.clip(np.round(srgb2 * 255.0), 0, 255).astype(np.uint8)


def variant_e_prime_index(fitted_rgb) -> np.ndarray:
    """E' = E + black-point compensation targeting the measured black ink's own colorimetry. Every
    other step is identical to `variant_e_index` (same tone LUT, same gamma-space FS, same palette) —
    only the black-point handling changes, so any spectral-signature change is attributable to that
    alone."""
    rgb8 = np.asarray(fitted_rgb.convert("RGB"), dtype=np.uint8)
    bpc = ep.Image.fromarray(_black_point_compensate(rgb8), mode="RGB")
    toned = bpc.point(list(ep._tone_lut(ep.SPECTRA6_WHITE_POINT, ep.SPECTRA6_GAMMA)) * 3)
    return _quantize_to_measured_palette(toned)


def build_work(spec: str, out: Path, w: int, h: int, seed: int) -> dict:
    rows = cand._load_corpus()
    n, path = cand.resolve_work(spec, rows)
    slug = cand.slug_for(path)
    crop, focal, authored = cand.frame_work(path, n, w, h)
    wdir = out / slug
    wdir.mkdir(parents=True, exist_ok=True)

    idx_a, _arr_a = cand.variant_a_index(path, w, h, focal, crop)
    fitted = ep._fit_rgb(path, w, h, "cover", focal, crop)
    rgb8 = np.asarray(fitted.convert("RGB"), dtype=np.uint8)
    lab = eg.map_srgb8(rgb8)
    q = eg.to_quantiser_srgb8(lab)                       # gamut-mapped onto the panel's achievable
                                                          # gamut, EXACTLY what B/C/D decompose against

    noise = cand.blue_noise(q.shape[0], q.shape[1], seed, slug)
    idx_b = cand.eba.dither(q, noise=noise)
    idx_c = h121.dither_residual(q)
    idx_d = h121.dither_clustered(q, tile=16)
    idx_e = variant_e_index(fitted)
    idx_ep = variant_e_prime_index(fitted)
    # Each variant's own pre-quantisation source, for its local-mean target — E and E' never
    # gamut-map (that is the whole point of testing shipping's own gamma-space path); E' additionally
    # runs the source through black-point compensation before the tone LUT.
    toned = fitted.point(list(ep._tone_lut(ep.SPECTRA6_WHITE_POINT, ep.SPECTRA6_GAMMA)) * 3)
    e_source = np.asarray(toned.convert("RGB"), dtype=np.uint8)
    ep_rgb8 = np.asarray(fitted.convert("RGB"), dtype=np.uint8)
    ep_bpc = ep.Image.fromarray(_black_point_compensate(ep_rgb8), mode="RGB")
    ep_toned = ep_bpc.point(list(ep._tone_lut(ep.SPECTRA6_WHITE_POINT, ep.SPECTRA6_GAMMA)) * 3)
    ep_source = np.asarray(ep_toned.convert("RGB"), dtype=np.uint8)

    indices = {"A": idx_a, "B": idx_b, "C": idx_c, "D": idx_d, "E": idx_e, "EP": idx_ep}
    fracs = {v: cand.ink_fractions(idx) for v, idx in indices.items()}
    for v, idx in indices.items():
        cand.preview_rgb(idx).save(wdir / f"{v}_preview.png")
    fitted.save(wdir / "reference.jpg", "JPEG", quality=92)

    # H-121's falsifiable claim, for the weight-decomposition realisations only (A is shipping's own
    # RGB-space FS, not a decomposition; each variant is scored against ITS OWN pre-quantisation
    # source, so the comparison is apples-to-apples per variant, not against a shared image).
    local_mean = {
        "C": h121.local_mean_error(q, idx_c, block=16),
        "D": h121.local_mean_error(q, idx_d, block=16),
        "E": h121.local_mean_error(e_source, idx_e, block=16),
        "EP": h121.local_mean_error(ep_source, idx_ep, block=16),
    }
    # Decision frequency + the spectral proxy (low-freq energy share, spectral peak) for ALL FIVE, so
    # A anchors "tone" and B anchors "grain" the way ADR-120 judged them on the panel.
    decision_freq = {v: h121.decision_frequency(idx) for v, idx in indices.items()}
    spectral = {v: h121.spectral_low_freq_share(idx) for v, idx in indices.items()}

    coll, title = cand._title_of(path)
    result = {
        "slug": slug, "n": n, "image": str(path), "collection": coll, "title": title,
        "crop": list(crop) if crop else None, "authored_box": authored,
        "size": [w, h],
        "fracs": fracs, "local_mean_error": local_mean, "decision_frequency": decision_freq,
        "spectral": {v: {"low_freq_share": lo, "peak_cpd": pk} for v, (lo, pk) in spectral.items()},
    }
    print(f"[{slug}] n={n} crop={crop if crop else 'none (focal cover)'}")
    for v in ("A", "B", "C", "D", "E", "EP"):
        lo, pk = spectral[v]
        print(f"  {v}: {fracs[v]}  decision_freq={decision_freq[v]:.3f}  "
              f"low_freq_share={lo:.3f} peak={pk:.1f}cpd"
              + (f"  local_mean_err={local_mean[v]:.3f}" if v in local_mean else ""))
    return result


CAVEAT_HTML = """
<section class="caveat">
  <strong>Read this before judging anything below.</strong> This page is an LCD at roughly 100 ppi,
  viewed at desk distance. The panel these variants are built for is a 13.3&Prime; e-ink display at
  150.3 ppi, meant to be viewed across a room. <strong>Grain reads WORSE here than it does on the
  panel</strong> &mdash; ADR-120 is direct proof that the panel and a digital preview can disagree: the
  panel chose the variant these same kinds of previews flattered least. So this view is good for
  judging <strong>colour, tone and direction</strong>, and it is <strong>not</strong> a substitute for
  an ADR-084 panel judgement. There is no panel shoot planned before 1.0 &mdash; this is what there is
  to look at until there is one.
</section>
"""


VARIANTS = ("A", "B", "C", "D", "E", "EP")


def _work_section(r: dict) -> str:
    slug = r["slug"]
    w, h = r["size"]
    rows = []
    for v in VARIANTS:
        f = r["fracs"][v]
        lme = r["local_mean_error"].get(v)
        df = r["decision_frequency"].get(v)
        sp = r["spectral"][v]
        ink_cells = "".join(f"<td>{f[name]:.1f}</td>" for name in
                             ("black", "white", "red", "yellow", "blue", "green"))
        lme_cell = f"<td>{lme:.3f}</td>" if lme is not None else "<td>&mdash;</td>"
        df_cell = f"<td>{df:.3f}</td>"
        sp_cell = f"<td>{sp['low_freq_share']:.3f}</td><td>{sp['peak_cpd']:.1f}</td>"
        rows.append(f"<tr><td>{v}</td><td>{VARIANT_LABEL[v]}</td>{ink_cells}{lme_cell}"
                    f"{df_cell}{sp_cell}</tr>")
    table = f"""
    <table class="fracs">
      <thead><tr><th></th><th>variant</th><th>black</th><th>white</th><th>red</th><th>yellow</th>
        <th>blue</th><th>green</th><th>local-mean err (pts)</th><th>decision freq</th>
        <th>low-freq share (&lt;10 cpd)</th><th>spectral peak (cpd)</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>"""

    options = "".join(f'<option value="{v}">{VARIANT_LABEL[v]}</option>' for v in VARIANTS)
    return f"""
  <section class="work" id="{slug}">
    <h2>{r['title']} <span class="coll">{r['collection']}</span>
      <span class="dims">{w}&times;{h}{' &middot; authored crop' if r['authored_box'] else ''}</span></h2>
    <div class="row">
      <figure>
        <img src="{slug}/reference.jpg" width="{w}" height="{h}" class="pixelated">
        <figcaption>Reference (source, gamut-unconstrained)</figcaption>
      </figure>
      <figure class="flicker-fig">
        <div class="controls">
          <label>Slot 1 <select class="v1" data-slug="{slug}">{options}</select></label>
          <label>Slot 2 <select class="v2" data-slug="{slug}">{options}</select></label>
          <button class="flip" data-slug="{slug}">Flip (space)</button>
          <span class="now" data-slug="{slug}"></span>
        </div>
        <img src="" width="{w}" height="{h}" class="pixelated flicker-img" id="flicker-{slug}"
             data-slug="{slug}">
        <figcaption>Same screen position — flip to see the difference, don't scroll between two images</figcaption>
      </figure>
    </div>
    <div class="row thumbs">
      {''.join(f'''<figure><img src="{slug}/{v}_preview.png" width="{w//3}" height="{h//3}"
        class="pixelated"><figcaption>{v}</figcaption></figure>''' for v in VARIANTS)}
    </div>
    {table}
  </section>"""


def _index_html(results: list[dict]) -> str:
    sections = "\n".join(_work_section(r) for r in results)
    default_slugs = [r["slug"] for r in results]
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pieria — H-121 candidate viewer (digital judgement, no panel)</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ background:#202020; color:#eee; font:14px/1.5 system-ui, sans-serif; margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  h2 {{ font-size:16px; margin:32px 0 8px; }}
  .coll, .dims {{ color:#aaa; font-size:12px; font-weight:normal; margin-left:8px; }}
  .caveat {{ background:#5a3d00; border:1px solid #a06a00; color:#ffe9b0; padding:14px 18px;
             border-radius:6px; max-width:900px; margin-bottom:24px; }}
  .row {{ display:flex; gap:18px; flex-wrap:wrap; align-items:flex-start; }}
  .thumbs figure img {{ image-rendering:pixelated; display:block; }}
  figure {{ margin:0; background:#333; padding:10px; border-radius:4px; }}
  figure.flicker-fig {{ background:#111; }}
  img.pixelated {{ image-rendering:pixelated; image-rendering:crisp-edges; display:block; max-width:none; }}
  .row > figure > img.pixelated {{ max-width:640px; height:auto; }}
  .flicker-img {{ max-width:none !important; width:auto; }}
  figcaption {{ text-align:center; font-size:11px; color:#bbb; margin-top:6px; }}
  .controls {{ display:flex; gap:12px; align-items:center; margin-bottom:8px; font-size:12px; flex-wrap:wrap; }}
  .now {{ font-weight:bold; color:#ffd479; }}
  table.fracs {{ border-collapse:collapse; margin-top:10px; font-size:12px; }}
  table.fracs th, table.fracs td {{ border:1px solid #444; padding:3px 8px; text-align:right; }}
  table.fracs th:nth-child(2), table.fracs td:nth-child(2) {{ text-align:left; }}
  .scroller {{ overflow:auto; max-width:100%; }}
  select, button {{ background:#333; color:#eee; border:1px solid #555; padding:2px 6px; }}
  code {{ background:#111; padding:1px 5px; border-radius:3px; }}
</style>
</head>
<body>
  <h1>H-121 candidate viewer — digital judgement, native resolution</h1>
  {CAVEAT_HTML}
  <p>Every variant is shown in the MEASURED ink colours (<code>eink_panel_model.ink_srgb8()</code>),
  not the swatch's pure primaries — what the shipping and barycentric palettes disagree about is
  hidden if previewed any other way (ADR-117). Images are shown at their native rendered size
  (no scaling, <code>image-rendering: pixelated</code>) — scroll the page, don't zoom.</p>
  {sections}
<script>
(function() {{
  var slugs = {json.dumps(default_slugs)};
  slugs.forEach(function(slug) {{
    var v1 = document.querySelector('select.v1[data-slug="' + slug + '"]');
    var v2 = document.querySelector('select.v2[data-slug="' + slug + '"]');
    var img = document.getElementById('flicker-' + slug);
    var now = document.querySelector('.now[data-slug="' + slug + '"]');
    var showing = 1;
    v1.value = 'A'; v2.value = 'EP';
    function src(v) {{ return slug + '/' + v + '_preview.png'; }}
    function render() {{
      var v = showing === 1 ? v1.value : v2.value;
      img.src = src(v);
      now.textContent = 'showing ' + v;
    }}
    function flip() {{ showing = showing === 1 ? 2 : 1; render(); }}
    v1.addEventListener('change', render);
    v2.addEventListener('change', render);
    document.querySelector('button.flip[data-slug="' + slug + '"]').addEventListener('click', flip);
    img.addEventListener('click', flip);
    img.dataset.flip = 'bound';
    img.tabIndex = 0;
    img.addEventListener('keydown', function(e) {{ if (e.key === ' ') {{ e.preventDefault(); flip(); }} }});
    render();
  }});
  document.addEventListener('keydown', function(e) {{
    if (e.key !== ' ') return;
    if (e.target && ['SELECT', 'INPUT', 'BUTTON'].includes(e.target.tagName)) return;
  }});
}})();
</script>
</body>
</html>
"""


def run(work_specs: list[str], out: Path, w: int, h: int, seed: int) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    results = [build_work(spec, out, w, h, seed) for spec in work_specs]
    (out / "results.json").write_text(json.dumps(results, indent=1))
    (out / "index.html").write_text(_index_html(results))
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", action="append", required=True, dest="works")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--width", type=int, default=DEFAULT_W)
    ap.add_argument("--height", type=int, default=DEFAULT_H)
    ap.add_argument("--seed", type=int, default=20260921)
    args = ap.parse_args()
    run(args.works, args.out, args.width, args.height, args.seed)
    print(f"\n{len(args.works)} works -> {args.out}/index.html")


if __name__ == "__main__":
    main()
