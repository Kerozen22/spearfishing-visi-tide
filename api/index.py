# Point d'entrée Vercel pour le backend FastAPI.
# Vercel détecte la variable `app` (WSGI) ou tout ASGI monté.
# Pour du ASGI pur, on utilise l'adapter fourni par vercel.
# Ce module sert de pont : il monte l'app FastAPI du backend existant.
#
# NOTE IMPORTANTE : l'import du backend doit pointer vers /backend/app.
# Vercel (root + api/) résout l'import relatif au répertoire de travail.
import sys
import os

# Ajoute le dossier backend/ au path pour que `from backend.app…` fonctionne.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# Réexporte l'app FastAPI (les routes définies dans backend/app/main.py)
try:
    from app.main import app
except Exception as e:  # noqa: BLE001
    # Fallback dégradé : expose un handler WSGI minimal pour que Vercel
    # ne plante pas au build et affiche un message clair.
    from fastapi import FastAPI
    app = FastAPI()
    @app.get("/health")
    async def health_fallback():
        return {"status": "error", "detail": f"Import backend échoué : {e!r}"}
