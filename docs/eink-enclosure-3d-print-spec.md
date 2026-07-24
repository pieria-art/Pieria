# Pieria — E‑Ink Art Frame: 3D‑Print Enclosure Spec

> **Status:** DRAFT v0.1 — awaiting answers from the printer owner (see [§10 Open Questions](#10-open-questions-fill-in-with-your-friends-answers)).
> **Audience:** someone who prints their own stuff and speaks maker.
> **Goal:** a shadow‑box‑depth wall enclosure for the Waveshare 13.3" Spectra‑6 e‑paper panel, sized so the controller can be a **Pi Zero 2 W** *or* a **Pi 4 / Pi 5 + active cooler** — because the Zero 2 W is chronically out of stock and we don't want the enclosure to lock us to it.

---

## 1. Design intent
- **Beauty appliance, not a dashboard.** This is wall art. The front face is the product; the electronics hide in a genuine gallery **shadow box**.
- **Pi‑agnostic depth.** Design to the *deepest* case (Pi 5 + active cooler). Smaller boards (Pi 4, Zero 2 W) just mount on a shim/riser inside the same box.
- **Two‑part architecture:** a **front bezel** that fits the panel precisely (borrow FrameOS's proven geometry) + a **custom deep back box** (the new work).

## 2. The panel it wraps
Waveshare **13.3" e‑Paper HAT+ (E)**, Spectra‑6 / E6 color — **SKU 29355** (bundle incl. driver HAT + FPC cable).

| Dimension | Value | Notes |
|---|---|---|
| Glass outline | **284.7 × 208.8 × 0.85 mm** | It's **glass** — fragile, don't clamp |
| Active / viewable area | **270.4 × 202.8 mm** | This is the aperture / mat window |
| Driver HAT board | **65 × 30.5 mm**, ~**9 mm** tall | Thickest front‑side component |
| Glass ↔ driver link | **FPC ribbon** | Delicate; needs strain relief |
| Controller attach | driver HAT sits on the **40‑pin GPIO** | Pi stacks directly behind the HAT |

## 3. Base plans (the link)
**FrameOS "Case Maker" — https://cases.frameos.net/**
Build guide / rationale — https://frameos.net/blog/eink-spectra-waveshare-pimoroni/

Web‑based **parametric generator**: pick the 13.3" Waveshare template → tune parameters → render → download STL. It gets the **front‑side fitment** right (glass seat, HAT pocket, `M2×4×3.5` heat‑set boss pattern, cooling holes, Pi‑retention pins).

**The catch:** its depth presets are only **6 / 9 / 12 mm**:
- **6 mm** — slimmest, requires janky soldering.
- **9 mm** — 13.3" Waveshare + Pi Zero 2 W on **right‑angle GPIO headers**.
- **12 mm** — Pimoroni panels with an attached Pi.

That's fine for a Zero 2 W, **nowhere near a Pi 5 + cooler**. So we use FrameOS for the **front bezel only** and mate a **custom deep back**.

## 4. Architecture
1. **Front bezel** — from the Case Maker (deepest template) *or* a clone of just its front face: glass rabbet + FPC slot + driver‑HAT pocket. This part must be **dimensionally exact** to the panel — do **not** reinvent it.
2. **Custom deep back box** — the new work. A shadow‑box "belly" that mates to the bezel perimeter and houses whichever Pi on a **swappable sled**.

## 5. Depth budget (why "shadow‑box thick")
Front‑to‑back stack, **Pi 5 + active cooler (worst case):**

```
glass          0.85
foam gasket   ~2
driver HAT    ~9
40‑pin header ~8.5
Pi PCB         1.6
Pi 5 cooler   ~18
rear clearance ~3
────────────────────
             ≈ 43 mm
```

→ **Target interior depth ≈ 40–45 mm.** That comfortably fits:
- Pi 5 + active cooler — tight
- Pi 4 + heatsink — ~28 mm, roomy
- Zero 2 W — ~12 mm, mount on a riser

Reads as a real gallery shadow box, not a slab.

> **Note:** you won't use a *separate* Pi case — **this enclosure IS the case.** The Pi mounts to an internal sled. Sizing as if for a cased Pi just buys margin.

## 6. ⚠ The #1 print gotcha: bed size
Frame footprint is **~285 × 210 mm** (larger with a bezel margin → ~300 × 230 mm). **This exceeds most beds** — Ender 220², Bambu / Prusa MK 256². Options, best first:

1. **Split into 4 corner sections or 2 halves**, joined with **alignment pins + M3 bolts** (or dovetails + CA glue). Do this on **both** the bezel and the back box; register seams off the panel corners.
2. **Print one‑piece on a large‑format bed** (Prusa XL 360², or any 300×300+). Cleanest if available.
3. **Ask the printer owner their bed size FIRST** — it decides 1 part vs 4. (See §10.)

## 7. Back‑box feature checklist (the custom part)
- **Swappable Pi sled** — a flat insert with **both** hole patterns so one box fits all:
  - Zero 2 W = **58 × 23 mm**
  - Pi 4 / Pi 5 = **58 × 49 mm**
  - all **M2.5**; use **M2.5 brass heat‑set inserts**, not printed threads.
  - Sled screws to standoffs in the belly so board height is shimmable.
- **Cooling for the Pi 5 active cooler** — **intake grille** near the fan + **exhaust** on the opposite wall. Don't seal a Pi 5 in solid plastic or it throttles. (Zero / passive Pi 4 don't need it, but design it in.)
- **Port cutouts** — **USB‑C power** on the **bottom edge** (hidden), ~0.5 mm clearance. Optional micro‑HDMI + USB slots for setup, or just make the back removable for that.
- **FPC strain relief** — a **radiused channel + printed clamp bar** where the ribbon bends glass→driver. A yanked FPC kills the (fragile, glass) panel.
- **Glass support, not clamp** — bezel seats the glass on a **continuous lip** with a **thin foam / EVA gasket** behind it. Never point‑load or clamp glass. Rear capture with soft standoffs only.
- **Removable back panel** — **M2.5 / M3 heat‑set + screws** (matches FrameOS's back‑cover approach) or **embedded magnets** for tool‑free access.
- **Wall mount** — **French cleat** cast into the back (best for a heavier deep box) or **keyhole slots**; center on the CG.

## 8. Print settings (maker defaults)
- **Filament:**
  - **Back box → PETG** — heat tolerance next to a Pi 5, better dimensional stability on a big flat part.
  - **Front bezel → matte PLA** is fine (looks, no heat there). **ASA** if it'll ever see sun/heat.
- **Walls:** 3–4 perimeters (a 285 mm flat part wants rigidity). **Infill** 20–25% gyroid.
- **Warp control:** big flat footprint = warp risk → **brim / mouse‑ears**, enclosure if ABS/ASA, or lean on the split‑part plan (smaller parts warp less).
- **Orientation:** bezel **face‑down** for a clean visible front (supports only in the FPC slot); back box **open‑face‑down** to avoid interior supports.
- **Heat‑set inserts** wherever threads meet screws (iron + inserts; don't trust printed threads in PETG).
- **Tolerances:** 0.15–0.20 mm clearance on the glass pocket and port cutouts. **Test‑print one bezel corner** to verify panel fit *before* the full run (FrameOS says the same: "always measure your components").

## 9. Bill of enclosure hardware (beyond the print)
- M2.5 brass heat‑set inserts (Pi mount) + M2.5 screws
- M2×4×3.5 heat‑set inserts (back cover, per FrameOS) or M3 + inserts
- Thin EVA / craft foam for the glass gasket
- French‑cleat hardware or keyhole‑mount screws
- (optional) small neodymium magnets if going tool‑free back

## 10. Open questions (fill in with your friend's answers)
1. **Bed size?** → decides 1‑piece vs split‑into‑4. `____________`
2. **Filament on hand** — PETG available, or PLA/PETG only? `____________`
3. **Heat‑set inserts + soldering iron** available? `____________`
4. Comfortable with a **parametric source** (so we tune depth per actual Pi), or **STL‑only**? `____________`
5. Any max **single‑part** dimension / print‑time preference? `____________`

## 11. If your friend prefers editable CAD over the FrameOS web tool
We can spec the same enclosure as **OpenSCAD parameters** — panel dims baked in as constants + a single `pi_depth` variable so switching Zero 2 W → Pi 4 → Pi 5 is a one‑line change. Say the word and I'll write the `.scad`.

---

*Panel dims from Waveshare SKU 29355 datasheet (parts list). Base geometry & depth presets from FrameOS Case Maker (cases.frameos.net) and its build blog. Depth budget is a worst‑case Pi‑5‑+‑cooler estimate — verify against the actual cooler before committing.*
