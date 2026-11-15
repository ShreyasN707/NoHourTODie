import { COMPLAINTS, DEMO_HOURS } from './format'
import type { DemoHour } from './format'

export type Ecg = 'yes' | 'no' | 'na'

export interface FormState {
  complaint: string
  gcs: string
  sbp: string
  rr: string
  ecg: Ecg
  strokeScore: string
  minutesSinceContact: string
}

function Num({
  label,
  hint,
  value,
  onChange,
  min,
  max,
}: {
  label: string
  hint?: string
  value: string
  onChange: (v: string) => void
  min: number
  max: number
}) {
  return (
    <label className="adv-field">
      <span>
        {label}
        {hint && <small>{hint}</small>}
      </span>
      <input
        type="number"
        inputMode="decimal"
        min={min}
        max={max}
        value={value}
        placeholder="not taken"
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  )
}

export function AssessmentForm({
  form,
  onChange,
  hour,
  onHour,
}: {
  form: FormState
  onChange: (f: FormState) => void
  hour: DemoHour
  onHour: (h: DemoHour) => void
}) {
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => onChange({ ...form, [k]: v })

  return (
    <form className="adv-form" onSubmit={(e) => e.preventDefault()} aria-label="EMT assessment">
      <fieldset className="adv-time">
        <legend>Time of day, Mon 16 Nov 2026</legend>
        <div className="adv-seg" role="radiogroup">
          {DEMO_HOURS.map((h) => (
            <button
              key={h}
              type="button"
              role="radio"
              aria-checked={hour === h}
              className={hour === h ? 'is-on' : ''}
              onClick={() => onHour(h)}
            >
              {h}:00 <small>{h === 11 ? 'off-peak' : 'evening rush'}</small>
            </button>
          ))}
        </div>
      </fieldset>

      <label className="adv-field">
        <span>Chief complaint</span>
        <select value={form.complaint} onChange={(e) => set('complaint', e.target.value)}>
          {COMPLAINTS.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
      </label>

      <fieldset>
        <legend>Vitals</legend>
        <div className="adv-row3">
          <Num label="GCS" hint="3–15" value={form.gcs} onChange={(v) => set('gcs', v)} min={3} max={15} />
          <Num label="SBP" hint="mmHg" value={form.sbp} onChange={(v) => set('sbp', v)} min={0} max={300} />
          <Num label="RR" hint="/min" value={form.rr} onChange={(v) => set('rr', v)} min={0} max={80} />
        </div>
      </fieldset>

      <fieldset className={form.complaint === 'chest_pain' ? '' : 'is-dim'}>
        <legend>
          ECG shows STEMI <small>(chest pain)</small>
        </legend>
        <div className="adv-seg" role="radiogroup">
          {(
            [
              ['yes', 'Yes'],
              ['no', 'No'],
              ['na', 'No ECG'],
            ] as const
          ).map(([v, l]) => (
            <button
              key={v}
              type="button"
              role="radio"
              aria-checked={form.ecg === v}
              className={form.ecg === v ? 'is-on' : ''}
              onClick={() => set('ecg', v)}
            >
              {l}
            </button>
          ))}
        </div>
      </fieldset>

      <div className="adv-row2">
        <div className={form.complaint === 'stroke_symptoms' ? '' : 'is-dim'}>
          <Num
            label="Stroke score"
            hint="LVO"
            value={form.strokeScore}
            onChange={(v) => set('strokeScore', v)}
            min={0}
            max={42}
          />
        </div>
        <Num
          label="First contact"
          hint="min ago"
          value={form.minutesSinceContact}
          onChange={(v) => set('minutesSinceContact', v)}
          min={0}
          max={600}
        />
      </div>
    </form>
  )
}
