import React, { Component, useEffect, useRef, useState } from 'react'
import Map, { Marker, Popup } from 'react-map-gl'
import maplibregl from 'maplibre-gl'
import { fmtTime, fmtDate } from './lib/geo.js'

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
// On assemble le fond de carte officiel du SHOM (FDC_GEBCO, trait de côte
// épuré) + la toponymie marine (noms des lieux) + le balisage (balises,
// bouées, feux). Le tout servit via notre proxy même-origine /v1/shom/...
// qui envoie le Referer exigé par services.data.shom.fr.
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
    longitude: -4.45, latitude: 48.4, zoom: 9,
  })
  const [selected, setSelected] = useState(null)
  const [spotData, setSpotData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [timeline, setTimeline] = useState([])
  const [sliderIdx, setSliderIdx] = useState(null)
  const [error, setError] = useState(null)
  const [baseline] = useState({ lat: 48.99, lng: -4.52 })
  const mapRef = useRef(null)

  // Charge la timeline 24h (marée + visi par heure) pour le slider
  useEffect(() => {
    let mounted = true
    async function load() {
      try {
        const res = await fetch(`/v1/timeline?lat=${baseline.lat}&lng=${baseline.lng}&hours=24&step=60`)
        const data = await res.json()
        if (!mounted) return
        setTimeline(data.points || [])
        setSliderIdx(0)
      } catch (e) {
        setError('Impossible de charger la timeline. Backend lancé ?')
      }
    }
    load()
    return () => { mounted = false }
  }, [baseline])

  const currentPoint = timeline[sliderIdx] || null
  const waterOffset = currentPoint?.water_level_offset_m ?? 0

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

  // Au chargement de la Map, force un repaint + recharge de la source SHOM
  // pour fiabiliser l'affichage des tuiles raster (écran blanc intermittent).
  const onMapLoad = () => {
    try {
      const m = mapRef.current
      if (!m) return
      m.triggerRepaint?.()
    } catch (e) { console.error('onMapLoad:', e) }
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
        <Marker longitude={-4.45} latitude={48.4} color={visiColor(displayVisi ?? 3)} />

        {selected && spotData && (
          <Popup longitude={selected.lng} latitude={selected.lat}
                 onClose={() => { setSelected(null); setSpotData(null) }}>
            <strong>{spotData.visi_m} m</strong>
            <div style={{ color: spotData.color_hex, fontWeight: 700 }}>
              {spotData.visi_qualitative}
            </div>
            <div className="popup-line">Fond : {spotData.depth_chart_m.toFixed(1)} m</div>
            <div className="popup-line">Marée (eau) : {spotData.water_level_offset_m.toFixed(2)} m</div>
            <div className="popup-line strong">
              Profondeur réelle : {(spotData.depth_chart_m + spotData.water_level_offset_m).toFixed(1)} m
            </div>
            <small>Coef {spotData.tidal_coefficient} · à {fmtTime(spotData.at)}</small>
          </Popup>
        )}
      </Map>
      </MapBoundary>

      <div className="hud top-right">
        <span className="dot" style={{ background: visiColor(displayVisi ?? 0) }} />
        <strong>{displayVisi != null ? displayVisi.toFixed(1) : '--'} m</strong>
        <span className="muted">à {displayLabel}</span>
        <span className="tide">🌊 {waterOffset.toFixed(1)} m</span>
      </div>

      <div className="hud bottom">
        <div className="timeline-row">
          <span className="muted">Marée : {fmtDate(currentPoint?.at || new Date().toISOString())}</span>
          <input type="range" min={0} max={Math.max(0, timeline.length - 1)}
                 value={sliderIdx ?? 0} onChange={(e) => setSliderIdx(Number(e.target.value))}
                 className="timeline" />
          <span className="muted">+24h · {waterOffset.toFixed(1)}m</span>
        </div>
      </div>

      {error && <div className="toast error">{error}</div>}
      {loading && <div className="toast">Chargement du spot…</div>}
    </div>
  )
}
