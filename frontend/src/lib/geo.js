// Utilitaires pour l'interprétation des données.
// Couleurs et libellés partagés avec le backend.

export const VISI_LEVELS = [
  { max: 1.0, label: 'Nulle', color: '#ef4444' },
  { max: 1.5, label: 'Très mauvaise', color: '#f43f5e' },
  { max: 3.0, label: 'Mauvaise', color: '#f97316' },
  { max: 6.0, label: 'Moyenne', color: '#f59e0b' },
  { max: 9.0, label: 'Bonne', color: '#84cc16' },
  { max: Infinity, label: 'Excellente', color: '#22c55e' },
]

export function visiLevel(score) {
  return VISI_LEVELS.find((l) => score < l.max) || VISI_LEVELS[VISI_LEVELS.length - 1]
}

export function kmhToMs(kmh) {
  return kmh / 3.6
}

export function fmtTime(iso) {
  const d = new Date(iso)
  return d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })
}

export function fmtDate(iso) {
  const d = new Date(iso)
  return d.toLocaleDateString('fr-FR', { weekday: 'short', day: 'numeric', month: 'short' })
}

// Sélecteur de fond : style de base fusionné + couche bathymétrie en overlay.
export const BASEMAP_STYLES = {
  satellite: {
    id: 'satellite',
    label: 'Satellite',
    url: 'https://tiles.geo.fr/...' // placeholder
  },
}
