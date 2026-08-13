"""
Estimation de la visibilité sous-marine (algorithme composite).

Ce module est AGNOSTIQUE des sources de données : il prend en entrée des
variables océanographiques déjà normalisées et renvoie un score de visibilité
explicable.

La visibilite n'est PAS une donnee d'API. Elle est estimee via un modele
additionnel a poids, inspire des travaux sur la remise en suspension des
sediments (sediment resuspension) cotier. Chaque facteur contribue avec une
influence negative sur un score de base = visibilite maximale theorique.

Modele utilise (explicable) :
   visi = VISI_MAX * product(reduction_i)   (modele multiplicatif -> log-additif)
   reduction_i dans [0,1], 1 = aucun impact, 0 = obstruction totale.

C'est un modele MULTIPLICATIF pluto qu'additif, car c'est plus physique :
deux facteurs defavorables multiplient leur effet (Loi de Beer-Lambert + (1-r)).

References conceptuelles :
  - Beer-Lambert : attenuation exponentielle de la lumiere avec la profondeur
    et la concentration en matieres en suspension (SPM).
  - SPM -> turbidite -> attenuation. Wave orbital velocity Uw provoque la
    resuspension quand elle depasse la vitesse critique de frottement du fond.
    (Soulsby 1997, "Dynamics of Marine Sands").
  - Floculation / coefficient de marie : les forts coefficients brassent plus.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Constantes physiques / seuils calibres
# ---------------------------------------------------------------------------

# Profondeur a laquelle on considere la visi "de surface". Sous la surface la
# lumiere decroit, mais pour le chasseur la visi utile est la couche 0-10m.
VISI_MAX_REFERENCE = 12.0       # metres, visi maximale theorique en eau claire
VISI_MIN_FLOOR = 0.3            # plancher minimum (jamais zero absolu)

# Seuils pour la remise en suspension par les vagues (Uw = wave orbital velocity)
# Soulsby (1997) : pour du sable fin, Uw_crit ~ 0.15-0.25 m/s selon la Taille.
UW_CRIT_SAND = 0.18             # m/s, vitesse ornitale critique (sable fin)
UW_SATURATION = 0.55            # m/s, au-dela la resuspension est max

# Seuil de periode de houle : au-dessous, la houle "courte" penetre mal et
# brasse surtout la surface; au-dela, l'orbite atteint le fond.
PERIOD_SHORT = 5.0
PERIOD_LONG = 12.0

# Vent : clapot local. Au-dela 25 noeuds (~12.9 m/s) la surface est froissee.
WIND_CRIT_MS = 8.0
WIND_SAT_MS = 16.0

# Coefficient de marie (France : de 20 a 120). Forts coefficients = brassage.
COEF_LOW = 45.0
COEF_HIGH = 90.0


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

@dataclass
class OceanParams:
    """Variables océanographiques normalisées, en entrée de l'algorithme.

    Toutes les unités sont SI sauf indication.
    """
    # Houle / vagues
    swell_height: float          # m  (houle : vagues longues, energie distante)
    swell_period: float          # s
    wind_wave_height: float      # m  (clapot : vagues courtes, vent local)
    # Vent
    wind_speed: float            # m/s
    # Marée
    tidal_coefficient: float     # 20-120 (coefficient de marée, France)
    tidal_current_ms: float      # m/s  (vitesse du courant de marée au point)
    tide_offset_minutes: float   # minutes avant/apres l'etale (negative = avant)
    # Courant général / hydrologie
    current_speed: float = 0.0   # m/s  (courant général/hydrodynamique)
    # Fond / sédiment (optionnel, valeur par défaut = sable fin)
    sediment_mobility: float = 1.0  # 0.5 (roche/algues) a 1.5 (vase) -> SPM
    depth_chart_m: Optional[float] = None  # profondeur carte (Z zéro hydro.)
    # Turbidité mesurée par satellite (v2, Copernicus SPM / NASA). Si None,
    # on estime un proxy via le vent + la bathymétrie.
    turbidity_gL: Optional[float] = None  # g/L  (0.5 clair, 10+ vaseux)


@dataclass
class VisibilityResult:
    score_m: float               # visibilité estimée en mètres
    qualitative: str             # "excellente" | "bonne" | "moyenne" | "mauvaise"
    color_hex: str               # couleur pour UI
    factors: dict                # détail des réductions par facteur (logique)
    explanation: list[str]       # phrases explicatives en français
    water_level_offset_m: float  # hauteur d'eau h(t) au point (marée)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Cœur de l'algorithme
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _reduction(p: float, lo: float, hi: float) -> float:
    """Reduction 1->0 : lineaire en log de la contrainte normalisee p.

    p est la variable physique, lo/hi = seuils de confort. On renvoie:
      1.0  si p <= lo (aucun impact)
      0.0  si p >= hi (impact max)
      interpolation log entre les deux.
    """
    if p <= lo:
        return 1.0
    if p >= hi:
        return 0.0
    # interpol. lisse (cos) pour eviter les angles vifs
    t = (p - lo) / (hi - lo)
    return 1.0 - (0.5 - 0.5 * math.cos(math.pi * t))


def component_wave(storm: float, period: float) -> float:
    """Impact de la houle/agitation sur la remise en suspension.

    Le déclencheur est la vitesse orbitale au fond Uw ~ H / T (approximation
    en eau peu profonde : u_max ~ pi*H / T / sinh(k h)). Pour simplifier et
    rester robuste sans la profondeur exacte, on utilise H/T comme proxy de
    l'energie de remise en suspension, module par la periode (period long =
    plus d'orbits atteignant le fond).

    Retour : réduction dans [0,1] de la visibilité due aux vagues.
    """
    if storm <= 0:
        return 1.0
    # ratio "steepness" : high H / low T = vagues courtes qui brassent la surface
    stirring = storm / max(period, 0.1)
    # penalite de periode : period courte (< short) brasse moins le fond,
    # on ne penalise donc moins fort; period longue tres penalisante.
    peri = 1.0
    if period < PERIOD_SHORT:
        peri = 0.6  # clapot court, impact fond reduit
    elif period > PERIOD_LONG:
        peri = 1.3  # grosse houle longue, gros brassage fond
    return _reduction(stirring, 0.08 * peri, 0.35 * peri)


def component_wind(wind_wave_height: float, wind_speed: float) -> float:
    """Impact du vent direct : cloque surface + clapot. Pointe donne visi
    moins bonne pres de surface (reflets, agitation).
    """
    wind = max(wind_wave_height, wind_speed / 15.0)
    return _reduction(wind, 0.15, 0.9)


def component_tide(tidal_coefficient: float, tidal_current_ms: float,
                   tide_offset_minutes: float) -> float:
    """Impact de la maree. Fort coefficient + forte vitesse de courant +
    pleine mi-maree (offset proche de +/- 3h) = max brassage.

    L'etale (offset ~0) est au contraire la fenetre de calme ideal: la visi
    se stabilise. On penalise la mi-maree (vitesse max), pas l'etale.
    """
    # Offsets: 0 = etale. La vitesse de courant au point est deja fournie.
    # On penalise davantage quand min(offset, 3h-offset) -> proche 0
    # (c'est-a-dire peak de flot/jusant ~ mi-maree). offset_minutes entre 0 et 360.
    # Normalisation : la penalisation est max a ~180 min de l'etale.
    peak_proximity = 1.0 - abs(tide_offset_minutes - 180.0) / 180.0
    peak_proximity = _clamp(peak_proximity, 0.0, 1.0)

    tide_drive = (tidal_coefficient / 120.0) * _clamp(tidal_current_ms * 3.0, 0.0, 1.0)
    # combine les deux
    force = 0.6 * tide_drive + 0.4 * peak_proximity
    return 1.0 - 0.9 * force


def component_current(current_speed: float) -> float:
    """Courant general continu (sans marée) : brasse en continu si fort."""
    return _reduction(current_speed, 0.3, 1.5)


def component_sediment(sediment_mobility: float,
                       current_speed: float,
                       tide_drive: float) -> float:
    """Facteur sedimentaire : plus le fond est meuble (vase > sable > roche),
    plus la moindre agitation resuspend et reste en suspension.
    """
    agitation = max(current_speed, tide_drive * 0.5)
    return _reduction(sediment_mobility * (0.5 + agitation), 0.5, 2.5)


def _spm_from_wind_fetch(wind_speed: float,
                         depth_m: float,
                         sediment_mobility: float) -> float:
    """Proxy physique de SPM (Suspended Particulate Matter) à partir du vent
    et de la bathymétrie, sans données satellite.

    Modèle de remise en suspension "fetch-limite" (inspiré Soulsby/Pickrill) :
    le vent crée une onde ; la vitesse orbitale au fond (Uw) décroît avec la
    profondeur (Uw ~ H/T / k² avec k = nombre d'onde). En eaux peu profondes,
    Uw atteint le fond et remet les sédiments en suspension.

    Approximation, calibrée pour du sable fin / vase côtière :
        stim = wind * k  avec  k = exp(-depth / lambda)  (atténuation par profondeur)
        SPM_gL = base + a * stim^n

    Retour SPM estimé en g/L (0.5 = eau claire, 20+ = vaseuse).
    """
    # Atténuation de l'onde avec la profondeur (l'e-folding dépend du fetch).
    # Pour de l'océan ouvert, lambda ~ 4m : à 4m de fond, Uw est ~37% de surface.
    lambda_ = 4.0
    k = math.exp(-max(depth_m, 0.3) / lambda_)
    # Le vent (10m) génère de l'énergie proportionnellement au carré (stress).
    stim = (wind_speed ** 2) * k * sediment_mobility
    # Courbe de réponse : base claire + terme puissance (seuil ~ 4 m2/s2).
    spm = 0.5 + 6.0 * max(0.0, (stim - 3.0)) ** 0.8
    return min(spm, 40.0)


def component_turbidity(turbidity_gL: float) -> float:
    """Impact direct de la turbidité/SPM mesurée (copernicus/NASA) sur la visi.

    Relation empirique : attenuation ~ exponentielle de la concentration.
    visi ≈ 5 / SPM  (règle de pouce pour eau côtière), donc réduit fortement.
    Retour : reduction [0,1] de la visibilité.
    """
    if turbidity_gL is None or turbidity_gL <= 0.5:
        return 1.0  # eau très claire, aucun impact
    # visi_decim ~ K / SPM ; normalisons sur la plage visée
    # SPM=2 g/L -> 2.5m ; SPM=10 -> 0.5m (presque nul)
    visi = 5.0 / max(turbidity_gL, 0.5)
    return _clamp(visi / VISI_MAX_REFERENCE, 0.05, 1.0)


def estimate_visibility(p: OceanParams, water_offset_m: float = 0.0) -> VisibilityResult:
    """Point d'entrée principal. Retourne la visi estimée + facteurs.

    water_offset_m = hauteur d'eau H(t) (marée) au point GPS, en mètres.
    Utile pour l'explication (dilution/effet de profondeur).
    """
    # --- sous-cotes individuelles ---
    r_wave = component_wave(p.swell_height, p.swell_period)
    r_wind = component_wind(p.wind_wave_height, p.wind_speed)

    wave_drive = max(p.tidal_current_ms, (p.swell_height / max(p.swell_period, 0.1)))
    r_tide = component_tide(p.tidal_coefficient, p.tidal_current_ms, p.tide_offset_minutes)
    r_current = component_current(p.current_speed)
    tide_drive_scalar = _clamp(p.tidal_current_ms * 3.0, 0.0, 1.0)
    r_sediment = component_sediment(p.sediment_mobility, p.current_speed, tide_drive_scalar)

    # --- turbidité / SPM (v2) : mesurée par satellite OU estimée par proxy vent+bathy ---
    depth_eff = p.depth_chart_m if p.depth_chart_m is not None else 6.0
    if p.turbidity_gL is not None:
        spm = p.turbidity_gL
        spm_source = "satellite"
    else:
        spm = _spm_from_wind_fetch(p.wind_speed, depth_eff, p.sediment_mobility)
        spm_source = "proxy-vent"
    r_turbidity = component_turbidity(spm)

    # --- produit (modele multiplicatif / Beer-Lambert) ---
    # On met a l'exposant la hauteur d'eau (plus d'eau au-dessus = plus
    # d'atténuation cumulée sur le trajet lumineux). Effet léger.
    water_factor = math.exp(-water_offset_m * 0.02)
    score = (VISI_MAX_REFERENCE * r_wave * r_wind * r_tide * r_current * r_sediment
             * r_turbidity * water_factor)
    score = _clamp(score, VISI_MIN_FLOOR, VISI_MAX_REFERENCE)

    # --- qualitatif diffuser ---
    if score >= 6.0:
        qual, color = "excellente", "#22c55e"
    elif score >= 3.0:
        qual, color = "bonne", "#84cc16"
    elif score >= 1.5:
        qual, color = "moyenne", "#f59e0b"
    else:
        qual, color = "mauvaise", "#ef4444"

    # --- explications (garde les raisons les plus fortes) ---
    expl: list[str] = []
    weakest = sorted(
        [("houle", r_wave), ("vent", r_wind), ("marée", r_tide),
         ("courant", r_current), ("fond/sédiment", r_sediment),
         ("turbidité", r_turbidity)],
        key=lambda kv: kv[1],
    )
    for name, r in weakest:
        if r < 0.85:
            impact = int(round((1.0 - r) * 100))
            expl.append(f"- {name} : réduit la visi d'environ {impact}%.")
    if score >= 6.0:
        expl.insert(0, "Conditions très calmes : la visibilité est optimale.")

    return VisibilityResult(
        score_m=round(score, 1),
        qualitative=qual,
        color_hex=color,
        factors={
            "houle": round(r_wave, 3),
            "vent": round(r_wind, 3),
            "marée": round(r_tide, 3),
            "courant": round(r_current, 3),
            "sédiment": round(r_sediment, 3),
            "turbidité": round(r_turbidity, 3),
            "spm_gL": round(spm, 2),
            "spm_source": spm_source,
            "depth_chart_m": round(depth_eff, 2),
            "hauteur_eau_m": round(water_offset_m, 2),
        },
        explanation=expl,
        water_level_offset_m=round(water_offset_m, 2),
    )


def tidal_coefficient_from_range(range24h_m: float, average_range_m: float = 5.0) -> float:
    """Approximation du coefficient de marée à partir de l'amplitude réelle
    observée du marnage sur 24h. Scalaire grossier mais pratique quand le
    coefficient exact n'est pas fourni par l'API.

    Coefficient 120 ~ marnage très grand en France. On normalise par rapport
    à une amplitude moyenne de référence.
    """
    if average_range_m <= 0:
        return 60.0
    coef = 57.0 * (range24h_m / average_range_m)
    return round(_clamp(coef, 20.0, 120.0), 1)


# ---------------------------------------------------------------------------
# Exemple d'utilisation / smoke test
# ---------------------------------------------------------------------------
def _demo() -> None:
    """Démonstration rapide avec des scénarios types."""
    scenarios = {
        "Idéal été (Bretagne, étale, calme)": OceanParams(
            swell_height=0.4, swell_period=9, wind_wave_height=0.1,
            wind_speed=2, tidal_coefficient=40, tidal_current_ms=0.1,
            tide_offset_minutes=0, current_speed=0.05, sediment_mobility=0.6),
        "Houle + mi-marée (brassage)": OceanParams(
            swell_height=2.2, swell_period=11, wind_wave_height=0.8,
            wind_speed=9, tidal_coefficient=95, tidal_current_ms=1.4,
            tide_offset_minutes=170, current_speed=0.2, sediment_mobility=1.0),
        "Vent fort + fond vaseux": OceanParams(
            swell_height=1.1, swell_period=7, wind_wave_height=1.4,
            wind_speed=14, tidal_coefficient=70, tidal_current_ms=0.6,
            tide_offset_minutes=100, current_speed=0.3, sediment_mobility=1.5),
    }
    for name, params in scenarios.items():
        r = estimate_visibility(params, water_offset_m=1.2)
        print(f"\n== {name} ==")
        print(f"   Visi : {r.score_m}m ({r.qualitative})")
        print(f"   Facteurs : {r.factors}")
        for line in r.explanation:
            print("   " + line)


if __name__ == "__main__":
    _demo()
