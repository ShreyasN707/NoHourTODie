// Types mirror docs/advisor_contract.md and src/advisor/models.py (pydantic model_dump(mode="json")).
// Datetimes are naive ISO 8601 strings in Bengaluru local time.

export type Provenance = 'REAL' | 'REAL_DERIVED' | 'SIMULATED' | 'PLACEHOLDER'

export interface Tagged {
  provenance: Provenance
  source: string
  as_of: string | null
}

export interface Capability extends Tagged {
  available: boolean
  hours: string // "24x7" | "daytime" | "unknown"
}

export interface Hospital {
  hospital_id: string
  name: string
  lat: number
  lon: number
  ownership: string
  location_tag: Tagged
  capabilities: Record<string, Capability>
  empanelment: string[]
  icu_beds_total: number | null
  ventilators_total: number | null
}

export interface RegistrySummary {
  hospitals: number
  location_provenance: Record<string, number>
  capability_rows: number
  capability_provenance: Record<string, number>
  hospitals_without_capabilities: number
  hospitals_file: string
  capabilities_file: string
}

export interface HospitalsResponse {
  is_demo: boolean
  banner: string | null
  summary: RegistrySummary
  hospitals: Hospital[]
}

export interface HealthResponse {
  ok: boolean
  registry_is_demo: boolean
  travel_provider: 'osm_graph' | 'haversine'
}

export interface Assessment {
  case_id: string
  scene_lat: number
  scene_lon: number
  base_lat: number
  base_lon: number
  complaint: string
  vitals: Record<string, number>
  onset_minutes?: number | null
  minutes_since_first_contact: number
  ecg_stemi: boolean | null
  stroke_score: number | null
  when: string
}

export interface ScoreBreakdown {
  travel_min: number
  ed_wait_min: number
  p_cannot_treat: number
  transfer_delay_min: number
  lam: number
  coverage_lost_min: number
  total: number
}

export interface RankedHospital {
  hospital_id: string
  name: string
  score: number
  components: ScoreBreakdown
  capable: boolean
  notes: string[]
  inputs_provenance: Record<string, Provenance>
}

export interface ExcludedHospital {
  hospital_id: string
  name: string
  reason: string
  components: ScoreBreakdown | null
}

export interface Recommendation {
  case_id: string
  care_category: string
  route: string | null
  ranked: RankedHospital[]
  excluded: ExcludedHospital[]
  reason_code: string
  reason: string
  lambda_used: number
  inputs_provenance: Record<string, Provenance>
  non_real_inputs_used: boolean
}

export interface RecommendResponse {
  recommendation: Recommendation
  is_demo_registry: boolean
  banner: string | null
  travel_provider: 'osm_graph' | 'haversine'
  hospitals: Record<string, { name: string; lat: number; lon: number }>
  todo_params: string[]
}

// ---------------------------------------------------------------- pre-alert (advisor.prealert.fallback)

export type PreAlertState = 'pending' | 'accepted' | 'exhausted'

export interface Attempt {
  hospital_id: string
  sent_at: string
  eta_min: number | null // NaN serialises as null
  answered_at: string | null
  accepted: boolean | null
  reason_code: string | null
}

export interface PreAlert {
  prealert_id: string
  case_id: string
  care_category: string
  queue: string[]
  etas: Record<string, number | null>
  attempts: Attempt[]
  state: PreAlertState
  crew_override: boolean
}

// ---------------------------------------------------------------- simulation results file

export interface Stat {
  n: number
  mean: number | null
  p50: number | null
  p90: number | null
}

export interface Metrics {
  n: number
  time_to_treatment: { overall: Stat; by_category: Record<string, Stat> }
  secondary_transfers: number
  secondary_transfer_rate: number
  ambulance_hours: number
  critical_capable_first_attempt_share: number
  prealert_rejects: number
  fallbacks: number
}

export interface Scenario {
  name: string
  overrides: Record<string, unknown>
  policies: { nearest: Metrics; advisor: Metrics }
}

export interface SensitivityRow {
  param: string
  value: unknown
  label: string
  nearest_mean_ttt_critical: number
  advisor_mean_ttt_critical: number
  delta_mean_ttt_critical: number
  advisor_better: boolean
}

export interface SimResults {
  generated_at: string
  seed: number
  banner: string | null
  inputs_provenance: Record<string, Provenance>
  window: { start: string; end: string; n_incidents: number }
  base: Scenario
  sensitivity: SensitivityRow[]
}
