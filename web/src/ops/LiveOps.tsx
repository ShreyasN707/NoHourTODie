import { useEffect, useRef, useState } from 'react'
import type { Dispatch } from '../Assistant'
import { CALL_LABEL, STAGE_TIMES, STATUS_LABEL, mmss, opsPost, unitName, useOps } from './ops'
import type { Incident } from './ops'
import './ops.css'

/** Command centre: live incident queue. A call comes in -> it is planned automatically -> the operator confirms
 *  -> the crew dashboard takes over, and its progress shows up here stage by stage. */
export default function LiveOps({ region, onShow }: { region: 'blr' | 'tmk'; onShow: (d: Dispatch) => void }) {
  const { incidents, now, online } = useOps()
  const [sel, setSel] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const seen = useRef<Set<string> | null>(null)

  // A new call: select it and show its plan on the map straight away.
  useEffect(() => {
    if (!online) return
    if (seen.current === null) { seen.current = new Set(incidents.map((i) => i.id)); return }
    const fresh = incidents.find((i) => !seen.current!.has(i.id))
    incidents.forEach((i) => seen.current!.add(i.id))
    if (fresh) { setSel(fresh.id); onShow(fresh.draft) }
  }, [incidents, online]) // oxlint-disable-line react-hooks/exhaustive-deps

  const act = async (path: string, body?: unknown) => {
    setBusy(true); setErr(null)
    try {
      const r = await opsPost(path, body)
      if (r.error) setErr(r.error)
    } catch { setErr('Live operations server is not running (make ops).') }
    setBusy(false)
  }

  const active = incidents.filter((i) => i.status !== 'closed')
  const done = incidents.filter((i) => i.status === 'closed')

  return (
    <section className="ops-panel" aria-label="Live operations">
      <header className="ops-head">
        <div>
          <h2>Live calls</h2>
          <p>{online ? `${active.length} open call${active.length === 1 ? '' : 's'}` : 'Server not running: start it with make ops'}</p>
        </div>
      </header>
      <div className="ops-actions">
        <button className="ops-btn primary" disabled={!online || busy} onClick={() => act('calls', { region })}>
          {busy ? 'Planning…' : 'Simulate incoming call'}
        </button>
        <button className="ops-btn" disabled={!online || busy || !incidents.length} onClick={() => act('reset')}>Clear all</button>
      </div>
      <p className="ops-note">Simulated calls are drawn from our modelled call log for this hour. In service they would come from the C-DAC system.</p>
      {err && <p className="ops-err">{err}</p>}
      <div className="ops-list">
        {active.length === 0 && online && <p className="ops-empty">No open calls. Press “Simulate incoming call”, or ask the assistant to send help somewhere.</p>}
        {[...active, ...done].map((inc) => (
          <Card key={inc.id} inc={inc} now={now} open={sel === inc.id} busy={busy}
            onSelect={() => { setSel(sel === inc.id ? null : inc.id); onShow(inc.draft) }}
            onConfirm={() => act(`incidents/${inc.id}/confirm`)}
            onApprove={() => act(`incidents/${inc.id}/approve_move`)} />
        ))}
      </div>
    </section>
  )
}

function Card({ inc, now, open, busy, onSelect, onConfirm, onApprove }: {
  inc: Incident; now: number; open: boolean; busy: boolean; onSelect: () => void; onConfirm: () => void; onApprove: () => void
}) {
  const d = inc.draft
  const end = inc.times.closed ?? now
  const c = inc.cover
  return (
    <article className={`ops-card s-${inc.status} ${open ? 'is-open' : ''}`}>
      <button className="ops-card-main" onClick={onSelect}>
        <span className="ops-row">
          <span className="ops-chip">{CALL_LABEL[d.call_type] ?? 'Emergency'}</span>
          <span className={`ops-status s-${inc.status}`}>{STATUS_LABEL[inc.status]}</span>
          <span className="ops-clock">{mmss(Math.max(0, end - (inc.times.call ?? end)))}</span>
        </span>
        <span className="ops-place">{d.patient.label}</span>
        <span className="ops-sub">
          {unitName(inc)}: <b>{Math.round(d.ambulance.eta_min)} min</b>{' '}
          <span className={d.ambulance.within_target ? 'ok' : 'late'}>
            ({d.ambulance.within_target ? 'within' : 'over'} the {d.target_min}-min target)
          </span>
        </span>
        {d.bike && <span className="ops-sub">Bike first responder: <b>{Math.round(d.bike.eta_min)} min</b></span>}
        <span className="ops-sub">Hospital: {inc.destination ?? d.hospital.name}{inc.destination ? ' (chosen by crew)' : ''}</span>
      </button>

      {inc.status === 'planned' && (
        <div className="ops-confirm">
          <p>{d.alerts.length} alerts are ready: crew, hospital, traffic police{d.bike ? ', bike rider' : ''}.</p>
          <button className="ops-btn primary" disabled={busy} onClick={onConfirm}>Confirm and alert crew</button>
        </div>
      )}

      {c && inc.status !== 'closed' && (
        <div className="ops-cover">
          {c.calls_per_hour_lost === 0 ? (
            <p>The area around {c.site} is still covered by other units within the target.</p>
          ) : (
            <>
              <p>
                With this unit away, <b>{c.cells_lost} area{c.cells_lost === 1 ? '' : 's'}</b>
                {c.zones.length ? ` (${c.zones.slice(0, 3).join(', ')})` : ''} have no free ambulance within the target,
                about <b>{c.calls_per_hour_lost}</b> expected calls an hour.
              </p>
              {c.move ? (
                c.approved ? (
                  <p className="ok">Cover move ordered: {c.move.from_site} → {c.move.to_site}.</p>
                ) : (
                  <div className="ops-move">
                    <p>Suggested: move the unit at <b>{c.move.from_site}</b> to <b>{c.move.to_site}</b> ({c.move.drive_min} min drive).</p>
                    <button className="ops-btn" disabled={busy} onClick={onApprove}>Approve move</button>
                  </div>
                )
              ) : (
                <p>{c.calls_per_hour_lost < 0.05
                  ? 'The gap is small, so no unit needs to move.'
                  : 'No nearby unit can move without opening a bigger gap, so we leave the plan as it is.'}</p>
              )}
            </>
          )}
        </div>
      )}

      {open && (
        <ol className="ops-timeline">
          {STAGE_TIMES.map(([a, b, label]) => {
            const ta = inc.times[a], tb = inc.times[b]
            return (
              <li key={b} className={tb ? 'done' : ta ? 'now' : ''}>
                <span>{label}</span>
                <b>{ta ? mmss((tb ?? now) - ta) : '–'}</b>
              </li>
            )
          })}
        </ol>
      )}
    </article>
  )
}
