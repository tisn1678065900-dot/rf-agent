"""Collapse a metric set into the one number the optimiser minimises.

Two parts, deliberately:

* a **penalty** for every requirement that is missed, normalised so a dB
  of return loss and a degree of phase imbalance are comparable;
* a small **margin reward** for every requirement that is met, capped.

Without the reward the landscape goes flat the moment a design is
compliant and the optimiser stops caring, which is how you end up
shipping the first thing that scraped past the limit. With it uncapped,
one wildly over-satisfied metric buys its way out of missing another.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .metrics import Metrics
from .spec import RFSpec

# Units per "one unit of badness". A dB of return loss and 0.1 dB of
# amplitude imbalance are about equally hard to buy, so they score alike.
SCALES: dict[str, float] = {
    "s11_db": 1.0,
    "output_return_loss_db": 1.0,
    "excess_loss_db": 0.2,
    "isolation_db": 1.0,
    "amplitude_imbalance_db": 0.1,
    "phase_imbalance_deg": 1.0,
}

# How much margin past a limit still earns credit, in scale units.
MARGIN_CAP = 6.0
MARGIN_WEIGHT = 0.05

# What a trial scores when it cannot be built or cannot be solved. Finite
# and large: a NaN or an exception tells the sampler nothing, while a big
# number teaches it that this corner of the space is barren.
INFEASIBLE = 1000.0
FAILED_SOLVE = 500.0


@dataclass
class Score:
    loss: float
    penalty: float
    reward: float
    meets_spec: bool
    terms: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "loss": round(self.loss, 5),
            "penalty": round(self.penalty, 5),
            "reward": round(self.reward, 5),
            "meets_spec": self.meets_spec,
            "terms": self.terms,
        }


def score(metrics: Metrics, spec: RFSpec) -> Score:
    penalty = 0.0
    reward = 0.0
    ok = True
    terms: list[dict] = []

    for t in spec.targets:
        v = metrics.values.get(t.metric)
        if v is None:
            continue
        scale = SCALES.get(t.metric, 1.0)
        viol = t.violation(v)
        if viol > 1e-9:
            if t.hard:
                ok = False
            penalty += t.weight * viol / scale
            margin = 0.0
        else:
            margin = (t.limit - v) if t.op == "<=" else (v - t.limit)
            reward += t.weight * min(margin / scale, MARGIN_CAP)
        terms.append(
            {
                "metric": t.metric,
                "value": round(v, 4),
                "limit": t.limit,
                "violation": round(viol, 4),
                "margin": round(margin, 4),
                "weight": t.weight,
            }
        )

    loss = penalty - MARGIN_WEIGHT * reward
    return Score(loss=loss, penalty=penalty, reward=reward, meets_spec=ok, terms=terms)


def infeasible_loss(n_violations: int) -> float:
    """Score for a geometry that failed DRC.

    Scaled by how badly, so the sampler gets a gradient pointing back
    toward the manufacturable region instead of a flat wall.
    """
    return INFEASIBLE + 10.0 * n_violations
