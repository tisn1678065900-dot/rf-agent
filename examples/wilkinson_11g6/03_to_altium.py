"""Stage 3 -- write the optimised board into a live Altium session.

This is the half that eda-agent owns. rf-agent turns the geometry into
tracks, arcs, vias, a pour and a clearance rule; eda-agent's DelphiScript
bridge places them in the PcbDoc you have open, while you watch.

Prerequisites, in order:

  1. Altium Designer running with the target PcbDoc open and focused.
  2. eda-agent installed (``pip install -e .`` in that repo).
  3. Its bridge script polling inside Altium:
     File -> Run Script -> Altium_API -> Dispatcher.pas -> StartMCPServer

Run the dry run first. It plans every primitive and places nothing:

    uv run python examples/wilkinson_11g6/03_to_altium.py

Then commit:

    uv run python examples/wilkinson_11g6/03_to_altium.py --write

Coordinates are integer mils on Altium's side. On a 0.34 mm arm that is a
~1 Ohm impedance step, which is why stage 2 re-solves the snapped
geometry and reports what the board will actually do rather than what the
optimiser wished for.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rf_agent.devices import get_device
from rf_agent.export.altium import AltiumWriter
from rf_agent.spec import RFSpec

HERE = Path(__file__).parent


def load_best_params() -> dict[str, float]:
    """The optimised dimensions from the recorded run."""
    trials = (HERE / "results" / "trials.csv").read_text(encoding="utf-8").splitlines()
    header = trials[0].split(",")
    best, best_loss = None, float("inf")
    for line in trials[1:]:
        row = dict(zip(header, line.split(",")))
        if row["state"] != "COMPLETE" or not row["loss"]:
            continue
        if float(row["loss"]) < best_loss:
            best_loss, best = float(row["loss"]), row
    return {k: float(best[k]) for k in ("l_arm", "w_arm", "r_sep", "r_ohms")}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="actually place the primitives (default is a dry run)")
    ap.add_argument("--origin-x", type=int, default=2000, help="board origin, mils")
    ap.add_argument("--origin-y", type=int, default=2000)
    a = ap.parse_args()

    spec = RFSpec.model_validate_json((HERE / "spec.json").read_text(encoding="utf-8"))
    params = load_best_params()
    print(f"optimised dimensions: "
          + ", ".join(f"{k}={v:g}" for k, v in params.items()))

    geom = get_device(spec.device).build(spec, params)
    writer = AltiumWriter(origin_mils=(a.origin_x, a.origin_y))

    pre = writer.preflight()
    print("\npreflight:")
    print(json.dumps(pre, indent=2, ensure_ascii=False)[:900])
    if not a.write and not pre.get("ready"):
        print("\nAltium is not ready; the dry run below still shows the full plan.")

    result = writer.write(geom, spec, dry_run=not a.write)
    print(f"\n{'WROTE' if a.write else 'DRY RUN'}:")
    print(json.dumps(result, indent=2, ensure_ascii=False)[:1400])


if __name__ == "__main__":
    main()
