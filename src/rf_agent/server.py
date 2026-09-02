"""MCP server: the RF/EM half of the toolchain.

Pair it with eda-agent, which owns the Altium half. An agent then holds
both: eda-agent's ~400 tools to read and edit the board a human has open,
and these to synthesise, simulate, optimise and commit RF structures.

The long stages (`rf_optimize`, `rf_design`) return a job id immediately
and are polled with `rf_job_status`. An MCP call that blocks for an hour
is not usable by an agent.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import get_settings
from .devices import REGISTRY, get_device
from .geometry import check_drc
from .jobs import get_registry
from .spec import RFSpec
from .stackup import LAMINATES

log = logging.getLogger("rf_agent.server")
mcp = FastMCP("rf-agent")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _load_spec(spec: dict | str) -> RFSpec:
    """Accept a spec as a dict, a JSON string, or a path to a spec file."""
    if isinstance(spec, RFSpec):
        return spec
    if isinstance(spec, dict):
        return RFSpec.model_validate(spec)
    text = str(spec)
    p = Path(text)
    if len(text) < 500 and p.exists():
        return RFSpec.model_validate_json(p.read_text(encoding="utf-8"))
    return RFSpec.model_validate_json(text)


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


@mcp.tool()
def rf_doctor() -> dict[str, Any]:
    """Check the RF toolchain on this machine before trusting any result.

    Reports the AEDT install PyAEDT will drive, the Python packages the
    loop needs, and whether a live Altium bridge is reachable. Call this
    first when anything behaves oddly.
    """
    s = get_settings()
    out: dict[str, Any] = {"settings": s.describe(), "packages": {}}
    for mod in ("ansys.aedt.core", "optuna", "skrf", "shapely", "ezdxf"):
        try:
            m = __import__(mod, fromlist=["__version__"])
            out["packages"][mod] = getattr(m, "__version__", "present")
        except Exception as e:
            out["packages"][mod] = f"MISSING ({e})"

    from .export import AltiumWriter

    out["altium"] = AltiumWriter().preflight()
    out["devices"] = sorted(REGISTRY)
    out["laminates"] = {k: v.model_dump() for k, v in LAMINATES.items()}
    return out


@mcp.tool()
def rf_list_devices() -> dict[str, Any]:
    """The parametric structures this server can synthesise and optimise."""
    return {
        k: {"title": v.title, "module": v.__module__}
        for k, v in sorted(REGISTRY.items())
    }


@mcp.tool()
def rf_line_model(
    laminate: str = "RO4350B-0.254",
    f0_ghz: float = 11.6,
    gap_mm: float = 0.25,
    width_mm: float | None = None,
    z0: float | None = None,
) -> dict[str, Any]:
    """Grounded-CPW impedance, effective permittivity and wavelength.

    Give `width_mm` to analyse a line, or `z0` to synthesise the width
    that hits an impedance. This is the closed-form model the optimiser
    seeds from -- it is accurate enough to start from and not accurate
    enough to finish on.
    """
    from .lines import gcpw, max_via_pitch, width_for_z0

    if laminate not in LAMINATES:
        return {"error": f"unknown laminate {laminate}", "have": sorted(LAMINATES)}
    lam = LAMINATES[laminate]
    f = f0_ghz * 1e9
    if z0 is not None:
        width_mm = width_for_z0(z0, gap_mm, lam.thickness_mm, lam.er, f)
    if width_mm is None:
        return {"error": "give either width_mm or z0"}

    r = gcpw(width_mm, gap_mm, lam.thickness_mm, lam.er, f, tand=lam.tand, t_mm=lam.copper_mm)
    return {
        "laminate": lam.model_dump(),
        "width_mm": round(width_mm, 5),
        "gap_mm": gap_mm,
        "z0_ohm": round(r.z0, 4),
        "eps_eff": round(r.eps_eff, 5),
        "lambda_g_mm": round(r.lambda_g_mm, 5),
        "quarter_wave_mm": round(r.quarter_wave_mm(), 5),
        "loss_db_per_m": round(r.alpha_db_per_mm * 1000, 3),
        "max_via_pitch_mm": round(max_via_pitch(lam.er, f), 4),
    }


# --------------------------------------------------------------------------
# spec + geometry
# --------------------------------------------------------------------------


@mcp.tool()
def rf_make_spec(
    f0_ghz: float,
    n_way: int = 2,
    laminate: str = "RO4350B-0.254",
    bandwidth_frac: float = 0.20,
    gap_mm: float = 0.25,
    s11_db: float = -20.0,
    isolation_db: float = -18.0,
    excess_loss_db: float = 0.5,
    amplitude_imbalance_db: float = 0.3,
    name: str | None = None,
    requirement_text: str = "",
    save_to: str | None = None,
) -> dict[str, Any]:
    """Turn a requirement into the structured spec everything else reads.

    Limits are stated the way a datasheet states them: `s11_db=-20` means
    S11 at or below -20 dB across the band, `isolation_db=-18` means the
    coupling between any two outputs stays at or below -18 dB. Keep
    `requirement_text` -- it goes in the report so the design can be
    traced back to the sentence that asked for it.
    """
    try:
        spec = RFSpec.divider(
            f0_ghz=f0_ghz,
            n_way=n_way,
            laminate=laminate,
            bandwidth_frac=bandwidth_frac,
            gap_mm=gap_mm,
            s11_db=s11_db,
            isolation_db=isolation_db,
            excess_loss_db=excess_loss_db,
            amplitude_imbalance_db=amplitude_imbalance_db,
            name=name,
            requirement_text=requirement_text,
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    out = {"spec": spec.model_dump(mode="json")}
    bad = spec.stackup.check()
    if bad:
        out["stackup_warnings"] = bad
    if save_to:
        p = Path(save_to)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out["spec"], indent=2), encoding="utf-8")
        out["saved_to"] = str(p)
    return out


@mcp.tool()
def rf_synthesize(
    spec: dict | str,
    params: dict[str, float] | None = None,
    render: bool = True,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """Draw a candidate layout without solving it.

    With no `params` this returns the analytic seed -- the textbook
    starting point the optimiser begins from. Always cheap; use it to
    sanity-check dimensions, DRC and board size before committing solver
    time. The PNG is worth looking at.
    """
    try:
        sp = _load_spec(spec)
    except Exception as e:
        return {"error": f"bad spec: {e}"}

    dev = get_device(sp.device)
    space = dev.param_space(sp)
    p = dict(dev.seed_params(sp))
    if params:
        unknown = [k for k in params if k not in space]
        if unknown:
            return {"error": f"unknown parameters {unknown}", "known": sorted(space)}
        p.update({k: float(v) for k, v in params.items()})

    try:
        g = dev.build(sp, p)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "params": p}

    drc = check_drc(g, sp.stackup.fab)
    out: dict[str, Any] = {
        "params": {k: round(v, 5) for k, v in p.items()},
        "param_space": {
            k: {"lo": round(r.lo, 5), "hi": round(r.hi, 5), "seed": round(r.seed, 5),
                "unit": r.unit, "description": r.description}
            for k, r in space.items()
        },
        "geometry": g.summary(),
        "drc": drc,
        "manufacturable": not drc,
    }

    d = Path(out_dir) if out_dir else get_settings().workspace / "designs" / sp.name
    d.mkdir(parents=True, exist_ok=True)
    from .export import quantisation_report, write_dxf

    out["quantisation"] = quantisation_report(g, sp)
    out["dxf"] = str(write_dxf(g, d / f"{sp.name}.dxf"))
    if render:
        try:
            from .render import preview

            out["preview_png"] = str(preview(g, d / "layout.png", title=sp.name))
        except Exception as e:
            out["preview_error"] = str(e)
    return out


# --------------------------------------------------------------------------
# EM
# --------------------------------------------------------------------------


@mcp.tool()
def rf_solve(
    spec: dict | str,
    params: dict[str, float] | None = None,
    draft: bool = False,
) -> dict[str, Any]:
    """Solve one candidate in HFSS and score it against the spec.

    Blocking, and a full-fidelity solve on a small divider is minutes.
    Use `draft=True` for a fast, coarser answer. Results are cached by
    geometry content, so re-solving the same candidate is free.
    """
    try:
        sp = _load_spec(spec)
    except Exception as e:
        return {"error": f"bad spec: {e}"}

    from .em import HfssSolver
    from .metrics import compliance
    from .optimize import Optimiser

    dev = get_device(sp.device)
    p = dict(dev.seed_params(sp))
    if params:
        p.update({k: float(v) for k, v in params.items()})

    g = dev.build(sp, p)
    drc = check_drc(g, sp.stackup.fab)
    if drc:
        return {"error": "geometry violates fab rules; not solved", "drc": drc, "params": p}

    solver = HfssSolver(keep_projects=True)
    try:
        opt = Optimiser(sp, solver=solver)
        mt, sc, info = opt.evaluate_geometry(g, tag=f"{sp.name}_solve", draft=draft)
        return {
            "params": {k: round(v, 5) for k, v in p.items()},
            "geometry": g.summary(),
            "metrics": mt.as_dict(),
            "score": sc.as_dict(),
            "compliance": compliance(mt, sp),
            "solve": info,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "params": p}
    finally:
        solver.close()


@mcp.tool()
def rf_optimize(
    spec: dict | str,
    n_trials: int = 40,
    timeout_s: float | None = None,
    seed: int = 0,
) -> dict[str, Any]:
    """Start an Optuna study over the device's parameters, scored by HFSS.

    Returns a job id immediately; poll `rf_job_status`. Trial 0 is always
    the analytic seed. Exploration runs at draft mesh; call `rf_design`
    instead if you want the winner verified, exported and reported in one
    go.
    """
    try:
        sp = _load_spec(spec)
    except Exception as e:
        return {"error": f"bad spec: {e}"}

    def work(job) -> dict:
        from .em import HfssSolver
        from .optimize import Optimiser

        solver = HfssSolver()
        try:
            job.note(f"study {sp.name}: {n_trials} trials")
            opt = Optimiser(sp, solver=solver, draft=True)

            def cb(study, trial):
                job.note(
                    f"trial {trial.number}: loss="
                    f"{trial.value if trial.value is not None else float('nan'):.4f}"
                )

            r = opt.run(n_trials=n_trials, timeout=timeout_s, seed=seed, callbacks=[cb])
            job.note(f"best loss {r.best_loss:.4f}")
            return r.as_dict()
        finally:
            solver.close()

    job = get_registry().submit("optimize", work)
    return {"job_id": job.id, "status": job.status, "poll_with": "rf_job_status"}


@mcp.tool()
def rf_design(
    spec: dict | str,
    n_trials: int = 40,
    timeout_s: float | None = None,
    write_altium: bool = False,
    altium_origin_mils: list[int] | None = None,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """Run the whole closed loop: seed, optimise, verify, export, report.

    Returns a job id; poll `rf_job_status`. The winner is re-solved at
    full fidelity before anything is reported, and if rounding the trace
    widths onto Altium's 1-mil grid shifts an impedance, the snapped
    geometry is solved too so the report states what the board will
    actually do rather than what the optimiser found.

    `write_altium=True` commits the result into the PCB the user has open,
    which needs a live eda-agent bridge -- check `rf_doctor` first.
    """
    try:
        sp = _load_spec(spec)
    except Exception as e:
        return {"error": f"bad spec: {e}"}

    origin = tuple(altium_origin_mils or (2000, 2000))

    def work(job) -> dict:
        from .pipeline import run_closed_loop

        job.note(f"closed loop for {sp.name}, {n_trials} trials")
        r = run_closed_loop(
            sp,
            n_trials=n_trials,
            timeout=timeout_s,
            out_dir=out_dir,
            write_altium=write_altium,
            altium_origin_mils=origin,  # type: ignore[arg-type]
        )
        job.note("meets spec" if r.meets_spec else "does NOT meet spec")
        return r.as_dict()

    job = get_registry().submit("design", work)
    return {"job_id": job.id, "status": job.status, "poll_with": "rf_job_status"}


@mcp.tool()
def rf_job_status(job_id: str, include_result: bool = True) -> dict[str, Any]:
    """Progress of a running study or design loop, and its result when done."""
    job = get_registry().get(job_id)
    if job is None:
        return {"error": f"no job {job_id}", "jobs": get_registry().list()}
    out = job.summary()
    if include_result and job.status == "done":
        out["result"] = job.result
    return out


@mcp.tool()
def rf_list_jobs() -> dict[str, Any]:
    """Every job this server has run in this session."""
    return {"jobs": get_registry().list()}


# --------------------------------------------------------------------------
# Altium
# --------------------------------------------------------------------------


@mcp.tool()
def rf_altium_preflight() -> dict[str, Any]:
    """Is a live Altium session reachable, with a PCB open and ready to write?

    Says which of the three things is missing -- eda-agent not importable,
    Altium not running, or the bridge script not started inside it -- and
    how to fix that one.
    """
    from .export import AltiumWriter

    return AltiumWriter().preflight()


@mcp.tool()
def rf_export_altium(
    spec: dict | str,
    params: dict[str, float] | None = None,
    origin_mils: list[int] | None = None,
    dry_run: bool = True,
    place_vias: bool = True,
    place_pour: bool = True,
) -> dict[str, Any]:
    """Write a design into the PCB document the user currently has open.

    Emits design intent, not EM polygons: centrelines as tracks with their
    real widths, ground vias as vias, and the coplanar ground as a polygon
    pour with a clearance rule sized to the gap the EM model used.

    `dry_run=True` (the default) resolves the whole plan to mils and
    returns it without touching the board. Look at the quantisation report
    before committing -- Altium's PCB command surface is integer mils, and
    on a narrow high-impedance arm that rounding moves the impedance.
    """
    try:
        sp = _load_spec(spec)
    except Exception as e:
        return {"error": f"bad spec: {e}"}

    dev = get_device(sp.device)
    p = dict(dev.seed_params(sp))
    if params:
        p.update({k: float(v) for k, v in params.items()})
    g = dev.build(sp, p)

    drc = check_drc(g, sp.stackup.fab)
    if drc and not dry_run:
        return {"error": "refusing to write a geometry that fails DRC", "drc": drc}

    from .export import AltiumWriter

    w = AltiumWriter(origin_mils=tuple(origin_mils or (2000, 2000)))  # type: ignore[arg-type]
    out = w.write(g, sp, dry_run=dry_run, place_vias=place_vias, place_pour=place_pour)
    out["drc"] = drc
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
