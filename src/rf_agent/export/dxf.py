"""DXF writer -- the exact, archival copy of the optimised layout.

Unlike the Altium path this loses nothing: polygons go out at full
double precision on named layers, which is what a fab house or a
mechanical CAD merge (an enclosure drawing, say) actually wants.
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
from shapely.geometry.base import BaseGeometry

from ..geometry import Geometry

LAYERS = {
    "trace": ("RF_TOP_SIGNAL", 2),
    "pour": ("RF_TOP_POUR", 3),
    "board": ("RF_BOARD_OUTLINE", 7),
    "via": ("RF_VIA", 1),
    "res": ("RF_RESISTOR", 5),
    "port": ("RF_PORT", 4),
}


def _add_poly(msp, shape: BaseGeometry, layer: str) -> int:
    if shape.is_empty:
        return 0
    geoms = list(shape.geoms) if shape.geom_type.startswith("Multi") else [shape]
    n = 0
    for g in geoms:
        if g.geom_type != "Polygon":
            continue
        msp.add_lwpolyline(list(g.exterior.coords), close=True, dxfattribs={"layer": layer})
        n += 1
        for ring in g.interiors:
            msp.add_lwpolyline(list(ring.coords), close=True, dxfattribs={"layer": layer})
            n += 1
    return n


def write_dxf(geom: Geometry, path: str | Path, origin: tuple[float, float] = (0.0, 0.0)) -> Path:
    """Write the layout to `path`. `origin` shifts board coords into world."""
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4  # millimetres
    for _, (name, color) in LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=color)
    msp = doc.modelspace()

    dx, dy = origin
    from shapely import affinity

    def moved(s: BaseGeometry) -> BaseGeometry:
        return affinity.translate(s, dx, dy) if (dx or dy) else s

    _add_poly(msp, moved(geom.board), LAYERS["board"][0])
    _add_poly(msp, moved(geom.pour), LAYERS["pour"][0])
    _add_poly(msp, moved(geom.trace), LAYERS["trace"][0])

    for v in geom.vias:
        msp.add_circle((v.x + dx, v.y + dy), v.drill / 2.0, dxfattribs={"layer": LAYERS["via"][0]})
        msp.add_circle((v.x + dx, v.y + dy), v.pad / 2.0, dxfattribs={"layer": LAYERS["via"][0]})

    for r in geom.resistors:
        hw = r.land / 2.0 if r.axis == "y" else r.gap / 2.0
        hh = r.gap / 2.0 if r.axis == "y" else r.land / 2.0
        msp.add_lwpolyline(
            [
                (r.x - hw + dx, r.y - hh + dy),
                (r.x + hw + dx, r.y - hh + dy),
                (r.x + hw + dx, r.y + hh + dy),
                (r.x - hw + dx, r.y + hh + dy),
            ],
            close=True,
            dxfattribs={"layer": LAYERS["res"][0]},
        )
        msp.add_text(
            f"{r.designator} {r.ohms:g}R",
            height=0.35,
            dxfattribs={"layer": LAYERS["res"][0]},
        ).set_placement((r.x + dx, r.y + hh + 0.15 + dy))

    for p in geom.ports:
        msp.add_text(
            p.name, height=0.5, dxfattribs={"layer": LAYERS["port"][0]}
        ).set_placement((p.x + dx, p.y + dy))

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out)
    return out
