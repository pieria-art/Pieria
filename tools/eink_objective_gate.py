"""
tools/eink_objective_gate.py — does a candidate objective agree with the human judge?
(maintainer tool — NOT part of the runtime image)

WHY THIS EXISTS. `eink_scurve.py` fits a tone curve by minimising a hand-weighted cost. ADR-092's
post-mortem records what happens when such a cost is trusted without a gate: five successive invented
metrics, each surviving only until the next label arrived. NEXT_SESSION.md states the rule plainly —
*an objective that cannot reproduce the 23 human calls has no business choosing a curve for 2,857
paintings* — and this file is that rule made runnable, so it is checked rather than remembered.

WHAT IT DOES. `bench-eink/wp3_labels.jsonl` holds 23 usable three-level white-point judgements
(rounds 3 and 4; one work excluded at the judge's request). For each labelled work it renders the
three levels through the EXACT bench path and asks whether the candidate's argmin is the level Josh
picked. Accuracy is reported against the base rate, never in isolation.

⚠️ THE BASE RATE IS 8/23 = 34.8%, NOT 61%. NEXT_SESSION.md carried 61%, which is wrong: the picks
split 0.64 x7 / 0.76 x8 / 0.88 x8, so always-guess-the-mode scores 8. A wrong bar is worse than no
bar — it fails objectives that are working and, in the other direction, would pass one that is not.

⚠️ ACCURACY ALONE IS NOT ENOUGH — READ THE PREDICTED DISTRIBUTION. The incumbent `eink_scurve.cost`
scores 30.4%, but the number that matters is that it predicts 0.64 on 23 of 23 works: it is a constant
function of its input, and it collects its 7 hits purely from the works where Josh happened to agree
with the constant. A degenerate objective can sit near the base rate by accident, which is exactly how
one survives review. `--ceiling` additionally answers the follow-up question — whether ANY non-negative
weighting of the candidate's terms could work — by searching them and cross-validating leave-one-out.
That is an upper bound, NOT a weight-tuning step; ADR-092 forbids the latter. The search is
stochastic, so it is repeated over several seeds and reported as a RANGE: a single run of it moves
between 34.8% and 43.5%, and quoting one of those as "the" number is the same defect as any other
figure that changes when you look again.

    python tools/eink_objective_gate.py                 # gate the incumbent eink_scurve.cost
    python tools/eink_objective_gate.py --ceiling       # + best-possible-weighting upper bound

⚠️ PILLOW. The calibration is measured against Pillow 10.3.0's Floyd-Steinberg. 10.3.0 and 12.3.0 were
verified byte-identical on this path (2 works x 4 white-points, sha1 match), so either is safe here;
re-check before trusting a third version rather than assuming the property holds.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import epaper as ep  # noqa: E402
from tools import eink_bench as eb  # noqa: E402
from tools import eink_scurve as sc  # noqa: E402

LEVELS = [0.64, 0.76, 0.88]
PANEL = (1600, 1200)
LABELS = ROOT / "bench-eink/wp3_labels.jsonl"
CORPUS = ROOT / "bench-eink/corpus.json"

# The terms `eink_scurve.cost` combines, as the raw non-negative losses it actually sums. Kept
# separate from cost() so `--ceiling` can re-weight them without touching the shipped weights.
TERMS = {
    "shadow": lambda s: float(np.nan_to_num(s["shadow_to_black"])),
    "highlight": lambda s: float(np.nan_to_num(s["highlight_to_white"])),
    "pale_chroma_loss": lambda s: max(0.0, 40.0 - float(np.nan_to_num(s["pale_chroma_kept"]))),
    "grain": lambda s: float(s["grain"]),
    "tone_error": lambda s: float(s["tone_error"]),
    "lost_contrast": lambda s: max(0.0, 1.0 - float(s["contrast_kept"])),
}


def load_labels() -> list:
    rows = [json.loads(l) for l in LABELS.open()]
    return [r for r in rows if not r.get("excluded") and r.get("pick") is not None]


def _library_path(rel: str) -> Path:
    """Works live in art-pack/_Library (complete) or Artwork/_Library (a 122-work subset)."""
    name = os.path.basename(rel)
    for d in ("art-pack/_Library", "Artwork/_Library"):
        p = ROOT / d / name
        if p.exists():
            return p
    raise FileNotFoundError(f"{name} in neither art-pack/_Library nor Artwork/_Library")


def framed(n: int, corpus: dict) -> Image.Image:
    """The exact 1600x1200 the bench harness put on the panel, BEFORE any lever.

    This must match `eink_bench full` or the gate scores pixels the judge never saw: the authored /
    DB crop changes which pixels exist, and both collapse metrics are fractions over those pixels.
    Round 2 notes record gamma 1.0, saturation 1.0, chroma_gamma 1.0, so no other stage fires.
    """
    p = _library_path(corpus[n]["image"])
    crop, focal = eb._db_crop_and_focal(p.name, *PANEL)
    return ep._fit_rgb(str(p), PANEL[0], PANEL[1], "cover", focal, crop)


def wp_lut(wp: float) -> list:
    return list(ep._tone_lut(wp, 1.0))  # ADR-098: one definition of the white-point LUT


def gate(costfn, labels, scores) -> dict:
    """Agreement with the judge, plus the degeneracy check that accuracy alone hides."""
    hits, pred = 0, []
    for r in labels:
        cs = {w: costfn(scores[r["n"]][w]) for w in LEVELS}
        p = min(cs, key=cs.get)
        pred.append(p)
        hits += (p == r["pick"])
    picks = [r["pick"] for r in labels]
    base = max(picks.count(v) for v in LEVELS) / len(labels)
    spread = {v: pred.count(v) for v in LEVELS}
    return {"hits": hits, "n": len(labels), "acc": hits / len(labels), "base": base,
            "pred": pred, "spread": spread,
            "degenerate": max(spread.values()) == len(labels)}


def ceiling(labels, scores, seed=20260829, repeats=5) -> dict:
    """Best achievable accuracy over NON-NEGATIVE weightings of TERMS, in-sample and leave-one-out.

    ⚠️ This is an UPPER BOUND on the feature set, not a weight recommendation. With 6 free parameters
    and 23 points the in-sample number overfits badly (measured: 65.2% in-sample against an LOO range
    of 34.8-43.5%). Quote the LOO RANGE; if it does not clear the base rate the terms cannot see what
    the judge sees, and re-weighting them is the exact move ADR-092's post-mortem says never works.
    """
    names = list(TERMS)
    F = np.array([[[TERMS[t](scores[r["n"]][w]) for t in names] for w in LEVELS] for r in labels])
    F = F / F.reshape(-1, len(names)).std(axis=0)
    y = np.array([LEVELS.index(r["pick"]) for r in labels])

    def best_w(Fs, ys, s):
        rng = np.random.default_rng(s)
        def acc(W):
            return ((Fs @ W.T).transpose(2, 0, 1).argmin(axis=2) == ys).mean(axis=1)
        top = (lambda W: W[np.argsort(-acc(W))[:40]])(rng.dirichlet(np.ones(len(names)), 200_000))
        for _ in range(25):
            c = np.clip(top[rng.integers(0, len(top), 8000)]
                        + rng.normal(0, .05, (8000, len(names))), 0, None)
            c /= c.sum(axis=1, keepdims=True)
            top = np.vstack([top, c[np.argsort(-acc(c))[:40]]])
            top = top[np.argsort(-acc(top))[:40]]
        return top[0], float(acc(top[:1])[0])

    w, ins = best_w(F, y, seed)
    idx = np.arange(len(labels))
    loos = [float(np.mean([(F[i] @ best_w(F[idx != i], y[idx != i], seed + 1000 * k + i)[0]).argmin()
                           == y[i] for i in idx])) for k in range(repeats)]
    return {"weights": dict(zip(names, w.round(3).tolist())), "in_sample": ins,
            "loo": loos, "loo_mean": float(np.mean(loos))}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate", help="module.py:function taking a score dict -> float. "
                                        "Default: the incumbent eink_scurve.cost")
    ap.add_argument("--ceiling", action="store_true",
                    help="also search all non-negative weightings of the terms (upper bound, LOO)")
    args = ap.parse_args()

    costfn, label = sc.cost, "eink_scurve.cost (incumbent)"
    if args.candidate:
        mod, fn = args.candidate.rsplit(":", 1)
        spec = importlib.util.spec_from_file_location("candidate", mod)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        costfn, label = getattr(m, fn), args.candidate

    corpus = {w["n"]: w for w in json.load(CORPUS.open())}
    labels = load_labels()
    print(f"{len(labels)} usable judgements (of {sum(1 for _ in LABELS.open())} rows)\n"
          f"scoring {len(labels)} works x {len(LEVELS)} levels at {PANEL[0]}x{PANEL[1]} ...")
    scores = {r["n"]: {w: sc.score(framed(r["n"], corpus), wp_lut(w)) for w in LEVELS}
              for r in labels}

    g = gate(costfn, labels, scores)
    verdict = "PASS" if (g["acc"] > g["base"] and not g["degenerate"]) else "FAIL"
    print(f"\n  {label}")
    print(f"    agreement  {g['hits']}/{g['n']} = {g['acc']:.1%}"
          f"   base rate {g['base']:.1%}   chance {1/len(LEVELS):.1%}")
    print(f"    predicts   {g['spread']}"
          f"{'   <-- DEGENERATE: one answer for every work' if g['degenerate'] else ''}")
    print(f"    VERDICT    {verdict}")
    print(f"\n    {'n':>3} {'human':>6} {'argmin':>7}     " + "  ".join(f"c({w})" for w in LEVELS))
    for r, p in zip(labels, g["pred"]):
        cs = [costfn(scores[r["n"]][w]) for w in LEVELS]
        print(f"    {r['n']:3d} {r['pick']:6.2f} {p:7.2f}  {'OK' if p == r['pick'] else '  '}  "
              + "  ".join(f"{c:8.2f}" for c in cs))

    if args.ceiling:
        c = ceiling(labels, scores)
        print("\n  BEST-POSSIBLE non-negative weighting of these terms (upper bound, not advice):")
        print(f"    in-sample {c['in_sample']:.1%}   LEAVE-ONE-OUT {min(c['loo']):.1%}-"
              f"{max(c['loo']):.1%} (mean {c['loo_mean']:.1%} over {len(c['loo'])} searches)"
              f"   base rate {g['base']:.1%}")
        print(f"    weights   {c['weights']}")
        if c["loo_mean"] <= g["base"] + 0.05:
            print("    => even at its ceiling the feature set does not generalise past the base rate.")
            print("       That is a finding about the TERMS, not the weights (ADR-096).")


if __name__ == "__main__":
    main()
