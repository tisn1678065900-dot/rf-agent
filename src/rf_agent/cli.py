"""Command line for rf-agent.

Everything the MCP server can do is reachable here too, so the loop can
be driven, debugged and re-run without an LLM in the way.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import get_settings
from .devices import REGISTRY, get_device
from .geometry import check_drc
from .spec import RFSpec
from .stackup import LAMINATES


def _spec_from_args(a) -> RFSpec:
    if a.spec:
        return RFSpec.model_validate_json(Path(a.spec).read_text(encoding="utf-8"))
    return RFSpec.divider(
        f0_ghz=a.f0,
        n_way=a.n_way,
        laminate=a.laminate,
        bandwidth_frac=a.bw,
        gap_mm=a.gap,
        s11_db=a.s11,
        isolation_db=a.isolation,
        name=a.name,
        requirement_text=a.text or "",
    )


def _add_spec_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--spec", help="path to a spec JSON (overrides the flags below)")
    p.add_argument("--f0", type=float, default=11.6, help="centre frequency in GHz")
    p.add_argument("--n-way", type=int, default=2, dest="n_way", help="2 or 4")
    p.add_argument("--laminate", default="RO4350B-0.254", choices=sorted(LAMINATES))
    p.add_argument("--bw", type=float, default=0.20, help="fractional bandwidth")
    p.add_argument("--gap", type=float, default=0.25, help="coplanar gap in mm")
    p.add_argument("--s11", type=float, default=-20.0, help="return-loss limit in dB")
    p.add_argument("--isolation", type=float, default=-18.0, help="isolation limit in dB")
    p.add_argument("--name", default=None)
    p.add_argument("--text", default=None, help="the requirement in words, for the report")


# --------------------------------------------------------------------------


def cmd_doctor(a) -> int:
    s = get_settings()
    print("workspace          :", s.workspace)
    print("AEDT version       :", s.aedt_version)
    print("ansysedt.exe       :", s.ansysedt_exe, "OK" if s.ansysedt_exe.exists() else "MISSING")
    print("non-graphical      :", s.non_graphical)
    print("cores per solve    :", s.n_cores)

    try:
        import ansys.aedt.core as c

        print("pyaedt             :", c.__version__)
    except Exception as e:
        print("pyaedt             : MISSING", e)

    for mod in ("optuna", "skrf", "shapely", "ezdxf"):
        try:
            m = __import__(mod)
            print(f"{mod:19s}:", getattr(m, "__version__", "?"))
        except Exception as e:
            print(f"{mod:19s}: MISSING {e}")

    print()
    from .export import AltiumWriter

    pre = AltiumWriter().preflight()
    if pre.get("ok"):
        print("Altium bridge      : ready")
    else:
        print(f"Altium bridge      : not ready ({pre.get('stage')}) - {pre.get('reason')}")

    print()
    print("devices            :", ", ".join(sorted(REGISTRY)))
    print("laminates          :", ", ".join(sorted(LAMINATES)))
    return 0


def cmd_line(a) -> int:
    from .lines import gcpw, max_via_pitch, width_for_z0

    lam = LAMINATES[a.laminate]
    f = a.f0 * 1e9
    if a.z0:
        w = width_for_z0(a.z0, a.gap, lam.thickness_mm, lam.er, f)
        print(f"Z0 = {a.z0} ohm  ->  w = {w:.4f} mm")
    else:
        w = a.width
    r = gcpw(w, a.gap, lam.thickness_mm, lam.er, f, tand=lam.tand, t_mm=lam.copper_mm)
    print(f"laminate      {lam.name} {lam.thickness_mm} mm, er {lam.er}")
    print(f"w / gap       {w:.4f} / {a.gap} mm")
    print(f"Z0            {r.z0:.3f} ohm")
    print(f"eps_eff       {r.eps_eff:.4f}")
    print(f"lambda_g @ {a.f0} GHz  {r.lambda_g_mm:.4f} mm   "
          f"(quarter wave = {r.quarter_wave_mm():.4f} mm)")
    print(f"loss          {r.alpha_db_per_mm * 1000:.2f} dB/m")
    print(f"max via pitch {max_via_pitch(lam.er, f):.3f} mm  (lambda_g/12 in the substrate)")
    return 0


def cmd_synth(a) -> int:
    spec = _spec_from_args(a)
    dev = get_device(spec.device)
    params = dict(dev.seed_params(spec))
    if a.set:
        for kv in a.set:
            k, v = kv.split("=", 1)
            params[k] = float(v)
    g = dev.build(spec, params)
    drc = check_drc(g, spec.stackup.fab)
    print(json.dumps({"params": params, "geometry": g.summary(), "drc": drc}, indent=2))

    out = Path(a.out) if a.out else get_settings().workspace / "designs" / spec.name
    out.mkdir(parents=True, exist_ok=True)
    from .export import write_dxf

    print("dxf     :", write_dxf(g, out / f"{spec.name}.dxf"))
    try:
        from .render import preview

        print("preview :", preview(g, out / "layout.png", title=spec.name))
    except ImportError:
        pass
    return 0 if not drc else 1


def cmd_design(a) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr
    )
    spec = _spec_from_args(a)
    from .pipeline import run_closed_loop

    res = run_closed_loop(
        spec,
        n_trials=a.trials,
        timeout=a.timeout,
        out_dir=a.out,
        write_altium=a.altium,
        altium_origin_mils=(a.origin_x, a.origin_y),
    )
    print()
    print("meets spec :", res.meets_spec)
    print("report     :", res.artefacts.get("report_md"))
    print("elapsed    : %.0f s" % res.elapsed_s)
    return 0 if res.meets_spec else 2


def cmd_altium(a) -> int:
    spec = RFSpec.model_validate_json(Path(a.spec).read_text(encoding="utf-8"))
    dev = get_device(spec.device)
    params = json.loads(Path(a.params).read_text(encoding="utf-8")) if a.params else dev.seed_params(spec)
    g = dev.build(spec, params)
    from .export import AltiumWriter

    w = AltiumWriter(origin_mils=(a.origin_x, a.origin_y))
    out = w.write(g, spec, dry_run=a.dry_run)
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok") else 1


def cmd_serve(a) -> int:
    from .server import main as serve_main

    return serve_main()


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rf-agent", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check the toolchain on this machine")
    d.set_defaults(func=cmd_doctor)

    ln = sub.add_parser("line", help="grounded-CPW impedance and wavelength")
    ln.add_argument("--laminate", default="RO4350B-0.254", choices=sorted(LAMINATES))
    ln.add_argument("--f0", type=float, default=11.6)
    ln.add_argument("--gap", type=float, default=0.25)
    ln.add_argument("--width", type=float, default=0.5)
    ln.add_argument("--z0", type=float, default=None, help="solve for the width at this Z0")
    ln.set_defaults(func=cmd_line)

    sy = sub.add_parser("synth", help="draw the layout without solving")
    _add_spec_args(sy)
    sy.add_argument("--set", action="append", help="override a parameter, e.g. --set l_arm=4.7")
    sy.add_argument("--out", default=None)
    sy.set_defaults(func=cmd_synth)

    dg = sub.add_parser("design", help="run the full closed loop")
    _add_spec_args(dg)
    dg.add_argument("--trials", type=int, default=40)
    dg.add_argument("--timeout", type=float, default=None, help="seconds")
    dg.add_argument("--out", default=None)
    dg.add_argument("--altium", action="store_true", help="write the winner into Altium")
    dg.add_argument("--origin-x", type=int, default=2000, dest="origin_x")
    dg.add_argument("--origin-y", type=int, default=2000, dest="origin_y")
    dg.set_defaults(func=cmd_design)

    al = sub.add_parser("altium", help="write a design into a live Altium session")
    al.add_argument("--spec", required=True)
    al.add_argument("--params", default=None, help="JSON of optimised parameters")
    al.add_argument("--origin-x", type=int, default=2000, dest="origin_x")
    al.add_argument("--origin-y", type=int, default=2000, dest="origin_y")
    al.add_argument("--dry-run", action="store_true")
    al.set_defaults(func=cmd_altium)

    sv = sub.add_parser("serve", help="run the MCP server on stdio")
    sv.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
