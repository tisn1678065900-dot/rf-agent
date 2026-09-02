"""Turn a solved touchstone into the numbers the requirement is written in.

Every metric is a worst case over the requirement band, because that is
how an RF requirement is actually read: "S11 <= -20 dB from 11.5 to
11.7 GHz" means at every frequency in there, not on average.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import skrf as rf

from .spec import RFSpec


def load(touchstone: str | Path) -> rf.Network:
    return rf.Network(str(touchstone))


def _band_mask(net: rf.Network, band_ghz: tuple[float, float]) -> np.ndarray:
    f = net.f / 1e9
    m = (f >= band_ghz[0] - 1e-9) & (f <= band_ghz[1] + 1e-9)
    if not m.any():
        raise ValueError(
            f"solved sweep {f[0]:.3f}..{f[-1]:.3f} GHz does not cover band {band_ghz}"
        )
    return m


def _db(x: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.abs(x) + 1e-30)


@dataclass
class Metrics:
    """Worst-case values over the band, plus a little context."""

    values: dict[str, float]
    band_ghz: tuple[float, float]
    # Where each worst case happened -- the single most useful thing when
    # a design misses: a notch at one frequency reads very differently
    # from a whole band that is 2 dB off.
    worst_at_ghz: dict[str, float]
    f_min_s11_ghz: float
    min_s11_db: float

    def __getitem__(self, k: str) -> float:
        return self.values[k]

    def as_dict(self) -> dict:
        return {
            "band_ghz": list(self.band_ghz),
            "values": {k: round(v, 4) for k, v in self.values.items()},
            "worst_at_ghz": {k: round(v, 4) for k, v in self.worst_at_ghz.items()},
            "f_min_s11_ghz": round(self.f_min_s11_ghz, 4),
            "min_s11_db": round(self.min_s11_db, 3),
        }


def evaluate(
    net: rf.Network,
    spec: RFSpec,
    port_map: dict[str, list[int]] | None = None,
    band_ghz: tuple[float, float] | None = None,
) -> Metrics:
    """Compute every metric the spec vocabulary defines."""
    pm = port_map or {"input": [1], "outputs": list(range(2, net.nports + 1))}
    band = band_ghz or spec.band_ghz
    m = _band_mask(net, band)
    f = net.f[m] / 1e9
    s = net.s[m]

    i = pm["input"][0] - 1
    outs = [o - 1 for o in pm["outputs"]]
    n_out = len(outs)

    vals: dict[str, float] = {}
    where: dict[str, float] = {}

    def worst(name: str, series: np.ndarray) -> None:
        k = int(np.argmax(series))
        vals[name] = float(series[k])
        where[name] = float(f[k])

    # --- input match -------------------------------------------------
    worst("s11_db", _db(s[:, i, i]))

    # --- output match ------------------------------------------------
    if outs:
        worst("output_return_loss_db", np.max(np.stack([_db(s[:, o, o]) for o in outs]), axis=0))

    # --- loss ---------------------------------------------------------
    # Everything that did not come out of an output port: dissipated in
    # copper, dielectric and the isolation resistors, plus whatever was
    # reflected. That is the number a link budget cares about.
    p_out = np.sum(np.stack([np.abs(s[:, o, i]) ** 2 for o in outs]), axis=0)
    worst("excess_loss_db", -10.0 * np.log10(np.maximum(p_out, 1e-30)))

    # --- isolation ----------------------------------------------------
    if n_out >= 2:
        pairs = list(itertools.combinations(outs, 2))
        worst("isolation_db", np.max(np.stack([_db(s[:, a, b]) for a, b in pairs]), axis=0))
    else:
        vals["isolation_db"] = -np.inf
        where["isolation_db"] = float(f[0])

    # --- balance ------------------------------------------------------
    if n_out >= 2:
        mags = np.stack([_db(s[:, o, i]) for o in outs])
        worst("amplitude_imbalance_db", mags.max(axis=0) - mags.min(axis=0))

        # Phase spread, referenced to the first output so the absolute
        # electrical length of the whole part cancels out.
        ref = np.angle(s[:, outs[0], i])
        rel = np.stack(
            [np.angle(s[:, o, i] * np.exp(-1j * ref)) for o in outs]
        )
        rel = np.degrees(rel)
        worst("phase_imbalance_deg", rel.max(axis=0) - rel.min(axis=0))
    else:
        vals["amplitude_imbalance_db"] = 0.0
        where["amplitude_imbalance_db"] = float(f[0])
        vals["phase_imbalance_deg"] = 0.0
        where["phase_imbalance_deg"] = float(f[0])

    # Context: the resonance the design actually landed on.
    all_f = net.f / 1e9
    s11_all = _db(net.s[:, i, i])
    k = int(np.argmin(s11_all))

    return Metrics(
        values=vals,
        band_ghz=band,
        worst_at_ghz=where,
        f_min_s11_ghz=float(all_f[k]),
        min_s11_db=float(s11_all[k]),
    )


def compliance(metrics: Metrics, spec: RFSpec) -> dict:
    """Per-target pass/fail table, and whether the whole spec is met."""
    rows = []
    ok = True
    for t in spec.targets:
        v = metrics.values.get(t.metric)
        if v is None:
            continue
        viol = t.violation(v)
        passed = viol <= 1e-9
        if t.hard and not passed:
            ok = False
        rows.append(
            {
                "metric": t.metric,
                "op": t.op,
                "limit": t.limit,
                "value": round(v, 4),
                "worst_at_ghz": round(metrics.worst_at_ghz.get(t.metric, 0.0), 4),
                "margin": round(-viol if not passed else abs(v - t.limit), 4),
                "pass": passed,
                "hard": t.hard,
            }
        )
    return {"meets_spec": ok, "targets": rows}


def usable_bandwidth_ghz(net: rf.Network, port: int, limit_db: float) -> tuple[float, float] | None:
    """Contiguous span around the deepest match where Sii stays under limit.

    Reported rather than optimised: it is the honest answer to "how much
    band does this actually have", which a worst-case-in-band number
    cannot give you.
    """
    f = net.f / 1e9
    d = _db(net.s[:, port - 1, port - 1])
    below = d <= limit_db
    if not below.any():
        return None
    k = int(np.argmin(d))
    lo = k
    while lo > 0 and below[lo - 1]:
        lo -= 1
    hi = k
    while hi < len(f) - 1 and below[hi + 1]:
        hi += 1
    return float(f[lo]), float(f[hi])
