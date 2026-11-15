import { useMemo } from 'react'
import DeckGL from '@deck.gl/react'
import { LineLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers'
import type { PickingInfo } from 'deck.gl'
import MapGL from 'react-map-gl/maplibre'
import 'maplibre-gl/dist/maplibre-gl.css'
import { capLabel, excludedReason, fmtMin } from './format'
import type { Hospital, Recommendation } from './types'

// Same basemap and Bengaluru view as GoldenHour (App.tsx).
const MAP_STYLE = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json'
const INITIAL_VIEW = { longitude: 77.5946, latitude: 12.9716, zoom: 10.6, pitch: 0, bearing: 0 }

export type LatLon = { lat: number; lon: number }
export type PlaceMode = 'scene' | 'base'

type Role = 'top' | 'ranked' | 'excluded' | 'idle'

interface HospitalPoint {
  h: Hospital
  role: Role
  rank: number | null // 1-based within ranked
  detail: string
}

type RGBA = [number, number, number, number]
const FILL: Record<Role, RGBA> = {
  top: [5, 150, 105, 255], // emerald: recommended
  ranked: [37, 99, 235, 255], // blue: capable, ranked
  excluded: [148, 163, 184, 200], // grey: excluded
  idle: [100, 116, 139, 220],
}
const RADIUS: Record<Role, number> = { top: 13, ranked: 10, excluded: 5, idle: 6 }
const SCENE: RGBA = [220, 38, 38, 255]
const BASE: RGBA = [15, 23, 42, 255]

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]!)

export function AdvisorMap({
  hospitals,
  rec,
  scene,
  base,
  mode,
  onModeChange,
  onPlace,
}: {
  hospitals: Hospital[]
  rec: Recommendation | null
  scene: LatLon
  base: LatLon
  mode: PlaceMode
  onModeChange: (m: PlaceMode) => void
  onPlace: (mode: PlaceMode, p: LatLon) => void
}) {
  const points = useMemo<HospitalPoint[]>(() => {
    const ranked = new Map(rec?.ranked.map((r, i) => [r.hospital_id, { r, i }]) ?? [])
    const excluded = new Map(rec?.excluded.map((x) => [x.hospital_id, x]) ?? [])
    const pts = hospitals.map<HospitalPoint>((h) => {
      const rk = ranked.get(h.hospital_id)
      if (rk) {
        return {
          h,
          role: rk.i === 0 ? 'top' : 'ranked',
          rank: rk.i + 1,
          detail: `#${rk.i + 1}, score ${fmtMin(rk.r.score, 1)}`,
        }
      }
      const ex = excluded.get(h.hospital_id)
      if (ex) return { h, role: 'excluded', rank: null, detail: `excluded: ${excludedReason(ex.reason)}` }
      return { h, role: 'idle', rank: null, detail: rec ? 'not considered' : 'no recommendation yet' }
    })
    // draw order: excluded under ranked under top
    const z: Record<Role, number> = { idle: 0, excluded: 1, ranked: 2, top: 3 }
    return pts.sort((a, b) => z[a.role] - z[b.role])
  }, [hospitals, rec])

  const top = rec?.ranked[0] ? hospitals.find((h) => h.hospital_id === rec.ranked[0].hospital_id) : undefined

  const layers = [
    new LineLayer<{ from: LatLon; to: LatLon }>({
      id: 'base-to-scene',
      data: [{ from: base, to: scene }],
      getSourcePosition: (d) => [d.from.lon, d.from.lat],
      getTargetPosition: (d) => [d.to.lon, d.to.lat],
      getColor: [15, 23, 42, 110],
      getWidth: 2,
      widthUnits: 'pixels',
    }),
    top &&
      new LineLayer<{ from: LatLon; to: LatLon }>({
        id: 'scene-to-top',
        data: [{ from: scene, to: { lat: top.lat, lon: top.lon } }],
        getSourcePosition: (d) => [d.from.lon, d.from.lat],
        getTargetPosition: (d) => [d.to.lon, d.to.lat],
        getColor: [5, 150, 105, 230],
        getWidth: 4,
        widthUnits: 'pixels',
      }),
    new ScatterplotLayer<HospitalPoint>({
      id: 'hospitals',
      data: points,
      pickable: true,
      stroked: true,
      radiusUnits: 'pixels',
      lineWidthUnits: 'pixels',
      getPosition: (d) => [d.h.lon, d.h.lat],
      getRadius: (d) => RADIUS[d.role],
      getFillColor: (d) => FILL[d.role],
      getLineColor: (d) => (d.role === 'excluded' || d.role === 'idle' ? [255, 255, 255, 200] : [255, 255, 255, 255]),
      getLineWidth: (d) => (d.role === 'top' ? 3 : d.role === 'ranked' ? 2 : 1),
      updateTriggers: { getRadius: [rec], getFillColor: [rec], getLineWidth: [rec] },
    }),
    new TextLayer<HospitalPoint>({
      id: 'ranks',
      data: points.filter((p) => p.rank != null),
      getPosition: (d) => [d.h.lon, d.h.lat],
      getText: (d) => String(d.rank),
      getSize: (d) => (d.role === 'top' ? 14 : 11),
      getColor: [255, 255, 255, 255],
      fontWeight: 800,
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      getTextAnchor: 'middle',
      getAlignmentBaseline: 'center',
    }),
    new ScatterplotLayer<{ kind: PlaceMode; p: LatLon }>({
      id: 'pins',
      data: [
        { kind: 'base', p: base },
        { kind: 'scene', p: scene },
      ],
      radiusUnits: 'pixels',
      lineWidthUnits: 'pixels',
      stroked: true,
      getPosition: (d) => [d.p.lon, d.p.lat],
      getRadius: 11,
      getFillColor: (d) => (d.kind === 'scene' ? SCENE : BASE),
      getLineColor: [255, 255, 255, 255],
      getLineWidth: 3,
    }),
    new TextLayer<{ kind: PlaceMode; p: LatLon }>({
      id: 'pin-labels',
      data: [
        { kind: 'base', p: base },
        { kind: 'scene', p: scene },
      ],
      getPosition: (d) => [d.p.lon, d.p.lat],
      getText: (d) => (d.kind === 'scene' ? 'S' : 'B'),
      getSize: 12,
      getColor: [255, 255, 255, 255],
      fontWeight: 800,
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      getTextAnchor: 'middle',
      getAlignmentBaseline: 'center',
    }),
  ]

  const tooltip = ({ object, layer }: PickingInfo) => {
    if (!object || layer?.id !== 'hospitals') return null
    const d = object as HospitalPoint
    const caps = Object.entries(d.h.capabilities)
      .filter(([, c]) => c.available)
      .map(([k]) => capLabel(k))
    return {
      html: `<div class="adv-tip"><strong>${esc(d.h.name)}</strong><div>${esc(d.detail)}</div>
        <div class="adv-tip__caps">${caps.length ? esc(caps.join(', ')) : 'no capabilities recorded'}</div></div>`,
      style: { background: 'transparent', padding: '0' },
    }
  }

  return (
    <div className="adv-map">
      <DeckGL
        initialViewState={INITIAL_VIEW}
        controller={true}
        layers={layers}
        getCursor={({ isDragging, isHovering }) => (isDragging ? 'grabbing' : isHovering ? 'pointer' : 'crosshair')}
        onClick={(info) => {
          if (info.object || !info.coordinate) return
          const [lon, lat] = info.coordinate
          onPlace(mode, { lat, lon })
        }}
        getTooltip={tooltip}
      >
        <MapGL mapStyle={MAP_STYLE} />
      </DeckGL>

      <div className="adv-map__mode" role="radiogroup" aria-label="What a map click places">
        <span>Click map to place</span>
        <button
          role="radio"
          aria-checked={mode === 'scene'}
          className={mode === 'scene' ? 'is-on' : ''}
          onClick={() => onModeChange('scene')}
        >
          <i className="adv-dot adv-dot--scene" aria-hidden="true" />
          Scene
        </button>
        <button
          role="radio"
          aria-checked={mode === 'base'}
          className={mode === 'base' ? 'is-on' : ''}
          onClick={() => onModeChange('base')}
        >
          <i className="adv-dot adv-dot--base" aria-hidden="true" />
          Ambulance base
        </button>
      </div>

      <ul className="adv-map__legend" aria-label="Map legend">
        <li>
          <i className="adv-dot adv-dot--top" aria-hidden="true" />
          Recommended (#1)
        </li>
        <li>
          <i className="adv-dot adv-dot--ranked" aria-hidden="true" />
          Capable, ranked
        </li>
        <li>
          <i className="adv-dot adv-dot--excluded" aria-hidden="true" />
          Excluded
        </li>
        <li>
          <i className="adv-dot adv-dot--scene" aria-hidden="true" />
          Scene (S)
        </li>
        <li>
          <i className="adv-dot adv-dot--base" aria-hidden="true" />
          Base (B)
        </li>
        <li className="adv-map__legend-note">Straight line to #1; road route not drawn</li>
      </ul>
    </div>
  )
}
