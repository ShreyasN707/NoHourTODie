import type {
  Assessment,
  HealthResponse,
  HospitalsResponse,
  PreAlert,
  RecommendResponse,
  SimResults,
} from './types'

export const API_BASE: string = (import.meta.env.VITE_ADVISOR_API as string | undefined) ?? 'http://localhost:8000'

/** The API could not be reached at all (server not started, wrong port, CORS). */
export class ApiUnreachableError extends Error {
  constructor() {
    super(`Advisor API not reachable at ${API_BASE}`)
    this.name = 'ApiUnreachableError'
  }
}

/** The API answered with a non-2xx status. `detail` is FastAPI's error detail, stringified. */
export class ApiHttpError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(`HTTP ${status}: ${detail}`)
    this.name = 'ApiHttpError'
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiUnreachableError()
  }
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail ?? body)
    } catch {
      // body was not JSON; keep statusText
    }
    throw new ApiHttpError(res.status, detail)
  }
  return (await res.json()) as T
}

const post = <T>(path: string, body: unknown, signal?: AbortSignal) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body), signal })

export const api = {
  health: () => request<HealthResponse>('/health'),
  hospitals: () => request<HospitalsResponse>('/hospitals'),
  recommend: (a: Assessment, signal?: AbortSignal) => post<RecommendResponse>('/recommend', a, signal),
  sendPreAlert: (case_id: string, crew_choice?: string) =>
    post<PreAlert>('/prealert', crew_choice ? { case_id, crew_choice } : { case_id }),
  respondPreAlert: (prealert_id: string, accepted: boolean, reason_code: string) =>
    post<PreAlert>(`/prealert/${encodeURIComponent(prealert_id)}/response`, { accepted, reason_code }),
  simResults: () => request<SimResults>('/sim/results'),
}
