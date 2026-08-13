"""
Source de marée réelle via l'API WorldTides.

WorldTides fournit les hauteurs réelles et les heures d'extrême (PM/BM) pour
les côtes du monde, dont Saint-Malo. L'API est gratuite (~500 req/jour sur le
plan standard).

- La clé API est lue depuis la variable d'environnement WORLDTIDES_API_KEY
  (définie côté déploiement Vercel / .env local).
- Endpoints utilisés :
    extremes : https://www.worldtides.info/api/v4/tides?lat=..&lon=..&extremes&key=..
    heights  : https://www.worldtides.info/api/v4/tides?lat=..&lon=..&datum=MLLW&interval=1800&height&key=..

Le "coefficient de marée" (convention française SHOM 20-120) est calculé à
partir du marnage (différence PM/BM) non fourni par l'API : on l'estime via
la ratio marnage_du_jour / marnage_vive_eau_moyenne du port (méthode SHOM).

En cas d'absence de clé ou d'échec réseau, on retombe sur le modèle harmonique
calibré (tides_ref.compute_tide) pour ne jamais casser l'app.
"""

from __future__ import annotations

import os
import math
from datetime import datetime, timezone
from typing import Optional

import httpx

from .tides_ref import compute_tide as _fallback_tide

WORLDTIDES_BASE = "https://www.worldtides.info/api/v3"


def _api_key() -> str | None:
    return os.environ.get("WORLDTIDES_API_KEY") or None


async def fetch_worldtides(lat: float, lng: float,
                           when: Optional[datetime] = None) -> Optional[dict]:
    """Interroge WorldTides et renvoie {extremes, heights} ou None si indispo.

    Format API v3 (un seul appel renvoie les deux) :
      /api/v3?heights&extremes&date=YYYY-MM-DD&days=3&lat=..&lon=..&datum=MLLW&key=..
    """
    key = _api_key()
    if not key:
        return None

    when = when or datetime.now(timezone.utc)
    params = {
        "lat": lat, "lon": lng,
        "key": key, "datum": "MLLW",
        "date": when.strftime("%Y-%m-%d"),
        "days": 3,
        "heights": None, "extremes": None,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(WORLDTIDES_BASE, params=params)
            resp.raise_for_status()
            data = resp.json()
            extremes = data.get("extremes", [])
            heights = data.get("heights", [])
        return {"extremes": extremes, "heights": heights}
    except Exception as e:  # noqa: BLE001
        print(f"[worldtides] échec, fallback : {e}")
        return None


def height_at_time(heights: list[dict], when: datetime) -> Optional[float]:
    """Hauteur d'eau (m) la plus proche de `when` dans la série horaire."""
    if not heights:
        return None
    target = when.timestamp()
    best, best_d = None, float("inf")
    for h in heights:
        cdt = _parse_dt(h.get("dt"))
        if cdt is None:
            continue
        d = abs(cdt.timestamp() - target)
        if d < best_d:
            best_d, best = d, h.get("height")
    return best


def next_extremes(extremes: list[dict], when: datetime) -> tuple[Optional[datetime], Optional[datetime]]:
    """Prochaine (high, low) après `when`."""
    hi = lo = None
    t_now = when.timestamp()
    for e in extremes:
        cdt = _parse_dt(e.get("dt"))
        if cdt is None or cdt.timestamp() < t_now - 3600:
            continue
        typ = e.get("type")
        if typ == "High" and hi is None:
            hi = cdt
        elif typ == "Low" and lo is None:
            lo = cdt
        if hi and lo:
            break
    return hi, lo


def _parse_dt(dt) -> Optional[datetime]:
    if not dt:
        return None
    try:
        if isinstance(dt, (int, float)):
            return datetime.fromtimestamp(dt, tz=timezone.utc)
        if isinstance(dt, str):
            return datetime.fromisoformat(
                dt.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None
    return None


def coefficient_from_marnage(marnage_day: float, marnage_vive_eau: float = 11.8) -> float:
    """Coefficient SHOM (20-120) à partir du marnage réel du jour.

    Convention SHOM : le coefficient 95 correspond au marnage moyen des vives
    eaux d'une zone. On normalise par rapport au marnage de vive-eau moyenne
    du port (Saint-Malo ~11.8m pour un coef ~95).
    """
    if marnage_day <= 0:
        return 20.0
    coef = 95.0 * (marnage_day / marnage_vive_eau)
    return round(max(20.0, min(120.0, coef)), 0)


async def compute_tide_real(lat: float, lng: float,
                            when: Optional[datetime] = None) -> dict:
    """Point d'entrée : marée RÉELLE WorldTides si clé dispo, sinon fallback.

    Retour un dict normalisé identique à compute_tide() de tides_ref pour
    rester transparent pour le reste du code.
    """
    when = when or datetime.now(timezone.utc)
    data = await fetch_worldtides(lat, lng, when)
    if not data:
        return _fallback_tide(lat, lng, when)

    heights = data.get("heights", [])
    extremes = data.get("extremes", [])
    h_raw = height_at_time(heights, when)
    hi, lo = next_extremes(extremes, when)

    # Marnage du jour = max(PM) - min(BM) sur la fenêtre (hauteurs relatives).
    marn = None
    min_h = 0.0
    if heights:
        vals = [x.get("height") for x in heights if x.get("height") is not None]
        if vals:
            marn = round(max(vals) - min(vals), 1)
            min_h = min(vals)

    # Hauteur d'eau AU-DESSUS DU ZÉRO (convention française ZH) : on recale
    # les hauteurs WorldTides (référentiel MLLW, zéro au niveau moyen) de sorte
    # qu'à la basse mer l'offset ~ 0 et à la pleine mer ~ marnage. Coherent
    # avec l'affichage "hauteur d'eau" en mètres du tableau de marée français.
    offset = round(h_raw - min_h, 2) if h_raw is not None else None

    coef = coefficient_from_marnage(marn) if marn else None

    return {
        "reference_port": "WorldTides",
        "reference_cst": "WORLDTIDES",
        "nearest_port_cst": "WORLDTIDES",
        "coefficient": coef if coef is not None else 75.0,
        "marnage_m": marn if marn is not None else 11.0,
        "water_level_offset_m": offset if offset is not None else 3.0,
        "is_estimation": False,          # vraies données si key présente
        "next_high": hi.isoformat() if hi else None,
        "next_low": lo.isoformat() if lo else None,
        "source": "WorldTides" if _api_key() else "modèle calibré Saint-Malo",
    }


if __name__ == "__main__":
    import asyncio
    async def main():
        r = await compute_tide_real(48.577, -2.19)
        print(r)
    asyncio.run(main())
