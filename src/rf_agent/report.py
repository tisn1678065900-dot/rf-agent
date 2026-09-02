"""The design report.

Written for someone who has to sign off on the board, so it says what was
required, what the EM solver actually returned, where every number came
from, and what is still an assumption. A report that only shows the
winning curve is not a report.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def _fmt(v: Any, n: int = 3) -> str:
    if isinstance(v, float):
        return f"{v:.{n}f}"
    return str(v)


def write_report(result) -> Path:
    """Render `LoopResult` to Markdown next to its artefacts."""
    spec = result.spec
    out = Path(result.out_dir) / "report.md"
    L: list[str] = []

    L.append(f"# {spec.name}")
    L.append("")
    if spec.requirement_text:
        L.append(f"> {spec.requirement_text}")
        L.append("")

    # ---- verdict ------------------------------------------------------
    ver = result.verification
    if ver is None:
        L.append("**Result: no verified design.** The run stopped before verification.")
    else:
        ab = ver.get("as_built")
        judged = ab["compliance"] if ab else ver["compliance"]
        missed = [t["metric"] for t in judged["targets"] if t["hard"] and not t["pass"]]
        which = (
            "as built on the 1-mil Altium grid" if ab else "on a full-fidelity solve"
        )
        if result.meets_spec:
            L.append(f"**Result: meets every hard requirement**, verified {which}.")
        else:
            L.append(
                f"**Result: does not meet spec** ({which}). "
                f"Missed: {', '.join(missed) or 'unknown'}."
            )
        # When the two disagree, say so: it is the single most misreadable
        # thing in the report.
        if ab and result.meets_spec != result.ideal_meets_spec:
            other = [
                t["metric"]
                for t in ver["compliance"]["targets"]
                if t["hard"] and not t["pass"]
            ]
            if result.meets_spec:
                L.append("")
                L.append(
                    f"The continuous-dimension optimum does *not* pass "
                    f"({', '.join(other)}); rounding the widths onto the mil grid moved "
                    f"it back inside the limit. The margin is that thin -- treat this as "
                    f"meeting spec by luck, not by design."
                )
            else:
                L.append("")
                L.append(
                    "The continuous-dimension optimum passes, but the board that gets "
                    "built does not. The mil grid is the binding constraint here."
                )
    L.append("")

    # ---- requirement --------------------------------------------------
    L.append("## Requirement")
    L.append("")
    L.append(
        _table(
            ["item", "value"],
            [
                ["device", f"{spec.device}, 1:{spec.n_way}"],
                ["centre frequency", f"{spec.f0_ghz} GHz"],
                ["band", f"{spec.band_ghz[0]} – {spec.band_ghz[1]} GHz"],
                ["reference impedance", f"{spec.z0} Ω"],
                ["laminate", f"{spec.stackup.laminate.name} {spec.stackup.h} mm, "
                             f"εr {spec.stackup.er}, tanδ {spec.stackup.tand}"],
                ["coplanar gap", f"{spec.stackup.gap_mm} mm"],
            ],
        )
    )
    L.append("")

    # ---- compliance ---------------------------------------------------
    if ver:
        L.append("## Verified performance")
        L.append("")
        L.append(
            _table(
                ["requirement", "limit", "achieved", "worst at", "margin", ""],
                [
                    [
                        t["metric"],
                        f"{t['op']} {t['limit']}",
                        _fmt(t["value"], 2),
                        f"{_fmt(t['worst_at_ghz'], 3)} GHz",
                        _fmt(t["margin"], 2),
                        "PASS" if t["pass"] else ("FAIL" if t["hard"] else "soft miss"),
                    ]
                    for t in ver["compliance"]["targets"]
                ],
            )
        )
        L.append("")
        m = ver["metrics"]
        L.append(
            f"Deepest input match {_fmt(m['min_s11_db'], 2)} dB at "
            f"{_fmt(m['f_min_s11_ghz'], 3)} GHz."
        )
        L.append("")

        ab = ver.get("as_built")
        if ab:
            L.append("### As built (widths on the 1-mil Altium grid)")
            L.append("")
            L.append(
                "The optimiser works in continuous dimensions; Altium's PCB command "
                "surface is integer mils. This is the same design re-solved with every "
                "trace width rounded to the grid, which is what the board will actually be."
            )
            L.append("")
            L.append(
                _table(
                    ["requirement", "limit", "as built", ""],
                    [
                        [t["metric"], f"{t['op']} {t['limit']}", _fmt(t["value"], 2),
                         "PASS" if t["pass"] else ("FAIL" if t["hard"] else "soft miss")]
                        for t in ab["compliance"]["targets"]
                    ],
                )
            )
            L.append("")

    # ---- dimensions ---------------------------------------------------
    if ver:
        L.append("## Optimised dimensions")
        L.append("")
        seed = next((s for s in result.stages if s["stage"] == "seed"), None)
        seed_p = (seed or {}).get("params", {})
        rows = []
        for k, v in ver["params"].items():
            sv = seed_p.get(k)
            rows.append([k, _fmt(sv, 4) if sv is not None else "", _fmt(v, 4),
                         _fmt(v - sv, 4) if sv is not None else ""])
        L.append(_table(["parameter", "analytic seed", "optimised", "Δ"], rows))
        L.append("")

        g = ver["geometry"]
        L.append(
            _table(
                ["geometry", "value"],
                [
                    ["board", f"{g['board_mm'][0]} × {g['board_mm'][1]} mm"],
                    ["signal copper", f"{g['trace_area_mm2']} mm²"],
                    ["ground pour", f"{g['pour_area_mm2']} mm²"],
                    ["ground vias", g["n_vias"]],
                    ["isolation resistors", g["n_resistors"]],
                    ["routed length", f"{g['routed_length_mm']} mm"],
                ],
            )
        )
        L.append("")
        for n in g.get("notes", []):
            L.append(f"- {n}")
        L.append("")
        if ver.get("drc"):
            L.append("**DRC violations on the verified geometry:**")
            for d in ver["drc"]:
                L.append(f"- {d}")
            L.append("")

    # ---- manufacture --------------------------------------------------
    qd = getattr(result, "quantisation", None)
    if qd:
        L.append("## Manufacture")
        L.append("")
        L.append(
            f"Altium coordinates land on a {qd['grid_um']} µm grid "
            f"(±{qd['max_position_error_um']} µm on any position). Trace widths round as:"
        )
        L.append("")
        L.append(
            _table(
                ["designed", "as placed", "error", "Z₀ designed", "Z₀ as placed", "ΔZ₀"],
                [
                    [f"{r['width_mm']} mm", f"{r['width_mil']} mil ({r['snapped_mm']} mm)",
                     f"{r['error_um']} µm", f"{r['z0_ideal']} Ω", f"{r['z0_snapped']} Ω",
                     f"{r['z0_shift']:+.3f} Ω"]
                    for r in qd["widths"]
                ],
            )
        )
        L.append("")
        L.append("The DXF export carries the full-precision geometry.")
        L.append("")

    # ---- how it was found ---------------------------------------------
    if result.study:
        st = result.study
        L.append("## How it was found")
        L.append("")
        c = st["counts"]
        line = (
            f"{st['n_trials']} Optuna trials: {c['solved']} solved in HFSS, "
            f"{c['drc_rejected']} rejected by fab rules before any solve, "
            f"{c['failed']} failed to solve."
        )
        new_this_run = c.get("new_this_run")
        if new_this_run == 0:
            line += " The study was resumed from its store; no new trials were needed."
        elif st["elapsed_s"] >= 1:
            line += f" This run added {new_this_run} of them, in {st['elapsed_s']:.0f} s."
        L.append(line)
        L.append("")
        best = [t for t in st["history"] if t["metrics"]]
        best.sort(key=lambda t: t["loss"])
        if best:
            L.append("Five best trials:")
            L.append("")
            L.append(
                _table(
                    ["trial", "loss", "S11 dB", "isolation dB", "excess loss dB", "params"],
                    [
                        [
                            t["number"], _fmt(t["loss"], 3),
                            _fmt(t["metrics"]["values"].get("s11_db", 0), 2),
                            _fmt(t["metrics"]["values"].get("isolation_db", 0), 2),
                            _fmt(t["metrics"]["values"].get("excess_loss_db", 0), 3),
                            ", ".join(f"{k}={v}" for k, v in t["params"].items()),
                        ]
                        for t in best[:5]
                    ],
                )
            )
            L.append("")

    # ---- method -------------------------------------------------------
    L.append("## Method and assumptions")
    L.append("")
    L.append(
        "- Trace widths and quarter-wave lengths are seeded from a conformal-mapping "
        "grounded-CPW model, then optimised against HFSS. The analytic model is the "
        "starting point, not the answer."
    )
    L.append(
        "- The EM model has no radiation boundary: the outer faces are PEC, which "
        "represents a metal housing. A design intended to sit in free space or under a "
        "plastic lid will behave differently."
    )
    L.append(
        f"- Exploration ran at a draft mesh (ΔS {spec.em.draft_delta_s}, "
        f"{spec.em.draft_max_passes} passes); the reported figures come from a "
        f"full-fidelity re-solve (ΔS {spec.em.delta_s}, {spec.em.max_passes} passes)."
    )
    L.append(
        "- Isolation resistors are ideal lumped elements. A real 0402 thin-film part has "
        "parasitic inductance that degrades isolation above roughly 10 GHz; that is not "
        "in this model."
    )
    L.append(
        f"- Laminate εr is the single design value {spec.stackup.er}; dispersion and "
        "batch tolerance are not swept."
    )
    L.append("")

    # ---- artefacts -----------------------------------------------------
    if result.artefacts:
        L.append("## Files")
        L.append("")
        for k, v in result.artefacts.items():
            L.append(f"- `{k}` — {v}")
        L.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    return out
