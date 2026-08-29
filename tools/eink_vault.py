"""
tools/eink_vault.py — drive the panel-characterisation battery and bank the evidence
(maintainer tool — NOT part of the runtime image).

Renders each target on the Pi, waits for the panel, photographs it, reads it back, and appends one
record per condition to bench-eink/panel_profile.jsonl — plus a rectified, corrected JPEG per capture
so the panel's actual appearance can be LOOKED AT later, not only read as numbers.

    python -m tools.eink_vault run --flat bench-eink/reference/flat.png
    python -m tools.eink_vault run --flat ... --only inkmix,primaries
    python -m tools.eink_vault dwell --flat ... --target tonefine
    python -m tools.eink_vault ghost --flat ...

⚠️ PACING, NOT A WEAR BUDGET. This tool used to be told that "colour e-ink has finite refresh
cycles". That was unsourced: E Ink publishes NO lifetime, endurance, MTBF or update-count rating for
the EL133UF1. What vendors do state is an INTERVAL and a DWELL — refresh no more often than every
180 s, refresh at least once every 24 h, do not leave a static image up indefinitely. Pimoroni gives
a real-world refresh cycle of 20-35 s, which matches the ~22 s measured here. So `--pace` is the
control that matters, and it is measured from the moment the render command is ISSUED, not as a
trailing sleep.

⚠️ RESUME, NEVER RESTART. Every row already in the profile is skipped, and one bad capture is caught
and recorded rather than ending the run.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import eink_calibrate as ec  # noqa: E402
from tools import eink_measure as em  # noqa: E402
from tools import eink_readout as er  # noqa: E402
from tools import eink_target as et  # noqa: E402

OUT = Path("bench-eink")
PROFILE = OUT / "panel_profile.jsonl"
VAULT = OUT / "vault"
PANEL = (1600, 1200)

#: Bumped whenever the target furniture moves (content_box, fiducials, patch strip). Captures taken
#: under different geometry are NOT comparable, and a silent comparison across a geometry change is
#: exactly the kind of error this file exists to prevent.
GEOMETRY_VERSION = 3


def _lever_row(kind, readout, wp=0.0, gamma=1.0, chroma=1.0, sat=1.0, floor_max=None,
               isolate=False):
    """One condition = one point in lever space. The name encodes every lever so resume is exact."""
    flags = ["--gamma", str(gamma)]
    if wp > 0:
        flags += ["--white-point", str(wp)]
    if abs(chroma - 1.0) > 1e-3:
        flags += ["--chroma-gamma", str(chroma)]
    if sat is not None and abs(sat - 1.0) > 1e-3:
        flags += ["--saturation", str(sat)]
    if floor_max is not None:
        flags += ["--chroma-floor-max", str(floor_max)]
    if isolate:
        flags += ["--isolate"]
    cond = (f"{kind}_wp{wp}_g{gamma}_k{chroma}_s{sat}"
            + (f"_hf{floor_max}" if floor_max is not None else "")
            + ("_iso" if isolate else ""))
    return {"cond": cond, "kind": kind, "flags": flags, "readout": readout}


def _rows(kinds=None) -> list:
    """The capture matrix: panel invariants once, then a FACTORIAL over the render levers.

    Two questions need different designs and this covers both:

      * MAIN EFFECTS — what does each lever do on its own. A one-at-a-time sweep answers that.
      * INTERACTIONS — does one lever change what another lever does. Only a crossed design answers
        that, and it is the question a one-at-a-time sweep silently cannot see. ADR-088's chroma work
        failed partly because chroma was swept while luminance, the binding constraint, was held at a
        value that made chroma look irrelevant.

    The crossing is chosen per target rather than run everywhere, because a full 4x3x3x3 factorial is
    108 conditions per target and most cells would be uninformative:

      * `tonefine` is NEUTRAL, so it crosses WHITE-POINT x GAMMA — the two levers that move tone —
        and carries a couple of chroma/saturation cells purely as a NULL CHECK: a chroma lever that
        moves a neutral ramp is misbehaving, and that is worth knowing.
      * `huevalue` is CHROMATIC, so it crosses WHITE-POINT x CHROMA — which is precisely ADR-091's
        open claim that the gamut is luminance-limited, i.e. that white-point and chroma are not
        independent at all.
      * `edges` and `linepairs` cross WHITE-POINT x GAMMA at the extremes only: they measure
        structure, and structure is cheap to sample coarsely.
    """
    # --- the design ------------------------------------------------------------------------------
    # A CENTRAL COMPOSITE DESIGN, which is the standard answer to "several continuous levers, want
    # main effects AND interactions, cannot afford a full factorial":
    #
    #   AXIAL points   each lever swept along its own axis with the others at centre. Captures
    #                  CURVATURE — white-point's effect is plainly nonlinear, so two levels would
    #                  fit a straight line through a bend and call it an effect.
    #   CORNER points  every lever at low/high, fully crossed. 2^4 = 16 is cheap enough to run FULL
    #                  rather than fractional, which leaves every main effect and every two-factor
    #                  interaction UNCONFOUNDED. One-at-a-time sweeps structurally cannot do this:
    #                  they cannot separate a main effect from an interaction at all.
    #   CENTRE reps    the same centre condition, repeated. This is the PURE ERROR estimate, and
    #                  without it an effect cannot be distinguished from noise. The repeatability
    #                  null on `primaries` does not substitute: that target is UNDITHERED, and
    #                  dither is stochastic, so its noise floor is a different and larger number.
    #
    # ⚠️ RUN ORDER IS RANDOMISED, and that is not fastidiousness. The rig runs on daylight. Running
    # all wp=0 early and all wp=0.88 late would alias any slow illumination drift directly onto the
    # white-point effect and report it as a finding. Randomisation converts a systematic error into
    # a noise term the centre replicates can actually measure.
    CENTRE = dict(wp=0.75, gamma=1.4, chroma=1.5, sat=1.0)
    AXIAL = {"wp": (0.0, 0.64, 0.75, 0.88, 1.0),
             "gamma": (1.0, 1.4, 1.8, 2.2),
             "chroma": (1.0, 1.5, 2.0, 2.5),
             "sat": (0.7, 0.85, 1.0, 1.15, 1.3)}
    CORNERS = {"wp": (0.64, 0.88), "gamma": (1.0, 1.8), "chroma": (1.0, 2.0), "sat": (0.7, 1.3)}
    WP = (0.0, 0.64, 0.75, 0.88)
    rows = [
        # --- M1 repeatability null: FIRST, and every later difference is reported against it.
        {"cond": "primaries#1", "kind": "primaries", "flags": [], "readout": "primaries"},
        {"cond": "primaries#2", "kind": "primaries", "flags": [], "readout": "primaries"},
        # --- panel invariants: undithered, no lever applies, captured once
        {"cond": "inkmix", "kind": "inkmix", "flags": [], "readout": "inkmix"},
        {"cond": "uniformity@0", "kind": "uniformity", "flags": [], "readout": "uniformity"},
        {"cond": "uniformity@180", "kind": "uniformity", "flags": [], "readout": "uniformity",
         "prompt": "ROTATE THE PANEL 180 DEGREES, then press Enter. Without this pair, panel "
                   "non-uniformity and the rig's flat-field residual are perfectly confounded and "
                   "the measurement is of the lighting, not of the panel."},
    ]
    # --- tone: WHITE-POINT x GAMMA, crossed. Gamma 1.4 is the INCUMBENT (_adaptive_gamma ships
    #     1.4-1.5 today with no white-point), so the vault records what is on customers' panels.
    for wp in WP:
        for g in (1.0, 1.4, 1.8):
            rows.append(_lever_row("tonefine", "tonefine", wp=wp, gamma=g))
    # null checks: a chroma or saturation lever should barely move a NEUTRAL ramp
    rows.append(_lever_row("tonefine", "tonefine", wp=0.75, chroma=2.0))
    rows.append(_lever_row("tonefine", "tonefine", wp=0.75, sat=0.7))
    rows.append(_lever_row("tonefine", "tonefine", wp=0.75, sat=1.3))
    # --- colour: WHITE-POINT x CHROMA, crossed — ADR-091's claim that these are not independent
    for wp in WP:
        for k in (1.0, 1.5, 2.0):
            rows.append(_lever_row("huevalue", "huevalue", wp=wp, chroma=k))
    for g in (1.4, 1.8):
        rows.append(_lever_row("huevalue", "huevalue", wp=0.75, gamma=g))
    for sat in (0.7, 1.3):
        rows.append(_lever_row("huevalue", "huevalue", wp=0.75, sat=sat))
    # the ADR-088 hue-conditioned floor, measured rather than argued about
    rows.append(_lever_row("huevalue", "huevalue", wp=0.75, chroma=2.0, floor_max=0.5))
    # the cross-cell dither-bleed control
    rows.append(_lever_row("huevalue", "huevalue", wp=0.75, isolate=True))
    # --- structure: coarse corners of WHITE-POINT x GAMMA
    for kind in ("edges", "linepairs"):
        for wp in (0.0, 0.75):
            for g in (1.0, 1.8):
                rows.append(_lever_row(kind, kind, wp=wp, gamma=g))
    rows.append(_lever_row("surround", "surround", wp=0.0))
    rows.append(_lever_row("surround", "surround", wp=0.75))
    rows.append(_lever_row("resample", "resample", wp=0.75))
    rows.append(_lever_row("resample", "resample", wp=0.0))
    # --- central composite blocks on the two informative targets ---------------------------------
    import itertools
    for kind in ("tonefine", "huevalue"):
        seen = {r["cond"] for r in rows}
        # axial: one lever off centre at a time
        for lever, values in AXIAL.items():
            for v in values:
                kw = dict(CENTRE)
                kw[lever] = v
                r = _lever_row(kind, kind, **kw)
                if r["cond"] not in seen:
                    seen.add(r["cond"])
                    rows.append(r)
        # corners: full 2^4, so no main effect is confounded with a two-factor interaction
        for combo in itertools.product(*CORNERS.values()):
            kw = dict(zip(CORNERS.keys(), combo))
            r = _lever_row(kind, kind, **kw)
            if r["cond"] not in seen:
                seen.add(r["cond"])
                rows.append(r)
        # centre replicates: the pure-error estimate for a DITHERED target
        for rep in range(3):
            r = _lever_row(kind, kind, **CENTRE)
            r = dict(r, cond=r["cond"] + f"_rep{rep + 1}")
            rows.append(r)

    if kinds:
        want = {k.strip() for k in kinds.split(",") if k.strip()}
        rows = [r for r in rows if r["kind"] in want or r["cond"] in want]
    return rows


def _randomised(rows, seed: int):
    """Shuffle run order, keeping the invariants first.

    See the design note in _rows(): on a daylight rig, running a lever's levels in order aliases slow
    illumination drift onto that lever's effect. The invariants stay at the front because the
    alignment prior and the repeatability floor are needed before anything else can be interpreted.
    """
    import random
    head = [r for r in rows if r["kind"] in ("primaries", "inkmix", "uniformity")]
    tail = [r for r in rows if r not in head]
    random.Random(seed).shuffle(tail)
    return head + tail


def _ssh(cmd: str, args, timeout: int = 300) -> tuple:
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         "-o", f"UserKnownHostsFile={args.known}", "-o", "ConnectTimeout=12",
         "-i", args.key, args.pi, cmd],
        check=False, capture_output=True, timeout=timeout, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _render(row, args) -> str:
    cmd = (f"cd {args.repo} && sudo python3 -m tools.eink_bench target {row['kind']} "
           + " ".join(row["flags"]))
    rc, out = _ssh(cmd, args)
    if rc != 0:
        raise RuntimeError(f"render failed rc={rc}: {out.strip()[:300]}")
    return out


def _capture(dest: Path, args) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(
        [sys.executable, "-m", "tools.eink_measure", "capture", "--device", args.device,
         "--size", "1920x1080", "--warmup", "14", "--settle", "--settle-delta", "3.0",
         "--settle-stable", "3", "--settle-tries", "30", "--out", str(dest)],
        check=False, capture_output=True, timeout=600, text=True)
    if not dest.exists():
        raise RuntimeError(f"capture produced no file: {(p.stderr or '')[:300]}")


def _settle_wait(args) -> None:
    """A FIXED pre-roll before the adaptive settle loop even starts.

    Belt and braces on purpose. A Spectra 6 refresh drives the pixels through inversion and flashing
    phases on the way to the final image, so a frame grabbed early is not an early version of that
    image — it is a DIFFERENT image, and one of those was once captured as a flat-field reference and
    silently corrupted everything divided by it. The pre-roll guarantees the refresh is over; the
    settle loop then catches a panel or a light that is slower than expected.
    """
    for remaining in range(args.preroll, 0, -1):
        print(f"\r  settling {remaining:3d}s ", end="", flush=True)
        time.sleep(1)
    print("\r" + " " * 24 + "\r", end="", flush=True)


def _record(rec: dict) -> None:
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    with PROFILE.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _done_keys() -> set:
    if not PROFILE.exists():
        return set()
    out = set()
    for ln in PROFILE.read_text().splitlines():
        if ln.strip():
            try:
                r = json.loads(ln)
                if r.get("ok"):
                    out.add(r["cond"])
            except json.JSONDecodeError:
                continue
    return out


def _conditions(args) -> dict:
    """Capture conditions, stamped on EVERY record. A measurement with no conditions attached is not
    evidence — it cannot be compared to anything later, and worse, it can be compared WRONGLY."""
    return {"date": time.strftime("%Y-%m-%d"), "time": time.strftime("%H:%M:%S"),
            "geometry_version": GEOMETRY_VERSION, "flat_field": args.flat,
            "device": args.device, "panel": list(PANEL),
            "units": "camera-RGB normalised to this panel's own black=0 / white=255 — NOT sRGB",
            "colour_reference": "none (no ColorChecker) — absolute colour claims are NOT supported"}


def _reference(row) -> Image.Image:
    """Rebuild the exact image that was pushed for this row, to align the photograph against."""
    kind, flags = row["kind"], row["flags"]
    kw = {}
    if "--white-point" in flags or "--gamma" in flags:
        wp = float(flags[flags.index("--white-point") + 1]) if "--white-point" in flags else 0.0
        gm = float(flags[flags.index("--gamma") + 1]) if "--gamma" in flags else 0.0

        def pre(im):
            if wp > 0:
                im = im.point([min(255, int(round(i * wp))) for i in range(256)] * 3)
            if gm > 0:
                im = ec.epaper._apply_gamma(im, gm)
            return im
        kw["pre"] = pre
    if kind == "huevalue":
        kw["isolate"] = "--isolate" in flags
    if kind in ("primaries", "inkmix", "uniformity", "flat"):
        kw = {}
    content = et.TARGETS[kind](*PANEL, **kw)
    return et.compose(content, *PANEL, patches=(kind != "flat"))


def one_row(row, flat, args) -> dict:
    t0 = time.time()
    shot = VAULT / "raw" / f"{row['cond'].replace('#', '_')}.png"
    _render(row, args)
    _settle_wait(args)
    _capture(shot, args)
    roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else None
    # Align against the render we actually sent. See em.align_to_reference: the homography fits its
    # four fiducials exactly and hides both lens distortion and any systematic centroid bias, and the
    # residual is a scale error big enough to move a dense grid by half a cell.
    r = em.read_panel(Image.open(shot), *PANEL, roi=roi, flat=flat, reference=_reference(row),
                      align_prior=getattr(args, "prior", None))
    fn = er.READOUTS[row["readout"]]
    data = fn(r["corrected"], *PANEL)
    # The visual record: rectified and corrected, downscaled. Numbers answer the question you thought
    # to ask; the picture answers the one you did not.
    VAULT.mkdir(parents=True, exist_ok=True)
    r["corrected"].resize((800, 600), Image.LANCZOS).save(
        VAULT / f"{row['cond'].replace('#', '_')}.jpg", "JPEG", quality=88)
    return {"cond": row["cond"], "kind": row["kind"], "flags": row["flags"], "ok": True,
            "patch_residual": round(float(r["patch_residual"]), 2),
            "gain": [round(v, 4) for v in r["gain"]], "offset": [round(v, 2) for v in r["offset"]],
            "align": r.get("align"),
            "seconds": round(time.time() - t0, 1), "conditions": _conditions(args),
            "readout": data}


def _align_prior(flat, args):
    """Measure the rig's alignment ONCE, on a target with enough structure to localise.

    Alignment is a property of the RIG — camera and panel do not move between rows — so searching for
    it per row is strictly worse than measuring it once and reusing it. `inkmix` is the right source:
    15 columns of distinct colour give a sharply peaked correlation surface, where a neutral tone
    ramp gives a nearly flat one and returns noise (measured: `tonefine` pinned dx at the +90 search
    limit, a boundary result, while every structured target agreed on scale 0.94 / dx~6 / dy-42).
    """
    raw = VAULT / "raw" / "inkmix.png"
    if not raw.exists():
        return None
    row = {"cond": "inkmix", "kind": "inkmix", "flags": [], "readout": "inkmix"}
    rect = em.rectify(Image.open(raw), *PANEL)
    aligned = em.align_to_reference(rect, _reference(row), *PANEL)
    prior = aligned.info.get("align")
    print(f"rig alignment measured from inkmix: correlation {prior[0]}, scale {prior[1]}, "
          f"dx {prior[2]:+d}, dy {prior[3]:+d}\n")
    return prior


def cmd_run(args) -> None:
    flat = em.build_flat_field(Image.open(args.flat), *PANEL)
    args.prior = _align_prior(flat, args)
    done = _done_keys()
    rows = _randomised([r for r in _rows(args.only) if r["cond"] not in done], args.seed)
    print(f"{len(done)} rows already banked, {len(rows)} to capture, pace {args.pace}s\n")
    last = 0.0
    for i, row in enumerate(rows, 1):
        if row.get("prompt"):
            print(f"\n  *** {row['prompt']}")
            input("  press Enter when ready > ")
        wait = args.pace - (time.time() - last)
        if last and wait > 0:
            print(f"  pacing {wait:.0f}s")
            time.sleep(wait)
        last = time.time()
        print(f"[{i}/{len(rows)}] {row['cond']}  ({' '.join(row['flags']) or 'no levers'})", flush=True)
        try:
            rec = one_row(row, flat, args)
            note = "" if rec["patch_residual"] < 14 else "   <-- PATCH RESIDUAL HIGH"
            print(f"      ok  residual {rec['patch_residual']:.2f}  {rec['seconds']:.0f}s{note}",
                  flush=True)
        except Exception as exc:                    # one bad capture must not end the run
            rec = {"cond": row["cond"], "kind": row["kind"], "flags": row["flags"], "ok": False,
                   "error": str(exc)[:300], "conditions": _conditions(args)}
            print(f"      FAILED: {str(exc)[:160]}", flush=True)
        _record(rec)
    print(f"\nprofile -> {PROFILE}   images -> {VAULT}/")


def cmd_dwell(args) -> None:
    """Photograph ONE refresh repeatedly as the pigment settles.

    Spectra 6 continues to settle after the drive waveform ends, so a reading taken 30 s after the
    push is not necessarily what a viewer sees at hour six. Six data points for a single refresh, and
    it retroactively validates or invalidates every other number in the battery. The 8 h and 24 h
    points cannot be taken in one sitting — park the panel and re-run with --resume-only.
    """
    flat = em.build_flat_field(Image.open(args.flat), *PANEL)
    row = {"cond": f"dwell_{args.target}", "kind": args.target,
           "flags": ["--gamma", "1.0", "--white-point", "0.75"], "readout": args.target}
    if not args.resume_only:
        _render(row, args)
        _settle_wait(args)
    marks = [int(v) for v in args.marks.split(",")]
    t_render = time.time()
    for m in marks:
        wait = m - (time.time() - t_render)
        if wait > 0:
            print(f"  waiting {wait:.0f}s to t+{m}s", flush=True)
            time.sleep(wait)
        shot = VAULT / "raw" / f"dwell_{args.target}_t{m}.png"
        try:
            _capture(shot, args)
            r = em.read_panel(Image.open(shot), *PANEL, flat=flat)
            data = er.READOUTS[args.target](r["corrected"], *PANEL)
            rec = {"cond": f"dwell_{args.target}_t{m}", "kind": args.target, "flags": row["flags"],
                   "ok": True, "dwell_seconds": m,
                   "patch_residual": round(float(r["patch_residual"]), 2),
                   "conditions": _conditions(args), "readout": data}
            print(f"  t+{m}s  residual {rec['patch_residual']:.2f}", flush=True)
        except Exception as exc:
            rec = {"cond": f"dwell_{args.target}_t{m}", "ok": False, "dwell_seconds": m,
                   "error": str(exc)[:300], "conditions": _conditions(args)}
            print(f"  t+{m}s  FAILED: {str(exc)[:120]}", flush=True)
        _record(rec)


def cmd_ghost(args) -> None:
    """Ghosting: the SAME frame preceded by different histories.

    ⚠️ Gated on the repeatability floor. A residual smaller than the panel's own refresh-to-refresh
    scatter is not a ghost, and reporting it as one would be inventing a defect.
    """
    flat = em.build_flat_field(Image.open(args.flat), *PANEL)
    histories = [("black", ["--centre", "0"]), ("white", ["--centre", "255"]),
                 ("inverse", ["--centre", "40"])]
    for name, hflags in histories:
        prev = {"cond": f"ghost_prev_{name}", "kind": "surround", "flags": hflags, "readout": "surround"}
        _render(prev, args)
        _settle_wait(args)
        row = {"cond": f"ghost_after_{name}", "kind": "tonefine",
               "flags": ["--gamma", "1.0", "--white-point", "0.75"], "readout": "tonefine"}
        try:
            rec = one_row(row, flat, args)
            rec["cond"] = f"ghost_after_{name}"
            rec["history"] = name
            print(f"  history={name:8s} residual {rec['patch_residual']:.2f}", flush=True)
        except Exception as exc:
            rec = {"cond": f"ghost_after_{name}", "ok": False, "history": name,
                   "error": str(exc)[:300], "conditions": _conditions(args)}
            print(f"  history={name:8s} FAILED: {str(exc)[:120]}", flush=True)
        _record(rec)


def cmd_rederive(args) -> None:
    """Recompute every readout from the BANKED RAW CAPTURES, without touching the panel.

    This is the single most valuable property of the vault. The irreplaceable asset is the raw
    photograph plus its capture conditions: the rig — this camera lock, this flat field, this
    lighting — cannot be recreated once it comes down, but a readout is just arithmetic over a file
    and can be recomputed as many times as it takes. Six separate analysis bugs were fixed on
    2026-08-29 after the captures were taken, and not one of them cost a panel refresh.

    So: capture generously and analyse later. A run that banks raws is never wasted, even when every
    number it derived at the time turns out to be wrong.
    """
    flat = em.build_flat_field(Image.open(args.flat), *PANEL)
    src = Path(args.profile)
    recs = [json.loads(ln) for ln in src.read_text().splitlines() if ln.strip()]
    out = Path(args.out)
    done, failed = 0, 0
    with out.open("w") as fh:
        for rec in recs:
            raw = VAULT / "raw" / f"{rec['cond'].replace('#', '_')}.png"
            if not rec.get("ok") or not raw.exists():
                fh.write(json.dumps(rec) + "\n")
                continue
            row = {"cond": rec["cond"], "kind": rec["kind"], "flags": rec["flags"],
                   "readout": rec.get("readout") or rec["kind"]}
            try:
                r = em.read_panel(Image.open(raw), *PANEL, flat=flat, reference=_reference(row))
                fn = er.READOUTS[row["readout"]]
                rec = dict(rec, readout=fn(r["corrected"], *PANEL),
                           patch_residual=round(float(r["patch_residual"]), 2),
                           align=r.get("align"), rederived=True)
                done += 1
            except Exception as exc:
                rec = dict(rec, ok=False, error=f"rederive: {str(exc)[:200]}")
                failed += 1
            fh.write(json.dumps(rec) + "\n")
            print(f"  {rec['cond']:38s} {'ok' if rec.get('ok') else 'FAILED'}", flush=True)
    print(f"\n{done} re-derived, {failed} failed -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "dwell", "ghost"):
        s = sub.add_parser(name)
        s.add_argument("--flat", required=True, help="photograph of the all-white panel, this rig setup")
        s.add_argument("--pi", default="pi@PI_HOST_REDACTED")
        s.add_argument("--key", default=str(Path.home() / ".ssh/id_ed25519"))
        s.add_argument("--known", default=str(Path.home() / ".ssh/known_hosts"))
        s.add_argument("--repo", default="/home/pi/Screen-Docent")
        s.add_argument("--device", default="/dev/video0")
        s.add_argument("--roi", default="", help="x0,y0,x1,y1 crop to the panel's active area")
        s.add_argument("--preroll", type=int, default=25,
                       help="fixed seconds to wait after the push, BEFORE the settle loop starts")
        s.add_argument("--pace", type=int, default=45,
                       help="minimum seconds between successive RENDER COMMANDS (not a trailing "
                            "sleep): if a capture overruns, no extra wait is added")
        if name == "run":
            s.add_argument("--only", default="", help="comma-separated target kinds or row keys")
            s.add_argument("--seed", type=int, default=20260829,
                           help="run-order randomisation seed; recorded so the order is replayable")
        if name == "dwell":
            s.add_argument("--target", default="tonefine")
            s.add_argument("--marks", default="30,120,600,3600",
                           help="seconds after the render at which to re-photograph")
            s.add_argument("--resume-only", action="store_true",
                           help="do not re-render; photograph the frame already on the panel (for "
                                "the 8 h / 24 h points, which cannot be taken in one sitting)")
    rd = sub.add_parser("rederive", help="recompute all readouts from banked raws, no panel needed")
    rd.add_argument("--flat", required=True)
    rd.add_argument("--profile", default=str(PROFILE))
    rd.add_argument("--out", default=str(OUT / "panel_profile_rederived.jsonl"))
    args = ap.parse_args()
    {"run": cmd_run, "dwell": cmd_dwell, "ghost": cmd_ghost,
     "rederive": cmd_rederive}[args.cmd](args)


if __name__ == "__main__":
    main()
