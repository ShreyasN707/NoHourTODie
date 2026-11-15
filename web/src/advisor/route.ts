/**
 * URL routing for the crew dashboard (Destination Advisor, module B).
 *
 *   /crew      or #crew       -> crew dashboard, live advisor tab
 *   /crew/sim  or #crew/sim   -> crew dashboard, simulation results tab
 *   #advisor, #advisor/sim    -> old links, still accepted
 *
 * The hash form works on any static host (the build uses a relative base). The /crew path form works on the
 * Vite dev server and on any host that serves index.html for unknown paths.
 */
const CREW_PATH = /\/crew(\/sim)?\/?$/
const CREW_HASH = /^#(crew|advisor)(\/sim)?$/

export function isCrewRoute(): boolean {
  return CREW_PATH.test(window.location.pathname) || CREW_HASH.test(window.location.hash)
}

export function crewTab(): 'live' | 'sim' {
  const p = window.location.pathname.match(CREW_PATH)
  const h = window.location.hash.match(CREW_HASH)
  return p?.[1] || h?.[2] ? 'sim' : 'live'
}

/** URL for the fleet (GoldenHour) view: strips a trailing /crew or /crew/sim from the path and drops the hash. */
export function fleetUrl(): string {
  const path = window.location.pathname.replace(CREW_PATH, '/') || '/'
  return path + window.location.search
}

/** URL for the crew dashboard: keeps a /crew path if already on one, otherwise uses #crew. */
export function crewUrl(tab: 'live' | 'sim' = 'live'): string {
  const suffix = tab === 'sim' ? '/sim' : ''
  if (CREW_PATH.test(window.location.pathname)) {
    return window.location.pathname.replace(CREW_PATH, `/crew${suffix}`) + window.location.search
  }
  return window.location.pathname + window.location.search + `#crew${suffix}`
}
