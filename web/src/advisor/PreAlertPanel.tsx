import { useState } from 'react'
import { Check, Send, X } from 'lucide-react'
import { api } from './api'
import { ApiErrorNotice } from './common'
import { REJECT_REASONS, fmtClock, fmtMin } from './format'
import type { PreAlert, Recommendation } from './types'

/** Demo stub of the pre-alert loop (advisor.prealert.fallback). Remount (key) when the recommendation changes. */
export function PreAlertPanel({ rec, names }: { rec: Recommendation; names: Record<string, string> }) {
  const [crewChoice, setCrewChoice] = useState('')
  const [rejectReason, setRejectReason] = useState<string>(REJECT_REASONS[0])
  const [pa, setPa] = useState<PreAlert | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const name = (id: string) => names[id] ?? id

  async function run(fn: () => Promise<PreAlert>) {
    setBusy(true)
    setError(null)
    try {
      setPa(await fn())
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  const current = pa?.state === 'pending' ? pa.attempts[pa.attempts.length - 1] : undefined

  return (
    <section className="adv-prealert" aria-label="Pre-alert">
      <p className="adv-stub">Demo stub: nothing is sent. Hospital answers below are simulated by you.</p>

      {!pa && (
        <div className="adv-prealert__send">
          <label>
            <span>Destination</span>
            <select value={crewChoice} onChange={(e) => setCrewChoice(e.target.value)} disabled={!rec.ranked.length}>
              <option value="">Advisor's #1: {rec.ranked[0]?.name ?? 'none'}</option>
              {rec.ranked.slice(1).map((r, i) => (
                <option key={r.hospital_id} value={r.hospital_id}>
                  Crew override: #{i + 2} {r.name}
                </option>
              ))}
            </select>
          </label>
          <button
            className="adv-btn adv-btn--primary"
            disabled={busy || !rec.ranked.length}
            onClick={() => run(() => api.sendPreAlert(rec.case_id, crewChoice || undefined))}
          >
            <Send size={14} aria-hidden="true" /> Send pre-alert
          </button>
        </div>
      )}

      {pa && (
        <>
          <div className={`adv-prealert__state adv-prealert__state--${pa.state}`} role="status">
            {pa.state === 'pending' && current && (
              <>
                Waiting for <strong>{name(current.hospital_id)}</strong> (ETA {fmtMin(current.eta_min)})
              </>
            )}
            {pa.state === 'accepted' && (
              <>
                Accepted by <strong>{name(pa.attempts[pa.attempts.length - 1].hospital_id)}</strong>. Proceed.
              </>
            )}
            {pa.state === 'exhausted' && <>Every hospital in the queue declined. Escalate to the command centre.</>}
            {pa.crew_override && <span className="adv-chip">crew override</span>}
          </div>

          {current && (
            <div className="adv-prealert__answer">
              <button
                className="adv-btn adv-btn--good"
                disabled={busy}
                onClick={() => run(() => api.respondPreAlert(pa.prealert_id, true, 'ACCEPTED'))}
              >
                <Check size={14} aria-hidden="true" /> Hospital accepts
              </button>
              <div className="adv-prealert__reject">
                <select value={rejectReason} onChange={(e) => setRejectReason(e.target.value)} aria-label="Reject reason">
                  {REJECT_REASONS.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
                <button
                  className="adv-btn adv-btn--bad"
                  disabled={busy}
                  onClick={() => run(() => api.respondPreAlert(pa.prealert_id, false, rejectReason))}
                >
                  <X size={14} aria-hidden="true" /> Hospital rejects
                </button>
              </div>
            </div>
          )}

          <table className="adv-table adv-attempts">
            <caption>
              Attempt log {pa.prealert_id}, queue of {pa.queue.length}
            </caption>
            <thead>
              <tr>
                <th>#</th>
                <th>Hospital</th>
                <th>Sent</th>
                <th>ETA</th>
                <th>Answer</th>
                <th>Reason code</th>
              </tr>
            </thead>
            <tbody>
              {pa.attempts.map((a, i) => (
                <tr key={i}>
                  <td>{i + 1}</td>
                  <td>{name(a.hospital_id)}</td>
                  <td>{fmtClock(a.sent_at)}</td>
                  <td>{fmtMin(a.eta_min)}</td>
                  <td>
                    {a.accepted == null ? (
                      <span className="adv-muted">waiting</span>
                    ) : a.accepted ? (
                      <span className="adv-ok">accepted {fmtClock(a.answered_at)}</span>
                    ) : (
                      <span className="adv-bad">rejected {fmtClock(a.answered_at)}</span>
                    )}
                  </td>
                  <td>
                    <code>{a.reason_code ?? ''}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <button className="adv-link" onClick={() => setPa(null)}>
            Start a new pre-alert
          </button>
        </>
      )}

      {error != null && <ApiErrorNotice error={error} />}
    </section>
  )
}
