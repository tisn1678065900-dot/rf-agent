"""Device generators: spec + parameters -> Geometry.

A device is the only place in the toolchain that knows what a particular
component looks like. Everything downstream -- the solver, the metrics,
the optimiser, the Altium writer -- works on the `Geometry` it returns
and never on the topology itself. Adding a coupler or a filter therefore
means adding one module here, not touching the loop.
"""

from __future__ import annotations

from .base import Device, ParamRange
from .wilkinson import Wilkinson

REGISTRY: dict[str, type[Device]] = {
    Wilkinson.key: Wilkinson,
}


def get_device(key: str) -> type[Device]:
    if key not in REGISTRY:
        raise KeyError(f"unknown device {key!r}; have {sorted(REGISTRY)}")
    return REGISTRY[key]


__all__ = ["Device", "ParamRange", "Wilkinson", "REGISTRY", "get_device"]
