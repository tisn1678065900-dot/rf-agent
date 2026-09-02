"""Closed-form grounded-coplanar-waveguide model.

This is the analytic layer that turns an impedance target into a trace
width and an electrical length into a physical one. It is not a
replacement for HFSS -- it is what gives the optimiser a starting point
and a search range that bracket the answer, so the EM loop spends its
solves refining rather than hunting.

Formulation: conformal mapping for conductor-backed CPW (Ghione &
Naldi). Two partial capacitances -- the coplanar one set by k1 = a/b and
the lower-ground one set by k3 = tanh(pi*a/2h)/tanh(pi*b/2h) -- combine
into a filling factor q, and the line follows from that.

Validated against an HFSS-tuned X-band GCPW divider on RO4350B 0.254 mm with
a 0.25 mm gap: the model returns 50.0 ohm at the drawn w = 0.524 mm and
70.8 ohm at the drawn w = 0.275 mm. Both within 0.1% of the drawn intent,
which is why the widths it synthesises can be trusted as a seed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.optimize import brentq
from scipy.special import ellipk

C0 = 299_792_458.0  # m/s


def _kk(k: float) -> float:
    """K(k)/K(k'), the ratio every CPW expression is written in.

    scipy's ellipk takes the parameter m = k^2, not the modulus.
    """
    k = min(max(k, 1e-12), 1.0 - 1e-12)
    kp = math.sqrt(1.0 - k * k)
    return float(ellipk(k * k) / ellipk(kp * kp))


@dataclass(frozen=True)
class LineResult:
    z0: float  # characteristic impedance [ohm]
    eps_eff: float  # effective permittivity
    lambda_g_mm: float  # guided wavelength at the evaluation frequency
    alpha_db_per_mm: float  # total attenuation (dielectric + conductor)

    def quarter_wave_mm(self) -> float:
        return self.lambda_g_mm / 4.0

    def length_for_degrees(self, deg: float) -> float:
        return self.lambda_g_mm * deg / 360.0


def gcpw(
    w_mm: float,
    gap_mm: float,
    h_mm: float,
    er: float,
    freq_hz: float,
    tand: float = 0.0,
    t_mm: float = 0.018,
    roughness_um: float = 0.4,
) -> LineResult:
    """Analyse a grounded coplanar line of width `w_mm` and gap `gap_mm`."""
    if w_mm <= 0 or gap_mm <= 0 or h_mm <= 0:
        raise ValueError("w, gap and h must all be positive")

    a = w_mm / 2.0
    b = a + gap_mm

    k1 = a / b
    k3 = math.tanh(math.pi * a / (2.0 * h_mm)) / math.tanh(math.pi * b / (2.0 * h_mm))

    r1 = _kk(k1)
    r3 = _kk(k3)

    q = r3 / r1
    eps_eff = (1.0 + er * q) / (1.0 + q)
    z0 = 60.0 * math.pi / math.sqrt(eps_eff) / (r1 + r3)

    lam0_mm = C0 / freq_hz * 1000.0
    lam_g_mm = lam0_mm / math.sqrt(eps_eff)

    alpha = _attenuation_db_per_mm(
        z0, eps_eff, er, tand, freq_hz, w_mm, gap_mm, t_mm, roughness_um
    )
    return LineResult(z0=z0, eps_eff=eps_eff, lambda_g_mm=lam_g_mm, alpha_db_per_mm=alpha)


def _attenuation_db_per_mm(
    z0: float,
    eps_eff: float,
    er: float,
    tand: float,
    freq_hz: float,
    w_mm: float,
    gap_mm: float,
    t_mm: float,
    roughness_um: float,
) -> float:
    """Dielectric + conductor loss.

    The dielectric term is exact for a TEM-ish line. The conductor term is
    a first-order incremental-inductance estimate with a Hammerstad
    roughness multiplier -- good to a few tens of percent, which is enough
    to rank candidate geometries and to sanity-check an HFSS insertion
    loss, not enough to quote in a report.
    """
    lam0_m = C0 / freq_hz
    # Dielectric: alpha_d = 27.3 * (er/sqrt(eps_eff)) * ((eps_eff-1)/(er-1)) * tand / lambda0
    if er > 1.0 and tand > 0.0:
        a_d_per_m = (
            27.3
            * (er / math.sqrt(eps_eff))
            * ((eps_eff - 1.0) / (er - 1.0))
            * tand
            / lam0_m
        )
    else:
        a_d_per_m = 0.0

    # Conductor: skin depth in copper, current crowded into the trace edges
    # and the two gap walls.
    sigma = 5.8e7
    mu0 = 4e-7 * math.pi
    delta_m = math.sqrt(2.0 / (2.0 * math.pi * freq_hz * mu0 * sigma))
    rs = 1.0 / (sigma * delta_m)
    # Hammerstad: roughness raises Rs by up to 2x as rms/delta grows.
    rq_m = roughness_um * 1e-6
    k_rough = 1.0 + (2.0 / math.pi) * math.atan(1.4 * (rq_m / delta_m) ** 2)
    # Effective current-carrying perimeter of the strip plus the two edges.
    w_m = w_mm * 1e-3
    t_m = max(t_mm, 1e-3) * 1e-3
    perim_m = 2.0 * (w_m + t_m)
    a_c_per_m = rs * k_rough / (z0 * perim_m) * 0.5
    a_c_db_per_m = 8.686 * a_c_per_m

    return (a_d_per_m + a_c_db_per_m) / 1000.0  # per mm


def width_for_z0(
    z0_target: float,
    gap_mm: float,
    h_mm: float,
    er: float,
    freq_hz: float,
    w_bounds: tuple[float, float] = (0.02, 20.0),
) -> float:
    """Solve for the trace width that hits `z0_target` at a fixed gap.

    Fixed gap, solved width: that is the manufacturable choice, because
    the gap is what a single Altium clearance rule reproduces across the
    whole board while the width is per-track anyway.
    """

    def err(w: float) -> float:
        return gcpw(w, gap_mm, h_mm, er, freq_hz).z0 - z0_target

    lo, hi = w_bounds
    f_lo, f_hi = err(lo), err(hi)
    if f_lo * f_hi > 0:
        raise ValueError(
            f"Z0={z0_target} unreachable with gap={gap_mm} h={h_mm} er={er} "
            f"for w in {w_bounds} (Z0 spans {gcpw(hi, gap_mm, h_mm, er, freq_hz).z0:.1f}"
            f"..{gcpw(lo, gap_mm, h_mm, er, freq_hz).z0:.1f} ohm)"
        )
    return float(brentq(err, lo, hi, xtol=1e-6))


def quarter_wave_len(
    z0: float, gap_mm: float, h_mm: float, er: float, freq_hz: float
) -> tuple[float, float]:
    """(width, quarter-wave length) for an impedance at a frequency."""
    w = width_for_z0(z0, gap_mm, h_mm, er, freq_hz)
    r = gcpw(w, gap_mm, h_mm, er, freq_hz)
    return w, r.quarter_wave_mm()


def max_via_pitch(er: float, freq_hz: float, divisor: float = 12.0) -> float:
    """Coarsest ground-via pitch that still behaves like a wall.

    lambda_g/12 in the *substrate* is the usual house rule; using er
    rather than eps_eff is the conservative reading.
    """
    lam0_mm = C0 / freq_hz * 1000.0
    return lam0_mm / math.sqrt(er) / divisor
