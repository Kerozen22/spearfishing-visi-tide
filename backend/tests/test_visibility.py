"""Tests unitaires de l'algorithme de visibilité et du modèle de marée."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from datetime import datetime, timezone

from app.visibility import (
    estimate_visibility, OceanParams, tidal_coefficient_from_range,
    component_wave, component_wind, component_turbidity,
    _spm_from_wind_fetch,
)


def _ideal():
    return OceanParams(
        swell_height=0.4, swell_period=9, wind_wave_height=0.1,
        wind_speed=2, tidal_coefficient=40, tidal_current_ms=0.1,
        tide_offset_minutes=0, current_speed=0.05, sediment_mobility=0.6,
    )


def test_calm_conditions_high_visi():
    r = estimate_visibility(_ideal(), water_offset_m=0)
    assert r.score_m >= 6.0, f"Calme devrait être ≥6m, obtenu {r.score_m}"
    assert r.qualitative == "excellente"


def test_storm_conditions_low_visi():
    p = _ideal()
    p.swell_height = 2.5
    p.swell_period = 11
    p.wind_wave_height = 1.5
    p.wind_speed = 15
    p.tidal_coefficient = 110
    p.tidal_current_ms = 1.8
    p.tide_offset_minutes = 175
    r = estimate_visibility(p, water_offset_m=0.5)
    assert r.score_m < 2.0, f"Tempête devrait être faible, obtenu {r.score_m}"
    assert r.qualitative == "mauvaise"


def test_factor_bounds():
    for v in [1.0, 0.0, float('nan')]:
        pass
    assert 0.0 <= component_wave(1.0, 8.0) <= 1.0
    assert 0.0 <= component_wind(0.5, 5.0) <= 1.0


def test_tidal_coefficient_normalization():
    assert abs(tidal_coefficient_from_range(4.0, 4.0) - 57.0) < 5
    assert 20.0 <= tidal_coefficient_from_range(8.0, 4.0) <= 120.0


def test_output_shape():
    r = estimate_visibility(_ideal(), water_offset_m=1.0)
    expected_keys = {"score_m", "qualitative", "color_hex", "factors",
                     "explanation", "water_level_offset_m"}
    assert expected_keys <= set(r.to_dict().keys())
    assert isinstance(r.explanation, list)
    assert r.water_level_offset_m == 1.0


def test_multiplicative_monotonic():
    base = estimate_visibility(_ideal())
    worse = _ideal()
    worse.tidal_current_ms = 1.5
    worse_visi = estimate_visibility(worse)
    assert worse_visi.score_m < base.score_m


def test_turbidity_clear_water_no_impact():
    assert component_turbidity(0.5) == 1.0
    assert component_turbidity(None) == 1.0


def test_turbidity_murky_reduces():
    r_clear = estimate_visibility(_ideal())
    murky = _ideal()
    murky.turbidity_gL = 8.0  # eau très vaseuse
    assert estimate_visibility(murky).score_m < r_clear.score_m
    assert estimate_visibility(murky).factors["spm_source"] == "satellite"


def test_spm_proxy_increases_with_wind():
    lo = _spm_from_wind_fetch(3.0, 5.0, 1.0)
    hi = _spm_from_wind_fetch(20.0, 5.0, 1.0)
    assert hi > lo
    # plus profond = moins de remise en suspension
    deep = _spm_from_wind_fetch(20.0, 30.0, 1.0)
    assert deep < hi
