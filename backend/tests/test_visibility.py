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


# --- Raffinements v3 : profondeur du fond, hauteur d'eau, inertie sédimentaire ---

def test_wave_reduced_on_deep_tombant():
    """Même houle produit moins de resuspension sur un tombant profond qu'un
    haut-fond (l'orbite des vagues se dissipe avant d'atteindre le fond)."""
    p_shallow = _ideal()
    p_shallow.swell_height = 2.0
    p_shallow.depth_chart_m = 3.0      # haut-fond
    p_deep = _ideal()
    p_deep.swell_height = 2.0
    p_deep.depth_chart_m = 25.0        # tombant
    r_shallow = estimate_visibility(p_shallow)
    r_deep = estimate_visibility(p_deep)
    assert r_deep.score_m > r_shallow.score_m, (
        f"Le tombant devrait avoir une meilleure visi que le haut-fond")
    assert p_shallow.depth_chart_m < p_deep.depth_chart_m


def test_water_factor_high_water_better():
    """Plus d'eau sur un fond dilue les sédiments -> visi meilleure en eau
    haute qu'en eau basse (correction physique : l'ancien sens pénalisait
    la pleine mer, ce qui était faux)."""
    p = _ideal()
    p.depth_chart_m = 3.0
    low = estimate_visibility(p, water_offset_m=0.5)      # eau très basse
    high = estimate_visibility(p, water_offset_m=6.0)     # eau haute
    assert high.score_m >= low.score_m, (
        f"Eau haute devrait améliorer (dilution), obtenu low={low.score_m} high={high.score_m}")


def test_inertia_penalizes_after_stirring():
    """L'agitation passée (mémoire sédimentaire) pénalise même si le vent
    est retombé maintenant."""
    calm = _ideal()
    calm_same_but_recent = _ideal()
    calm_same_but_recent.past_stirring = 0.9
    r_none = estimate_visibility(calm)
    r_recent = estimate_visibility(calm_same_but_recent)
    assert r_recent.score_m < r_none.score_m
    assert r_none.factors["brassage_passé"] == 1.0
    assert r_recent.factors["brassage_passé"] < 1.0


def test_inertia_zero_no_effect():
    """Sans historique de brassage, le facteur inertie est neutre (1.0)."""
    r = estimate_visibility(_ideal())
    assert r.factors["brassage_passé"] == 1.0


# --- v4 : direction du vent (vent de mer vs vent de terre) ---

def test_wind_direction_component():
    """Le vent de mer pénalise la visi, le vent de terre et l'inconnu non."""
    from app.visibility import component_wind_direction as cwd, _angle_diff
    assert cwd(303, 310) < 1.0          # vent de large (mer) -> pénalité
    assert cwd(130, 310) == 1.0         # vent de terre opposé -> neutre
    assert cwd(310, 310) <= 0.8         # plein vent de mer -> forte pénalité
    assert cwd(None, 310) == 1.0        # direction inconnue -> neutre
    assert cwd(303, None) == 1.0
    # _angle_diff minimal
    assert _angle_diff(350, 10) == 20.0


def test_wind_sea_degrades_visi_but_land_neutral():
    """À vitesse identique, un vent de mer dégrade la visi vs un vent de terre."""
    p_sea = _ideal()
    p_sea.wind_direction_deg = 310.0
    p_sea.ocean_sector_deg = 310.0
    p_land = _ideal()
    p_land.wind_direction_deg = 130.0   # opposé, vent de terre
    p_land.ocean_sector_deg = 310.0
    r_sea = estimate_visibility(p_sea)
    r_land = estimate_visibility(p_land)
    assert r_sea.score_m < r_land.score_m
    assert r_sea.factors["vent_de_mer"] < 1.0
    assert r_land.factors["vent_de_mer"] == 1.0
