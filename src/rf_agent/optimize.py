"""The optimisation loop: Optuna over parametric geometry, scored by HFSS.

Three things make this workable on a budget where one sample costs
minutes rather than microseconds:

1. **The first trial is the textbook answer.** The analytic line model's
   seed is enqueued ahead of the sampler, so trial 0 is already a
   plausible divider and TPE starts from a real gradient.
2. **DRC before EM.** A geometry that cannot be fabricated is scored from
   its rule violations alone and never reaches the solver.
3. **Draft then verify.** Exploration runs a coarse mesh and a short
   sweep; only the winner is re-solved at full fidelity. A design that
   looks good in draft and falls over on the real solve is reported as
   exactly that, not quietly accepted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import optuna
from optuna.trial import TrialState

from .config import Settings, get_settings
from .devices import Device, get_device
from .em import EMError, HfssSolver
from .geometry import Geometry, check_drc
from .metrics import Metrics, compliance, evaluate, load
from .objective import FAILED_SOLVE, Score, infeasible_loss, score
from .spec import RFSpec

optuna.logging.set_verbosity(optuna.logging.WARNING)
log = logging.getLogger("rf_agent.optimize")


@dataclass
class TrialRecord:
    number: int
    params: dict[str, float]
    loss: float
    meets_spec: bool = False
    metrics: dict | None = None
    drc: list[str] = field(default_factory=list)
    error: str = ""
    elapsed_s: float = 0.0
    cached: bool = False

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "params": {k: round(v, 5) for k, v in self.params.items()},
            "loss": round(self.loss, 5),
            "meets_spec": self.meets_spec,
            "metrics": self.metrics,
            "drc": self.drc,
            "error": self.error,
            "elapsed_s": round(self.elapsed_s, 1),
            "cached": self.cached,
        }


@dataclass
class StudyResult:
    study_name: str
    storage: str
    n_trials: int
    best_params: dict[str, float]
    best_loss: float
    best_metrics: dict | None
    meets_spec: bool
    history: list[TrialRecord]
    elapsed_s: float
    n_solved: int
    n_drc_rejected: int
    n_failed: int
    #: How many of `n_trials` this particular call actually ran. Zero when
    #: a resumed study was already at its target.
    n_new_trials: int = 0

    def as_dict(self) -> dict:
        return {
            "study_name": self.study_name,
            "storage": self.storage,
            "n_trials": self.n_trials,
            "best_params": {k: round(v, 5) for k, v in self.best_params.items()},
            "best_loss": round(self.best_loss, 5),
            "best_metrics": self.best_metrics,
            "meets_spec": self.meets_spec,
            "elapsed_s": round(self.elapsed_s, 1),
            "counts": {
                "solved": self.n_solved,
                "drc_rejected": self.n_drc_rejected,
                "failed": self.n_failed,
                "new_this_run": self.n_new_trials,
            },
            "history": [t.as_dict() for t in self.history],
        }


def _records_from_study(study) -> tuple[list[TrialRecord], dict[str, int]]:
    """Reconstruct the trial history and counts from the sqlite store.

    The in-process history covers only what this call ran. A study is
    resumable, so the truthful account of how a design was found lives in
    the store and has to be read back from it.
    """
    records: list[TrialRecord] = []
    counts = {"solved": 0, "drc": 0, "failed": 0}
    for t in study.trials:
        if t.state == TrialState.WAITING or t.value is None:
            continue
        metrics = t.user_attrs.get("metrics")
        drc = t.user_attrs.get("drc", [])
        error = t.user_attrs.get("error", "")
        if metrics:
            counts["solved"] += 1
        elif drc:
            counts["drc"] += 1
        elif error:
            counts["failed"] += 1
        records.append(
            TrialRecord(
                number=t.number,
                params=dict(t.params),
                loss=float(t.value),
                meets_spec=bool(t.user_attrs.get("score", {}).get("meets_spec", False)),
                metrics=metrics,
                drc=drc,
                error=error,
                elapsed_s=(
                    (t.datetime_complete - t.datetime_start).total_seconds()
                    if t.datetime_complete and t.datetime_start
                    else 0.0
                ),
            )
        )
    return records, counts


class Optimiser:
    """Runs one study against one spec."""

    def __init__(
        self,
        spec: RFSpec,
        solver: HfssSolver | None = None,
        settings: Settings | None = None,
        study_name: str | None = None,
        draft: bool = True,
    ):
        self.spec = spec
        self.s = settings or get_settings()
        self.s.ensure_dirs()
        self.device: type[Device] = get_device(spec.device)
        self.solver = solver or HfssSolver(self.s)
        self.draft = draft
        self.study_name = study_name or spec.name
        self.history: list[TrialRecord] = []
        self._counts = {"solved": 0, "drc": 0, "failed": 0}

    @property
    def storage_url(self) -> str:
        db = self.s.studies_dir / f"{self.study_name}.db"
        return f"sqlite:///{db.as_posix()}"

    # ------------------------------------------------------------------
    def build(self, params: dict[str, float]) -> Geometry:
        return self.device.build(self.spec, params)

    def evaluate_geometry(
        self, geom: Geometry, *, tag: str, draft: bool
    ) -> tuple[Metrics, Score, dict]:
        """Solve and score one geometry. Raises EMError if the solve fails."""
        res = self.solver.solve(geom, self.spec, tag=tag, draft=draft)
        net = load(res.touchstone)
        mt = evaluate(net, self.spec, self.device.port_map(self.spec))
        sc = score(mt, self.spec)
        return mt, sc, res.as_dict()

    # ------------------------------------------------------------------
    def _objective(self, trial: optuna.Trial) -> float:
        t0 = time.time()
        space = self.device.param_space(self.spec)
        params = {
            k: trial.suggest_float(k, r.lo, r.hi, step=r.step) for k, r in space.items()
        }

        geom = self.build(params)
        drc = check_drc(geom, self.spec.stackup.fab)
        if drc:
            self._counts["drc"] += 1
            trial.set_user_attr("drc", drc)
            rec = TrialRecord(
                number=trial.number, params=params, loss=infeasible_loss(len(drc)),
                drc=drc, elapsed_s=time.time() - t0,
            )
            self.history.append(rec)
            log.info("trial %d rejected by DRC: %s", trial.number, "; ".join(drc[:2]))
            return rec.loss

        try:
            mt, sc, solve_info = self.evaluate_geometry(
                geom, tag=f"{self.study_name}_t{trial.number}", draft=self.draft
            )
        except Exception as e:  # a solver crash must not end the study
            self._counts["failed"] += 1
            msg = f"{type(e).__name__}: {e}"
            trial.set_user_attr("error", msg)
            rec = TrialRecord(
                number=trial.number, params=params, loss=FAILED_SOLVE,
                error=msg, elapsed_s=time.time() - t0,
            )
            self.history.append(rec)
            log.warning("trial %d solve failed: %s", trial.number, msg)
            return FAILED_SOLVE

        self._counts["solved"] += 1
        trial.set_user_attr("metrics", mt.as_dict())
        trial.set_user_attr("score", sc.as_dict())
        trial.set_user_attr("geometry", geom.summary())
        trial.set_user_attr("solve", solve_info)

        rec = TrialRecord(
            number=trial.number, params=params, loss=sc.loss,
            meets_spec=sc.meets_spec, metrics=mt.as_dict(),
            elapsed_s=time.time() - t0, cached=solve_info.get("cached", False),
        )
        self.history.append(rec)
        log.info(
            "trial %d loss=%.4f%s  S11=%.2f iso=%.2f  (%.0fs)",
            trial.number, sc.loss, " OK" if sc.meets_spec else "",
            mt.values.get("s11_db", 0.0), mt.values.get("isolation_db", 0.0),
            rec.elapsed_s,
        )
        return sc.loss

    # ------------------------------------------------------------------
    def run(
        self,
        n_trials: int = 40,  # target total for the study, not an increment
        timeout: float | None = None,
        seed: int | None = 0,
        extra_seeds: Sequence[dict[str, float]] = (),
        callbacks: Sequence[Callable] = (),
    ) -> StudyResult:
        t0 = time.time()
        space = self.device.param_space(self.spec)

        study = optuna.create_study(
            study_name=self.study_name,
            storage=self.storage_url,
            direction="minimize",
            load_if_exists=True,
            sampler=optuna.samplers.TPESampler(
                seed=seed,
                n_startup_trials=max(5, min(12, n_trials // 3)),
                multivariate=True,
            ),
        )

        # Trial 0 is the analytic answer; anything else the caller wants
        # tried first (a previous winner, a hand-tuned starting point)
        # queues behind it.
        if not study.trials:
            study.enqueue_trial({k: r.seed for k, r in space.items()})
        for extra in extra_seeds:
            study.enqueue_trial({k: space[k].clip(v) for k, v in extra.items() if k in space})

        # `n_trials` is a target total, not an increment. A study is
        # resumable by design -- asking for 30 trials twice should leave
        # 30 trials, not 60 and a second bill for the same search.
        # Enqueued-but-not-yet-run trials sit in WAITING and must not
        # count against the budget, or the seed eats one of them.
        already_ran = sum(1 for t in study.trials if t.state != TrialState.WAITING)
        remaining = max(0, n_trials - already_ran)
        study.optimize(
            self._objective,
            n_trials=remaining,
            timeout=timeout,
            n_jobs=1,  # one HFSS solve at a time: licence and cores are the limit
            callbacks=list(callbacks),
            gc_after_trial=True,
        )

        # A DRC-rejected trial still returns a loss, so Optuna counts it as
        # complete and `best_trial` would happily hand back a geometry that
        # was never simulated. The condition that matters is whether
        # anything was actually solved.
        solved = [t for t in study.trials if t.user_attrs.get("metrics")]
        if not solved:
            raise RuntimeError(
                f"every one of {len(study.trials)} trials failed. "
                f"{self._counts['drc']} were rejected by fab rules and "
                f"{self._counts['failed']} could not be solved -- check rf_doctor "
                f"and the DRC on the analytic seed before running a study."
            )
        # Report from the *store*, not from this process's session. A
        # resumed study that had nothing left to run would otherwise
        # report "0 solved", which is false about the design being
        # returned.
        records, counts = _records_from_study(study)
        best = study.best_trial
        return StudyResult(
            study_name=self.study_name,
            storage=self.storage_url,
            n_trials=sum(1 for t in study.trials if t.state != TrialState.WAITING),
            best_params=dict(best.params),
            best_loss=float(best.value),
            best_metrics=best.user_attrs.get("metrics"),
            meets_spec=bool(best.user_attrs.get("score", {}).get("meets_spec", False)),
            history=records,
            elapsed_s=time.time() - t0,
            n_new_trials=len(self.history),
            n_solved=counts["solved"],
            n_drc_rejected=counts["drc"],
            n_failed=counts["failed"],
        )

    # ------------------------------------------------------------------
    def verify(self, params: dict[str, float], tag: str = "verify") -> dict:
        """Re-solve one parameter set at full fidelity and judge it.

        The draft mesh is there to rank candidates cheaply, not to certify
        one. This is the number that goes in the report.
        """
        geom = self.build(params)
        drc = check_drc(geom, self.spec.stackup.fab)
        mt, sc, solve_info = self.evaluate_geometry(geom, tag=tag, draft=False)
        return {
            "params": {k: round(v, 5) for k, v in params.items()},
            "geometry": geom.summary(),
            "drc": drc,
            "metrics": mt.as_dict(),
            "score": sc.as_dict(),
            "compliance": compliance(mt, self.spec),
            "solve": solve_info,
        }


def resume_study(spec: RFSpec, settings: Settings | None = None, study_name: str | None = None):
    """Load a previous study without running anything."""
    s = settings or get_settings()
    name = study_name or spec.name
    db = s.studies_dir / f"{name}.db"
    if not db.exists():
        raise FileNotFoundError(f"no study database at {db}")
    return optuna.load_study(study_name=name, storage=f"sqlite:///{db.as_posix()}")
