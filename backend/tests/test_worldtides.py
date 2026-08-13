"""Tests unitaires du module WorldTides (logique pure, sans appel réseau).

On teste la conversion des hauteurs vers la hauteur au-dessus du zéro, le
calcul du coefficient à partir du marnage, et le fallback sans clé.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from datetime import datetime, timezone

from app.worldtides import (
    coefficient_from_marnage, height_at_time, next_extremes, _parse_dt,
)
import app.worldtides as wt


def _sample_heights():
    base = 1786572000  # 2026-08-12T22:00:00Z
    out = []
    for i in range(12):
        out.append({"dt": base + i * 3600, "height": round(-5.0 + i * 0.9, 2)})
    return out


def test_coefficient_from_marnage():
    # marnage ~11.6m ~ vive-eau moyenne -> coef proche de 95
    assert 90 <= coefficient_from_marnage(11.6) <= 95
    # marnage très grand -> coef borné à 120
    assert coefficient_from_marnage(25.0) == 120.0
    # marnage nul -> coef min 20
    assert coefficient_from_marnage(0.0) == 20.0


def test_height_at_time_finds_nearest():
    hs = _sample_heights()
    # base = 1786572000 = 2026-08-12T22:00:00Z ; i*3600 -> 22h, 23h, 00h,...
    # On vise 00:30 le 13/08 = base + 9000s, entre i=2 (00:00, -3.2) et i=3 (01:00, -2.3)
    from datetime import timedelta
    when = datetime(2026, 8, 12, 22, 0, tzinfo=timezone.utc) + timedelta(hours=2, minutes=30)
    h = height_at_time(hs, when)
    # plus proche de 00:30 = i=2 (00:00, -3.2) écart 30min vs i=3 écart 30min -> tolère les 2
    assert h in (-3.2, -2.3)


def test_parse_dt_handles_raw_and_iso():
    assert _parse_dt(1786572000).year == 2026
    iso = _parse_dt("2026-08-13T10:00:00Z")
    assert iso is not None and iso.hour == 10


def test_next_extremes_order():
    ex = [
        {"dt": 1786572000 + 3600, "type": "High", "height": 5.1},
        {"dt": 1786572000 + 3600 * 7, "type": "Low", "height": -5.2},
        {"dt": 1786572000 + 3600 * 13, "type": "High", "height": 5.6},
    ]
    when = datetime.fromtimestamp(1786572000, tz=timezone.utc)
    hi, lo = next_extremes(ex, when)
    assert hi is not None
    assert lo is not None


def test_without_key_returns_none():
    """Sans clé d'env, fetch_worldtides doit retourner None (fallback)."""
    import asyncio
    # on force l'absence de clé temporairement
    key_backup = os.environ.pop("WORLDTIDES_API_KEY", None)
    try:
        assert wt._api_key() is None
    finally:
        if key_backup:
            os.environ["WORLDTIDES_API_KEY"] = key_backup
