# E-ink gamma calibration — bench session, 2026-08-01

Record of the first real labelling session against the 13.3" Spectra 6 panel (ADR-079). Written so the
fit can be reproduced, audited, or rejected later — a calibration without its viewing conditions and
its judging criteria is not reproducible, and the judgement is the expensive part.

## Viewing conditions (part of the measurement, not a footnote)

E-ink is **reflective**: it has no backlight, so the illuminant is part of the instrument. Judgements
below are only valid under conditions comparable to these.

| | |
|---|---|
| Illuminant | WiZ tunable-white bulb, **5000K**, **50%** brightness, in a lamp above the panel |
| Mode | CCT white mode, **not** RGB colour mode (RGB has large spectral gaps and would make the six primaries reflect non-uniformly) |
| Ambient | Curtains closed — no daylight, which drifts over a session |
| Distance | ~1–1.5 m, held constant (dither speckle integrates with distance) |
| Panel | Pimoroni Inky, 1600x1200, `inky` 2.4.0, via SPI on the bench Pi |

5000K because ISO 3664 specifies D50 for judging **reflective** media, and e-ink is far closer to a
print than to a display. For a *gamma* fit the CCT is second-order anyway; holding it constant is what
matters. Brightness was deliberately set to a normal living-room level rather than cranked: calibrating
under a bright lamp fits a gamma that is too low and washes out in a real room.

## Method

`tools/eink_bench.py` — corpus frozen once, then `show N` / `record N LETTER` per image, driven
remotely over SSH while the judge stood at the panel calling letters. Contact sheet = 3x2 cells,
production-fidelity dither, standard grid **γ1.2 / 1.5 / 1.8 / 2.1 / 2.4 / 2.7**.

Corpus: 30 images by greedy farthest-point selection over a per-collection stratified sample of the
**full installed library** (28 collections, 2857 works, 16.0 GB). This mattered — the dev laptop's
library is 122 works and 67% one collection, which would have reproduced exactly the narrow-corpus
blind spot ADR-079 blames for the incumbent.

## Findings

### 1. The incumbent `_adaptive_gamma` is wrong in principle, not merely mistuned

```
INCUMBENT:            R² = -2.600     MAE = 0.617     output range 1.4 .. 1.5
fitted (3 features):  R² =  0.48      MAE = 0.226     judged range 1.2 .. 3.0
```

A **negative R² means it does worse than predicting the mean of the labels every time.** Worse, its
sole predictor `wash_pct` proved the *worst* of the six candidates (leave-one-out MAE 0.333 vs 0.284
for chroma or luminance alone), and its fitted coefficient is **negative** — high wash wants LESS
pulldown, the opposite of the incumbent's premise. Wrong variable, wrong sign, far too narrow a range.

### 2. Six features overfit at n=30 — ship three

```
leave-one-out MAE (grid step = 0.30):
  0.297   6 features (the current FEATURES tuple)
  0.270   4: wash_pct, mean_lum, mean_chroma, edge_pct
  0.266   3: mean_lum, mean_chroma, edge_pct        <- best
  0.284   1: mean_chroma alone
```

Adding features made it worse. One feature gets within 0.02 of the best, so most of the signal is in
chroma. More features only become supportable with more labels.

### 3. Judge repeatability is 0.18 — so the gap is model error, not noise

Six already-judged sheets were re-shown in shuffled order, without revealing the original answers:

```
ring-nebula          1.20 -> 1.20   +0.00
world-map (ext grid) 3.00 -> 3.00   +0.00
flaming-june         2.40 -> 2.10   -0.30
hunefer              2.10 -> 1.80   -0.30
bronze-figure        1.50 -> 1.80   +0.30
macgillivray-finch   1.80 -> 2.40   +0.60   (judged before the cream rule, see below)

MAE excluding the finch = 0.180      signed mean = +0.05 (no bias)
```

Model error (0.266) sits **above** the human noise floor (0.180), so there is real structure still
uncaptured and more labelling is worth the panel time. Had the model landed at ~0.18 it would already
be as good as the data allows, and the answer would instead be better features or a different form.

### 4. "Yellowing" on aged-paper plates is the PAPER, not a dither artifact

The judge flagged a yellow cast appearing in pale backgrounds as gamma rose. Measured directly:

```
botanical lily   highlight mean RGB = (235, 230, 185)   R-B = +50   <- cream stock
Lange photograph highlight mean RGB = (254, 254, 254)   R-B =  +0   <- neutral, and showed NO cast
```

The cast only appears where the source carries chroma. Low gamma clips those near-whites to the
panel's white ink and **discards** the paper tone; higher gamma drops them into the dither's range
where they render correctly. A "neutral highlight guard" in the quantise step — the obvious first fix —
would have actively destroyed the character of every antique print in the catalog.

**Decision: render faithfully.** Aged paper reads as aged paper. Normalising it would be a separate
white-point step, not a gamma change.

### 5. The grid ceiling was clipping

The engraved world map picked F (2.7), the top cell. An extended **2.4–3.9** grid was shown and **3.0**
won — so 2.7 had been a censored observation. Any future session must treat a boundary pick as a signal
to widen the grid, not as an answer.

### 6. Contact sheets under-represent fine detail

Each tile is 1/6 of the panel, so a 5230px master is judged at ~10x downscale — roughly 3x lower linear
resolution than production. "Pale Blue Dot" was unjudgeable for its subject (the dot averages away
before the dither runs; framing was verified fine, the crop keeps 93% of width). This likely biases the
whole session **toward higher gamma**, pushing pulldown to recover detail that would have been legible
at full size. **Finalists must be verified with full-panel renders before coefficients are committed.**

### 7. A mid-session criterion change contaminated the early labels

The faithful-cream rule was settled at sheet 3, so sheets 1–2 were judged under a different rule. The
retest is a clean natural experiment: the paper-dominated finch moved **+0.60** (two grid steps) while
the paperless nebula reproduced **exactly**. Sheet 1's label was replaced with the retest value; sheet
2's was left alone. Lesson: settle the judging criteria before the first sheet, not during.

## Conflicts a single gamma cannot resolve (recorded for follow-up)

1. **Shadow recesses crush before highlights are optimal** (Rodin, *The Kiss*) — the gap between the
   figures lost readability well below the best overall setting.
2. **Hue desaturates as gamma rises** (Leighton, *Flaming June*) — the orange visibly weakened at the
   settings that best separated the drapery folds. This is what server-side saturation is for; it should
   be fitted jointly with gamma, not left at a fixed 1.0.

## Provenance

- `bench-eink/labels.jsonl` — the judgements (sheet 1 replaced, four retested rows averaged).
- `bench-eink/corpus.json` — the frozen corpus: which images, in what order, with what features.
- `bench-eink/retest.jsonl` — the raw repeatability re-readings, before averaging.
