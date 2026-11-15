import { useCallback, useEffect, useMemo, useState } from 'react'
import { Hospital as HospitalIcon, Loader2, Moon, Sun } from 'lucide-react'
import { API_BASE, api } from './api'
import { AdvisorMap } from './AdvisorMap'
import type { LatLon, PlaceMode } from './AdvisorMap'
import { AssessmentForm } from './AssessmentForm'
import type { FormState } from './AssessmentForm'
import { ApiErrorNotice, DataBanner, ProvenanceLegend, ProvenanceList } from './common'
import { whenAt } from './format'
import type { DemoHour } from './format'
import { PreAlertPanel } from './PreAlertPanel'
import { DecisionCard, ExcludedList, RankedList } from './Results'
import { SimResultsView } from './SimResults'
import type { Assessment, HealthResponse, HospitalsResponse, RecommendResponse } from './types'
import { crewTab, crewUrl } from './route'
import './advisor.css'

const INITIAL_FORM: FormState = {
  complaint: 'chest_pain',
  gcs: '15',
  sbp: '',
  rr: '',
  ecg: 'yes',
  strokeScore: '',
  minutesSinceContact: '10',
}
// Demo starting points (any click on the map moves them).
const INITIAL_SCENE: LatLon = { lat: 12.9767, lon: 77.5713 }
const INITIAL_BASE: LatLon = { lat: 12.925, lon: 77.5938 }

type Theme = 'dark' | 'light'
const THEME_KEY = 'advisor-theme'

function readTheme(): Theme {
  try {
    return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

const num = (s: string): number | undefined => {
  if (s.trim() === '') return undefined
  const n = Number(s)
  return Number.isFinite(n) ? n : undefined
}

function buildAssessment(case_id: string, f: FormState, scene: LatLon, base: LatLon, hour: number): Assessment {
  const vitals: Record<string, number> = {}
  for (const k of ['gcs', 'sbp', 'rr'] as const) {
    const v = num(f[k])
    if (v !== undefined) vitals[k] = v
  }
  return {
    case_id,
    scene_lat: scene.lat,
    scene_lon: scene.lon,
    base_lat: base.lat,
    base_lon: base.lon,
    complaint: f.complaint,
    vitals,
    minutes_since_first_contact: num(f.minutesSinceContact) ?? 0,
    ecg_stemi: f.ecg === 'yes' ? true : f.ecg === 'no' ? false : null,
    stroke_score: num(f.strokeScore) ?? null,
    when: whenAt(hour),
  }
}

type Recs = Partial<Record<DemoHour, RecommendResponse>>

export default function AdvisorView() {
  const [theme, setTheme] = useState<Theme>(readTheme)
  const [tab, setTab] = useState<'live' | 'sim'>(crewTab)
  const selectTab = (t: 'live' | 'sim') => {
    setTab(t)
    window.history.replaceState(null, '', crewUrl(t))
  }
  const [caseId] = useState(() => `web-${Math.random().toString(36).slice(2, 8)}`)

  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [hosp, setHosp] = useState<HospitalsResponse | null>(null)
  const [hospError, setHospError] = useState<unknown>(null)

  const [form, setForm] = useState<FormState>(INITIAL_FORM)
  const [scene, setScene] = useState<LatLon>(INITIAL_SCENE)
  const [base, setBase] = useState<LatLon>(INITIAL_BASE)
  const [mode, setMode] = useState<PlaceMode>('scene')
  const [hour, setHour] = useState<DemoHour>(11)

  const [recs, setRecs] = useState<Recs>({})
  const [recError, setRecError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)
  const [retry, setRetry] = useState(0) // bumps on 'Try again' to re-run the recommendation
  const [rev, setRev] = useState(0) // bumps per new recommendation; remounts the pre-alert panel

  useEffect(() => {
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      // storage unavailable: theme just won't persist
    }
  }, [theme])

  const loadRegistry = useCallback(() => {
    api.health().then(setHealth, () => setHealth(null))
    api.hospitals().then((h) => {
      setHosp(h)
      setHospError(null)
    }, (e) => {
      setHosp(null)
      setHospError(e)
    })
  }, [])
  useEffect(loadRegistry, [loadRegistry])

  // Re-recommend whenever the assessment changes. Both hours are requested so the traffic flip is visible;
  // the displayed hour is sent under the real case_id last, so /prealert uses the recommendation on screen.
  const payloadKey = JSON.stringify({ form, scene, base, hour })
  useEffect(() => {
    const ctrl = new AbortController()
    const other: DemoHour = hour === 11 ? 18 : 11
    const t = setTimeout(async () => {
      setLoading(true)
      try {
        const cmp = await api.recommend(buildAssessment(`${caseId}-cmp`, form, scene, base, other), ctrl.signal)
        const main = await api.recommend(buildAssessment(caseId, form, scene, base, hour), ctrl.signal)
        setRecs({ [other]: cmp, [hour]: main })
        setRecError(null)
        setRev((r) => r + 1)
      } catch (e) {
        if (ctrl.signal.aborted) return
        setRecs({}) // never keep showing a stale answer after a failure
        setRecError(e)
      } finally {
        if (!ctrl.signal.aborted) setLoading(false)
      }
    }, 250)
    return () => {
      clearTimeout(t)
      ctrl.abort()
    }
    // payloadKey captures form, scene, base and hour (objects are re-created on every edit)
  }, [payloadKey, caseId, retry]) // oxlint-disable-line react-hooks/exhaustive-deps

  const resp = recs[hour] ?? null
  const rec = resp?.recommendation ?? null

  const flip = useMemo(
    () =>
      recs[11] && recs[18]
        ? { at11: recs[11].recommendation.ranked[0], at18: recs[18].recommendation.ranked[0] }
        : null,
    [recs],
  )

  const names = useMemo(() => {
    const m: Record<string, string> = {}
    for (const h of hosp?.hospitals ?? []) m[h.hospital_id] = h.name
    for (const [id, h] of Object.entries(resp?.hospitals ?? {})) m[id] = h.name
    return m
  }, [hosp, resp])

  const banners = Array.from(
    new Set(
      (tab === 'live' ? [resp?.banner, hosp?.banner] : []).filter((b): b is string => typeof b === 'string' && !!b),
    ),
  )

  const apiDown = hospError != null && recError != null

  return (
    <div className="adv-root" data-theme={theme}>
      <header className="adv-header">
        <div className="adv-brand">
          <span className="adv-brand__mark" aria-hidden="true">
            <HospitalIcon size={20} />
          </span>
          <div>
            <h1>Destination Advisor</h1>
            <p>Which hospital gets this patient to definitive care fastest</p>
          </div>
        </div>
        <nav className="adv-tabs" aria-label="Advisor views">
          <button className={tab === 'live' ? 'is-on' : ''} aria-pressed={tab === 'live'} onClick={() => selectTab('live')}>
            Live case
          </button>
          <button className={tab === 'sim' ? 'is-on' : ''} aria-pressed={tab === 'sim'} onClick={() => selectTab('sim')}>
            Simulation results
          </button>
        </nav>
        <div className="adv-header__meta">
          <span className={`adv-api ${health?.ok ? 'is-up' : 'is-down'}`} title={API_BASE}>
            <i aria-hidden="true" />
            {health?.ok ? `API up, travel: ${health.travel_provider === 'osm_graph' ? 'road graph' : 'straight-line estimate'}` : 'API not connected'}
          </span>
          <button
            className="adv-icon-btn"
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
          >
            {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
          </button>
        </div>
      </header>

      {banners.map((b) => (
        <DataBanner key={b} text={b} />
      ))}

      {tab === 'sim' ? (
        <main className="adv-main adv-main--sim">
          <SimResultsView />
        </main>
      ) : apiDown ? (
        <main className="adv-main adv-main--sim">
          <ApiErrorNotice error={hospError} />
          <button
            className="adv-btn"
            onClick={() => {
              loadRegistry()
              setRetry((r) => r + 1)
            }}
          >
            Try again
          </button>
        </main>
      ) : (
        <main className="adv-main adv-live">
          <aside className="adv-col adv-col--form">
            <h2 className="adv-h">Assessment</h2>
            <AssessmentForm form={form} onChange={setForm} hour={hour} onHour={setHour} />
            <div className="adv-pins">
              <p>
                <i className="adv-dot adv-dot--scene" aria-hidden="true" /> Scene {scene.lat.toFixed(4)},{' '}
                {scene.lon.toFixed(4)}
              </p>
              <p>
                <i className="adv-dot adv-dot--base" aria-hidden="true" /> Base {base.lat.toFixed(4)}, {base.lon.toFixed(4)}
              </p>
              <p className="adv-muted adv-small">Click the map to move the scene; switch the map mode to move the base.</p>
            </div>
            {resp && (
              <div className="adv-meta-block">
                <h3 className="adv-h3">Inputs behind this recommendation</h3>
                <ProvenanceList items={resp.recommendation.inputs_provenance} />
                {resp.todo_params.length > 0 && (
                  <p className="adv-muted adv-small" title={resp.todo_params.join('\n')}>
                    {resp.todo_params.length} config parameter{resp.todo_params.length === 1 ? '' : 's'} still marked
                    “TODO: source”.
                  </p>
                )}
                <ProvenanceLegend />
              </div>
            )}
          </aside>

          <section className="adv-col adv-col--map" aria-label="Map">
            {hospError != null && <ApiErrorNotice error={hospError} />}
            <AdvisorMap
              hospitals={hosp?.hospitals ?? []}
              rec={rec}
              scene={scene}
              base={base}
              mode={mode}
              onModeChange={setMode}
              onPlace={(m, p) => (m === 'scene' ? setScene(p) : setBase(p))}
            />
            {loading && (
              <span className="adv-loading" role="status">
                <Loader2 size={14} className="adv-spin" aria-hidden="true" /> Updating
              </span>
            )}
          </section>

          <section className="adv-col adv-col--results" aria-label="Results" aria-busy={loading}>
            {recError != null && <ApiErrorNotice error={recError} />}
            {!rec && recError == null && <p className="adv-muted">Asking the advisor…</p>}
            {rec && (
              <>
                <DecisionCard rec={rec} hour={hour} flip={flip} />
                <h2 className="adv-h">Ranked hospitals</h2>
                <RankedList rec={rec} />
                <h2 className="adv-h">Excluded</h2>
                <ExcludedList rec={rec} />
                <h2 className="adv-h">Pre-alert</h2>
                <PreAlertPanel key={rev} rec={rec} names={names} />
              </>
            )}
          </section>
        </main>
      )}
    </div>
  )
}
