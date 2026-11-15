import { useEffect, useMemo, useRef, useState } from 'react'
import DeckGL from '@deck.gl/react'
import { GeoJsonLayer, IconLayer, PathLayer, TextLayer } from '@deck.gl/layers'
import { H3HexagonLayer } from '@deck.gl/geo-layers'
import { PathStyleExtension } from '@deck.gl/extensions'
import { FlyToInterpolator, WebMercatorViewport } from '@deck.gl/core'
import MapGL from 'react-map-gl/maplibre'
import 'maplibre-gl/dist/maplibre-gl.css'
import { icon, signUrl, type SignId } from './signs'
import './rural.css'

// ----------------------------------------------------------------------------- data (pipeline/rural.py)
type LngLat = [number, number]
type LegKind = 'unit' | 'hospital' | 'transfer' | 'rejected' | 'backup'
interface Leg {
  kind: LegKind
  label: string
  minutes?: number
  km?: number
  coords?: LngLat[]
  no_signal?: LngLat[][]
  no_signal_km?: number
  blocked?: boolean
}
interface CaseRun {
  legs: Leg[]
  [k: string]: unknown
}
type ScenarioId = 'dry' | 'monsoon' | 'shiradi' | 'both'
type CaseId = 'crash_ghat' | 'chest_pain' | 'icu_refusal' | 'estate' | 'standby'
interface Kpi {
  within_10: number
  within_20: number
  within_30: number
  median: number
}
interface Hospital {
  id: string
  name: string
  lat: number
  lon: number
  tier: 'hub' | 'taluk'
  caps: string[]
}
interface Unit {
  id: string
  lat: number
  lon: number
  site: string
  als: boolean
  neighbour?: boolean
}
interface RuralData {
  region: string
  target_min: number
  population: number
  calls_per_day: number
  fleet: number
  scenarios: Record<ScenarioId, { label: string; weight: number }>
  area: GeoJSON.Geometry
  cells: { h: string; pop: number; lam: number }[]
  ghats: Record<string, { name: string; lines: LngLat[][]; landslide: LngLat }>
  hairpins: LngLat[]
  hospitals: Hospital[]
  units: Unit[]
  plans: Record<'dry' | 'robust', { sites: { lat: number; lon: number; name: string; n: number }[]; kpis: Record<ScenarioId, Kpi> }>
  cases: Record<Exclude<CaseId, 'standby'>, Record<ScenarioId, CaseRun>>
  cutoff: Record<'shiradi' | 'both', string[]>
  estate_name: string
  assumptions: [string, string][]
}

// ----------------------------------------------------------------------------- look
const MAP_STYLE = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json'
const HOME = { longitude: 75.78, latitude: 12.9, zoom: 9.4, pitch: 0, bearing: 0 }
type RGBA = [number, number, number, number]
const LEG_STYLE: Record<LegKind, { color: RGBA; width: number; dash?: [number, number] }> = {
  unit: { color: [214, 51, 74, 255], width: 6 }, // --critical
  hospital: { color: [30, 156, 98, 255], width: 6 }, // --ok
  transfer: { color: [124, 58, 237, 255], width: 5 },
  rejected: { color: [214, 51, 74, 210], width: 4, dash: [3, 2] },
  backup: { color: [91, 102, 117, 200], width: 3, dash: [2, 2] }, // --muted
}
const LEG_NAME: Record<LegKind, string> = {
  unit: 'Ambulance to patient',
  hospital: 'Patient to hospital',
  transfer: 'Onward transfer',
  rejected: 'Route we avoid',
  backup: 'Fallback route',
}

const SCENARIO_ORDER: ScenarioId[] = ['dry', 'monsoon', 'shiradi', 'both']
const SCENARIO_SHORT: Record<ScenarioId, string> = {
  dry: 'Dry season',
  monsoon: 'Monsoon',
  shiradi: 'Shiradi landslide',
  both: 'Both ghats shut',
}
const CASES: { id: CaseId; title: string }[] = [
  { id: 'crash_ghat', title: 'Crash on the ghat' },
  { id: 'chest_pain', title: 'Heart attack in a village' },
  { id: 'icu_refusal', title: 'Hospital has no ICU bed' },
  { id: 'estate', title: 'Patient on a coffee estate' },
  { id: 'standby', title: 'Where ambulances wait' },
]

const fmt = (m?: number | null) => (m == null ? '–' : `${Math.round(m)} min`)
const mid = (c: LngLat[], f = 0.5) => c[Math.min(c.length - 1, Math.floor(c.length * f))]
// label position along each leg, staggered so labels of legs sharing a road do not overlap
const LABEL_AT: Record<LegKind, number> = { unit: 0.35, hospital: 0.7, transfer: 0.5, rejected: 0.45, backup: 0.6 }

// ----------------------------------------------------------------------------- component
export default function RuralView() {
  const [data, setData] = useState<RuralData | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [scenario, setScenario] = useState<ScenarioId>('dry')
  const [caseId, setCaseId] = useState<CaseId>('crash_ghat')
  const [view, setView] = useState<any>(HOME)
  const fitted = useRef(false)

  useEffect(() => {
    fetch('../data/rural_skp.json') // works at /ruraldemo, /ruraldemo/ and dist/ruraldemo/
      .then((r) => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then(setData)
      .catch((e) => setErr(`Could not load rural data (${e}). Run: make rural`))
  }, [])

  const run: CaseRun | null = data && caseId !== 'standby' ? data.cases[caseId][scenario] : null
  const monsoon = scenario !== 'dry'
  const shut = scenario === 'shiradi' ? ['shiradi'] : scenario === 'both' ? ['shiradi', 'charmadi'] : []

  // fit the map to the active case
  useEffect(() => {
    if (!data) return
    const pts: LngLat[] = run
      ? run.legs.filter((l) => l.kind !== 'backup').flatMap((l) => l.coords ?? []).concat((run.track as LngLat[] | undefined) ?? [])
      : data.plans.robust.sites.map((s) => [s.lon, s.lat] as LngLat).concat(data.cells.length ? [[75.55, 12.82], [75.95, 13.05]] : [])
    if (!pts.length) return
    const lons = pts.map((p) => p[0])
    const lats = pts.map((p) => p[1])
    const narrow = window.innerWidth < 900
    const w = narrow ? window.innerWidth : window.innerWidth - 420 // map area beside the docked panel
    const h = (narrow ? window.innerHeight * 0.55 : window.innerHeight) - 56
    const vp = new WebMercatorViewport({ width: w, height: h }).fitBounds(
      [
        [Math.min(...lons), Math.min(...lats)],
        [Math.max(...lons), Math.max(...lats)],
      ],
      { padding: narrow ? 30 : { top: 70, bottom: 90, left: 60, right: 60 } },
    )
    const next = { longitude: vp.longitude, latitude: vp.latitude, zoom: Math.min(vp.zoom, 13), pitch: 0, bearing: 0 }
    setView(fitted.current ? { ...next, transitionDuration: 700, transitionInterpolator: new FlyToInterpolator() } : next)
    fitted.current = true
  }, [data, caseId, scenario]) // eslint-disable-line react-hooks/exhaustive-deps

  const layers = useMemo(() => {
    if (!data) return []
    const cutoff = new Set(scenario === 'shiradi' || scenario === 'both' ? data.cutoff[scenario] : [])
    const maxLam = Math.max(...data.cells.map((c) => c.lam))
    const L: any[] = []

    L.push(
      new H3HexagonLayer({
        id: 'cells',
        data: data.cells,
        getHexagon: (d: any) => d.h,
        filled: true,
        stroked: false,
        extruded: false,
        getFillColor: (d: any) =>
          cutoff.has(d.h)
            ? [220, 38, 38, caseId === 'standby' ? 150 : 70]
            : [37, 99, 235, Math.round((caseId === 'standby' ? 160 : 60) * Math.sqrt(d.lam / maxLam))],
        updateTriggers: { getFillColor: [scenario, caseId] },
        pickable: caseId === 'standby',
      }),
      new GeoJsonLayer({
        id: 'area',
        data: { type: 'Feature', geometry: data.area, properties: {} } as any,
        filled: false,
        getLineColor: [30, 41, 59, 200],
        lineWidthMinPixels: 1.5,
        getDashArray: [4, 3],
        extensions: [new PathStyleExtension({ dash: true })],
      }),
    )

    // ghat roads: brown, thicker in the monsoon; shut stretch beyond the slip greyed out
    const ghatPaths = Object.entries(data.ghats).flatMap(([gid, g]) => g.lines.map((p) => ({ gid, p })))
    L.push(
      new PathLayer({
        id: 'ghats',
        data: ghatPaths,
        getPath: (d: any) => d.p,
        getColor: (d: any) => (shut.includes(d.gid) ? [120, 53, 15, 140] : [180, 83, 9, 230]),
        getWidth: monsoon ? 7 : 5,
        widthUnits: 'pixels',
        updateTriggers: { getColor: [scenario], getWidth: [scenario] },
      }),
    )

    // no-signal stretches under the routes
    const legs = run?.legs.filter((l) => !l.blocked && l.coords) ?? []
    const dead = legs.flatMap((l) => (l.no_signal ?? []).map((p) => ({ p, kind: l.kind })))
    L.push(
      new PathLayer({
        id: 'no-signal',
        data: dead,
        getPath: (d: any) => d.p,
        getColor: [71, 85, 105, 90],
        getWidth: 14,
        widthUnits: 'pixels',
        capRounded: true,
        jointRounded: true,
      }),
    )

    // routes
    L.push(
      new PathLayer({
        id: 'routes',
        data: legs,
        getPath: (d: Leg) => d.coords!,
        getColor: (d: Leg) => LEG_STYLE[d.kind].color,
        getWidth: (d: Leg) => LEG_STYLE[d.kind].width,
        widthUnits: 'pixels',
        capRounded: true,
        jointRounded: true,
        getDashArray: (d: Leg) => LEG_STYLE[d.kind].dash ?? [0, 0],
        dashJustified: true,
        // legs that share a road (ambulance out, patient back) sit side by side instead of on top of each other
        getOffset: (d: Leg) => (d.kind === 'unit' ? -0.6 : d.kind === 'hospital' ? 0.6 : 0),
        extensions: [new PathStyleExtension({ dash: true, offset: true })],
        pickable: true,
      }),
      new TextLayer({
        id: 'route-minutes',
        // one label per distinct time: an out-and-back on the same road would otherwise show the same number twice
        data: legs.filter((l, i) => l.minutes != null && (l.km ?? 0) > 1 && !legs.slice(0, i).some((o) => Math.round(o.minutes ?? -1) === Math.round(l.minutes!))),
        getPosition: (d: Leg) => mid(d.coords!, LABEL_AT[d.kind]),
        getText: (d: Leg) => `${Math.round(d.minutes!)} min`,
        getSize: 14,
        getColor: (d: Leg) => LEG_STYLE[d.kind].color.slice(0, 3).concat(255) as RGBA,
        fontWeight: 700,
        background: true,
        getBackgroundColor: [255, 255, 255, 235],
        backgroundPadding: [5, 3],
        getBorderColor: (d: Leg) => LEG_STYLE[d.kind].color,
        getBorderWidth: 1.5,
        getPixelOffset: [0, -16],
      }),
    )

    if (caseId === 'estate' && run) {
      L.push(
        new PathLayer({
          id: 'estate-track',
          data: [{ p: run.track as LngLat[] }, { p: [run.patient as LngLat, run.meet as LngLat] }],
          getPath: (d: any) => d.p,
          getColor: [0, 132, 61, 230],
          getWidth: 4,
          widthUnits: 'pixels',
          getDashArray: [2, 1.5],
          extensions: [new PathStyleExtension({ dash: true })],
        }),
      )
    }

    // ---------------------------------------------------------------- signs
    type Sign = { pos: LngLat; sign: SignId; size: number; tip: string; off?: [number, number] }
    const signs: Sign[] = []
    for (const hp of data.hairpins) signs.push({ pos: hp, sign: monsoon ? 'slippery' : 'hairpin', size: 26, tip: monsoon ? 'Ghat road: slippery in the monsoon' : 'Ghat road: hairpin bends' })
    for (const [gid, g] of Object.entries(data.ghats)) {
      if (shut.includes(gid)) {
        signs.push({ pos: g.landslide, sign: 'closed', size: 40, tip: `${g.name}: road shut by a landslide (modelled spot)`, off: [-24, 0] })
        signs.push({ pos: g.landslide, sign: 'landslide', size: 40, tip: `${g.name}: landslide`, off: [24, 0] })
        signs.push({ pos: g.landslide, sign: 'diversion', size: 34, tip: 'Traffic diverted to another ghat', off: [0, 40] })
      } else if (monsoon) {
        signs.push({ pos: g.landslide, sign: 'landslide', size: 34, tip: `${g.name}: landslide-prone in the monsoon` })
      }
    }
    for (const l of legs)
      for (const p of l.no_signal ?? []) if (p.length > 8) signs.push({ pos: mid(p), sign: 'noSignal', size: 26, tip: 'No mobile signal on this stretch (modelled). Route and hospital details are already on the crew phone.' })

    const refused = caseId === 'icu_refusal' ? 'hassan_dh' : null
    for (const h of data.hospitals)
      signs.push({
        pos: [h.lon, h.lat],
        sign: h.id === refused ? 'refused' : h.tier === 'hub' ? 'hospital' : 'firstAid',
        size: h.id === refused ? 40 : 30,
        tip: `${h.name}${h.id === refused ? ': no ICU bed (pre-alert answer)' : ''}\n${h.tier === 'hub' ? 'Referral hub' : 'Taluk hospital'}: ${h.caps.join(', ')} (≈ assumed)`,
      })

    const unitSign = (u: { als: boolean; neighbour?: boolean }): SignId => (u.neighbour ? 'ambulanceNeighbour' : u.als ? 'ambulanceAls' : 'ambulanceBls')
    if (caseId === 'standby') {
      const plan = data.plans[monsoon ? 'robust' : 'dry']
      for (const s of plan.sites) for (let i = 0; i < s.n; i++) signs.push({ pos: [s.lon, s.lat], sign: 'ambulanceBls', size: 30, tip: `Standby: ${s.name}`, off: [i * 14, 0] })
    } else {
      for (const u of data.units) signs.push({ pos: [u.lon, u.lat], sign: unitSign(u), size: 30, tip: `${u.id} at ${u.site}${u.neighbour ? ' (next taluk)' : ''}${u.als ? ' · Advanced' : ''}` })
    }

    if (run) {
      const first = run.legs.find((l) => l.kind === 'unit' && l.coords)
      const patient = (caseId === 'estate' ? run.patient : first?.coords?.at(-1)) as LngLat | undefined
      if (caseId === 'estate') signs.push({ pos: run.meet as LngLat, sign: 'meet', size: 34, tip: 'Meeting point: ambulance waits here on the public road' })
      if (patient) signs.push({ pos: patient, sign: 'patient', size: 28, tip: 'Patient' })
    }

    L.push(
      new IconLayer({
        id: 'signs',
        data: signs,
        getPosition: (d: Sign) => d.pos,
        getIcon: (d: Sign) => icon(d.sign),
        getSize: (d: Sign) => d.size,
        getPixelOffset: (d: Sign) => d.off ?? [0, 0],
        sizeUnits: 'pixels',
        pickable: true,
        updateTriggers: { getIcon: [scenario, caseId] },
      }),
    )
    return L
  }, [data, run, scenario, caseId, monsoon]) // eslint-disable-line react-hooks/exhaustive-deps

  if (err) return <div className="rural-msg">{err}</div>
  if (!data) return <div className="rural-msg">Loading the Sakleshpura map…</div>

  return (
    <div className="rural">
      <header className="rural-head">
        <a className="rural-brand" href="../" title="Back to the main GoldenHour app">
          <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
            <path d="M7 1h6v6h6v6h-6v6H7v-6H1V7h6Z" fill="var(--critical)" />
          </svg>
          GoldenHour
        </a>
        <span className="rural-title">Rural demo · Sakleshpura and the Western Ghats</span>
        <div className="rural-right">
          <div className="rural-seg" role="radiogroup" aria-label="Season and road conditions">
            {SCENARIO_ORDER.map((s) => (
              <button key={s} role="radio" aria-checked={scenario === s} onClick={() => setScenario(s)}>
                {SCENARIO_SHORT[s]}
              </button>
            ))}
          </div>
          <a className="rural-link" href="../">
            Main app
          </a>
        </div>
      </header>

      <div className="rural-body">
        <div className="rural-map">
          <DeckGL
            viewState={view}
            onViewStateChange={({ viewState }: any) => setView(viewState)}
            controller
            layers={layers}
            getTooltip={({ object, layer }: any) => {
              if (!object) return null
              if (layer?.id === 'signs') return { text: object.tip }
              if (layer?.id === 'routes') return { text: `${LEG_NAME[object.kind as LegKind]}: ${object.label}\n${fmt(object.minutes)} · ${object.km} km${object.no_signal_km ? ` · ${object.no_signal_km} km without signal` : ''}` }
              if (layer?.id === 'cells') return { text: `About ${object.pop.toLocaleString('en-IN')} people · ${(object.lam * 30).toFixed(1)} calls a month (modelled)` }
              return null
            }}
          >
            <MapGL mapStyle={MAP_STYLE} />
          </DeckGL>
          <Legend caseId={caseId} monsoon={monsoon} shut={shut.length > 0} />
        </div>

        <aside className="rural-dock">
          <div className="rural-dock-head">
            <h1>Rural difficulties</h1>
            <p>
              {data.population.toLocaleString('en-IN')} people, about {data.calls_per_day} calls a day and {data.fleet} ambulances. Roads, hospitals and villages are real. Calls,
              closures and hospital capabilities are modelled.
            </p>
            <div className="rural-cases" role="tablist" aria-label="Situation">
              {CASES.map((c) => (
                <button key={c.id} role="tab" aria-selected={caseId === c.id} onClick={() => setCaseId(c.id)}>
                  {c.title}
                </button>
              ))}
            </div>
          </div>

          <section className="rural-story">{caseId === 'standby' ? <Standby data={data} scenario={scenario} /> : <Story id={caseId} run={run!} scenario={scenario} />}</section>

          {run && (
            <section>
              <h2>Routes</h2>
              <ul className="rural-legs">
                {run.legs.map((l, i) => (
                  <li key={i}>
                    <span className={`swatch ${l.kind}`} />
                    <span className="lbl">{l.label}</span>
                    <span className="min">{l.blocked ? 'no road' : fmt(l.minutes)}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <Difficulties />

          <details className="rural-more">
            <summary>What is real and what is modelled</summary>
            <table>
              <tbody>
                {data.assumptions.map(([k, v]) => (
                  <tr key={k}>
                    <th>{k}</th>
                    <td>{v}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </aside>
      </div>
    </div>
  )
}

// Floating key (bottom-left, like the main map): only the signs that can appear in the current view.
function Legend({ caseId, monsoon, shut }: { caseId: CaseId; monsoon: boolean; shut: boolean }) {
  const items: [SignId, string][] = [
    ...(shut ? ([['closed', 'Road closed'], ['landslide', 'Landslide'], ['diversion', 'Diversion']] as [SignId, string][]) : monsoon ? ([['landslide', 'Landslide risk']] as [SignId, string][]) : []),
    [monsoon ? 'slippery' : 'hairpin', monsoon ? 'Slippery ghat road' : 'Hairpin bends'],
    ['hospital', 'Referral hospital'],
    ['firstAid', 'Taluk hospital'],
    ...(caseId === 'icu_refusal' ? ([['refused', 'Hospital said no']] as [SignId, string][]) : []),
    ...(caseId === 'estate' ? ([['meet', 'Meeting point']] as [SignId, string][]) : []),
    ...(caseId !== 'standby' ? ([['noSignal', 'No mobile signal'], ['ambulanceAls', 'Advanced'], ['ambulanceBls', 'Basic'], ['ambulanceNeighbour', 'Next taluk']] as [SignId, string][]) : ([['ambulanceBls', 'Standby spot']] as [SignId, string][])),
  ]
  return (
    <div className="rural-legend" aria-label="Map key">
      {items.map(([id, label]) => (
        <span key={id}>
          <img src={signUrl(id)} alt="" />
          {label}
        </span>
      ))}
      <span className="rural-legend-note" title="Road signs follow the Indian road-sign code (IRC:67). The meeting point uses the ISO 7010 assembly-point sign.">
        IRC:67 signs
      </span>
    </div>
  )
}

// ----------------------------------------------------------------------------- case text
function Big({ value, label, tone }: { value: string; label: string; tone?: 'good' | 'bad' }) {
  return (
    <div className={`rural-big ${tone ?? ''}`}>
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  )
}

function Story({ id, run, scenario }: { id: Exclude<CaseId, 'standby'>; run: CaseRun; scenario: ScenarioId }) {
  const leg = (k: LegKind) => run.legs.find((l) => l.kind === k)
  const shut = scenario === 'shiradi' || scenario === 'both'
  if (id === 'crash_ghat') {
    const own = run.own_unit_min as number
    const chosen = run.chosen_unit_min as number
    const cross = run.cross_border as boolean
    return (
      <>
        <p>
          A crash on the lower Shiradi Ghat, below the stretch that slips in heavy rain.
          {!cross && ` The nearest ambulance, ${run.unit}, reaches the patient in ${fmt(chosen)} and takes them up to ${leg('hospital')?.label.replace('To ', '').replace(' (stabilise)', '')}.`}
          {cross &&
            ` A landslide has shut the ghat between the patient and Sakleshpura. Our own ambulance would have to go round by another road, which takes ${fmt(own)}. GoldenHour sends ${run.unit} from the next taluk instead, and the patient goes downhill to be stabilised.`}
        </p>
        <div className="rural-bigs">
          {cross ? (
            <>
              <Big value={fmt(own)} label="own ambulance, going round" tone="bad" />
              <Big value={fmt(chosen)} label="next taluk’s ambulance" tone="good" />
            </>
          ) : (
            <Big value={fmt(chosen)} label="ambulance reaches patient" />
          )}
        </div>
        <p className="rural-why">
          Why it matters: in the hills there is often only one road. When it shuts, the nearest ambulance on the map can be the slowest. Help across the taluk or district
          border is planned in advance, not argued over on the radio.
        </p>
      </>
    )
  }
  if (id === 'chest_pain') {
    const spoke = run.drug_at_spoke_min as number
    const hub = run.drug_at_hub_min as number
    return (
      <>
        <p>
          Chest pain in Hanbal, a village west of Sakleshpura. Hassan has the nearest heart unit but is far away. So the ambulance stops at Crawford Govt Hospital in Sakleshpura
          for the clot-busting drug, then goes on to Hassan. This is the hub-and-spoke model of Karnataka’s Hrudaya Jyothi scheme.
        </p>
        <div className="rural-bigs">
          <Big value={`${spoke} min`} label="drug given, stopping at the taluk hospital" tone="good" />
          <Big value={`${hub} min`} label="drug given, going straight to Hassan" tone="bad" />
        </div>
        <p className="rural-why">
          The drug arrives {hub - spoke} minutes sooner. Mangaluru is the fallback if Hassan is full: {fmt(run.mangaluru_min as number)} by road
          {shut ? ', because the landslide forces a long way round. In this monsoon Mangaluru is not a realistic fallback.' : '.'}
        </p>
      </>
    )
  }
  if (id === 'icu_refusal') {
    const w = run.with_prealert_min as number
    const wo = run.without_prealert_min as number
    return (
      <>
        <p>
          A road crash near Alur. Before leaving the scene the crew pre-alerts District Hospital, Hassan. It answers that it has no ICU bed. The ambulance goes straight to Mani
          Super Speciality instead.
        </p>
        <div className="rural-bigs">
          <Big value={fmt(w)} label="to an ICU bed, with pre-alert" tone="good" />
          <Big value={fmt(wo)} label="if turned away at the door" tone="bad" />
        </div>
        <p className="rural-why">
          In May 2026 an accident victim from Chikkamagaluru was taken to Wenlock Hospital in Mangaluru, refused for lack of an ICU bed, and brought back. Asking first means
          the refusal costs no driving.
        </p>
      </>
    )
  }
  return (
    <>
      <p>
        The patient is up a coffee-estate track{run.estate_name ? '' : ''} that an ambulance cannot use, and it is not a public road on the map. The crew is sent to a meeting
        point on the nearest public road. The estate jeep or a stretcher covers the last {String(run.last_km)} km.
      </p>
      <div className="rural-bigs">
        <Big value={fmt(leg('unit')?.minutes)} label="ambulance at meeting point" />
        <Big value={`${run.last_km} km`} label="last stretch by estate jeep" />
      </div>
      <p className="rural-why">Villages and estates often have no street address. The caller is asked for the estate or village name, and the crew gets a pin they can drive to.</p>
    </>
  )
}

function Standby({ data, scenario }: { data: RuralData; scenario: ScenarioId }) {
  const cut = scenario === 'shiradi' || scenario === 'both' ? data.cutoff[scenario] : []
  const cutSet = new Set(cut)
  const cutPeople = data.cells.filter((c) => cutSet.has(c.h)).reduce((a, c) => a + c.pop, 0)
  const row = (p: 'dry' | 'robust') => data.plans[p].kpis[scenario]
  return (
    <>
      <p>
        Where the {data.fleet} ambulances wait. The normal plan is tuned for the dry season. The monsoon plan is solved for dry, monsoon and landslide conditions together,
        weighted by how often each happens, so it still holds up when the ghat shuts.
      </p>
      <table className="rural-kpi">
        <thead>
          <tr>
            <th>{SCENARIO_SHORT[scenario]}</th>
            <th>Normal plan</th>
            <th>Monsoon plan</th>
          </tr>
        </thead>
        <tbody>
          {(['within_10', 'within_20', 'within_30'] as const).map((k) => (
            <tr key={k}>
              <th>Calls reached in {k.slice(7)} min</th>
              <td>{row('dry')[k]}%</td>
              <td>{row('robust')[k]}%</td>
            </tr>
          ))}
        </tbody>
      </table>
      {cut.length > 0 && (
        <p className="rural-cut">
          <span className="dot" /> About {Math.round(cutPeople).toLocaleString('en-IN')} people in {cut.length} map cells are cut off: their nearest ambulance is more than 15
          minutes further away while the ghat is shut. Calls from there go to the next taluk’s ambulance.
        </p>
      )}
      <p className="rural-why">
        Honest finding: with {data.fleet} ambulances for about 1,050 km², only about a quarter of calls can be reached in {data.target_min} minutes, whatever the plan. Meeting
        the rural target here needs more vehicles, not just better parking.
      </p>
    </>
  )
}

function Difficulties() {
  const rows: [string, string, boolean][] = [
    ['Ghat roads shut after landslides', 'Routes avoid the shut stretch; the next taluk’s ambulance is sent when it is faster', true],
    ['One road in, one road out', 'Standby plan solved for open and shut ghat together', true],
    ['Few hospitals, the nearest cannot treat', 'Stabilise at the taluk hospital, then transfer to the hub', true],
    ['Hospital turns the patient away', 'Pre-alert before leaving; re-route before the trip, not after', true],
    ['No mobile signal on ghats and in forest', 'Dead stretches flagged; route and hospital loaded on the crew phone before them; SMS fallback', true],
    ['Estate roads, no street addresses', 'Meeting point on the nearest public road; search by village or estate name', true],
    ['Rain and fog slow the ghats', 'Monsoon speeds used for ETAs and for the plan', true],
    ['Long transfers leave the taluk empty', 'Another unit moves up to cover while one is away (fleet planner)', false],
  ]
  return (
    <section>
      <h2>Rural difficulties and what we do</h2>
      <ul className="rural-diff">
        {rows.map(([d, a, onMap]) => (
          <li key={d}>
            <strong>{d}</strong>
            <span>{a}</span>
            {onMap && <em>on the map</em>}
          </li>
        ))}
      </ul>
    </section>
  )
}
