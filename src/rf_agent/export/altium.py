"""Write the optimised layout into a live Altium Designer session.

This goes through eda-agent's DelphiScript bridge, calling it as a Python
library rather than over MCP. That matters for a closed loop: the
optimiser must be able to commit a board without an LLM turn in the
middle of it.

What gets written is *design intent*, not the EM model's polygons:
centrelines as tracks with their real widths, ground vias as vias, and
the coplanar ground as a polygon pour with a clearance rule sized to the
gap the EM model used. Altium then regenerates the same copper, and the
result is routable, net-aware and DRC-clean instead of a frozen blob of
regions.

Two constraints of the bridge shape this file, and both are reported
rather than hidden:

* **The PCB command surface is integer mils.** Coordinates and widths
  quantise to 25.4 um. `quantisation_report` says exactly what that costs
  in trace width and hence in impedance; the DXF export is the
  full-precision copy.
* **`pcb.place_arc` carries no net.** Filleted bends are therefore
  emitted as short track segments, which do carry a net and so keep
  connectivity and DRC intact.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from shapely.geometry import Point

from ..geometry import Arc, Geometry, Path as RFPath, Seg
from ..lines import gcpw
from ..spec import RFSpec

MM_PER_MIL = 0.0254


class AltiumExportError(RuntimeError):
    pass


def mm_to_mil(v: float) -> int:
    return int(round(v / MM_PER_MIL))


# --------------------------------------------------------------------------
# what the mil grid costs
# --------------------------------------------------------------------------


def quantisation_report(geom: Geometry, spec: RFSpec) -> dict:
    """What rounding to the 1-mil grid does to this particular layout.

    Position error is bounded and harmless; width error is not, so it is
    converted into the impedance the fabricated line will actually have.
    """
    st = spec.stackup
    widths = sorted({round(p.width, 6) for path in geom.paths for p in path.prims})
    rows = []
    for w in widths:
        w_mil = max(1, mm_to_mil(w))
        w_snapped = w_mil * MM_PER_MIL
        try:
            z_ideal = gcpw(w, st.gap_mm, st.h, st.er, spec.f0_hz).z0
            z_snap = gcpw(w_snapped, st.gap_mm, st.h, st.er, spec.f0_hz).z0
        except Exception:
            z_ideal = z_snap = float("nan")
        rows.append(
            {
                "width_mm": round(w, 5),
                "width_mil": w_mil,
                "snapped_mm": round(w_snapped, 5),
                "error_um": round((w_snapped - w) * 1000.0, 2),
                "z0_ideal": round(z_ideal, 3),
                "z0_snapped": round(z_snap, 3),
                "z0_shift": round(z_snap - z_ideal, 3),
            }
        )
    return {
        "grid_um": round(MM_PER_MIL * 1000, 1),
        "max_position_error_um": round(MM_PER_MIL * 1000 / 2.0, 2),
        "widths": rows,
        "worst_z0_shift_ohm": (
            round(max((abs(r["z0_shift"]) for r in rows), default=0.0), 3)
        ),
    }


def snap_geometry(geom: Geometry) -> Geometry:
    """A copy of the geometry with widths on the 1-mil grid.

    Use it for a final verification solve so the reported performance is
    the board Altium will hold, not the ideal one the optimiser found.
    Positions are left alone: a half-mil shift on a centreline is far
    below what a trace width change does.
    """
    from shapely.ops import unary_union

    new_paths = []
    for p in geom.paths:
        prims = []
        for pr in p.prims:
            w = max(1, mm_to_mil(pr.width)) * MM_PER_MIL
            prims.append(replace(pr, width=w))
        new_paths.append(RFPath(prims, p.net))

    # Signal copper is not only the routed paths: resistor lands, pads and
    # any other fixed shape live in `trace` too. Rebuilding from the paths
    # alone drops them -- which leaves the lumped resistors bridging bare
    # substrate and destroys isolation in a way that looks like physics.
    routed = unary_union([p.polygon() for p in geom.paths])
    extras = geom.trace.difference(routed)
    trace = unary_union([p.polygon() for p in new_paths] + [extras])
    return Geometry(
        paths=new_paths,
        trace=trace,
        pour=geom.pour,
        vias=geom.vias,
        resistors=geom.resistors,
        ports=geom.ports,
        board=geom.board,
        params=dict(geom.params),
        notes=list(geom.notes) + ["widths snapped to the 1-mil Altium grid"],
    )


# --------------------------------------------------------------------------
# bridge access
# --------------------------------------------------------------------------


def _import_bridge():
    """Import eda-agent, adding its checkout to sys.path if it is not installed.

    Kept optional on purpose: rf-agent is useful on a machine that has
    HFSS and no Altium, and a hard dependency would break that.
    """
    try:
        from eda_agent.bridge import get_bridge  # noqa: F401

        return get_bridge
    except ImportError:
        pass

    candidates = []
    env = os.environ.get("RF_AGENT_EDA_AGENT")
    if env:
        candidates.append(Path(env))
    candidates += [
        Path.home() / "Desktop" / "eda-agent",
        Path.cwd().parent / "eda-agent",
    ]
    for c in candidates:
        src = c / "src"
        if (src / "eda_agent" / "__init__.py").exists():
            sys.path.insert(0, str(src))
            try:
                from eda_agent.bridge import get_bridge  # noqa: F811

                return get_bridge
            except ImportError:
                sys.path.pop(0)
    raise AltiumExportError(
        "eda-agent is not importable. Install it (pip install -e path/to/eda-agent) "
        "or set RF_AGENT_EDA_AGENT to its checkout directory."
    )


# --------------------------------------------------------------------------
# the writer
# --------------------------------------------------------------------------


@dataclass
class WritePlan:
    """Everything that would be sent, resolved to mils. Inspectable dry."""

    tracks: list[dict] = field(default_factory=list)
    vias: list[dict] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    outline: tuple[int, int, int, int] | None = None
    polygons: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "n_tracks": len(self.tracks),
            "n_vias": len(self.vias),
            "nets": self.nets,
            "outline_mils": list(self.outline) if self.outline else None,
            "polygons": self.polygons,
            "rules": self.rules,
        }


class AltiumWriter:
    """Turns a `Geometry` into calls on the live Altium PCB document."""

    def __init__(
        self,
        origin_mils: tuple[int, int] = (2000, 2000),
        layer: str = "TopLayer",
        gnd_net: str = "GND",
        timeout: float = 60.0,
        ground_layer: str = "BottomLayer",
    ):
        self.origin = origin_mils
        self.layer = layer
        # The reference conductor the stackup is built on. Every via drops
        # to it and the top pour's islands are tied together through it.
        self.ground_layer = ground_layer
        self.gnd_net = gnd_net
        self.timeout = timeout
        self._bridge = None

    # -- connection ----------------------------------------------------
    @property
    def bridge(self):
        if self._bridge is None:
            self._bridge = _import_bridge()()
        return self._bridge

    def preflight(self) -> dict:
        """Is there a live Altium with the bridge script polling?"""
        try:
            b = self.bridge
        except AltiumExportError as e:
            return {"ok": False, "reason": str(e), "stage": "import"}

        if not b.is_altium_running():
            return {
                "ok": False,
                "stage": "process",
                "reason": "Altium Designer is not running.",
            }
        if not b.ping():
            return {
                "ok": False,
                "stage": "script",
                "reason": (
                    "Altium is running but the bridge script is not polling. "
                    "In Altium: File > Run Script..., pick Altium_API > "
                    "Dispatcher.pas > StartMCPServer, and Run."
                ),
            }
        try:
            info = b.send_command("pcb.get_board_statistics", {}, timeout=15.0)
        except Exception as e:
            return {
                "ok": False,
                "stage": "pcb",
                "reason": f"bridge is alive but no PCB document responded: {e}",
            }
        return {"ok": True, "board": info}

    # -- geometry -> plan ---------------------------------------------
    def _x(self, mm: float) -> int:
        return self.origin[0] + mm_to_mil(mm)

    def _y(self, mm: float) -> int:
        return self.origin[1] + mm_to_mil(mm)

    @staticmethod
    def _net_groups(geom: Geometry) -> dict[int, str]:
        """Map each path index to the net its copper actually belongs to.

        A Wilkinson's feed, arms and output runs are one continuous piece
        of copper -- writing them as separate nets would have Altium
        report a short at every tee. So connectivity decides the netlist,
        not the generator's internal names for the runs. Each connected
        group takes the name of a path that reaches a port if there is
        one, since that is the name a schematic would use.
        """
        polys = [p.polygon() for p in geom.paths]
        n = len(polys)
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            a, b = find(i), find(j)
            if a != b:
                parent[b] = a

        for i in range(n):
            for j in range(i + 1, n):
                # `intersects` catches shapes that merely touch, which is
                # exactly what two runs meeting at a tee do.
                if polys[i].intersects(polys[j]):
                    union(i, j)

        port_pts = [Point(pt.x, pt.y) for pt in geom.ports]
        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)

        out: dict[int, str] = {}
        for k, (root, members) in enumerate(sorted(groups.items())):
            named = [
                geom.paths[i].net
                for i in members
                if geom.paths[i].net
                and any(polys[i].distance(q) < 1e-6 for q in port_pts)
            ]
            name = named[0] if named else (geom.paths[members[0]].net or f"RF_NET{k + 1}")
            for i in members:
                out[i] = name
        return out

    def plan(self, geom: Geometry, spec: RFSpec) -> WritePlan:
        p = WritePlan()

        x0, y0, x1, y1 = geom.board.bounds
        p.outline = (self._x(x0), self._y(y0), self._x(x1), self._y(y1))

        net_of = self._net_groups(geom)
        nets: list[str] = []
        for idx, path in enumerate(geom.paths):
            net = net_of.get(idx, path.net or "")
            if net and net not in nets:
                nets.append(net)
            for pr in path.prims:
                width = max(1, mm_to_mil(pr.width))
                pts = pr.centreline()
                for a, b in zip(pts, pts[1:]):
                    if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9:
                        continue
                    p.tracks.append(
                        {
                            "x1": self._x(a[0]),
                            "y1": self._y(a[1]),
                            "x2": self._x(b[0]),
                            "y2": self._y(b[1]),
                            "width": width,
                            "layer": self.layer,
                            "net_name": net,
                        }
                    )
        # Drop zero-length segments the mil grid collapsed.
        p.tracks = [
            t for t in p.tracks if t["x1"] != t["x2"] or t["y1"] != t["y2"]
        ]

        if self.gnd_net not in nets:
            nets.append(self.gnd_net)
        p.nets = nets

        for v in geom.vias:
            p.vias.append(
                {
                    "x": self._x(v.x),
                    "y": self._y(v.y),
                    "net": v.net or self.gnd_net,
                    "size": max(1, mm_to_mil(v.pad)),
                    "hole_size": max(1, mm_to_mil(v.drill)),
                }
            )

        # Two pours, and both are load-bearing.
        #
        # The signal layer carries the coplanar ground, poured around the
        # RF nets by the clearance rule below. The traces cut it into
        # several islands -- on this divider, an upper half, a lower half
        # and a pocket between the outputs.
        #
        # The bottom layer carries the ground plane those islands are tied
        # together through, and that every stitching via is drilled down
        # to reach. It is not decoration: the EM model is built on a solid
        # conductor at z=0, so a board without it is not the board that
        # was simulated. Omitting it leaves the vias landing on bare
        # laminate and the top pour in electrically separate pieces.
        p.polygons = [
            {
                "x1": p.outline[0], "y1": p.outline[1],
                "x2": p.outline[2], "y2": p.outline[3],
                "net": self.gnd_net,
                "layer": self.layer,
                "pour_over": False,
            },
            {
                "x1": p.outline[0], "y1": p.outline[1],
                "x2": p.outline[2], "y2": p.outline[3],
                "net": self.gnd_net,
                "layer": self.ground_layer,
                "pour_over": True,
            },
        ]

        gap_mil = max(1, mm_to_mil(spec.stackup.gap_mm))
        p.rules = [
            {
                "name": f"RF_Clearance_{gap_mil}mil",
                "rule_type": "clearance",
                "value": gap_mil,
                "net_scope": "different_nets",
            }
        ]
        return p

    # -- commit --------------------------------------------------------
    def write(
        self,
        geom: Geometry,
        spec: RFSpec,
        *,
        dry_run: bool = False,
        place_pour: bool = True,
        place_vias: bool = True,
        create_rule: bool = True,
    ) -> dict:
        """Send the plan to Altium. `dry_run` resolves it without sending."""
        plan = self.plan(geom, spec)
        quant = quantisation_report(geom, spec)
        result: dict[str, Any] = {
            "plan": plan.summary(),
            "quantisation": quant,
            "dry_run": dry_run,
        }
        if dry_run:
            result["ok"] = True
            return result

        pre = self.preflight()
        result["preflight"] = pre
        if not pre.get("ok"):
            result["ok"] = False
            return result

        b = self.bridge
        steps: list[dict] = []

        def step(name: str, cmd: str, params: dict, timeout: float | None = None):
            try:
                out = b.send_command(cmd, params, timeout=timeout or self.timeout)
                steps.append({"step": name, "ok": True, "result": out})
                return out
            except Exception as e:
                steps.append({"step": name, "ok": False, "error": f"{type(e).__name__}: {e}"})
                return None

        # Pipe-separated, not comma. The bridge splits this list on "|",
        # so a comma-joined string arrives as a single net literally named
        # "RF_IN,GND" -- which then makes every later lookup of "RF_IN" or
        # "GND" miss, and every track, via and the pour lands with no net
        # at all. The copper looks perfect and is electrically inert.
        step("nets", "pcb.create_nets_from_list", {"nets": "|".join(plan.nets)})

        if create_rule:
            for r in plan.rules:
                step(
                    "rule",
                    "pcb.create_design_rule",
                    {
                        "name": r["name"],
                        "rule_type": r["rule_type"],
                        "value": str(r["value"]),
                        "net_scope": r["net_scope"],
                    },
                )

        # Tracks go in one batch: the bridge wraps the whole list in a
        # single PreProcess/PostProcess, so 300 segments cost about what
        # one does.
        if plan.tracks:
            parts = [
                f"{t['x1']},{t['y1']},{t['x2']},{t['y2']},{t['width']},{t['layer']},{t['net_name']}"
                for t in plan.tracks
            ]
            step(
                "tracks",
                "pcb.place_tracks",
                {"tracks": "|".join(parts)},
                timeout=max(self.timeout, 5.0 + 0.05 * len(parts)),
            )

        if place_vias and plan.vias:
            placed, failed = 0, 0
            for v in plan.vias:
                out = step(
                    "via",
                    "pcb.place_via",
                    {
                        "x": str(v["x"]),
                        "y": str(v["y"]),
                        "net": v["net"],
                        "size": str(v["size"]),
                        "hole_size": str(v["hole_size"]),
                        "low_layer": "TopLayer",
                        "high_layer": "BottomLayer",
                    },
                    timeout=20.0,
                )
                placed += 1 if out is not None else 0
                failed += 0 if out is not None else 1
            # Collapse the per-via noise; only the counts are interesting.
            steps = [s for s in steps if s["step"] != "via"]
            steps.append({"step": "vias", "ok": failed == 0, "placed": placed, "failed": failed})

        if place_pour:
            for g in plan.polygons:
                step(
                    f"polygon:{g['layer']}",
                    "pcb.place_polygon_rect",
                    {
                        "x1": str(g["x1"]), "y1": str(g["y1"]),
                        "x2": str(g["x2"]), "y2": str(g["y2"]),
                        "net": g["net"], "layer": g["layer"],
                        "pour_over": "true" if g["pour_over"] else "false",
                    },
                    timeout=120.0,
                )

        step("save", "application.save_all", {}, timeout=120.0)

        result["steps"] = steps
        result["ok"] = all(s.get("ok", False) for s in steps)
        return result
