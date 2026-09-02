"""Stage 1 -- draw the board from the requirement. No solver needed.

This is the part of the loop that runs on any machine: read the spec,
size the lines analytically, generate the layout, check it against the
fab rules, and write a DXF and a preview PNG.

    uv run python examples/wilkinson_11g6/01_synthesize.py

Nothing here touches HFSS or Altium, so it is the honest place to start
if you want to see what the generator produces before committing a
licence-hour to optimising it.
"""

from __future__ import annotations

from pathlib import Path

from rf_agent.devices import get_device
from rf_agent.export.dxf import write_dxf
from rf_agent.geometry import check_drc
from rf_agent.lines import gcpw, max_via_pitch
from rf_agent.render import preview
from rf_agent.spec import RFSpec

HERE = Path(__file__).parent
OUT = HERE / "out"


def main() -> None:
    spec = RFSpec.model_validate_json((HERE / "spec.json").read_text(encoding="utf-8"))
    st = spec.stackup
    print(f"{spec.name}: {spec.requirement_text}\n")

    # --- what the analytic line model says --------------------------------
    dev = get_device(spec.device)
    space = dev.param_space(spec)
    print("parameter space (seeded from the conformal-mapping GCPW model):")
    for name, r in space.items():
        print(f"  {name:8s} {r.lo:8.4f} .. {r.hi:8.4f}   seed {r.seed:8.4f} {r.unit}")

    pitch_max = max_via_pitch(st.er, spec.f0_hz)
    print(f"\nground-via pitch: using {st.via_pitch_mm} mm, "
          f"lambda_g/12 at f0 is {pitch_max:.3f} mm"
          f"{'  <-- coarser than the rule of thumb' if st.via_pitch_mm > pitch_max else ''}")

    # --- draw it -----------------------------------------------------------
    geom = dev.build(spec, dev.seed_params(spec))
    s = geom.summary()
    print(f"\nboard {s['board_mm'][0]} x {s['board_mm'][1]} mm, "
          f"{s['n_vias']} ground vias, {s['routed_length_mm']} mm routed")
    for note in geom.notes:
        print(f"  {note}")

    feed = gcpw(geom.params["w_main"], st.gap_mm, st.h, st.er, spec.f0_hz, tand=st.tand)
    print(f"  feed line {feed.z0:.2f} ohm, eps_eff {feed.eps_eff:.3f}, "
          f"{feed.alpha_db_per_mm * 1000:.1f} dB/m")
    for p in geom.ports:
        print(f"  {p.name} at ({p.x:7.3f}, {p.y:6.3f}) on the {p.edge} edge")

    # --- would a fab build it? --------------------------------------------
    violations = check_drc(geom, st.fab)
    print("\nDRC:", "clean" if not violations else "")
    for v in violations:
        print(f"  ! {v}")

    # --- artefacts ---------------------------------------------------------
    OUT.mkdir(exist_ok=True)
    png = preview(geom, OUT / "layout_seed.png",
                  title=f"{spec.name} -- analytic seed, not yet optimised")
    dxf = write_dxf(geom, OUT / "layout_seed.dxf")
    print(f"\nwrote {png.relative_to(HERE)}")
    print(f"wrote {dxf.relative_to(HERE)}")
    print("\nThis is the starting point, not the answer. Stage 2 optimises it "
          "against HFSS;\nresults/ has the recorded output of that run.")


if __name__ == "__main__":
    main()
