"""The structured requirement.

`RFSpec` is the contract between the language layer and everything
mechanical. An LLM turns "I need a 1:2 splitter at 11.6 GHz on 10-mil
4350B, return loss better than 20 dB, isolation better than 18" into one
of these; from there on nothing guesses -- the geometry generator, the
HFSS setup, the objective function and the Altium writer all read the
same object.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .stackup import LAMINATES, Stackup

MetricName = Literal[
    "s11_db",  # input return loss (more negative is better)
    "output_return_loss_db",  # worst Sii over the output ports
    "excess_loss_db",  # loss beyond the ideal 10log10(N) split
    "isolation_db",  # worst coupling between two output ports
    "amplitude_imbalance_db",  # spread of |Si1| across outputs
    "phase_imbalance_deg",  # spread of arg(Si1) across outputs
]

# Which direction counts as "better" for each metric. The objective layer
# reads this rather than carrying a pile of special cases.
LOWER_IS_BETTER: dict[str, bool] = {
    "s11_db": True,
    "output_return_loss_db": True,
    "excess_loss_db": True,
    "isolation_db": True,
    "amplitude_imbalance_db": True,
    "phase_imbalance_deg": True,
}


class Target(BaseModel):
    """One requirement line, evaluated over a band.

    `limit` is always stated the way a datasheet states it -- "S11 <= -20"
    is `metric="s11_db", op="<=", limit=-20`. Isolation, conventionally
    quoted as a positive number, is stated as the negative dB it really
    is: `metric="isolation_db", op="<=", limit=-18`.
    """

    metric: MetricName
    op: Literal["<=", ">="] = "<="
    limit: float
    band_ghz: tuple[float, float] | None = Field(
        default=None, description="None = use the spec's own band"
    )
    weight: float = 1.0
    # A hard target failing makes the design non-compliant; a soft one only
    # costs objective points. Both are optimised, only hard ones gate the
    # "meets spec" verdict.
    hard: bool = True

    def violation(self, value: float) -> float:
        """How far past the limit, in the metric's own units. 0 = met."""
        if self.op == "<=":
            return max(0.0, value - self.limit)
        return max(0.0, self.limit - value)


class EMSettings(BaseModel):
    """HFSS solve settings. The optimiser trades these against wall time."""

    # Adaptive mesh at the design frequency, then an interpolating sweep.
    delta_s: float = 0.02
    max_passes: int = 12
    min_converged_passes: int = 2
    sweep_points: int = 201
    sweep_type: Literal["Interpolating", "Fast", "Discrete"] = "Interpolating"
    # Air above the board. On a cavity design this is the lid height; the
    # outer boundary stays PEC, which is what a metal housing is.
    air_mm: float = 2.0
    # Optional lossy lid layer (t_mm, er, tand_e, mur, tand_m) to damp
    # cavity modes, as used on the reference divider.
    absorber: tuple[float, float, float, float, float] | None = None
    # Wave-port face size. None derives it from the line cross-section.
    port_width_mm: float | None = None
    port_height_mm: float | None = None
    # Solve at reduced fidelity while the optimiser is exploring, then
    # re-solve the winner properly.
    draft_delta_s: float = 0.05
    draft_max_passes: int = 6
    draft_sweep_points: int = 81


class RFSpec(BaseModel):
    """A complete, machine-actionable RF component requirement."""

    name: str = "device"
    device: str = Field(default="wilkinson", description="key in the device registry")

    # --- electrical ------------------------------------------------------
    f0_ghz: float
    band_ghz: tuple[float, float]
    z0: float = 50.0
    n_way: int = Field(default=2, description="output count for a divider")

    # --- physical --------------------------------------------------------
    stackup: Stackup
    # Board envelope the layout must fit. None = size to the geometry.
    max_board_mm: tuple[float, float] | None = None

    # --- requirements ----------------------------------------------------
    targets: list[Target] = Field(default_factory=list)

    # --- solver ----------------------------------------------------------
    em: EMSettings = Field(default_factory=EMSettings)

    # Free-text the requirement came from, kept for the report so a design
    # can always be traced back to the sentence that asked for it.
    requirement_text: str = ""

    @model_validator(mode="after")
    def _check(self) -> "RFSpec":
        lo, hi = self.band_ghz
        if lo >= hi:
            raise ValueError(f"band_ghz {self.band_ghz} is not increasing")
        if not (lo <= self.f0_ghz <= hi):
            raise ValueError(f"f0_ghz {self.f0_ghz} outside band {self.band_ghz}")
        if self.n_way < 2 or (self.n_way & (self.n_way - 1)) != 0:
            raise ValueError(f"n_way {self.n_way} must be a power of two >= 2")
        return self

    @property
    def f0_hz(self) -> float:
        return self.f0_ghz * 1e9

    @property
    def n_ports(self) -> int:
        return self.n_way + 1

    def band_for(self, t: Target) -> tuple[float, float]:
        return t.band_ghz or self.band_ghz

    # --- convenience -----------------------------------------------------
    @classmethod
    def divider(
        cls,
        *,
        f0_ghz: float,
        n_way: int = 2,
        laminate: str = "RO4350B-0.254",
        bandwidth_frac: float = 0.20,
        gap_mm: float = 0.25,
        s11_db: float = -20.0,
        isolation_db: float = -18.0,
        excess_loss_db: float = 0.5,
        amplitude_imbalance_db: float = 0.3,
        phase_imbalance_deg: float = 3.0,
        name: str | None = None,
        requirement_text: str = "",
    ) -> "RFSpec":
        """A Wilkinson divider spec with the usual five requirement lines."""
        if laminate not in LAMINATES:
            raise KeyError(f"unknown laminate {laminate!r}; have {sorted(LAMINATES)}")
        half = f0_ghz * bandwidth_frac / 2.0
        return cls(
            name=name or f"wilkinson_1to{n_way}_{f0_ghz:g}GHz".replace(".", "p"),
            device="wilkinson",
            f0_ghz=f0_ghz,
            band_ghz=(f0_ghz - half, f0_ghz + half),
            n_way=n_way,
            stackup=Stackup.from_laminate_key(laminate, gap_mm=gap_mm),
            targets=[
                Target(metric="s11_db", op="<=", limit=s11_db, weight=1.0),
                Target(metric="isolation_db", op="<=", limit=isolation_db, weight=1.0),
                Target(
                    metric="output_return_loss_db", op="<=", limit=s11_db + 4, weight=0.6
                ),
                Target(
                    metric="excess_loss_db", op="<=", limit=excess_loss_db, weight=0.8
                ),
                Target(
                    metric="amplitude_imbalance_db",
                    op="<=",
                    limit=amplitude_imbalance_db,
                    weight=0.5,
                ),
                Target(
                    metric="phase_imbalance_deg",
                    op="<=",
                    limit=phase_imbalance_deg,
                    weight=0.3,
                    hard=False,
                ),
            ],
            requirement_text=requirement_text,
        )
