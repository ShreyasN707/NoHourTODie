import type { CauseKey, Severity } from './types'

export type RGB = [number, number, number]

/** Cause of a late arrival. Red and amber are the app's own critical / at-risk tokens (index.css). */
export const CAUSE_RGB: Record<CauseKey, RGB> = {
  unreachable: [214, 51, 74], // --critical: no site can reach it
  uncovered: [217, 138, 0], // --risk: a site could, none is staffed
  busy: [124, 58, 237], // violet: staffed nearby, unit was out
}

/** Severity: 90th-percentile response vs target (moderate <= 1.5x, high <= 2x, severe > 2x). Sequential red. */
export const SEVERITY_RGB: Record<Severity, RGB> = {
  moderate: [244, 166, 175],
  high: [214, 51, 74],
  severe: [128, 18, 38],
}

/** Status against the reference (previous 12 weeks, or the stage before), selection, and proposed posts. */
export const CHANGE_RGB = {
  resolved: [30, 156, 98] as RGB, // --ok: green outline
  new: [192, 38, 211] as RGB, // magenta outline
  chronic: [23, 32, 43] as RGB, // --ink: dark outline
  post: [8, 145, 178] as RGB, // cyan pin
  selected: [31, 95, 209] as RGB, // --action
}

export const css = (c: RGB) => `rgb(${c[0]}, ${c[1]}, ${c[2]})`

const CAUSE_LABEL: Record<CauseKey, string> = {
  unreachable: 'Out of reach',
  uncovered: 'No unit planned nearby',
  busy: 'Nearby unit busy',
}
export const causeLabel = (c: CauseKey) => CAUSE_LABEL[c]

export const SEVERITY_LABEL: Record<Severity, string> = {
  severe: 'Severe: slowest 1 in 10 > 2× target',
  high: 'High: > 1.5× target',
  moderate: 'Moderate',
}
