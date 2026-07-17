# Screen Docent — Hardware Profile & Sizing

> What Screen Docent actually needs to run, by output type — derived from a real soak, not guesswork.
> Screen Docent is **hardware-agnostic**: it runs on whatever panel + small computer you point at it.
> These are *validated recommendations*, not requirements.

## TL;DR — pick by output

| Config | What it drives | RAM (min / rec) | Cores | Storage | Board (options) |
|---|---|---|---|---|---|
| **LCD / TV all-in-one** | a TV or monitor via the browser Canvas (Ken Burns motion) | **4 GB / 8 GB** | 4 | **64 GB+** | Pi 5 or Pi 4 |
| **E-ink all-in-one** | an e-ink panel (still-render), server on the same box | **2 GB / 4 GB** | 2–4 | 32 GB+ | Pi 4 / Pi 5 (+ the e-ink HAT) |
| **E-ink client (satellite)** | an e-ink panel only; fetches from a hub elsewhere | **512 MB / 1 GB** | any | 8 GB+ | Pi Zero 2 W |

**The working set is the browser.** On the LCD/TV config, Chromium is the dominant consumer (~1 GB, more on
a 4K canvas) — that's why the TV tier wants more RAM and the e-ink tiers (no browser) run markedly lighter.

## Evidence — 10-day soak (LCD all-in-one, reference unit)

A reference all-in-one Pi driving a 4K TV in portrait, over ~10 days continuous:

| Metric | Result | Meaning |
|---|---|---|
| RAM (of 8 GB) | peak **4.66 GB** / avg 2.7 GB; **swap never touched** | 4 GB is a *tight floor*; 8 GB comfortable |
| Browser RSS | avg **1.04 GB**, flat over 10 days | **no memory leak** — stable for months-long unattended runs |
| CPU temp | avg 52 °C, **peak 59.5 °C** | ~25 °C below throttle — active cooling is ample |
| CPU load | avg ~29%, brief peaks | never saturated on 4 cores |
| Disk | ~12 GB used | + the bundled art-pack (see below) |

**Storage note:** the offline art-pack (the bundled museum collection) is **~16–25 GB** baked into the
image, so **64 GB+ is the practical floor** for an all-in-one that ships with art. E-ink client/satellite
units hold no pack (they fetch), so they need almost nothing.

**Cooling:** an active cooler keeps the reference unit ~25 °C under throttle. A passive heatsink is likely
fine in a cool room, but ship active cooling for margin (esp. inside a sealed enclosure).

## Cost (⚠ 2026 prices are volatile — memory-driven)

Board prices **rose sharply through 2026** on LPDDR memory costs — the old "$35 Pi" framing no longer holds
for the RAM this needs. Approximate US retail (raspberrypi.com, ~April 2026 — **re-check before quoting**):

| Board | ~Price | Fits |
|---|---|---|
| Pi 4 — 2 GB / 4 GB / 8 GB | $55 / $105 / $165 | e-ink all-in-one (2 GB); LCD (4–8 GB) — often the value pick |
| Pi 5 — 2 GB / 4 GB / 8 GB / 16 GB | $65 / $110 / $175 / $305 | LCD all-in-one (better 4K/Chromium headroom) |
| Pi Zero 2 W | ~$15 (chronically out of stock) | e-ink client / satellite |

**Takeaways for positioning + BOM:**
- The **recommended LCD all-in-one (8 GB) is now a ~$165–175 board**, not a throwaway — update any
  "poor-man's Frame TV / $35 Pi" copy accordingly. The value prop is *no subscription + can't be bricked +
  any panel*, not *dirt-cheap hardware*.
- **Pi 4 is often the value pick** at equal RAM (e.g. 8 GB: Pi 4 $165 vs Pi 5 $175) and is adequate for the
  workload; reach for Pi 5 mainly for smoother 4K Canvas.
- **E-ink tiers get cheaper** as memory drives price: no browser → low-RAM boards suffice, so the e-ink
  BOM is dominated by the *panel*, not the compute.
- **Pi Zero 2 W supply is the real constraint** for battery/satellite e-ink frames (price stable, stock
  isn't) — bench-test the satellite role on a Pi 5 stand-in until supply normalizes.

---
*Sizing derived from a 10-day production soak (2026-07). Prices are point-in-time and volatile; verify at
purchase. See `.ai/spec_eink_spectra6.md` for the e-ink client design and `docs/eink-enclosure-3d-print-spec.md`
for the frame enclosure.*
