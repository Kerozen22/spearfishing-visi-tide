"""
FastAPI — API principale de la carte pêche/chasse sous-marine.

Endpoints :
  GET /health                 — healthcheck
  GET /v1/spot?lat=&lng=&at=  — fiche spot (visi + marée + profondeur) à un instant
  GET /v1/timeline?lat=&lng=&hours= — série temporelle 24/48h pour le slider

Lancement :
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .tide_plus import build_visibility, _infer_tidal_coefficient, _synthetic_tide_offset
from .worldtides import compute_tide_real, height_at_time, next_extremes
from .tides_ref import resolve_reference_port
from .visibility import estimate_visibility, OceanParams, tidal_coefficient_from_range

app = FastAPI(
    title="Spearfishing Visi/Tide API",
    description="Estimation de visibilité sous-marine + marée dynamique.",
    version="0.1.0",
)

# CORS : autorise le frontend (dev Vite sur :5173)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # MVP; à restreindre en prod.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SpotInfo(BaseModel):
    lat: float
    lng: float
    at: str
    visi_m: float
    visi_qualitative: str
    color_hex: str
    factors: dict
    explanation: list[str]
    water_level_offset_m: float
    tidal_coefficient: float
    depth_chart_m: Optional[float] = None  # profondeur carte (EMODnet), négative en mer
    next_high: str   # prochaine pleine mer (approx.)
    next_low: str    # prochaine basse mer (approx.)


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Proxy de tuiles bathymétriques EMODnet.
# Le serveur EMODnet rejette les requêtes no-cors de MapLibre et son cache est
# fragile dans le navigateur. En passant par notre API (même origine), la carte
# charge des tuiles via notre domaine : aucun problème de CORS/cache. On ajoute
# un en-tête Cache-Control pour limiter les appels sortants.
# ---------------------------------------------------------------------------
_EMODNET_TILE = "https://tiles.emodnet-bathymetry.eu/latest/mean_multicolour/web_mercator/{z}/{x}/{y}.png"


@app.get("/v1/bathytile/{z}/{x}/{y}")
async def bathytile(z: int, x: int, y: int):
    url = _EMODNET_TILE.format(z=z, x=x, y=y)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Erreur proxy EMODnet : {e!r}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, "Tuile indisponible sur EMODnet")
    return Response(
        content=r.content,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},  # 24h
    )


# ---------------------------------------------------------------------------
# Proxy de tuiles "carte marine" OpenSeaMap (symboles nautiques : balises,
# bouées, mouillages, feux...). Transparentes, à superposer sur le fond
# bathymétrique. Même origine pour éviter CORS/cache navigateur.
# ---------------------------------------------------------------------------
_SEAMARK_TILE = "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png"


@app.get("/v1/seamark/{z}/{x}/{y}")
async def seamark(z: int, x: int, y: int):
    url = _SEAMARK_TILE.format(z=z, x=x, y=y)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Erreur proxy OpenSeaMap : {e!r}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, "Tuile indisponible sur OpenSeaMap")
    return Response(
        content=r.content,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},  # 24h
    )


# ---------------------------------------------------------------------------
# Proxy de tuiles SHOM (services.data.shom.fr). Ce service vérifie le header
# HTTP "Referer" : sans lui, il répond 401 "Wrong referer". Notre backend envoie
# donc systématiquement un Referer data.shom.fr lors de la récupération, puis
# sert la tuile en même-origine (aucun CORS/cache navigateur côté app).
# Couches réellement utiles (officielles) :
#   BALISAGE_PYR_PNG_3857_WMTS  -> balises, bouées, feux (superposable)
#   EPAVES_PYR                  -> épaves
# ---------------------------------------------------------------------------
_SHOM_TILE = (
    "https://services.data.shom.fr/clevisu/wmts"
    "?layer={layer}{suffix}"
    "&style=normal&tilematrixset=3857"
    "&Service=WMTS&Request=GetTile&Version=1.0.0&Format=image%2Fpng"
    "&TileMatrix={z}&TileCol={x}&TileRow={y}"
)
_SHOM_REFERER = "https://data.shom.fr/"
# Couches SHOM accessibles via ce proxy (identifiant WMTS -> nom de couche).
# Attention : certaines couches déclarées (ex. épaves) répondent 401 "No rights".
# On ne garde que les couches librement consultables. L'identifiant exact est
# utilisé tel quel (certains comportent un tiret, ex. FDC_GEBCO_PYR-PNG_3857_WMTS).
_SHOM_LAYERS = {
    "rastermarine": "RASTER_MARINE_3857_WMTS",
    "balisage": "BALISAGE_PYR_PNG_3857_WMTS",
    "toponymie": "TOPONYMIE_PYR_PNG_3857_WMTS",
    "fond": "FDC_GEBCO_PYR-PNG_3857_WMTS",
    "batim": "BATHYELLI_ZH_PYR_PNG_3857_WMTS",
}


@app.get("/v1/shom/{layer}/{z}/{x}/{y}")
async def shom_tile(layer: str, z: int, x: int, y: int):
    l = _SHOM_LAYERS.get(layer)
    if l is None:
        raise HTTPException(404, f"Couche SHOM inconnue : {layer}")
    # Les identifiants SHOM peuvent contenir un tiret (ex: ..._PYR-PNG_3857_WMTS)
    # via le {suffix} ; les couches simples n'ont pas de suffixe.
    suffix = ""
    url = _SHOM_TILE.format(layer=l, suffix=suffix, z=z, x=x, y=y)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers={"Referer": _SHOM_REFERER})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Erreur proxy SHOM : {e!r}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, "Tuile indisponible sur le SHOM")
    return Response(
        content=r.content,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/v1/spot", response_model=SpotInfo)
async def spot(
    lat: float = Query(..., ge=-90, le=90, description="Latitude"),
    lng: float = Query(..., ge=-180, le=180, description="Longitude"),
    at: Optional[str] = Query(None, description="ISO datetime UTC. Défaut = maintenant"),
):
    """Fiche d'un spot à un instant donné : visi + marée + hauteur d'eau."""
    when = _parse_at(at)
    try:
        result = await build_visibility(lat, lng, when)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Source de données indisponible : {e}")

    tide = await compute_tide_real(lat, lng, when)
    coef = tide["coefficient"]
    offset_h = tide["water_level_offset_m"]
    if tide.get("next_high") and tide.get("next_low"):
        from datetime import datetime as _dt
        next_high = _dt.fromisoformat(tide["next_high"])
        next_low = _dt.fromisoformat(tide["next_low"])
    else:
        next_high, next_low = await _next_extremes(lat, lng, when)
    # Profondeur carte (négative en mer) : exposée via le facteur depth_chart_m.
    depth = result.factors.get("depth_chart_m")

    return SpotInfo(
        lat=lat, lng=lng,
        at=when.isoformat(),
        visi_m=result.score_m,
        visi_qualitative=result.qualitative,
        color_hex=result.color_hex,
        factors=result.factors,
        explanation=result.explanation,
        water_level_offset_m=result.water_level_offset_m,
        tidal_coefficient=coef,
        depth_chart_m=depth,
        next_high=next_high.isoformat(),
        next_low=next_low.isoformat(),
    )


@app.get("/v1/timeline")
async def timeline(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    start: Optional[str] = Query(None, description="ISO datetime UTC de début. Défaut = maintenant"),
    hours: int = Query(24, ge=1, le=72, description="Nombre d'heures de prévision"),
    step: int = Query(60, ge=15, le=360, description="Pas en minutes"),
):
    """Série temporelle pour le slider : visi + profondeur + marée par pas.

    `start` permet de naviguer sur un jour précis (passé ou futur) : on passe
    la date choisie par le sélecteur de jour de la fiche spot.
    """
    when = _parse_at(start).replace(minute=0, second=0, microsecond=0)
    points = []
    for i in range(0, hours * 60, step):
        t = when + timedelta(minutes=i)
        r = await build_visibility(lat, lng, t)
        points.append({
            "at": t.isoformat(),
            "visi_m": r.score_m,
            "visi_qualitative": r.qualitative,
            "color_hex": r.color_hex,
            "water_level_offset_m": r.water_level_offset_m,
            "tidal_coefficient": r.tidal_coefficient,
            "factors": r.factors,
        })
    return {"lat": lat, "lng": lng, "start": when.isoformat(),
            "step_minutes": step, "points": points}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_at(at: Optional[str]) -> datetime:
    if not at:
        return datetime.now(timezone.utc)
    try:
        s = at
        # Python 3.9 n'accepte pas le suffixe 'Z' (norme ISO); on le normalise.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise HTTPException(400, "Format 'at' invalide. utilisez ISO8601 (ex: 2026-08-13T08:00:00Z)")


async def _next_extremes(lat: float, lng: float, when: datetime) -> tuple[datetime, datetime]:
    """Prochaine pleine mer et basse mer.

    Utilise la hauteur du dispo réel (compute_tide_real, WorldTides si clé
    sinon modèle calibré) pour garder une cohérence totale avec la hauteur
    d'eau affichée dans l'app. On cherche les extrema sur les prochaines 25h.
    """
    def h(t: datetime) -> float:
        # compute_tide_real est async mais h() est synchrone ici ; on calcule
        # la hauteur via un appel séparé du modèle (cheap sans API) ou on
        # recourt à un fallback synchrone local.
        from .tides_ref import compute_tide
        tid = compute_tide(lat, lng, t)
        return tid["water_level_offset_m"]

    def derivative(t: datetime, dt_h: float = 0.1) -> float:
        s_plus = (t + timedelta(hours=dt_h))
        s_moins = (t - timedelta(hours=dt_h))
        return (h(s_plus) - h(s_moins)) / (2 * dt_h)

    high, low = None, None
    t = when
    step = timedelta(minutes=10)
    prev_d = derivative(t)
    for _ in range(int(25 * 6)):  # 25h / 10min
        t += step
        d = derivative(t)
        if prev_d >= 0 and d < 0:
            if high is None:
                high = t
        elif prev_d <= 0 and d > 0:
            if low is None:
                low = t
        prev_d = d
        if high and low:
            break
    high = high or (when + timedelta(hours=6))
    low = low or (when + timedelta(hours=6))
    return high, low


# ---------------------------------------------------------------------------
# Frontend buildé (SPA) : sert index.html de dist/ à la racine + assets.
# Le front est *versionné* dans /dist (committé) pour être garanti présent
# dans le runtime de la fonction serverless Vercel. En dev local (uvicorn
# seul), dist/ peut ne pas exister -> on ignore.
# ---------------------------------------------------------------------------
_public_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "dist")
# Résolution robuste du répertoire de travail selon l'hébergement (Vercel).
_candidates = [
    _public_dir,
    os.path.join(os.getcwd(), "dist"),
    os.path.join(os.getcwd(), "dist_web"),
]
_public_dir = next((p for p in _candidates if os.path.isdir(p)), _public_dir)
_index_html = os.path.join(_public_dir, "index.html")

if os.path.isdir(_public_dir) and os.path.isfile(_index_html):
    app.mount("/assets", StaticFiles(directory=os.path.join(_public_dir, "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(_index_html)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        # Fallback SPA : tout chemin non-API renvoie l'index (routing client).
        # NB: déclaré APRÈS les routes /v1, /health -> Vercel ne les capte pas.
        candidate = os.path.join(_public_dir, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        if full_path.startswith("v1/") or full_path == "health":
            raise HTTPException(404)
        return FileResponse(_index_html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
