/** Shapes of web/public/data/blackspots_{region}.json, written by pipeline/blackspots.py. */

export type Region = 'blr' | 'tmk'
export type CauseKey = 'unreachable' | 'uncovered' | 'busy'
export type Severity = 'moderate' | 'high' | 'severe'
/** Stage 3 keys are fleet{percent}, one per fleet step in pipeline/config.py (BLACKSPOT_FLEET_STEPS). */
export type FixKey = 'targeted' | 'posts' | `fleet${number}`
export type ScenarioKey = 'today' | 'static' | 'current' | FixKey
export const isFleetKey = (k: string) => k.startsWith('fleet')
/** The stage each stage is compared with (before -> after). */
export const PREV_STAGE: Record<string, ScenarioKey> = { targeted: 'current', posts: 'targeted' }
export const prevStage = (k: ScenarioKey): ScenarioKey | null => PREV_STAGE[k] ?? (isFleetKey(k) ? 'posts' : null)

export type Status = 'chronic' | 'new' | 'resolved'
export const STATUSES: Status[] = ['chronic', 'new', 'resolved']

export const CAUSE_KEYS: CauseKey[] = ['unreachable', 'uncovered', 'busy']
export const SEVERITIES: Severity[] = ['moderate', 'high', 'severe']

export interface Kpis {
  calls: number
  within_pct: number
  median_min: number
  p90_min: number
  late: number
  excess_min: number
  black_spots: number
  black_calls: number
  black_late: number
  first_help_10m_pct: number
  queued_pct: number
}

/** [cell index, calls, late calls, minutes late (sum), p90 min, dominant cause index (-1 = none),
 *   severity index (-1 = not a black spot), confidence (Wilson lower bound of the late share),
 *   late calls out of reach, no unit planned nearby, nearby unit busy] */
export type CellRow = [number, number, number, number, number, number, number, number, number, number, number]
export const ROW = { idx: 0, calls: 1, late: 2, excess: 3, p90: 4, cause: 5, sev: 6, lo: 7, nCause: 8 } as const

/** Per block: [site index, ALS, BLS, bikes]. Site index runs over sites_{r}.json, then proposed posts. */
export type Deployment = [number, number, number, number]

export interface Cluster {
  id: number
  cells: number[]
  lat: number
  lon: number
  zones: string[]
  calls: number
  late: number
  late_pct: number
  excess_min: number
  mean_late_by_min: number
  worst_p90_min: number
  causes: Record<CauseKey, number>
  cause: CauseKey
  fix: string
  severity: Severity
}

export interface Scenario {
  key: ScenarioKey
  label: string
  description: string
  fleet: { ambulances: number; als: number; bls: number; bikes: number }
  kpis: Kpis
  late_by_cause: Record<CauseKey, number>
  cells: CellRow[]
  clusters: Cluster[]
  deployments: Deployment[][]
  /** Black spots of the same plan in the previous window */
  before_black: number[]
  vs_before: { chronic: number; new: number; resolved: number }
  vs_current?: { fixed: number; remaining: number; new: number }
  posts_used?: number[]
}

export interface Post {
  site_id: string
  h3_index: string
  site_type: 'proposed_post'
  name: string
  lat: number
  lon: number
  zone: string
  late_calls_in_reach: number
  index: number
}

export interface Iteration {
  round: number
  label: string
  tune: Kpis
  test: Kpis
  max_weight?: number
  selected?: boolean
}

export interface BlackSpotData {
  region: Region
  region_name: string
  zone_label: string
  target_min: number
  generated_at: string
  quick: boolean
  synthetic: boolean
  windows: { tune: Window; test: Window; days: number }
  criteria: {
    min_late: number
    min_late_share: number
    confidence: string
    alpha: number
    max_weight: number
    rounds: number
    max_new_posts: number
    min_post_gain: number
    candidate_posts: number
  }
  causes: { key: CauseKey; label: string; detail: string; fix: string }[]
  n_sites: number
  posts: Post[]
  scenarios: Scenario[]
  iterations: Iteration[]
  fleet_curve: { extra_pct: number; physical: number; on_road: number; within_pct: number; black_spots: number; late: number; p90_min: number }[]
  stages: Stage[]
  solver_seconds: number
}

export interface Stage {
  key: ScenarioKey
  label: string
  prev: ScenarioKey | null
  kpis: Pick<Kpis, 'black_spots' | 'black_late' | 'late' | 'within_pct' | 'p90_min' | 'median_min'>
  late_by_cause: Record<CauseKey, number>
  fleet: { ambulances: number; als: number; bls: number; bikes: number }
  vs_prev?: { resolved: number; remaining: number; new: number }
}

export interface Window {
  start: string
  end: string
  calls: number
}

export interface Cell {
  h3: string
  lat: number
  lon: number
  zone: string
}

export interface Site {
  site_id: string
  name: string
  site_type: string
  lat: number
  lon: number
}
