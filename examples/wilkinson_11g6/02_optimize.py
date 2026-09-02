"""Stage 2 -- the closed loop. Needs HFSS.

Optuna proposes dimensions, the fab rules reject the unbuildable ones for
free, HFSS solves the rest, scikit-rf turns S-parameters into the six
numbers the spec asks about, and the objective turns those into one loss.
The winner is then re-solved at full mesh fidelity and again on the
1 mil (25.4 um) Altium grid, because those are two different boards.

    uv run python examples/wilkinson_11g6/02_optimize.py --trials 30

Roughly 45 s per draft solve on 4 cores, so 30 trials is about 25 minutes.
The study is resumable: run it again with a bigger --trials and it picks
up where it stopped instead of starting over.

The recorded output of exactly this run is committed under results/.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rf_agent.pipeline import run_closed_loop
from rf_agent.spec import RFSpec

HERE = Path(__file__).parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=30,
                    help="target total trials in the study, not additional ones")
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--altium", action="store_true",
                    help="also write the winner into a live Altium session")
    a = ap.parse_args()

    spec = RFSpec.model_validate_json((HERE / "spec.json").read_text(encoding="utf-8"))
    print(f"{spec.name}: {spec.requirement_text}")
    print(f"band {spec.band_ghz[0]}-{spec.band_ghz[1]} GHz, "
          f"{len(spec.targets)} requirements, target {a.trials} trials\n")

    result = run_closed_loop(spec, n_trials=a.trials, out_dir=a.out,
                             write_altium=a.altium)

    print("\n--- verified ---")
    for t in (result.verification or {}).get("terms", []):
        flag = "PASS" if t["violation"] == 0 else "FAIL"
        print(f"  {t['metric']:24s} <= {t['limit']:7.2f}   "
              f"{t['value']:8.3f}   {flag}")
    print(f"\nmeets every hard requirement: {result.meets_spec}")
    print(f"artefacts in {result.out_dir}")
    for k, v in result.artefacts.items():
        print(f"  {k}: {Path(v).name}")


if __name__ == "__main__":
    main()
