# Architecture — Application Chasse sous-marine / Pêche : Visibilité & Marée dynamique

> Version 0.1 — MVP validé (sources de données réelles testées le 13/08/2026)

---

## 1. Vue d'ensemble

Système en 2 couches, conçu pour être **déployable à coût minimal** :

```
┌─────────────────────────────────────────────────────────────────┐
│  FRONTEND (React + Vite + MapLibre GL)                         │
│  Carte fluide / curseur temporel / panneau Spot Info           │
│  PWA (installable, hors-ligne possible)                        │
└───────────────────────────────┬─────────────────────────────────┘
                                │ REST (JSON)  /v1/spot /v1/timeline
┌───────────────────────────────▼─────────────────────────────────┐
│  BACKEND (Python 3.11 + FastAPI)                                │
│  - API métier : spot info, timeline 24/48h                      │
│  - Agrégateur de données (httpx async)                          │
│  - Cœur algorithmique : estimate_visibility() (pur, testable)   │
│  - Modèle de marée harmonique (M2/S2) pour la France            │
└───────────────────────────────┬─────────────────────────────────┘
                 ┌──────────────┼──────────────┐
                 ▼              ▼              ▼
      ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
      │ Open-Meteo   │  │  Marées      │  │ EMODnet      │
      │ Marine (libre│  │  SHOM / AVISO│  │ Bathymétrie  │
      │  houle, vent,│  │  (clé gratuite│  │ WMS (libre)  │
      │  courant)    │  │  ou modèle)  │  │ isobathes    │
      └──────────────┘  └──────────────┘  └──────────────┘
```

**Choix clés (justifiés)**
- **Frontend** : React 18 + Vite + `react-map-gl`/MapLibre GL — léger, WebGL, tuiles
  gratuites OSM en clair, et **exportable en PWA** (manifest + service worker).
- **Backend** : Python 3.11 + **FastAPI** — idéal pour le calcul géospatial/océano,
  type-hints avec Pydantic pour valider les réponses API, `async` pour appeler
  les 3 sources en parallèle.
- **Persistance**: PostgreSQL + PostGIS en production (rasters/vecteurs bathymétrie
  + points favoris). **Inutile pour le MVP** — tout est calculé à la volée.
- **Cache/Jobs**: Redis + Celery (ou APScheduler) pour rafraîchir les prévisions
  toutes les heures et éviter de re-taper les APIs à chaque requête.

---

## 2. Faisabilité des données (TESTÉ, pas supposé)

| Donnée            | Source                    | Clé ? | Endpoint testé (13/08) | Verdict |
|-------------------|---------------------------|-------|------------------------|---------|
| Houle, clapot, courant, vent | **Open-Meteo Marine** | ❌ Aucune | `https://marine-api.open-meteo.com/v1/marine` | ✅ Fonctionne |
| Marées (hauteur, pleine/basse) | SHOM (France, officiel) | 🔑 clé gratuite | — (inscription My Copernicus / data.shom.fr) | ⚠️ clé requise |
| Marées fallback    | Modèle harmonique **M2/S2 interne** | — | intégré au code | ✅ Sans réseau |
| Bathymétrie        | **EMODnet Bathymetry WMS** | ❌ Aucune | `https://ows.emodnet-bathymetry.eu/wms` | ✅ 200 OK |
| Sédiments / Chloro-A | Copernicus Marine (Sentinel-3 OLCI) | 🔑 clé NOAA/CDS | (registration my.copernicus.eu) | ⚠️ clé requise |

**Conclusion MVP** : Avec seulement Open-Meteo (gratuit) + un modèle de marée
harmonique interne, on obtient déjà un **indicateur de visibilité fonctionnel et
explicable** sans payer un centime. Les APIs SHOM/Copernicus (clés gratuites)
viennent en v2 pour la précision officielle.

---

## 3. Sources de données — guide d'intégration

### 3.1 Bathymétrie (isobathes / fond)
- **EMODnet Bathymetry** (Europe, gratuit, citation requise) :
  - WMS : `https://ows.emodnet-bathymetry.eu/wms?service=WMS&request=GetMap&layers=emodnet:mean_atlas_land&...&bbox={bbox}`
  - Tiles `{bbox-epsg-3857}` directes dans MapLibre (procédé utilisé en front).
  - Tuiles vectorielles `getmotion` / `bathy` dispo aussi.
- **GEBCO** (monde entier) : grilles + tuiles `gebco://` via outils dédiés.
- **SHOM Data.shom.fr** : WFS/WMS officiel français si besoin de précision côtière.

### 3.2 Marées
- **SHOM REFMAR / API port** (France, gratuit sur inscription) : hauteur + prédictions.
- **AVISO/CNES** & **Stormglass** (clé, monde) : complémentaire.
- **Fallback intégré** : modèle semi-diurne M2+S2 (période 12.42h), phase espacée
  selon longitude. Suffisant pour un MVP ; à remplacer par SHOM en sérieux.

### 3.3 Météo/Océano (houle, clapot, courant, vent)
- **Open-Meteo Marine API** — GRATUIT, pas de clé, 3 jours de prévision, horaire :
  ```
  GET /v1/marine?latitude=&longitude=&hourly=swell_wave_height,swell_wave_period,
       wind_wave_height,wind_wave_period,ocean_current_velocity,wind_speed_10m
  ```
  → c'est LA source principale du MVP.

### 3.4 Turbidité / SPM (v2, qualité pro)
- **Copernicus Marine** (Motu/NetCDF) : Sentinel-3 OLCI chl-a, SPM.
- **NASA Ocean Color** : réflectance → SPM (rétrocalcul).
- S'ajoute au modèle comme un paramètre `turbidity` multiplié sur le score.

---

## 4. Algorithme de visibilité (cœur)

La visibilité n'existe **pas** en API : c'est un **modèle composite multiplicatif**,
inspiré de la physique de resuspension des sédiments (Beer-Lambert, Soulsby 1997).

```
visi = VISI_MAX (12m)  ×  f(houle) × f(vent) × f(marée) × f(courant) × f(sédiment) × exp(−h_eau×0.02)
```

- **f(vent)** : clapot local → reflets, agitation surface.
- **f(houle)** : le ratio `H/T` (raideur) déclenche la remise en suspension au fond
  (vitesse orbitale Uw > seuil critique).
- **f(marée)** : max de pénalité à la **mi-marée** (courant max), min à l'**étale**.
- **f(courant)** : courant général continu → brassage persistant.
- **f(sédiment)** : coefficient de "mobilité du fond" (roche=0.5, sable=1.0, vase=1.5).

**Qualificatifs** : ≥6m=excellente (vert), 3-6m=bonne, 1.5-3m=moyenne (orange), <1.5m=mauvaise (rouge).

Chaque facteur peut expliquer son impact → l'UI affiche "pourquoi ce score".

---

## 5. Structure des dossiers

```
spearfishing-visi-tide/
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI: endpoints /health /v1/spot /v1/timeline
│   │   ├── tide_plus.py     # agrégation APIs + modèle marée + build_visibility()
│   │   ├── visibility.py    # CŒUR: estimate_visibility() + composants physiques
│   │   └── __init__.py
│   ├── tests/
│   ├── requirements.txt
│   └── .venv/
├── frontend/
│   ├── src/
│   │   ├── App.jsx          # carte MapLibre + HUD + slider
│   │   ├── main.jsx
│   │   ├── styles.css
│   │   └── lib/geo.js       # helpers couleurs/format
│   ├── index.html
│   ├── package.json
│   └── vite.config.js       # proxy /v1 → backend:8000
└── docs/                    # cette documentation
```

---

## 6. Endpoints API

```
GET /health                          → {status:"ok"}
GET /v1/spot?lat=&lng=&at=ISO        → fiche spot (visi, marée, hauteur eau, prochaine PM/BM)
GET /v1/timeline?lat=&lng=&hours=&step=  → série visi + niveau eau pour le slider
```

`at` = optionnel (défaut maintenant). `hours` max 72. `step` en minutes (défaut 60).

---

## 7. Déploiement & prochaines étapes

- **Prod** : FastAPI derrière nginx/Caddy + une stack Postgres/PostGIS si besoin
  de favoris/sync. Front distribuable en statique (build Vite) + PWA.
- **Cache** : une tâche Celery/APScheduler re-fetch toutes les heures Open-Meteo,
  stocke les points en Redis → API ultra-rapide.
- **V2** : brancher SHOM (marées précises) et Copernicus (turbidité SPM) pour
  passer de "bonne estimation" à "estimation pro".
- **Modèle** : calibrer les poids avec des retours terrain de chasseurs (crowdsource).

---

## 8. Lancement rapide

```bash
# Backend
cd backend && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend
cd frontend && npm install && npm run dev
# -> http://localhost:5173
```
