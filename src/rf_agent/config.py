"""Machine-local paths and knobs.

Everything that differs between installs lives here and is overridable by
environment variable, so nothing downstream has to know where Ansys or
Altium landed on this particular box.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# The AEDT install this machine actually has. PyAEDT can usually discover a
# version on its own, but pinning it removes a class of "picked the wrong
# one" failures on boxes with several AEDT releases side by side.
DEFAULT_AEDT_VERSION = "2026.1"
DEFAULT_AEDT_ROOT = Path(r"C:\Program Files\ANSYS Inc\v261\AnsysEM")


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else default


@dataclass
class Settings:
    """Resolved configuration for one rf-agent session."""

    # --- workspace -------------------------------------------------------
    # Every run writes under here: HFSS projects, touchstone files, the
    # Optuna sqlite store, reports. Kept out of the source tree so the
    # repo stays clean and a study survives a reinstall.
    workspace: Path = field(
        default_factory=lambda: _env_path(
            "RF_AGENT_WORKSPACE", Path.home() / "RF Agent"
        )
    )

    # --- Ansys -----------------------------------------------------------
    aedt_version: str = field(
        default_factory=lambda: os.environ.get("RF_AGENT_AEDT_VERSION", DEFAULT_AEDT_VERSION)
    )
    aedt_root: Path = field(
        default_factory=lambda: _env_path("RF_AGENT_AEDT_ROOT", DEFAULT_AEDT_ROOT)
    )
    # Headless by default: the optimiser opens and closes AEDT dozens of
    # times and a visible desktop would steal focus every single trial.
    non_graphical: bool = field(
        default_factory=lambda: os.environ.get("RF_AGENT_AEDT_GUI", "0") != "1"
    )
    # Cores handed to the solver per trial. Left at 4 because Optuna runs
    # trials back to back and an over-subscribed box thrashes.
    n_cores: int = field(default_factory=lambda: int(os.environ.get("RF_AGENT_CORES", "4")))
    # Hard ceiling on a single HFSS solve. A geometry that meshes badly can
    # otherwise hang a study overnight; the trial is failed instead.
    solve_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("RF_AGENT_SOLVE_TIMEOUT", "3600"))
    )

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace)

    # --- derived ---------------------------------------------------------
    @property
    def runs_dir(self) -> Path:
        return self.workspace / "runs"

    @property
    def cache_dir(self) -> Path:
        """Solved touchstone files, keyed by geometry+setup hash."""
        return self.workspace / "cache"

    @property
    def studies_dir(self) -> Path:
        return self.workspace / "studies"

    @property
    def ansysedt_exe(self) -> Path:
        return self.aedt_root / "ansysedt.exe"

    def ensure_dirs(self) -> None:
        for d in (self.workspace, self.runs_dir, self.cache_dir, self.studies_dir):
            d.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict:
        return {
            "workspace": str(self.workspace),
            "aedt_version": self.aedt_version,
            "aedt_root": str(self.aedt_root),
            "ansysedt_exe": str(self.ansysedt_exe),
            "ansysedt_present": self.ansysedt_exe.exists(),
            "non_graphical": self.non_graphical,
            "n_cores": self.n_cores,
            "solve_timeout_s": self.solve_timeout_s,
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def reset_settings() -> None:
    """Drop the singleton so the next get_settings() re-reads the env."""
    global _settings
    _settings = None
