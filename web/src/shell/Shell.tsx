import { useEffect, useState } from 'react'
import App, { type View } from '../App'
import './shell.css'

type Region = 'blr' | 'tmk'

const TABS: { id: View; label: string; hint: string }[] = [
  { id: 'live', label: 'Live calls', hint: 'Incoming calls, the assistant and routes' },
  { id: 'plan', label: 'Fleet plan', hint: 'Where each ambulance and bike waits, hour by hour' },
  { id: 'evidence', label: 'Results', hint: 'Simulations, stress tests, fleet size and sources' },
]
const CREW_APP = `${import.meta.env.BASE_URL}crew.html`

/** Tab from the URL: #live / #plan / #results. The hospital choice is a crew tool, so old crew links
 *  (/crew, #crew) open the crew phone app; old simulation links (/crew/sim) open Results. */
function tabFromUrl(): View {
  const { hash, pathname } = window.location
  const where = hash || pathname
  if (/(^#|\/)(crew|advisor)\/sim\/?$/.test(where)) return 'evidence'
  if (/(^#|\/)(crew|advisor)\/?$/.test(where)) {
    window.location.replace(CREW_APP)
    return 'live'
  }
  const h = hash.slice(1)
  if (h === 'results' || h === 'evidence') return 'evidence'
  return h === 'plan' ? 'plan' : 'live'
}

export default function Shell() {
  const [tab, setTab] = useState<View>(tabFromUrl)
  const [region, setRegion] = useState<Region>('blr')

  useEffect(() => {
    const onHash = () => setTab(tabFromUrl())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const go = (t: View) => {
    setTab(t)
    const path = window.location.pathname.replace(/\/crew(\/sim)?\/?$/, '/') || '/'
    window.history.replaceState(null, '', `${path}${window.location.search}#${t === 'evidence' ? 'results' : t}`)
  }

  return (
    <div className="sh">
      <header className="sh-head">
        <div className="sh-brand" aria-label="GoldenHour">
          <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
            <rect x="8" y="1" width="6" height="20" rx="1.5" fill="var(--critical)" />
            <rect x="1" y="8" width="20" height="6" rx="1.5" fill="var(--critical)" />
          </svg>
          <span>GoldenHour</span>
        </div>
        <nav className="sh-tabs" aria-label="Sections">
          {TABS.map((t) => (
            <button key={t.id} className={tab === t.id ? 'on' : ''} aria-current={tab === t.id ? 'page' : undefined}
              title={t.hint} onClick={() => go(t.id)}>
              {t.label}
            </button>
          ))}
        </nav>
        <div className="sh-right">
          <div className="sh-seg" role="group" aria-label="Region">
            <button aria-pressed={region === 'blr'} onClick={() => setRegion('blr')}>Bengaluru</button>
            <button aria-pressed={region === 'tmk'} onClick={() => setRegion('tmk')}>Tumakuru</button>
          </div>
          <nav className="sh-links" aria-label="Other apps">
            <a href={CREW_APP} target="_blank" rel="noreferrer" title="Phone app for ambulance crews">Crew app</a>
            <a href={`${import.meta.env.BASE_URL}ruraldemo/`} target="_blank" rel="noreferrer" title="Rural district demo (Sakleshpura)">Rural demo</a>
          </nav>
          <span className="sh-note" title="108 call logs are not public: calls are generated from public data. Roads, population and hospitals are real OpenStreetMap / WorldPop data.">
            Modelled data
          </span>
        </div>
      </header>
      <main className="sh-body">
        <App view={tab} region={region} onRegion={setRegion} />
      </main>
    </div>
  )
}
