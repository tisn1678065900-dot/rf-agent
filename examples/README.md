# Examples

| example | what it shows | needs |
|---|---|---|
| [`wilkinson_11g6/`](wilkinson_11g6/) | the whole loop end to end: requirement → layout → HFSS → Optuna → DXF → Altium, with the recorded output of a real 30-solve run | stage 1 nothing, stage 2 HFSS, stage 3 Altium |

Start with [`wilkinson_11g6/01_synthesize.py`](wilkinson_11g6/01_synthesize.py).
It runs on any machine in about a second, needs no Ansys licence and no
Altium, and produces a DXF and a preview PNG — enough to see what the
generator does before you spend solver time on it.

Everything under `wilkinson_11g6/results/` is real output, committed so
the example is readable without running anything.

## The same thing from the CLI

```bash
uv run rf-agent doctor                       # what is available on this machine
uv run rf-agent line --z0 50 --f0 11.6       # GCPW width and wavelength
uv run rf-agent synth --spec examples/wilkinson_11g6/spec.json --out /tmp/w
uv run rf-agent design --spec examples/wilkinson_11g6/spec.json --trials 30
```

`rf-agent doctor` is the one to run first — it reports whether HFSS,
Altium and the eda-agent bridge are reachable, and says which stages you
can actually run.
