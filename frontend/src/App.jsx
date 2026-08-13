import { useEffect, useRef, useState } from 'react'
import Map, { Marker, Popup } from 'react-map-gl'
import maplibregl from 'maplibre-gl'
import { fmtTime, fmtDate } from './lib/geo.js'

// Style de base : tuiles OSM libres (fond fiable universel).
const BASE_STYLE = {
  version: 8,
  sources: {
    osm: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      attribution: '© OpenStreetMap',
    },
  },
  layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
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

  return (
    <div className="app">
      <Map
        {...viewport}
        onMove={(e) => setViewport(e.viewState)}
        style={{ width: '100%', height: '100vh' }}
        mapStyle={BASE_STYLE}
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
