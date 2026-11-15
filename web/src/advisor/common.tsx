import type { ReactNode } from 'react'
import { AlertTriangle, PlugZap } from 'lucide-react'
import { ApiHttpError, ApiUnreachableError } from './api'
import { PROVENANCE_INFO, PROVENANCE_ORDER, inputLabel } from './format'
import type { Provenance } from './types'

export function ProvenanceBadge({ p, input }: { p: Provenance; input?: string }) {
  const info = PROVENANCE_INFO[p]
  return (
    <span
      className={`adv-prov adv-prov--${p.toLowerCase()}`}
      title={`${input ? inputLabel(input) + ': ' : ''}${p} (${info.meaning})`}
    >
      <span className="adv-prov__swatch" aria-hidden="true" />
      {input ? <span className="adv-prov__input">{inputLabel(input)}</span> : null}
      <span className="adv-prov__tag">{p}</span>
    </span>
  )
}

export function ProvenanceList({ items }: { items: Record<string, Provenance> }) {
  const entries = Object.entries(items)
  if (!entries.length) return <span className="adv-muted">no inputs reported</span>
  return (
    <div className="adv-prov-list">
      {entries.map(([k, p]) => (
        <ProvenanceBadge key={k} p={p} input={k} />
      ))}
    </div>
  )
}

export function ProvenanceLegend() {
  return (
    <dl className="adv-prov-legend" aria-label="Provenance legend">
      {PROVENANCE_ORDER.map((p) => (
        <div key={p}>
          <dt>
            <ProvenanceBadge p={p} />
          </dt>
          <dd>{PROVENANCE_INFO[p].meaning}</dd>
        </div>
      ))}
    </dl>
  )
}

export function DataBanner({ text }: { text: string }) {
  return (
    <div className="adv-banner" role="status">
      <AlertTriangle size={16} aria-hidden="true" />
      <span>{text}</span>
    </div>
  )
}

/** Error display. Unreachable API gets the start-it instruction; never falls back to fake data. */
export function ApiErrorNotice({ error, notFound }: { error: unknown; notFound?: ReactNode }) {
  if (error instanceof ApiUnreachableError) {
    return (
      <div className="adv-notice adv-notice--offline" role="alert">
        <PlugZap size={18} aria-hidden="true" />
        <div>
          <strong>
            Advisor API not running: start it with <code>make advisor-api</code>
          </strong>
          <p>{error.message}. Nothing is shown until it answers.</p>
        </div>
      </div>
    )
  }
  if (error instanceof ApiHttpError && error.status === 404 && notFound) {
    return <div className="adv-notice">{notFound}</div>
  }
  const msg = error instanceof Error ? error.message : String(error)
  return (
    <div className="adv-notice adv-notice--error" role="alert">
      <AlertTriangle size={18} aria-hidden="true" />
      <div>
        <strong>The advisor API returned an error</strong>
        <p>{msg}</p>
      </div>
    </div>
  )
}
