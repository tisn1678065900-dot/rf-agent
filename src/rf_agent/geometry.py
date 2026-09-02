"""Layout primitives shared by the EM model, the DXF writer and Altium.

The important design choice here: a routed trace is kept as *primitives*
(straight segments and circular arcs with a width), not only as the
shapely polygon they sweep out. HFSS wants the polygon; Altium wants the
primitives. Deriving the polygon from the primitives -- rather than
trying to recover arcs from a polygon later -- is what lets the same
geometry go into the solver and come out of the optimiser as real,
DRC-clean Altium tracks and arcs instead of a faceted copper blob.

All coordinates are millimetres in board-local space, origin wherever the
device generator puts it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Literal, Sequence

from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

XY = tuple[float, float]


# --------------------------------------------------------------------------
# routed primitives
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Seg:
    """A straight track of constant width."""

    p0: XY
    p1: XY
    width: float

    @property
    def length(self) -> float:
        return math.hypot(self.p1[0] - self.p0[0], self.p1[1] - self.p0[1])

    def centreline(self) -> list[XY]:
        return [self.p0, self.p1]


@dataclass(frozen=True)
class Arc:
    """A circular arc track. Angles in radians, CCW positive."""

    center: XY
    radius: float
    a0: float
    a1: float
    width: float

    @property
    def sweep(self) -> float:
        return self.a1 - self.a0

    @property
    def length(self) -> float:
        return abs(self.sweep) * self.radius

    def point_at(self, t: float) -> XY:
        a = self.a0 + self.sweep * t
        return (
            self.center[0] + self.radius * math.cos(a),
            self.center[1] + self.radius * math.sin(a),
        )

    def centreline(self, nseg: int | None = None) -> list[XY]:
        if nseg is None:
            # ~4.5 deg facets: 4.6 um sag on a 1.5 mm radius, far below any
            # fab tolerance and invisible at X band.
            nseg = max(2, int(math.ceil(abs(self.sweep) / math.radians(4.5))))
        return [self.point_at(k / nseg) for k in range(nseg + 1)]


Primitive = Seg | Arc


@dataclass
class Path:
    """One routed run: an ordered chain of segments and arcs."""

    prims: list[Primitive] = field(default_factory=list)
    net: str = ""

    @property
    def length(self) -> float:
        return sum(p.length for p in self.prims)

    def centreline(self) -> list[XY]:
        pts: list[XY] = []
        for p in self.prims:
            for q in p.centreline():
                if not pts or (q[0] - pts[-1][0]) ** 2 + (q[1] - pts[-1][1]) ** 2 > 1e-14:
                    pts.append(q)
        return pts

    def polygon(self) -> BaseGeometry:
        """The copper this path actually covers."""
        parts = []
        for p in self.prims:
            pts = p.centreline()
            if len(pts) < 2:
                continue
            parts.append(
                LineString(pts).buffer(
                    p.width / 2.0, cap_style=2, join_style=1, quad_segs=8
                )
            )
        return unary_union(parts) if parts else Polygon()


def route(points: Sequence[XY], width: float, radius: float, net: str = "") -> Path:
    """Turn a polyline into a filleted Path of segments and arcs.

    Every interior corner becomes a tangent circular arc of centreline
    radius `radius`, shrunk automatically when the adjacent runs are too
    short to hold it. A corner too tight to fillet at all is left square.
    """
    pts = [tuple(map(float, p)) for p in points]
    if radius <= 0 or len(pts) < 3:
        return Path([Seg(pts[i], pts[i + 1], width) for i in range(len(pts) - 1)], net)

    prims: list[Primitive] = []
    cursor = pts[0]

    for i in range(1, len(pts) - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        v1 = (b[0] - a[0], b[1] - a[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        l1, l2 = math.hypot(*v1), math.hypot(*v2)
        if l1 < 1e-12 or l2 < 1e-12:
            continue
        u1 = (v1[0] / l1, v1[1] / l1)
        u2 = (v2[0] / l2, v2[1] / l2)
        cross = u1[0] * u2[1] - u1[1] * u2[0]
        dot = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
        theta = math.atan2(abs(cross), dot)  # turn angle
        if theta < 1e-9 or abs(theta - math.pi) < 1e-9:
            continue

        # Trim back from the corner by t on both legs; shrink the radius
        # rather than overrun a short leg.
        t = radius * math.tan(theta / 2.0)
        # leave a hair of straight line so consecutive arcs never merge
        t = min(t, l1 * 0.999, l2 * 0.999)
        r = t / math.tan(theta / 2.0)

        p1 = (b[0] - u1[0] * t, b[1] - u1[1] * t)
        p2 = (b[0] + u2[0] * t, b[1] + u2[1] * t)

        if math.hypot(p1[0] - cursor[0], p1[1] - cursor[1]) > 1e-9:
            prims.append(Seg(cursor, p1, width))

        sgn = 1.0 if cross > 0 else -1.0
        n = (-u1[1] * sgn, u1[0] * sgn)  # unit normal toward the arc centre
        o = (p1[0] + n[0] * r, p1[1] + n[1] * r)
        a0 = math.atan2(p1[1] - o[1], p1[0] - o[0])
        a1 = math.atan2(p2[1] - o[1], p2[0] - o[0])
        da = a1 - a0
        while da > math.pi:
            da -= 2 * math.pi
        while da < -math.pi:
            da += 2 * math.pi
        prims.append(Arc(o, r, a0, a0 + da, width))
        cursor = p2

    if math.hypot(pts[-1][0] - cursor[0], pts[-1][1] - cursor[1]) > 1e-9:
        prims.append(Seg(cursor, pts[-1], width))
    return Path(prims, net)


# --------------------------------------------------------------------------
# device geometry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Via:
    x: float
    y: float
    drill: float
    pad: float
    net: str = "GND"


@dataclass(frozen=True)
class Resistor:
    """A chip resistor bridging two lands, modelled in HFSS as a lumped RLC."""

    x: float
    y: float
    ohms: float
    gap: float  # solder gap between the lands (current path length)
    land: float  # land size along the other axis
    axis: Literal["x", "y"] = "y"  # direction current flows
    designator: str = ""
    footprint: str = "0402"


@dataclass(frozen=True)
class Port:
    """A wave port on a board edge."""

    name: str
    x: float
    y: float
    edge: Literal["+x", "-x", "+y", "-y"]
    z0: float = 50.0


@dataclass
class Geometry:
    """Everything the EM model and the PCB writer need about one candidate."""

    paths: list[Path]
    trace: BaseGeometry  # union of path copper + resistor lands
    pour: BaseGeometry  # top-side ground pour
    vias: list[Via]
    resistors: list[Resistor]
    ports: list[Port]
    board: Polygon
    params: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def width(self) -> float:
        x0, _, x1, _ = self.board.bounds
        return x1 - x0

    @property
    def height(self) -> float:
        _, y0, _, y1 = self.board.bounds
        return y1 - y0

    def summary(self) -> dict:
        return {
            "board_mm": [round(self.width, 3), round(self.height, 3)],
            "trace_area_mm2": round(self.trace.area, 3),
            "pour_area_mm2": round(self.pour.area, 3),
            "n_vias": len(self.vias),
            "n_resistors": len(self.resistors),
            "n_ports": len(self.ports),
            "routed_length_mm": round(sum(p.length for p in self.paths), 3),
            "params": {k: round(v, 4) for k, v in self.params.items()},
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------
# pour + stitching
# --------------------------------------------------------------------------


def make_pour(
    board: Polygon,
    trace: BaseGeometry,
    gap: float,
    keepouts: Iterable[BaseGeometry] = (),
    band: float | None = None,
    min_via_clear: float = 0.35,
) -> BaseGeometry:
    """Carve the coplanar ground pour out of the board.

    `band` limits the pour to a strip of that half-width around the
    traces; None pours the whole board.

    Islands are filtered by whether they can hold a ground via, not by
    area. That is the physical criterion: copper that cannot be stitched
    is copper that floats between the top pour and the bottom ground and
    resonates as a patch. `min_via_clear` is the via pad half-width plus
    its clearance.
    """
    keep = trace.buffer(gap, join_style=2, mitre_limit=8)
    for k in keepouts:
        keep = keep.union(k)
    pour = board.difference(keep)
    if band:
        pour = pour.intersection(trace.buffer(band, join_style=2, mitre_limit=8))
    parts = list(pour.geoms) if pour.geom_type.startswith("Multi") else [pour]
    kept = [q for q in parts if not q.buffer(-min_via_clear).is_empty]
    return unary_union(kept) if kept else Polygon()


def fence_vias(
    paths: Iterable[Path],
    pour: BaseGeometry,
    *,
    pitch: float,
    drill: float,
    pad: float,
    offset: float | None = None,
    edge_clear: float = 0.90,
    existing: list[Via] | None = None,
) -> list[Via]:
    """Two rows of ground vias tracking each routed centreline.

    By default the row offset is derived per path as
    ``trace_half_width + edge_clear``, so a narrow high-impedance arm gets
    its fence as close as the wide feed line does rather than inheriting
    an offset sized for some other trace. Pass `offset` to override with a
    fixed distance (used for the board-edge ring, where there is no trace).
    """
    vias: list[Via] = list(existing or [])
    clear = pad / 2.0 + 0.05

    def add(x: float, y: float) -> None:
        if not pour.contains(Point(x, y).buffer(clear)):
            return
        for v in vias:
            if (v.x - x) ** 2 + (v.y - y) ** 2 < (pitch * 0.75) ** 2:
                return
        vias.append(Via(round(x, 4), round(y, 4), drill, pad))

    for path in paths:
        pts = path.centreline()
        if len(pts) < 2:
            continue
        if offset is not None:
            off = offset
        else:
            w = max((p.width for p in path.prims), default=0.0)
            off = w / 2.0 + edge_clear
        ls = LineString(pts)
        n = max(1, int(round(ls.length / pitch)))
        for k in range(n + 1):
            s = k * ls.length / n
            a = ls.interpolate(max(0.0, s - 0.05))
            b = ls.interpolate(min(ls.length, s + 0.05))
            dx, dy = b.x - a.x, b.y - a.y
            m = math.hypot(dx, dy)
            if m < 1e-9:
                continue
            nx, ny = -dy / m, dx / m
            c = ls.interpolate(s)
            add(c.x + off * nx, c.y + off * ny)
            add(c.x - off * nx, c.y - off * ny)
    return vias


def fill_vias(
    pour: BaseGeometry,
    *,
    pitch: float,
    drill: float,
    pad: float,
    existing: list[Via] | None = None,
) -> list[Via]:
    """Triangular-grid stitching over the pour interior.

    Not optional on thin high-Dk laminate: a fence along the traces alone
    leaves the pour interior free to behave as a patch resonator, which on
    the reference divider cost 3.9 dB at 7.34 GHz.
    """
    vias: list[Via] = list(existing or [])
    clear = pad / 2.0 + 0.05
    x0, y0, x1, y1 = pour.bounds
    dy = pitch * math.sqrt(3.0) / 2.0
    row = 0
    y = y0
    while y <= y1:
        x = x0 + (pitch / 2.0 if row % 2 else 0.0)
        while x <= x1:
            p = Point(x, y)
            if pour.contains(p.buffer(clear)):
                if all(
                    (v.x - x) ** 2 + (v.y - y) ** 2 >= (pitch * 0.6) ** 2 for v in vias
                ):
                    vias.append(Via(round(x, 4), round(y, 4), drill, pad))
            x += pitch
        y += dy
        row += 1
    return vias


# --------------------------------------------------------------------------
# DRC
# --------------------------------------------------------------------------


def check_drc(geom: Geometry, fab) -> list[str]:
    """Manufacturability violations, cheapest checks first.

    Runs before any solve: a geometry that cannot be built should never
    cost an HFSS licence-second.
    """
    bad: list[str] = []

    for i, p in enumerate(geom.paths):
        for pr in p.prims:
            if pr.width < fab.min_trace_mm - 1e-9:
                bad.append(
                    f"path{i} width {pr.width:.4f} < min_trace {fab.min_trace_mm}"
                )
                break
            if isinstance(pr, Arc) and pr.radius < pr.width * 0.5:
                bad.append(
                    f"path{i} bend radius {pr.radius:.4f} tighter than half its width"
                )
                break

    if geom.trace.is_empty:
        bad.append("trace copper is empty")
    if not geom.trace.is_valid:
        bad.append("trace copper is not a valid polygon (self-intersection)")

    # Trace-to-pour: the coplanar gap, measured rather than assumed.
    if not geom.pour.is_empty and not geom.trace.is_empty:
        d = geom.trace.distance(geom.pour)
        if d < fab.min_gap_mm - 1e-6:
            bad.append(f"trace-to-pour gap {d:.4f} < min_gap {fab.min_gap_mm}")

    # Vias: annular ring, clearance to signal copper, inside the board.
    ring = None
    for v in geom.vias:
        ring = (v.pad - v.drill) / 2.0
        if ring < fab.min_annular_ring_mm - 1e-9:
            bad.append(f"via annular ring {ring:.4f} < min {fab.min_annular_ring_mm}")
            break
    if any(v.drill < fab.min_via_drill_mm - 1e-9 for v in geom.vias):
        bad.append(f"via drill below min_via_drill {fab.min_via_drill_mm}")

    if geom.vias and not geom.trace.is_empty:
        worst = min(
            geom.trace.distance(Point(v.x, v.y)) - v.pad / 2.0 for v in geom.vias
        )
        if worst < fab.min_via_to_copper_mm - 1e-6:
            bad.append(
                f"via-to-signal clearance {worst:.4f} < min {fab.min_via_to_copper_mm}"
            )

    shrunk = geom.board.buffer(-fab.edge_clearance_mm)
    if not geom.trace.is_empty and not shrunk.contains(geom.trace):
        # Ports legitimately run to the edge; only flag copper that leaves
        # the board entirely.
        if not geom.board.buffer(1e-6).contains(geom.trace):
            bad.append("signal copper extends outside the board outline")

    return bad


def bbox(*shapes: BaseGeometry) -> Polygon:
    xs0, ys0, xs1, ys1 = zip(*(s.bounds for s in shapes if not s.is_empty))
    return box(min(xs0), min(ys0), max(xs1), max(ys1))
