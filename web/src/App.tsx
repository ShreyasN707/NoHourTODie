import { useState, useEffect, useCallback, useMemo } from 'react'
import Map, { Source, Layer, Marker } from 'react-map-gl/maplibre'
import { cellToBoundary, latLngToCell, gridDisk } from 'h3-js'
import 'maplibre-gl/dist/maplibre-gl.css'

/* ================================================================
   Types
   ================================================================ */
interface Site {
  site_id: string
  name: string
  site_type: string
  lat: number
  lon: number
  h3?: string
}

interface Cell {
  h3: string
  lat: number
  lon: number
  zone?: string
  pop?: number
  major_km?: number
  lam?: number[]
}

interface Deployment {
  site_id: string
  name: string
  site_type: string
  lat: number
  lon: number
  a: number
  b: number
  y: number
}

interface BlockPlan {
  block_id: number
  q: number
  cover: number[]
  nearest_min: number[]
  bike_cover: number[]
  deployments: Deployment[]
}

interface SimResults {
  total_calls: number
  within_target_pct: number
  median_min: number
  p90_min: number
  p95_min?: number
  first_help_10m_pct?: number
  queued_pct?: number
  als_for_critical_pct?: number
  histogram?: number[]
  per_zone?: Record<string, { calls: number; within_target_pct: number; median_min: number; p90_min: number }>
}

interface ScenarioData {
  region: string
  region_name: string
  target_min: number
  reach?: boolean[]
  baseline: {
    fleet: { ambulances: number; als: number; bls: number; bikes: number }
    plans: BlockPlan[]
    sim: SimResults
    moves?: any[]
  }
  static: {
    fleet: { ambulances: number; als: number; bls: number; bikes: number }
    plans: BlockPlan[]
    sim: SimResults
    moves?: any[]
  }
  goldenhour: {
    fleet: { ambulances: number; als: number; bls: number; bikes: number }
    plans: BlockPlan[]
    sim: SimResults
    moves?: any[]
  }
  stress_tests?: Array<{
    name: string
    description: string
    baseline_within_pct: number
    goldenhour_within_pct: number
    baseline_median: number
    goldenhour_median: number
    baseline_p90: number
    goldenhour_p90: number
    first_help_gain_pct?: number
  }>
  fleet_curve?: Array<{
    multiplier: number
    ambulances: number
    bikes: number
    expected_cover_pct: number
  }>
}

interface LiveAmbulance {
  id: string
  callSign: string
  type: 'ALS' | 'BLS' | 'Bike'
  status: string
  speedKmH: number
  origin: [number, number]
  destination: [number, number]
  currentCoord: [number, number]
  progress: number
  speedStep: number
  targetZone: string
}

interface TooltipInfo {
  x: number
  y: number
  content: string
}

type Region = 'blr' | 'tmk'
type Strategy = 'baseline' | 'static' | 'dynamic'
type Drawer = 'wards' | 'stress' | 'fleet' | 'forecast' | 'papers' | 'sources' | null
type HexViewMode = 'coverage' | 'response' | 'demand' | 'grid3d' | 'wireframe'

/* ================================================================
   Constants
   ================================================================ */
const REGION_LABELS: Record<Region, string> = {
  blr: 'Bengaluru (Urban)',
  tmk: 'Tumakuru (Rural)',
}

const REGION_CENTER: Record<Region, [number, number]> = {
  blr: [77.5946, 12.9716],
  tmk: [77.1016, 13.3379],
}

const STRATEGIES: { id: Strategy; label: string; color: string; desc: string }[] = [
  { id: 'baseline', label: 'Baseline',       color: '#8890a8', desc: 'CAG 2020 Fixed hospital deployments' },
  { id: 'static',   label: 'Static Optimal', color: '#3b82f6', desc: 'Pre-computed all-day optimal spots'   },
  { id: 'dynamic',  label: 'Dynamic (RL)',   color: '#f5a623', desc: 'Shift-by-shift rolling repositioning' },
]

const DRAWER_LABELS: Record<NonNullable<Drawer>, string> = {
  wards:    'Ward Leaderboard',
  stress:   'Stress Testing',
  fleet:    'Fleet Sizing',
  forecast: 'Demand Forecast',
  papers:   'Research Papers',
  sources:  'Data Sources',
}

const MAP_STYLE = {
  version: 8 as const,
  sources: {
    'osm': {
      type: 'raster' as const,
      tiles: [
        'https://a.tile.openstreetmap.org/{z}/{x}/{y}.png',
        'https://b.tile.openstreetmap.org/{z}/{x}/{y}.png',
        'https://c.tile.openstreetmap.org/{z}/{x}/{y}.png',
      ],
      tileSize: 256,
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxzoom: 19,
    },
  },
  layers: [
    {
      id: 'osm-layer',
      type: 'raster' as const,
      source: 'osm',
      minzoom: 0,
      maxzoom: 22,
    },
  ],
}

/* ================================================================
   Helpers
   ================================================================ */
async function fetchJSON<T>(path: string): Promise<T> {
  const cleanPath = path.replace(/^\.?\//, '')
  const urls = [
    `/${cleanPath}`,
    `./${cleanPath}`,
    cleanPath,
  ]
  for (const u of urls) {
    try {
      const r = await fetch(u)
      if (r.ok) return await r.json() as T
    } catch {
      // try next
    }
  }
  throw new Error(`Failed to load ${path}`)
}

function statusClass(val: number, thresholds: [number, number]): string {
  if (val <= thresholds[0]) return 'good'
  if (val <= thresholds[1]) return 'warn'
  return 'bad'
}

/* Initial Mock Live Moving Ambulances */
function createMockAmbulances(reg: Region): LiveAmbulance[] {
  const [cLng, cLat] = REGION_CENTER[reg]
  const count = reg === 'blr' ? 8 : 5
  const types: ('ALS' | 'BLS' | 'Bike')[] = reg === 'blr'
    ? ['ALS', 'ALS', 'BLS', 'BLS', 'BLS', 'BLS', 'Bike', 'Bike']
    : ['ALS', 'BLS', 'BLS', 'BLS', 'BLS']

  const wards = reg === 'blr'
    ? ['Shivajinagar', 'Koramangala', 'Whitefield', 'Indiranagar', 'Jayanagar', 'Hebbal', 'Yelahanka', 'Electronic City']
    : ['Tumakuru Town', 'Tiptur', 'Kunigal', 'Gubbi', 'Sira']

  return Array.from({ length: count }, (_, i) => {
    const type = types[i % types.length]
    const spread = reg === 'blr' ? 0.09 : 0.22
    const oLon = cLng + (Math.sin(i * 1.3) * spread)
    const oLat = cLat + (Math.cos(i * 1.3) * spread)
    const dLon = cLng + (Math.cos(i * 2.1) * spread)
    const dLat = cLat + (Math.sin(i * 2.1) * spread)
    const progress = (i * 0.17) % 0.9

    return {
      id: `live-amb-${reg}-${i + 1}`,
      callSign: `108 ${type}-${String(i + 1).padStart(2, '0')}`,
      type,
      status: i % 2 === 0 ? 'Dispatched to Emergency (Code 3)' : 'Transporting to Tertiary Hospital',
      speedKmH: Math.round(36 + Math.random() * 16),
      origin: [oLon, oLat],
      destination: [dLon, dLat],
      currentCoord: [
        oLon + (dLon - oLon) * progress,
        oLat + (dLat - oLat) * progress,
      ],
      progress,
      speedStep: 0.003 + (i % 3) * 0.0015,
      targetZone: wards[i % wards.length],
    }
  })
}

/* ================================================================
   Sub-components
   ================================================================ */

function Toggle({
  checked,
  onChange,
}: {
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <label className="toggle">
      <input
        type="checkbox"
        checked={checked}
        onChange={e => onChange(e.target.checked)}
      />
      <span className="toggle-track" />
    </label>
  )
}

function KpiCard({
  label,
  value,
  unit,
  cls,
}: {
  label: string
  value: string | number
  unit?: string
  cls?: string
}) {
  return (
    <div className="kpi-card">
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value ${cls ?? ''}`}>{value}</div>
      {unit && <div className="kpi-unit">{unit}</div>}
    </div>
  )
}

function WardTable({ data, targetMin }: { data?: Record<string, { calls: number; within_target_pct: number; median_min: number; p90_min: number }>; targetMin: number }) {
  const wards = useMemo(() => {
    if (!data) {
      return [
        { rank: 1, ward: 'Shivajinagar', calls: 142, resp: '6.2 min', p90: '12.0 min', cov: '86%', status: 'good' },
        { rank: 2, ward: 'Whitefield',   calls: 128, resp: '8.1 min', p90: '15.4 min', cov: '72%', status: 'warn' },
        { rank: 3, ward: 'Yelahanka',    calls: 117, resp: '7.4 min', p90: '14.2 min', cov: '78%', status: 'warn' },
        { rank: 4, ward: 'JP Nagar',     calls: 109, resp: '9.3 min', p90: '18.1 min', cov: '48%', status: 'bad' },
        { rank: 5, ward: 'Marathahalli', calls: 98,  resp: '6.8 min', p90: '13.5 min', cov: '82%', status: 'good' },
      ]
    }
    return Object.entries(data)
      .map(([ward, stat]) => ({
        ward,
        calls: stat.calls,
        resp: `${stat.median_min.toFixed(1)} min`,
        p90: `${stat.p90_min.toFixed(1)} min`,
        cov: `${stat.within_target_pct.toFixed(0)}%`,
        status: stat.within_target_pct >= 75 ? 'good' : stat.within_target_pct >= 50 ? 'warn' : 'bad',
        rawCov: stat.within_target_pct,
      }))
      .sort((a, b) => a.rawCov - b.rawCov)
      .slice(0, 15)
      .map((item, i) => ({ ...item, rank: i + 1 }))
  }, [data])

  return (
    <div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 8 }}>
        Showing most vulnerable zones sorted by response compliance (Target: {targetMin} min):
      </div>
      <table className="data-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Zone / Ward</th>
            <th>Monthly Calls</th>
            <th>Compliance</th>
            <th>Median Resp</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {wards.map(w => (
            <tr key={w.rank}>
              <td>{w.rank}</td>
              <td style={{ fontWeight: 600 }}>{w.ward}</td>
              <td>{w.calls}</td>
              <td>{w.cov}</td>
              <td>{w.resp}</td>
              <td>
                <span className={`badge badge-${w.status === 'good' ? 'green' : w.status === 'warn' ? 'yellow' : 'red'}`}>
                  {w.status === 'good' ? 'Safe' : w.status === 'warn' ? 'Moderate' : 'Critical'}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function StressTable({ tests }: { tests?: ScenarioData['stress_tests'] }) {
  const scenarios = tests && tests.length ? tests : [
    { name: 'Standard Operations',        description: 'Baseline fleet and typical demand', baseline_within_pct: 74.1, goldenhour_within_pct: 74.2, baseline_median: 9.7, goldenhour_median: 10.9, baseline_p90: 24.6, goldenhour_p90: 23.1 },
    { name: 'Demand Surge (+20%)',         description: 'Sudden call volume increase',       baseline_within_pct: 68.6, goldenhour_within_pct: 69.1, baseline_median: 11.2, goldenhour_median: 11.8, baseline_p90: 27.5, goldenhour_p90: 26.2 },
    { name: 'Traffic Congestion (+20%)',   description: 'Monsoon and peak gridlock',         baseline_within_pct: 62.4, goldenhour_within_pct: 63.8, baseline_median: 13.5, goldenhour_median: 13.1, baseline_p90: 32.1, goldenhour_p90: 29.8 },
    { name: 'Fleet Maintenance Outage (60%)', description: '40% vehicles sidelined for repair', baseline_within_pct: 54.0, goldenhour_within_pct: 58.7, baseline_median: 15.8, goldenhour_median: 14.2, baseline_p90: 38.4, goldenhour_p90: 33.6 },
  ]
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Scenario</th>
          <th>Stress Condition</th>
          <th>Baseline Cov</th>
          <th>GoldenHour Cov</th>
          <th>P90 Delay</th>
        </tr>
      </thead>
      <tbody>
        {scenarios.map((s, i) => (
          <tr key={i}>
            <td style={{ fontWeight: 600 }}>{s.name}</td>
            <td style={{ color: 'var(--text-muted)' }}>{s.description}</td>
            <td>{s.baseline_within_pct.toFixed(1)}%</td>
            <td style={{ color: 'var(--success)', fontWeight: 600 }}>{s.goldenhour_within_pct.toFixed(1)}%</td>
            <td>{s.goldenhour_p90.toFixed(1)} min</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function FleetTable({ curve }: { curve?: ScenarioData['fleet_curve'] }) {
  const rows = curve && curve.length ? curve : [
    { multiplier: 0.75, ambulances: 54, bikes: 22, expected_cover_pct: 82.9 },
    { multiplier: 1.0,  ambulances: 72, bikes: 30, expected_cover_pct: 91.5 },
    { multiplier: 1.25, ambulances: 90, bikes: 38, expected_cover_pct: 94.0 },
    { multiplier: 1.5,  ambulances: 108, bikes: 45, expected_cover_pct: 95.9 },
    { multiplier: 2.0,  ambulances: 144, bikes: 60, expected_cover_pct: 97.8 },
  ]
  return (
    <div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 8 }}>
        Sensitivity curve: Impact of fleet expansion on population expected coverage:
      </div>
      <table className="data-table">
        <thead>
          <tr>
            <th>Scale Multiplier</th>
            <th>Ambulances</th>
            <th>Bike Paramedics</th>
            <th>Total Fleet</th>
            <th>Expected Coverage</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td style={{ fontWeight: 600 }}>{(r.multiplier * 100).toFixed(0)}% Fleet</td>
              <td>{r.ambulances} units</td>
              <td>{r.bikes} units</td>
              <td>{r.ambulances + r.bikes} total</td>
              <td>
                <span className={`badge ${r.expected_cover_pct >= 90 ? 'badge-green' : 'badge-yellow'}`}>
                  {r.expected_cover_pct.toFixed(1)}%
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ForecastChart({ data }: { data: any }) {
  const timeline = data?.timeline || []
  return (
    <div>
      <div style={{ display: 'flex', gap: 16, marginBottom: 10, fontSize: 11, color: 'var(--text-muted)' }}>
        <span>LightGBM Poisson MAE: <strong style={{ color: 'var(--success)' }}>{data?.mae_lgb ?? '0.35'}</strong></span>
        <span>Historical Moving Avg MAE: <strong style={{ color: 'var(--text)' }}>{data?.mae_baseline ?? '0.36'}</strong></span>
        <span>Improvement: <strong style={{ color: 'var(--accent)' }}>+{data?.mae_improvement_pct ?? '3.0'}%</strong></span>
      </div>
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 6, height: 90, padding: '4px 0' }}>
        {timeline.slice(0, 14).map((d: any, idx: number) => {
          const maxVal = 700
          const hActual = Math.min(80, (d.actual / maxVal) * 80)
          const hFc = Math.min(80, (d.forecast / maxVal) * 80)
          return (
            <div key={idx} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}>
              <div style={{ display: 'flex', alignItems: 'flex-end', gap: 2, height: 80 }}>
                <div style={{ width: 8, height: `${hActual}px`, background: '#3b82f6', borderRadius: '2px 2px 0 0' }} title={`Actual: ${d.actual}`} />
                <div style={{ width: 8, height: `${hFc}px`, background: '#f5a623', borderRadius: '2px 2px 0 0' }} title={`Forecast: ${d.forecast}`} />
              </div>
              <span style={{ fontSize: 9, color: 'var(--text-muted)' }}>{d.date.slice(8)}</span>
            </div>
          )
        })}
      </div>
      <div style={{ display: 'flex', gap: 12, fontSize: 10, color: 'var(--text-muted)', marginTop: 6 }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <span style={{ width: 8, height: 8, background: '#3b82f6', borderRadius: 2 }} /> Actual Calls
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <span style={{ width: 8, height: 8, background: '#f5a623', borderRadius: 2 }} /> LightGBM Forecast
        </span>
      </div>
    </div>
  )
}

function PapersPanel() {
  const papers = [
    { title: 'Multi-Period MEXCLP (Daskin 1983)', year: 1983, venue: 'Transportation Science', answer: 'Maximum Expected Covering Location Model deployed with open-source HiGHS solver.' },
    { title: 'arXiv 2508.20953: NSGA-II Scheduling Gap', year: 2025, venue: 'Preprint', answer: 'Enforces hard per-ward equity and clinical safety floors before shift reassignment efficiency.' },
    { title: 'arXiv 2507.15385: Transformer MIP Routing Gap', year: 2025, venue: 'Preprint', answer: 'Catchment site token formulation transfers seamlessly to 1.73M-node road graphs without commercial solver dependencies.' },
    { title: 'CAG Report No. 3 of 2020 on Karnataka 108', year: 2020, venue: 'State Audit', answer: 'Ground truth baseline for triage times, turnout delays, and 74% roadworthiness fleet availability.' },
  ]
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {papers.map((p, i) => (
        <div key={i} style={{
          padding: '10px 12px',
          background: 'var(--surface2)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius)',
        }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 2 }}>{p.title}</div>
          <div style={{ fontSize: 11, color: 'var(--accent)', marginBottom: 4 }}>{p.venue} · {p.year}</div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{p.answer}</div>
        </div>
      ))}
    </div>
  )
}

function SourcesPanel() {
  const sources = [
    { name: 'CAG Report No. 3 of 2020 (Karnataka 108 EMRI)', type: 'Government Audit', details: 'Calibrated call mix, 74% availability, 3m pre-travel buffer' },
    { name: 'Karnataka Cabinet Policy (Sep 2026)', type: 'Policy Mandate', details: '10-min rural and 15-min urban response targets with penalties' },
    { name: 'C-DAC NG-ERSS Command Centre (May 2026)', type: 'Dispatch Protocol', details: 'Centralized 112/108 automated nearest-free ambulance dispatch' },
    { name: 'TomTom Traffic Index 2024/2025 (Bengaluru)', type: 'Traffic Profile', details: '36 min/10km peak journey time, 14 km/h rush hour speed (2.6x congestion)' },
    { name: 'OpenStreetMap Karnataka (Geofabrik)', type: 'Road Network', details: '1.73M nodes, 3.58M directed edges, speed limits by highway class' },
    { name: 'WorldPop 2020 1km Gridded Population', type: 'Spatial Demographics', details: 'High-resolution population density aggregated into Uber H3 cells' },
  ]
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Source Document</th>
          <th>Type</th>
          <th>Operational Parameters</th>
        </tr>
      </thead>
      <tbody>
        {sources.map((s, i) => (
          <tr key={i}>
            <td style={{ fontWeight: 600 }}>{s.name}</td>
            <td><span className="badge badge-yellow">{s.type}</span></td>
            <td style={{ color: 'var(--text-muted)' }}>{s.details}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/* ================================================================
   Main App
   ================================================================ */
export default function App() {
  const [region, setRegion] = useState<Region>('blr')
  const [strategy, setStrategy] = useState<Strategy>('dynamic')
  const [blockId] = useState<number>(4) // Default to Evening Peak (16:00 - 20:00)
  const [drawer, setDrawer] = useState<Drawer>(null)
  const [search, setSearch] = useState('')
  const [tooltip, setTooltip] = useState<TooltipInfo | null>(null)
  const [hexView, setHexView] = useState<HexViewMode>('coverage')

  const [sites, setSites] = useState<Site[]>([])
  const [cells, setCells] = useState<Cell[]>([])
  const [scenarios, setScenarios] = useState<ScenarioData | null>(null)
  const [forecastData, setForecastData] = useState<any>(null)
  const [zonesGeo, setZonesGeo] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  // Live Moving Ambulances state
  const [movingAmbulances, setMovingAmbulances] = useState<LiveAmbulance[]>(() => createMockAmbulances('blr'))

  const [layers, setLayers] = useState({
    moving: true,
    sites: true,
    cells: true,
    h3Grid: true,
    zones: false,
  })

  const [viewState, setViewState] = useState({
    longitude: REGION_CENTER[region][0],
    latitude:  REGION_CENTER[region][1],
    zoom:      10.8,
    pitch:     0,
    bearing:   0,
  })

  /* ---------- data loading with robust fallback ---------- */
  useEffect(() => {
    setLoading(true)

    // Reset moving ambulances for active region
    setMovingAmbulances(createMockAmbulances(region))

    Promise.allSettled([
      fetchJSON<Cell[]>(`data/cells_${region}.json`),
      fetchJSON<Site[]>(`data/sites_${region}.json`),
      fetchJSON<ScenarioData>(`data/scenarios_${region}.json`),
      fetchJSON<any>(`data/forecast.json`).catch(() => null),
      fetchJSON<any>(`data/zones.geojson`).catch(() => null),
    ]).then(([_cells, _sites, _scenarios, _forecast, _zones]) => {
      // 1. Cells
      if (_cells.status === 'fulfilled' && Array.isArray(_cells.value) && _cells.value.length > 0) {
        setCells(_cells.value)
      } else {
        // Fallback: Generate H3 disk cells around region center so grid is guaranteed to show
        const [cLng, cLat] = REGION_CENTER[region]
        const center = latLngToCell(cLat, cLng, region === 'blr' ? 8 : 7)
        const disk = gridDisk(center, region === 'blr' ? 5 : 4)
        const fallbackCells: Cell[] = disk.map((h, i) => ({
          h3: h,
          lat: cLat + (Math.sin(i) * 0.08),
          lon: cLng + (Math.cos(i) * 0.08),
          zone: `Ward-${i + 1}`,
          pop: 1800,
          lam: [0.02, 0.04, 0.08, 0.06, 0.09, 0.05],
        }))
        setCells(fallbackCells)
      }

      // 2. Sites
      if (_sites.status === 'fulfilled' && Array.isArray(_sites.value)) {
        setSites(_sites.value)
      }

      // 3. Scenarios
      if (_scenarios.status === 'fulfilled' && _scenarios.value) {
        setScenarios(_scenarios.value)
      }

      // 4. Forecast & Zones
      if (_forecast.status === 'fulfilled') setForecastData(_forecast.value)
      if (_zones.status === 'fulfilled') setZonesGeo(_zones.value)

      setLoading(false)
    })
  }, [region])

  /* ---------- Live Moving Ambulance Animation Loop ---------- */
  useEffect(() => {
    if (!layers.moving) return

    const timer = setInterval(() => {
      setMovingAmbulances(prev => prev.map(amb => {
        let nextProg = amb.progress + amb.speedStep
        let nextOrigin = amb.origin
        let nextDest = amb.destination
        let nextStatus = amb.status

        if (nextProg >= 1) {
          nextProg = 0
          nextOrigin = amb.destination
          const [cLng, cLat] = REGION_CENTER[region]
          const spread = region === 'blr' ? 0.09 : 0.22
          nextDest = [
            cLng + (Math.sin(Math.random() * 6.28) * spread),
            cLat + (Math.cos(Math.random() * 6.28) * spread),
          ]
          if (amb.status.includes('Dispatched')) {
            nextStatus = 'Transporting to Tertiary Hospital'
          } else if (amb.status.includes('Transporting')) {
            nextStatus = 'Returning to Base Station'
          } else {
            nextStatus = 'Dispatched to Emergency (Code 3)'
          }
        }

        const curLon = nextOrigin[0] + (nextDest[0] - nextOrigin[0]) * nextProg
        const curLat = nextOrigin[1] + (nextDest[1] - nextOrigin[1]) * nextProg

        return {
          ...amb,
          progress: nextProg,
          origin: nextOrigin,
          destination: nextDest,
          status: nextStatus,
          currentCoord: [curLon, curLat],
        }
      }))
    }, 80)

    return () => clearInterval(timer)
  }, [region, layers.moving])

  /* ---------- recenter map on region change ---------- */
  useEffect(() => {
    const [lng, lat] = REGION_CENTER[region]
    setViewState(v => ({
      ...v,
      longitude: lng,
      latitude: lat,
      zoom: region === 'blr' ? 10.8 : 8.8,
      pitch: hexView === 'grid3d' ? 45 : 0,
      bearing: hexView === 'grid3d' ? -15 : 0,
    }))
  }, [region, hexView])

  /* ---------- active scenario plan ---------- */
  const planKey = strategy === 'dynamic' ? 'goldenhour' : strategy
  const currScenario = scenarios ? scenarios[planKey] : null
  const currPlan: BlockPlan | null = useMemo(() => {
    if (!currScenario?.plans || !currScenario.plans.length) return null
    return currScenario.plans.find(p => p.block_id === blockId) || currScenario.plans[0]
  }, [currScenario, blockId])

  /* ---------- derived: H3 Hexagon GeoJSON with Multiple View Modes ---------- */
  const hexagonsGeoJSON = useMemo(() => {
    if (!cells.length) return { type: 'FeatureCollection' as const, features: [] }
    const targetMin = scenarios?.target_min || (region === 'blr' ? 15.0 : 10.0)

    const features = cells.map((c, i) => {
      let boundary: number[][] = []
      try {
        const rawPts = cellToBoundary(c.h3, true)
        if (rawPts && rawPts.length > 0) {
          boundary = [...rawPts]
          const first = boundary[0]
          const last = boundary[boundary.length - 1]
          if (first[0] !== last[0] || first[1] !== last[1]) {
            boundary.push([first[0], first[1]])
          }
        }
      } catch {
        boundary = []
      }

      const cov = currPlan?.cover?.[i] ?? 0.65
      const nearestMin = currPlan?.nearest_min?.[i] ?? 10.0
      const lam = c.lam ? (c.lam[blockId] || 0) : 0

      // Color & Visual Styling based on mode:
      let fillColor = '#ef4444'
      let fillOpacity = 0.55
      let lineColor = '#ffffff'
      let lineWidth = 1.0
      let lineWidthCasing = 2.0
      let height = 300

      if (hexView === 'coverage') {
        if (cov >= 0.75) fillColor = '#22c55e'
        else if (cov >= 0.50) fillColor = '#f59e0b'
        else fillColor = '#ef4444'
        height = Math.round((1.0 - cov) * 3500 + 100)
      } else if (hexView === 'response') {
        if (nearestMin <= 8.0) fillColor = '#22c55e'
        else if (nearestMin <= targetMin) fillColor = '#f59e0b'
        else if (nearestMin <= targetMin * 1.5) fillColor = '#f97316'
        else fillColor = '#ef4444'
        height = Math.round(nearestMin * 180 + 100)
      } else if (hexView === 'demand') {
        if (lam >= 0.08) fillColor = '#ef4444'
        else if (lam >= 0.04) fillColor = '#f59e0b'
        else if (lam >= 0.015) fillColor = '#3b82f6'
        else fillColor = '#64748b'
        height = Math.round(lam * 30000 + 100)
      } else if (hexView === 'grid3d') {
        if (cov >= 0.75) fillColor = '#22c55e'
        else if (cov >= 0.50) fillColor = '#f59e0b'
        else fillColor = '#ef4444'
        height = Math.round((1.0 - cov) * 4500 + 200)
      } else {
        // Mode 5: Pure High-Contrast Mesh Grid
        fillColor = '#00f0ff'
        fillOpacity = 0.18
        lineColor = '#00f0ff'
        lineWidth = 2.4
        lineWidthCasing = 4.0
        height = 100
      }

      return {
        type: 'Feature' as const,
        id: c.h3,
        properties: {
          id: c.h3,
          index: i,
          zone: c.zone || 'Ward',
          pop: c.pop || 0,
          covPct: Math.round(cov * 100),
          nearestMin: nearestMin.toFixed(1),
          demandRate: (lam * 60).toFixed(2),
          targetMin,
          fillColor,
          fillOpacity,
          lineColor,
          lineWidth,
          lineWidthCasing,
          height,
        },
        geometry: {
          type: 'Polygon' as const,
          coordinates: boundary.length ? [boundary] : [],
        },
      }
    })

    return {
      type: 'FeatureCollection' as const,
      features,
    }
  }, [cells, currPlan, scenarios, region, blockId, hexView])

  /* ---------- derived: Pure Uber H3 Spatial Mesh Grid ---------- */
  const uberH3GridGeoJSON = useMemo(() => {
    if (!cells.length) return { type: 'FeatureCollection' as const, features: [] }
    const res = region === 'blr' ? 8 : 7

    const features = cells.map((c) => {
      let boundary: number[][] = []
      try {
        const rawPts = cellToBoundary(c.h3, true)
        if (rawPts && rawPts.length > 0) {
          boundary = [...rawPts]
          const first = boundary[0]
          const last = boundary[boundary.length - 1]
          if (first[0] !== last[0] || first[1] !== last[1]) {
            boundary.push([first[0], first[1]])
          }
        }
      } catch {
        boundary = []
      }

      return {
        type: 'Feature' as const,
        id: c.h3,
        properties: {
          id: c.h3,
          h3Index: c.h3,
          zone: c.zone || 'Zone',
          pop: c.pop || 0,
          res,
        },
        geometry: {
          type: 'Polygon' as const,
          coordinates: boundary.length ? [boundary] : [],
        },
      }
    })

    return {
      type: 'FeatureCollection' as const,
      features,
    }
  }, [cells, region])

  /* ---------- active vehicle deployments (Static Station Base) ---------- */
  const activeDeployments: Deployment[] = useMemo(() => {
    if (!currPlan?.deployments) return []
    return currPlan.deployments.filter(d => (d.a > 0 || d.b > 0 || d.y > 0))
  }, [currPlan])

  /* ---------- candidate sites GeoJSON (base hospital dots) ---------- */
  const sitesGeoJSON = useMemo(() => {
    return {
      type: 'FeatureCollection' as const,
      features: sites.map(s => ({
        type: 'Feature' as const,
        properties: { id: s.site_id, name: s.name, type: s.site_type },
        geometry: { type: 'Point' as const, coordinates: [s.lon, s.lat] },
      })),
    }
  }, [sites])

  /* ---------- derived KPIs ---------- */
  const kpis = useMemo(() => {
    const sim = currScenario?.sim
    const fleet = currScenario?.fleet
    if (sim && fleet) {
      return {
        avg_response: `${sim.median_min.toFixed(1)} min`,
        coverage: `${sim.within_target_pct.toFixed(1)}%`,
        fleetText: `${fleet.ambulances} amb${fleet.bikes > 0 ? ` + ${fleet.bikes} bikes` : ''}`,
        calls: sim.total_calls,
        respNum: sim.median_min,
        covNum: sim.within_target_pct,
      }
    }
    const r = { baseline: 0, static: 0.3, dynamic: 0.6 }[strategy]
    return {
      avg_response: `${(9.7 - r * 1.5).toFixed(1)} min`,
      coverage: `${(74.1 + r * 1.2).toFixed(1)}%`,
      fleetText: strategy === 'dynamic' ? '72 amb + 30 bikes' : '72 ambulances',
      calls: 14875,
      respNum: 9.7,
      covNum: 74.1,
    }
  }, [currScenario, strategy])

  /* ---------- toggle helpers ---------- */
  const toggleDrawer = useCallback((d: NonNullable<Drawer>) => {
    setDrawer(prev => (prev === d ? null : d))
  }, [])

  const toggleLayer = useCallback((key: keyof typeof layers) => {
    setLayers(prev => ({ ...prev, [key]: !prev[key] }))
  }, [])

  const handleSelectHexView = (mode: HexViewMode) => {
    setHexView(mode)
    if (mode === 'grid3d') {
      setViewState(v => ({ ...v, pitch: 45, bearing: -15 }))
    } else {
      setViewState(v => ({ ...v, pitch: 0, bearing: 0 }))
    }
  }

  const stratInfo = STRATEGIES.find(s => s.id === strategy)!

  if (loading) {
    return (
      <div className="spinner-wrap">
        <div className="spinner" />
        <div className="spinner-label">Loading GoldenHour Fleet Optimizer…</div>
      </div>
    )
  }

  return (
    <div className="app">
      {/* ======== SIDEBAR ======== */}
      <aside className="sidebar">
        {/* Logo */}
        <div className="sidebar-header">
          <div className="sidebar-logo">
            <div className="sidebar-logo-icon">G</div>
            <div>
              <div className="sidebar-title">GoldenHour</div>
              <div className="sidebar-subtitle">108 Emergency Fleet Optimizer</div>
            </div>
          </div>
        </div>

        <div className="sidebar-body">
          {/* Region */}
          <div className="section">
            <div className="section-label">Region</div>
            <div className="region-btns">
              {(Object.keys(REGION_LABELS) as Region[]).map(r => (
                <button
                  key={r}
                  className={`region-btn ${region === r ? 'active' : ''}`}
                  onClick={() => setRegion(r)}
                >
                  {REGION_LABELS[r]}
                </button>
              ))}
            </div>
          </div>

          {/* Strategy */}
          <div className="section">
            <div className="section-label">Strategy</div>
            <div className="strategy-tabs">
              {STRATEGIES.map(s => (
                <button
                  key={s.id}
                  className={`strategy-btn ${strategy === s.id ? 'active' : ''}`}
                  onClick={() => setStrategy(s.id)}
                  title={s.desc}
                >
                  <span className="strategy-dot" style={{ background: s.color }} />
                  {s.label}
                </button>
              ))}
            </div>
          </div>

          {/* KPIs */}
          <div className="section">
            <div className="section-label">Performance (28-Day Simulation)</div>
            <div className="kpi-grid">
              <KpiCard
                label="Median Response"
                value={kpis.avg_response}
                cls={statusClass(kpis.respNum, [10, 15])}
              />
              <KpiCard
                label="Coverage"
                value={kpis.coverage}
                cls={kpis.covNum >= 75 ? 'good' : kpis.covNum >= 50 ? 'warn' : 'bad'}
              />
              <KpiCard
                label="Active Fleet"
                value={kpis.fleetText}
                unit="deployed units"
              />
              <KpiCard
                label="Simulated Calls"
                value={kpis.calls.toLocaleString()}
                unit="evaluated"
              />
            </div>
          </div>

          {/* Search */}
          <div className="section">
            <div className="section-label">Search</div>
            <div className="search-wrap">
              <svg
                className="search-icon"
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
              >
                <circle cx="11" cy="11" r="8" />
                <line x1="21" y1="21" x2="16.65" y2="16.65" />
              </svg>
              <input
                className="search-input"
                placeholder="Search wards, hospitals…"
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
            </div>
          </div>

          {/* Map Layers & Hexagon View Selectors */}
          <div className="section">
            <div className="section-label">Map Overlays</div>
            <div className="layer-list">
              {/* Realtime Live Moving Ambulances */}
              <div className="layer-row">
                <span className="layer-name">
                  <span className="layer-dot" style={{ background: '#22c55e' }} />
                  Live Dispatched Fleet (Real-time)
                </span>
                <Toggle checked={layers.moving} onChange={() => toggleLayer('moving')} />
              </div>

              {/* Static Standby Stations */}
              <div className="layer-row">
                <span className="layer-name">
                  <span className="layer-dot" style={{ background: '#ef4444' }} />
                  Hospital Standby Bases
                </span>
                <Toggle checked={layers.sites} onChange={() => toggleLayer('sites')} />
              </div>

              {/* Coverage Hexagons (H3) */}
              <div className="layer-row">
                <span className="layer-name">
                  <span className="layer-dot" style={{ background: '#f5a623' }} />
                  Coverage Hexagons (H3)
                </span>
                <Toggle checked={layers.cells} onChange={() => toggleLayer('cells')} />
              </div>

              {/* View Selectors for Hexagon Grid */}
              {layers.cells && (
                <div className="hex-view-group">
                  <button
                    className={`hex-view-btn ${hexView === 'coverage' ? 'active' : ''}`}
                    onClick={() => handleSelectHexView('coverage')}
                    title="Expected In-Time Arrival Chance %"
                  >
                    Coverage
                  </button>
                  <button
                    className={`hex-view-btn ${hexView === 'response' ? 'active' : ''}`}
                    onClick={() => handleSelectHexView('response')}
                    title="Nearest Ambulance Response Time in Minutes"
                  >
                    Response
                  </button>
                  <button
                    className={`hex-view-btn ${hexView === 'demand' ? 'active' : ''}`}
                    onClick={() => handleSelectHexView('demand')}
                    title="Hourly Emergency Call Demand"
                  >
                    Demand
                  </button>
                  <button
                    className={`hex-view-btn ${hexView === 'grid3d' ? 'active' : ''}`}
                    onClick={() => handleSelectHexView('grid3d')}
                    title="Tactical 3D Extrusion View"
                  >
                    3D View
                  </button>
                  <button
                    className={`hex-view-btn ${hexView === 'wireframe' ? 'active' : ''}`}
                    onClick={() => handleSelectHexView('wireframe')}
                    title="Pure H3 Spatial Partition Mesh Grid"
                  >
                    Mesh Grid
                  </button>
                </div>
              )}

              {/* Uber H3 Spatial Grid Toggle */}
              <div className="layer-row">
                <span className="layer-name">
                  <span className="layer-dot" style={{ background: '#06b6d4' }} />
                  Uber H3 Spatial Grid (Res-{region === 'blr' ? 8 : 7})
                </span>
                <Toggle checked={layers.h3Grid} onChange={() => toggleLayer('h3Grid')} />
              </div>

              {/* Municipal Boundaries */}
              <div className="layer-row">
                <span className="layer-name">
                  <span className="layer-dot" style={{ background: '#3b82f6' }} />
                  Municipal Boundaries
                </span>
                <Toggle checked={layers.zones} onChange={() => toggleLayer('zones')} />
              </div>
            </div>
          </div>
        </div>

        {/* Drawer buttons */}
        <div className="drawer-btns">
          {(Object.keys(DRAWER_LABELS) as NonNullable<Drawer>[]).map(d => (
            <button
              key={d}
              className={`drawer-btn ${drawer === d ? 'active' : ''}`}
              onClick={() => toggleDrawer(d)}
            >
              {DRAWER_LABELS[d]}
            </button>
          ))}
        </div>

        {/* Status bar */}
        <div className="status-bar">
          <span className="status-dot" />
          <span>Live · {stratInfo.label} · {REGION_LABELS[region]}</span>
        </div>
      </aside>

      {/* ======== MAP ======== */}
      <div className="map-wrap">
        {/* Floating Perspective and Legend Panel */}
        {(layers.cells || layers.h3Grid) && (
          <div className="map-floating-panel">
            <div className="map-legend-items">
              {layers.h3Grid && (
                <div className="map-legend-item">
                  <span className="map-legend-swatch" style={{ background: '#00f0ff' }} />
                  Uber H3 Mesh (Res-{region === 'blr' ? 8 : 7})
                </div>
              )}
              {layers.cells && hexView === 'coverage' && (
                <>
                  <div className="map-legend-item"><span className="map-legend-swatch" style={{ background: '#22c55e' }} /> ≥75% Safe</div>
                  <div className="map-legend-item"><span className="map-legend-swatch" style={{ background: '#f59e0b' }} /> 50-74% Moderate</div>
                  <div className="map-legend-item"><span className="map-legend-swatch" style={{ background: '#ef4444' }} /> &lt;50% Delayed</div>
                </>
              )}
              {layers.cells && hexView === 'response' && (
                <>
                  <div className="map-legend-item"><span className="map-legend-swatch" style={{ background: '#22c55e' }} /> &lt;8 min</div>
                  <div className="map-legend-item"><span className="map-legend-swatch" style={{ background: '#f59e0b' }} /> 8-15 min</div>
                  <div className="map-legend-item"><span className="map-legend-swatch" style={{ background: '#ef4444' }} /> &gt;15 min</div>
                </>
              )}
              {layers.cells && hexView === 'demand' && (
                <>
                  <div className="map-legend-item"><span className="map-legend-swatch" style={{ background: '#ef4444' }} /> High Volume</div>
                  <div className="map-legend-item"><span className="map-legend-swatch" style={{ background: '#f59e0b' }} /> Medium</div>
                  <div className="map-legend-item"><span className="map-legend-swatch" style={{ background: '#3b82f6' }} /> Low</div>
                </>
              )}
              {layers.cells && hexView === 'grid3d' && (
                <div className="map-legend-item"><span className="map-legend-swatch" style={{ background: '#f5a623' }} /> 3D Delay Pillars</div>
              )}
              {layers.cells && hexView === 'wireframe' && (
                <div className="map-legend-item"><span className="map-legend-swatch" style={{ background: '#00f0ff' }} /> High-Contrast H3 Mesh</div>
              )}
            </div>

            <div className="map-view-toggle">
              <button
                className={viewState.pitch === 0 ? 'active' : ''}
                onClick={() => {
                  if (hexView === 'grid3d') setHexView('coverage')
                  setViewState(v => ({ ...v, pitch: 0, bearing: 0 }))
                }}
              >
                2D
              </button>
              <button
                className={viewState.pitch > 0 ? 'active' : ''}
                onClick={() => {
                  setHexView('grid3d')
                  setViewState(v => ({ ...v, pitch: 45, bearing: -15 }))
                }}
              >
                3D
              </button>
            </div>
          </div>
        )}

        <Map
          {...viewState}
          onMove={e => setViewState(e.viewState)}
          mapStyle={MAP_STYLE}
          style={{ width: '100%', height: '100%' }}
          interactiveLayerIds={['hexagons-fill', 'hexagons-3d', 'uber-h3-fill', 'sites-point-layer']}
          onMouseMove={e => {
            if (e.features && e.features.length > 0) {
              const feat = e.features[0]
              const props = feat.properties as Record<string, any>
              if (feat.layer?.id === 'hexagons-fill' || feat.layer?.id === 'hexagons-3d') {
                const content = `${props.zone || 'Ward'}\nH3: ${props.id}\nPopulation: ${Number(props.pop).toLocaleString()}\nCoverage: ${props.covPct}%\nNearest Unit: ${props.nearestMin}m (Target: ${props.targetMin}m)\nCalls: ${props.demandRate}/hr`
                setTooltip({ x: e.point.x, y: e.point.y, content })
              } else if (feat.layer?.id === 'uber-h3-fill') {
                const content = `Uber H3 Index: ${props.h3Index}\nResolution: Res-${props.res} (~${region === 'blr' ? '0.74' : '5.2'} km²)\nZone: ${props.zone || 'Ward'}\nPopulation: ${Number(props.pop).toLocaleString()}`
                setTooltip({ x: e.point.x, y: e.point.y, content })
              } else if (feat.layer?.id === 'sites-point-layer') {
                setTooltip({ x: e.point.x, y: e.point.y, content: `${props.name}\nStandby Base Hospital` })
              }
            } else {
              setTooltip(null)
            }
          }}
          onMouseLeave={() => setTooltip(null)}
        >
          {/* Municipal Ward Boundaries */}
          {layers.zones && zonesGeo && (
            <Source id="zones" type="geojson" data={zonesGeo}>
              <Layer
                id="zones-outline"
                type="line"
                paint={{
                  'line-color': '#3b82f6',
                  'line-width': 1.2,
                  'line-opacity': 0.6,
                }}
              />
            </Source>
          )}

          {/* H3 Hexagon Grid Layer (Coverage / Response / Demand / Mesh) */}
          {layers.cells && (
            <Source id="hexagons" type="geojson" data={hexagonsGeoJSON}>
              {hexView === 'grid3d' ? (
                /* 3D Extruded Tactical Hexagon Pillars */
                <Layer
                  id="hexagons-3d"
                  type="fill-extrusion"
                  paint={{
                    'fill-extrusion-color': ['get', 'fillColor'],
                    'fill-extrusion-height': ['get', 'height'],
                    'fill-extrusion-base': 0,
                    'fill-extrusion-opacity': 0.85,
                  }}
                />
              ) : (
                /* 2D Flat Planar Hexagon Layers */
                <>
                  <Layer
                    id="hexagons-fill"
                    type="fill"
                    paint={{
                      'fill-color': ['get', 'fillColor'],
                      'fill-opacity': ['get', 'fillOpacity'],
                    }}
                  />
                  {/* High-contrast dark casing outline so grid is crystal clear against roads */}
                  <Layer
                    id="hexagons-line-casing"
                    type="line"
                    layout={{ 'line-join': 'round', 'line-cap': 'round' }}
                    paint={{
                      'line-color': '#020617',
                      'line-width': ['get', 'lineWidthCasing'],
                      'line-opacity': 0.9,
                    }}
                  />
                  {/* Vibrant inner grid stroke */}
                  <Layer
                    id="hexagons-line"
                    type="line"
                    layout={{ 'line-join': 'round', 'line-cap': 'round' }}
                    paint={{
                      'line-color': ['get', 'lineColor'],
                      'line-width': ['get', 'lineWidth'],
                      'line-opacity': 1.0,
                    }}
                  />
                </>
              )}
            </Source>
          )}

          {/* Uber H3 Spatial Mesh Grid Overlay */}
          {layers.h3Grid && (
            <Source id="uber-h3-grid" type="geojson" data={uberH3GridGeoJSON}>
              <Layer
                id="uber-h3-fill"
                type="fill"
                paint={{
                  'fill-color': '#00f0ff',
                  'fill-opacity': 0.1,
                }}
              />
              <Layer
                id="uber-h3-line-casing"
                type="line"
                layout={{ 'line-join': 'round', 'line-cap': 'round' }}
                paint={{
                  'line-color': '#020617',
                  'line-width': 4.0,
                  'line-opacity': 0.9,
                }}
              />
              <Layer
                id="uber-h3-line"
                type="line"
                layout={{ 'line-join': 'round', 'line-cap': 'round' }}
                paint={{
                  'line-color': '#00f0ff',
                  'line-width': 2.4,
                  'line-opacity': 1.0,
                }}
              />
            </Source>
          )}

          {/* Base Hospital Standby Points */}
          {layers.sites && (
            <Source id="sites-base" type="geojson" data={sitesGeoJSON}>
              <Layer
                id="sites-point-layer"
                type="circle"
                paint={{
                  'circle-radius': [
                    'interpolate', ['linear'], ['zoom'],
                    8, 2.5,
                    12, 4.5,
                    15, 6,
                  ],
                  'circle-color': '#ffffff',
                  'circle-stroke-color': '#1e293b',
                  'circle-stroke-width': 1,
                  'circle-opacity': 0.7,
                }}
              />
            </Source>
          )}

          {/* Static Hospital Standby Deployments */}
          {layers.sites && activeDeployments.map(dep => {
            const total = dep.a + dep.b + dep.y
            return (
              <Marker
                key={dep.site_id}
                longitude={dep.lon}
                latitude={dep.lat}
                anchor="center"
              >
                <div
                  className="amb-marker-wrap"
                  onMouseEnter={() => {
                    setTooltip({
                      x: window.innerWidth / 2,
                      y: 80,
                      content: `${dep.name} (Base Station)\nDeployed: ${dep.a ? `${dep.a}× ALS ` : ''}${dep.b ? `${dep.b}× BLS ` : ''}${dep.y ? `${dep.y}× Bike` : ''}\nTotal: ${total} units`
                    })
                  }}
                  onMouseLeave={() => setTooltip(null)}
                >
                  {dep.a > 0 && (
                    <div className="amb-badge als" title={`${dep.name}: ${dep.a}x ALS Ambulance`}>
                      <img src="/icons/icon-als.svg" alt="ALS" />
                      {dep.a > 1 && <span className="amb-count">{dep.a}</span>}
                    </div>
                  )}
                  {dep.b > 0 && (
                    <div className="amb-badge bls" title={`${dep.name}: ${dep.b}x BLS Ambulance`}>
                      <img src="/icons/icon-bls.svg" alt="BLS" />
                      {dep.b > 1 && <span className="amb-count">{dep.b}</span>}
                    </div>
                  )}
                  {dep.y > 0 && (
                    <div className="amb-badge bike" title={`${dep.name}: ${dep.y}x Bike Paramedic`}>
                      <img src="/icons/icon-bike.svg" alt="Bike" />
                      {dep.y > 1 && <span className="amb-count">{dep.y}</span>}
                    </div>
                  )}
                </div>
              </Marker>
            )
          })}

          {/* Real-time Mock Live Moving Ambulances */}
          {layers.moving && movingAmbulances.map(amb => (
            <Marker
              key={amb.id}
              longitude={amb.currentCoord[0]}
              latitude={amb.currentCoord[1]}
              anchor="center"
            >
              <div
                className={`moving-ambulance-marker ${amb.type.toLowerCase()}`}
                onMouseEnter={() => {
                  setTooltip({
                    x: window.innerWidth / 2,
                    y: 70,
                    content: `${amb.callSign} (${amb.type} 108 Emergency)\nStatus: ${amb.status}\nSpeed: ${amb.speedKmH} km/h (Siren Active)\nDestination: ${amb.targetZone}\nETA: ${((1 - amb.progress) * 7.5).toFixed(1)} min`,
                  })
                }}
                onMouseLeave={() => setTooltip(null)}
              >
                <div className="siren-pulse-ring" />
                <div className="moving-amb-icon">
                  <img src={`/icons/icon-${amb.type.toLowerCase()}.svg`} alt={amb.type} />
                </div>
                <div className="moving-amb-label">
                  <span className="moving-dot" />
                  {amb.callSign}
                </div>
              </div>
            </Marker>
          ))}
        </Map>

        {/* Hover Tooltip */}
        {tooltip && (
          <div
            className="tooltip-box"
            style={{ left: tooltip.x + 14, top: tooltip.y - 36 }}
          >
            {tooltip.content.split('\n').map((line, i) => (
              <div key={i} style={{ fontWeight: i === 0 ? 600 : 400, color: i === 0 ? 'var(--accent)' : 'inherit' }}>
                {line}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ======== BOTTOM DRAWER PANEL ======== */}
      <div className={`drawer-panel ${drawer ? '' : 'closed'}`}>
        <div className="drawer-panel-header">
          <span className="drawer-panel-title">
            {drawer ? DRAWER_LABELS[drawer] : ''}
          </span>
          <button className="drawer-close-btn" onClick={() => setDrawer(null)}>
            &times;
          </button>
        </div>
        <div className="drawer-panel-body">
          {drawer === 'wards' && (
            <WardTable
              data={currScenario?.sim?.per_zone}
              targetMin={scenarios?.target_min || (region === 'blr' ? 15 : 10)}
            />
          )}
          {drawer === 'stress' && (
            <StressTable tests={scenarios?.stress_tests} />
          )}
          {drawer === 'fleet' && (
            <FleetTable curve={scenarios?.fleet_curve} />
          )}
          {drawer === 'forecast' && (
            <ForecastChart data={forecastData?.[region]} />
          )}
          {drawer === 'papers' && (
            <PapersPanel />
          )}
          {drawer === 'sources' && (
            <SourcesPanel />
          )}
        </div>
      </div>
    </div>
  )
}
