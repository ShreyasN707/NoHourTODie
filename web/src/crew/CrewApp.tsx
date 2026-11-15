import { useEffect, useState } from 'react'
import type { ComponentType, ReactNode } from 'react'
import {
  Activity, Ambulance, Baby, Bike, Bone, Brain, Check, CircleHelp, HeartPulse, Hospital, MapPin, Smile, Wind, X,
} from 'lucide-react'
import { api } from '../advisor/api'
import { whenAt } from '../advisor/format'
import type { RecommendResponse } from '../advisor/types'
import { CALL_LABEL, mmss, opsPost, useOps } from '../ops/ops'
import type { Incident } from '../ops/ops'
import { CrewMap, along } from './CrewMap'
import type { Pin, Route } from './CrewMap'
import './crew.css'

// Demo only: vehicles move along their route this many times faster than real time, so a drive takes under a minute.
const DEMO_SPEED = 8

type Icon = ComponentType<{ size?: number; strokeWidth?: number }>
const CALL_ICON: Record<string, Icon> = {
  cardiac: HeartPulse, stroke: Brain, trauma: Bone, pregnancy: Baby, respiratory: Wind, other: CircleHelp,
}
// Dispatch call type -> the advisor's complaint. The crew confirms or changes it on scene.
const COMPLAINT_OF: Record<string, string> = {
  cardiac: 'chest_pain', stroke: 'stroke_symptoms', trauma: 'trauma', pregnancy: 'obstetric', respiratory: 'airway', other: 'other',
}
const COMPLAINTS: [string, string, Icon][] = [
  ['chest_pain', 'Chest pain', HeartPulse], ['stroke_symptoms', 'Stroke signs', Brain], ['trauma', 'Injury', Bone],
  ['obstetric', 'Pregnancy', Baby], ['paediatric', 'Child', Smile], ['cardiac_arrest', 'Not breathing, no pulse', Activity],
  ['airway', 'Breathing trouble', Wind], ['other', 'Something else', CircleHelp],
]
const CAPABILITY: Record<string, string> = {
  ed_24x7: '24-hour emergency', ct_24x7: '24-hour CT scan', pci_24x7: '24-hour cath lab', thrombolysis: 'clot-busting drugs',
  thrombectomy: 'clot removal', trauma_ot: 'trauma theatre', neurosurgery: 'neurosurgery', orthopaedics: 'orthopaedics',
  blood_bank: 'blood bank', obstetric_emergency: 'emergency obstetrics', nicu: 'newborn ICU', picu: "children's ICU", burns_unit: 'burns unit',
}
const whyNot = (reason: string) =>
  reason.startsWith('missing:')
    ? `no ${reason.slice(8).split(',').map((c) => CAPABILITY[c] ?? c).join(' or ')}`
    : reason === 'diversion' ? 'not taking patients right now' : reason === 'over_travel_cap' ? 'too far' : reason.replace(/_/g, ' ')

const shortName = (n: string) => n.replace(/\s*\(.*\)\s*$/, '')
const ll = (lat: number, lon: number): [number, number] => [lon, lat]

export default function CrewApp() {
  const { incidents, now, online } = useOps()
  // The job this phone accepted; kept across a page reload.
  const [activeId, setActiveIdState] = useState<string | null>(() => { try { return localStorage.getItem('crew-job') } catch { return null } })
  const setActiveId = (id: string | null) => {
    setActiveIdState(id)
    try { if (id) localStorage.setItem('crew-job', id); else localStorage.removeItem('crew-job') } catch { /* storage blocked */ }
  }
  const [declined, setDeclined] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)

  const job: Incident | null = activeId
    ? incidents.find((i) => i.id === activeId) ?? null
    : incidents.find((i) => i.status === 'alerted' && !declined.has(i.id)) ?? null

  // Lost the job (cleared on the command centre): back to waiting.
  useEffect(() => { if (activeId && online && !incidents.some((i) => i.id === activeId)) setActiveId(null) }, [incidents, activeId, online])

  const post = async (status: string, destination?: unknown) => {
    if (!job) return
    setBusy(true)
    await opsPost(`incidents/${job.id}/status`, { status, destination }).catch(() => null)
    setBusy(false)
  }
  const accept = async () => {
    if (!job) return
    setActiveId(job.id)
    await post('accepted')
    await post('en_route') // ponytail: accept and wheels-rolling are one tap on the phone; split them if crews leave late
  }
  const reject = async () => {
    if (!job) return
    setDeclined((s) => new Set(s).add(job.id))
    await post('rejected')
  }

  return (
    <div className="crew-app">
      <Stage job={job} now={now} activeId={activeId} />
      <section className="crew-panel">
        <header className="crew-top">
          <Ambulance size={20} />
          <b>{job && activeId ? `${job.draft.ambulance.kind} · ${job.draft.ambulance.from_site}` : 'GoldenHour crew'}</b>
          <span className={`crew-dot ${online ? 'on' : ''}`}>{online ? 'Connected' : 'Offline'}</span>
        </header>
        {!online ? (
          <Waiting title="Not connected" text="The live operations server isn't running. Start it with make ops." />
        ) : !job || !activeId ? (
          <Waiting title="You're available" text="New jobs from the command centre will pop up here." />
        ) : (
          <Steps job={job} now={now} busy={busy} post={post} done={() => setActiveId(null)} />
        )}
      </section>
      {job && !activeId && job.status === 'alerted' && <AlertPopup job={job} busy={busy} onAccept={accept} onReject={reject} />}
    </div>
  )
}

// ---------------------------------------------------------------- map for the current step
function Stage({ job, now, activeId }: { job: Incident | null; now: number; activeId: string | null }) {
  const [options, setOptions] = useState<Pin[]>([])
  useEffect(() => {
    const h = (e: Event) => setOptions((e as CustomEvent<Pin[]>).detail)
    window.addEventListener('crew-options', h)
    return () => window.removeEventListener('crew-options', h)
  }, [])
  if (!job) return <CrewMap routes={[]} pins={[]} fitKey="idle" />
  const d = job.draft
  const patient = ll(d.patient.lat, d.patient.lon)
  const frac = (from?: number, etaMin?: number) => (from && etaMin ? ((now - from) * DEMO_SPEED) / (etaMin * 60) : 0)
  const routes: Route[] = []
  const pins: Pin[] = [{ pos: patient, kind: 'patient' }]
  const s = job.status
  if (s === 'alerted' || s === 'accepted' || s === 'en_route' || !activeId) {
    if (d.bike) routes.push({ path: d.bike.path, kind: 'bike' }) // under the ambulance route
    routes.push({ path: d.ambulance.path, kind: 'to_patient' })
    pins.push({ pos: along(d.ambulance.path, frac(job.times.en_route, d.ambulance.eta_min)), kind: 'unit' })
    if (d.bike) pins.push({ pos: along(d.bike.path, frac(job.times.en_route, d.bike.eta_min)), kind: 'bike' })
  } else if (s === 'on_scene') {
    pins.push(...options)
  } else {
    const h = ll(d.hospital.lat, d.hospital.lon)
    routes.push({ path: d.hospital.path, kind: 'to_hospital' })
    pins.push({ pos: h, kind: 'hospital' })
    pins.push({ pos: s === 'transporting' ? along(d.hospital.path, frac(job.times.transporting, d.hospital.eta_min)) : h, kind: 'unit' })
  }
  return <CrewMap routes={routes} pins={pins} fitKey={`${job.id}-${s}-${activeId ? 1 : 0}-${options.length}-${d.hospital.name}`} />
}

// ---------------------------------------------------------------- new-job popup
function AlertPopup({ job, busy, onAccept, onReject }: { job: Incident; busy: boolean; onAccept: () => void; onReject: () => void }) {
  const d = job.draft
  const Ico = CALL_ICON[d.call_type] ?? CircleHelp
  const eta = Math.round(d.ambulance.eta_min)
  return (
    <div className="crew-modal" role="alertdialog" aria-labelledby="crew-alert-title">
      <div className="crew-alert">
        <div className="crew-alert-head">
          <span className="crew-alert-ico"><Ico size={34} strokeWidth={2.2} /></span>
          <div>
            <p className="crew-kicker">New emergency</p>
            <h1 id="crew-alert-title">{CALL_LABEL[d.call_type] ?? 'Emergency'}</h1>
          </div>
        </div>
        <p className="crew-alert-place"><MapPin size={18} /> {d.patient.label}</p>
        <div className="crew-tiles">
          <Tile big={`${eta}`} unit="min" label="Your drive" tone={d.ambulance.within_target ? 'ok' : 'late'} />
          <Tile big={`${d.target_min}`} unit="min" label="Target" />
          <Tile big={d.bike ? `${Math.round(d.bike.eta_min)}` : '–'} unit={d.bike ? 'min' : ''} label="Bike first help" />
        </div>
        <EtaBar eta={d.ambulance.eta_min} target={d.target_min} />
        <p className="crew-alert-meta">{d.ambulance.kind} unit · call came in {job.times.call ? 'just now' : ''}</p>
        <div className="crew-alert-actions">
          <button className="crew-btn reject" disabled={busy} onClick={onReject}><X size={22} /> Reject</button>
          <button className="crew-btn accept" disabled={busy} onClick={onAccept}><Check size={22} /> Accept</button>
        </div>
      </div>
    </div>
  )
}

function Tile({ big, unit, label, tone }: { big: string; unit: string; label: string; tone?: 'ok' | 'late' }) {
  return (
    <div className={`crew-tile ${tone ?? ''}`}>
      <b>{big}<small>{unit}</small></b>
      <span>{label}</span>
    </div>
  )
}

function EtaBar({ eta, target }: { eta: number; target: number }) {
  const max = Math.max(eta, target) * 1.15
  return (
    <div className="crew-etabar" aria-label={`Drive ${Math.round(eta)} minutes against a ${target}-minute target`}>
      <i className={eta <= target ? 'ok' : 'late'} style={{ width: `${(eta / max) * 100}%` }} />
      <em style={{ left: `${(target / max) * 100}%` }} />
    </div>
  )
}

function Waiting({ title, text }: { title: string; text: string }) {
  return (
    <div className="crew-waiting">
      <span className="crew-pulse" aria-hidden="true" />
      <h2>{title}</h2>
      <p>{text}</p>
    </div>
  )
}

// ---------------------------------------------------------------- one step at a time
const STEP_NAMES = ['Drive', 'Assess', 'Hospital', 'Transport', 'Handover']

function Steps({ job, now, busy, post, done }: {
  job: Incident; now: number; busy: boolean; post: (s: string, dest?: unknown) => Promise<void>; done: () => void
}) {
  const d = job.draft
  const [phase, setPhase] = useState<'assess' | 'choose'>('assess')
  const s = job.status
  const step = s === 'on_scene' ? (phase === 'assess' ? 1 : 2) : s === 'transporting' ? 3 : s === 'at_hospital' || s === 'closed' ? 4 : 0

  const left = (from: number | undefined, eta: number) =>
    Math.max(0, eta - (from ? ((now - from) * DEMO_SPEED) / 60 : 0))

  return (
    <div className="crew-steps-wrap">
      <ol className="crew-progress" aria-label="Job steps">
        {STEP_NAMES.map((n, k) => <li key={n} className={k < step ? 'done' : k === step ? 'now' : ''}>{n}</li>)}
      </ol>

      {(s === 'accepted' || s === 'en_route') && (
        <Card title="Drive to the patient" foot={<Big onClick={() => post('on_scene')} busy={busy}>Reached patient</Big>}>
          <Countdown min={left(job.times.en_route, d.ambulance.eta_min)} of={d.ambulance.eta_min} label="to the patient" />
          <Fact icon={MapPin} title={d.patient.label} sub={`${CALL_LABEL[d.call_type] ?? 'Emergency'} call`} />
          {d.bike && <Fact icon={Bike} title="Bike first responder is also going" sub={`Arrives in about ${Math.round(d.bike.eta_min)} min`} />}
          <p className="crew-demo-note">Demo: the vehicle moves {DEMO_SPEED}× faster than real time.</p>
        </Card>
      )}

      {s === 'on_scene' && phase === 'assess' && (
        <Assess job={job} now={now} onDone={() => setPhase('choose')} />
      )}
      {s === 'on_scene' && phase === 'choose' && (
        <Choose job={job} busy={busy} onBack={() => setPhase('assess')}
          onGo={(dest) => post('transporting', dest)} />
      )}

      {s === 'transporting' && (
        <Card title="Drive to hospital" foot={<Big onClick={() => post('at_hospital')} busy={busy}>Reached hospital</Big>}>
          <Countdown min={left(job.times.transporting, d.hospital.eta_min)} of={d.hospital.eta_min} label="to the hospital" />
          <Fact icon={Hospital} title={d.hospital.name} sub="The hospital has been alerted (simulated)." />
        </Card>
      )}

      {s === 'at_hospital' && (
        <Card title="Hand over the patient" foot={<Big onClick={() => post('closed')} busy={busy}>Handover done</Big>}>
          <Fact icon={Hospital} title={d.hospital.name} sub="Tap when the emergency team has taken over." />
        </Card>
      )}

      {s === 'closed' && (
        <Card title="Job complete" foot={<Big onClick={done} busy={false}>Ready for the next job</Big>}>
          <Summary job={job} />
        </Card>
      )}
    </div>
  )
}

function Card({ title, children, foot }: { title: string; children: ReactNode; foot: ReactNode }) {
  return (
    <div className="crew-card">
      <h2>{title}</h2>
      <div className="crew-card-body">{children}</div>
      <div className="crew-card-foot">{foot}</div>
    </div>
  )
}

function Big({ children, onClick, busy, tone }: { children: ReactNode; onClick: () => void; busy: boolean; tone?: string }) {
  return <button className={`crew-btn big ${tone ?? 'go'}`} disabled={busy} onClick={onClick}>{children}</button>
}

function Fact({ icon: Ico, title, sub }: { icon: Icon; title: string; sub: string }) {
  return (
    <div className="crew-fact">
      <span><Ico size={20} /></span>
      <div><b>{title}</b><small>{sub}</small></div>
    </div>
  )
}

function Countdown({ min, of, label }: { min: number; of: number; label: string }) {
  const pct = of ? Math.min(100, (1 - min / of) * 100) : 100
  return (
    <div className="crew-count">
      <b>{min < 0.5 ? 'Arriving' : `${Math.ceil(min)} min`}</b>
      <span>{label}</span>
      <div className="crew-count-bar"><i style={{ width: `${pct}%` }} /></div>
    </div>
  )
}

function Summary({ job }: { job: Incident }) {
  const t = job.times
  const rows: [string, number | undefined, number | undefined][] = [
    ['Call to crew alert', t.call, t.alerted], ['Crew answered', t.alerted, t.accepted], ['Drive to patient', t.en_route, t.on_scene],
    ['On scene', t.on_scene, t.transporting], ['Drive to hospital', t.transporting, t.at_hospital], ['Handover', t.at_hospital, t.closed],
  ]
  const total = t.closed && t.call ? t.closed - t.call : 0
  return (
    <>
      <p className="crew-big-line">{mmss(total)} <small>call to free again</small></p>
      <ul className="crew-summary">
        {rows.map(([n, a, b]) => (
          <li key={n}><span>{n}</span><b>{a && b ? mmss(b - a) : '–'}</b></li>
        ))}
      </ul>
    </>
  )
}

// ---------------------------------------------------------------- on scene: assessment, then hospital
type Form = { complaint: string; ecg: 'yes' | 'no' | 'unknown'; stroke: string; gcs: string; sbp: string; rr: string }
let lastForm: Record<string, Form> = {} // per job, so "Change answers" keeps what was entered

function Assess({ job, now, onDone }: { job: Incident; now: number; onDone: () => void }) {
  const [f, setF] = useState<Form>(() => lastForm[job.id] ?? {
    complaint: COMPLAINT_OF[job.draft.call_type] ?? 'other', ecg: 'unknown', stroke: '', gcs: '', sbp: '', rr: '',
  })
  const [vitals, setVitals] = useState(false)
  const set = (k: keyof Form, v: string) => setF((x) => ({ ...x, [k]: v }))
  const go = () => {
    lastForm = { ...lastForm, [job.id]: f }
    sessionAssessment.set(job.id, { f, at: now })
    onDone()
  }
  return (
    <Card title="What's wrong with the patient?" foot={<Big onClick={go} busy={false}>Find the best hospital</Big>}>
      <div className="crew-grid" role="radiogroup" aria-label="Main problem">
        {COMPLAINTS.map(([v, label, Ico]) => (
          <button key={v} role="radio" aria-checked={f.complaint === v} className={`crew-choice ${f.complaint === v ? 'on' : ''}`}
            onClick={() => set('complaint', v)}>
            <Ico size={24} /><span>{label}</span>
          </button>
        ))}
      </div>
      {f.complaint === 'chest_pain' && (
        <Segment label="ECG shows a STEMI heart attack?" value={f.ecg} onChange={(v) => set('ecg', v)}
          options={[['yes', 'Yes'], ['no', 'No'], ['unknown', 'No ECG']]} />
      )}
      {f.complaint === 'stroke_symptoms' && (
        <label className="crew-num">Stroke score (if taken)
          <input inputMode="decimal" value={f.stroke} onChange={(e) => set('stroke', e.target.value)} placeholder="not taken" />
        </label>
      )}
      <button className="crew-link" onClick={() => setVitals((x) => !x)}>{vitals ? 'Hide vitals' : 'Add vitals (optional)'}</button>
      {vitals && (
        <div className="crew-vitals">
          <label className="crew-num">GCS<input inputMode="numeric" value={f.gcs} onChange={(e) => set('gcs', e.target.value)} placeholder="3–15" /></label>
          <label className="crew-num">BP top<input inputMode="numeric" value={f.sbp} onChange={(e) => set('sbp', e.target.value)} placeholder="mmHg" /></label>
          <label className="crew-num">Breaths<input inputMode="numeric" value={f.rr} onChange={(e) => set('rr', e.target.value)} placeholder="/min" /></label>
        </div>
      )}
    </Card>
  )
}
const sessionAssessment = new Map<string, { f: Form; at: number }>()

function Segment({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: any) => void; options: [string, string][]
}) {
  return (
    <div className="crew-seg-wrap">
      <span>{label}</span>
      <div className="crew-seg" role="radiogroup" aria-label={label}>
        {options.map(([v, l]) => (
          <button key={v} role="radio" aria-checked={value === v} className={value === v ? 'on' : ''} onClick={() => onChange(v)}>{l}</button>
        ))}
      </div>
    </div>
  )
}

function Choose({ job, busy, onBack, onGo }: {
  job: Incident; busy: boolean; onBack: () => void; onGo: (dest: { name: string; lat: number; lon: number }) => void
}) {
  const [resp, setResp] = useState<RecommendResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [pick, setPick] = useState<string | null>(null)
  const a = sessionAssessment.get(job.id)

  useEffect(() => {
    if (!a) return
    const d = job.draft
    const num = (s: string) => (s.trim() && Number.isFinite(Number(s)) ? Number(s) : undefined)
    const vitals: Record<string, number> = {}
    for (const k of ['gcs', 'sbp', 'rr'] as const) { const v = num(a.f[k]); if (v !== undefined) vitals[k] = v }
    const [blon, blat] = d.ambulance.path[0]
    api.recommend({
      case_id: job.id, scene_lat: d.patient.lat, scene_lon: d.patient.lon, base_lat: blat, base_lon: blon,
      complaint: a.f.complaint, vitals,
      minutes_since_first_contact: job.times.on_scene && job.times.call ? Math.round((job.times.on_scene - job.times.call) / 60) : 0,
      ecg_stemi: a.f.ecg === 'yes' ? true : a.f.ecg === 'no' ? false : null,
      stroke_score: num(a.f.stroke) ?? null, when: whenAt(d.hour),
    }).then((r) => { setResp(r); setPick(r.recommendation.ranked[0]?.hospital_id ?? null) },
      () => setErr("Couldn't reach the hospital advisor. Taking the nearest hospital is still possible."))
  }, [job.id]) // oxlint-disable-line react-hooks/exhaustive-deps

  const ranked = resp?.recommendation.ranked.slice(0, 3) ?? []
  // Numbered pins for the options on the map.
  useEffect(() => {
    const pins: Pin[] = ranked.flatMap((r, k) => {
      const h = resp?.hospitals[r.hospital_id]
      return h ? [{ pos: ll(h.lat, h.lon), kind: r.hospital_id === pick ? 'hospital' : 'option', label: String(k + 1) } as Pin] : []
    })
    window.dispatchEvent(new CustomEvent('crew-options', { detail: pins }))
    return () => { window.dispatchEvent(new CustomEvent('crew-options', { detail: [] })) }
  }, [resp, pick]) // oxlint-disable-line react-hooks/exhaustive-deps

  const go = async () => {
    const h = pick && resp?.hospitals[pick]
    if (pick && h) {
      await api.sendPreAlert(job.id, pick !== ranked[0]?.hospital_id ? pick : undefined).catch(() => null)
      onGo({ name: h.name, lat: h.lat, lon: h.lon })
    } else {
      const d = job.draft.hospital
      onGo({ name: d.name, lat: d.lat, lon: d.lon })
    }
  }
  const max = Math.max(1, ...ranked.map((r) => r.components.total))
  const chosen = ranked.find((r) => r.hospital_id === pick)
  const skipped = (resp?.recommendation.excluded ?? []).filter((x) => x.components).slice(0, 2)

  return (
    <Card title="Best hospital for this patient"
      foot={<Big onClick={go} busy={busy || (!resp && !err)}>{chosen ? `Go to ${shortName(chosen.name)}` : err ? `Go to ${job.draft.hospital.name}` : 'Checking hospitals…'}</Big>}>
      {err && <p className="crew-err">{err}</p>}
      {!resp && !err && <p className="crew-muted">Comparing hospitals by time to proper treatment…</p>}
      {resp && (
        <>
          <div className="crew-options" role="radiogroup" aria-label="Hospital">
            {ranked.map((r, k) => {
              const c = r.components
              const risk = c.p_cannot_treat * c.transfer_delay_min
              return (
                <button key={r.hospital_id} role="radio" aria-checked={pick === r.hospital_id}
                  className={`crew-option ${pick === r.hospital_id ? 'on' : ''}`} onClick={() => setPick(r.hospital_id)}>
                  <span className="crew-option-n">{k + 1}</span>
                  <span className="crew-option-main">
                    <b>{r.name}</b>
                    <span className="crew-stack" style={{ width: `${(c.total / max) * 100}%` }}>
                      <i className="t" style={{ flex: c.travel_min }} /><i className="w" style={{ flex: c.ed_wait_min }} /><i className="r" style={{ flex: risk }} />
                    </span>
                    <small>{Math.round(c.travel_min)} min drive · {Math.round(c.ed_wait_min)} min wait{risk >= 1 ? ` · ${Math.round(risk)} min transfer risk` : ''}</small>
                  </span>
                  <span className="crew-option-total"><b>{Math.round(c.total)}</b><small>min to care</small></span>
                </button>
              )
            })}
          </div>
          <p className="crew-legend"><i className="t" />Drive <i className="w" />Emergency room wait <i className="r" />Risk of a second transfer</p>
          {skipped.length > 0 && (
            <div className="crew-skipped">
              <b>Not suggested</b>
              {skipped.map((x) => <p key={x.hospital_id}>{x.name}: {whyNot(x.reason)}</p>)}
            </div>
          )}
          {resp.banner && <p className="crew-sim">Simulated hospital data: fictional hospitals and waits.</p>}
        </>
      )}
      <button className="crew-link" onClick={onBack}>Change answers</button>
    </Card>
  )
}
