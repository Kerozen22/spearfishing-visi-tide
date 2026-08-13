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

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .tide_plus import build_visibility, _infer_tidal_coefficient, _synthetic_tide_offset
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

    coef, marnage = _infer_tidal_coefficient(lat, lng, when)
    offset_h, _ = _synthetic_tide_offset(lat, lng, when)
    next_high, next_low = _next_extremes(lat, lng, when)
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
    hours: int = Query(48, ge=1, le=72, description="Nombre d'heures de prévision"),
    step: int = Query(60, ge=15, le=360, description="Pas en minutes"),
):
    """Série temporelle pour le slider : visi + profondeur + marée par pas."""
    when = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    points = []
    for i in range(0, hours * 60, step):
        t = when + timedelta(minutes=i)
        r = await build_visibility(lat, lng, t)
        coef, _ = _infer_tidal_coefficient(lat, lng, t)
        offset_h, _ = _synthetic_tide_offset(lat, lng, t)
        points.append({
            "at": t.isoformat(),
            "visi_m": r.score_m,
            "qualitative": r.qualitative,
            "water_level_offset_m": offset_h,
            "tidal_coefficient": coef,
            "factors": r.factors,
        })
    return {"lat": lat, "lng": lng, "step_minutes": step, "points": points}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_at(at: Optional[str]) -> datetime:
    if not at:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise HTTPException(400, "Format 'at' invalide. utilisez ISO8601 (ex: 2026-08-13T08:00:00Z)")


def _next_extremes(lat: float, lng: float, when: datetime) -> tuple[datetime, datetime]:
    """Prochaine pleine mer et basse mer via le modèle harmonique simplifié.

    On cherche les zéros de la dérivée de la hauteur sur les prochaines 25h.
    """
    def h(t: datetime) -> float:
        val, _ = _synthetic_tide_offset(lat, lng, t)
        return val

    def derivative(t: datetime, dt_h: float = 0.1) -> float:
        s_plus = (t + timedelta(hours=dt_h))
        s_moins = (t - timedelta(hours=dt_h))
        return (h(s_plus) - h(s_moins)) / (2 * dt_h)

    high, low = None, None
    t = when
    step = timedelta(minutes=10)
    prev_d = derivative(t)
    prev_h = h(t)
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
