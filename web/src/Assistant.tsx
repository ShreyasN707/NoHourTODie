import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import './Assistant.css'
import { PathLayer, ScatterplotLayer } from '@deck.gl/layers'
import { AdviceCard, type Advice } from './AdviceCard'

// One assistant request per message the user sends: nothing is fetched on load, while typing, or in the background.

export type Dispatch = {
  dispatch_id: string
  region: string
  hour: number
  call_type: string
  target_min: number
  patient: { label: string; lat: number; lon: number }
  ambulance: { kind: string; from_site: string; eta_min: number; within_target: boolean; path: [number, number][] }
  bike: null | { from_site: string; eta_min: number; path: [number, number][] }
  hospital: { name: string; lat: number; lon: number; eta_min: number; path: [number, number][] }
  alerts: { to: string; text: string }[]
  note: string
  alt?: string | null
}
type Msg = { role: 'user' | 'bot'; text: string; dispatch?: Dispatch; advice?: Advice; sent?: { to: string; text: string; at: string }[] }

const SESSION = Math.random().toString(36).slice(2)
const SUGGESTIONS = [
  'Patient at DSCE college, bike accident',
  'Based on yesterday\'s black spots, how should I rearrange my fleet today?',
  'Where are the black spots?',
]

async function post(path: string, body: unknown) {
  const r = await fetch(`/api/${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (!r.ok) throw new Error(`${r.status}`)
  return r.json()
}

/** Map layers for the active dispatch: routes for ambulance (red), bike (orange), to hospital (green). */
export function dispatchLayers(d: Dispatch | null) {
  if (!d) return []
  const paths = [
    { path: d.hospital.path, color: [34, 197, 94], width: 5 },
    ...(d.bike ? [{ path: d.bike.path, color: [249, 115, 22], width: 5 }] : []),
    { path: d.ambulance.path, color: [239, 68, 68], width: 7 },
  ]
  return [
    new PathLayer({
      id: 'dispatch-routes', data: paths, getPath: (p: any) => p.path, getColor: (p: any) => p.color,
      getWidth: (p: any) => p.width, widthUnits: 'pixels', capRounded: true, jointRounded: true,
    }),
    new ScatterplotLayer({
      id: 'dispatch-points',
      data: [
        { pos: [d.patient.lon, d.patient.lat], color: [255, 255, 255], line: [239, 68, 68], r: 11 },
        { pos: [d.hospital.lon, d.hospital.lat], color: [34, 197, 94], line: [255, 255, 255], r: 8 },
        { pos: d.ambulance.path[0], color: [239, 68, 68], line: [255, 255, 255], r: 8 },  // where the ambulance starts
        ...(d.bike ? [{ pos: d.bike.path[0], color: [249, 115, 22], line: [255, 255, 255], r: 7 }] : []),
      ],
      getPosition: (p: any) => p.pos, getFillColor: (p: any) => p.color, getLineColor: (p: any) => p.line,
      getRadius: (p: any) => p.r, radiusUnits: 'pixels', stroked: true, lineWidthMinPixels: 3,
    }),
  ]
}

function Orb({ size = 64 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" className="gh-orb-svg" aria-hidden="true">
      <defs>
        <radialGradient id="ghCore" cx="50%" cy="45%" r="55%">
          <stop offset="0%" stopColor="#2a1470" />
          <stop offset="100%" stopColor="#0d0533" />
        </radialGradient>
        <linearGradient id="ghWaveA" x1="0" x2="1">
          <stop offset="0%" stopColor="#a78bfa" stopOpacity="0.9" />
          <stop offset="60%" stopColor="#6366f1" stopOpacity="0.85" />
          <stop offset="100%" stopColor="#4338ca" stopOpacity="0.6" />
        </linearGradient>
        <linearGradient id="ghWaveB" x1="0" x2="1">
          <stop offset="0%" stopColor="#67e8f9" />
          <stop offset="55%" stopColor="#c4b5fd" />
          <stop offset="100%" stopColor="#e879f9" stopOpacity="0.7" />
        </linearGradient>
      </defs>
      <circle cx="50" cy="50" r="46" fill="url(#ghCore)" />
      <g className="gh-wave gh-wave-a">
        <path d="M14 52 C 30 22, 55 26, 70 34 C 82 40, 86 30, 88 26 C 86 44, 76 62, 56 66 C 38 70, 24 60, 14 52 Z" fill="url(#ghWaveA)" />
      </g>
      <g className="gh-wave gh-wave-b">
        <path d="M14 52 C 32 44, 48 58, 66 50 C 76 46, 82 50, 86 56 C 74 66, 56 72, 40 66 C 28 62, 20 56, 14 52 Z" fill="url(#ghWaveB)" />
      </g>
      <circle cx="50" cy="50" r="46" fill="none" stroke="#c4b5fd" strokeOpacity="0.55" strokeWidth="1.5" />
    </svg>
  )
}

const CALL_LABEL: Record<string, string> = {
  pregnancy: 'Pregnancy', trauma: 'Accident / injury', cardiac: 'Heart', stroke: 'Stroke', respiratory: 'Breathing', other: 'Emergency',
}
const short = (s: string) => (s.length > 34 ? s.slice(0, 32) + '…' : s)

function Row({ icon, title, sub, eta, unit }: { icon: string; title: string; sub: string; eta: number; unit: string }) {
  return (
    <div className="gh-row">
      <div className="gh-ico" aria-hidden="true">{icon}</div>
      <div className="gh-row-text">
        <div className="gh-row-title">{title}</div>
        <div className="gh-row-sub" title={sub}>{short(sub)}</div>
      </div>
      <div className="gh-eta"><b>{Math.round(eta)}</b><span>{unit}</span></div>
    </div>
  )
}

function DispatchCard({ d, sent, onConfirm, onShow }: { d: Dispatch; sent?: { to: string; at: string }[]; onConfirm: () => void; onShow: () => void }) {
  const [showAlerts, setShowAlerts] = useState(false)
  const place = d.patient.label.replace(/^near /, '')
  return (
    <div className="gh-card">
      <div className="gh-card-head">
        <span className="gh-chip">{CALL_LABEL[d.call_type] ?? 'Emergency'}</span>
        <span className={`gh-chip ${d.ambulance.within_target ? 'ok' : 'late'}`}>
          {d.ambulance.within_target ? 'Within' : 'Over'} {d.target_min}-min target
        </span>
      </div>
      <div className="gh-place" title={place}>📍 {d.patient.label.startsWith('near ') ? 'Near ' : ''}{place}</div>
      <Row icon="🚑" title={`${d.ambulance.kind} ambulance`} sub={`from ${d.ambulance.from_site}`} eta={d.ambulance.eta_min} unit="min" />
      {d.bike && <Row icon="🏍️" title="Bike first responder" sub={`from ${d.bike.from_site}`} eta={d.bike.eta_min} unit="min" />}
      <Row icon="🏥" title={d.hospital.name} sub="drive from patient" eta={d.hospital.eta_min} unit="min" />
      {!sent ? (
        <>
          <button className="gh-link" onClick={() => setShowAlerts((x) => !x)}>
            {showAlerts ? '▾' : '▸'} {d.alerts.length} alerts ready to send
          </button>
          {showAlerts && <ul className="gh-alert-list">{d.alerts.map((a, k) => <li key={k}><b>{a.to}</b><br />{a.text}</li>)}</ul>}
          <div className="gh-actions">
            <button className="gh-confirm" onClick={onConfirm}>Confirm and send</button>
            <button className="gh-show" onClick={onShow}>Map</button>
          </div>
        </>
      ) : (
        <>
          <button className="gh-link sent" onClick={() => setShowAlerts((x) => !x)}>
            ✓ {sent.length} alerts sent at {sent[0]?.at.slice(0, 5)} (simulated) {showAlerts ? '▾' : '▸'}
          </button>
          {showAlerts && <ul className="gh-alert-list">{sent.map((a, k) => <li key={k}>✓ {a.to}</li>)}</ul>}
        </>
      )}
      {d.alt && !sent && <div className="gh-alt">Wrong place? Also found: {d.alt}</div>}
    </div>
  )
}

export default function Assistant({ onDispatch, onAdvice, region }: {
  onDispatch: (d: Dispatch | null) => void
  onAdvice: (a: Advice | null) => void
  region: 'blr' | 'tmk'
}) {
  const [open, setOpen] = useState(() => new URLSearchParams(location.search).has('assistant')) // ?assistant opens it (demo links)
  const [msgs, setMsgs] = useState<Msg[]>([
    { role: 'bot', text: 'Tell me where the patient is and what happened, ask how an area is doing, or ask how to rearrange the fleet after yesterday\'s late calls. I plan; you confirm before anything is sent.' },
  ])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  useEffect(() => { // scroll only the chat box, never the page
    const el = bodyRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [msgs, open, busy])

  async function send(text: string) {
    const t = text.trim().slice(0, 500)
    if (!t || busy) return // one request at a time; no empty or duplicate sends
    setInput('')
    setBusy(true)
    setMsgs((m) => [...m, { role: 'user', text: t }])
    try {
      const r = await post('chat', { session: SESSION, message: t, region })
      setMsgs((m) => [...m, { role: 'bot', text: r.reply, dispatch: r.dispatch || undefined, advice: r.advice || undefined }])
      if (r.dispatch) onDispatch(r.dispatch)
      if (r.advice) onAdvice(r.advice)
    } catch {
      setMsgs((m) => [...m, { role: 'bot', text: 'The assistant server is not reachable. Start it with: make assistant' }])
    } finally {
      setBusy(false)
    }
  }

  async function confirm(i: number, d: Dispatch) {
    const r = await post('confirm', { dispatch_id: d.dispatch_id }).catch(() => ({ error: 'Could not reach the server.' }))
    setMsgs((m) => m.map((x, j) => (j === i ? { ...x, sent: r.alerts ?? [] } : x)).concat(r.error ? [{ role: 'bot', text: r.error }] : []))
  }

  // rendered on <body> so `position: fixed` is relative to the window, whatever the app layout does
  return createPortal(
    <>
      {!open && (
        <button className="gh-orb" onClick={() => setOpen(true)} aria-label="Open GoldenHour assistant" title="GoldenHour assistant">
          <Orb />
        </button>
      )}
      {open && (
        <div className="gh-panel" role="dialog" aria-label="GoldenHour assistant">
          <div className="gh-head">
            <Orb size={30} />
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 800, fontSize: 14 }}>GoldenHour Assistant</div>
              <div style={{ fontSize: 12.5, color: 'var(--muted)' }}>Plans units, routes and fleet moves. You confirm.</div>
            </div>
            <button className="gh-x" onClick={() => setOpen(false)} aria-label="Close">×</button>
          </div>
          <div className="gh-body" ref={bodyRef}>
            {msgs.map((m, i) => (
              <div key={i} className={`gh-msg ${m.role}`}>
                <div>{m.text}</div>
                {m.dispatch && <DispatchCard d={m.dispatch} sent={m.sent} onConfirm={() => confirm(i, m.dispatch!)} onShow={() => onDispatch(m.dispatch!)} />}
                {m.advice && <AdviceCard a={m.advice} onShow={() => onAdvice(m.advice!)} />}
              </div>
            ))}
            {busy && <div className="gh-msg bot gh-typing"><span /><span /><span /></div>}
          </div>
          {msgs.length <= 1 && (
            <div className="gh-chips">
              {SUGGESTIONS.map((s) => <button key={s} onClick={() => send(s)} disabled={busy}>{s}</button>)}
            </div>
          )}
          <form className="gh-input" onSubmit={(e) => { e.preventDefault(); send(input) }}>
            <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="e.g. Patient at DSCE college, chest pain" disabled={busy} maxLength={500} />
            <button type="submit" disabled={busy || !input.trim()}>Send</button>
          </form>
        </div>
      )}
    </>,
    document.body,
  )
}
