"""The closed loop, end to end.

    requirement -> spec -> seed layout -> HFSS -> Optuna -> verify
                -> DXF + Altium -> report

Each stage writes its artefacts into one run directory and records what
it did, so the report can say not just what the answer is but how it was
reached and what was assumed on the way.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings, get_settings
from .devices import get_device
from .em import HfssSolver
from .export import quantisation_report, snap_geometry, write_dxf
from .geometry import check_drc
from .metrics import compliance, usable_bandwidth_ghz
from .optimize import Optimiser
from .report import write_report
from .spec import RFSpec

log = logging.getLogger("rf_agent.pipeline")


@dataclass
class LoopResult:
    spec: RFSpec
    out_dir: Path
    stages: list[dict] = field(default_factory=list)
    study: dict | None = None
    verification: dict | None = None
    artefacts: dict[str, str] = field(default_factory=dict)
    quantisation: dict | None = None
    altium: dict | None = None
    elapsed_s: float = 0.0

    @property
    def meets_spec(self) -> bool:
        """The verdict on the geometry that will actually be fabricated.

        When the widths were snapped onto Altium's mil grid and re-solved,
        that solve is the one that counts -- it is the board, and it can
        differ from the continuous-dimension optimum in either direction.
        """
        if not self.verification:
            return False
        ab = self.verification.get("as_built")
        if ab:
            return bool(ab["compliance"]["meets_spec"])
        return bool(self.verification["compliance"]["meets_spec"])

    @property
    def ideal_meets_spec(self) -> bool:
        """The verdict on the un-snapped, continuous-dimension geometry."""
        return bool(self.verification and self.verification["compliance"]["meets_spec"])

    def as_dict(self) -> dict:
        return {
            "spec": self.spec.model_dump(mode="json"),
            "out_dir": str(self.out_dir),
            "meets_spec": self.meets_spec,
            "elapsed_s": round(self.elapsed_s, 1),
            "stages": self.stages,
            "study": self.study,
            "verification": self.verification,
            "quantisation": self.quantisation,
            "altium": self.altium,
            "artefacts": self.artefacts,
        }


def run_closed_loop(
    spec: RFSpec,
    *,
    n_trials: int = 40,
    timeout: float | None = None,
    settings: Settings | None = None,
    out_dir: Path | str | None = None,
    write_altium: bool = False,
    altium_origin_mils: tuple[int, int] = (2000, 2000),
    keep_projects: bool = True,
    seed: int | None = 0,
    render: bool = True,
) -> LoopResult:
    s = settings or get_settings()
    s.ensure_dirs()
    out = Path(out_dir) if out_dir else s.workspace / "designs" / spec.name
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    res = LoopResult(spec=spec, out_dir=out)
    device = get_device(spec.device)

    def stage(name: str, **kw) -> None:
        res.stages.append({"stage": name, **kw})
        log.info("stage %s: %s", name, kw)

    # ---------------------------------------------------------------- 1
    (out / "spec.json").write_text(
        json.dumps(spec.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    res.artefacts["spec"] = str(out / "spec.json")

    seed_params = device.seed_params(spec)
    seed_geom = device.build(spec, seed_params)
    seed_drc = check_drc(seed_geom, spec.stackup.fab)
    stage(
        "seed",
        params={k: round(v, 4) for k, v in seed_params.items()},
        geometry=seed_geom.summary(),
        drc=seed_drc,
    )
    if seed_drc:
        # The analytic starting point being unmanufacturable means the
        # spec and the fab rules disagree, not that the optimiser should
        # go hunting.
        stage("abort", reason="seed geometry violates fab rules", drc=seed_drc)
        res.elapsed_s = time.time() - t0
        write_report(res)
        return res

    if render:
        from .render import preview

        p = preview(seed_geom, out / "layout_seed.png", title=f"{spec.name} (analytic seed)")
        res.artefacts["layout_seed_png"] = str(p)

    # ---------------------------------------------------------------- 2
    solver = HfssSolver(s, keep_projects=keep_projects)
    opt = Optimiser(spec, solver=solver, settings=s, draft=True)
    try:
        study = opt.run(n_trials=n_trials, timeout=timeout, seed=seed)
        res.study = study.as_dict()
        stage(
            "optimise",
            trials=study.n_trials,
            solved=study.n_solved,
            drc_rejected=study.n_drc_rejected,
            failed=study.n_failed,
            best_loss=round(study.best_loss, 4),
            elapsed_s=round(study.elapsed_s, 1),
        )

        # ------------------------------------------------------------ 3
        ver = opt.verify(study.best_params, tag=f"{spec.name}_verify")
        res.verification = ver
        stage(
            "verify",
            meets_spec=ver["compliance"]["meets_spec"],
            metrics=ver["metrics"]["values"],
        )

        best_geom = device.build(spec, study.best_params)

        # ------------------------------------------------------------ 4
        # What the mil grid will do to it, and what that costs.
        quant = quantisation_report(best_geom, spec)
        stage("quantisation", worst_z0_shift_ohm=quant["worst_z0_shift_ohm"])
        res.quantisation = quant

        # If snapping moves an impedance by more than a tenth of an ohm,
        # re-solve the snapped geometry: the honest number is the one the
        # fabricated board will hold, not the ideal the optimiser found.
        if quant["worst_z0_shift_ohm"] > 0.1:
            snapped = snap_geometry(best_geom)
            try:
                mt, sc, info = opt.evaluate_geometry(
                    snapped, tag=f"{spec.name}_snapped", draft=False
                )
                res.verification["as_built"] = {
                    "metrics": mt.as_dict(),
                    "score": sc.as_dict(),
                    "compliance": compliance(mt, spec),
                    "solve": info,
                }
                stage(
                    "verify_as_built",
                    meets_spec=compliance(mt, spec)["meets_spec"],
                    note="widths snapped to the 1-mil Altium grid, re-solved",
                )
            except Exception as e:
                stage("verify_as_built", error=f"{type(e).__name__}: {e}")

        # ------------------------------------------------------------ 5
        if render:
            from .metrics import load
            from .render import preview, sparam_plot

            res.artefacts["layout_png"] = str(
                preview(best_geom, out / "layout.png", title=f"{spec.name} (optimised)")
            )
            net = load(ver["solve"]["touchstone"])
            res.artefacts["sparams_png"] = str(
                sparam_plot(
                    net, device.port_map(spec), out / "sparams.png", spec=spec,
                    title=f"{spec.name} -- verified",
                )
            )
            bw = usable_bandwidth_ghz(net, 1, -15.0)
            if bw:
                stage("bandwidth", s11_below_15db_ghz=[round(b, 4) for b in bw])

        # ------------------------------------------------------------ 6
        dxf = write_dxf(best_geom, out / f"{spec.name}.dxf")
        res.artefacts["dxf"] = str(dxf)
        ts = Path(ver["solve"]["touchstone"])
        if ts.exists():
            import shutil

            dst = out / ts.name
            shutil.copy2(ts, dst)
            res.artefacts["touchstone"] = str(dst)
        if ver["solve"].get("project"):
            res.artefacts["hfss_project"] = ver["solve"]["project"]

        # ------------------------------------------------------------ 7
        if write_altium:
            from .export import AltiumWriter

            w = AltiumWriter(origin_mils=altium_origin_mils)
            res.altium = w.write(best_geom, spec)
            stage("altium", ok=res.altium.get("ok"), reason=res.altium.get("preflight"))
    finally:
        solver.close()

    res.elapsed_s = time.time() - t0
    md = write_report(res)
    res.artefacts["report_md"] = str(md)
    (out / "result.json").write_text(
        json.dumps(res.as_dict(), indent=2, default=str), encoding="utf-8"
    )
    res.artefacts["result_json"] = str(out / "result.json")
    return res
