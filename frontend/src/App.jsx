import React, { Component, useEffect, useRef, useState } from 'react'
import Map, { Marker } from 'react-map-gl'
import maplibregl from 'maplibre-gl'
import { fmtTime } from './lib/geo.js'

// Garde-fou : si la carte (MapLibre/WebGL) crash sur un appareil, on évite
// l'écran blanc : on affiche un fond neutre + message, le reste de l'UI reste.
class MapBoundary extends Component {
  constructor(props) { super(props); this.state = { err: null } }
  static getDerivedStateFromError(e) { return { err: e } }
  componentDidCatch(e) { console.error('MapBoundary:', e) }
  render() {
    if (this.state.err) {
      return (
        <div className="map-fallback">
          <p>⚠️ La carte n'a pas pu s'afficher sur cet appareil (WebGL indisponible).</p>
          <p className="muted">Les données de marée et de visibilité restent disponibles ci-dessous.</p>
        </div>
      )
    }
    return this.props.children
  }
}

// ---------------------------------------------------------------------------
// Carte marine SHOM (unique fond de carte).
// Assemblage façon data.shom.fr :
//   1) Fond de carte mondial FDC_GEBCO (couverture de base)
//   2) RASTER_MARINE : les vraies cartes marines officielles (sondes,
//      isobathes, reliefs, noms) là où elles existent
//   3) Toponymie marine (noms des lieux) + balisage (balises, bouées, feux)
// Le tout servit via notre proxy même-origine /v1/shom/... qui envoie le
// Referer exigé par services.data.shom.fr.
// ---------------------------------------------------------------------------
const SHOM_STYLE = {
  version: 8,
  glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
  sources: {
    shom_fond: {
      type: 'raster',
      tiles: [window.location.origin + '/v1/shom/fond/{z}/{x}/{y}'],
      tileSize: 256,
      minzoom: 0,
      maxzoom: 18,
      attribution: 'Fond © SHOM',
    },
    shom_marines: {
      type: 'raster',
      tiles: [window.location.origin + '/v1/shom/rastermarine/{z}/{x}/{y}'],
      tileSize: 256,
      minzoom: 0,
      maxzoom: 18,
    },
    shom_toponymie: {
      type: 'raster',
      tiles: [window.location.origin + '/v1/shom/toponymie/{z}/{x}/{y}'],
      tileSize: 256,
      minzoom: 0,
      maxzoom: 18,
    },
    shom_balisage: {
      type: 'raster',
      tiles: [window.location.origin + '/v1/shom/balisage/{z}/{x}/{y}'],
      tileSize: 256,
      minzoom: 0,
      maxzoom: 18,
    },
  },
  layers: [
    { id: 'shom_fond', type: 'raster', source: 'shom_fond' },
    { id: 'shom_marines', type: 'raster', source: 'shom_marines' },
    { id: 'shom_toponymie', type: 'raster', source: 'shom_toponymie' },
    { id: 'shom_balisage', type: 'raster', source: 'shom_balisage' },
  ],
}

function visiColor(score) {
  if (score >= 6) return '#22c55e'
  if (score >= 3) return '#84cc16'
  if (score >= 1.5) return '#f59e0b'
  return '#ef4444'
}

export default function App() {
  const [viewport, setViewport] = useState({
    longitude: -2.19, latitude: 48.577, zoom: 12,
  })
  const [selected, setSelected] = useState(null)   // {lat, lng} du spot cliqué
  const [spotData, setSpotData] = useState(null)   // fiche du spot (fond fixe)
  const [loading, setLoading] = useState(false)
  const [timeline, setTimeline] = useState([])     // points 24h du jour choisi
  const [extremes, setExtremes] = useState([])     // PM/BM du jour choisi
  const [sliderIdx, setSliderIdx] = useState(null)
  const [dayOffset, setDayOffset] = useState(0)    // 0=aujourd'hui, -1=hier, +1=demain
  const [error, setError] = useState(null)
  const mapRef = useRef(null)

  // Charge la timeline 24h pour le SPOT cliqué et le JOUR choisi (dayOffset).
  // La timeline ne s'affiche que quand un spot est sélectionné.
  const timelineTarget = selected || null

  useEffect(() => {
    if (!timelineTarget) {
      setTimeline([])
      setSliderIdx(null)
      return
    }
    let mounted = true
    async function load() {
      // Heure de début = début (00:00 UTC) du jour cible pour naviguer
      // tranquillement : aujourd'hui commence maintenant, autres jours à 00:00.
      const base = new Date()
      base.setUTCHours(0, 0, 0, 0)
      const start = new Date(base)
      start.setUTCDate(base.getUTCDate() + dayOffset)
      const startIso = start.toISOString()
      try {
        const res = await fetch(
          `/v1/timeline?lat=${timelineTarget.lat}&lng=${timelineTarget.lng}`
          + `&start=${startIso}&hours=24&step=60`)
        if (!res.ok) throw new Error()
        const data = await res.json()
        if (!mounted) return
        setTimeline(data.points || [])
        setExtremes(data.extremes || [])
        // Sélectionne l'heure courante si on est sur aujourd'hui, sinon début de jour
        const now = Date.now()
        const curIdx = (data.points || []).findIndex((p) => {
          const t = new Date(p.at).getTime()
          return now >= t && now < t + 3600000
        })
        setSliderIdx(dayOffset === 0 && curIdx >= 0 ? curIdx : data.points.length ? 0 : null)
      } catch (e) {
        if (mounted) setError('Impossible de charger la timeline. Backend lancé ?')
      }
    }
    load()
    return () => { mounted = false }
  }, [timelineTarget, dayOffset])

  const currentPoint = timeline[sliderIdx] || null
  const waterOffset = currentPoint?.water_level_offset_m ?? 0

  // Vue du popup = combinaison du spot cliqué (fond fixe) et de l'heure
  // courante de la timeline (marée, visi, coef qui varient avec le temps).
  // Le fond (depth_chart_m) est une donnée de la carte, indépendante de l'heure.
  const popupView = spotData && currentPoint ? {
    ...spotData,
    water_level_offset_m: currentPoint.water_level_offset_m,
    visi_m: currentPoint.visi_m,
    visi_qualitative: currentPoint.visi_qualitative,
    color_hex: currentPoint.color_hex,
    tidal_coefficient: currentPoint.tidal_coefficient,
    at: currentPoint.at,
  } : spotData

  async function onMapClick(evt) {
    const { lng, lat } = evt.lngLat
    setSelected({ lat, lng })
    setLoading(true)
    setSpotData(null)
    try {
      const res = await fetch(`/v1/spot?lat=${lat}&lng=${lng}`)
      if (!res.ok) throw new Error()
      const data = await res.json()
      setSpotData(data)
    } catch (e) {
      setError("Erreur de chargement du spot (backend lancé ?)")
    } finally {
      setLoading(false)
    }
  }

  const displayLabel = currentPoint ? fmtTime(currentPoint.at) : '—'
  const displayVisi = currentPoint ? currentPoint.visi_m : null

  // Écran blanc intermittent : les tuiles raster se chargent mais MapLibre ne
  // les peint pas toujours. On recharge toutes les sources SHOM et on force
  // plusieurs repaints (immédiat + différés) pour fiabiliser l'affichage.
  const onMapLoad = () => {
    try {
      const m = mapRef.current
      if (!m) return
      const reload = () => {
        try {
          for (const id of ['shom_fond', 'shom_marines', 'shom_toponymie', 'shom_balisage']) {
            m.getSource(id)?.reload?.()
          }
          m.triggerRepaint?.()
        } catch (e) { /* no-op */ }
      }
      reload()
      setTimeout(reload, 600)
      setTimeout(reload, 1800)
      setTimeout(reload, 4000)
    } catch (e) { console.error('onMapLoad:', e) }
  }

  // Libellé du jour pour l'en-tête de la fiche
  const dayLabel = (offset) => {
    const d = new Date()
    d.setUTCHours(0, 0, 0, 0)
    d.setUTCDate(d.getUTCDate() + offset)
    return d.toLocaleDateString('fr-FR', { weekday: 'short', day: 'numeric', month: 'short' })
  }

  return (
    <div className="app">
      <MapBoundary>
      <Map
        {...viewport}
        ref={mapRef}
        onLoad={onMapLoad}
        onMove={(e) => setViewport(e.viewState)}
        style={{ width: '100%', height: '100vh' }}
        mapStyle={SHOM_STYLE}
        mapLib={maplibregl}
        onClick={onMapClick}
      >
        {/* Marqueur au point du spot sélectionné (pas de popup flottant) */}
        {selected && popupView && (
          <Marker longitude={selected.lng} latitude={selected.lat}
                  color={visiColor(popupView.visi_m ?? 3)} />
        )}
      </Map>
      </MapBoundary>

      {selected && popupView && (
        <div className="hud top-right">
          <span className="dot" style={{ background: visiColor(displayVisi ?? 0) }} />
          <strong>{displayVisi != null ? displayVisi.toFixed(1) : '--'} m</strong>
          <span className="muted">à {displayLabel}</span>
          <span className="tide">🌊 {waterOffset.toFixed(1)} m</span>
        </div>
      )}

      {/* Fiche spot en bas : infos + sélecteur de jour + slider 24h */}
      {selected && popupView && (
        <div className="spot-sheet">
          {/* En-tête : coordonnées + navigation de jour */}
          <div className="sheet-head">
            <div className="sheet-coords">
              <span className="sheet-title">Spot</span>
              <span className="muted">
                {selected.lat.toFixed(4)}, {selected.lng.toFixed(4)}
              </span>
            </div>
            <div className="day-nav">
              <button className="day-btn" onClick={() => setDayOffset((d) => d - 1)}
                      aria-label="Jour précédent">◀</button>
              <span className="day-label">{dayLabel(dayOffset)}</span>
              <button className="day-btn" onClick={() => setDayOffset((d) => d + 1)}
                      aria-label="Jour suivant">▶</button>
            </div>
            <button className="sheet-close" onClick={() => { setSelected(null); setSpotData(null); setExtremes([]) }}
                    aria-label="Fermer">✕</button>
          </div>

          {/* Ligne de données principales */}
          <div className="sheet-stats">
            <div className="stat">
              <span className="stat-value" style={{ color: popupView.color_hex }}>
                {popupView.visi_m.toFixed(1)} m
              </span>
              <span className="stat-label">{popupView.visi_qualitative}</span>
            </div>
            <div className="stat">
              <span className="stat-value">{Math.abs(popupView.depth_chart_m).toFixed(1)} m</span>
              <span className="stat-label">Fond</span>
            </div>
            <div className="stat">
              <span className="stat-value">{popupView.water_level_offset_m.toFixed(1)} m</span>
              <span className="stat-label">Eau</span>
            </div>
            <div className="stat">
              <span className="stat-value" style={{ color: '#7dd3fc' }}>
                {Math.max(0, popupView.water_level_offset_m - popupView.depth_chart_m).toFixed(1)} m
              </span>
              <span className="stat-label">Prof. réelle</span>
            </div>
            <div className="stat">
              <span className="stat-value">{popupView.tidal_coefficient?.toFixed(0)}</span>
              <span className="stat-label">Coef</span>
            </div>
          </div>

          {/* Pleines / basses mers du jour choisi */}
          {extremes.length > 0 && (
            <div className="sheet-extremes">
              {extremes.map((e) => (
                <span key={e.at} className={`extreme extreme-${e.type === 'PM' ? 'pm' : 'bm'}`}>
                  {e.type} {fmtTime(e.at)} · {e.height_m}m
                </span>
              ))}
            </div>
          )}

          {/* Slider temporel 24h du jour choisi */}
          {timeline.length > 1 && (
            <div className="sheet-slider">
              <span className="muted">{popupView.at ? fmtTime(popupView.at) : '—'}</span>
              <input type="range" min={0} max={timeline.length - 1}
                     value={sliderIdx ?? 0} onChange={(e) => setSliderIdx(Number(e.target.value))}
                     className="timeline" />
              <span className="muted">{waterOffset.toFixed(1)}m</span>
            </div>
          )}
          <div className="sheet-ref">🌊 Marée estimée par modèle · réf. Saint-Malo (SHOM)</div>
        </div>
      )}

      {error && <div className="toast error">{error}</div>}
      {loading && <div className="toast">Chargement du spot…</div>}
    </div>
  )
}
