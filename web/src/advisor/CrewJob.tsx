import { useEffect, useState } from 'react'
import { CALL_LABEL, STAGES, mmss, opsPost, unitName, useOps } from '../ops/ops'
import type { Incident, Stage } from '../ops/ops'
import '../ops/ops.css'

// The crew's next button for each stage.
const NEXT: Partial<Record<Stage, [Stage, string]>> = {
  alerted: ['accepted', 'Accept job'],
  accepted: ['en_route', 'Leaving now'],
  en_route: ['on_scene', 'Reached patient'],
  on_scene: ['transporting', 'Leave for hospital'],
  transporting: ['at_hospital', 'Reached hospital'],
  at_hospital: ['closed', 'Handover done, unit free'],
}

/** Crew side of live operations: the job the command centre sent to this unit, moved on one stage per tap.
 *  When a job is opened, `onJob` fills the advisor's scene, base and complaint from it. */
export function CrewJob({ hospital, onJob }: { hospital: string | null; onJob: (inc: Incident) => void }) {
  const { incidents, now, online } = useOps()
  const [unit, setUnit] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const jobs = incidents.filter((i) => i.status !== 'planned' && i.status !== 'closed')
  // No unit picked yet (or its job ended): follow the newest alert, so the demo needs no setup.
  const job = jobs.find((i) => i.unit_id === unit) ?? jobs.find((i) => i.status === 'alerted') ?? null
  const jobId = job?.id

  useEffect(() => {
    if (job) { setUnit(job.unit_id); onJob(job) }
  }, [jobId]) // oxlint-disable-line react-hooks/exhaustive-deps

  if (!online) return <div className="crew-job"><span className="crew-what">Waiting for the command centre (live operations server not running).</span></div>
  if (!job) return <div className="crew-job"><span className="crew-what">No job assigned. New jobs from the command centre appear here.</span></div>

  const next = NEXT[job.status]
  const go = async () => {
    if (!next) return
    setBusy(true)
    const dest = next[0] === 'transporting' ? hospital ?? job.draft.hospital.name : undefined
    await opsPost(`incidents/${job.id}/status`, { status: next[0], destination: dest }).catch(() => null)
    setBusy(false)
  }
  const step = STAGES.indexOf(job.status)
  return (
    <div className={`crew-job ${job.status === 'alerted' ? 'ringing' : ''}`} role="status">
      {jobs.length > 1 && (
        <select value={job.unit_id} onChange={(e) => setUnit(e.target.value)} aria-label="Your unit">
          {jobs.map((i) => <option key={i.id} value={i.unit_id}>{unitName(i)}</option>)}
        </select>
      )}
      <span className="crew-what">
        <b>{job.status === 'alerted' ? 'New job: ' : ''}{CALL_LABEL[job.draft.call_type] ?? 'Emergency'} at {job.draft.patient.label}</b>
        <small>
          {unitName(job)} · drive about {Math.round(job.draft.ambulance.eta_min)} min · call came in {mmss(now - (job.times.call ?? now))} ago
          {job.destination ? ` · going to ${job.destination}` : ''}
        </small>
      </span>
      <span className="crew-steps" aria-hidden="true">
        {STAGES.slice(2).map((s, k) => <i key={s} className={k + 2 <= step ? 'on' : ''} />)}
      </span>
      {next && (
        <button className="crew-next" disabled={busy} onClick={go}>
          {next[0] === 'transporting' ? `Leave for ${hospital ?? job.draft.hospital.name}` : next[1]}
        </button>
      )}
    </div>
  )
}
