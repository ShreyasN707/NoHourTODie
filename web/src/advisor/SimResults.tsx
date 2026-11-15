import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { api } from './api'
import { ApiErrorNotice, DataBanner, ProvenanceLegend, ProvenanceList } from './common'
import { fmtNum, fmtPct, humanize } from './format'
import type { Metrics, SimResults, Stat } from './types'

type Load = { state: 'loading' } | { state: 'error'; error: unknown } | { state: 'ok'; data: SimResults }

export function SimResultsView() {
  const [load, setLoad] = useState<Load>({ state: 'loading' })

  const fetchIt = useCallback(() => {
    api.simResults().then(
      (data) => setLoad({ state: 'ok', data }),
      (error) => setLoad({ state: 'error', error }),
    )
  }, [])
  const reload = () => {
    setLoad({ state: 'loading' })
    fetchIt()
  }

  useEffect(fetchIt, [fetchIt])

  return (
    <div className="adv-sim">
      <div className="adv-sim__head">
        <div>
          <h2>Simulation: nearest-hospital routing vs the advisor</h2>
          <p className="adv-muted">
            Replays the same incidents under both policies (SimPy). Lower time to treatment is better.
          </p>
        </div>
        <button className="adv-btn" onClick={reload} disabled={load.state === 'loading'}>
          <RefreshCw size={14} aria-hidden="true" /> Reload
        </button>
      </div>

      {load.state === 'loading' && <p className="adv-muted">Loading simulation results…</p>}
      {load.state === 'error' && (
        <ApiErrorNotice
          error={load.error}
          notFound={
            <>
              <strong>Simulation not run yet:</strong> run <code>make advisor-sim</code>, then reload.
            </>
          }
        />
      )}
      {load.state === 'ok' && <SimBody r={load.data} />}
    </div>
  )
}

function SimBody({ r }: { r: SimResults }) {
  const { nearest, advisor } = r.base.policies
  return (
    <>
      {r.banner && <DataBanner text={r.banner} />}
      <p className="adv-sim__meta">
        {r.window.n_incidents.toLocaleString()} incidents, {r.window.start.replace('T', ' ')} to{' '}
        {r.window.end.replace('T', ' ')}. Seed {r.seed}. Generated {r.generated_at.replace('T', ' ')}. Base scenario:{' '}
        {r.base.name}.
      </p>

      <div className="adv-sim__grid">
        <section className="adv-card">
          <h3>Time to definitive treatment, by care category</h3>
          <TttChart nearest={nearest} advisor={advisor} />
        </section>

        <section className="adv-card">
          <h3>Operational metrics</h3>
          <MetricsTable nearest={nearest} advisor={advisor} />
        </section>

        <section className="adv-card adv-sim__wide">
          <h3>Sensitivity: does the advisor still win when assumptions change?</h3>
          <p className="adv-muted">
            Change in mean time to treatment for critical patients, advisor minus nearest. Left of zero: the advisor is
            faster.
          </p>
          <SensitivityTable rows={r.sensitivity} />
        </section>

        <section className="adv-card adv-sim__wide">
          <h3>Where the inputs come from</h3>
          <ProvenanceList items={r.inputs_provenance} />
          <ProvenanceLegend />
        </section>
      </div>
    </>
  )
}

function SeriesLegend() {
  return (
    <ul className="adv-series-legend" aria-label="Legend">
      <li>
        <i className="adv-sw adv-sw--nearest" aria-hidden="true" /> Nearest hospital
      </li>
      <li>
        <i className="adv-sw adv-sw--advisor" aria-hidden="true" /> Advisor
      </li>
      <li>
        <i className="adv-p90" aria-hidden="true" /> 90th percentile
      </li>
    </ul>
  )
}

/** Paired horizontal bars (mean) with a p90 tick, one shared minute axis. */
function TttChart({ nearest, advisor }: { nearest: Metrics; advisor: Metrics }) {
  const cats = Array.from(
    new Set([...Object.keys(nearest.time_to_treatment.by_category), ...Object.keys(advisor.time_to_treatment.by_category)]),
  ).sort()
  const rows: { key: string; label: string; n: Stat | undefined; a: Stat | undefined }[] = [
    { key: '__overall', label: 'All patients', n: nearest.time_to_treatment.overall, a: advisor.time_to_treatment.overall },
    ...cats.map((c) => ({
      key: c,
      label: humanize(c),
      n: nearest.time_to_treatment.by_category[c],
      a: advisor.time_to_treatment.by_category[c],
    })),
  ]
  const max = Math.max(1, ...rows.flatMap((r) => [r.n?.p90 ?? 0, r.a?.p90 ?? 0, r.n?.mean ?? 0, r.a?.mean ?? 0]))
  const ticks = niceTicks(max)
  const axisMax = ticks[ticks.length - 1]
  const pct = (v: number) => `${(v / axisMax) * 100}%`

  const bar = (s: Stat | undefined, who: 'nearest' | 'advisor') =>
    s && s.mean != null ? (
      <div className="adv-ttt__bar-row">
        <div className="adv-ttt__plot">
          <span
            className={`adv-ttt__bar adv-sw--${who}`}
            style={{ width: pct(s.mean) }}
            title={`${who === 'nearest' ? 'Nearest' : 'Advisor'}: mean ${fmtNum(s.mean)} min, p50 ${fmtNum(s.p50)}, p90 ${fmtNum(s.p90)}, n ${s.n}`}
          />
          {s.p90 != null && <span className="adv-ttt__p90" style={{ left: pct(s.p90) }} title={`p90 ${fmtNum(s.p90)} min`} />}
        </div>
        <span className="adv-ttt__val">
          {fmtNum(s.mean, 0)}
          <small> / {fmtNum(s.p90, 0)}</small>
        </span>
      </div>
    ) : (
      <div className="adv-ttt__bar-row">
        <div className="adv-ttt__plot" />
        <span className="adv-ttt__val adv-muted">n/a</span>
      </div>
    )

  return (
    <div className="adv-ttt">
      <SeriesLegend />
      <div className="adv-ttt__rows">
        {rows.map((r) => (
          <div key={r.key} className={`adv-ttt__row ${r.key === '__overall' ? 'is-overall' : ''}`}>
            <div className="adv-ttt__label">
              {r.label}
              <small>n {r.a?.n ?? r.n?.n ?? 0}</small>
            </div>
            <div className="adv-ttt__bars">
              {bar(r.n, 'nearest')}
              {bar(r.a, 'advisor')}
            </div>
          </div>
        ))}
        <div className="adv-ttt__row adv-ttt__axis" aria-hidden="true">
          <div />
          <div className="adv-ttt__bar-row">
            <div className="adv-ttt__plot">
              {ticks.map((t) => (
                <span key={t} style={{ left: pct(t) }}>
                  {t}
                </span>
              ))}
            </div>
            <span className="adv-ttt__val" />
          </div>
        </div>
      </div>
      <p className="adv-muted adv-small">Values: mean / 90th percentile, minutes.</p>
    </div>
  )
}

function niceTicks(max: number): number[] {
  const raw = max / 4
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw
  const out = []
  for (let t = 0; t < max + step; t += step) out.push(Math.round(t * 100) / 100)
  return out
}

function MetricsTable({ nearest, advisor }: { nearest: Metrics; advisor: Metrics }) {
  type Row = { label: string; n: number; a: number; fmt: (x: number) => string; lowerBetter: boolean }
  const rows: Row[] = [
    {
      label: 'Mean time to treatment',
      n: nearest.time_to_treatment.overall.mean ?? NaN,
      a: advisor.time_to_treatment.overall.mean ?? NaN,
      fmt: (x) => `${fmtNum(x)} min`,
      lowerBetter: true,
    },
    {
      label: 'Secondary transfer rate',
      n: nearest.secondary_transfer_rate,
      a: advisor.secondary_transfer_rate,
      fmt: (x) => fmtPct(x),
      lowerBetter: true,
    },
    {
      label: 'Critical patients: capable hospital on first attempt',
      n: nearest.critical_capable_first_attempt_share,
      a: advisor.critical_capable_first_attempt_share,
      fmt: (x) => fmtPct(x),
      lowerBetter: false,
    },
    {
      label: 'Ambulance hours used',
      n: nearest.ambulance_hours,
      a: advisor.ambulance_hours,
      fmt: (x) => fmtNum(x, 0),
      lowerBetter: true,
    },
    { label: 'Secondary transfers', n: nearest.secondary_transfers, a: advisor.secondary_transfers, fmt: String, lowerBetter: true },
    { label: 'Pre-alert rejections', n: nearest.prealert_rejects, a: advisor.prealert_rejects, fmt: String, lowerBetter: true },
    { label: 'Fallbacks to next hospital', n: nearest.fallbacks, a: advisor.fallbacks, fmt: String, lowerBetter: true },
  ]
  return (
    <table className="adv-table adv-metrics">
      <thead>
        <tr>
          <th>Metric</th>
          <th>
            <i className="adv-sw adv-sw--nearest" aria-hidden="true" /> Nearest
          </th>
          <th>
            <i className="adv-sw adv-sw--advisor" aria-hidden="true" /> Advisor
          </th>
          <th>Which is better</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const diff = r.a - r.n
          const better = Number.isNaN(diff) || diff === 0 ? 'same' : (diff < 0) === r.lowerBetter ? 'advisor' : 'nearest'
          return (
            <tr key={r.label}>
              <th scope="row">{r.label}</th>
              <td>{r.fmt(r.n)}</td>
              <td>{r.fmt(r.a)}</td>
              <td className={better === 'advisor' ? 'adv-ok' : better === 'nearest' ? 'adv-bad' : 'adv-muted'}>
                {better === 'same' ? 'no difference' : better === 'advisor' ? 'Advisor' : 'Nearest'}
              </td>
            </tr>
          )
        })}
      </tbody>
      <tfoot>
        <tr>
          <td colSpan={4} className="adv-muted adv-small">
            n = {nearest.n} (nearest), {advisor.n} (advisor). Critical = care categories with λ = 0, excluding arrest/airway.
          </td>
        </tr>
      </tfoot>
    </table>
  )
}

function SensitivityTable({ rows }: { rows: SimResults['sensitivity'] }) {
  if (!rows.length) return <p className="adv-muted">No sensitivity rows in this run.</p>
  const m = Math.max(1, ...rows.map((r) => Math.abs(r.delta_mean_ttt_critical)))
  return (
    <table className="adv-table adv-sens">
      <thead>
        <tr>
          <th>Scenario</th>
          <th>Nearest</th>
          <th>Advisor</th>
          <th className="adv-sens__chart-h">Advisor minus nearest (min)</th>
          <th>Result</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => {
          const d = r.delta_mean_ttt_critical
          const w = (Math.abs(d) / m) * 50
          return (
            <tr key={i} className={r.advisor_better ? '' : 'is-worse'}>
              <th scope="row">
                {r.label}
                <small>
                  {r.param} = {JSON.stringify(r.value)}
                </small>
              </th>
              <td>{fmtNum(r.nearest_mean_ttt_critical)}</td>
              <td>{fmtNum(r.advisor_mean_ttt_critical)}</td>
              <td className="adv-sens__cell">
                <div className="adv-sens__plot" title={`${d >= 0 ? '+' : ''}${fmtNum(d)} min`}>
                  <span className="adv-sens__zero" />
                  <span
                    className={`adv-sens__bar ${d < 0 ? 'is-better' : 'is-worse'}`}
                    style={d < 0 ? { right: '50%', width: `${w}%` } : { left: '50%', width: `${w}%` }}
                  />
                </div>
                <span className="adv-sens__num">
                  {d >= 0 ? '+' : ''}
                  {fmtNum(d)}
                </span>
              </td>
              <td>{r.advisor_better ? <span className="adv-ok">Advisor better</span> : <strong className="adv-bad">Advisor not better</strong>}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
