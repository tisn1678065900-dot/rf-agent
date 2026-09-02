"""Wilkinson power divider, 1:2 and 1:4, in grounded coplanar waveguide.

Topology is a binary tree of two-way Wilkinson cells laid out along -x:
the input enters on the +x edge, each cell splits vertically into two
quarter-wave arms bridged by an isolation resistor, and the outputs leave
on the -x edge. That is the arrangement the reference X-band divider uses,
so the generated geometry drops into a cavity of the same shape.

Every dimension comes from either the analytic line model (widths,
quarter-wave lengths) or an explicit parameter. Nothing is a magic
number carried over from one particular design.
"""

from __future__ import annotations

import math

from shapely.geometry import box
from shapely.ops import unary_union

from ..geometry import (
    Geometry,
    Port,
    Resistor,
    fence_vias,
    fill_vias,
    make_pour,
    route,
)
from ..lines import gcpw, quarter_wave_len, width_for_z0
from ..spec import RFSpec
from .base import Device, ParamRange

# Fixed layout constants. These are drafting conventions, not electrical
# degrees of freedom -- exposing them to the optimiser would spend solves
# on choices a router should make.
BEND_R_LOW = 1.50  # centreline bend radius on the wide (50 ohm) line [mm]
BEND_R_HIGH = 0.50  # ... on the narrow (70 ohm) arm
FEED_MM = 3.00  # straight run from the input tee to the port plane
TAIL_MM = 3.00  # straight run from the last tee to the output port plane
MARGIN_Y = 3.50  # copper-to-board-edge margin above/below the outer outputs
RES_BODY_CLEAR = 0.30  # pour clearance around the chip resistor body


class Wilkinson(Device):
    key = "wilkinson"
    title = "Wilkinson power divider"

    # ------------------------------------------------------------------
    # parameters
    # ------------------------------------------------------------------
    @classmethod
    def param_space(cls, spec: RFSpec) -> dict[str, ParamRange]:
        st = spec.stackup
        f = spec.f0_hz
        z_arm = spec.z0 * math.sqrt(2.0)

        w_arm, lam4 = quarter_wave_len(z_arm, st.gap_mm, st.h, st.er, f)

        # The drawn arm always runs longer than the ideal quarter wave:
        # the tee and the two bends add electrical length that the
        # straight-line model does not see. On the reference divider the drawn
        # arm came out ~19% over lambda/4 after HFSS tuning, so the seed
        # is placed above the analytic value and the range spans both
        # sides of it generously.
        l_seed = lam4 * 1.15
        w_min = max(st.fab.min_trace_mm, 0.5 * w_arm)
        w_max = w_arm * 1.60
        if w_min >= w_max:
            # The fab's minimum trace is wider than the arm this
            # impedance needs. No amount of searching fixes that -- say
            # so here rather than letting it surface as an opaque
            # "low must be smaller than high" from the sampler.
            raise ValueError(
                f"a {z_arm:.1f} ohm arm on this stackup is {w_arm:.4f} mm wide, but "
                f"the fab minimum trace is {st.fab.min_trace_mm} mm. Widen the "
                f"coplanar gap, use a thicker laminate, or relax min_trace_mm."
            )

        return {
            "l_arm": ParamRange(
                lo=lam4 * 0.80,
                hi=lam4 * 1.45,
                seed=l_seed,
                description="drawn length of each quarter-wave arm, tee to resistor land",
            ),
            "w_arm": ParamRange(
                lo=w_min,
                hi=w_max,
                seed=w_arm,
                description=f"arm trace width (analytic {z_arm:.1f} ohm = {w_arm:.4f} mm)",
            ),
            "r_sep": ParamRange(
                lo=max(0.7, st.gap_mm * 2 + 0.3),
                hi=2.4,
                seed=1.10,
                description="centre-to-centre arm separation at the isolation resistor",
            ),
            "r_ohms": ParamRange(
                lo=60.0,
                hi=160.0,
                seed=2.0 * spec.z0,
                unit="ohm",
                description="isolation resistor (ideal 2*Z0)",
            ),
        }

    # ------------------------------------------------------------------
    # geometry
    # ------------------------------------------------------------------
    @classmethod
    def build(cls, spec: RFSpec, params: dict[str, float]) -> Geometry:
        st = spec.stackup
        f = spec.f0_hz
        stages = int(math.log2(spec.n_way))
        if stages not in (1, 2):
            raise NotImplementedError(
                f"n_way={spec.n_way} needs {stages} stages; v1 draws 1:2 and 1:4 only"
            )

        p = dict(cls.seed_params(spec))
        p.update(params)

        l_arm = float(p["l_arm"])
        w_arm = float(p["w_arm"])
        r_sep = float(p["r_sep"])
        r_ohms = float(p["r_ohms"])

        w_main = width_for_z0(spec.z0, st.gap_mm, st.h, st.er, f)
        land = float(p.get("res_land", 0.60))  # 0402 land
        d = r_sep / 2.0

        # Vertical geometry. `out_pitch` is the spacing inside an output
        # pair; `spread` is the half-separation of the two sub-trees of a
        # 1:4. Both default to something routable and can be pinned by the
        # caller when a housing dictates the connector positions.
        out_pitch = float(p.get("out_pitch", max(4.0 * r_sep, 6.0)))
        spread = float(p.get("spread", max(out_pitch * 1.6, 8.0)))

        # X positions, right (input) to left (outputs). Each stage needs
        # l_arm of run plus room for the following bend.
        stage_pitch = l_arm + BEND_R_LOW * 2.0 + 1.0
        t_x = [stage_pitch * (stages - 1 - k) + TAIL_MM for k in range(stages)]
        a_x = [t - (l_arm - d) for t in t_x]
        x_in = t_x[0] + FEED_MM
        x_out = a_x[-1] - TAIL_MM

        paths = []
        resistors = []
        keepouts = []
        lands = []
        out_ys: list[float] = []

        # --- input feed ------------------------------------------------
        paths.append(route([(x_in, 0.0), (t_x[0], 0.0)], w_main, BEND_R_LOW, "RF_IN"))

        def cell(depth: int, y_c: float, tag: str) -> None:
            """One two-way Wilkinson cell centred on y_c, feeding the next."""
            tx, ax = t_x[depth], a_x[depth]
            last = depth == stages - 1
            # Where each output of this cell has to end up vertically.
            if last:
                child_off = out_pitch / 2.0
            else:
                child_off = spread / 2.0

            for s in (+1, -1):
                y_arm = y_c + s * d
                # quarter-wave arm: up out of the tee, then along -x
                paths.append(
                    route(
                        [(tx, y_c), (tx, y_arm), (ax, y_arm)],
                        w_arm,
                        BEND_R_HIGH,
                        f"{tag}{'H' if s > 0 else 'L'}",
                    )
                )
                y_child = y_c + s * child_off
                if last:
                    paths.append(
                        route(
                            [(ax, y_arm), (ax, y_child), (x_out, y_child)],
                            w_main,
                            BEND_R_LOW,
                            f"RF_OUT{len(out_ys) + 1}",
                        )
                    )
                    out_ys.append(y_child)
                else:
                    nx = t_x[depth + 1]
                    paths.append(
                        route(
                            [(ax, y_arm), (ax, y_child), (nx, y_child)],
                            w_main,
                            BEND_R_LOW,
                            f"{tag}LINK{'H' if s > 0 else 'L'}",
                        )
                    )

            # resistor lands + the chip bridging them
            for s in (+1, -1):
                lands.append(
                    box(
                        ax - land / 2.0,
                        y_c + s * d - land / 2.0,
                        ax + land / 2.0,
                        y_c + s * d + land / 2.0,
                    )
                )
            resistors.append(
                Resistor(
                    x=ax,
                    y=y_c,
                    ohms=r_ohms,
                    gap=r_sep - land,
                    land=land,
                    axis="y",
                    designator=f"R{len(resistors) + 1}",
                )
            )
            # Keep the pour off the chip: the two lands plus the body
            # between them, grown by the solder clearance.
            keepouts.append(
                box(
                    ax - land / 2.0 - RES_BODY_CLEAR,
                    y_c - (d + land / 2.0) - RES_BODY_CLEAR,
                    ax + land / 2.0 + RES_BODY_CLEAR,
                    y_c + (d + land / 2.0) + RES_BODY_CLEAR,
                )
            )

            if not last:
                for s in (+1, -1):
                    cell(depth + 1, y_c + s * child_off, f"{tag}{'H' if s > 0 else 'L'}")

        cell(0, 0.0, "S")

        trace = unary_union([p_.polygon() for p_ in paths] + lands)

        # --- board envelope --------------------------------------------
        y_span = max(abs(y) for y in out_ys) if out_ys else spread
        bw = x_in - x_out
        bh = 2.0 * (y_span + MARGIN_Y)
        board = box(x_out, -bh / 2.0, x_in, bh / 2.0)

        pour = make_pour(
            board,
            trace,
            st.gap_mm,
            keepouts=keepouts,
            band=st.pour_band_mm,
            min_via_clear=st.via_pad_mm / 2.0 + 0.05,
        )

        vias = fence_vias(
            paths,
            pour,
            pitch=st.via_pitch_mm,
            drill=st.via_drill_mm,
            pad=st.via_pad_mm,
            edge_clear=st.via_trace_clear_mm,
        )
        # Board-edge ring, then flood the interior. Skipping the flood is
        # what turns an unremarkable pour into a patch resonator.
        edge = board.buffer(-st.via_edge_mm).exterior
        ring_path = route(list(edge.coords), 0.05, 0.0)
        vias = fence_vias(
            [ring_path],
            pour,
            offset=0.0,
            pitch=st.via_pitch_mm,
            drill=st.via_drill_mm,
            pad=st.via_pad_mm,
            existing=vias,
        )
        vias = fill_vias(
            pour,
            pitch=st.via_fill_pitch_mm,
            drill=st.via_drill_mm,
            pad=st.via_pad_mm,
            existing=vias,
        )

        ports = [Port("P1", x_in, 0.0, "+x", spec.z0)]
        for i, y in enumerate(sorted(out_ys, reverse=True)):
            ports.append(Port(f"P{i + 2}", x_out, y, "-x", spec.z0))

        arm = gcpw(w_arm, st.gap_mm, st.h, st.er, f, tand=st.tand)
        notes = [
            f"main line {w_main:.4f} mm -> {spec.z0:.1f} ohm",
            f"arm {w_arm:.4f} mm -> {arm.z0:.2f} ohm "
            f"(ideal {spec.z0 * math.sqrt(2):.2f}), drawn {l_arm:.4f} mm "
            f"= {l_arm / arm.lambda_g_mm * 360:.1f} deg at f0",
        ]

        return Geometry(
            paths=paths,
            trace=trace,
            pour=pour,
            vias=vias,
            resistors=resistors,
            ports=ports,
            board=board,
            params={
                "l_arm": l_arm,
                "w_arm": w_arm,
                "w_main": w_main,
                "r_sep": r_sep,
                "r_ohms": r_ohms,
                "out_pitch": out_pitch,
                "spread": spread,
            },
            notes=notes,
        )
