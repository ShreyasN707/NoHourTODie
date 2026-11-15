import type { Provenance, ScoreBreakdown } from './types'

/** Mirrors src/advisor/rules/triage.py COMPLAINTS (order kept). */
export const COMPLAINTS: { value: string; label: string }[] = [
  { value: 'chest_pain', label: 'Chest pain' },
  { value: 'stroke_symptoms', label: 'Stroke symptoms' },
  { value: 'trauma', label: 'Trauma' },
  { value: 'obstetric', label: 'Obstetric' },
  { value: 'paediatric', label: 'Paediatric' },
  { value: 'cardiac_arrest', label: 'Cardiac arrest' },
  { value: 'airway', label: 'Airway' },
  { value: 'other', label: 'Other' },
]

/** Traffic-flip demo: same assessment at 11:00 and 18:00 on this day (contract: no special endpoint). */
export const DEMO_DAY = '2026-11-16'
export const DEMO_HOURS = [11, 18] as const
export type DemoHour = (typeof DEMO_HOURS)[number]
export const whenAt = (hour: number) => `${DEMO_DAY}T${String(hour).padStart(2, '0')}:00:00`

export const REJECT_REASONS = ['NO_BED', 'NO_SPECIALIST', 'EQUIPMENT_DOWN', 'OTHER'] as const

export const PROVENANCE_ORDER: Provenance[] = ['REAL', 'REAL_DERIVED', 'SIMULATED', 'PLACEHOLDER']

export const PROVENANCE_INFO: Record<Provenance, { label: string; meaning: string }> = {
  REAL: { label: 'Real', meaning: 'from a named public source or our own verification' },
  REAL_DERIVED: { label: 'Real, derived', meaning: 'computed from real data' },
  SIMULATED: { label: 'Simulated', meaning: 'no real source available; modelled' },
  PLACEHOLDER: { label: 'Placeholder', meaning: 'tunable value awaiting calibration or sign-off' },
}

export const INPUT_LABELS: Record<string, string> = {
  travel: 'Travel time',
  ed_wait: 'ED wait',
  capabilities: 'Capabilities',
  p_cannot_treat: 'P(cannot treat)',
  transfer_delay: 'Transfer delay',
  lambda: 'Coverage weight λ',
  triage_thresholds: 'Triage thresholds',
  incidents: 'Incidents',
  hospitals: 'Hospitals',
  road_network: 'Road network',
  travel_speeds: 'Travel speeds',
  ed_crowding: 'ED crowding',
  rejections: 'Rejections',
}

export const CAPABILITY_LABELS: Record<string, string> = {
  ed_24x7: '24x7 emergency department',
  ct_24x7: '24x7 CT',
  pci_24x7: '24x7 cath lab (PCI)',
  thrombolysis: 'thrombolysis',
  thrombectomy: 'thrombectomy',
  trauma_ot: 'trauma theatre',
  neurosurgery: 'neurosurgery',
  orthopaedics: 'orthopaedics',
  blood_bank: 'blood bank',
  obstetric_emergency: 'obstetric emergency',
  nicu: 'NICU',
  picu: 'PICU',
  burns_unit: 'burns unit',
}

export const humanize = (s: string) => s.replace(/_/g, ' ')
export const inputLabel = (k: string) => INPUT_LABELS[k] ?? humanize(k)
export const capLabel = (k: string) => CAPABILITY_LABELS[k] ?? humanize(k)

/** ExcludedHospital.reason in plain words: "missing:pci_24x7" -> "no 24x7 cath lab (PCI)". */
export function excludedReason(reason: string): string {
  if (reason.startsWith('missing:')) {
    return 'no ' + reason.slice('missing:'.length).split(',').map(capLabel).join(', no ')
  }
  if (reason === 'diversion') return 'on diversion'
  if (reason.startsWith('over_')) return `beyond the ${humanize(reason.slice(5))}`
  return humanize(reason)
}

export const fmtMin = (m: number | null | undefined, digits = 0) =>
  m == null || Number.isNaN(m) ? 'n/a' : `${m.toFixed(digits)} min`

export const fmtNum = (m: number | null | undefined, digits = 1) =>
  m == null || Number.isNaN(m) ? 'n/a' : m.toFixed(digits)

export const fmtPct = (x: number | null | undefined, digits = 1) =>
  x == null || Number.isNaN(x) ? 'n/a' : `${(x * 100).toFixed(digits)}%`

/** "2026-11-16T18:00:03.123" -> "18:00:03" */
export const fmtClock = (iso: string | null) => (iso ? (iso.split('T')[1] ?? iso).slice(0, 8) : '')

/** The four additive terms of the score (spec §4). Order = stacking order = palette order. */
export const SCORE_PARTS = [
  { key: 'travel', label: 'Travel', value: (c: ScoreBreakdown) => c.travel_min },
  { key: 'ed_wait', label: 'ED wait', value: (c: ScoreBreakdown) => c.ed_wait_min },
  {
    key: 'transfer',
    label: 'P(cannot treat) × transfer delay',
    short: 'Transfer risk',
    value: (c: ScoreBreakdown) => c.p_cannot_treat * c.transfer_delay_min,
  },
  {
    key: 'coverage',
    label: 'λ × coverage lost',
    short: 'Coverage cost',
    value: (c: ScoreBreakdown) => c.lam * c.coverage_lost_min,
  },
] as const

export const scoreParts = (c: ScoreBreakdown) =>
  SCORE_PARTS.map((p) => ({ key: p.key, label: p.label, short: 'short' in p ? p.short : p.label, v: Math.max(0, p.value(c)) }))
