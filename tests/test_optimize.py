"""Loop mechanics, with the solver replaced by a cheap surrogate.

The point is not to check the physics -- that is `test_metrics.py` and the
line-model anchors. It is to check that the loop gates DRC before EM,
starts from the analytic seed, survives a solver that throws, and picks
the winner it says it picked.
"""

import numpy as np
import pytest
import skrf as rf

from rf_agent.devices import get_device
from rf_agent.metrics import evaluate
from rf_agent.objective import FAILED_SOLVE, INFEASIBLE, score
from rf_agent.optimize import Optimiser
from rf_agent.spec import RFSpec


def _surrogate_network(l_arm: float, f_ghz=np.linspace(9.0, 14.0, 101)) -> rf.Network:
    """A divider whose match resonates at a frequency set by the arm length.

    Crude on purpose: it just has to reward the right direction so the
    loop's bookkeeping can be checked without a solver.
    """
    # The optimum sits at l_arm = 5.05, above the analytic seed of 4.61 --
    # the same direction the real structure needs, because the tee and the
    # bends add electrical length the closed-form model cannot see.
    f0 = 11.6 * 5.05 / max(l_arm, 0.5)
    n = len(f_ghz)
    s = np.zeros((n, 3, 3), complex)
    detune = (f_ghz - f0) / 1.5
    s11 = 10 ** (-30 / 20) + 0.55 * detune**2
    s11 = np.clip(s11, 1e-4, 0.99)
    for i in range(3):
        s[:, i, i] = s11
    thru = np.sqrt(np.maximum(0.0, (1 - s11**2) / 2)) * 10 ** (-0.15 / 20)
    s[:, 1, 0] = s[:, 0, 1] = thru
    s[:, 2, 0] = s[:, 0, 2] = thru
    s[:, 1, 2] = s[:, 2, 1] = 10 ** (-24 / 20)
    return rf.Network(frequency=rf.Frequency.from_f(f_ghz, unit="ghz"), s=s)


class FakeOptimiser(Optimiser):
    """Optimiser with `evaluate_geometry` swapped for the surrogate."""

    def __init__(self, *a, fail_on=(), **kw):
        super().__init__(*a, **kw)
        self.fail_on = set(fail_on)
        self.calls = 0

    def evaluate_geometry(self, geom, *, tag, draft):
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("pretend the mesher gave up")
        net = _surrogate_network(geom.params["l_arm"])
        mt = evaluate(net, self.spec, self.device.port_map(self.spec))
        return mt, score(mt, self.spec), {"cached": False, "touchstone": "fake"}


@pytest.fixture
def spec(tmp_path, monkeypatch):
    from rf_agent import config

    monkeypatch.setenv("RF_AGENT_WORKSPACE", str(tmp_path))
    config.reset_settings()
    yield RFSpec.divider(f0_ghz=11.6, n_way=2, name="loop_test")
    config.reset_settings()


def test_trial_zero_is_the_analytic_seed(spec):
    opt = FakeOptimiser(spec, solver=object())
    r = opt.run(n_trials=3, seed=1)
    seeded = get_device(spec.device).seed_params(spec)
    first = opt.history[0]
    assert first.number == 0
    for k, v in seeded.items():
        assert first.params[k] == pytest.approx(v, rel=1e-9)


def test_drc_failures_never_reach_the_solver(spec, monkeypatch):
    # Force every candidate to fail DRC without making the parameter
    # space itself impossible.
    from rf_agent import optimize as optmod

    monkeypatch.setattr(optmod, "check_drc", lambda g, fab: ["forced failure"])
    opt = FakeOptimiser(spec, solver=object())
    with pytest.raises(RuntimeError, match="every one of"):
        opt.run(n_trials=4, seed=1)
    assert opt.calls == 0
    assert opt._counts["drc"] == 4
    assert all(t.loss >= INFEASIBLE for t in opt.history)


def test_an_impossible_stackup_is_named_not_left_to_the_sampler(spec):
    # A fab minimum wider than the arm the impedance needs is a spec
    # conflict, and should say so rather than surfacing as a sampler error.
    spec.stackup.fab.min_trace_mm = 5.0
    with pytest.raises(ValueError, match="fab minimum trace"):
        get_device(spec.device).param_space(spec)


def test_a_thrown_solver_costs_one_trial_not_the_study(spec):
    opt = FakeOptimiser(spec, solver=object(), fail_on={2})
    r = opt.run(n_trials=5, seed=1)
    assert r.n_failed == 1
    assert r.n_solved == 4
    failed = [t for t in opt.history if t.error]
    assert len(failed) == 1
    assert failed[0].loss == FAILED_SOLVE
    # and the study still returns a real winner
    assert r.best_loss < FAILED_SOLVE


def test_the_reported_best_is_the_lowest_loss_seen(spec):
    opt = FakeOptimiser(spec, solver=object())
    r = opt.run(n_trials=8, seed=3)
    solved = [t for t in opt.history if t.metrics]
    assert r.best_loss == pytest.approx(min(t.loss for t in solved))
    best_rec = min(solved, key=lambda t: t.loss)
    for k, v in r.best_params.items():
        assert best_rec.params[k] == pytest.approx(v, rel=1e-6)


def test_the_loop_actually_improves_on_the_seed(spec):
    opt = FakeOptimiser(spec, solver=object())
    r = opt.run(n_trials=15, seed=5)
    seed_loss = opt.history[0].loss
    assert r.best_loss < seed_loss


def test_a_study_resumes_from_its_sqlite_store(spec):
    # n_trials is a target total, not an increment: resuming a study
    # tops it up to the target rather than paying for the search twice.
    a = FakeOptimiser(spec, solver=object())
    a.run(n_trials=3, seed=1)
    b = FakeOptimiser(spec, solver=object())
    r = b.run(n_trials=5, seed=1)
    assert r.n_trials == 5
    assert len(b.history) == 2  # only the two it still owed
    # and it does not re-enqueue the seed on top of the existing run
    assert b.history[0].number == 3


def test_asking_for_no_more_than_a_study_already_has_runs_nothing(spec):
    a = FakeOptimiser(spec, solver=object())
    a.run(n_trials=4, seed=1)
    b = FakeOptimiser(spec, solver=object())
    r = b.run(n_trials=4, seed=1)
    assert r.n_trials == 4
    assert b.history == []
    assert b.calls == 0


def test_every_sampled_parameter_stays_inside_its_range(spec):
    opt = FakeOptimiser(spec, solver=object())
    opt.run(n_trials=10, seed=7)
    space = get_device(spec.device).param_space(spec)
    for t in opt.history:
        for k, v in t.params.items():
            assert space[k].lo - 1e-9 <= v <= space[k].hi + 1e-9
