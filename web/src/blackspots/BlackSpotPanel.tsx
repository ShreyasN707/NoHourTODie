/** Black-spot sections for the Fleet plan dock (App.tsx), plus the stage table reused on the Results page. */
import { useState } from 'react'
import { CAUSE_RGB, CHANGE_RGB, causeLabel, css } from './palette'
import { STATUS_LABEL, effectiveMode, type Classified, type CompareMode, type SpotColor, type SpotFilter } from './layers'
import type { BlackSpotData, CauseKey, Cell, Cluster, FixKey, Scenario, Stage, Status } from './types'
import { CAUSE_KEYS, ROW, STATUSES } from './types'
import './blackspots.css'

const fmt = (n: number, d = 0) => n.toLocaleString('en-IN', { maximumFractionDigits: d, minimumFractionDigits: d })
const signed = (v: number, d = 0) => (Number(v.toFixed(d)) === 0 ? '0' : `${v > 0 ? '+' : '−'}${fmt(Math.abs(v), d)}`)
const STATUS_RGB: Record<Status, string> = { chronic: css(CHANGE_RGB.chronic), new: css(CHANGE_RGB.new), resolved: css(CHANGE_RGB.resolved) }

export function BlackSpotPanel(props: {
  data: BlackSpotData
  scenario: Scenario
  current: Scenario
  cls: Classified
  cells: Cell[]
  fix: FixKey | null
  onFix: (f: FixKey | null) => void
  color: SpotColor
  onColor: (c: SpotColor) => void
  mode: CompareMode
  onMode: (m: CompareMode) => void
  filter: SpotFilter
  onFilter: (f: SpotFilter) => void
  selectedCluster: number | null
  onSelectCluster: (c: Cluster | null) => void
  selectedHex: number | null
  onSelectHex: (i: number | null) => void
}) {
  const { data, scenario, cls, filter } = props
  const k = scenario.kpis
  const weeks = Math.round(data.windows.days / 7)
  const m = effectiveMode(scenario, props.mode)
  const labels = STATUS_LABEL[m]
  const toggle = <T,>(set: Set<T>, v: T) => {
    const n = new Set(set)
    if (n.has(v)) n.delete(v)
    else n.add(v)
    return n
  }
  const clusters = scenario.clusters.filter((c) => filter.causes.has(c.cause))
  const selCluster = scenario.clusters.find((c) => c.id === props.selectedCluster) ?? null

  return (
    <>
      <section className="sec">
        <div className="row between">
          <h2>Black spots</h2>
          <div className="seg bs-seg" role="group" aria-label="Colour black spots by">
            {(['cause', 'severity'] as SpotColor[]).map((c) => (
              <button key={c} aria-pressed={props.color === c} onClick={() => props.onColor(c)}>
                {c === 'cause' ? 'Cause' : 'Severity'}
              </button>
            ))}
          </div>
        </div>
        <p className="small">
          Hexagons (~0.7 km²) where help repeatedly took over {data.target_min} min: at least {data.criteria.min_late} late
          calls and, at 95% confidence (Wilson lower bound), at least {Math.round(data.criteria.min_late_share * 100)}% of calls
          late. {weeks} simulated weeks, {data.windows.test.start} to {data.windows.test.end}.
        </p>
        <dl className="stats bs-stats">
          <div>
            <dt>Black spots</dt>
            <dd>{fmt(k.black_spots)}</dd>
            <span className="muted">{fmt(k.black_late)} late calls in them</span>
          </div>
          <div>
            <dt>Reached in time</dt>
            <dd>{fmt(k.within_pct, 1)}%</dd>
            <span className="muted">all {fmt(k.calls)} calls</span>
          </div>
        </dl>

        {selectedHexCard(props, labels)}

        <h3 className="bs-h3">Compare with</h3>
        <div className="seg bs-seg bs-wide" role="group" aria-label="Compare with">
          <button aria-pressed={m === 'before'} onClick={() => props.onMode('before')}>
            Previous {weeks} weeks
          </button>
          <button aria-pressed={m === 'stage'} disabled={!props.fix} title={props.fix ? '' : 'Pick a stage below first'} onClick={() => props.onMode('stage')}>
            Stage before
          </button>
        </div>
        <p className="muted small">Against {cls.refLabel}. Untick to hide on the map.</p>
        <div className="bs-filters" role="group" aria-label="Show black spots by status">
          {STATUSES.map((s) => (
            <label key={s} className={filter.statuses.has(s) ? 'on' : ''}>
              <input type="checkbox" checked={filter.statuses.has(s)} onChange={() => props.onFilter({ ...filter, statuses: toggle(filter.statuses, s) })} />
              <i className={`bs-sw ${s}`} style={{ borderColor: STATUS_RGB[s] }} />
              {labels[s]} <b>{cls.counts[s]}</b>
            </label>
          ))}
        </div>
        <div className="bs-filters" role="group" aria-label="Show black spots by cause">
          {CAUSE_KEYS.map((c) => (
            <label key={c} className={filter.causes.has(c) ? 'on' : ''}>
              <input type="checkbox" checked={filter.causes.has(c)} onChange={() => props.onFilter({ ...filter, causes: toggle(filter.causes, c) })} />
              <i className="bs-sw" style={{ background: css(CAUSE_RGB[c]), borderColor: css(CAUSE_RGB[c]) }} />
              {causeLabel(c)} <b>{cls.causeCounts[c]}</b>
            </label>
          ))}
        </div>
      </section>

      <section className="sec">
        <h2>Place ambulances to fix them</h2>
        <p className="small muted">
          Each stage builds on the one before. The map shows that stage&apos;s ambulances for the chosen block. Fixes are chosen
          on the {weeks} weeks before; the numbers are from weeks they never saw.
        </p>
        <StageTable data={data} selected={props.fix ?? 'current'} onPick={(key) => props.onFix(key === 'current' ? null : (key as FixKey))} />
        <p className="muted small">
          {scenario.fleet.ambulances} ambulances on the road ({scenario.fleet.als} Advanced, {scenario.fleet.bls} Basic)
          {scenario.fleet.bikes ? `, ${scenario.fleet.bikes} bikes` : ''}
          {scenario.posts_used ? `; ${scenario.posts_used.length} of ${data.posts.length} proposed posts get an ambulance` : ''}.
        </p>
      </section>

      <Causes s={scenario} data={data} />

      <section className="sec">
        <h2>Worst black spots</h2>
        {selCluster && <ClusterDetail c={selCluster} onClose={() => props.onSelectCluster(null)} />}
        <ClusterList clusters={clusters} selected={props.selectedCluster} onSelect={props.onSelectCluster} />
      </section>

      <Method data={data} />
    </>
  )
}

function selectedHexCard(p: Parameters<typeof BlackSpotPanel>[0], labels: Record<Status, string>) {
  if (p.selectedHex === null) return null
  const i = p.selectedHex
  const r = p.cls.rows.get(i)
  const st = p.cls.status.get(i)
  const cell = p.cells[i]
  const cluster = p.scenario.clusters.find((c) => c.cells.includes(i))
  const counts = r ? CAUSE_KEYS.map((_, j) => r[ROW.nCause + j]) : [0, 0, 0]
  const tot = Math.max(1, counts.reduce((a, b) => a + b, 0))
  return (
    <div className="bs-detail">
      <button className="x" aria-label="Close" onClick={() => p.onSelectHex(null)}>
        ×
      </button>
      <h3>{cell?.zone ?? 'Hexagon'}</h3>
      <p className="small">
        {st ? (
          <b style={{ color: STATUS_RGB[st] }}>{st === 'resolved' ? 'Resolved black spot' : `Black spot · ${labels[st].toLowerCase()}`}</b>
        ) : (
          'Not a black spot'
        )}
        {cluster ? ` · part of black spot #${cluster.id}` : ''}
      </p>
      {r ? (
        <>
          <dl className="bs-facts">
            <div><dt>Total calls</dt><dd>{r[ROW.calls]}</dd></div>
            <div><dt>Late calls</dt><dd>{r[ROW.late]}</dd></div>
            <div><dt>Late</dt><dd>{Math.round((100 * r[ROW.late]) / Math.max(1, r[ROW.calls]))}%</dd></div>
            <div><dt>Confidence</dt><dd title="Wilson lower bound, one-sided 95%">≥ {Math.round(r[ROW.lo] * 100)}%</dd></div>
          </dl>
          <p className="muted small">
            Confidence: with 95% confidence, at least {Math.round(r[ROW.lo] * 100)}% of calls here are late (black spot from{' '}
            {Math.round(p.data.criteria.min_late_share * 100)}%). Slowest 1 in 10 waited over {r[ROW.p90].toFixed(1)} min.
          </p>
          {r[ROW.late] > 0 && (
            <>
              <div className="bs-bar small" role="img" aria-label={CAUSE_KEYS.map((c, j) => `${causeLabel(c)} ${counts[j]}`).join(', ')}>
                {CAUSE_KEYS.map((c, j) => <span key={c} style={{ width: `${(100 * counts[j]) / tot}%`, background: css(CAUSE_RGB[c]) }} />)}
              </div>
              <ul className="bs-causelist">
                {CAUSE_KEYS.map((c, j) => (
                  <li key={c} className={r[ROW.cause] === j ? 'main' : ''}>
                    <i style={{ background: css(CAUSE_RGB[c]) }} />
                    {causeLabel(c)} <b>{counts[j]}</b>
                    {r[ROW.cause] === j && <span> · main cause: {p.data.causes[j].fix.toLowerCase()}</span>}
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      ) : (
        <p className="small">No calls here in these weeks.</p>
      )}
    </div>
  )
}

/** Before/after for every stage: each row against the stage before it. Clickable when onPick is given. */
export function StageTable({ data, selected, onPick }: { data: BlackSpotData; selected?: string; onPick?: (key: string) => void }) {
  const by = new Map(data.stages.map((s) => [s.key, s]))
  const step = (s: Stage) => (s.key === 'current' ? 'GH' : s.key === 'targeted' ? '1' : s.key === 'posts' ? '2' : '3')
  return (
    <table className="bs-table bs-stages">
      <thead>
        <tr>
          <th scope="col">Stage</th>
          <th scope="col">Spots</th>
          <th scope="col" title="Black spots of the stage before that are gone">Resolved</th>
          <th scope="col" title="Black spots that were not there in the stage before">New</th>
          <th scope="col">In time</th>
        </tr>
      </thead>
      <tbody>
        {data.stages.map((s) => {
          const prev = s.prev ? by.get(s.prev) : null
          const d = prev ? s.kpis.black_spots - prev.kpis.black_spots : 0
          const dt = prev ? s.kpis.within_pct - prev.kpis.within_pct : 0
          return (
            <tr key={s.key} className={selected === s.key ? 'on' : ''} onClick={onPick ? () => onPick(s.key) : undefined}
              tabIndex={onPick ? 0 : undefined} onKeyDown={onPick ? (e) => (e.key === 'Enter' || e.key === ' ') && onPick(s.key) : undefined}
              aria-selected={onPick ? selected === s.key : undefined}>
              <th scope="row">
                <div className="bs-stage-name">
                  <em className={`bs-step ${s.key === 'current' ? 'ref' : ''}`}>{step(s)}</em>
                  <span>
                    {s.key === 'current' ? 'GoldenHour plan' : s.label}
                    {prev && <small>vs {prev.key === 'current' ? 'GoldenHour plan' : `stage ${step(prev)}`}</small>}
                  </span>
                </div>
              </th>
              <td>
                {s.kpis.black_spots}
                {prev && <small className={d < 0 ? 'up' : d > 0 ? 'down' : 'flat'}> {signed(d)}</small>}
              </td>
              <td className="up">{s.vs_prev ? s.vs_prev.resolved : '–'}</td>
              <td className={s.vs_prev?.new ? 'down' : ''}>{s.vs_prev ? s.vs_prev.new : '–'}</td>
              <td>
                {s.kpis.within_pct.toFixed(1)}%
                {prev && <small className={dt > 0.05 ? 'up' : dt < -0.05 ? 'down' : 'flat'}> {signed(dt, 1)}</small>}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function Causes({ s, data }: { s: Scenario; data: BlackSpotData }) {
  const total = Math.max(1, CAUSE_KEYS.reduce((a, c) => a + s.late_by_cause[c], 0))
  const spots = CAUSE_KEYS.map((_, i) => s.cells.filter((r) => r[ROW.sev] >= 0 && r[ROW.cause] === i).length)
  return (
    <section className="sec">
      <h2>Why the ambulance was late</h2>
      <div className="bs-bar" role="img" aria-label={CAUSE_KEYS.map((c) => `${causeLabel(c)}: ${s.late_by_cause[c]} late calls`).join(', ')}>
        {CAUSE_KEYS.map((c) => <span key={c} style={{ width: `${(100 * s.late_by_cause[c]) / total}%`, background: css(CAUSE_RGB[c]) }} />)}
      </div>
      <table className="bs-table">
        <thead>
          <tr>
            <th scope="col">Cause, and the fix</th>
            <th scope="col">Late calls</th>
            <th scope="col">Spots</th>
          </tr>
        </thead>
        <tbody>
          {data.causes.map((c, i) => (
            <tr key={c.key} title={c.detail}>
              <th scope="row">
                <i style={{ background: css(CAUSE_RGB[c.key as CauseKey]) }} />
                <span>
                  {c.label}
                  <small>{c.fix}</small>
                </span>
              </th>
              <td>
                {fmt(s.late_by_cause[c.key])} <span className="muted">{Math.round((100 * s.late_by_cause[c.key]) / total)}%</span>
              </td>
              <td>{spots[i]}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        Every late call gets one cause from road travel times: could any feasible site reach it in time, was one staffed, was that
        ambulance free. A hexagon&apos;s colour is the cause of most of its late calls.
      </p>
    </section>
  )
}

function ClusterList({ clusters, selected, onSelect }: { clusters: Cluster[]; selected: number | null; onSelect: (c: Cluster) => void }) {
  const [all, setAll] = useState(false)
  if (!clusters.length) return <p className="muted small">No black spots match.</p>
  return (
    <>
      <p className="muted small">Touching black-spot hexagons grouped, by total minutes late. Click to zoom.</p>
      <ul className="bs-clusters">
        {(all ? clusters : clusters.slice(0, 8)).map((c) => (
          <li key={c.id}>
            <button className={selected === c.id ? 'on' : ''} aria-pressed={selected === c.id} onClick={() => onSelect(c)}>
              <span className="bs-cl-id">#{c.id}</span>
              <span className="bs-cl-main">
                <b>
                  {c.zones[0]}
                  {c.zones.length > 1 && <span className="muted"> +{c.zones.length - 1}</span>}
                </b>
                <span className="muted">
                  {c.late} of {c.calls} late · {c.cells.length} hex
                </span>
                <span className="bs-cl-fix">
                  <i style={{ background: css(CAUSE_RGB[c.cause]) }} />
                  {c.fix}
                </span>
              </span>
              <span className="bs-cl-min">
                {fmt(c.excess_min)}
                <small>min late</small>
              </span>
            </button>
          </li>
        ))}
      </ul>
      {clusters.length > 8 && (
        <button className="btn small bs-more" onClick={() => setAll((v) => !v)}>
          {all ? 'Show the worst 8' : `Show all ${clusters.length}`}
        </button>
      )}
    </>
  )
}

function ClusterDetail({ c, onClose }: { c: Cluster; onClose: () => void }) {
  const tot = Math.max(1, CAUSE_KEYS.reduce((a, k) => a + c.causes[k], 0))
  return (
    <div className="bs-detail">
      <button className="x" aria-label="Close" onClick={onClose}>
        ×
      </button>
      <h3>
        Black spot #{c.id}: {c.zones.join(', ')}
      </h3>
      <p>
        {c.late} of {c.calls} calls late ({Math.round(c.late_pct)}%), by {c.mean_late_by_min} min on average. The slowest 1 in 10
        waited over {c.worst_p90_min} min. Severity: {c.severity}.
      </p>
      <div className="bs-bar small" role="img" aria-label={CAUSE_KEYS.map((k) => `${causeLabel(k)} ${c.causes[k]}`).join(', ')}>
        {CAUSE_KEYS.map((k) => <span key={k} style={{ width: `${(100 * c.causes[k]) / tot}%`, background: css(CAUSE_RGB[k]) }} />)}
      </div>
      <p className="small">
        {CAUSE_KEYS.filter((k) => c.causes[k] > 0)
          .map((k) => `${causeLabel(k)} ${c.causes[k]}`)
          .join(' · ')}
      </p>
      <p>
        <b>Fix: {c.fix.toLowerCase()}.</b>
      </p>
    </div>
  )
}

function Method({ data }: { data: BlackSpotData }) {
  const c = data.criteria
  const weeks = Math.round(data.windows.days / 7)
  return (
    <section className="sec">
      <details className="bs-method">
        <summary>How black spots are found and fixed</summary>
        <ol>
          <li>
            <b>Find.</b> {weeks} weeks of calls are replayed through the ambulance simulation: nearest free unit, road drive
            times, traffic by hour, units busy on earlier jobs. Per hexagon: total calls n, late calls k. Black spot when k ≥{' '}
            {c.min_late} and the one-sided 95% Wilson lower bound of k/n is at least {c.min_late_share}.
          </li>
          <li>
            <b>Chronic, new, resolved.</b> The same plan&apos;s previous {weeks} weeks: black in both (chronic, dark outline), now only
            (new, magenta), before only (resolved, green). For a stage, you can compare with the stage before instead.
          </li>
          <li>
            <b>Cause.</b> For each late call the replay records the drive from the nearest staffed site and from the best feasible
            site of any kind: out of reach, no unit planned nearby, or nearby unit busy.
          </li>
          <li>
            <b>Stage 1, re-place.</b> The same MEXCLP optimiser (HiGHS) re-plans the same fleet with black-spot hexagons weighted
            × (1 + {c.alpha} × late-share lower bound), capped at {c.max_weight}×, for {c.rounds} round(s). The round with the
            fewest total minutes late on the planning weeks is kept.
          </li>
          <li>
            <b>Stage 2, add posts.</b> Candidates are road points at {c.candidate_posts} out-of-reach black spots, with drive
            times from Dijkstra on the OpenStreetMap road graph. Greedy maximum marginal coverage keeps up to {c.max_new_posts}{' '}
            posts, each reaching at least {c.min_post_gain} more previously late calls in time. The optimiser then decides
            which get an ambulance.
          </li>
          <li>
            <b>Stage 3, add ambulances:</b> {data.fleet_curve.map((f) => `+${f.extra_pct}%`).join(' and ')}, each re-planned and
            re-simulated.
          </li>
        </ol>
        <p className="small">
          Planning weeks: {data.windows.tune.start} to {data.windows.tune.end}. Shown weeks: {data.windows.test.start} to{' '}
          {data.windows.test.end}. Calls are modelled (108 logs are not public), so these are simulation results.
        </p>
        <table className="bs-table">
          <caption>Stage 1 feedback rounds</caption>
          <thead>
            <tr>
              <th scope="col">Round</th>
              <th scope="col">Min late, planning</th>
              <th scope="col">Spots, shown</th>
              <th scope="col">In time</th>
            </tr>
          </thead>
          <tbody>
            {data.iterations.map((it) => (
              <tr key={it.round} className={it.selected ? 'on' : ''}>
                <th scope="row">
                  {it.label}
                  {it.selected ? ' (kept)' : ''}
                </th>
                <td>{fmt(it.tune.excess_min)}</td>
                <td>{it.test.black_spots}</td>
                <td>{it.test.within_pct.toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data.posts.length > 0 && (
          <p className="small">
            <b>Proposed posts</b> (cyan pins) are road points at hexagon centres, not buildings; a pilot would use a fire station,
            health centre or fuel station nearby: {data.posts.map((p, i) => `P${i + 1} ${p.zone} (${p.late_calls_in_reach})`).join(', ')}.
          </p>
        )}
        <p className="muted small">
          Made by pipeline/blackspots.py; thresholds in pipeline/config.py (BLACKSPOT_*).
        </p>
      </details>
    </section>
  )
}
