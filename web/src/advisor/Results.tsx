import { useState } from 'react'
import { ArrowRightLeft, CheckCircle2, Clock } from 'lucide-react'
import { ProvenanceBadge } from './common'
import { SCORE_PARTS, excludedReason, fmtMin, fmtNum, humanize, scoreParts } from './format'
import type { DemoHour } from './format'
import type { ExcludedHospital, RankedHospital, Recommendation, ScoreBreakdown } from './types'

/** Nearest hospital by travel time among everything the advisor scored (ranked or excluded with components). */
function nearestScored(rec: Recommendation) {
  const all: { id: string; name: string; c: ScoreBreakdown; ex: ExcludedHospital | null; rank: number | null }[] = [
    ...rec.ranked.map((r, i) => ({ id: r.hospital_id, name: r.name, c: r.components, ex: null, rank: i + 1 })),
    ...rec.excluded.flatMap((x) => (x.components ? [{ id: x.hospital_id, name: x.name, c: x.components, ex: x, rank: null }] : [])),
  ]
  return all.length ? all.reduce((a, b) => (b.c.travel_min < a.c.travel_min ? b : a)) : null
}

export function DecisionCard({
  rec,
  hour,
  flip,
}: {
  rec: Recommendation
  hour: DemoHour
  flip: { at11: RankedHospital | undefined; at18: RankedHospital | undefined } | null
}) {
  const top = rec.ranked[0]
  const nearest = nearestScored(rec)
  const nearestIsTop = nearest && top && nearest.id === top.hospital_id
  const flips = flip && flip.at11 && flip.at18 && flip.at11.hospital_id !== flip.at18.hospital_id

  return (
    <section className="adv-decision" aria-label="Recommendation">
      {top ? (
        <>
          <p className="adv-decision__kicker">Take the patient to</p>
          <h2 className="adv-decision__name">{top.name}</h2>
          <p className="adv-decision__score">
            <strong>{fmtNum(top.score, 0)}</strong> min expected to definitive care
            <span className="adv-muted"> (travel {fmtMin(top.components.travel_min)})</span>
          </p>
        </>
      ) : (
        <>
          <p className="adv-decision__kicker">No hospital to recommend</p>
          <h2 className="adv-decision__name">{humanize(rec.reason_code)}</h2>
        </>
      )}

      {nearest && !nearestIsTop && (
        <p className="adv-decision__vs">
          <span>
            Nearest hospital: <strong>{nearest.name}</strong>
            {nearest.ex ? `, ${excludedReason(nearest.ex.reason)}` : nearest.rank ? `, ranked #${nearest.rank}` : ''}
          </span>
          <span className="adv-decision__vs-num">
            {fmtNum(nearest.c.total, 0)} min
            {top ? (
              <em>
                {' '}
                ({nearest.c.total - top.score >= 0 ? '+' : ''}
                {fmtNum(nearest.c.total - top.score, 0)})
              </em>
            ) : null}
          </span>
        </p>
      )}
      {nearestIsTop && <p className="adv-decision__vs adv-muted">The recommended hospital is also the nearest.</p>}

      <div className="adv-reason">
        <p>{rec.reason || 'No reason given.'}</p>
        <dl>
          <div>
            <dt>Care category</dt>
            <dd>{humanize(rec.care_category)}</dd>
          </div>
          <div>
            <dt>Route</dt>
            <dd>{rec.route ? humanize(rec.route) : 'none'}</dd>
          </div>
          <div>
            <dt>Reason code</dt>
            <dd>
              <code>{rec.reason_code}</code>
            </dd>
          </div>
          <div>
            <dt>Coverage weight λ</dt>
            <dd>{rec.lambda_used}</dd>
          </div>
        </dl>
      </div>

      {flip && flip.at11 && flip.at18 && (
        <div className={`adv-flip ${flips ? 'adv-flip--yes' : ''}`} role="status">
          {flips ? <ArrowRightLeft size={16} aria-hidden="true" /> : <CheckCircle2 size={16} aria-hidden="true" />}
          <div>
            <strong>{flips ? 'Top choice flips with traffic' : 'Same top choice at 11:00 and 18:00'}</strong>
            <div className="adv-flip__rows">
              {([11, 18] as const).map((h) => {
                const r = h === 11 ? flip.at11! : flip.at18!
                return (
                  <span key={h} className={h === hour ? 'is-current' : ''}>
                    <Clock size={12} aria-hidden="true" /> {h}:00 {r.name} ({fmtNum(r.score, 0)} min)
                  </span>
                )
              })}
            </div>
          </div>
        </div>
      )}
    </section>
  )
}

export function ScoreLegend() {
  return (
    <ul className="adv-score-legend" aria-label="Score components">
      {SCORE_PARTS.map((p, i) => (
        <li key={p.key}>
          <i className={`adv-sw adv-sw--${i + 1}`} aria-hidden="true" />
          {p.label}
        </li>
      ))}
    </ul>
  )
}

function ScoreBar({ c, max, faded }: { c: ScoreBreakdown; max: number; faded?: boolean }) {
  const parts = scoreParts(c)
  const total = parts.reduce((s, p) => s + p.v, 0)
  return (
    <div className={`adv-bar ${faded ? 'adv-bar--faded' : ''}`}>
      <div
        className="adv-bar__track"
        role="img"
        aria-label={parts.map((p) => `${p.label} ${p.v.toFixed(1)} min`).join(', ')}
      >
        <div className="adv-bar__fill" style={{ width: `${max > 0 ? (total / max) * 100 : 0}%` }}>
          {parts.map((p, i) =>
            p.v > 0 ? (
              <span
                key={p.key}
                className={`adv-bar__seg adv-sw--${i + 1}`}
                style={{ flexGrow: p.v }}
                title={`${p.label}: ${p.v.toFixed(1)} min`}
              />
            ) : null,
          )}
        </div>
      </div>
      <div className="adv-bar__values">
        {parts.map((p, i) => (
          <span key={p.key} title={p.label}>
            <i className={`adv-sw adv-sw--${i + 1}`} aria-hidden="true" />
            {p.short} {p.v.toFixed(1)}
          </span>
        ))}
      </div>
    </div>
  )
}

const SHOW_FIRST = 5

export function RankedList({ rec }: { rec: Recommendation }) {
  const [all, setAll] = useState(false)
  const max = Math.max(
    1,
    ...rec.ranked.map((r) => r.components.total),
    ...rec.excluded.flatMap((x) => (x.components ? [x.components.total] : [])),
  )
  const shown = all ? rec.ranked : rec.ranked.slice(0, SHOW_FIRST)
  if (!rec.ranked.length) return <p className="adv-muted">No capable hospital was ranked.</p>
  return (
    <>
      <ScoreLegend />
      <ol className="adv-ranked">
        {shown.map((r, i) => (
          <li key={r.hospital_id} className={i === 0 ? 'is-top' : ''}>
            <div className="adv-ranked__head">
              <span className="adv-rank" aria-label={`Rank ${i + 1}`}>
                {i + 1}
              </span>
              <span className="adv-ranked__name">{r.name}</span>
              <span className="adv-ranked__score">{fmtNum(r.score, 1)} min</span>
            </div>
            <ScoreBar c={r.components} max={max} />
            {r.components.p_cannot_treat > 0 && (
              <p className="adv-ranked__math">
                P(cannot treat) {r.components.p_cannot_treat.toFixed(2)} × transfer delay{' '}
                {fmtMin(r.components.transfer_delay_min)}
                {r.components.lam > 0 &&
                  `; λ ${r.components.lam} × coverage lost ${fmtMin(r.components.coverage_lost_min, 1)}`}
              </p>
            )}
            {r.notes.length > 0 && (
              <ul className="adv-notes">
                {r.notes.map((n, k) => (
                  <li key={k}>{n}</li>
                ))}
              </ul>
            )}
            <div className="adv-prov-list">
              {Object.entries(r.inputs_provenance).map(([k, p]) => (
                <ProvenanceBadge key={k} p={p} input={k} />
              ))}
            </div>
          </li>
        ))}
      </ol>
      {rec.ranked.length > SHOW_FIRST && (
        <button className="adv-link" onClick={() => setAll((v) => !v)}>
          {all ? 'Show top 5 only' : `Show all ${rec.ranked.length} ranked hospitals`}
        </button>
      )}
    </>
  )
}

export function ExcludedList({ rec }: { rec: Recommendation }) {
  const [all, setAll] = useState(false)
  if (!rec.excluded.length) return <p className="adv-muted">No hospital was excluded.</p>
  // scored ones first, nearest first, so "nearest, no cath lab: 68 min" leads
  const sorted = [...rec.excluded].sort(
    (a, b) => (a.components?.travel_min ?? Infinity) - (b.components?.travel_min ?? Infinity),
  )
  const shown = all ? sorted : sorted.slice(0, 6)
  return (
    <>
      <ul className="adv-excluded">
        {shown.map((x) => (
          <li key={x.hospital_id}>
            <span className="adv-excluded__name">{x.name}</span>
            <span className="adv-excluded__why">{excludedReason(x.reason)}</span>
            {x.components ? (
              <span className="adv-excluded__score" title="Scored as if taken there: P(cannot treat) = 1, so a transfer is added">
                {fmtNum(x.components.total, 0)} min
                <small>travel {fmtNum(x.components.travel_min, 0)}</small>
              </span>
            ) : (
              <span className="adv-excluded__score adv-muted">not scored</span>
            )}
          </li>
        ))}
      </ul>
      {sorted.length > 6 && (
        <button className="adv-link" onClick={() => setAll((v) => !v)}>
          {all ? 'Show fewer' : `Show all ${sorted.length} excluded`}
        </button>
      )}
    </>
  )
}
