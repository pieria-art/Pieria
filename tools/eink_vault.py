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


def _rows(kinds=None) -> list:
    """The capture matrix.

    Split by whether a render setting can touch the target at all. The four undithered targets are
    PANEL INVARIANTS — no lever applies, so they are captured once and never swept, which is most of
    why the battery costs ~30 refreshes instead of ~120.
    """
    rows = [
        # --- M1, the repeatability null. FIRST, and everything downstream is reported against it.
        # Without an inter-REFRESH floor, no difference measured later is interpretable: "the hue
        # moved 9 units" is meaningless until 9 is known to be more than the panel's own scatter.
        {"cond": "primaries#1", "kind": "primaries", "flags": [], "readout": "primaries"},
        {"cond": "primaries#2", "kind": "primaries", "flags": [], "readout": "primaries"},

        # --- panel invariants
        {"cond": "inkmix", "kind": "inkmix", "flags": [], "readout": "inkmix"},
        {"cond": "uniformity@0", "kind": "uniformity", "flags": [], "readout": "uniformity",
         "prompt": None},
        {"cond": "uniformity@180", "kind": "uniformity", "flags": [], "readout": "uniformity",
         "prompt": "ROTATE THE PANEL 180 DEGREES, then press Enter. Without this pair, panel "
                   "non-uniformity and the rig's flat-field residual are perfectly confounded and "
                   "the measurement is of the lighting."},

        # --- tone response and grain across the white-point ladder, at gamma 1.0
        {"cond": "tonefine_wp0", "kind": "tonefine", "flags": ["--gamma", "1.0"], "readout": "tonefine"},
        {"cond": "tonefine_wp0.64", "kind": "tonefine",
         "flags": ["--gamma", "1.0", "--white-point", "0.64"], "readout": "tonefine"},
        {"cond": "tonefine_wp0.75", "kind": "tonefine",
         "flags": ["--gamma", "1.0", "--white-point", "0.75"], "readout": "tonefine"},
        {"cond": "tonefine_wp0.88", "kind": "tonefine",
         "flags": ["--gamma", "1.0", "--white-point", "0.88"], "readout": "tonefine"},
        # The INCUMBENT. _adaptive_gamma ships 1.4-1.5 today with no white-point, so the vault must
        # record what is actually on customers' panels, not only the challenger.
        {"cond": "tonefine_ship", "kind": "tonefine", "flags": ["--gamma", "1.4"], "readout": "tonefine"},

        # --- ADR-091's table, measured on glass
        {"cond": "huevalue_wp0", "kind": "huevalue", "flags": ["--gamma", "1.0"], "readout": "huevalue"},
        {"cond": "huevalue_wp0.75", "kind": "huevalue",
         "flags": ["--gamma", "1.0", "--white-point", "0.75"], "readout": "huevalue"},
        {"cond": "huevalue_iso_wp0.75", "kind": "huevalue",
         "flags": ["--gamma", "1.0", "--white-point", "0.75", "--isolate"], "readout": "huevalue"},

        # --- validity, structure and detail at the shipping candidate
        {"cond": "surround_wp0.75", "kind": "surround",
         "flags": ["--gamma", "1.0", "--white-point", "0.75"], "readout": "surround"},
        {"cond": "edges_wp0.75", "kind": "edges",
         "flags": ["--gamma", "1.0", "--white-point", "0.75"], "readout": "edges"},
        {"cond": "edges_wp0", "kind": "edges", "flags": ["--gamma", "1.0"], "readout": "edges"},
        {"cond": "linepairs_wp0.75", "kind": "linepairs",
         "flags": ["--gamma", "1.0", "--white-point", "0.75"], "readout": "linepairs"},
        {"cond": "linepairs_wp0", "kind": "linepairs", "flags": ["--gamma", "1.0"], "readout": "linepairs"},
        {"cond": "resample_wp0.75", "kind": "resample",
         "flags": ["--gamma", "1.0", "--white-point", "0.75"], "readout": "resample"},
    ]
    if kinds:
        want = {k.strip() for k in kinds.split(",") if k.strip()}
        rows = [r for r in rows if r["kind"] in want or r["cond"] in want]
    return rows


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
    r = em.read_panel(Image.open(shot), *PANEL, roi=roi, flat=flat, reference=_reference(row))
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


def cmd_run(args) -> None:
    flat = em.build_flat_field(Image.open(args.flat), *PANEL)
    done = _done_keys()
    rows = [r for r in _rows(args.only) if r["cond"] not in done]
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
        if name == "dwell":
            s.add_argument("--target", default="tonefine")
            s.add_argument("--marks", default="30,120,600,3600",
                           help="seconds after the render at which to re-photograph")
            s.add_argument("--resume-only", action="store_true",
                           help="do not re-render; photograph the frame already on the panel (for "
                                "the 8 h / 24 h points, which cannot be taken in one sitting)")
    args = ap.parse_args()
    {"run": cmd_run, "dwell": cmd_dwell, "ghost": cmd_ghost}[args.cmd](args)


if __name__ == "__main__":
    main()
