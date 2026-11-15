/** Black-spot map layers and the chronic / new / resolved classification, shared by the map (App.tsx) and the panel. */
import { H3HexagonLayer } from '@deck.gl/geo-layers'
import { ScatterplotLayer, TextLayer } from '@deck.gl/layers'
import type { Layer } from '@deck.gl/core'
import { CAUSE_RGB, CHANGE_RGB, SEVERITY_RGB, causeLabel } from './palette'
import type { RGB } from './palette'
import type { BlackSpotData, CauseKey, Cell, CellRow, Post, Scenario, ScenarioKey, Site, Status } from './types'
import { CAUSE_KEYS, ROW, SEVERITIES, prevStage } from './types'

export type SpotColor = 'cause' | 'severity'
/** What chronic / new / resolved are measured against: the same plan's previous 12 weeks, or the stage before. */
export type CompareMode = 'before' | 'stage'

export interface SpotFilter {
  statuses: Set<Status>
  causes: Set<CauseKey>
}
export const ALL_FILTER: SpotFilter = { statuses: new Set(['chronic', 'new', 'resolved']), causes: new Set(CAUSE_KEYS) }

/** A hexagon on the map. `spot` marks it for App's tooltip and click handlers. */
export interface SpotHex {
  spot: 'black' | 'resolved' | 'context'
  h3: string
  idx: number
  row: CellRow | null // null: no calls in this window
  zone: string
  status: Status | null
  cluster: number | null
}

export interface PostPin extends Post {
  n: number
  used: boolean
  spotPost: true
}

export interface Classified {
  /** Reference the statuses are measured against */
  refKey: ScenarioKey | null // null = previous 12 weeks of the same plan
  refLabel: string
  rows: Map<number, CellRow>
  status: Map<number, Status> // black (chronic/new) and resolved cells
  counts: Record<Status, number>
  causeCounts: Record<CauseKey, number> // black spots by dominant cause
}

const rgba = (c: RGB, a: number): [number, number, number, number] => [c[0], c[1], c[2], a]
const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]!)

export const STATUS_LABEL: Record<CompareMode, Record<Status, string>> = {
  before: { chronic: 'Chronic', new: 'New', resolved: 'Resolved' },
  stage: { chronic: 'Still black', new: 'New', resolved: 'Resolved' },
}

/** Effective comparison: a plan as it is has no stage before it, so it is always compared with its previous weeks. */
export function effectiveMode(scenario: Scenario, mode: CompareMode): CompareMode {
  return mode === 'stage' && prevStage(scenario.key) ? 'stage' : 'before'
}

export function classify(data: BlackSpotData, scenario: Scenario, mode: CompareMode): Classified {
  const m = effectiveMode(scenario, mode)
  const refKey = m === 'stage' ? prevStage(scenario.key) : null
  const refScenario = refKey ? data.scenarios.find((s) => s.key === refKey) : null
  const ref = new Set(refScenario ? refScenario.cells.filter((r) => r[ROW.sev] >= 0).map((r) => r[ROW.idx]) : scenario.before_black)
  const rows = new Map(scenario.cells.map((r) => [r[ROW.idx], r]))
  const status = new Map<number, Status>()
  const counts: Record<Status, number> = { chronic: 0, new: 0, resolved: 0 }
  const causeCounts: Record<CauseKey, number> = { unreachable: 0, uncovered: 0, busy: 0 }
  for (const r of scenario.cells) {
    if (r[ROW.sev] < 0) continue
    const st: Status = ref.has(r[ROW.idx]) ? 'chronic' : 'new'
    status.set(r[ROW.idx], st)
    counts[st]++
    causeCounts[CAUSE_KEYS[r[ROW.cause]]]++
  }
  for (const i of ref) {
    if (!status.has(i)) {
      status.set(i, 'resolved')
      counts.resolved++
    }
  }
  const refLabel = refScenario ? refScenario.label : `the ${Math.round(data.windows.days / 7)} weeks before`
  return { refKey, refLabel, rows, status, counts, causeCounts }
}

export const SPOT_LAYER_IDS = ['spot-context', 'spot-black', 'spot-resolved', 'spot-selected', 'spot-posts', 'spot-post-labels']

/**
 * Layers for one scenario's black spots.
 *  mode 'full'    (Fleet plan, Black spots colouring): all hexagons with calls, statuses, filters, posts, selection
 *  mode 'overlay' (Live calls): black spots only, lighter, so live calls and routes stay on top
 */
export function blackSpotLayers(opts: {
  data: BlackSpotData
  scenario: Scenario
  cls: Classified
  cells: Cell[]
  color: SpotColor
  mode: 'full' | 'overlay'
  filter: SpotFilter
  selectedCluster: number | null
  selectedHex: number | null
}): Layer[] {
  const { data, scenario, cls, cells, color, mode, filter, selectedCluster, selectedHex } = opts
  const full = mode === 'full'
  const clusterOf = new Map<number, number>()
  for (const c of scenario.clusters) for (const i of c.cells) clusterOf.set(i, c.id)
  const hex = (idx: number, spot: SpotHex['spot']): SpotHex => ({
    spot,
    h3: cells[idx].h3,
    idx,
    row: cls.rows.get(idx) ?? null,
    zone: cells[idx].zone,
    status: cls.status.get(idx) ?? null,
    cluster: clusterOf.get(idx) ?? null,
  })

  const black: SpotHex[] = []
  const context: SpotHex[] = []
  for (const r of scenario.cells) {
    const i = r[ROW.idx]
    if (!cells[i]) continue
    if (r[ROW.sev] >= 0) {
      const st = cls.status.get(i)!
      if (!full || (filter.statuses.has(st) && filter.causes.has(CAUSE_KEYS[r[ROW.cause]]))) black.push(hex(i, 'black'))
    } else if (full && cls.status.get(i) !== 'resolved') context.push(hex(i, 'context'))
  }
  const resolved =
    full && filter.statuses.has('resolved')
      ? [...cls.status].filter(([i, st]) => st === 'resolved' && cells[i]).map(([i]) => hex(i, 'resolved'))
      : []

  const alpha = full ? 215 : 160
  const fill = (d: SpotHex) =>
    color === 'severity' ? rgba(SEVERITY_RGB[SEVERITIES[d.row![ROW.sev]]], alpha) : rgba(CAUSE_RGB[CAUSE_KEYS[d.row![ROW.cause]]], alpha)

  const selCells = selectedHex !== null ? [selectedHex] : (scenario.clusters.find((c) => c.id === selectedCluster)?.cells ?? [])

  const used = new Set(scenario.posts_used ?? [])
  const posts: PostPin[] =
    full && (scenario.posts_used !== undefined)
      ? data.posts.map((p, i) => ({ ...p, n: i + 1, used: used.has(p.index), spotPost: true }))
      : []

  const layers: (Layer | null)[] = [
    context.length
      ? new H3HexagonLayer<SpotHex>({
          id: 'spot-context',
          data: context,
          pickable: true,
          extruded: false,
          stroked: false,
          getHexagon: (d) => d.h3,
          // Neutral grey that darkens with the share of late calls: context only, black spots carry the colour
          getFillColor: (d) => [91, 102, 117, Math.round(20 + 130 * (d.row![ROW.late] / Math.max(1, d.row![ROW.calls])))],
          updateTriggers: { getFillColor: [scenario.key] },
        })
      : null,
    resolved.length
      ? new H3HexagonLayer<SpotHex>({
          id: 'spot-resolved',
          data: resolved,
          pickable: true,
          extruded: false,
          stroked: true,
          highPrecision: true,
          lineWidthUnits: 'pixels',
          getLineWidth: 2.5,
          getHexagon: (d) => d.h3,
          getFillColor: rgba(CHANGE_RGB.resolved, 60),
          getLineColor: rgba(CHANGE_RGB.resolved, 255),
        })
      : null,
    new H3HexagonLayer<SpotHex>({
      id: 'spot-black',
      data: black,
      pickable: true,
      extruded: false,
      stroked: true,
      highPrecision: true,
      lineWidthUnits: 'pixels',
      // Dark outline: chronic (also black in the reference); magenta: new. The overlay keeps a thin white edge.
      getLineWidth: (d) => (full ? (d.status === 'chronic' ? 2 : 2.5) : 1),
      getLineColor: (d) =>
        !full ? [255, 255, 255, 200] : d.status === 'chronic' ? rgba(CHANGE_RGB.chronic, 235) : rgba(CHANGE_RGB.new, 255),
      getHexagon: (d) => d.h3,
      getFillColor: fill,
      updateTriggers: { getFillColor: [color, scenario.key, mode], getLineWidth: [mode, cls], getLineColor: [mode, cls] },
    }),
    selCells.length
      ? new H3HexagonLayer<{ h3: string }>({
          id: 'spot-selected',
          data: selCells.filter((i) => cells[i]).map((i) => ({ h3: cells[i].h3 })),
          extruded: false,
          filled: false,
          stroked: true,
          highPrecision: true,
          lineWidthUnits: 'pixels',
          getLineWidth: 4,
          getHexagon: (d) => d.h3,
          getLineColor: rgba(CHANGE_RGB.selected, 255),
        })
      : null,
    posts.length
      ? new ScatterplotLayer<PostPin>({
          id: 'spot-posts',
          data: posts,
          pickable: true,
          stroked: true,
          filled: true,
          radiusUnits: 'pixels',
          getRadius: 12,
          lineWidthUnits: 'pixels',
          getLineWidth: 3,
          getPosition: (d) => [d.lon, d.lat],
          getFillColor: (d) => (d.used ? rgba(CHANGE_RGB.post, 240) : [255, 255, 255, 235]),
          getLineColor: rgba(CHANGE_RGB.post, 255),
          updateTriggers: { getFillColor: [scenario.key] },
        })
      : null,
    posts.length
      ? new TextLayer<PostPin>({
          id: 'spot-post-labels',
          data: posts,
          getPosition: (d) => [d.lon, d.lat],
          getText: (d) => `P${d.n}`,
          getSize: 10,
          fontWeight: 800,
          fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
          getColor: (d) => (d.used ? [255, 255, 255, 255] : [23, 32, 43, 255]),
          updateTriggers: { getColor: [scenario.key] },
        })
      : null,
  ]
  return layers.filter((l): l is Layer => l !== null)
}

/** Tooltip HTML for a black-spot hexagon or a proposed post, or null if the object is neither. */
export function spotTooltipHtml(object: unknown, target: number, statusLabels: Record<Status, string>): string | null {
  const o = object as Partial<SpotHex & PostPin>
  if (o.spotPost) {
    const p = object as PostPin
    return `<b>${esc(p.name)}</b><br/>Proposed standby post: ${p.used ? 'a unit waits here in this plan' : 'this plan puts no unit here'}<br/><span style="color:#5B6675">Reaches ${p.late_calls_in_reach} out-of-reach late calls in time (planning weeks)</span>`
  }
  if (!o.spot) return null
  const lines = [`<b>${esc(o.zone ?? '')}</b>`]
  if (o.status) {
    const colour = o.status === 'resolved' ? '#1E9C62' : '#D6334A'
    const what = o.status === 'resolved' ? 'Resolved black spot' : `Black spot · ${statusLabels[o.status].toLowerCase()}`
    lines.push(`<span style="color:${colour};font-weight:700">${what}</span>`)
  }
  const r = o.row
  if (!r) {
    lines.push('No calls in these weeks')
    return lines.join('<br/>')
  }
  const [, calls, late, , p90] = r
  lines.push(`${late} of ${calls} calls late (${Math.round((100 * late) / Math.max(1, calls))}%)`)
  lines.push(`Confidence: at least ${Math.round(r[ROW.lo] * 100)}% late (95%)`)
  lines.push(`Slowest 1 in 10: ${p90.toFixed(1)} min (target ${target})`)
  if (r[ROW.cause] >= 0) lines.push(`Main cause: ${causeLabel(CAUSE_KEYS[r[ROW.cause]])}`)
  lines.push('<span style="color:#5B6675">Click for details</span>')
  return lines.join('<br/>')
}

/** A fix scenario's units for one block, in the deployment shape App's vehicle markers already draw. */
export function fixDeployments(data: BlackSpotData, scenario: Scenario, block: number, sites: Site[]) {
  const all: (Site | Post)[] = [...sites, ...data.posts]
  return (scenario.deployments[block] ?? []).flatMap(([s, a, b, y]) => {
    const site = all[s]
    if (!site) return []
    return [{ site_id: site.site_id, name: site.name, site_type: site.site_type, lat: site.lat, lon: site.lon, a, b, y }]
  })
}
