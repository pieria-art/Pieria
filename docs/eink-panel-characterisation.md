# The Spectra 6 panel characterisation vault

> A measured corpus of what this specific EL133UF1 actually does, captured 2026-08-29, so that later
> sessions can look up panel behaviour instead of re-deriving it from judgements.
> Rig: `docs/eink-measurement-rig.md` · decisions: `.ai/decision_log.md` ADR-053, 081, 084, 088-093.

## What this is, and what it is not

**⚠️ UNITS: camera-RGB normalised to THIS panel's own black = 0 and white = 255. NOT sRGB.**
The per-photograph correction is an affine anchored on the black and white calibration patches. It
absorbs exposure and gross white balance; it does **not** characterise the camera's spectral
response, and no ColorChecker was available.

| In reach | Out of reach |
|---|---|
| Tone response, separation, collapse | Absolute colorimetry |
| Gamut *survival* (relative) | Any claim of the form "this ink is (r,g,b)" |
| Dither grain, anisotropy, modulation | Rewriting `SPECTRA6_DITHER_PALETTE` |
| Every A-vs-B comparison on this panel | Cross-panel comparison |

**Structure beats colour on this rig.** The normalisation is a *global* affine, so it distorts means
but leaves *local* structure intact. Variance, texture, anisotropy and modulation readouts are
therefore markedly more robust than mean-colour readouts and are not bounded by the mean error.

## How to use it

    python -m tools.eink_vault rederive --flat bench-eink/reference/flat.png

**The raw captures are the asset; every number is re-derivable from them.** The rig — this camera
lock, this flat field, this lighting, this geometry — cannot be recreated once it comes down. A
readout is arithmetic over a file. On the capture day, *six* analysis bugs were found and fixed
**after** the photographs were taken, and none cost a panel refresh.

📏 **Capture generously, analyse later.** A run that banks raws with their conditions has value even
when every number derived at the time turns out to be wrong.

## The error bars — read these before believing any number

| quantity | value | how it was obtained |
|---|---|---|
| Camera-only repeatability | **~0.9/255** | 3 grabs, unchanged frame |
| **Refresh-to-refresh floor** | **~16/255 worst, 6.7 mean** | same target, two separate refreshes |
| Flat-field residual | 4/255 (from 29) | field built on photo A, applied to photo B |
| Illumination drift | 0.5% / 20 min | two flat fields, 20 min apart |

**A difference smaller than ~16/255 in a dark chromatic channel is below resolution, not a finding.**
The floor is dominated by red and green in their *dark* channels, where the affine's ~2.4x stretch
amplifies small raw differences.

⚠️ **Field-vs-strip agreement is NOT an error bar.** Black and white are the affine's anchors, so
their agreement is circular; the chromatic inks differ by 40-81/255 between a 250x96 strip patch and
a 509x345 field, which is a patch-size effect, not instrument noise.

## The design

A **central composite design** over four render levers — white-point, gamma, chroma-gamma,
saturation. One-at-a-time sweeps answer "what does this lever do" and are structurally unable to
answer "does this lever change what another lever does"; only a crossed design separates main effects
from interactions.

- **Axial** points — each lever along its own axis, others at centre. Captures curvature.
- **Corners** — full 2^4, so every main effect and two-factor interaction is unconfounded.
- **Centre replicates** — the pure-error estimate for a *dithered* target.

⚠️ **Run order is randomised.** The rig runs on daylight; running a lever's levels in sequence would
alias illumination drift onto that lever's effect.

## Targets

| target | dithered | measures |
|---|---|---|
| `primaries` | no | what each ink actually produces |
| `inkmix` | no | the optical mixing law, 15 ink pairs x 5 ratios, + element-size + metamer |
| `uniformity` | no | spatial non-uniformity — **needs a 180-degree paired capture** |
| `tonefine` | yes | tone response *and* dither grain, 26 steps through the ink ceiling |
| `huevalue` | yes | chroma survival across hue x value — ADR-091's table |
| `surround` | yes | whether an identical input measures the same in 25 surrounds |
| `edges` | yes | error-diffusion smear asymmetry |
| `linepairs` | yes | detail retention, coarse periods only |
| `resample` | yes | resampler loss vs panel loss |

Deliberately **not** built, so they are not re-proposed: slanted-edge MTF (returns the webcam's MTF at
0.86 camera px per panel px, and error diffusion makes the edge stochastic); per-ink ramps (redundant
with `inkmix`); a text legibility ladder (a human threshold the rig cannot resolve); viewing angle,
ambient dependence and temperature (the rig would be measuring itself).

## Findings

*(Filled in from the analysis pass. Numbers below are the first coherent read and carry the caveats
above; they are not yet final.)*

### Ink mixing is linear in light, with dot gain at high coverage
A 1:1 black/white checkerboard measures **178.9** against a linear-light prediction of 188 — so the
inks mix linearly in *light*, and any additivity test must linearise first. At high coverage the
measurement runs far darker than linear (66 where linear predicts 137), which is **dot gain**.

### ADR-091 confirmed in its mechanism, refined at the low end
Chroma survival does collapse as value rises, and white-point moves where chroma lives:

| white-point | mean chroma, v=40 -> v=245 |
|---|---|
| off | 8 - 54 - 80 - **97** - 75 - 50 |
| 0.64 | 4 - 27 - 49 - 70 - 97 - **105** |
| 0.88 | 6 - 41 - 65 - **84** - 68 - 43 |

⚠️ The v=40 row sits at 4-8/255, **below the dark measurement floor**, so the apparent low-value
collapse may be instrument rather than panel. It needs a dedicated low-value target.

### White-point and chroma interact strongly
Chroma-gamma 2.0 at the top of the range: **`wp off` leaves chroma 2.5 with all 12 hues collapsed;
`wp 0.64` leaves 49.7 with one.** White-point protects colour against the chroma lever by ~20x. A
one-lever-at-a-time sweep cannot see this, and it is a plausible reason ADR-088's chroma work failed.

### The detail-versus-grain trade, measured
Collapse falls as either lever is applied (7 -> 0-1) while grain climbs steeply with gamma
(30 -> 63 -> 98). This is the trade a judge described on a bronze statue, now with numbers.

## Instrument defects found and fixed on the capture day

Recorded because every one was **silent**, and because the pattern is the lesson: *checks that can
only pass*.

1. The rig's own self-test had been failing since `a20d785`; the doc quoted an accuracy figure from
   before the break.
2. `panel_bbox` subtracted a fixed 48 px to undo a dilation — worst on a *clean* capture.
3. Camera controls were discarded at stream start: `lock` verified a value the stream then threw
   away. Exposure and gain both walked freely.
4. The measurable area extended 96 px **beyond** the fiducials, where the homography extrapolates.
5. A residual scale error the homography cannot report, because it fits its own four points exactly.
6. The calibration strip's black anchor sat beside the white patch and was lifted by its scatter.
7. `read_panel` aligned **before** solving the affine, moving the anchors out from under the function
   that reads them. `patch_residual` reported a healthy 2-3 while the image was destroyed.
