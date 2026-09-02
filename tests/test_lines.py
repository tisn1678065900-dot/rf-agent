"""The line model is the whole toolchain's anchor -- if it drifts, every
seed and every search range drifts with it."""

import math

import pytest

from rf_agent.lines import gcpw, max_via_pitch, quarter_wave_len, width_for_z0

# RO4350B 0.254 mm, 0.25 mm coplanar gap, X band -- the cross-section the
# reference divider was built and correlated on. Its two drawn widths came
# out of HFSS tuning, so they are a real external check.
XBAND = dict(gap_mm=0.25, h_mm=0.254, er=3.66, freq_hz=11.6e9)


@pytest.mark.parametrize(
    "width,expected_z0",
    [
        (0.524, 50.0),   # drawn 50 ohm line
        (0.275, 70.711),  # drawn 70.7 ohm quarter-wave arm
    ],
)
def test_matches_hfss_tuned_widths(width, expected_z0):
    z = gcpw(width, **XBAND).z0
    assert z == pytest.approx(expected_z0, abs=0.15)


def test_width_synthesis_round_trips():
    for z0 in (35.0, 50.0, 70.711, 90.0):
        w = width_for_z0(z0, XBAND["gap_mm"], XBAND["h_mm"], XBAND["er"], XBAND["freq_hz"])
        assert gcpw(w, **XBAND).z0 == pytest.approx(z0, rel=1e-4)


def test_narrower_line_is_higher_impedance():
    wide = gcpw(0.8, **XBAND).z0
    narrow = gcpw(0.2, **XBAND).z0
    assert narrow > wide


def test_eps_eff_between_air_and_substrate():
    r = gcpw(0.524, **XBAND)
    assert 1.0 < r.eps_eff < XBAND["er"]


def test_guided_wavelength_and_quarter_wave_agree():
    r = gcpw(0.275, **XBAND)
    lam0 = 299_792_458.0 / XBAND["freq_hz"] * 1000.0
    assert r.lambda_g_mm == pytest.approx(lam0 / math.sqrt(r.eps_eff), rel=1e-9)
    assert r.quarter_wave_mm() == pytest.approx(r.lambda_g_mm / 4.0)


def test_quarter_wave_len_helper():
    w, q = quarter_wave_len(70.711, XBAND["gap_mm"], XBAND["h_mm"], XBAND["er"], XBAND["freq_hz"])
    assert w == pytest.approx(0.2753, abs=2e-3)
    assert q == pytest.approx(4.006, abs=0.02)


def test_loss_is_positive_and_grows_with_tand():
    lossless = gcpw(0.524, tand=0.0, **XBAND).alpha_db_per_mm
    lossy = gcpw(0.524, tand=0.0037, **XBAND).alpha_db_per_mm
    assert 0 < lossless < lossy


def test_via_pitch_rule():
    # lambda_g/12 in the substrate at 11.6 GHz on er=3.66
    assert max_via_pitch(3.66, 11.6e9) == pytest.approx(1.126, abs=0.01)


def test_unreachable_impedance_is_reported_not_guessed():
    with pytest.raises(ValueError, match="unreachable"):
        width_for_z0(500.0, XBAND["gap_mm"], XBAND["h_mm"], XBAND["er"], XBAND["freq_hz"])
