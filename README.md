# 🎯 Spearfishing / Pêche — Visibilité & Marée dynamique

Application web/PWA pour chasseurs sous-marins et pêcheurs :
- **Cartographie bathymétrique** ajustée en temps réel au niveau de la marée.
- **Indicateur de visibilité sous-marine** estimé par modèle composite
  (houle, vent, marée, courant, sédiment).
- Curseur temporel 24h/48h pour simuler évolution profondeur + visi.
- Clic carte → fiche "Spot Info" (profondeur, visi, étales, coefficient).

> **État** : MVP fonctionnel. Backend FastAPI + algorithme validés et testés.
> Front React + MapLibre compiles et se connecte. Voir `docs/ARCHITECTURE.md`.

---

## 🌊 Démo rapide (résultat réel calculé)

Pour le spot **Bretagne nord (48.99, -4.52)** le 13/08/2026, l'API renvoie :

```json
{
  "visi_m": 4.9,
  "visi_qualitative": "bonne",
  "water_level_offset_m": -0.79,
  "explanation": [
    "- fond/sédiment : réduit la visi d'environ 27%.",
    "- courant : réduit la visi d'environ 24%.",
    "- marée : réduit la visi d'environ 16%."
  ]
}
```

## 🚀 Lancement

### Backend (FastAPI, port 8000)
```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Frontend (Vite, port 5173)
```bash
cd frontend
npm install
npm run dev
# → http://localhost:5173
```

### Tests
```bash
cd backend && source .venv/bin/activate && python -m pytest tests/ -v
```

---

## 📚 Documentation
- **`docs/ARCHITECTURE.md`** — architecture complète, sources de données validées,
  guide d'intégration API, explication de l'algorithme.

## 🗺️ Sources de données (testées, gratuites)
| Besoin | Source | Clé |
|--------|--------|-----|
| Météo/océanographie (houle, courant, vent) | Open-Meteo Marine API | ❌ aucune |
| Marées (fallback fr) | Modèle harmonique M2/S2 intégré | — |
| Marées (officiel fr) | SHOM / data.shom.fr | 🔑 gratuite |
| Bathymétrie | EMODnet Bathymetry WMS | ❌ aucune |
| Turbidité SPM (v2) | Copernicus Marine (Sentinel-3) | 🔑 gratuite |
