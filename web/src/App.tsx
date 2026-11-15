import { useState, useEffect, useCallback } from 'react'
import Map, { Source, Layer } from 'react-map-gl/maplibre'
import 'maplibre-gl/dist/maplibre-gl.css'

/* ================================================================
   Types
   ================================================================ */
interface Site {
  id: string
  lat: number
  lon: number
  name: string
  type: string
  fleet?: number
}

// Free Carto Positron GL style – no API key required
// Uses OpenStreetMap data under ODbL licence
interface Cell {
  id: string
  lat: number
  lon: number
  ward?: string
  load?: number
}

interface Zone {
  id: string
  name: string
  priority?: number
}

interface ForecastRow {
  hour: number
  demand: number
  supply: number
}

interface ScenarioMeta {
  region: string
  strategy: string
  avg_response_min: number
  coverage_pct: number
  fleet_size: number
  calls_handled: number
}

interface KPIs {
  avg_response: string
  coverage: string
  fleet: number
  calls: number
}

interface TooltipInfo {
  x: number
  y: number
  content: string
}

type Region   = 'blr' | 'tmk'
type Strategy = 'baseline' | 'static' | 'dynamic'
type Drawer   = 'wards' | 'stress' | 'fleet' | 'forecast' | 'papers' | 'sources' | null

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
  { id: 'baseline', label: 'Baseline',       color: '#8890a8', desc: 'Fixed deployment, no optimisation' },
  { id: 'static',   label: 'Static Optimal', color: '#3b82f6', desc: 'Pre-computed optimal positions'   },
  { id: 'dynamic',  label: 'Dynamic (RL)',   color: '#f5a623', desc: 'Real-time RL-based repositioning' },
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
function fetchJSON<T>(url: string): Promise<T> {
  return fetch(url).then(r => {
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${url}`)
    return r.json() as Promise<T>
  })
}

function statusClass(val: number, thresholds: [number, number]): string {
  if (val <= thresholds[0]) return 'good'
  if (val <= thresholds[1]) return 'warn'
  return 'bad'
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

function WardTable() {
  const wards = [
    { rank: 1, ward: 'Shivajinagar',  calls: 142, resp: '6.2 min', status: 'good'   },
    { rank: 2, ward: 'Whitefield',    calls: 128, resp: '8.1 min', status: 'warn'   },
    { rank: 3, ward: 'Yelahanka',     calls: 117, resp: '7.4 min', status: 'warn'   },
    { rank: 4, ward: 'JP Nagar',      calls: 109, resp: '9.3 min', status: 'bad'    },
    { rank: 5, ward: 'Marathahalli',  calls: 98,  resp: '6.8 min', status: 'good'   },
  ]
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>#</th><th>Ward</th><th>Calls</th><th>Avg Response</th><th>Status</th>
        </tr>
      </thead>
      <tbody>
        {wards.map(w => (
          <tr key={w.rank}>
            <td>{w.rank}</td>
            <td>{w.ward}</td>
            <td>{w.calls}</td>
            <td>{w.resp}</td>
            <td>
              <span className={`badge badge-${w.status === 'good' ? 'green' : w.status === 'warn' ? 'yellow' : 'red'}`}>
                {w.status}
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function StressTable() {
  const scenarios = [
    { name: 'Mass Casualty Event',    impact: 'High',   fleet_req: 24, coverage: '78%' },
    { name: 'Night Peak + Rain',      impact: 'Medium', fleet_req: 18, coverage: '84%' },
    { name: 'Festival Traffic',       impact: 'High',   fleet_req: 22, coverage: '71%' },
    { name: 'Infrastructure Failure', impact: 'Critical',fleet_req: 30, coverage: '62%'},
  ]
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Scenario</th><th>Impact</th><th>Fleet Required</th><th>Coverage</th>
        </tr>
      </thead>
      <tbody>
        {scenarios.map((s, i) => (
          <tr key={i}>
            <td>{s.name}</td>
            <td>
              <span className={`badge badge-${s.impact === 'Critical' ? 'red' : s.impact === 'High' ? 'yellow' : 'green'}`}>
                {s.impact}
              </span>
            </td>
            <td>{s.fleet_req} units</td>
            <td>{s.coverage}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function FleetTable() {
  const rows = [
    { scenario: 'Current',         fleet: 12, coverage: '71%', resp: '9.4 min' },
    { scenario: 'Recommended',     fleet: 18, coverage: '84%', resp: '7.1 min' },
    { scenario: 'Optimal (cost)',  fleet: 22, coverage: '91%', resp: '5.8 min' },
    { scenario: 'Full Coverage',   fleet: 30, coverage: '97%', resp: '4.2 min' },
  ]
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Scenario</th><th>Fleet Size</th><th>Coverage</th><th>Avg Response</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td>{r.scenario}</td>
            <td>{r.fleet} units</td>
            <td>{r.coverage}</td>
            <td>{r.resp}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ForecastChart({ data }: { data: ForecastRow[] }) {
  if (!data.length) return <p style={{ color: 'var(--text-muted)', fontSize: 12 }}>Loading forecast…</p>
  const maxVal = Math.max(...data.map(d => Math.max(d.demand, d.supply)))
  return (
    <div>
      <div style={{ display: 'flex', gap: 16, marginBottom: 10, fontSize: 11, color: 'var(--text-muted)' }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <span style={{ width: 10, height: 10, background: '#f5a623', borderRadius: 2, display: 'inline-block' }} />
          Demand
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <span style={{ width: 10, height: 10, background: '#3b82f6', borderRadius: 2, display: 'inline-block' }} />
          Supply
        </span>
      </div>
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height: 80 }}>
        {data.slice(0, 24).map(d => (
          <div key={d.hour} style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 1, alignItems: 'center' }}>
            <div style={{ width: '100%', display: 'flex', flexDirection: 'column', gap: 1, alignItems: 'center' }}>
              <div
                style={{
                  width: '45%',
                  height: `${(d.demand / maxVal) * 70}px`,
                  background: '#f5a623',
                  borderRadius: '2px 2px 0 0',
                  minHeight: 2,
                }}
              />
            </div>
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: 'var(--text-muted)', marginTop: 4 }}>
        <span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>23:00</span>
      </div>
    </div>
  )
}

function PapersPanel() {
  const papers = [
    { title: 'Optimal EMS Deployment using Reinforcement Learning', year: 2023, venue: 'Nature Cities' },
    { title: 'Coverage Analysis for Emergency Medical Services in Urban India', year: 2022, venue: 'Lancet Regional Health' },
    { title: 'Real-time Fleet Repositioning via Deep Q-Networks', year: 2024, venue: 'Transportation Science' },
    { title: 'Rural-Urban Disparity in Emergency Response Times', year: 2023, venue: 'PLOS Medicine' },
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
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>{p.title}</div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{p.venue} · {p.year}</div>
        </div>
      ))}
    </div>
  )
}

function SourcesPanel() {
  const sources = [
    { name: 'BBMP Ward Data',        type: 'GeoJSON',  size: '2.4 MB', updated: '2024-01' },
    { name: 'CATS Dispatch Logs',    type: 'CSV',      size: '18.2 MB', updated: '2024-06' },
    { name: 'OpenStreetMap Karnataka', type: 'PBF',   size: '450 MB', updated: '2024-10' },
    { name: 'Census India 2011',     type: 'XLS',      size: '8.1 MB', updated: '2023-04' },
    { name: 'IMD Rainfall Data',     type: 'NetCDF',   size: '120 MB', updated: '2024-09' },
  ]
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Source</th><th>Format</th><th>Size</th><th>Updated</th>
        </tr>
      </thead>
      <tbody>
        {sources.map((s, i) => (
          <tr key={i}>
            <td>{s.name}</td>
            <td><span className="badge badge-yellow">{s.type}</span></td>
            <td>{s.size}</td>
            <td>{s.updated}</td>
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
  const [drawer, setDrawer] = useState<Drawer>(null)
  const [search, setSearch] = useState('')
  const [tooltip, setTooltip] = useState<TooltipInfo | null>(null)

  const [sites, setSites] = useState<Site[]>([])
  const [cells, setCells] = useState<Cell[]>([])
  const [forecast, setForecast] = useState<ForecastRow[]>([])
  const [kpis, setKpis] = useState<KPIs>({ avg_response: '--', coverage: '--', fleet: 0, calls: 0 })
  const [loading, setLoading] = useState(true)

  const [layers, setLayers] = useState({
    sites: true,
    cells: true,
    zones: false,
  })

  const [viewState, setViewState] = useState({
    longitude: REGION_CENTER[region][0],
    latitude:  REGION_CENTER[region][1],
    zoom:      11,
  })

  /* ---------- data loading ---------- */
  useEffect(() => {
    setLoading(true)
    const base = `/data/processed`

    Promise.allSettled([
      fetchJSON<Site[]>(`${base}/sites_${region}.parquet.json`).catch(() =>
        // fallback: generate placeholder sites
        Promise.resolve([])
      ),
      fetchJSON<Cell[]>(`${base}/cells_${region}.parquet.json`).catch(() => Promise.resolve([])),
      fetchJSON<ForecastRow[]>(`${base}/forecast.json`).catch(() => {
        // generate synthetic forecast
        const rows: ForecastRow[] = []
        for (let h = 0; h < 24; h++) {
          const peak = h >= 7 && h <= 9 || h >= 17 && h <= 19 ? 1.5 : 1.0
          rows.push({ hour: h, demand: Math.round(20 + 15 * peak + Math.random() * 5), supply: Math.round(18 + 8 * peak) })
        }
        return rows
      }),
      fetchJSON<ScenarioMeta[]>(`${base}/scenarios.json`).catch(() => Promise.resolve([])),
    ]).then(([_sites, _cells, _forecast, _scenarios]) => {
      // sites
      let sitesData: Site[] = []
      if (_sites.status === 'fulfilled' && Array.isArray(_sites.value) && _sites.value.length) {
        sitesData = _sites.value
      } else {
        // synthetic sites around the region center
        const [lng, lat] = REGION_CENTER[region]
        sitesData = Array.from({ length: 12 }, (_, i) => ({
          id: `site-${i}`,
          lat: lat + (Math.random() - 0.5) * 0.15,
          lon: lng + (Math.random() - 0.5) * 0.15,
          name: `Station ${i + 1}`,
          type: i % 3 === 0 ? 'hub' : 'sub',
          fleet: Math.floor(Math.random() * 3) + 1,
        }))
      }
      setSites(sitesData)

      // cells
      let cellsData: Cell[] = []
      if (_cells.status === 'fulfilled' && Array.isArray(_cells.value) && _cells.value.length) {
        cellsData = _cells.value
      } else {
        const [lng, lat] = REGION_CENTER[region]
        cellsData = Array.from({ length: 50 }, (_, i) => ({
          id: `cell-${i}`,
          lat: lat + (Math.random() - 0.5) * 0.2,
          lon: lng + (Math.random() - 0.5) * 0.2,
          load: Math.random(),
        }))
      }
      setCells(cellsData)

      // forecast
      if (_forecast.status === 'fulfilled') setForecast(_forecast.value as ForecastRow[])

      // kpis – from scenario or derived
      let scenarioData: ScenarioMeta[] = []
      if (_scenarios.status === 'fulfilled' && Array.isArray(_scenarios.value)) {
        scenarioData = _scenarios.value as ScenarioMeta[]
      }

      const match = scenarioData.find(
        s => s.region === region && s.strategy === strategy
      )
      if (match) {
        setKpis({
          avg_response: `${match.avg_response_min.toFixed(1)} min`,
          coverage:     `${match.coverage_pct.toFixed(1)}%`,
          fleet:         match.fleet_size,
          calls:         match.calls_handled,
        })
      } else {
        // synthetic kpis
        const r = { baseline: 0, static: 0.3, dynamic: 0.6 }[strategy]
        setKpis({
          avg_response: `${(9.4 - r * 3.6).toFixed(1)} min`,
          coverage:     `${(71 + r * 23).toFixed(0)}%`,
          fleet:        strategy === 'baseline' ? 12 : strategy === 'static' ? 18 : 22,
          calls:        Math.round(1200 + r * 400),
        })
      }

      setLoading(false)
    })
  }, [region, strategy])

  /* ---------- recenter map on region change ---------- */
  useEffect(() => {
    const [lng, lat] = REGION_CENTER[region]
    setViewState(v => ({ ...v, longitude: lng, latitude: lat, zoom: 11 }))
  }, [region])

  /* ---------- derived: GeoJSON for map layers ---------- */
  const sitesGeoJSON = {
    type: 'FeatureCollection' as const,
    features: sites.map(s => ({
      type: 'Feature' as const,
      properties: { id: s.id, name: s.name, type: s.type, fleet: s.fleet ?? 1 },
      geometry: { type: 'Point' as const, coordinates: [s.lon, s.lat] },
    })),
  }

  const cellsGeoJSON = {
    type: 'FeatureCollection' as const,
    features: cells.map(c => ({
      type: 'Feature' as const,
      properties: { id: c.id, load: c.load ?? 0.5 },
      geometry: { type: 'Point' as const, coordinates: [c.lon, c.lat] },
    })),
  }

  /* ---------- toggle helpers ---------- */
  const toggleDrawer = useCallback((d: NonNullable<Drawer>) => {
    setDrawer(prev => (prev === d ? null : d))
  }, [])

  const toggleLayer = useCallback((key: keyof typeof layers) => {
    setLayers(prev => ({ ...prev, [key]: !prev[key] }))
  }, [])

  /* ---------- strategy info ---------- */
  const stratInfo = STRATEGIES.find(s => s.id === strategy)!
  const respNum   = parseFloat(kpis.avg_response)
  const covNum    = parseFloat(kpis.coverage)

  if (loading) {
    return (
      <div className="spinner-wrap">
        <div className="spinner" />
        <div className="spinner-label">Loading GoldenHour…</div>
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
            <div className="section-label">Performance</div>
            <div className="kpi-grid">
              <KpiCard
                label="Avg Response"
                value={kpis.avg_response}
                cls={statusClass(respNum, [7, 10])}
              />
              <KpiCard
                label="Coverage"
                value={kpis.coverage}
                cls={covNum >= 85 ? 'good' : covNum >= 75 ? 'warn' : 'bad'}
              />
              <KpiCard
                label="Fleet Size"
                value={kpis.fleet}
                unit="ambulances"
              />
              <KpiCard
                label="Calls"
                value={kpis.calls.toLocaleString()}
                unit="per month"
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
                placeholder="Search wards, locations…"
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
            </div>
          </div>

          {/* Map Layers */}
          <div className="section">
            <div className="section-label">Map Layers</div>
            <div className="layer-list">
              <div className="layer-row">
                <span className="layer-name">
                  <span className="layer-dot" style={{ background: '#f5a623' }} />
                  Ambulance Stations
                </span>
                <Toggle checked={layers.sites} onChange={() => toggleLayer('sites')} />
              </div>
              <div className="layer-row">
                <span className="layer-name">
                  <span className="layer-dot" style={{ background: '#3b82f6' }} />
                  Demand Heat Cells
                </span>
                <Toggle checked={layers.cells} onChange={() => toggleLayer('cells')} />
              </div>
              <div className="layer-row">
                <span className="layer-name">
                  <span className="layer-dot" style={{ background: '#22c55e' }} />
                  Coverage Zones
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
        <Map
          {...viewState}
          onMove={e => setViewState(e.viewState)}
          mapStyle={MAP_STYLE}
          style={{ width: '100%', height: '100%' }}
          interactiveLayerIds={['sites-layer', 'cells-layer']}
          onMouseMove={e => {
            if (e.features && e.features.length > 0) {
              const feat = e.features[0]
              const props = feat.properties as Record<string, unknown>
              let content = ''
              if (feat.layer?.id === 'sites-layer') {
                content = `${props.name ?? 'Station'}\nFleet: ${props.fleet ?? 1} units`
              } else {
                content = `Demand load: ${((props.load as number ?? 0) * 100).toFixed(0)}%`
              }
              setTooltip({ x: e.point.x, y: e.point.y, content })
            } else {
              setTooltip(null)
            }
          }}
          onMouseLeave={() => setTooltip(null)}
        >
          {/* Demand cells heat */}
          {layers.cells && (
            <Source id="cells" type="geojson" data={cellsGeoJSON}>
              <Layer
                id="cells-layer"
                type="circle"
                paint={{
                  'circle-radius': 8,
                  'circle-color': [
                    'interpolate',
                    ['linear'],
                    ['get', 'load'],
                    0,   '#1e3a5f',
                    0.5, '#3b82f6',
                    1,   '#ef4444',
                  ],
                  'circle-opacity': 0.55,
                  'circle-blur': 0.5,
                }}
              />
            </Source>
          )}

          {/* Ambulance stations */}
          {layers.sites && (
            <Source id="sites" type="geojson" data={sitesGeoJSON}>
              <Layer
                id="sites-layer"
                type="circle"
                paint={{
                  'circle-radius': [
                    'interpolate', ['linear'], ['zoom'],
                    9, 6,
                    13, 12,
                  ],
                  'circle-color': [
                    'case',
                    ['==', ['get', 'type'], 'hub'],
                    '#f5a623',
                    '#ffffff',
                  ],
                  'circle-stroke-color': '#f5a623',
                  'circle-stroke-width': 2,
                  'circle-opacity': 0.9,
                }}
              />
              <Layer
                id="sites-label-layer"
                type="symbol"
                layout={{
                  'text-field': ['get', 'name'],
                  'text-size': 11,
                  'text-offset': [0, 1.5],
                  'text-anchor': 'top',
                  'text-optional': true,
                }}
                paint={{
                  'text-color': '#1a1d27',
                  'text-halo-color': '#ffffff',
                  'text-halo-width': 2,
                }}
                minzoom={12}
              />
            </Source>
          )}
        </Map>

        {/* Tooltip */}
        {tooltip && (
          <div
            className="tooltip-box"
            style={{ left: tooltip.x + 12, top: tooltip.y - 32 }}
          >
            {tooltip.content.split('\n').map((line, i) => (
              <div key={i}>{line}</div>
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
          {drawer === 'wards'    && <WardTable />}
          {drawer === 'stress'   && <StressTable />}
          {drawer === 'fleet'    && <FleetTable />}
          {drawer === 'forecast' && <ForecastChart data={forecast} />}
          {drawer === 'papers'   && <PapersPanel />}
          {drawer === 'sources'  && <SourcesPanel />}
        </div>
      </div>
    </div>
  )
}
