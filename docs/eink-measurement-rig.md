# The e-ink measurement rig

> How to photograph the Spectra 6 panel so the result is a *measurement* rather than a picture.
> Built 2026-08-28. Companion to `tools/eink_target.py` (renders targets) and
> `tools/eink_measure.py` (reads photographs back).

## Why it exists

Every calibration judgement before this was ordinal and human — "B is closer than A" — which caps
throughput at a person standing in front of a panel, cannot be replayed, and carries the noise of
comparing across a ~15 s refresh from memory. A photograph is a machine-readable observation of the
actual output, so trueness can be *computed* against the reference.

The human eye is not removed from the project; it is removed from the *metric*. Measured trueness is
the engineering target. Whether a true render is the one you want on your wall is a separate question
with at least one recorded disagreement (see the Milkmaid, `bench-eink/fullpanel.jsonl`).

## Physical setup

- **Camera:** Logitech C920 (1920x1080) on a fixed overhead arm, straight down, panel laid flat.
- **Distance:** panel filling ~85–90% of frame width — about **0.86 camera px per panel px**. The
  panel is 1600 px wide and the sensor is 1920, so ~1.05 is the physical ceiling. The whole black
  registration border must stay in shot; without it there is no homography and no measurement.
- **Light:** ambient with the window curtain closed, plus a clip light **raking across the panel at a
  shallow angle** — never aimed at it.

  ⚠️ **A light perpendicular to the panel reflects straight back into a straight-down lens.** That was
  tried: it put a specular blowout across the calibration patch strip, clipping the exact patches
  every measurement is anchored to. Specular glare is unrecoverable — where it lands, the ink beneath
  is simply gone. A brightness *gradient* is by contrast entirely correctable (see flat-field, below),
  so prefer soft uneven light over hard even light.

- **Surface:** the panel currently sits on a bright wood floor. This defeats automatic panel
  detection (the floor is brighter than parts of the panel), so `read --roi` may be needed. A dark
  matte surface under the panel removes the problem.

## Camera lock — and why read-back is mandatory

```
python -m tools.eink_measure lock --device /dev/video0
```

Auto exposure, auto white balance and autofocus each re-decide per frame, so with them on, two
photographs of the same panel are two different measurements.

| control | value | note |
|---|---|---|
| `auto_exposure` | 1 | Manual Mode |
| `exposure_time_absolute` | 620 | **lighting-specific — re-sweep whenever the light changes.** 620 suits ambient-only; the same rig wanted 200 under a clip light. Quantised: 560–740 are indistinguishable |
| `gain` | 24 | see the warning below |
| `white_balance_automatic` | 0 | |
| `white_balance_temperature` | 4000 | |
| `focus_automatic_continuous` | 0 | must be set **before** `focus_absolute` |
| `focus_absolute` | 30 | writing this first fails with "Permission denied" — the control is
  inactive until continuous AF is off. It looks like a permissions problem and is not one. |
| `power_line_frequency` | 2 | 60 Hz; stops mains flicker beating with the shutter |

**exposure 620 + gain 24** (ambient only) puts the panel's white at 214 with **zero clipped pixels on
the panel**. Headroom matters more than brightness: a clipped white anchor invalidates the entire
correction.

⚠️ `exposure_dynamic_framerate` must be **0** here. With it at 1 the camera drops the frame rate to
allow longer shutters, which changes what a given exposure number means — the same value produced a
panel max of 214 with it off and 148 with it on. Either is workable; mixing them across a flat-field
and a measurement is not.

**Measured, and worth knowing before adding a lamp:** ambient-only light was FLATTER than the clip
light — spread 54/255 versus 116/255. The lamp was solving a problem the rig did not have.

⚠️ **The C920 walks its own gain back up.** Measured during setup: gain went 0 → 109 → 255 across an
exposure sweep *while in manual exposure mode*. At gain 255 roughly 30% of the panel clips. Every
capture therefore re-asserts gain immediately before grabbing (`GAIN_CTRL` in `eink_measure.py`), and
`lock` reads every control back. The failure mode is nasty precisely because it is quiet — it presents
as a lighting problem, not a camera problem.

## Capturing

```
python -m tools.eink_measure capture --device /dev/video0 --settle --out bench-eink/shot.png
```

⚠️ **Wait for the panel, not for a stopwatch.** A Spectra 6 refresh takes **~22 s measured on this
panel** (the ~9–16 s figure came from spec sheets) and drives the
pixels through inversion and flashing phases on the way, so a frame grabbed early is not an early
version of the final image — it is a *different* image. The first capture of this rig was taken
mid-refresh and produced a patch residual of 97/255 and a negative channel gain, which reads exactly
like a badly calibrated camera. `--settle` grabs until two consecutive frames agree, which adapts to
whatever the panel and light are actually doing; a fixed sleep would be slower on average and wrong
exactly when the panel is slowest.

Persistent failure to settle means something is moving in shot, or the light is flickering.

## Targets

```
sudo python3 -m tools.eink_bench target primaries    # what THIS panel's inks actually are
sudo python3 -m tools.eink_bench target ramp         # tone response / where highlights stop separating
sudo python3 -m tools.eink_bench target huegrid      # gamut survival across hue x saturation
sudo python3 -m tools.eink_bench target art --n 16 [render flags]
```

Every target carries a black **registration frame** (four corners → homography) and a strip of **pure
ink patches** (→ per-photograph colour correction). Composition happens *after* quantisation, because
re-quantising the finished canvas would dither the calibration patches, and a dithered patch measures
the dither rather than the ink.

**Pack many conditions into one frame.** The panel refresh is now the bottleneck, and colour e-ink has
finite refresh cycles — this is the only Spectra 6 panel the project owns. `huegrid` measures 72
hue×saturation cells in a *single* refresh because the camera resolves them spatially. Dense targets,
not parameter sweeps.

⚠️ **A pattern is not art.** Floyd–Steinberg's output depends on neighbouring content, so flat patches
do not exercise error diffusion the way a painting does. Patterns characterise the **panel**; the
`art` target characterises the **render**. Neither substitutes for the other (ADR-084, one level up).

## Reading a photograph

```
python -m tools.eink_measure read shot.png --target bench-eink/target_primaries_1600x1200.png --primaries
```

1. **Rectify** — the registration frame's corners give a homography onto the render's pixel grid.
   Re-solved per frame even on a fixed rig: a nudged mount would otherwise corrupt every later
   measurement with no visible symptom.
2. **Normalise** — a per-channel affine solved from the **black and white patches only**.

   ⚠️ Anchoring on all six inks is wrong twice over: the panel cannot emit pure primaries, so the fit
   chases impossible targets (measured: residual 65–108/255 and a negative channel gain); and it is
   circular, because the chromatic inks are the thing being measured. Black and white are definitional
   — they set the range — so everything is expressed in **panel-relative units**, this panel's own
   black = 0 and its own white = 255.
3. **Fuse, then measure** — dithered output must be downscaled before comparison. The dither carries
   colour in the spatial mix of pure primaries, so per-pixel comparison of a dithered frame measures
   every pixel as fully saturated and tells you nothing.

## Validation

`python -m tools.eink_measure selftest` synthesises photographs with a *known* perspective warp,
camera colour distortion and noise, and requires the pipeline to recover the truth — worst ink error
3.4/255 at 4% warp. Built this way deliberately, before the camera existed.

It earned its keep immediately: the first corner finder took the extremes of the dark mask, which is
correct only if the registration frame is the darkest outermost thing — and a real panel has a **dark
bezel**. The synthetic photos include one, so every case failed loudly instead of silently
mis-registering against hardware later. `tests/test_eink_measure.py` guards it.

## Instrument accuracy — measured, not assumed

The honest way to characterise this rig is to photograph the SAME ink in two different places on the
panel and see whether it measures the same. The primaries target has each ink twice: once as a large
content cell, once in the calibration strip.

```
white:  253.2  vs  250.2     agreement within  3 of 255  (1.2%)
black:   17.2  vs   41.9     disagreement of  32 of 255  (12.6%)
```

**The rig is precise on bright tones and unreliable on dark ones**, and the cause is physical.
Ambient light reflecting off the panel's glass adds a veiling glare that sits on top of dark ink.
Flat-field division removes a MULTIPLICATIVE gradient; veiling glare is ADDITIVE, so it survives.
It is also why exposure hits a ceiling: the panel's black is simply not very black under room light,
so the whole black-to-white span lands in ~85 of 255 levels and the correction has to stretch it ~3x,
amplifying every residual along with the signal.

⚠️ **Blue and green are dark inks.** The measurements the colour work most needs are the ones this
rig is currently worst at. Treat dark-ink numbers as indicative, not settled.

**The fix is a hood**, not more processing: shield the panel and lens from stray ambient so the only
light reaching the glass is the light you chose. That raises contrast at the dark end, where all the
uncertainty lives.

## Known-open

- **Corner detection finds the panel bezel, not the registration frame's inner edge.** On a real
  photograph the rectified image includes the Pimoroni silkscreen and the flex cable, so patch
  rectangles straddle boundaries. This is the next fix.
- **A ceiling fan was running during early captures.** It swept shadows across the panel between
  frames, which is why `--settle` sometimes never converged. Anything moving overhead invalidates a
  flat-field reference by baking a transient into it. Turn it off.
- **The lighting gradient caps dynamic range.** Above exposure 1000 the brightest part of the panel
  clips while the dimmest is still at ~215, so ~7% of the panel saturates before white is full scale.
  Flat-field cannot recover clipped data — this is a lighting fix, not a software one.
- **`sd-eink` holds the panel's SPI/GPIO lines** (`Restart=always`, 10 s). Stop it for a session and
  restart it afterwards, or renders fail at the push with "some pins we need are in use".
