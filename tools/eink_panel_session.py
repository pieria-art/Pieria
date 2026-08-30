"""
tools/eink_panel_session.py — drive a blinded panel session. RUN ON THE BENCH PI.

⚠️ `sd-eink` HOLDS THE PANEL'S SPI/GPIO LINES. This stops it before the first render and restarts it
on the way out, including on Ctrl-C — forgetting either half is how a session ends with a wedged panel.

The runner prints ONLY the blind slot label. It never prints the recipe. The mapping lives in
`panel_session_<date>_blinding.json`, which the judge does not open until every call is recorded —
the protocol ADR-092 had to be corrected into existence, and ADR-096 keeps.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _svc(action: str) -> None:
    subprocess.run(["sudo", "systemctl", action, "sd-eink"], check=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", help="panel_session_*_plan.json")
    ap.add_argument("--start-at", type=int, default=0, help="resume mid-session (0-based)")
    args = ap.parse_args()
    plan = json.loads(Path(args.plan).read_text())

    print(f"\n{len(plan)} renders. The panel takes ~22 s each.")
    print("Judge: note your call for each SLOT. Do not open the blinding file until the end.\n")
    _svc("stop")
    try:
        for i, step in enumerate(plan):
            if i < args.start_at:
                continue
            input(f"  [{i + 1}/{len(plan)}]  press Enter to render slot  ***{step['slot']}***  ")
            cmd = [sys.executable, "tools/eink_show.py", step["image"],
                   "--white-point", str(step["white_point"]), "--gamma", str(step["gamma"])]
            subprocess.run(cmd, cwd=ROOT, check=False)
            print(f"      slot {step['slot']} is on the panel. Look at it before pressing Enter again.\n")
        input("  all four shown. Press Enter to restore the art cycle. ")
    finally:
        _svc("start")           # also runs on Ctrl-C — never leave the panel wedged
        print("\n  sd-eink restarted.")


if __name__ == "__main__":
    main()
