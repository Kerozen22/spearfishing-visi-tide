# Fonction Vercel exposant le backend FastAPI.
# Vercel (runtime Python) appelle `app`. On réexporte l'app du module backend.
import sys
import os

# Rend `backend` importable (dossier racine du dépôt).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.app.main import app  # noqa: E402  (FastAPI instance)
