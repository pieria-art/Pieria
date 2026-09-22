"""
tools/eink_wpfit.py — measure each work's ideal white-point on the panel, unattended
(maintainer tool — NOT part of the runtime image).

WHAT IT MEASURES. `wp*`, the white-point scale at which the panel's rendered luminance MATCHES the
reference artwork's. Below it the render is too dark, above it too bright, so the crossing is a real
optimum rather than a preference — and it is found by the camera, with no human in the loop.

WHY THAT MATTERS. ADR-088 killed a fitted per-image model because the labels were human gamma
judgements: noisy, un-replayable, subject to priming and to criteria drifting mid-session. `wp*` has
none of those problems. It is reproducible, it is generated at panel speed rather than human speed,
and the corpus it can be run over is 2857 works rather than however many a person can sit through.
The same regression that was abandoned becomes reasonable on this data.

THE BRACKET MUST CONTAIN THE CROSSING. First run of this used a fixed [0.64, 0.78] bracket and
reported wp* of 0.929 and 0.897 for the two palest works — both EXTRAPOLATIONS, because dLum was
negative at both ends. That is the ADR-084 grid-ceiling lesson again: a boundary result is a signal
to widen the search, never an answer. This widens automatically and marks anything it could not
bracket.

⚠️ PACING IS THE REAL BUDGET, NOT WEAR. Every measurement is a panel refresh (~22 s) and vendors ask
for no more than one per ~180 s, so a run is bounded by the clock. "Colour e-ink has finite refresh
cycles" was unsourced and is struck (ADR-113). Two refreshes per work is the floor; widening costs
more wall-clock. Resume rather than restart.

    python -m tools.eink.eink_wpfit --flat bench-eink/reference/flat.png --works 1,4,9
    python -m tools.eink.eink_wpfit --flat ... --all          # every corpus work not yet measured
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.eink import eink_measure as em  # noqa: E402

OUT = Path("bench-eink")
RESULTS = OUT / "wpfit.jsonl"
PANEL = (1600, 1200)


def _ssh(host_cmd: str, pi: str, key: str, known: str, timeout: int = 240) -> int:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         "-o", f"UserKnownHostsFile={known}", "-o", "ConnectTimeout=12", "-i", key, pi, host_cmd],
        check=False, capture_output=True, timeout=timeout).returncode


def _render(n: int, wp: float, args) -> None:
    _ssh(f"cd {args.repo} && sudo python3 -m tools.eink.eink_bench target art --n {n} "
         f"--gamma 1.0 --white-point {wp}", args.pi, args.key, args.known)


def _capture(dest: Path, args) -> None:
    subprocess.run([sys.executable, "-m", "tools.eink.eink_measure", "capture",
                    "--device", args.device, "--size", "1920x1080", "--warmup", "14",
                    "--settle", "--settle-delta", "3.0", "--settle-stable", "3",
                    "--settle-tries", "30", "--out", str(dest)],
                   check=False, capture_output=True, timeout=500)


def _reference(n: int, args) -> Path:
    dest = OUT / "wpfit" / f"ref_{n:02d}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["curl", "-s", "-m", "25", "-o", str(dest),
                    f"http://{args.pi.split('@')[-1]}:{args.port}/artref_{n:02d}.jpg"],
                   check=False, capture_output=True, timeout=40)
    return dest


def measure_dlum(n: int, wp: float, flat, args) -> float:
    shot = OUT / "wpfit" / f"w_{n:02d}_{wp:.2f}.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    _render(n, wp, args)
    _capture(shot, args)
    ref = _reference(n, args)
    m = em.score_against_reference(Image.open(shot), Image.open(ref), *PANEL, flat=flat)
    return float(m["d_luminance"])


def fit_one(n: int, flat, args) -> dict:
    """Bracket the luminance-match crossing, widening until it is genuinely bracketed."""
    lo, hi = args.lo, args.hi
    d_lo = measure_dlum(n, lo, flat, args)
    d_hi = measure_dlum(n, hi, flat, args)
    widened = 0
    while d_lo * d_hi > 0 and widened < args.max_widen:
        widened += 1
        if d_hi < 0:                      # still too dark at the top — push the ceiling up
            lo, d_lo = hi, d_hi
            hi = min(1.0, hi + 0.12)
            d_hi = measure_dlum(n, hi, flat, args)
        else:                             # already too bright at the bottom — drop the floor
            hi, d_hi = lo, d_lo
            lo = max(0.20, lo - 0.12)
            d_lo = measure_dlum(n, lo, flat, args)
    bracketed = d_lo * d_hi <= 0
    wp = lo + (hi - lo) * (0 - d_lo) / (d_hi - d_lo) if d_hi != d_lo else float("nan")
    return {"n": n, "wp_star": round(wp, 4), "bracketed": bracketed, "widened": widened,
            "lo": lo, "hi": hi, "d_lo": round(d_lo, 2), "d_hi": round(d_hi, 2)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flat", required=True, help="flat-field photograph for this rig setup")
    ap.add_argument("--works", default="", help="comma-separated corpus numbers")
    ap.add_argument("--all", action="store_true", help="every corpus work not already measured")
    ap.add_argument("--pi", default=os.environ.get("PIERIA_BENCH_PI", ""))
    ap.add_argument("--key", default=str(Path.home() / ".ssh/id_ed25519"))
    ap.add_argument("--known", default=os.environ.get("PIERIA_KNOWN_HOSTS", str(Path.home() / ".ssh/known_hosts")))
    ap.add_argument("--repo", default="/home/pi/Screen-Docent")
    ap.add_argument("--device", default="/dev/video0")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--lo", type=float, default=0.64)
    ap.add_argument("--hi", type=float, default=0.78)
    ap.add_argument("--max-widen", type=int, default=3)
    args = ap.parse_args()

    flat = em.build_flat_field(Image.open(args.flat), *PANEL)
    done = set()
    if RESULTS.exists():
        done = {json.loads(ln)["n"] for ln in RESULTS.read_text().splitlines() if ln.strip()}

    corpus = json.loads((OUT / "corpus.json").read_text())
    if args.all:
        todo = [r["n"] for r in corpus if r["n"] not in done]
    else:
        todo = [int(v) for v in args.works.split(",") if v.strip() and int(v) not in done]
    print(f"{len(done)} already measured, {len(todo)} to go")

    for i, n in enumerate(todo, 1):
        t0 = time.time()
        try:
            rec = fit_one(n, flat, args)
        except Exception as exc:                       # one bad work must not end an overnight run
            rec = {"n": n, "wp_star": None, "error": str(exc)[:200]}
        with RESULTS.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        flag = "" if rec.get("bracketed", False) else "  <-- NOT BRACKETED"
        print(f"[{i}/{len(todo)}] n={n:3d}  wp* {rec.get('wp_star')}  "
              f"widened {rec.get('widened', '-')}  {time.time() - t0:.0f}s{flag}", flush=True)


if __name__ == "__main__":
    main()
