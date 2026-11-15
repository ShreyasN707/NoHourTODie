import { useEffect, useRef, useState } from 'react'
import DeckGL from '@deck.gl/react'
import { PathLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers'
import { FlyToInterpolator, WebMercatorViewport } from '@deck.gl/core'
import MapGL from 'react-map-gl/maplibre'
import 'maplibre-gl/dist/maplibre-gl.css'

const MAP_STYLE = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json'
type LonLat = [number, number]
export type Pin = { pos: LonLat; kind: 'patient' | 'hospital' | 'unit' | 'bike' | 'option'; label?: string }
export type Route = { path: LonLat[]; kind: 'to_patient' | 'to_hospital' | 'bike' }

const COLOR = {
  patient: [214, 51, 74], hospital: [30, 156, 98], unit: [31, 95, 209], bike: [226, 115, 26], option: [91, 102, 117],
  to_patient: [214, 51, 74], to_hospital: [30, 156, 98],
} as const

/** Point at fraction t (0..1) of the way along a path, by distance. */
export function along(path: LonLat[], t: number): LonLat {
  if (path.length < 2 || t <= 0) return path[0]
  const seg = path.slice(1).map((p, i) => Math.hypot(p[0] - path[i][0], p[1] - path[i][1]))
  let left = Math.min(1, t) * seg.reduce((a, b) => a + b, 0)
  for (let i = 0; i < seg.length; i++) {
    if (left <= seg[i]) {
      const f = seg[i] ? left / seg[i] : 0
      return [path[i][0] + f * (path[i + 1][0] - path[i][0]), path[i][1] + f * (path[i + 1][1] - path[i][1])]
    }
    left -= seg[i]
  }
  return path[path.length - 1]
}

/** Crew map: routes, pins and the moving vehicle. Re-fits whenever `fitKey` changes (a new step or job). */
export function CrewMap({ routes, pins, fitKey }: { routes: Route[]; pins: Pin[]; fitKey: string }) {
  const box = useRef<HTMLDivElement>(null)
  const [view, setView] = useState<any>({ longitude: 77.59, latitude: 12.97, zoom: 11, pitch: 0, bearing: 0 })

  useEffect(() => {
    const pts = [...routes.flatMap((r) => r.path), ...pins.map((p) => p.pos)]
    const el = box.current
    if (!pts.length || !el) return
    const lons = pts.map((p) => p[0]), lats = pts.map((p) => p[1])
    const { clientWidth: width, clientHeight: height } = el
    const pad = Math.min(60, width / 6, height / 6)
    const fit = new WebMercatorViewport({ width, height }).fitBounds(
      [[Math.min(...lons) - 0.002, Math.min(...lats) - 0.002], [Math.max(...lons) + 0.002, Math.max(...lats) + 0.002]],
      { padding: pad, maxZoom: 16 },
    )
    setView((v: any) => ({ ...v, longitude: fit.longitude, latitude: fit.latitude, zoom: fit.zoom,
      transitionDuration: 900, transitionInterpolator: new FlyToInterpolator() }))
  }, [fitKey]) // oxlint-disable-line react-hooks/exhaustive-deps

  const layers = [
    new PathLayer<Route>({
      id: 'routes', data: routes, getPath: (r) => r.path,
      getColor: (r) => [...(r.kind === 'bike' ? COLOR.bike : COLOR[r.kind])] as [number, number, number],
      getWidth: (r) => (r.kind === 'bike' ? 4 : 7), widthUnits: 'pixels', capRounded: true, jointRounded: true,
    }),
    new ScatterplotLayer<Pin>({
      id: 'pins', data: pins, getPosition: (p) => p.pos,
      getFillColor: (p) => [...COLOR[p.kind]] as [number, number, number], getLineColor: [255, 255, 255],
      getRadius: (p) => (p.kind === 'unit' ? 11 : p.kind === 'option' ? 9 : 10), radiusUnits: 'pixels',
      stroked: true, lineWidthMinPixels: 3, updateTriggers: { getPosition: pins.map((p) => p.pos.join()).join() },
    }),
    new TextLayer<Pin>({
      id: 'pin-labels', data: pins.filter((p) => p.label), getPosition: (p) => p.pos, getText: (p) => p.label!,
      getSize: 13, getColor: [255, 255, 255], fontWeight: 800, fontFamily: 'Public Sans, sans-serif',
    }),
  ]
  return (
    <div ref={box} className="crew-map">
      <DeckGL viewState={view} onViewStateChange={(e: any) => setView(e.viewState)} controller layers={layers}>
        <MapGL mapStyle={MAP_STYLE} />
      </DeckGL>
    </div>
  )
}
