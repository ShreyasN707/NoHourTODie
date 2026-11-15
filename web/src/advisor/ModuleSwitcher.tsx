import { useState } from 'react'
import type { ReactNode } from 'react'
import AdvisorView from './AdvisorView'
import { crewUrl, fleetUrl, isCrewRoute } from './route'
import './switcher.css'

type Module = 'fleet' | 'advisor'

const OPTIONS: [Module, string][] = [
  ['fleet', 'GoldenHour (fleet)'],
  ['advisor', 'Crew dashboard'],
]

/**
 * Top-level switch between GoldenHour (module A, passed in as `fleet`) and the Destination Advisor (module B).
 * A small floating control, so neither module's layout has to change. The choice is kept in the URL
 * (/crew or #crew, plus /sim for the simulation tab; see route.ts) so a reload stays on the same module.
 */
export function ModuleSwitcher({ fleet }: { fleet: ReactNode }) {
  const [module, setModule] = useState<Module>(() => (isCrewRoute() ? 'advisor' : 'fleet'))
  const pick = (m: Module) => {
    setModule(m)
    window.history.replaceState(null, '', m === 'advisor' ? crewUrl() : fleetUrl())
  }
  return (
    <>
      {module === 'advisor' ? <AdvisorView /> : fleet}
      <nav className="module-switcher" aria-label="Module">
        {OPTIONS.map(([m, label]) => (
          <button key={m} aria-pressed={module === m} className={module === m ? 'is-on' : ''} onClick={() => pick(m)}>
            {label}
          </button>
        ))}
      </nav>
    </>
  )
}
