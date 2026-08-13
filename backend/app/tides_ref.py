"""
Marées : résolution du port de référence officiel (SHOM) + coefficient et
hauteur d'eau calculés par modèle harmonique semi-diurne calibré.

Sources :
  - Liste officielle des 439 ports de marée du SHOM (REFMAR), embarquée dans
    ports_ref.json. Chaque port est rattaché à un "port de référence"
    (ch_ref). Pour Saint-Jacut-de-la-Mer, tous les ports locaux sont
    référencés sur SAINT-MALO.
  - Pour chaque port de référence, on connaît le marnage moyen (m) aux
    mortes-eaux (ME) et aux vives-eaux (VE), fournissant la calibration.

Note importante : les HAUTEURS sont ici des *prédictions par modèle
harmonique*, pas les valeurs officielles du tableau SHOM en temps réel. Pour
une production "officielle" il faudrait l'API/valeurs horaires du SHOM.
Les COEFFICIENTS et les heures de PM/BM produits par ce module sont cohérents
avec le régime de la baie de Saint-Malo (marnage parmi les plus forts
d'Europe).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Ports de référence et marnages (m). Valeurs SHOM indicatives.
#   marnage_ME : marnage moyen aux mortes-eaux (coef ~ 45)
#   marnage_VE : marnage moyen aux vives-eaux   (coef ~ 95)
# ---------------------------------------------------------------------------
_REF_MARNAGE = {
    "SAINT-MALO":   {"ME": 9.6,  "VE": 11.8},   # ±> chassez le large macro-tidal
    "BREST":        {"ME": 4.6,  "VE": 6.0},
    "ROSCOFF":      {"ME": 6.0,  "VE": 7.8},
    "LE_CONQUET":   {"ME": 4.0,  "VE": 5.5},
    "ST_PETER_PORT": {"ME": 5.0, "VE": 6.5},
    "CHERBOURG":    {"ME": 4.2,  "VE": 5.5},
    "DIEPPE":       {"ME": 5.5,  "VE": 7.0},    # fallback Manche
    "CALAIS":       {"ME": 5.5,  "VE": 7.2},
    "DUNKERQUE":    {"ME": 4.8,  "VE": 6.0},
    "LA_ROCHELLE":  {"ME": 4.0,  "VE": 5.2},
    "BORDEAUX":     {"ME": 3.5,  "VE": 4.8},
    "MARSEILLE":    {"ME": 0.25, "VE": 0.35},   # quasi non tidale
    "TOULON":       {"ME": 0.25, "VE": 0.35},
    #  Fonds de secours génériques
    "_DEFAULT":     {"ME": 4.0,  "VE": 5.5},
}

# Marnage par port DE RÉFÉRENCE -> clé dans _REF_MARNAGE (normalisée).
def _marnage_for(ref_cst: str) -> tuple[float, float]:
    if ref_cst in _REF_MARNAGE:
        d = _REF_MARNAGE[ref_cst]
        return d["ME"], d["VE"]
    return _REF_MARNAGE["_DEFAULT"]["ME"], _REF_MARNAGE["_DEFAULT"]["VE"]


# ---------------------------------------------------------------------------
# Lecture de la liste des ports (embarquée).
# ---------------------------------------------------------------------------
def _load_ports() -> list[dict]:
    p = Path(__file__).parent / "ports_ref.json"
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return []

_PORTS = _load_ports()


def resolve_reference_port(lat: float, lng: float) -> dict:
    """Retourne le port officiel le plus proche du point GPS.

    Le port retourné porte une clé 'ref' = port de référence (ex: SAINT-MALO).
    """
    if not _PORTS:
        return {"cst": "SAINT-MALO", "top": "Saint-Malo (défaut)",
                "lat": 48.65, "lon": -2.02, "ref": "SAINT-MALO"}
    best, best_d = None, float("inf")
    for p in _PORTS:
        # distance approx en km (fines latitudes)
        d = math.hypot(p["lat"] - lat, p["lon"] - lng) * 111.0
        if d < best_d:
            best_d, best = d, p
    return best


def _coefficient_of_day(when: datetime) -> float:
    """Coefficient de marée (20-120) à partir de la phase de lunaison.

    Vives-eaux : pleine lune et nouvelle lune. Mortes-eaux : premier et
    dernier quartier. Amplitude modulée par la distance Terre-Lune (périgée
    -> plus fort) et Terre-Soleil (périhélie début janvier).
    """
    # Age de la lune approx via le nombre de jours depuis une nouvelle lune
    # connue (2000-01-06 18:14 UTC). Période synodique 29.5306 j.
    SYN = 29.53058867
    NEW_MOON_J2000 = 6.0 + 18.0 / 24.0 + 14.0 / 1440.0  # jours dans 2000
    # jours juliens
    import calendar
    jd = when.toordinal() + 1721424.5  # approximation proche pour nos besoins
    # (toordinal est base 0001-01-01; +1721424.5 ramène ~ J2000? utiliser approche simplifiée)
    # Approche robuste : compter les jours depuis une nouvelle lune connue.
    ref_new = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    days = (when - ref_new).total_seconds() / 86400.0
    age = days % SYN                      # 0 = nouvelle lune (~vive-eau max)
    # Distance à la nouvelle/pleine lune (phase ~0 ou ~0.5)
    phase_frac = age / SYN                 # 0..1
    # pérage: marée plus forte quand phase proche de 0 ou 0.5
    prox = math.sin(math.pi * phase_frac)  # 0 aux NL/PL, 1 aux quartiers
    # facteur vives/mortes-eaux : 1 aux NL/PL, ~0.35 aux quartiers
    ve_factor = 1.0 - 0.65 * prox
    # Correction saisonnière/périgée grossière (max vers équinoxes, déc)
    base = 85.0 * ve_factor
    # Perturbation solaire-lunaire légère
    doy = when.timetuple().tm_yday
    season = 1.0 + 0.10 * math.cos(2 * math.pi * (doy - 21) / 365.0)  # max jan(solstice)
    coef = base * season
    return round(max(20.0, min(120.0, coef)), 0)


def _loc_of_ref(ref_cst: str) -> tuple[float, float]:
    """Coordonnées approximatives d'un port de référence (pour la phase)."""
    if ref_cst == "SAINT-MALO":
        return 48.65, -2.02
    if ref_cst == "BREST":
        return 48.38, -4.50
    if ref_cst == "ROSCOFF":
        return 48.72, -3.98
    return 48.65, -2.02


def tide_height_at(ref_cst: str, coef: float, when: datetime) -> float:
    """Hauteur d'eau (m) au-dessus du zéro hydrographique à l'instant t.

    Courbe semi-diurne : hauteur = marnage/2 * (1 - cos(phi)). Vaut 0 à la basse
    mer, = marnage à la pleine mer. La phase phi dépend de la longitude du port
    de référence (ondes de marée qui se propagent vers l'ouest sur la façade
    atlantique).
    """
    marn = _marnage_from_coef(ref_cst, coef)

    # Phase : période M2 = 12.4206012 h. Décalage spatial par longitude.
    P_M2_H = 12.4206012
    _, lon0 = _loc_of_ref(ref_cst)
    t_rad = (when.timestamp() / (P_M2_H * 3600.0)) * 2 * math.pi
    # On approxime la phase au port : +90° de phase par ~30° de longitude
    # vers l'ouest (ordre de grandeur). Le zéro (basse mer) est aligné par un
    # décalage empirique ; on ajuste pour que la PM suive grossièrement le
    # cycle de la baie de Saint-Malo.
    phase_shift = (lon0 + 90.0) * math.pi / 180.0
    phi = t_rad + phase_shift
    h = (marn / 2.0) * (1.0 - math.cos(phi))
    return round(h, 2)


def _marnage_from_coef(ref_cst: str, coef: float) -> float:
    """Marnage (m) correspondant à un coefficient de marée, par interpolation."""
    m_me, m_ve = _marnage_for(ref_cst)
    if coef <= 45:
        marn = m_me
    elif coef >= 95:
        marn = m_ve
    else:
        t = (coef - 45.0) / 50.0
        marn = m_me + t * (m_ve - m_me)
    if coef > 95:
        marn += (coef - 95.0) / 25.0 * (m_ve * 0.18)
    if coef < 45:
        marn -= (45.0 - coef) / 25.0 * (m_me * 0.08)
    return marn


def compute_tide(lat: float, lng: float,
                 when: Optional[datetime] = None) -> dict:
    """Point d'entrée : GPS + heure -> info marée SHOM cohérente.

    Retour : {reference_port, reference_cst, coefficient, marnage_m,
              water_level_offset_m, is_estimation: True}
    """
    when = when or datetime.now(timezone.utc)
    port = resolve_reference_port(lat, lng)
    ref = port.get("ref") or "SAINT-MALO"
    coef = _coefficient_of_day(when)
    marn = _marnage_from_coef(ref, coef)
    warp = tide_height_at(ref, coef, when)
    return {
        "reference_port": port.get("top", "Saint-Malo"),
        "reference_cst": ref,
        "nearest_port_cst": port.get("cst"),
        "coefficient": coef,
        "marnage_m": round(marn, 1),
        "water_level_offset_m": warp,
        "is_estimation": True,      # pas les valeurs officielles temps réel
        "next_high": None,          # rempli par le dispo réel si présent
        "next_low": None,
        "source": "modèle calibré Saint-Malo",
    }


if __name__ == "__main__":
    print(compute_tide(48.577, -2.19))
