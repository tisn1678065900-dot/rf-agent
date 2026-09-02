"""Look at the layout before spending a solve on it.

A PNG is the cheapest possible check that the generator drew what it
thinks it drew. It is also what an LLM client can actually read back, so
the agent can inspect its own candidate instead of trusting the numbers.
"""

from __future__ import annotations

from pathlib import Path

from shapely.geometry.base import BaseGeometry

from .geometry import Geometry


def _patches(ax, shape: BaseGeometry, **kw):
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MPath

    if shape.is_empty:
        return
    geoms = list(shape.geoms) if shape.geom_type.startswith("Multi") else [shape]
    for g in geoms:
        if g.geom_type != "Polygon":
            continue
        verts, codes = [], []
        for ring in [g.exterior, *g.interiors]:
            pts = list(ring.coords)
            verts.extend(pts)
            codes.extend([MPath.MOVETO] + [MPath.LINETO] * (len(pts) - 2) + [MPath.CLOSEPOLY])
        ax.add_patch(PathPatch(MPath(verts, codes), **kw))


def preview(geom: Geometry, out_png: str | Path, title: str = "", dpi: int = 160) -> Path:
    """Top view: pour, signal copper, vias, resistors, port planes."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x0, y0, x1, y1 = geom.board.bounds
    w, h = x1 - x0, y1 - y0
    scale = 7.0 / max(w, h)
    fig, ax = plt.subplots(figsize=(max(3.2, w * scale), max(3.2, h * scale)))

    _patches(ax, geom.board, facecolor="#101418", edgecolor="#3d4852", lw=1.0, zorder=0)
    _patches(ax, geom.pour, facecolor="#1f6f5c", edgecolor="none", zorder=1)
    _patches(ax, geom.trace, facecolor="#f0a500", edgecolor="none", zorder=3)

    for v in geom.vias:
        ax.add_patch(
            plt.Circle((v.x, v.y), v.pad / 2.0, facecolor="#8a5a2b", edgecolor="none", zorder=2)
        )
        ax.add_patch(
            plt.Circle((v.x, v.y), v.drill / 2.0, facecolor="#2b2b2b", edgecolor="none", zorder=2)
        )

    for r in geom.resistors:
        dx, dy = (r.land / 2.0, r.gap / 2.0) if r.axis == "y" else (r.gap / 2.0, r.land / 2.0)
        ax.add_patch(
            plt.Rectangle(
                (r.x - dx, r.y - dy), 2 * dx, 2 * dy,
                facecolor="#c0392b", edgecolor="none", zorder=4,
            )
        )
        ax.annotate(
            f"{r.designator} {r.ohms:g}Ω", (r.x, r.y), color="#ffb3a7",
            fontsize=6, ha="center", va="bottom", zorder=6,
            xytext=(0, r.gap / 2.0 + 0.2), textcoords="offset points",
        )

    for p in geom.ports:
        ax.plot([p.x], [p.y], marker="s", ms=5, color="#3aa3ff", zorder=5)
        ax.annotate(
            p.name, (p.x, p.y), color="#8fd0ff", fontsize=7, zorder=6,
            ha="right" if p.edge == "+x" else "left",
            xytext=(-6 if p.edge == "+x" else 6, 0), textcoords="offset points",
        )

    ax.set_xlim(x0 - 0.5, x1 + 0.5)
    ax.set_ylim(y0 - 0.5, y1 + 0.5)
    ax.set_aspect("equal")
    ax.set_facecolor("#0b0e11")
    fig.patch.set_facecolor("#0b0e11")
    for s in ax.spines.values():
        s.set_color("#3d4852")
    ax.tick_params(colors="#8899a6", labelsize=6)
    ax.set_xlabel("x [mm]", color="#8899a6", fontsize=7)
    ax.set_ylabel("y [mm]", color="#8899a6", fontsize=7)
    if title:
        ax.set_title(title, color="#dfe6ec", fontsize=9)

    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out, dpi=dpi, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out


def sparam_plot(
    net, port_map: dict[str, list[int]], out_png: str | Path, spec=None, title: str = ""
) -> Path:
    """S11 / through / isolation against the spec limits."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    f = net.f / 1e9
    inp = port_map["input"][0] - 1
    outs = [o - 1 for o in port_map["outputs"]]

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.plot(f, 20 * np.log10(np.abs(net.s[:, inp, inp]) + 1e-30), lw=1.8,
            color="#3aa3ff", label="S11")
    for o in outs:
        ax.plot(f, 20 * np.log10(np.abs(net.s[:, o, inp]) + 1e-30), lw=1.2,
                color="#f0a500", alpha=0.85,
                label="through" if o == outs[0] else None)
    if len(outs) >= 2:
        ax.plot(f, 20 * np.log10(np.abs(net.s[:, outs[0], outs[1]]) + 1e-30), lw=1.2,
                color="#2ecc71", label="isolation")

    if spec is not None:
        lo, hi = spec.band_ghz
        ax.axvspan(lo, hi, color="#3aa3ff", alpha=0.07, lw=0)
        for t in spec.targets:
            if t.metric in ("s11_db", "isolation_db"):
                b0, b1 = spec.band_for(t)
                ax.plot([b0, b1], [t.limit, t.limit], ls="--", lw=1.0,
                        color="#ff5c5c" if t.metric == "s11_db" else "#7bed9f")

    ax.set_xlabel("frequency [GHz]")
    ax.set_ylabel("dB")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8)
    if title:
        ax.set_title(title, fontsize=10)
    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out
