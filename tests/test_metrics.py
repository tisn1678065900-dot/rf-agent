"""Metrics are checked against networks whose right answer is known by
construction, so a change in the metric layer cannot quietly redefine
what "meets spec" means."""

import numpy as np
import pytest
import skrf as rf

from rf_agent.metrics import compliance, evaluate, usable_bandwidth_ghz
from rf_agent.objective import score
from rf_agent.spec import RFSpec, Target

PORT_MAP = {"input": [1], "outputs": [2, 3]}


def _network(s: np.ndarray, f_ghz=np.linspace(10.0, 13.0, 61)) -> rf.Network:
    return rf.Network(frequency=rf.Frequency.from_f(f_ghz, unit="ghz"), s=s)


def ideal_divider(n=61, loss_db=0.0, imbalance_db=0.0, s11_db=-40.0, iso_db=-30.0):
    """A perfect 1:2 split, optionally spoiled in one specific way."""
    s = np.zeros((n, 3, 3), complex)
    a = 10 ** (-(3.0103 + loss_db) / 20.0)
    b = a * 10 ** (imbalance_db / 20.0)
    s[:, 1, 0] = s[:, 0, 1] = a
    s[:, 2, 0] = s[:, 0, 2] = b
    for i in range(3):
        s[:, i, i] = 10 ** (s11_db / 20.0)
    s[:, 1, 2] = s[:, 2, 1] = 10 ** (iso_db / 20.0)
    return _network(s, np.linspace(10.0, 13.0, n))


def spec(**kw) -> RFSpec:
    return RFSpec.divider(f0_ghz=11.6, n_way=2, **kw)


def test_ideal_divider_has_no_excess_loss():
    m = evaluate(ideal_divider(), spec(), PORT_MAP)
    assert m["excess_loss_db"] == pytest.approx(0.0, abs=0.01)


def test_dissipation_shows_up_as_excess_loss():
    m = evaluate(ideal_divider(loss_db=0.4), spec(), PORT_MAP)
    assert m["excess_loss_db"] == pytest.approx(0.4, abs=0.02)


def test_amplitude_imbalance_is_the_spread_between_outputs():
    m = evaluate(ideal_divider(imbalance_db=-0.5), spec(), PORT_MAP)
    assert m["amplitude_imbalance_db"] == pytest.approx(0.5, abs=0.01)


def test_isolation_and_return_loss_read_straight_through():
    m = evaluate(ideal_divider(s11_db=-22.0, iso_db=-17.0), spec(), PORT_MAP)
    assert m["s11_db"] == pytest.approx(-22.0, abs=0.01)
    assert m["isolation_db"] == pytest.approx(-17.0, abs=0.01)
    assert m["output_return_loss_db"] == pytest.approx(-22.0, abs=0.01)


def test_phase_imbalance_ignores_common_delay():
    n = 61
    s = ideal_divider(n=n).s.copy()
    # same extra delay on both outputs: the part is longer, not unbalanced
    s[:, 1, 0] *= np.exp(-1j * 1.2)
    s[:, 2, 0] *= np.exp(-1j * 1.2)
    m = evaluate(_network(s, np.linspace(10.0, 13.0, n)), spec(), PORT_MAP)
    assert m["phase_imbalance_deg"] == pytest.approx(0.0, abs=1e-6)


def test_phase_imbalance_catches_a_real_difference():
    n = 61
    s = ideal_divider(n=n).s.copy()
    s[:, 2, 0] *= np.exp(-1j * np.deg2rad(7.0))
    m = evaluate(_network(s, np.linspace(10.0, 13.0, n)), spec(), PORT_MAP)
    assert m["phase_imbalance_deg"] == pytest.approx(7.0, abs=0.01)


def test_metrics_are_worst_case_over_the_band_not_average():
    n = 61
    f = np.linspace(10.0, 13.0, n)
    net = ideal_divider(n=n, s11_db=-40.0)
    s = net.s.copy()
    # one bad point inside the band
    k = int(np.argmin(np.abs(f - 11.6)))
    s[k, 0, 0] = 10 ** (-8.0 / 20.0)
    m = evaluate(_network(s, f), spec(), PORT_MAP)
    assert m["s11_db"] == pytest.approx(-8.0, abs=0.01)
    assert m.worst_at_ghz["s11_db"] == pytest.approx(11.6, abs=0.06)


def test_compliance_and_score_agree_on_a_pass():
    sp = spec()
    m = evaluate(ideal_divider(s11_db=-30, iso_db=-25, loss_db=0.1), sp, PORT_MAP)
    c = compliance(m, sp)
    s = score(m, sp)
    assert c["meets_spec"] is True
    assert s.meets_spec is True
    assert s.penalty == 0.0
    # a compliant design still gets a gradient to improve along
    assert s.loss < 0.0


def test_a_hard_miss_fails_and_costs_penalty():
    sp = spec()
    m = evaluate(ideal_divider(s11_db=-9.0), sp, PORT_MAP)
    c = compliance(m, sp)
    s = score(m, sp)
    assert c["meets_spec"] is False
    assert s.meets_spec is False
    assert s.penalty > 0
    assert s.loss > 0


def test_soft_target_miss_does_not_fail_the_spec():
    sp = RFSpec.divider(f0_ghz=11.6)
    sp.targets = [Target(metric="phase_imbalance_deg", op="<=", limit=1.0, hard=False)]
    n = 61
    s = ideal_divider(n=n).s.copy()
    s[:, 2, 0] *= np.exp(-1j * np.deg2rad(9.0))
    m = evaluate(_network(s, np.linspace(10.0, 13.0, n)), sp, PORT_MAP)
    assert compliance(m, sp)["meets_spec"] is True
    assert score(m, sp).penalty > 0


def test_band_outside_the_sweep_is_an_error_not_a_silent_zero():
    sp = RFSpec.divider(f0_ghz=11.6)
    sp.band_ghz = (20.0, 21.0)
    sp.f0_ghz = 20.5
    with pytest.raises(ValueError, match="does not cover"):
        evaluate(ideal_divider(), sp, PORT_MAP)


def test_usable_bandwidth_spans_the_contiguous_region():
    n = 121
    f = np.linspace(10.0, 13.0, n)
    s = np.zeros((n, 3, 3), complex)
    # a match that is good only between 11 and 12 GHz
    depth = np.where((f > 11.0) & (f < 12.0), 10 ** (-25 / 20), 10 ** (-5 / 20))
    s[:, 0, 0] = depth
    lo, hi = usable_bandwidth_ghz(_network(s, f), 1, -15.0)
    assert lo == pytest.approx(11.0, abs=0.05)
    assert hi == pytest.approx(12.0, abs=0.05)
