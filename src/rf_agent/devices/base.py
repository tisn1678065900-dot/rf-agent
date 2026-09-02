"""The device contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..geometry import Geometry
from ..spec import RFSpec


@dataclass(frozen=True)
class ParamRange:
    """One optimisable dimension.

    `seed` is the analytic starting point. Optuna is told about it via an
    enqueued first trial, so the study begins from the textbook answer
    rather than from a random corner of the box -- which on an EM loop,
    where every sample costs minutes, is most of the win.
    """

    lo: float
    hi: float
    seed: float
    unit: str = "mm"
    step: float | None = None
    description: str = ""

    def clip(self, v: float) -> float:
        return min(max(v, self.lo), self.hi)


class Device(ABC):
    """A parametric RF structure that can be drawn, solved and scored."""

    key: str = "device"
    #: Human-readable, used in reports and MCP tool output.
    title: str = "device"

    @classmethod
    @abstractmethod
    def param_space(cls, spec: RFSpec) -> dict[str, ParamRange]:
        """Free dimensions and their bounds, derived from the spec.

        Bounds are computed from the analytic line model rather than
        hard-coded, so the same device works at any frequency on any
        laminate the stackup model supports.
        """

    @classmethod
    def seed_params(cls, spec: RFSpec) -> dict[str, float]:
        return {k: r.seed for k, r in cls.param_space(spec).items()}

    @classmethod
    @abstractmethod
    def build(cls, spec: RFSpec, params: dict[str, float]) -> Geometry:
        """Draw the structure. Must be pure: same params, same geometry."""

    @classmethod
    def port_map(cls, spec: RFSpec) -> dict[str, list[int]]:
        """Which 1-based port indices are the input and which the outputs.

        The metric layer reads this instead of assuming port 1 is the
        input and the rest are outputs.
        """
        return {"input": [1], "outputs": list(range(2, spec.n_ports + 1))}
