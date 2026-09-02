"""Laminates and the two-layer RF stackup the synthesiser works on.

v1 targets the cross-section the reference X-band divider proved out: one signal
layer over one solid ground, thin high-Dk laminate, grounded coplanar
waveguide. That is deliberately narrow -- every dimension the layout
generator emits is derived from these numbers, so widening the stackup
model later widens the whole toolchain at once.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Laminate(BaseModel):
    """A dielectric with the properties HFSS and the line model both need."""

    name: str
    er: float = Field(description="design Dk at the band of interest")
    tand: float = Field(description="dielectric loss tangent")
    thickness_mm: float
    copper_mm: float = Field(default=0.018, description="finished copper, 0.018 = 1/2 oz")
    roughness_um: float = Field(default=0.4, description="conductor surface roughness")

    @property
    def h(self) -> float:
        return self.thickness_mm

    @property
    def t(self) -> float:
        return self.copper_mm


# Laminates worth having on hand. RO4350B 0.254 mm is the one the reference
# X-band divider is built on, and the only one validated end to end here.
LAMINATES: dict[str, Laminate] = {
    "RO4350B-0.254": Laminate(
        name="RO4350B", er=3.66, tand=0.0037, thickness_mm=0.254, copper_mm=0.018
    ),
    "RO4350B-0.508": Laminate(
        name="RO4350B", er=3.66, tand=0.0037, thickness_mm=0.508, copper_mm=0.018
    ),
    "RO4003C-0.203": Laminate(
        name="RO4003C", er=3.55, tand=0.0027, thickness_mm=0.203, copper_mm=0.018
    ),
    "RO4003C-0.406": Laminate(
        name="RO4003C", er=3.55, tand=0.0027, thickness_mm=0.406, copper_mm=0.018
    ),
    "RT5880-0.254": Laminate(
        name="RT/duroid 5880", er=2.20, tand=0.0009, thickness_mm=0.254, copper_mm=0.018
    ),
    "FR4-0.2": Laminate(name="FR-4", er=4.4, tand=0.02, thickness_mm=0.2, copper_mm=0.035),
}


class FabRules(BaseModel):
    """Fabricator limits the layout must not violate.

    Defaults are a conservative Chinese quick-turn RF house: 0.1 mm
    trace/space, 0.2 mm laser-free mechanical drill. The DRC gate in the
    optimiser reads these, so a trial that would be unmanufacturable is
    pruned before it ever costs an HFSS solve.
    """

    min_trace_mm: float = 0.10
    min_gap_mm: float = 0.10
    min_via_drill_mm: float = 0.20
    min_via_pad_mm: float = 0.45
    min_via_to_copper_mm: float = 0.15
    min_annular_ring_mm: float = 0.075
    edge_clearance_mm: float = 0.30


class Stackup(BaseModel):
    """Two-layer grounded-coplanar stackup plus its via and pour policy."""

    laminate: Laminate
    # Coplanar gap between the signal trace and the top-side pour. Held
    # constant across the whole board so one clearance rule in Altium
    # reproduces every gap in the EM model.
    gap_mm: float = 0.25

    # Ground-via policy. The pitch bound matters more than it looks: a
    # fence coarser than lambda_g/12 leaks, and an unstitched pour
    # interior resonates as a patch (the reference divider lost 3.9 dB at
    # 7.34 GHz to exactly that).
    via_drill_mm: float = 0.30
    via_pad_mm: float = 0.60
    via_pitch_mm: float = 1.20
    # Trace *edge* to via centre. The fence offset from a centreline is
    # this plus the trace's own half-width, so a narrow arm and a wide
    # feed both get their fence at the same physical clearance.
    via_trace_clear_mm: float = 0.90
    via_edge_mm: float = 0.85
    via_fill_pitch_mm: float = 2.50

    # None pours the whole board and stitches the interior on
    # `via_fill_pitch_mm`. A number pours only a band of that half-width
    # around each trace, which cuts mesh count but leaves the rest of the
    # board bare -- use it for fast exploratory solves, not for the
    # geometry you ship.
    pour_band_mm: float | None = None

    fab: FabRules = Field(default_factory=FabRules)

    @property
    def er(self) -> float:
        return self.laminate.er

    @property
    def h(self) -> float:
        return self.laminate.thickness_mm

    @property
    def tand(self) -> float:
        return self.laminate.tand

    @classmethod
    def from_laminate_key(cls, key: str, **kwargs) -> "Stackup":
        if key not in LAMINATES:
            raise KeyError(
                f"unknown laminate {key!r}; have {sorted(LAMINATES)}"
            )
        return cls(laminate=LAMINATES[key], **kwargs)

    def check(self) -> list[str]:
        """Violations of the fab rules by the stackup's own constants."""
        bad: list[str] = []
        if self.gap_mm < self.fab.min_gap_mm:
            bad.append(f"coplanar gap {self.gap_mm} < min_gap {self.fab.min_gap_mm}")
        if self.via_drill_mm < self.fab.min_via_drill_mm:
            bad.append(
                f"via drill {self.via_drill_mm} < min_via_drill {self.fab.min_via_drill_mm}"
            )
        ring = (self.via_pad_mm - self.via_drill_mm) / 2.0
        if ring < self.fab.min_annular_ring_mm:
            bad.append(f"annular ring {ring:.3f} < min {self.fab.min_annular_ring_mm}")
        return bad
