# wilkinson_1to2_11p6GHz

> 1:2 Wilkinson LO divider for X-band local-oscillator distribution, GCPW on 0.254 mm RO4350B, in a metal cavity

**Result: meets every hard requirement**, verified as built on the 1-mil Altium grid.

The continuous-dimension optimum does *not* pass (isolation_db); rounding the widths onto the mil grid moved it back inside the limit. The margin is that thin -- treat this as meeting spec by luck, not by design.

## Requirement

| item | value |
|---|---|
| device | wilkinson, 1:2 |
| centre frequency | 11.6 GHz |
| band | 10.44 – 12.76 GHz |
| reference impedance | 50.0 Ω |
| laminate | RO4350B 0.254 mm, εr 3.66, tanδ 0.0037 |
| coplanar gap | 0.25 mm |

## Verified performance

| requirement | limit | achieved | worst at | margin |  |
|---|---|---|---|---|---|
| s11_db | <= -20.0 | -21.28 | 10.440 GHz | 1.28 | PASS |
| isolation_db | <= -18.0 | -17.56 | 12.760 GHz | -0.44 | FAIL |
| output_return_loss_db | <= -16.0 | -19.50 | 12.760 GHz | 3.50 | PASS |
| excess_loss_db | <= 0.5 | 0.32 | 12.760 GHz | 0.18 | PASS |
| amplitude_imbalance_db | <= 0.3 | 0.01 | 10.440 GHz | 0.29 | PASS |
| phase_imbalance_deg | <= 3.0 | 0.06 | 10.440 GHz | 2.94 | PASS |

Deepest input match -32.97 dB at 11.832 GHz.

### As built (widths on the 1-mil Altium grid)

The optimiser works in continuous dimensions; Altium's PCB command surface is integer mils. This is the same design re-solved with every trace width rounded to the grid, which is what the board will actually be.

| requirement | limit | as built |  |
|---|---|---|---|
| s11_db | <= -20.0 | -22.72 | PASS |
| isolation_db | <= -18.0 | -18.43 | PASS |
| output_return_loss_db | <= -16.0 | -18.92 | PASS |
| excess_loss_db | <= 0.5 | 0.33 | PASS |
| amplitude_imbalance_db | <= 0.3 | 0.03 | PASS |
| phase_imbalance_deg | <= 3.0 | 0.05 | PASS |

## Optimised dimensions

| parameter | analytic seed | optimised | Δ |
|---|---|---|---|
| l_arm | 4.6067 | 4.9752 | 0.3685 |
| w_arm | 0.2753 | 0.3419 | 0.0666 |
| r_sep | 1.1000 | 0.9318 | -0.1682 |
| r_ohms | 100.0000 | 126.7027 | 26.7027 |

| geometry | value |
|---|---|
| board | 10.509 × 13.0 mm |
| signal copper | 10.074 mm² |
| ground pour | 115.223 mm² |
| ground vias | 41 |
| isolation resistors | 1 |
| routed length | 22.331 mm |

- main line 0.5238 mm -> 50.0 ohm
- arm 0.3419 mm -> 63.45 ohm (ideal 70.71), drawn 4.9752 mm = 112.6 deg at f0

## Manufacture

Altium coordinates land on a 25.4 µm grid (±12.7 µm on any position). Trace widths round as:

| designed | as placed | error | Z₀ designed | Z₀ as placed | ΔZ₀ |
|---|---|---|---|---|---|
| 0.3419 mm | 13 mil (0.3302 mm) | -11.7 µm | 63.448 Ω | 64.598 Ω | +1.150 Ω |
| 0.52385 mm | 21 mil (0.5334 mm) | 9.55 µm | 50.0 Ω | 49.462 Ω | -0.538 Ω |

The DXF export carries the full-precision geometry.

## How it was found

30 Optuna trials: 30 solved in HFSS, 0 rejected by fab rules before any solve, 0 failed to solve. The study was resumed from its store; no new trials were needed.

Five best trials:

| trial | loss | S11 dB | isolation dB | excess loss dB | params |
|---|---|---|---|---|---|
| 13 | 0.122 | -21.28 | -17.56 | 0.319 | l_arm=4.97523, w_arm=0.3419, r_sep=0.93185, r_ohms=126.7027 |
| 11 | 0.491 | -18.96 | -22.20 | 0.300 | l_arm=4.70062, w_arm=0.32158, r_sep=0.86519, r_ohms=108.76972 |
| 16 | 0.994 | -18.56 | -21.81 | 0.386 | l_arm=5.30189, w_arm=0.35861, r_sep=1.4942, r_ohms=79.79964 |
| 9 | 1.703 | -17.96 | -19.49 | 0.319 | l_arm=4.39236, w_arm=0.30979, r_sep=0.83006, r_ohms=121.76355 |
| 25 | 2.421 | -17.13 | -20.51 | 0.340 | l_arm=4.7625, w_arm=0.35358, r_sep=1.05446, r_ohms=99.68064 |

## Method and assumptions

- Trace widths and quarter-wave lengths are seeded from a conformal-mapping grounded-CPW model, then optimised against HFSS. The analytic model is the starting point, not the answer.
- The EM model has no radiation boundary: the outer faces are PEC, which represents a metal housing. A design intended to sit in free space or under a plastic lid will behave differently.
- Exploration ran at a draft mesh (ΔS 0.05, 6 passes); the reported figures come from a full-fidelity re-solve (ΔS 0.02, 12 passes).
- Isolation resistors are ideal lumped elements. A real 0402 thin-film part has parasitic inductance that degrades isolation above roughly 10 GHz; that is not in this model.
- Laminate εr is the single design value 3.66; dispersion and batch tolerance are not swept.

## Files

- `spec` — [../spec.json](../spec.json)
- `layout_seed_png` — [layout_seed.png](layout_seed.png)
- `layout_png` — [layout_optimised.png](layout_optimised.png)
- `sparams_png` — [sparams.png](sparams.png)
- `dxf` — [divider.dxf](divider.dxf)
- `touchstone` — [divider_optimised.s3p](divider_optimised.s3p)
