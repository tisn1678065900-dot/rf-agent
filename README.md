# rf-agent

A closed loop for RF passive design: **a requirement in words → a
parametric layout → HFSS → Optuna → an EM-verified board in Altium**,
driven by an LLM over MCP.

```
requirement  ──▶  RFSpec  ──▶  parametric layout  ──▶  HFSS (PyAEDT)
                    ▲                │                      │
                    │                │  DRC gate            │  S-parameters
                    │                ▼                      ▼
                    └────────  Optuna  ◀────────────  metrics + objective
                                     │
                                     ▼
                     verify at full fidelity ──▶ DXF + Altium + report
```

It is the physics half of a pair. [`eda-agent`](https://github.com/salitronic/eda-agent)
owns the board a human has open in Altium — ~400 tools for schematic,
PCB, library and design-agent work. `rf-agent` owns what `eda-agent`
cannot know: what the copper should *be*. Run both and an agent has the
whole path.

> **Status: working, narrow.** The loop runs end to end and the EM model
> is correlated against a real X-band design. One device family
> (Wilkinson dividers, 1:2 and 1:4) and one cross-section (grounded
> coplanar waveguide over a single ground) are implemented. Everything
> else is scaffolding waiting for a second device.

## See it work first

[**`examples/wilkinson_11g6/`**](examples/wilkinson_11g6/) is a complete
recorded run — an 11.6 GHz 1:2 divider taken from one sentence of
requirement to an EM-verified board, with all 30 HFSS solves, the trial
history, the S-parameters, the DXF and the generated report committed.

**What it achieved.** The textbook starting point fails: −17.07 dB return
loss against a −20 dB requirement, with the match sitting 1.1 GHz above
where it was asked for. Thirty trials and 24 minutes of solver time
later:

| | analytic seed | after the loop | requirement |
|---|---|---|---|
| worst in-band S11 | −17.07 dB ✗ | **−22.72 dB** ✓ | ≤ −20 |
| match centred at | 12.69 GHz | **11.83 GHz** | 11.6 |
| isolation | −20.04 dB | −18.43 dB ✓ | ≤ −18 |
| amplitude imbalance | 0.02 dB | **0.03 dB** ✓ | ≤ 0.3 |
| phase imbalance | 0.12° | **0.05°** ✓ | ≤ 3.0 |

A working X-band divider that meets every requirement, from one sentence
of English, with nothing drawn by hand and nobody watching. Return loss
improved 4.2 dB, the match moved 860 MHz onto centre, and the balance
metrics came in 30× and 50× inside their limits.

The example is also worth reading for what it *doesn't* do: isolation
clears by 0.44 dB and only after the widths round onto Altium's
placement grid, and the report says so in those words instead of
banking the pass. That is the behaviour to check before trusting any of
this on a real board.

Stage 1 of that example needs no Ansys licence and no Altium:

```bash
uv run python examples/wilkinson_11g6/01_synthesize.py
```

## What it actually does

**Synthesises, rather than templating.** Trace widths come from a
conformal-mapping grounded-CPW model, quarter-wave lengths from the
effective permittivity that model returns, via pitch from λg/12 in the
substrate. Change the laminate or the frequency and every dimension
moves. Nothing is a constant carried over from one previous design.

The line model is anchored: on RO4350B 0.254 mm with a 0.25 mm gap it
returns **49.99 Ω at the HFSS-tuned width of 0.524 mm** and **70.75 Ω at
0.275 mm** — the two drawn widths of an X-band GCPW divider that was
tuned in HFSS and built. `tests/test_lines.py` holds that as a regression.

**Rejects the unbuildable before it costs a solve.** Every candidate is
checked against the fab rules — trace and gap minima, annular ring, via
clearance to signal copper, board-edge keepout — and a failure is scored
from its violations rather than sent to HFSS. On a loop where one sample
is a minute, that is most of the budget.

**Seeds the optimiser with the textbook answer.** Optuna trial 0 is
always the analytic design, so TPE starts from a real gradient instead of
a random corner. The seed is deliberately placed above the ideal
quarter wave: a drawn Wilkinson arm always runs long, because the tee and
the bends add electrical length the closed-form model cannot see. On the
reference design that was about 19%.

**Explores cheap, reports honest.** The study runs a draft mesh and a
short sweep. The winner is re-solved at full fidelity before anything is
reported. And because Altium's PCB command surface is integer mils
(1 mil = 25.4 µm), if
rounding the trace widths onto that grid moves an impedance, the snapped
geometry is solved *again* — so the report states what the board will do,
not what the optimiser found.

**Writes design intent, not polygons.** The Altium export sends
centrelines as tracks with their real widths, ground vias as vias, and
the coplanar ground as a polygon pour plus a clearance rule sized to the
gap the EM model used. Altium regenerates the copper from that, so the
result is net-aware, routable and DRC-clean.

## Install

Needs Python 3.11+, and Ansys Electronics Desktop for anything that
solves.

```bash
uv sync --extra plot
```

Optional, for the Altium half: have `eda-agent` importable (`pip install
-e path/to/eda-agent`, or set `RF_AGENT_EDA_AGENT` to its checkout).

Check the machine:

```bash
uv run rf-agent doctor
```

## Use it from the command line

```bash
uv run rf-agent line --laminate RO4350B-0.254 --f0 11.6 --z0 70.711
```

```bash
uv run rf-agent synth --f0 11.6 --n-way 2
```

```bash
uv run rf-agent design --f0 11.6 --n-way 2 --trials 40
```

`design` runs the whole loop and writes a run directory: `spec.json`,
`layout.png`, `sparams.png`, the touchstone, the DXF, the HFSS project,
`report.md` and `result.json`.

## Use it from an agent

Register the MCP server:

```bash
claude mcp add rf-agent -- uv --directory C:/path/to/rf-agent run rf-agent serve
```

Tools:

| tool | what it is for |
|---|---|
| `rf_doctor` | is HFSS reachable, is the Altium bridge live |
| `rf_line_model` | impedance ↔ width, λg, loss, via pitch |
| `rf_make_spec` | requirement → structured spec |
| `rf_synthesize` | draw a candidate, DRC it, render it — no solve |
| `rf_solve` | one candidate through HFSS, scored |
| `rf_optimize` | start a study; returns a job id |
| `rf_design` | the whole loop; returns a job id |
| `rf_job_status` / `rf_list_jobs` | poll the long stages |
| `rf_altium_preflight` | why the bridge is not ready, specifically |
| `rf_export_altium` | commit a design into the open PCB |

The long stages return a job id rather than blocking — an MCP call that
takes an hour is not usable by an agent.

`skills/rfdesign/SKILL.md` is the companion skill: how to sequence these,
and what not to claim about the results.

## The EM model

Correlated against that same built X-band divider, and reproduced by
`src/rf_agent/em/hfss.py`:

- substrate box on the laminate material, PEC via barrels punched through
- top copper as zero-thickness sheets with a finite-conductivity boundary
  carrying the real copper thickness and surface roughness
- an air box above the board, **outer faces left on HFSS's default PEC
  boundary** — that is the metal housing, and it is why there is no
  radiation boundary anywhere in the file
- isolation resistors as lumped RLC sheets bridging the two lands
- wave ports on the board edges, integration line running from the ground
  plane up to the trace

Ports are sized from the line cross-section *and* from the spacing
between neighbouring ports on the same edge. Two overlapping wave-port
sheets assign without complaint and fail during the solve, which is an
expensive way to find out.

## What this does not model

Stated plainly, because a report that only shows the winning curve is not
a report:

- **Isolation resistors are ideal.** A real 0402's parasitic inductance
  degrades isolation above roughly 10 GHz. An X-band isolation figure
  from this loop is optimistic.
- **The enclosure is a perfect box.** No lid channels, no absorber unless
  you configure one, no connector launches.
- **εr is a single design value.** No dispersion, no batch tolerance, no
  temperature.
- **Conductor loss is first-order.** The analytic attenuation is good to
  a few tens of percent — enough to rank candidates and sanity-check an
  HFSS insertion loss, not enough to quote.
- **One signal layer over one ground.** Multilayer, buried structures and
  differential pairs are not in the stackup model.

## A note on the pour

The generator floods the pour interior with stitching vias, not just a
fence along the traces. That is not conservatism. On 0.254 mm RO4350B at
X band, running ground vias only along the trace edges leaves large
un-stitched islands that behave as patch resonators between the top pour
and the bottom ground — on the reference divider, a 3.9 dB notch 260 MHz
from the design point. Islands too small to hold a via are dropped
entirely rather than left floating.

## Layout of the code

```
src/rf_agent/
  lines.py        closed-form grounded CPW: the analytic seed layer
  stackup.py      laminates, via policy, fab rules
  spec.py         RFSpec: the contract between words and geometry
  geometry.py     Seg/Arc primitives, pour, stitching, DRC
  devices/        parametric structures (wilkinson.py)
  em/hfss.py      PyAEDT model build, solve, content-addressed cache
  metrics.py      touchstone → worst-case-in-band numbers
  objective.py    metrics → the scalar Optuna minimises
  optimize.py     the study
  pipeline.py     the closed loop
  export/         dxf.py (exact) and altium.py (via the eda-agent bridge)
  report.py       the design report
  server.py       MCP surface
  cli.py          the same, from a terminal
```

Adding a device means adding one module under `devices/`: a parameter
space derived from the spec and a `build()` that returns a `Geometry`.
The solver, metrics, optimiser and exporters never learn its topology.
