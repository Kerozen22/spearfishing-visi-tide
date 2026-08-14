"""Tests du module de marée calibré (tides_ref) : port de référence SHOM,
coefficient et hauteur d'eau cohérents.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from datetime import datetime, timezone, timedelta

from app.tides_ref import (
    compute_tide, resolve_reference_port, tide_height_at,
    _coefficient_of_day, _marnage_from_coef, _marnage_for, _msl_for,
)


def test_resolve_reference_port_saint_jacut():
    """Autour de Saint-Jacut, le port de référence doit être SAINT-MALO."""
    port = resolve_reference_port(48.577, -2.19)
    assert port["ref"] == "SAINT-MALO"
    # le plus proche doit être l'Île des Hébihens (vue sur data.shom.fr)
    assert port["cst"] in ("LES_OITELLIERES", "SAINT_BRIAC_SUR_MER", "SAINT-CAST")


def test_coefficient_bounds():
    """Le coefficient doit rester dans la plage SHOM 20-120."""
    t0 = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
    for i in range(0, 40):
        w = t0 + timedelta(days=i)
        c = _coefficient_of_day(w)
        assert 20.0 <= c <= 120.0, f"coef {c} hors plage à j+{i}"


def test_marnage_monotonic_with_coefficient():
    """Le marnage augmente avec le coefficient (20 -> 120)."""
    m20 = _marnage_from_coef("SAINT-MALO", 20)
    m45 = _marnage_from_coef("SAINT-MALO", 45)
    m95 = _marnage_from_coef("SAINT-MALO", 95)
    m120 = _marnage_from_coef("SAINT-MALO", 120)
    assert m20 <= m45 <= m95 <= m120
    # Saint-Malo est macro-tidal : marnage VE > 10m
    assert m95 > 10.0


def test_water_level_within_marnage():
    """La hauteur oscille autour du niveau moyen (MSL) et le marnage observé
    PM-BM sur la courbe vaut bien le marnage du coefficient.

    Nouvelle physique (corrige la corrélation coef/hauteur) : h(t) = MSL +
    (marnage/2)*cos(phi). La PM ~ MSL+marnage/2, la BM ~ MSL-marnage/2.
    Aux coefs moyens la BM n'est PAS écrasée à 0.
    """
    ref = "SAINT-MALO"
    coef = 75.0
    marn = _marnage_from_coef(ref, coef)
    msl = _msl_for(ref)
    t0 = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
    heights = [tide_height_at(ref, coef, t0 + timedelta(minutes=m)) for m in range(0, 12 * 60, 15)]
    # La hauteur reste dans un intervalle ~centré sur MSL, borné par
    # MSL ± marnage/2 (tolérance d'arrondi). Peut être légèrement négative
    # à la basse mer des grandes marées (champ macro-tidal) : pas de clamp.
    assert all(h <= msl + marn / 2.0 + 0.01 for h in heights)
    assert all(h >= msl - marn / 2.0 - 0.01 for h in heights)
    # Le MARNAGE observé (PM - BM sur la courbe) vaut bien celui du coefficient.
    observed = max(heights) - min(heights)
    assert abs(observed - marn) < 0.5
    # La PM atteint bien ~MSL + marnage/2.
    assert max(heights) > msl + marn / 2.0 - 0.2
    # Aux coef moyens, la BM reste positive (pas faussement écrasée à 0).
    assert min(heights) > 0.1


def test_compute_tide_shape():
    """compute_tide renvoie les bonnes clés et valeurs cohérentes."""
    w = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)
    r = compute_tide(48.577, -2.19, w)
    assert r["reference_cst"] == "SAINT-MALO"
    assert 20.0 <= r["coefficient"] <= 120.0
    # La hauteur d'eau oscille autour du MSL : PM ~ msl+marn/2, BM ~ msl-marn/2.
    msl = r["marnage_m"] and _msl_for("SAINT-MALO")
    marn = r["marnage_m"]
    assert msl - marn / 2.0 - 1.0 <= r["water_level_offset_m"] <= msl + marn / 2.0 + 1.0
    assert r["is_estimation"] is True
    assert "reference_port" in r and r["reference_port"]
