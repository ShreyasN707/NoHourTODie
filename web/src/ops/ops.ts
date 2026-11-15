import { useEffect, useState } from 'react'
import type { Dispatch } from '../Assistant'

// Live-operations backend (backend/ops.py). Shared by the command centre and the crew dashboard.
export const OPS_API: string =
  (import.meta.env.VITE_OPS_API as string | undefined) ?? `${window.location.protocol}//${window.location.hostname}:8788`

export const STAGES = ['planned', 'alerted', 'accepted', 'en_route', 'on_scene', 'transporting', 'at_hospital', 'closed'] as const
export type Stage = (typeof STAGES)[number]

export type Incident = {
  id: string
  source: string
  status: Stage
  unit_id: string
  times: Partial<Record<'call' | Stage, number>>
  draft: Dispatch & { ambulance: Dispatch['ambulance'] & { id: string } }
  destination: string | null
  log: { at: number; text: string }[]
  cover: null | {
    site: string
    cells_lost: number
    calls_per_hour_lost: number
    zones: string[]
    approved: boolean
    move: null | { from_site: string; to_site: string; drive_min: number; net_calls_per_hour: number }
  }
}

export const STATUS_LABEL: Record<Stage, string> = {
  planned: 'Waiting for confirmation',
  alerted: 'Crew alerted',
  accepted: 'Crew accepted',
  en_route: 'On the way',
  on_scene: 'With the patient',
  transporting: 'Going to hospital',
  at_hospital: 'At hospital',
  closed: 'Done, unit free',
}

// Stage name -> the time it measures (the delays the CAG audit pointed to).
export const STAGE_TIMES: [Stage | 'call', Stage, string][] = [
  ['call', 'alerted', 'Call to crew alert'],
  ['alerted', 'accepted', 'Crew answers'],
  ['accepted', 'en_route', 'Wheels rolling'],
  ['en_route', 'on_scene', 'Drive to patient'],
  ['on_scene', 'transporting', 'Time on scene'],
  ['transporting', 'at_hospital', 'Drive to hospital'],
  ['at_hospital', 'closed', 'Handover'],
]

export const CALL_LABEL: Record<string, string> = {
  pregnancy: 'Pregnancy', trauma: 'Accident / injury', cardiac: 'Heart', stroke: 'Stroke', respiratory: 'Breathing', other: 'Emergency',
}

export const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`
export const unitName = (inc: Incident) => `${inc.draft.ambulance.kind} from ${inc.draft.ambulance.from_site}`

export async function opsPost<T = Incident>(path: string, body: unknown = {}): Promise<T & { error?: string }> {
  const r = await fetch(`${OPS_API}/ops/${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  return r.json()
}

/** Polls the shared incident list once a second. `online` is false while the backend can't be reached. */
export function useOps() {
  const [state, setState] = useState<{ incidents: Incident[]; now: number; online: boolean }>({ incidents: [], now: 0, online: false })
  useEffect(() => {
    let stop = false
    const tick = async () => {
      try {
        const r = await fetch(`${OPS_API}/ops/state`)
        const j = await r.json()
        if (!stop) setState({ incidents: j.incidents, now: j.now, online: true })
      } catch {
        if (!stop) setState((s) => ({ ...s, online: false }))
      }
    }
    tick()
    const t = setInterval(tick, 1000) // ponytail: 1 s polling; switch to server-sent events if it ever lags
    return () => { stop = true; clearInterval(t) }
  }, [])
  return state
}
