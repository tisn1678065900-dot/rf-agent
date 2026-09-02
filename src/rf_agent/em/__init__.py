"""EM solving. Today HFSS through PyAEDT; the interface is the seam."""

from .hfss import EMError, HfssSolver, SolveResult, geometry_digest

__all__ = ["HfssSolver", "SolveResult", "EMError", "geometry_digest"]
