# E-Ink Spectra 6 (EL133UF1) — Render Calibration

> **Status:** ✅ Bench-validated on real hardware, 2026-07-19 — **but §4 is RETIRED (ADR-098,
> 2026-08-29).** Recipe lives in `epaper.py` (`SPECTRA6_DITHER_PALETTE` / `SPECTRA6_OUTPUT_PALETTE` /
> `SPECTRA6_WHITE_POINT` / `SPECTRA6_GAMMA` / `_tone_lut`, under `palette="spectra6"` in
> `render_for_epaper`). Decision records: `.ai/decision_log.md` ADR-053, **ADR-093, ADR-098**.
> Product spec for the client/topology this feeds: `.ai/spec_eink_spectra6.md`.

## The panel

**Pimoroni Inky Impression 13.3"**, panel **EL133UF1** (E Ink Spectra 6 / E6, 6-colour: black, white,
yellow, red, blue, green). Bench unit: Raspberry Pi 5 + `inky` 2.4.0.

- Auto-detects at **1600×1200** ("multi" variant).
- Full panel refresh: **~9s** — much faster than the datasheet's 20–35s estimate. No ghosting observed.
- **Peel the protective film before judging colour** — it scatters light and dulls everything, and can
  read as a calibration problem when it isn't one.

## Why the nominal palette wasn't enough

`epaper.py` originally dithered `spectra6` output toward *nominal* near-pure sRGB anchors (e.g. red
`(191,0,0)`). Against real glass those anchors are more saturated than the panel can physically
reproduce. Two visible failures resulted:

1. **Heavy dither grain** — Floyd–Steinberg diffuses error trying to average toward a target the palette
   can't hit, so it never converges and the output looks noisy.
2. **Reds drifting orange** — the dither borrows yellow to approximate a red more saturated than the
   panel's actual red ink, contaminating the result.

## The recipe (locked, except gamma)

`spectra6 = white-point 0.75 · gamma 1.0 · dither to measured real-primaries · Floyd–Steinberg · re-encode to pure-primary output`

⚠️ **The gamma pre-pulldown in §4 below is RETIRED (ADR-098).** It is kept as a record of how the
recipe was reached, not as a description of what ships.

### 1. Dither toward the panel's measured primaries

Use inky's own `EL133UF1` `SATURATED_PALETTE` — the panel's actual achievable colours — as the dither
target, not idealized sRGB. Order: black, white, red, yellow, blue, green.

| Colour | RGB (measured) |
|---|---|
| Black | `(0, 0, 0)` |
| White | `(161, 164, 165)` — physically a **grey**; highlights can't beat this |
| Red | `(156, 72, 75)` |
| Yellow | `(208, 190, 71)` |
| Blue | `(61, 59, 94)` |
| Green | `(58, 91, 70)` |

Saturation stays at **1.0** (dither straight to these values, no further desaturation). Josh's read on
trying a lower saturation: "paying reds for whites" — not worth it; keep the reds.

### 2. Re-encode the output to pure primaries

The dithered `P`-mode image (still on the measured-primary palette above) is re-encoded — same pixel
*indices*, new palette values — to pure primaries before being served:

| Colour | RGB (output) |
|---|---|
| Black | `(0, 0, 0)` |
| White | `(255, 255, 255)` |
| Red | `(255, 0, 0)` |
| Yellow | `(255, 255, 0)` |
| Blue | `(0, 0, 255)` |
| Green | `(0, 255, 0)` |

**Why this matters:** an `inky` client's `set_image()` does its own internal re-quantize against its
palette. Feed it the *muted* measured values and its re-quantize snaps the muted blue/green
(`(61,59,94)` / `(58,91,70)`) to **black** — they're closer to black than to inky's idea of blue/green.
Feeding it pure primaries at the same indices round-trips correctly through any client's re-quantize,
inky or otherwise.

### 3. Universal by construction

The dithering and re-encoding both happen **server-side**, once, in `render_for_epaper`. Any client —
`InkyClient`, a bare Waveshare/ESP32 firmware, a TRMNL in BYOS mode — just fetches the finished bitmap
and blits it. No `inky` library or per-device colour math needed on the client.

**Validated on the panel:** the server-dithered "universal" path was judged visually indistinguishable
from — arguably better than — `inky`'s own native dithering path. Same quality, works everywhere.

### 4. ~~Wash-adaptive gamma (highlight pulldown)~~ — 🔴 RETIRED 2026-08-29 (ADR-098)

**What ships now is a constant:** white-point **0.75**, gamma **1.0**, applied as one LUT by
`epaper._tone_lut()` before the dither. `_adaptive_gamma` is gone from `epaper.py`; it survives only as
`tools/eink_calibrate.legacy_adaptive_gamma()` so historical baselines still reproduce.

**Why it was retired — two independent condemnations:**

| finding | number |
|---|---|
| ADR-081 — worse than a plain constant, cross-validated | R² = **−3.137** |
| ADR-094 — the WORST option measured on dark paintings | picks γ 1.40 on The Night Watch → **85.0%** of the shadow region as bare black ink, against **67.4%** for *no correction at all* |

Measured effect of the swap (bare-black fraction of the sub-luminance-60 region, 700 px renders):

```
                 γ(old)   OLD      NEW (wp 0.75)   delta
The Night Watch   1.40    84.4%       74.2%        -10.2
Olympia           1.40    82.8%       71.1%        -11.7
Sunflowers        1.40    68.4%       54.3%        -14.1
```

⚠️ **0.75 is INTERIM and LABEL-DERIVED** (ADR-093, 23 three-level human judgements). The physics puts
the media-relative value at `Y_white^(1/2.4)` = **0.660** (naive palette ratio 163.3/255 = 0.641), and
says the exact transform is a **curve** — ~0.37 in the shadows rising to 0.64 at the top — not a scale.
The gap to the human mean of 0.73–0.80 is a one-parameter preference residual. The physics-first model
re-decides this.

**A measured cost of compression, previously unrecorded:** a flat saturated yellow (230,230,20) renders
**100% yellow ink** with no correction but **76.7% yellow / 23.3% green** at wp 0.75 — compression
knocks it off its own ink. Green is nowhere near a saturated yellow perceptually; it wins only on the
quantiser's naive *gamma-encoded* RGB distance. That is a symptom of the linearisation defect, not of
the white point.

**The original reasoning, kept for the record.** The panel's single light ink is physically grey, not
white, so bright/flat regions lose structure at gamma 1.0 ("wash"). The amount of pulldown needed did
not track overall brightness — a flat pale woodblock print needed more than an equally-bright chromatic
painting — so gamma was keyed on the *wash* fraction (bright AND low-chroma):

```
wash_pct = % of pixels where L > 0.80·255  AND  chroma < 40   (chroma = max(R,G,B) − min(R,G,B))
gamma    = 1.4 + 0.1 · clamp((wash_pct − 10) / 15, 0, 1)      # 1.4 at ≤10%, 1.5 at ≥25%
```

📏 **Why it failed is worth keeping.** Its whole output range was 0.1 wide (1.4–1.5), it keyed on one
feature with four hand-set constants, and the direction it moved — γ > 1 darkens — is the wrong
direction for the end that was actually starved. Its known confound (skin-heavy bright pieces wanted
*less* pulldown, since darkening flesh nudges it toward the brick-red ink) was parked, and is now moot.

## Bench findings — per-piece preferred gamma (saturation 1.0)

Bench-swept on 9 pieces via `eink_palette_test` / `eink_render_check` harnesses, judged live on the panel.

| Piece | Character | Plain γ1.0 | Best γ | Notes |
|---|---|---|---|---|
| Nighthawks | dark, red/yellow | good | **1.4** | 1.4 beats 1.0 even though it's already dark (shadows + reds improve); 1.5 crushes shadows |
| Starry Night | dark, blue/yellow | good | ~1.4* | not γ-swept directly; dark tone → expected ~1.4 |
| Boating Party | bright, skin + fine background | too washed | **1.4** | 1.0 worst (wash); 1.5 pushes skin toward orange; 1.3 kindest to skin; 1.4 best overall balance |
| Water Lilies | high-key, bright lily pads | washed | **≥1.4** | "wayyyy better" than 1.0; 1.4 vs 1.5 was the closest call of the set |
| Great Wave (Kanagawa) | high-key woodblock print | badly washed | **1.5** | the only piece that clearly wanted 1.5 — recovers the cartouche outline + peach sky |
| Sunflowers | very high-key, yellow-on-yellow | washed/flat | **1.4** | gamma restores form/greens/golds; 1.4 beat 1.5 even at near-Great-Wave brightness |

\* inferred from character, not directly γ-swept on the bench.

**Read:** γ1.4 carries almost everything, including very-high-key Sunflowers. Great Wave — a flat, pale
woodblock print with fine cartouche linework — was the lone outlier wanting 1.5. The wash metric (not
brightness) explains why: Great Wave measured **25.2% wash** against everything else at **≤7.7%** (mostly
0%), despite Sunflowers being nearly as bright by mean luminance (61 vs. 65). That's what the wash-adaptive
rule above is built from.

## Hardware validation log (2026-07-19)

- **Direct path:** production `render_for_epaper` (calibrated) → `inky.set_image` on the bench Pi.
  Confirmed the pure-primary `P`-mode output round-trips through inky's `set_image` re-quantize correctly.
- **Mode B (satellite), full production path:** laptop app (`:8080`) `render_for_epaper` → `sd-eink`
  (systemd service on the Pi, httpx poll) → `InkyClient` → panel, cycling the playlist every ~30s. Josh:
  "it painted and is swapping." Every displayed frame is the tuned pipeline end to end — the server
  renders, the client only blits, confirming the universal-client design holds in practice.
- **Robustness fixes landed alongside the render work** (same bench session, same branch
  `feat/eink-client`):
  - `sd-eink` didn't release its GPIO claim on abrupt death (e.g. ssh parent dying mid-bench-run) —
    "pins in use, claimed by inky" blocked a second instance from starting. Fixed with a clean
    shutdown/atexit release on SIGTERM so `Restart=always` can re-acquire cleanly. (Note for future
    bench runs: `timeout ssh ...` does not propagate SIGTERM to the remote process — use a remote-side
    timeout instead.)
  - `sd-eink`/`eink_client` logged through a logger with no handler, so a live run printed nothing.
    Fixed with `logging.basicConfig` so `journalctl -u sd-eink` shows poll/paint/dedupe activity.
  - Heartbeat file default (`/run/sd-eink.state`) was unwritable by the non-root `pi` user — moved under
    a systemd `RuntimeDirectory`.

## Open follow-ups

1. **Mode A (all-in-one)** — not yet bench-tested. The bench Pi was mis-flashed with Debian 13 trixie
   (Python 3.13) instead of the targeted Raspberry Pi OS Bookworm (Debian 12 / Python 3.11); all-in-one
   can't run bare on trixie (`pillow-heif` needs `libheif`; legacy `wikipedia==1.4.0`'s `setup.py` fails
   on py3.13 — both hard top-level imports). Docker/the appliance image sidesteps this (bundles the
   matched py3.11 environment), but a bare-metal Mode A bench test needs a reimage to Bookworm first.
   This is an OS/dependency issue only — the render calibration in this doc is panel-specific, not
   OS-specific, and carries over unchanged after the reimage. All-in-one must work for **both** e-ink and
   LCD/headless outputs; only the co-located server needs the matched environment — the e-ink client
   (`sd-eink`) itself already runs fine on either OS.
2. **Soak** — cycle a full day once Mode A is up; watch for ghosting, thermal behavior, long-run
   stability.
3. **Framing/focal.** The panel is 1600×1200 landscape; `render_for_epaper`'s `fit=cover` crops to that
   4:3-ish aspect, so portrait-oriented works lose their top/bottom — a bench portrait had its subject's
   head cropped by a poorly-tuned focal point. The bench tuning harnesses (`eink_palette_test` /
   `eink_render_check`) also hardcoded `focal=(0.5, 0.5)` for testing convenience; the real production
   endpoint already passes each work's DB `focal_x`/`focal_y`. Levers to fix the underlying crop problem:
   (a) better focal-point data for tight crops, (b) `cover` vs. `contain` per orientation, (c) a
   **portrait** panel orientation for portrait-heavy playlists — `eink_client` already supports
   `EINK_ORIENTATION=portrait`; decide per-frame orientation at install time.
4. **Deep per-piece render profiles (parked).** Wash-adaptive gamma is the launch default. A richer
   per-work adaptation could key off metadata (IIIF manifest fields, catalog `medium`/`date`/
   `resolution_tier`) or a one-time Sonnet/deep-research pass reviewing each piece and baking a
   `render_gamma`/`render_profile` hint into the pack manifest — cheap at seed time, zero per-display
   cost. The one known confound this would need to solve: skin-heavy bright pieces want less pulldown
   than the wash metric alone predicts.
5. **Portability.** The mechanism here (measure real primaries → dither toward them → re-encode to pure
   primaries for the client) generalizes to other e-ink families (ACeP 7-colour, mono greyscale); the
   specific palette values and gamma rule in this doc are EL133UF1-specific and would need their own
   bench pass on different hardware.

---

## Panel orientation — which way is "portrait"? (bench, 2026-07-21)

`EINK_ORIENTATION=portrait` tells `eink_client` to compose at `h × w` and rotate back onto the panel's
native landscape buffer at paint time. But *which* 90° turn is a physical question about how the panel
is mounted, and it isn't discoverable from software.

**For the Pimoroni Inky Impression 13.3" (EL133UF1): `90` is the correct portrait setting.** The
practical check on the bench: **at 90 the silkscreened text on the PCB reads the right way up.** Use
that as the orientation reference rather than guessing from the ribbon cable or the connector side.

Validated end-to-end in the first-run wizard: choosing "Portrait — rotated 90°" writes `ROTATE=90`
(wlroots/HDMI) *and* `EINK_ORIENTATION=portrait`, and the panel painted portrait-composed art in
production — not a spun landscape frame.

⚠ **Unverified on the raw Waveshare equivalent.** It very likely matches (same EL133UF1 glass), but the
carrier board differs, so treat `90` as confirmed-for-Pimoroni and re-check on first contact with other
hardware. If a future panel disagrees, the fix belongs in the wizard's orientation labels, not in
`eink_client` — the compose-then-rotate rule is panel-independent.
