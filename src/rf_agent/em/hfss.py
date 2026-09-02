"""Build and solve a `Geometry` in HFSS via PyAEDT.

Cross-section, boundaries and setup follow the model that was correlated
on a built X-band GCPW divider:

- substrate box on laminate material, PEC via barrels punched through it;
- top copper (signal + pour) as zero-thickness sheets with a finite
  conductivity boundary carrying the real copper thickness and roughness;
- an air box above the board whose outer faces stay on HFSS's default PEC
  outer boundary -- that is the metal housing, and it is why there is no
  radiation boundary anywhere in this file;
- isolation resistors as lumped RLC sheets bridging the two lands;
- wave ports on the board edges, integration line running from the ground
  plane up to the trace.

Both the wave ports and the lumped RLCs are given their integration line
as explicit start and end points rather than an axis keyword. That
matters: the line has to run from the ground plane at z=0 up to the trace
at z=h. Handed an axis, HFSS spans the whole port face including the air
above it, and the renormalised impedance comes out wrong.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry.base import BaseGeometry

from ..config import Settings, get_settings
from ..geometry import Geometry
from ..spec import RFSpec


class EMError(RuntimeError):
    """A solve that could not be built, could not run, or produced nothing."""


# Everything handed to AEDT is quantised to this many decimal places of a
# millimetre (1 nm). Shapely arithmetic routinely lands on values like
# 0.25000000000000006, and a lumped-RLC current line whose endpoint misses
# its own sheet by 6e-17 mm is rejected outright ("both endpoints of the
# directed line must lie on the boundary face"). 1 nm is far below AEDT's
# own 0.1 nm internal unit and far below any fab tolerance.
_Q = 6


def _q(v: float) -> float:
    return round(float(v), _Q)


def _clear_project(prj: Path) -> None:
    """Remove an AEDT project and everything AEDT keeps beside it."""
    for p in (
        prj,
        prj.with_suffix(".aedt.lock"),
        Path(str(prj) + ".lock"),
        prj.with_suffix(".aedtresults"),
        prj.with_suffix(".aedb"),
        prj.with_suffix(".aedt.bak"),
    ):
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except OSError:
            pass


@dataclass
class SolveResult:
    touchstone: Path
    project: Path | None
    elapsed_s: float
    cached: bool = False
    draft: bool = False
    log: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "touchstone": str(self.touchstone),
            "project": str(self.project) if self.project else None,
            "elapsed_s": round(self.elapsed_s, 1),
            "cached": self.cached,
            "draft": self.draft,
        }


# --------------------------------------------------------------------------
# caching
# --------------------------------------------------------------------------


def _rings(shape: BaseGeometry) -> list[tuple[list, list[list]]]:
    """(exterior, holes) coordinate rings, closing point dropped."""
    if shape.is_empty:
        return []
    geoms = list(shape.geoms) if shape.geom_type.startswith("Multi") else [shape]
    out = []
    for g in geoms:
        if g.geom_type != "Polygon":
            continue
        out.append(
            (
                [(_q(x), _q(y)) for x, y in list(g.exterior.coords)[:-1]],
                [
                    [(_q(x), _q(y)) for x, y in list(r.coords)[:-1]]
                    for r in g.interiors
                ],
            )
        )
    return out


def geometry_digest(geom: Geometry, spec: RFSpec, draft: bool) -> str:
    """Content hash over everything that changes the answer.

    Two trials that differ only in a parameter the geometry rounds away
    hash the same and reuse the solved result -- which on an EM loop is
    the difference between a study that finishes and one that does not.
    """
    em = spec.em.model_dump()
    if draft:
        em["delta_s"] = em.pop("draft_delta_s")
        em["max_passes"] = em.pop("draft_max_passes")
        em["sweep_points"] = em.pop("draft_sweep_points")
    else:
        for k in ("draft_delta_s", "draft_max_passes", "draft_sweep_points"):
            em.pop(k, None)

    payload = {
        "trace": _rings(geom.trace),
        "pour": _rings(geom.pour),
        "vias": sorted((round(v.x, 4), round(v.y, 4), v.drill, v.pad) for v in geom.vias),
        "res": sorted(
            (round(r.x, 4), round(r.y, 4), r.ohms, round(r.gap, 4), r.land, r.axis)
            for r in geom.resistors
        ),
        "ports": [(p.name, round(p.x, 4), round(p.y, 4), p.edge, p.z0) for p in geom.ports],
        "board": [round(c, 4) for c in geom.board.bounds],
        "laminate": spec.stackup.laminate.model_dump(),
        "gap": spec.stackup.gap_mm,
        "f0": spec.f0_ghz,
        "band": list(spec.band_ghz),
        "em": em,
        "draft": draft,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:20]


# --------------------------------------------------------------------------
# solver
# --------------------------------------------------------------------------


class HfssSolver:
    """Builds and solves geometries, reusing one AEDT session across calls."""

    def __init__(self, settings: Settings | None = None, keep_projects: bool = False):
        self.s = settings or get_settings()
        self.s.ensure_dirs()
        # Keeping the .aedt around is useful for the winner and wasteful
        # for the 60 trials before it.
        self.keep_projects = keep_projects
        self._desktop = None

    # -- session -------------------------------------------------------
    def _open_desktop(self):
        if self._desktop is not None:
            return self._desktop
        from ansys.aedt.core import Desktop

        self._desktop = Desktop(
            version=self.s.aedt_version,
            non_graphical=self.s.non_graphical,
            new_desktop=True,
            close_on_exit=True,
        )
        return self._desktop

    def close(self) -> None:
        if self._desktop is not None:
            try:
                self._desktop.release_desktop(close_projects=True, close_on_exit=True)
            except Exception:
                pass
            self._desktop = None

    def __enter__(self) -> "HfssSolver":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- public --------------------------------------------------------
    def solve(
        self,
        geom: Geometry,
        spec: RFSpec,
        *,
        tag: str = "run",
        draft: bool = False,
        force: bool = False,
    ) -> SolveResult:
        """Solve `geom`, returning a touchstone path. Cached by content."""
        digest = geometry_digest(geom, spec, draft)
        n = len(geom.ports)
        cached = self.s.cache_dir / f"{digest}.s{n}p"
        if cached.exists() and not force:
            return SolveResult(touchstone=cached, project=None, elapsed_s=0.0, cached=True, draft=draft)

        # A draft solve exists only to rank a candidate; its project is
        # ~20 MB and there are dozens of them in a study. Keep the
        # full-fidelity ones, which are the results anyone will want to
        # open, and throw the rest away once the touchstone is cached.
        keep = self.keep_projects and not draft

        t0 = time.time()
        run_dir = self.s.runs_dir / f"{tag}_{digest}"
        run_dir.mkdir(parents=True, exist_ok=True)
        snp = run_dir / f"{spec.name}.s{n}p"

        hfss = None
        try:
            hfss = self._build(geom, spec, run_dir, draft)
            setup_name, sweep_name = self._setup(hfss, spec, draft)
            hfss.save_project()
            ok = hfss.analyze_setup(setup_name, cores=self.s.n_cores)
            if not ok:
                raise EMError(f"HFSS reported failure analysing {setup_name}")
            out = hfss.export_touchstone(
                setup=setup_name, sweep=sweep_name, output_file=str(snp)
            )
            if not out or not Path(out).exists():
                raise EMError("solve finished but no touchstone was written")
            snp = Path(out)
            project = Path(hfss.project_file) if keep else None
            hfss.save_project()
        finally:
            if hfss is not None:
                try:
                    hfss.close_project(save=keep)
                except Exception:
                    pass

        self.s.cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snp, cached)
        if not keep:
            shutil.rmtree(run_dir, ignore_errors=True)

        return SolveResult(
            touchstone=cached,
            project=project,
            elapsed_s=time.time() - t0,
            cached=False,
            draft=draft,
        )

    # -- model construction -------------------------------------------
    def _build(self, geom: Geometry, spec: RFSpec, run_dir: Path, draft: bool):
        from ansys.aedt.core import Hfss

        self._open_desktop()
        st = spec.stackup
        h = st.h
        air = spec.em.air_mm

        prj = run_dir / f"{spec.name}.aedt"
        # A project left behind by an earlier attempt at the same geometry
        # would be *opened*, not replaced, and every object created below
        # would land beside a namesake -- which surfaces much later as a
        # boundary assigned to names that no longer mean anything.
        _clear_project(prj)

        hfss = Hfss(
            project=str(prj),
            design="em",
            solution_type="Modal",
            version=self.s.aedt_version,
            non_graphical=self.s.non_graphical,
            new_desktop=False,
            close_on_exit=False,
        )
        hfss.modeler.model_units = "mm"

        mat_name = self._material(hfss, spec)

        x0, y0, x1, y1 = (_q(c) for c in geom.board.bounds)
        bw, bh = _q(x1 - x0), _q(y1 - y0)

        hfss.modeler.create_box(
            [x0, y0, 0.0], [bw, bh, _q(h)], name="Sub", material=mat_name
        )

        absorber = spec.em.absorber
        if absorber:
            t_abs, er_a, tde, mur, tdm = absorber
            abs_name = self._absorber_material(hfss, er_a, tde, mur, tdm)
            hfss.modeler.create_box(
                [x0, y0, h], [bw, bh, air - t_abs], name="Air", material="vacuum"
            )
            hfss.modeler.create_box(
                [x0, y0, h + air - t_abs], [bw, bh, t_abs], name="Absorber",
                material=abs_name,
            )
        else:
            hfss.modeler.create_box(
                [x0, y0, h], [bw, bh, air], name="Air", material="vacuum"
            )

        # --- via barrels ------------------------------------------------
        via_names = []
        for i, v in enumerate(geom.vias):
            o = hfss.modeler.create_cylinder(
                orientation="Z",
                origin=[_q(v.x), _q(v.y), 0.0],
                radius=_q(v.drill / 2.0),
                height=_q(h),
                num_sides=6,
                name=f"V{i}",
                material="pec",
            )
            via_names.append(o.name)
        if via_names:
            hfss.modeler.unite(via_names)
            hfss.modeler.subtract("Sub", via_names[0], keep_originals=True)

        # --- top copper -------------------------------------------------
        trace_sheets = self._sheets(hfss, geom.trace, _q(h), "Trace")
        pour_sheets = self._sheets(hfss, geom.pour, _q(h), "Pour")
        copper = trace_sheets + pour_sheets
        if not copper:
            raise EMError("no copper sheets were created")

        hfss.assign_finite_conductivity(
            assignment=copper,
            material="copper",
            use_thickness=True,
            thickness=f"{st.laminate.copper_mm}mm",
            roughness=f"{st.laminate.roughness_um}um",
            is_two_side=False,
            is_internal=True,
            name="TopCopper",
        )

        self._resistors(hfss, geom, h)
        self._ports(hfss, geom, spec, h)
        return hfss

    def _material(self, hfss, spec: RFSpec) -> str:
        st = spec.stackup
        name = f"{st.laminate.name}_rfagent".replace("/", "_").replace(" ", "_")
        if not hfss.materials.exists_material(name):
            m = hfss.materials.add_material(name)
            m.permittivity = st.er
            m.dielectric_loss_tangent = st.tand
        return name

    def _absorber_material(self, hfss, er, tde, mur, tdm) -> str:
        name = "RFAgentAbsorber"
        if not hfss.materials.exists_material(name):
            m = hfss.materials.add_material(name)
            m.permittivity = er
            m.dielectric_loss_tangent = tde
            m.permeability = mur
            m.magnetic_loss_tangent = tdm
        return name

    def _sheets(self, hfss, shape: BaseGeometry, z: float, base: str) -> list[str]:
        """One covered sheet per polygon, holes subtracted."""
        names: list[str] = []
        for k, (ext, holes) in enumerate(_rings(shape)):
            name = f"{base}_{k}"
            hfss.modeler.create_polyline(
                points=[[x, y, z] for x, y in ext],
                cover_surface=True,
                close_surface=True,
                name=name,
                material="vacuum",
            )
            hole_names = []
            for j, hr in enumerate(holes):
                hn = f"{base}_{k}_h{j}"
                hfss.modeler.create_polyline(
                    points=[[x, y, z] for x, y in hr],
                    cover_surface=True,
                    close_surface=True,
                    name=hn,
                    material="vacuum",
                )
                hole_names.append(hn)
            if hole_names:
                hfss.modeler.subtract(name, hole_names, keep_originals=False)
            names.append(name)
        return names

    def _resistors(self, hfss, geom: Geometry, h: float) -> None:
        """Lumped RLC across each land pair, current flowing land to land."""
        hz = _q(h)
        for i, r in enumerate(geom.resistors):
            # The sheet and the current line are built from the *same*
            # quantised numbers, so the endpoints land exactly on the
            # sheet's edges rather than a rounding error away from them.
            cx, cy = _q(r.x), _q(r.y)
            if r.axis == "y":
                lo, hi = _q(cy - r.gap / 2.0), _q(cy + r.gap / 2.0)
                x0 = _q(cx - r.land / 2.0)
                origin = [x0, lo, hz]
                sizes = [_q(r.land), hi - lo]
                line = [[cx, lo, hz], [cx, hi, hz]]
            else:
                lo, hi = _q(cx - r.gap / 2.0), _q(cx + r.gap / 2.0)
                y0 = _q(cy - r.land / 2.0)
                origin = [lo, y0, hz]
                sizes = [hi - lo, _q(r.land)]
                line = [[lo, cy, hz], [hi, cy, hz]]

            sheet = hfss.modeler.create_rectangle(
                orientation="XY", origin=origin, sizes=sizes,
                name=f"RSheet{i}", material="vacuum",
            )
            bnd = hfss.assign_lumped_rlc_to_sheet(
                assignment=sheet.name,
                start_direction=line,
                name=r.designator or f"Res{i}",
                rlc_type="Parallel",
                resistance=r.ohms,
            )
            if not bnd:
                raise EMError(f"could not assign lumped RLC for {r.designator or i}")

    def _ports(self, hfss, geom: Geometry, spec: RFSpec, h: float) -> None:
        """One wave port per board-edge port."""
        st = spec.stackup
        # A port face has to be wide and tall enough to enclose the line's
        # fields, and small enough that it neither reaches the cavity
        # sidewalls nor touches the next port on the same edge. Two
        # overlapping wave-port sheets do not fail at assignment time --
        # they fail during the solve, which is an expensive way to find out.
        w_line = max((p.width for path in geom.paths for p in path.prims), default=0.5)
        pw = spec.em.port_width_mm or max(6.0 * (w_line + 2 * st.gap_mm), 8.0 * h)

        span = {"+x": geom.height, "-x": geom.height, "+y": geom.width, "-y": geom.width}
        for edge in {p.edge for p in geom.ports}:
            same = [p for p in geom.ports if p.edge == edge]
            coord = (lambda q: q.y) if edge in ("+x", "-x") else (lambda q: q.x)
            vals = sorted(coord(p) for p in same)
            gaps = [b - a for a, b in zip(vals, vals[1:])]
            if gaps:
                # 0.9 of the closest spacing leaves a visible sliver
                # between neighbouring port faces.
                pw = min(pw, 0.9 * min(gaps))
            pw = min(pw, span[edge] * 0.9)

        ph = spec.em.port_height_mm or max(6.0 * h, h + 1.0)
        ph = min(ph, h + spec.em.air_mm)
        if pw < w_line + 2 * st.gap_mm:
            raise EMError(
                f"port face would be {pw:.3f} mm wide, narrower than the line and its "
                f"gaps ({w_line + 2 * st.gap_mm:.3f} mm). Space the ports further apart "
                f"or set em.port_width_mm explicitly."
            )

        for p in geom.ports:
            # PyAEDT's `orientation` names the plane the sheet lies in,
            # not the axis normal to it (HFSS's own WhichAxis). Only XY,
            # YZ and ZX exist; anything else silently becomes ZX.
            if p.edge in ("+x", "-x"):
                plane = "YZ"  # sizes are [Y extent, Z extent]
                origin = [_q(p.x), _q(p.y - pw / 2.0), 0.0]
                sizes = [_q(pw), _q(ph)]
            else:
                plane = "ZX"  # sizes are [Z extent, X extent]
                origin = [_q(p.x - pw / 2.0), _q(p.y), 0.0]
                sizes = [_q(ph), _q(pw)]

            sheet = hfss.modeler.create_rectangle(
                orientation=plane, origin=origin, sizes=sizes,
                name=f"PT{p.name}", material="vacuum",
            )
            bnd = hfss.wave_port(
                assignment=sheet.name,
                integration_line=[[_q(p.x), _q(p.y), 0.0], [_q(p.x), _q(p.y), _q(h)]],
                modes=1,
                impedance=p.z0,
                name=p.name,
                renormalize=True,
                deembed=0,
                characteristic_impedance="Zpi",
            )
            if not bnd:
                raise EMError(f"could not assign wave port {p.name}")

    # -- analysis setup -------------------------------------------------
    def _setup(self, hfss, spec: RFSpec, draft: bool) -> tuple[str, str]:
        em = spec.em
        delta = em.draft_delta_s if draft else em.delta_s
        passes = em.draft_max_passes if draft else em.max_passes
        npts = em.draft_sweep_points if draft else em.sweep_points

        setup = hfss.create_setup(
            name="Setup1",
            Frequency=f"{spec.f0_ghz}GHz",
            MaxDeltaS=delta,
            MaximumPasses=passes,
            MinimumPasses=2,
            MinimumConvergedPasses=em.min_converged_passes,
            BasisOrder=1,
        )
        lo, hi = spec.band_ghz
        # Solve wider than the requirement: a resonance parked just
        # outside the band is a design problem, and a sweep that stops at
        # the band edge cannot see it.
        margin = (hi - lo) * 0.75
        setup.create_frequency_sweep(
            unit="GHz",
            name="Sweep",
            start_frequency=max(0.1, lo - margin),
            stop_frequency=hi + margin,
            num_of_freq_points=npts,
            sweep_type=em.sweep_type,
            save_fields=False,
        )
        return setup.name, "Sweep"
