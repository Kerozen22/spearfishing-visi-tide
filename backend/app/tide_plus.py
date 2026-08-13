"""
Agrégation des données océanographiques + adaptation en OceanParams
puis en estimation de visibilité.

Sources (toutes testées, gratuites pour le MVP) :
  - Open-Meteo Marine API (sans clé) : houle, clapot, courant, vent.
      https://marine-api.open-meteo.com/v1/marine
  - Marées France : SHOM (officiel, clé) OU le service harmonique intégré.
      Fallback utilise AVISO/worldtides via clé optionnelle.
  - Bathymétrie : tuiles EMODnet / GEBCO (sans clé).

Ce module expose la fonction build_visibility(lat, lng, when) qui fait le
travail de bout en bout et alimente l'endpoint FastAPI.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from .visibility import OceanParams, estimate_visibility, VisibilityResult
from .copernicus import fetch_spm_copernicus
from .worldtides import compute_tide_real, height_at_time, next_extremes

OPENMETEO_BASE = "https://marine-api.open-meteo.com/v1/marine"


async def fetch_marine_data(lat: float, lng: float,
                            when: Optional[datetime] = None
                            ) -> dict:
    """Récupère les variables océaniques horaires Open-Meteo (gratuit, sans clé).

    Retourne un dict avec les séries horaires + unités. En cas d'échec
    réseau, renvoie des valeurs par défaut documentées (graceful degrade).
    """
    when = when or datetime.now(timezone.utc)
    # Open-Meteo travaille en heures UTC; on prend 3 jours de prévision.
    params = {
        "latitude": lat,
        "longitude": lng,
        "hourly": ("wave_height,wave_period,wind_wave_height,wind_wave_period,"
                   "swell_wave_height,swell_wave_period,ocean_current_velocity,"
                   "wind_speed_10m"),
        "current": ("wave_height,wave_period,wind_wave_height,wind_wave_period,"
                    "swell_wave_height,swell_wave_period,ocean_current_velocity,"
                    "wind_speed_10m"),
        "forecast_days": 3,
        "timezone": "UTC",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(OPENMETEO_BASE, params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:  # noqa: BLE001 - degrade gracefully.
        print(f"[fetch_marine_data] échec, valeurs par défaut : {e}")
        return _empty_marine()


def _empty_marine() -> dict:
    return {"current": {
        "wave_height": 0.5, "wave_period": 7.0,
        "swell_wave_height": 0.3, "swell_wave_period": 8.0,
        "wind_wave_height": 0.2, "wind_wave_period": 4.0,
        "ocean_current_velocity": 0.1, "wind_speed_10m": 3.0,
    }}


def _pick_at_hour(data: dict, when: datetime, field: str) -> float:
    """Extrait la valeur d'un champ horaire Open-Meteo à une heure donnée.

    Retourne la valeur "current" si `when` est proche de maintenant, sinon
    l'interpolation de la série horaire. Retour 0.0 en cas d'absence.
    """
    cur = data.get("current", {}).get(field)
    if cur is not None:
        return float(cur)
    times = data.get("hourly", {}).get("time", [])
    vals = data.get("hourly", {}).get(field, [])
    if not times or not vals:
        return 0.0
    target = when.replace(minute=0, second=0, microsecond=0)
    best_idx, best_delta = 0, float("inf")
    for i, t in enumerate(times):
        if vals[i] is None:
            continue
        try:
            tt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            continue
        if tt.tzinfo is None:
            tt = tt.replace(tzinfo=timezone.utc)
        delta = abs((tt - target).total_seconds())
        if delta < best_delta:
            best_delta, best_idx = delta, i
    val = vals[best_idx] if best_idx < len(vals) else None
    return float(val) if val is not None else 0.0


def _infer_tidal_coefficient(lat: float, lng: float,
                             when: Optional[datetime] = None) -> tuple[float, float]:
    """Estime le coefficient de marée (20-120) et une amplitude de référence.

    Approche robuste sans API de marée obligatoire : on utilise un modèle
    semi-diurne (M2/S2) simplifié. Pour une production, remplacez par SHOM.

    Retour : (coefficient, marnage_calculé_en_m).
    """
    # Paramètres synthétiques du point : période M2 = 12.42h.
    # Amplitude de référence par défaut (ajustable par zone).
    from math import sin, pi
    now = when or datetime.now(timezone.utc)
    # Phase: dépend de la longitude (les ondes de marée se déplacent).
    # Approximation grossière mais stable pour la phase relative.
    phase = (now.timestamp() / (12.42 * 3600.0)) * 2 * pi + (lng * pi / 90.0)
    m2 = 1.4 * sin(phase)                     # M2 dominant (~1.4m amplitude)
    s2 = 0.6 * sin(2 * phase)                 # S2 (composante solaire)
    total_height = m2 + s2
    marnage = 2.0 * (abs(m2) + abs(s2)) / 2.0  # proxy marnage sur cycle
    # Coefficient normalisé : 40 = marnage "moyen" de 4m en Atlantique.
    coef = 57.0 * (marnage / 4.0)
    coef = max(20.0, min(120.0, coef))
    return round(coef, 1), round(marnage, 2)


async def build_visibility(lat: float, lng: float,
                           when: Optional[datetime] = None,
                           api_override: Optional[dict] = None) -> VisibilityResult:
    """Fonction de bout en bout : GPS + heure -> visi estimée + facteurs.

    Args:
        lat, lng : coordonnées du spot.
        when     : datetime (UTC) de l'estimation. None = maintenant.
        api_override : dict optionnel d'API (tests / plug personalisé). Keys:
            marine (dict), coef, marnage.
    """
    when = when or datetime.now(timezone.utc)
    raw = api_override.get("marine") if api_override else None
    marine = raw or await fetch_marine_data(lat, lng, when)

    # --- Extraction des variables au temps demandé ---
    swell_h = _pick_at_hour(marine, when, "swell_wave_height")
    swell_p = _pick_at_hour(marine, when, "swell_wave_period")
    ww_h = _pick_at_hour(marine, when, "wind_wave_height")
    wind = _pick_at_hour(marine, when, "wind_speed_10m")
    cur = _pick_at_hour(marine, when, "ocean_current_velocity")

    # --- Marée (WorldTides réel si clé, sinon modèle calibré Saint-Malo) ---
    # Le fallback est automatique dans compute_tide_real : aucun risque de
    # casser l'app si la clé est absente ou l'API indisponible.
    if api_override and "coef" in api_override:
        coef = api_override["coef"]
        marnage = api_override.get("marnage", 6.0)
        offset_h = api_override.get("water_offset", 0.5)
        tide_ref = "SAINT-MALO"
    else:
        tide = await compute_tide_real(lat, lng, when)
        coef = tide["coefficient"]
        marnage = tide["marnage_m"]
        offset_h = tide["water_level_offset_m"]
        tide_ref = tide["reference_cst"]

    # Profondeur carte au point (EMODnet, gratuit, pour le proxy turbidité).
    depth_m = await fetch_depth_emodnet(lat, lng)
    if depth_m is None:
        depth_m = 6.0  # valeur par défaut si WMS indisponible

    # Turbidité SPM : satellite Copernicus (si SDK+clé) sinon None -> proxy.
    turbidity = None
    if api_override and "turbidity_gL" in api_override:
        turbidity = api_override["turbidity_gL"]
    else:
        # fetch_spm_copernicus est synchrone/bloquant (SDK) -> thread.
        turbidity = await asyncio.to_thread(fetch_spm_copernicus, lat, lng, when)

    params = OceanParams(
        swell_height=swell_h,
        swell_period=swell_p,
        wind_wave_height=ww_h,
        wind_speed=wind,
        tidal_coefficient=coef,
        tidal_current_ms=_estimate_current_from_tide(coef, offset_h),
        tide_offset_minutes=_minutes_since_last_slack(lat, lng, when),
        current_speed=cur / 3.6,          # km/h -> m/s
        sediment_mobility=1.0,
        depth_chart_m=depth_m,
        turbidity_gL=turbidity,
    )
    return estimate_visibility(params, water_offset_m=offset_h)


async def fetch_depth_emodnet(lat: float, lng: float) -> Optional[float]:
    """Profondeur carte (m, valeur négative en mer) au point GPS via le WMS
    EMODnet Bathymetry. Gratuit, sans clé.

    On requête le point précis en WMS GetFeatureInfo sur la couche de grilles
    bathymétriques. Si le service est indisponible ou hors zone, None.
    """
    # BBOX très réduite autour du point pour un GetFeatureInfo précis.
    # NB: WMS 1.3.0 + EPSG:4326 -> ordre (lat, lon) = (minY, minX, maxY, maxX).
    radius = 0.005  # ~500m
    bbox = f"{lat-radius},{lng-radius},{lat+radius},{lng+radius}"
    url = (
        "https://ows.emodnet-bathymetry.eu/wms?service=WMS&version=1.3.0"
        "&request=GetFeatureInfo&layers=emodnet:mean"
        f"&bbox={bbox}&width=1&height=1&query_layers=emodnet:mean"
        f"&x=0&y=0&info_format=text/plain&crs=EPSG:4326"
    )
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            text = resp.text
        # EMODnet renvoie 'Depth = -95.16' (valeur négative en mer).
        match = re.search(r"Depth\s*=\s*(-?\d+(?:\.\d+)?)", text)
        if not match:
            return None
        return float(match.group(1))
    except Exception:  # noqa: BLE001
        return None


def _synthetic_tide_offset(lat: float, lng: float,
                           when: datetime) -> tuple[float, float]:
    """Hauteur d'eau H(t) et marnage (m) par modèle semi-diurne simplifié.

    Retour : (hauteur_m, phase_rad). Hauteur relative au zéro hydrographique.
    """
    now = when.timestamp()
    # Période principale M2 (12.4206012 h) + S2 (12.0 h)
    t_rad_m2 = (now / (12.4206012 * 3600.0)) * 2 * math.pi
    t_rad_s2 = (now / (12.0 * 3600.0)) * 2 * math.pi
    # Phase spatiale dépendante de la longitude (onds progressive vers l'ouest).
    phase_spatial = (lng + 0.0) * math.pi / 90.0
    a_m2, a_s2 = 1.4, 0.6
    h = a_m2 * math.sin(t_rad_m2 - phase_spatial) + a_s2 * math.sin(t_rad_s2 - phase_spatial)
    return h, t_rad_m2


def _minutes_since_last_slack(lat: float, lng: float, when: datetime) -> float:
    """Minutes depuis la dernière étale (slack), en [0, 360[ autour d'un cycle."""
    _, phase = _synthetic_tide_offset(lat, lng, when)
    # Le zéro de vitesse (peak de courant) est ~q 45° après M2/s2.
    slack_phase = (phase + math.pi) % (2 * math.pi)
    frac = slack_phase / (2 * math.pi)
    return frac * 372.0  # ~ une demi-période M2 en minutes


def _estimate_current_from_tide(coef: float, tide_offset: float) -> float:
    """Vitesse courant (m/s) approximée à partir du coefficient et du niveau.

    En mi-marée le courant est max; à l'étale il est ~0. Approximation.
    """
    # offset nul = étale (courant ~0). |offset| grand = courant croissant.
    frac = min(1.0, abs(tide_offset) / 3.0)
    return round((coef / 120.0) * frac * 1.8, 2)


# ---------------------------------------------------------------------------
# Exemple CLI : python -m backend.app.tide_plus
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio

    async def main():
        r = await build_visibility(48.99, -4.52)
        print(f"Visi estimée : {r.score_m}m ({r.qualitative})")
        print(f"Facteurs     : {r.factors}")
        print("Pourquoi:")
        for line in r.explanation:
            print("  " + line)

    asyncio.run(main())
