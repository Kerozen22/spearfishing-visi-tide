"""
Connecteur Copernicus Marine — turbidité / SPM (Suspended Particulate Matter).

En production, ce module interroge le produit Copernicus Marine (Sentinel-3
OLCI / MODIS) pour obtenir la concentration en SPM (g/m³) au point GPS et au
temps demandé, et l'injecter dans l'algorithme de visibilité (mode "satellite").

Configuration (env) :
  COP_MARINE_USER      : identifiant my.copernicus.eu
  COP_MARINE_PASSWORD  : mot de passe my.copernicus.eu
  COP_MARINE_PRODUCT   : nom du produit, défaut "cmems_obs-oc_glo_bgc-plankton_nrt_l4-multi-1km_P1D"
                         (chl-a / SPM global Near-Real-Time L4, 1km)

Le SDK officiel 'copernicusmarine' (~ pip install copernicusmarine) est utilisé
s'il est présent. S'il n'est PAS installé (ou pas de credentials), ce module
renvoie None -> l'algorithme retombe sur le proxy vent+bathymétrie intégré
(qui fonctionne SANS clé). C'est par design : la plateforme reste utilisable
sans aucun compte, et se précise quand les clés sont branchées.

Les données SPM avec le SDK (subsets) :
    copernicusmarine subset
        --dataset-id <PRODUCT>
        --variable total_suspended_matter
        --minimum-longitude/-latitude/... pour le point
        --start-datetime <t0> --end-datetime <t1>
        --output-directory <dir>   -> écrit un NetCDF
    puis lecture du NetCDF (xarray) à la coordonnée la plus proche.

NOTE IMPORTANTE : le produit exact SPM varie selon la zone (IBI, NORTHWEST,
GLOBAL). Copernicus organise les produits par bassin. Ce module expose un
factory `get_spm(lat, lng, when)` qui peut être étendu par zone.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Optional


def _sdk_available() -> bool:
    try:
        import copernicusmarine  # noqa: F401
        return True
    except ImportError:
        return False


def _credentials() -> Optional[tuple[str, str]]:
    user = os.getenv("COP_MARINE_USER")
    pwd = os.getenv("COP_MARINE_PASSWORD")
    if user and pwd:
        return user, pwd
    return None


def fetch_spm_copernicus(lat: float, lng: float,
                         when: Optional[datetime] = None,
                         product: Optional[str] = None) -> Optional[float]:
    """Récupère le SPM (g/m³) au point/temps via Copernicus Marine.

    Retourne None si SDK absent, credentials absentes, hors couverture ou
    erreur réseau — dans tous ces cas l'algorithme utilise le proxy interne.

    This is a scaffolding implementation: real-world usage requires a valid
    my.copernicus.eu account and the correct per-basin dataset-id. The snippet
    below shows the exact SDK call for a global 1km NRT L4 product.
    """
    if not _sdk_available():
        return None
    creds = _credentials()
    if not creds:
        return None

    when = when or datetime.now(timezone.utc)
    # SPM (total suspended matter) global Near-Real-Time 1km, daily L4.
    product = product or "cmems_obs-oc_glo_bgc-plankton_nrt_l4-multi-1km_P1D"
    import xarray as xr

    try:
        import copernicusmarine as cm
        outdir = tempfile.mkdtemp(prefix="cop_spm_")
        cm.subset(
            dataset_id=product,
            variable=["total_suspended_matter"],
            minimum_longitude=lng - 0.01,
            maximum_longitude=lng + 0.01,
            minimum_latitude=lat - 0.01,
            maximum_latitude=lat + 0.01,
            start_datetime=when.strftime("%Y-%m-%dT%H:%M:%S"),
            end_datetime=(when + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
            output_directory=outdir,
            force_download=True,
        )
        # Lit le NetCDF téléchargé et extrait la valeur au point le plus proche.
        import glob
        files = glob.glob(os.path.join(outdir, "*.nc"))
        if not files:
            return None
        ds = xr.open_dataset(files[0])
        spm = float(ds["total_suspended_matter"].sel(
            latitude=lat, longitude=lng,
            method="nearest").mean().compute())
        ds.close()
        # g/m³ -> g/L (1 g/m³ = 0.001 g/L). L'algorithme travaille en g/L.
        return round(spm * 0.001, 4)
    except Exception as e:  # noqa: BLE001
        print(f"[copernicus] échec subset SPM : {e}")
        return None
