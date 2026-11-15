/** Fleet advice from the assistant (backend fleet_advice -> pipeline/advise.py): card in the chat, layers on the map. */
import { useState } from 'react'
import { H3HexagonLayer } from '@deck.gl/geo-layers'
import { ArcLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers'

type Site = { site_id: string; name: string; type: string; zone: string; lat: number; lon: number }
type BlockStats = { calls: number; late: number; within_pct: number }

export type Advice = {
  region: 'blr' | 'tmk'
  region_name: string
  target_min: number
  day: string
  day_note: string | null
  hour: number
  block: number
  block_label: string
  looked_back: {
    calls: number
    late: number
    late_pct: number
    by_cause: Record<string, number>
    hotspots: { zone: string; late: number; calls: number; chronic: boolean; cause: string; lat: number; lon: number }[]
    late_cells: [string, number][]
    in_block: BlockStats
    in_block_after: BlockStats
  }
  moves: {
    kind: string
    from: Site
    to: Site
    drive_min: number
    helped_cells: number
    helped_calls_per_day: number
    thinned_cells: number
    zones: string[]
    thinned_zones: string[]
    late_yesterday_cells: number
    black_spot_cells: number
    black_spots: number[]
  }[]
  moves_considered: number
  check: null | { weeks: number; start: string; end: string; before: BlockStats; after: BlockStats }
  causes: Record<string, string>
}

const CAUSE_COLOR: Record<string, string> = { unreachable: '#D6334A', uncovered: '#D98A00', busy: '#7C3AED' }
const day = (iso: string) => new Date(iso + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })

export function AdviceCard({ a, onShow }: { a: Advice; onShow: () => void }) {
  const [all, setAll] = useState(false)
  const lb = a.looked_back
  const total = Math.max(1, Object.values(lb.by_cause).reduce((x, y) => x + y, 0))
  const c = a.check
  return (
    <div className="gh-card gh-advice">
      <div className="gh-card-head">
        <span className="gh-chip">{a.region === 'blr' ? 'Bengaluru' : 'Tumakuru'}</span>
        <span className="gh-chip">Looked back: {day(a.day)}</span>
        <span className="gh-chip">Plan for {a.block_label}</span>
      </div>

      <div>
        <div className="gh-adv-title">
          {lb.late} of {lb.calls} calls late ({lb.late_pct}%)
        </div>
        <div className="gh-adv-bar" role="img" aria-label={Object.entries(lb.by_cause).map(([k, v]) => `${a.causes[k]} ${v}`).join(', ')}>
          {Object.entries(lb.by_cause).map(([k, v]) => <span key={k} style={{ width: `${(100 * v) / total}%`, background: CAUSE_COLOR[k] }} />)}
        </div>
        <div className="gh-adv-key">
          {Object.entries(lb.by_cause).map(([k, v]) => (
            <span key={k}><i style={{ background: CAUSE_COLOR[k] }} />{a.causes[k]} {v}</span>
          ))}
        </div>
        {lb.hotspots.length > 0 && (
          <div className="gh-adv-sub">
            Worst: {lb.hotspots.slice(0, 4).map((h) => `${h.zone} ${h.late}/${h.calls}${h.chronic ? ' (black spot)' : ''}`).join(' · ')}
          </div>
        )}
      </div>

      {a.moves.length > 0 ? (
        <ol className="gh-moves">
          {(all ? a.moves : a.moves.slice(0, 3)).map((m, i) => (
            <li key={i}>
              <b className="gh-move-n">{i + 1}</b>
              <div>
                <div className="gh-move-main">
                  {m.kind.startsWith('Advanced') ? 'Advanced' : 'Basic'} ambulance: <b>{m.from.zone || m.from.name}</b> → <b>{m.to.zone || m.to.name}</b>
                </div>
                <div className="gh-adv-sub" title={`${m.from.name} → ${m.to.name}`}>
                  {m.drive_min} min drive · to {m.to.name}
                </div>
                <div className="gh-adv-sub">
                  One more ambulance in reach of {m.helped_cells} hexagons{m.zones.length ? ` (${m.zones.slice(0, 2).join(', ')})` : ''}
                  {m.late_yesterday_cells ? `, ${m.late_yesterday_cells} late on ${day(a.day)}` : ''}; thins {m.thinned_cells}
                  {m.thinned_zones.length ? ` (${m.thinned_zones[0]})` : ''}.
                </div>
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <div className="gh-adv-sub">No move helps for this block: keep the current plan.</div>
      )}
      {a.moves.length > 3 && (
        <button className="gh-link" onClick={() => setAll((x) => !x)}>{all ? '▾ Fewer' : `▸ All ${a.moves.length} moves`}</button>
      )}

      {c && (
        <div className="gh-adv-check">
          <b>Checked on the {c.weeks} weeks before</b> ({day(c.start)} to {day(c.end)}): late calls in {a.block_label}{' '}
          {c.before.late.toLocaleString('en-IN')} → <b>{c.after.late.toLocaleString('en-IN')}</b> ({c.before.within_pct}% → {c.after.within_pct}% in
          time). {a.moves.length < a.moves_considered && `Kept ${a.moves.length} of ${a.moves_considered} moves; the rest did not help there.`}
        </div>
      )}
      <div className="gh-actions">
        <button className="gh-show" onClick={onShow}>Show on map</button>
      </div>
      <div className="gh-alt">Simulated: modelled calls, replayed on the GoldenHour plan. Moves are suggestions; the command centre decides.</div>
    </div>
  )
}

/** Map: that day's late calls per hexagon (red), and the suggested moves (arcs, numbered at the destination). */
export function adviceLayers(a: Advice | null) {
  if (!a) return []
  const max = Math.max(1, ...a.looked_back.late_cells.map((c) => c[1]))
  const moves = a.moves.map((m, i) => ({ ...m, n: i + 1 }))
  return [
    new H3HexagonLayer<[string, number]>({
      id: 'advice-late',
      data: a.looked_back.late_cells,
      pickable: true,
      extruded: false,
      stroked: true,
      lineWidthMinPixels: 1,
      getHexagon: (d) => d[0],
      getFillColor: (d) => [214, 51, 74, Math.round(70 + 170 * (d[1] / max))],
      getLineColor: [255, 255, 255, 180],
    }),
    new ArcLayer<(typeof moves)[number]>({
      id: 'advice-moves',
      data: moves,
      pickable: true,
      getSourcePosition: (m) => [m.from.lon, m.from.lat],
      getTargetPosition: (m) => [m.to.lon, m.to.lat],
      getSourceColor: [91, 102, 117, 230],
      getTargetColor: [31, 95, 209, 255],
      getWidth: 5,
      widthUnits: 'pixels',
    }),
    new ScatterplotLayer<(typeof moves)[number]>({
      id: 'advice-from',
      data: moves,
      getPosition: (m) => [m.from.lon, m.from.lat],
      radiusUnits: 'pixels',
      getRadius: 7,
      stroked: true,
      filled: true,
      getFillColor: [255, 255, 255, 255],
      getLineColor: [91, 102, 117, 255],
      lineWidthMinPixels: 3,
    }),
    new ScatterplotLayer<(typeof moves)[number]>({
      id: 'advice-to',
      data: moves,
      pickable: true,
      getPosition: (m) => [m.to.lon, m.to.lat],
      radiusUnits: 'pixels',
      getRadius: 12,
      stroked: true,
      getFillColor: [31, 95, 209, 255],
      getLineColor: [255, 255, 255, 255],
      lineWidthMinPixels: 2,
    }),
    new TextLayer<(typeof moves)[number]>({
      id: 'advice-n',
      data: moves,
      getPosition: (m) => [m.to.lon, m.to.lat],
      getText: (m) => String(m.n),
      getSize: 12,
      fontWeight: 800,
      getColor: [255, 255, 255, 255],
    }),
  ]
}

/** Every point the advice touches, for fitting the map view. */
export function advicePoints(a: Advice): [number, number][] {
  const pts: [number, number][] = a.moves.flatMap((m) => [[m.from.lon, m.from.lat], [m.to.lon, m.to.lat]] as [number, number][])
  return pts.length ? pts : a.looked_back.hotspots.map((h) => [h.lon, h.lat] as [number, number])
}
