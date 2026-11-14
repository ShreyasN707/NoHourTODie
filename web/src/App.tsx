import { useEffect, useMemo, useState, useRef } from 'react'
import DeckGL from '@deck.gl/react'
import { H3HexagonLayer } from '@deck.gl/geo-layers'
import {
  ScatterplotLayer,
  ArcLayer,
  GeoJsonLayer,
  IconLayer,
  TextLayer,
} from '@deck.gl/layers'
import Map from 'react-map-gl/maplibre'
import 'maplibre-gl/dist/maplibre-gl.css'
import {
  Activity,
  ChevronLeft,
  ChevronRight,
  Pause,
  Play,
  Search,
  X,
} from 'lucide-react'

// Initial map camera views
const INITIAL_VIEW_STATES: Record<string, any> = {
  blr: {
    longitude: 77.5946,
    latitude: 12.9716,
    zoom: 10.6,
    pitch: 25,
    bearing: 0,
    maxZoom: 16,
    minZoom: 8,
  },
  tmk: {
    longitude: 77.101,
    latitude: 13.3422,
    zoom: 8.8,
    pitch: 20,
    bearing: 0,
    maxZoom: 16,
    minZoom: 7,
  },
}

const MAP_STYLE = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json'

const BLOCK_NAMES = [
  'Night (00:00 - 04:00)',
  'Early Morning (04:00 - 08:00)',
  'Morning Peak (08:00 - 12:00)',
  'Afternoon (12:00 - 16:00)',
  'Evening Peak (16:00 - 20:00)',
  'Late Evening (20:00 - 24:00)',
]

export default function App() {
  const [region, setRegion] = useState<'blr' | 'tmk'>('blr')
  const [planType, setPlanType] = useState<'baseline' | 'static' | 'goldenhour'>('goldenhour')
  const [blockId, setBlockId] = useState<number>(4) // Default to Evening Peak (16:00-20:00)
  const [layerMode, setLayerMode] = useState<'cover' | 'nearest' | 'demand' | 'reach'>('cover')
  const [bottomTab, setBottomTab] = useState<'none' | 'wards' | 'stress' | 'fleet' | 'forecast' | 'papers' | 'sources'>('none')

  // Operator usability state
  const [isPlaying, setIsPlaying] = useState<boolean>(false)
  const [searchQuery, setSearchQuery] = useState<string>('')
  const [showSearchResults, setShowSearchResults] = useState<boolean>(false)
  const [selectedStation, setSelectedStation] = useState<any | null>(null)
  const [selectedMove, setSelectedMove] = useState<any | null>(null)
  const [viewState, setViewState] = useState<any>(INITIAL_VIEW_STATES.blr)

  // Layer visibility toggles
  const [showHexagons, setShowHexagons] = useState<boolean>(true)
  const [showVehicles, setShowVehicles] = useState<boolean>(true)
  const [showArcs, setShowArcs] = useState<boolean>(true)
  const [showWards, setShowWards] = useState<boolean>(true)

  // Loaded data
  const [cells, setCells] = useState<any[]>([])
  const [sites, setSites] = useState<any[]>([])
  const [scenarios, setScenarios] = useState<any | null>(null)
  const [zonesGeo, setZonesGeo] = useState<any | null>(null)
  const [forecastData, setForecastData] = useState<any | null>(null)
  const [meta, setMeta] = useState<any | null>(null)

  // Timer reference for auto-play
  const playTimerRef = useRef<any>(null)

  // Handle region switch and fetch
  useEffect(() => {
    setViewState(INITIAL_VIEW_STATES[region])
    setSelectedStation(null)
    setSelectedMove(null)
    setSearchQuery('')

    Promise.all([
      fetch(`/data/cells_${region}.json`).then((r) => r.json()),
      fetch(`/data/sites_${region}.json`).then((r) => r.json()),
      fetch(`/data/scenarios_${region}.json`).then((r) => r.json()),
      fetch('/data/zones.geojson').then((r) => r.json()).catch(() => null),
      fetch('/data/forecast.json').then((r) => r.json()).catch(() => null),
      fetch('/data/meta.json').then((r) => r.json()).catch(() => null),
    ])
      .then(([cellsData, sitesData, scenariosData, zonesData, fcData, metaData]) => {
        setCells(cellsData)
        setSites(sitesData)
        setScenarios(scenariosData)
        if (zonesData) setZonesGeo(zonesData)
        if (fcData) setForecastData(fcData)
        if (metaData) setMeta(metaData)
      })
      .catch((err) => {
        console.error('Failed to load data:', err)
      })
  }, [region])

  // 24-hour Auto-Play Loop
  useEffect(() => {
    if (isPlaying) {
      playTimerRef.current = setInterval(() => {
        setBlockId((prev) => (prev + 1) % 6)
      }, 3000)
    } else {
      if (playTimerRef.current) clearInterval(playTimerRef.current)
    }
    return () => {
      if (playTimerRef.current) clearInterval(playTimerRef.current)
    }
  }, [isPlaying])

  const targetMin = scenarios?.target_min || (region === 'blr' ? 15.0 : 10.0)

  // Active scenario and plan
  const currScenario = scenarios ? scenarios[planType] : null
  const baseScenario = scenarios ? scenarios['baseline'] : null
  const currBlockPlan = currScenario?.plans?.[blockId] || null
  const currSim = currScenario?.sim || null
  const baseSim = baseScenario?.sim || null

  // Delta helpers
  const deltaTarget = currSim && baseSim ? currSim.within_target_pct - baseSim.within_target_pct : 0
  const deltaMedian = currSim && baseSim ? currSim.median_min - baseSim.median_min : 0
  const deltaP90 = currSim && baseSim ? currSim.p90_min - baseSim.p90_min : 0
  const deltaBike = currSim && baseSim ? currSim.first_help_10m_pct - baseSim.first_help_10m_pct : 0

  // Moves for current block in GoldenHour
  const blockMoves = useMemo(() => {
    if (planType !== 'goldenhour' || !currScenario?.moves) return []
    const m = currScenario.moves.find((mv: any) => mv.from_block === blockId)
    return m ? m.moves : []
  }, [planType, currScenario, blockId])

  // Search Results (Wards & Hospitals)
  const searchResults = useMemo(() => {
    if (!searchQuery.trim()) return []
    const q = searchQuery.toLowerCase()
    const results: any[] = []

    // Search Hospitals / Sites
    sites.forEach((s) => {
      if (s.name.toLowerCase().includes(q)) {
        results.push({ type: 'Hospital', name: s.name, lat: s.lat, lon: s.lon, raw: s })
      }
    })

    // Search Wards
    const seenZones = new Set<string>()
    cells.forEach((c) => {
      if (c.zone && c.zone.toLowerCase().includes(q) && !seenZones.has(c.zone)) {
        seenZones.add(c.zone)
        results.push({ type: 'Ward / Taluk', name: c.zone, lat: c.lat, lon: c.lon, raw: c })
      }
    })

    return results.slice(0, 8)
  }, [searchQuery, sites, cells])

  // Select Search Item
  const handleSelectSearch = (item: any) => {
    setViewState({
      longitude: item.lon,
      latitude: item.lat,
      zoom: 13,
      pitch: 35,
      bearing: 0,
      transitionDuration: 1000,
    })
    if (item.type === 'Hospital') {
      setSelectedStation(item.raw)
    }
    setShowSearchResults(false)
    setSearchQuery(item.name)
  }

  // Deployed vehicles data with SVG symbols
  const { vehicleMarkers, vehicleBadges, moveEndpoints } = useMemo(() => {
    if (!currBlockPlan?.deployments) {
      return { vehicleMarkers: [], vehicleBadges: [], moveEndpoints: [] }
    }

    const markers: any[] = []
    const badges: any[] = []

    currBlockPlan.deployments.forEach((d: any) => {
      // ALS Ambulance
      if (d.a > 0) {
        markers.push({
          ...d,
          vType: 'ALS',
          count: d.a,
          iconUrl: '/icons/icon-als.svg',
          color: [239, 68, 68],
          offsetY: d.b > 0 || d.y > 0 ? 0.0008 : 0,
        })
        if (d.a > 1) {
          badges.push({
            lat: d.lat + (d.b > 0 || d.y > 0 ? 0.0016 : 0.0008),
            lon: d.lon,
            text: `${d.a}× ALS`,
            color: [239, 68, 68],
          })
        }
      }

      // BLS Ambulance
      if (d.b > 0) {
        markers.push({
          ...d,
          vType: 'BLS',
          count: d.b,
          iconUrl: '/icons/icon-bls.svg',
          color: [59, 130, 246],
          offsetY: d.a > 0 ? -0.0008 : 0,
        })
        if (d.b > 1) {
          badges.push({
            lat: d.lat + (d.a > 0 ? -0.0016 : 0.0008),
            lon: d.lon,
            text: `${d.b}× BLS`,
            color: [59, 130, 246],
          })
        }
      }

      // Bike Paramedic
      if (d.y > 0) {
        markers.push({
          ...d,
          vType: 'Bike',
          count: d.y,
          iconUrl: '/icons/icon-bike.svg',
          color: [249, 115, 22],
          offsetY: d.a > 0 || d.b > 0 ? 0.0015 : 0,
        })
        if (d.y > 1) {
          badges.push({
            lat: d.lat + 0.0022,
            lon: d.lon,
            text: `${d.y}× Bike`,
            color: [249, 115, 22],
          })
        }
      }
    })

    // Endpoints for moves (Destination Target Rings)
    const endpoints: any[] = blockMoves.map((m: any) => ({
      lat: m.to_lat,
      lon: m.to_lon,
      to_name: m.to_name,
      travel_min: m.travel_min,
    }))

    return { vehicleMarkers: markers, vehicleBadges: badges, moveEndpoints: endpoints }
  }, [currBlockPlan, blockMoves])

  // Deck.gl Layers Definition
  const layers = useMemo(() => {
    if (!cells.length || !currBlockPlan) return []

    // 1. High-Contrast Hexagon Layer
    const hexLayer = showHexagons
      ? new H3HexagonLayer({
          id: 'h3-hexagons',
          data: cells,
          pickable: true,
          wireframe: true,
          filled: true,
          extruded: false,
          lineWidthMinPixels: 1,
          getLineColor: [30, 41, 59, 90], // Subtle dark slate hexagon borders
          getHexagon: (d: any) => d.h3,
          getFillColor: (d: any, { index }: any) => {
            // Layer 1: Reachability
            if (layerMode === 'reach') {
              const reachable = scenarios?.reach?.[index]
              return reachable
                ? [16, 185, 129, 140] // Clear Emerald Green
                : [220, 38, 38, 170]   // Warning Red
            }

            // Layer 2: Demand Density (Multi-Stop Heatmap)
            if (layerMode === 'demand') {
              const lam = d.lam?.[blockId] || 0
              if (lam > 0.10) return [239, 68, 68, 190] // High Demand: Red
              if (lam > 0.05) return [245, 158, 11, 160] // Medium-High: Amber
              if (lam > 0.02) return [59, 130, 246, 130] // Moderate: Blue
              return [71, 85, 105, 100] // Low: Slate
            }

            // Layer 3: Response Time in Minutes
            if (layerMode === 'nearest') {
              const m = currBlockPlan.nearest_min?.[index] ?? 25
              if (m <= targetMin * 0.55) return [16, 185, 129, 150] // < 8 min: Emerald Green
              if (m <= targetMin) return [245, 158, 11, 150]       // 8 - 15 min: Warm Amber
              if (m <= targetMin * 1.5) return [249, 115, 22, 170] // 15 - 22 min: Orange
              return [220, 38, 38, 190]                           // > 22 min: Critical Red
            }

            // Layer 4: Chance In-Time (Expected Cover)
            const cov = currBlockPlan.cover?.[index] ?? 0
            if (cov >= 0.75) return [16, 185, 129, 145] // High Coverage: Emerald Green
            if (cov >= 0.50) return [245, 158, 11, 145] // Moderate: Warm Amber
            return [220, 38, 38, 175]                   // Delayed/Sub-floor: Critical Red
          },
          updateTriggers: {
            getFillColor: [layerMode, blockId, planType, currBlockPlan],
          },
        })
      : null

    // 2. Municipal Ward Boundaries Layer
    const zoneLayer =
      showWards && zonesGeo
        ? new GeoJsonLayer({
            id: 'admin-zones',
            data: zonesGeo,
            pickable: false,
            stroked: true,
            filled: false,
            lineWidthMinPixels: 1.5,
            getLineColor: [71, 85, 105, 140], // Crisp boundary lines
          })
        : null

    // 3. Crew Move Arcs Layer (Relocations)
    const movesLayer =
      showArcs && blockMoves.length > 0
        ? new ArcLayer({
            id: 'crew-moves-arcs',
            data: blockMoves,
            pickable: true,
            getWidth: 4.5,
            getSourcePosition: (d: any) => [d.from_lon, d.from_lat],
            getTargetPosition: (d: any) => [d.to_lon, d.to_lat],
            // Gradient: Green (Origin) ➔ Red (Destination)
            getSourceColor: [16, 185, 129, 230],
            getTargetColor: [239, 68, 68, 240],
            onClick: ({ object }: any) => {
              if (object) setSelectedMove(object)
            },
          })
        : null

    // 4. Move Target Rings (Destination Arrival Indicators)
    const moveRingsLayer =
      showArcs && moveEndpoints.length > 0
        ? new ScatterplotLayer({
            id: 'move-arrival-rings',
            data: moveEndpoints,
            pickable: false,
            stroked: true,
            filled: false,
            lineWidthMinPixels: 2.5,
            radiusMinPixels: 14,
            radiusMaxPixels: 26,
            getPosition: (d: any) => [d.lon, d.lat],
            getRadius: 300,
            getLineColor: [239, 68, 68, 240], // Red destination beacon ring
          })
        : null

    // 5. Vehicle Radar Halos (High-visibility background circles)
    const vehicleHaloLayer = showVehicles
      ? new ScatterplotLayer({
          id: 'vehicle-halos',
          data: vehicleMarkers,
          pickable: false,
          stroked: true,
          filled: true,
          lineWidthMinPixels: 2,
          radiusMinPixels: 12,
          radiusMaxPixels: 22,
          getPosition: (d: any) => [d.lon, d.lat + (d.offsetY || 0)],
          getRadius: (d: any) => 180 + d.count * 60,
          getFillColor: (d: any) => [d.color[0], d.color[1], d.color[2], 140],
          getLineColor: [255, 255, 255, 240],
        })
      : null

    // 6. Professional Vehicle Symbols (IconLayer)
    const vehicleIconsLayer = showVehicles
      ? new IconLayer({
          id: 'vehicle-icons',
          data: vehicleMarkers,
          pickable: true,
          getIcon: (d: any) => ({
            url: d.iconUrl,
            width: 64,
            height: 64,
            anchorY: 32,
            anchorX: 32,
          }),
          sizeScale: 1,
          getPosition: (d: any) => [d.lon, d.lat + (d.offsetY || 0)],
          getSize: (d: any) => (d.count > 1 ? 38 : 32),
          onClick: ({ object }: any) => {
            if (object) setSelectedStation(object)
          },
        })
      : null

    // 7. Multi-Vehicle Count Badges (TextLayer)
    const badgeLayer = showVehicles
      ? new TextLayer({
          id: 'vehicle-count-badges',
          data: vehicleBadges,
          pickable: false,
          getPosition: (d: any) => [d.lon, d.lat],
          getText: (d: any) => d.text,
          getSize: 11,
          getColor: [255, 255, 255, 255],
          getTextAnchor: 'middle',
          getAlignmentBaseline: 'center',
          fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
          fontWeight: 800,
          background: true,
          getBackgroundColor: [15, 23, 42, 230],
          backgroundPadding: [4, 2],
        })
      : null

    return [
      hexLayer,
      zoneLayer,
      movesLayer,
      moveRingsLayer,
      vehicleHaloLayer,
      vehicleIconsLayer,
      badgeLayer,
    ].filter(Boolean)
  }, [
    cells,
    currBlockPlan,
    layerMode,
    blockId,
    planType,
    zonesGeo,
    blockMoves,
    vehicleMarkers,
    vehicleBadges,
    moveEndpoints,
    showHexagons,
    showVehicles,
    showArcs,
    showWards,
    scenarios,
    targetMin,
  ])

  // Histogram SVG Renderer
  const renderHistogram = () => {
    if (!currSim?.histogram || !baseSim?.histogram) return null
    const maxVal = Math.max(...currSim.histogram, ...baseSim.histogram, 1)
    const targetBin = Math.round(targetMin)

    return (
      <div style={{ marginTop: '14px', backgroundColor: '#131d31', padding: '10px 12px', borderRadius: '8px', border: '1px solid #1e293b' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '11px', color: '#94a3b8', marginBottom: '6px' }}>
          <span style={{ fontWeight: 600, color: '#f8fafc' }}>Response Distribution (0–60 min)</span>
          <span style={{ color: '#ef4444', fontWeight: 700 }}>Mandate: {targetMin}m</span>
        </div>
        <svg width="100%" height="70" style={{ background: '#090d16', borderRadius: '6px', padding: '4px' }}>
          {/* Target line */}
          <line
            x1={`${(targetBin / 60) * 100}%`}
            y1="0"
            x2={`${(targetBin / 60) * 100}%`}
            y2="70"
            stroke="#ef4444"
            strokeDasharray="3 3"
            strokeWidth="1.5"
          />
          {/* Baseline Bars (Gray) */}
          {baseSim.histogram.map((val: number, i: number) => {
            if (i > 50) return null
            const h = (val / maxVal) * 55
            const x = (i / 50) * 100
            return (
              <rect
                key={`b-${i}`}
                x={`${x}%`}
                y={60 - h}
                width="1.6%"
                height={h}
                fill="#64748b"
                opacity="0.4"
              />
            )
          })}
          {/* Selected Plan Bars (Green/Amber) */}
          {currSim.histogram.map((val: number, i: number) => {
            if (i > 50) return null
            const h = (val / maxVal) * 55
            const x = (i / 50) * 100
            const inTarget = i <= targetBin
            return (
              <rect
                key={`p-${i}`}
                x={`${x + 0.3}%`}
                y={60 - h}
                width="1.4%"
                height={h}
                fill={inTarget ? '#10b981' : '#f59e0b'}
                opacity="0.9"
              />
            )
          })}
        </svg>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px', color: '#64748b', marginTop: '3px' }}>
          <span>0m (Instant)</span>
          <span>15m Target</span>
          <span>30m</span>
          <span>45m</span>
          <span>60m+</span>
        </div>
      </div>
    )
  }

  // Demand Forecast Timeline SVG
  const renderForecastChart = () => {
    const tl = forecastData?.[region]?.timeline || []
    if (!tl.length) return <p style={{ color: '#94a3b8' }}>Loading forecast telemetry...</p>
    const maxVal = Math.max(...tl.map((d: any) => Math.max(d.actual, d.forecast, d.baseline)), 1)

    return (
      <svg width="100%" height="160" style={{ background: '#090d16', borderRadius: '6px', padding: '10px' }}>
        {tl.map((d: any, i: number) => {
          if (i === 0) return null
          const prev = tl[i - 1]
          const x1 = ((i - 1) / (tl.length - 1)) * 100 + '%'
          const x2 = (i / (tl.length - 1)) * 100 + '%'
          const y1Act = 140 - (prev.actual / maxVal) * 120
          const y2Act = 140 - (d.actual / maxVal) * 120
          const y1Fc = 140 - (prev.forecast / maxVal) * 120
          const y2Fc = 140 - (d.forecast / maxVal) * 120
          const y1Base = 140 - (prev.baseline / maxVal) * 120
          const y2Base = 140 - (d.baseline / maxVal) * 120

          return (
            <g key={i}>
              <line x1={x1} y1={y1Base} x2={x2} y2={y2Base} stroke="#64748b" strokeWidth="1" strokeDasharray="3 3" />
              <line x1={x1} y1={y1Act} x2={x2} y2={y2Act} stroke="#3b82f6" strokeWidth="1.5" />
              <line x1={x1} y1={y1Fc} x2={x2} y2={y2Fc} stroke="#10b981" strokeWidth="2" />
            </g>
          )
        })}
      </svg>
    )
  }

  return (
    <div style={{ position: 'relative', width: '100vw', height: '100vh', overflow: 'hidden', display: 'flex', backgroundColor: '#090d16' }}>
      {/* 1. Left Sidebar: Operator Command Center */}
      <div
        style={{
          width: '430px',
          minWidth: '400px',
          height: '100%',
          backgroundColor: '#0c1322',
          borderRight: '1px solid #1e293b',
          zIndex: 10,
          display: 'flex',
          flexDirection: 'column',
          overflowY: 'auto',
          padding: '18px 20px',
          boxShadow: '4px 0 24px rgba(0,0,0,0.6)',
        }}
      >
        {/* Header */}
        <div style={{ marginBottom: '16px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <div style={{ backgroundColor: '#ef4444', padding: '6px', borderRadius: '8px', display: 'flex' }}>
              <Activity color="#ffffff" size={22} />
            </div>
            <div>
              <h1 style={{ fontSize: '22px', fontWeight: 800, letterSpacing: '-0.5px', color: '#f8fafc', lineHeight: 1.1 }}>
                GoldenHour
              </h1>
              <div style={{ fontSize: '11px', color: '#38bdf8', fontWeight: 600, marginTop: '2px' }}>
                PES University Zero Hour • Track 3: Optimisation
              </div>
            </div>
            <span
              style={{
                marginLeft: 'auto',
                fontSize: '11px',
                padding: '3px 8px',
                borderRadius: '6px',
                backgroundColor: '#1e293b',
                color: '#10b981',
                fontWeight: 700,
                border: '1px solid #334155',
              }}
            >
              LIVE 108
            </span>
          </div>
          <p style={{ fontSize: '12px', color: '#94a3b8', marginTop: '6px', lineHeight: 1.4 }}>
            Karnataka 108 Emergency Ambulance Fleet Allocation & Multi-Period Response Planner
          </p>
        </div>

        {/* Region Switcher Tabs */}
        <div style={{ display: 'flex', backgroundColor: '#131d31', borderRadius: '8px', padding: '3px', marginBottom: '14px', border: '1px solid #1e293b' }}>
          <button
            onClick={() => setRegion('blr')}
            style={{
              flex: 1,
              padding: '8px 10px',
              borderRadius: '6px',
              border: 'none',
              backgroundColor: region === 'blr' ? '#2563eb' : 'transparent',
              color: '#ffffff',
              fontWeight: 700,
              fontSize: '12.5px',
              transition: 'background 0.2s',
            }}
          >
            Greater Bengaluru (Urban)
          </button>
          <button
            onClick={() => setRegion('tmk')}
            style={{
              flex: 1,
              padding: '8px 10px',
              borderRadius: '6px',
              border: 'none',
              backgroundColor: region === 'tmk' ? '#2563eb' : 'transparent',
              color: '#ffffff',
              fontWeight: 700,
              fontSize: '12.5px',
              transition: 'background 0.2s',
            }}
          >
            Tumakuru District (Rural)
          </button>
        </div>

        {/* Strategy Selector */}
        <div style={{ marginBottom: '14px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
            <label style={{ fontSize: '11px', textTransform: 'uppercase', color: '#64748b', fontWeight: 800, letterSpacing: '0.6px' }}>
              Strategic Deployment Model
            </label>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: '6px' }}>
            <button
              onClick={() => setPlanType('baseline')}
              style={{
                textAlign: 'left',
                padding: '9px 12px',
                borderRadius: '8px',
                border: planType === 'baseline' ? '1.5px solid #3b82f6' : '1px solid #1e293b',
                backgroundColor: planType === 'baseline' ? '#172554' : '#111827',
                color: '#f8fafc',
              }}
            >
              <div style={{ fontWeight: 700, fontSize: '12.5px', color: planType === 'baseline' ? '#60a5fa' : '#f8fafc' }}>
                1. Today: Hospital Proportional (CAG Baseline)
              </div>
              <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '2px' }}>
                Parked exclusively at government tertiary hospitals by catchment. 0 bike paramedics.
              </div>
            </button>

            <button
              onClick={() => setPlanType('static')}
              style={{
                textAlign: 'left',
                padding: '9px 12px',
                borderRadius: '8px',
                border: planType === 'static' ? '1.5px solid #3b82f6' : '1px solid #1e293b',
                backgroundColor: planType === 'static' ? '#172554' : '#111827',
                color: '#f8fafc',
              }}
            >
              <div style={{ fontWeight: 700, fontSize: '12.5px', color: planType === 'static' ? '#60a5fa' : '#f8fafc' }}>
                2. Best Fixed Spots (Static Optimization)
              </div>
              <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '2px' }}>
                Single fixed 24-hour placement across all POIs, calibrated to peak traffic. No shift moves.
              </div>
            </button>

            <button
              onClick={() => setPlanType('goldenhour')}
              style={{
                textAlign: 'left',
                padding: '10px 12px',
                borderRadius: '8px',
                border: planType === 'goldenhour' ? '1.5px solid #10b981' : '1px solid #1e293b',
                backgroundColor: planType === 'goldenhour' ? 'rgba(6, 78, 59, 0.35)' : '#111827',
                color: '#f8fafc',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <span style={{ fontWeight: 800, fontSize: '13px', color: planType === 'goldenhour' ? '#34d399' : '#f8fafc' }}>
                  3. GoldenHour Dynamic Plan (Rolling MILP)
                </span>
                <span style={{ fontSize: '10px', backgroundColor: '#059669', color: '#ffffff', padding: '1px 6px', borderRadius: '4px', fontWeight: 800 }}>
                  RECOMMENDED
                </span>
              </div>
              <div style={{ fontSize: '11px', color: '#cbd5e1', marginTop: '3px' }}>
                Shift-responsive relocation, 30 bike first-responders, and 25% smooth shift repositioning via HiGHS solver.
              </div>
            </button>
          </div>
        </div>

        {/* Active Fleet & Cabinet Target Badge */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '9px 12px',
            backgroundColor: '#131d31',
            borderRadius: '8px',
            fontSize: '12px',
            marginBottom: '14px',
            border: '1px solid #1e293b',
          }}
        >
          <div>
            <span style={{ color: '#94a3b8' }}>Fleet on Road: </span>
            <span style={{ fontWeight: 700, color: '#f8fafc' }}>
              {scenarios?.baseline?.fleet?.ambulances || 0} Ambulances
            </span>
            {region === 'blr' && (
              <span style={{ color: '#f97316', fontWeight: 800 }}> + 30 Bikes</span>
            )}
          </div>
          <div style={{ borderLeft: '1px solid #334155', paddingLeft: '10px' }}>
            <span style={{ color: '#94a3b8' }}>Mandate: </span>
            <span style={{ fontWeight: 800, color: '#ef4444' }}>{targetMin} min</span>
          </div>
        </div>

        {/* Operator Shift Stepper & Auto-Play Controls */}
        <div style={{ backgroundColor: '#131d31', padding: '12px', borderRadius: '8px', marginBottom: '14px', border: '1px solid #1e293b' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span style={{ fontSize: '11px', textTransform: 'uppercase', color: '#64748b', fontWeight: 800 }}>
                Shift Horizon
              </span>
              <span style={{ fontSize: '11px', color: blockId === 4 ? '#ef4444' : '#38bdf8', fontWeight: 700 }}>
                {blockId === 4 ? '• PEAK RUSH (2.6× Traffic)' : `• Shift ${blockId}`}
              </span>
            </div>

            {/* Stepper buttons */}
            <div style={{ display: 'flex', gap: '4px' }}>
              <button
                onClick={() => setBlockId((prev) => (prev === 0 ? 5 : prev - 1))}
                style={{ backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '4px', padding: '3px 6px', color: '#f8fafc' }}
                title="Previous Shift"
              >
                <ChevronLeft size={14} />
              </button>
              <button
                onClick={() => setIsPlaying(!isPlaying)}
                style={{
                  backgroundColor: isPlaying ? '#ef4444' : '#2563eb',
                  border: 'none',
                  borderRadius: '4px',
                  padding: '3px 8px',
                  color: '#ffffff',
                  fontSize: '11px',
                  fontWeight: 700,
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                }}
              >
                {isPlaying ? <Pause size={12} /> : <Play size={12} />}
                {isPlaying ? 'Pause' : 'Play 24h'}
              </button>
              <button
                onClick={() => setBlockId((prev) => (prev + 1) % 6)}
                style={{ backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '4px', padding: '3px 6px', color: '#f8fafc' }}
                title="Next Shift"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </div>

          <div style={{ fontSize: '13px', fontWeight: 700, color: '#f8fafc', marginBottom: '6px' }}>
            {BLOCK_NAMES[blockId]}
          </div>

          <input
            type="range"
            min={0}
            max={5}
            step={1}
            value={blockId}
            onChange={(e) => setBlockId(Number(e.target.value))}
          />
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px', color: '#64748b', marginTop: '4px' }}>
            <span>00:00 (Night)</span>
            <span>08:00 (Morning)</span>
            <span style={{ color: '#ef4444', fontWeight: 800 }}>16:00 (Peak Rush)</span>
            <span>24:00</span>
          </div>
        </div>

        {/* 4 Emergency Response KPI Cards */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', marginBottom: '14px' }}>
          {/* KPI 1 */}
          <div style={{ backgroundColor: '#131d31', padding: '10px 12px', borderRadius: '8px', border: '1px solid #1e293b' }}>
            <div style={{ fontSize: '11px', color: '#94a3b8', fontWeight: 600 }}>Reached ≤ {targetMin}m Target</div>
            <div style={{ fontSize: '22px', fontWeight: 800, marginTop: '2px', color: '#f8fafc' }}>
              {currSim ? currSim.within_target_pct + '%' : '--'}
            </div>
            <div style={{ fontSize: '11px', color: deltaTarget >= 0 ? '#10b981' : '#ef4444', fontWeight: 700, marginTop: '2px' }}>
              {deltaTarget >= 0 ? `+${deltaTarget.toFixed(1)}%` : `${deltaTarget.toFixed(1)}%`} vs Today
            </div>
          </div>

          {/* KPI 2 */}
          <div style={{ backgroundColor: '#131d31', padding: '10px 12px', borderRadius: '8px', border: '1px solid #1e293b' }}>
            <div style={{ fontSize: '11px', color: '#94a3b8', fontWeight: 600 }}>Median Response Time</div>
            <div style={{ fontSize: '22px', fontWeight: 800, marginTop: '2px', color: '#f8fafc' }}>
              {currSim ? `${currSim.median_min}m` : '--'}
            </div>
            <div style={{ fontSize: '11px', color: deltaMedian <= 0 ? '#10b981' : '#ef4444', fontWeight: 700, marginTop: '2px' }}>
              {deltaMedian <= 0 ? `${deltaMedian.toFixed(1)} min` : `+${deltaMedian.toFixed(1)} min`} vs Today
            </div>
          </div>

          {/* KPI 3 */}
          <div style={{ backgroundColor: '#131d31', padding: '10px 12px', borderRadius: '8px', border: '1px solid #1e293b' }}>
            <div style={{ fontSize: '11px', color: '#94a3b8', fontWeight: 600 }}>90th-Percentile Wait</div>
            <div style={{ fontSize: '22px', fontWeight: 800, marginTop: '2px', color: '#f8fafc' }}>
              {currSim ? `${currSim.p90_min}m` : '--'}
            </div>
            <div style={{ fontSize: '11px', color: deltaP90 <= 0 ? '#10b981' : '#ef4444', fontWeight: 700, marginTop: '2px' }}>
              {deltaP90 <= 0 ? `${deltaP90.toFixed(1)} min` : `+${deltaP90.toFixed(1)} min`} vs Today
            </div>
          </div>

          {/* KPI 4 */}
          <div style={{ backgroundColor: '#131d31', padding: '10px 12px', borderRadius: '8px', border: '1px solid #1e293b' }}>
            <div style={{ fontSize: '11px', color: '#94a3b8', fontWeight: 600 }}>First Help ≤ 10m</div>
            <div style={{ fontSize: '22px', fontWeight: 800, marginTop: '2px', color: '#f97316' }}>
              {currSim ? `${currSim.first_help_10m_pct}%` : '--'}
            </div>
            <div style={{ fontSize: '11px', color: deltaBike >= 0 ? '#10b981' : '#ef4444', fontWeight: 700, marginTop: '2px' }}>
              {deltaBike >= 0 ? `+${deltaBike.toFixed(1)}%` : `${deltaBike.toFixed(1)}%`} (Bike Impact)
            </div>
          </div>
        </div>

        {/* Selected Station / Selected Move Inspector Panel */}
        {selectedStation && (
          <div style={{ backgroundColor: '#1e293b', padding: '12px', borderRadius: '8px', marginBottom: '14px', border: '1.5px solid #3b82f6', position: 'relative' }}>
            <button
              onClick={() => setSelectedStation(null)}
              style={{ position: 'absolute', top: '8px', right: '8px', background: 'transparent', border: 'none', color: '#94a3b8' }}
            >
              <X size={16} />
            </button>
            <div style={{ fontSize: '11px', textTransform: 'uppercase', color: '#38bdf8', fontWeight: 800 }}>
              Standby Station Inspector
            </div>
            <div style={{ fontSize: '14px', fontWeight: 800, color: '#f8fafc', marginTop: '2px' }}>
              {selectedStation.name}
            </div>
            <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '1px' }}>
              Facility Type: {selectedStation.site_type || 'hospital'}
            </div>
            <div style={{ display: 'flex', gap: '8px', marginTop: '8px' }}>
              <span style={{ fontSize: '11px', backgroundColor: '#dc2626', color: '#ffffff', padding: '2px 8px', borderRadius: '4px', fontWeight: 700 }}>
                ALS: {selectedStation.a || 0}
              </span>
              <span style={{ fontSize: '11px', backgroundColor: '#2563eb', color: '#ffffff', padding: '2px 8px', borderRadius: '4px', fontWeight: 700 }}>
                BLS: {selectedStation.b || 0}
              </span>
              <span style={{ fontSize: '11px', backgroundColor: '#ea580c', color: '#ffffff', padding: '2px 8px', borderRadius: '4px', fontWeight: 700 }}>
                Bikes: {selectedStation.y || 0}
              </span>
            </div>
          </div>
        )}

        {selectedMove && (
          <div style={{ backgroundColor: '#1e293b', padding: '12px', borderRadius: '8px', marginBottom: '14px', border: '1.5px solid #ef4444', position: 'relative' }}>
            <button
              onClick={() => setSelectedMove(null)}
              style={{ position: 'absolute', top: '8px', right: '8px', background: 'transparent', border: 'none', color: '#94a3b8' }}
            >
              <X size={16} />
            </button>
            <div style={{ fontSize: '11px', textTransform: 'uppercase', color: '#ef4444', fontWeight: 800 }}>
              Active Relocation Transit Docket
            </div>
            <div style={{ fontSize: '13px', fontWeight: 700, color: '#f8fafc', marginTop: '4px' }}>
              🟢 From: {selectedMove.from_name}
            </div>
            <div style={{ fontSize: '13px', fontWeight: 700, color: '#f8fafc', marginTop: '2px' }}>
              🔴 To: {selectedMove.to_name}
            </div>
            <div style={{ fontSize: '12px', color: '#fbbf24', fontWeight: 700, marginTop: '4px' }}>
              Estimated Transit: {selectedMove.travel_min} minutes via Primary Road
            </div>
          </div>
        )}

        {/* Current Block Status Note */}
        <div style={{ backgroundColor: '#131d31', padding: '10px 12px', borderRadius: '8px', fontSize: '12px', color: '#cbd5e1', marginBottom: '10px', border: '1px solid #1e293b' }}>
          During this shift ({BLOCK_NAMES[blockId].split(' ')[0]}), expected vehicle busy fraction is{' '}
          <strong style={{ color: '#38bdf8' }}>q = {currBlockPlan?.q ?? 0.3}</strong>.
          {planType === 'goldenhour' && blockMoves.length > 0 && (
            <div style={{ marginTop: '5px', color: '#f59e0b', fontWeight: 700 }}>
              ➔ {blockMoves.length} ambulance relocations scheduled across sites for shift rebalancing.
            </div>
          )}
        </div>

        {/* Histogram SVG */}
        {renderHistogram()}

        {/* Bottom Drawer Nav Toggle Pills */}
        <div style={{ marginTop: 'auto', paddingTop: '16px', borderTop: '1px solid #1e293b' }}>
          <div style={{ fontSize: '11px', textTransform: 'uppercase', color: '#64748b', fontWeight: 800, marginBottom: '6px' }}>
            Operator Telemetry & Analysis Drawers
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '5px' }}>
            <button
              onClick={() => setBottomTab(bottomTab === 'wards' ? 'none' : 'wards')}
              style={{
                fontSize: '11px',
                padding: '5px 9px',
                borderRadius: '5px',
                border: '1px solid #334155',
                backgroundColor: bottomTab === 'wards' ? '#2563eb' : '#1e293b',
                color: '#ffffff',
                fontWeight: 600,
              }}
            >
              Wards / Taluks
            </button>
            <button
              onClick={() => setBottomTab(bottomTab === 'stress' ? 'none' : 'stress')}
              style={{
                fontSize: '11px',
                padding: '5px 9px',
                borderRadius: '5px',
                border: '1px solid #334155',
                backgroundColor: bottomTab === 'stress' ? '#2563eb' : '#1e293b',
                color: '#ffffff',
                fontWeight: 600,
              }}
            >
              Stress Tests
            </button>
            <button
              onClick={() => setBottomTab(bottomTab === 'fleet' ? 'none' : 'fleet')}
              style={{
                fontSize: '11px',
                padding: '5px 9px',
                borderRadius: '5px',
                border: '1px solid #334155',
                backgroundColor: bottomTab === 'fleet' ? '#2563eb' : '#1e293b',
                color: '#ffffff',
                fontWeight: 600,
              }}
            >
              How Many Vehicles?
            </button>
            <button
              onClick={() => setBottomTab(bottomTab === 'forecast' ? 'none' : 'forecast')}
              style={{
                fontSize: '11px',
                padding: '5px 9px',
                borderRadius: '5px',
                border: '1px solid #334155',
                backgroundColor: bottomTab === 'forecast' ? '#2563eb' : '#1e293b',
                color: '#ffffff',
                fontWeight: 600,
              }}
            >
              Demand Forecast
            </button>
            <button
              onClick={() => setBottomTab(bottomTab === 'papers' ? 'none' : 'papers')}
              style={{
                fontSize: '11px',
                padding: '5px 9px',
                borderRadius: '5px',
                border: '1px solid #334155',
                backgroundColor: bottomTab === 'papers' ? '#2563eb' : '#1e293b',
                color: '#ffffff',
                fontWeight: 600,
              }}
            >
              Paper Gaps (v2)
            </button>
            <button
              onClick={() => setBottomTab(bottomTab === 'sources' ? 'none' : 'sources')}
              style={{
                fontSize: '11px',
                padding: '5px 9px',
                borderRadius: '5px',
                border: '1px solid #334155',
                backgroundColor: bottomTab === 'sources' ? '#2563eb' : '#1e293b',
                color: '#ffffff',
                fontWeight: 600,
              }}
            >
              Verified Sources
            </button>
          </div>
        </div>
      </div>

      {/* 2. Map Canvas & Operator Overlays */}
      <div style={{ flex: 1, position: 'relative', height: '100%' }}>
        {/* Top-Left Search Bar (Operator Dispatch Tool) */}
        <div style={{ position: 'absolute', top: '16px', left: '16px', zIndex: 15, width: '320px' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              backgroundColor: 'rgba(15, 23, 42, 0.94)',
              backdropFilter: 'blur(8px)',
              border: '1px solid #334155',
              borderRadius: '8px',
              padding: '8px 12px',
              boxShadow: '0 4px 16px rgba(0,0,0,0.5)',
            }}
          >
            <Search size={16} color="#94a3b8" style={{ marginRight: '8px' }} />
            <input
              type="text"
              placeholder="Search Ward (e.g. Indiranagar) or Hospital..."
              value={searchQuery}
              onChange={(e) => {
                setSearchQuery(e.target.value)
                setShowSearchResults(true)
              }}
              onFocus={() => setShowSearchResults(true)}
              style={{
                background: 'transparent',
                border: 'none',
                outline: 'none',
                color: '#f8fafc',
                fontSize: '13px',
                width: '100%',
              }}
            />
            {searchQuery && (
              <button
                onClick={() => {
                  setSearchQuery('')
                  setShowSearchResults(false)
                }}
                style={{ background: 'transparent', border: 'none', color: '#94a3b8', cursor: 'pointer' }}
              >
                <X size={14} />
              </button>
            )}
          </div>

          {/* Search Dropdown */}
          {showSearchResults && searchResults.length > 0 && (
            <div
              style={{
                marginTop: '6px',
                backgroundColor: '#0f172a',
                border: '1px solid #334155',
                borderRadius: '8px',
                overflow: 'hidden',
                boxShadow: '0 8px 24px rgba(0,0,0,0.6)',
                maxHeight: '260px',
                overflowY: 'auto',
              }}
            >
              {searchResults.map((r, i) => (
                <div
                  key={i}
                  onClick={() => handleSelectSearch(r)}
                  style={{
                    padding: '8px 12px',
                    borderBottom: '1px solid #1e293b',
                    cursor: 'pointer',
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = '#1e293b')}
                  onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = 'transparent')}
                >
                  <span style={{ fontSize: '13px', color: '#f8fafc', fontWeight: 600 }}>{r.name}</span>
                  <span style={{ fontSize: '10px', color: '#38bdf8', backgroundColor: '#1e293b', padding: '2px 6px', borderRadius: '4px' }}>
                    {r.type}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Top-Right Control Palette: Layer Switcher & Visibility Toggles */}
        <div
          style={{
            position: 'absolute',
            top: '16px',
            right: '16px',
            zIndex: 15,
            backgroundColor: 'rgba(15, 23, 42, 0.94)',
            backdropFilter: 'blur(8px)',
            border: '1px solid #334155',
            borderRadius: '8px',
            padding: '12px 16px',
            boxShadow: '0 4px 16px rgba(0,0,0,0.5)',
            maxWidth: '380px',
          }}
        >
          <div style={{ fontSize: '11px', textTransform: 'uppercase', color: '#94a3b8', fontWeight: 800, marginBottom: '6px' }}>
            Map Layer Metric
          </div>
          <div style={{ display: 'flex', gap: '6px', marginBottom: '10px' }}>
            <button
              onClick={() => setLayerMode('cover')}
              style={{
                fontSize: '12px',
                padding: '5px 9px',
                borderRadius: '5px',
                border: 'none',
                backgroundColor: layerMode === 'cover' ? '#2563eb' : '#1e293b',
                color: '#ffffff',
                fontWeight: 700,
              }}
            >
              Chance In-Time
            </button>
            <button
              onClick={() => setLayerMode('nearest')}
              style={{
                fontSize: '12px',
                padding: '5px 9px',
                borderRadius: '5px',
                border: 'none',
                backgroundColor: layerMode === 'nearest' ? '#2563eb' : '#1e293b',
                color: '#ffffff',
                fontWeight: 700,
              }}
            >
              Minutes to Standby
            </button>
            <button
              onClick={() => setLayerMode('demand')}
              style={{
                fontSize: '12px',
                padding: '5px 9px',
                borderRadius: '5px',
                border: 'none',
                backgroundColor: layerMode === 'demand' ? '#2563eb' : '#1e293b',
                color: '#ffffff',
                fontWeight: 700,
              }}
            >
              Demand Heatmap
            </button>
            <button
              onClick={() => setLayerMode('reach')}
              style={{
                fontSize: '12px',
                padding: '5px 9px',
                borderRadius: '5px',
                border: 'none',
                backgroundColor: layerMode === 'reach' ? '#2563eb' : '#1e293b',
                color: '#ffffff',
                fontWeight: 700,
              }}
            >
              Peak Reachable
            </button>
          </div>

          {/* Visibility Toggles */}
          <div style={{ display: 'flex', gap: '8px', borderTop: '1px solid #1e293b', paddingTop: '8px', fontSize: '11px', color: '#cbd5e1' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer' }}>
              <input type="checkbox" checked={showHexagons} onChange={(e) => setShowHexagons(e.target.checked)} />
              Hexagons
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer' }}>
              <input type="checkbox" checked={showVehicles} onChange={(e) => setShowVehicles(e.target.checked)} />
              Vehicles
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer' }}>
              <input type="checkbox" checked={showArcs} onChange={(e) => setShowArcs(e.target.checked)} />
              Relocation Arcs
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer' }}>
              <input type="checkbox" checked={showWards} onChange={(e) => setShowWards(e.target.checked)} />
              Wards
            </label>
          </div>

          {/* Operator Legend & Symbols */}
          <div style={{ borderTop: '1px solid #1e293b', marginTop: '8px', paddingTop: '8px' }}>
            <div style={{ fontSize: '10px', textTransform: 'uppercase', color: '#64748b', fontWeight: 800, marginBottom: '4px' }}>
              Fleet Symbols
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', fontSize: '11px', color: '#f8fafc' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                <img src="/icons/icon-als.svg" width="18" height="18" alt="ALS" />
                <span>ALS (ICU)</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                <img src="/icons/icon-bls.svg" width="18" height="18" alt="BLS" />
                <span>BLS (Basic)</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                <img src="/icons/icon-bike.svg" width="18" height="18" alt="Bike" />
                <span>Bike Paramedic</span>
              </div>
            </div>

            {/* Relocation Legend */}
            {blockMoves.length > 0 && planType === 'goldenhour' && showArcs && (
              <div style={{ marginTop: '6px', fontSize: '11px', color: '#cbd5e1', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style={{ fontWeight: 700, color: '#f59e0b' }}>Relocation Move:</span>
                <span style={{ color: '#10b981', fontWeight: 700 }}>🟢 Origin</span>
                <span>────►</span>
                <span style={{ color: '#ef4444', fontWeight: 700 }}>🔴 Destination</span>
              </div>
            )}
          </div>
        </div>

        {/* DeckGL Canvas */}
        <DeckGL
          viewState={viewState}
          onViewStateChange={({ viewState: vs }: any) => setViewState(vs)}
          controller={true}
          layers={layers}
          getTooltip={({ object }: any) => {
            if (!object) return null
            if (object.h3) {
              const lam = object.lam?.[blockId] || 0
              return {
                html: `
                  <div style="font-family: sans-serif; font-size: 12px; line-height: 1.4; padding: 6px;">
                    <div style="font-weight: 800; color: #38bdf8; font-size: 13px;">${object.zone}</div>
                    <div><strong>Population:</strong> ${object.pop.toLocaleString()}</div>
                    <div><strong>Expected Emergency Calls:</strong> ${(lam).toFixed(2)} / hour</div>
                    <div><strong>Major Road Length:</strong> ${object.major_km} km</div>
                  </div>
                `,
                style: { backgroundColor: '#0f172a', color: '#ffffff', borderRadius: '8px', border: '1px solid #334155' },
              }
            }
            if (object.site_id) {
              return {
                html: `
                  <div style="font-family: sans-serif; font-size: 12px; line-height: 1.4; padding: 6px;">
                    <div style="font-weight: 800; color: #f8fafc; font-size: 13px;">${object.name}</div>
                    <div style="color: #94a3b8; font-size: 11px;">Type: ${object.site_type}</div>
                    <div style="margin-top: 4px; font-weight: 700;">
                      <span style="color: #ef4444;">ALS: ${object.a}</span> | 
                      <span style="color: #3b82f6;">BLS: ${object.b}</span> | 
                      <span style="color: #f97316;">Bikes: ${object.y}</span>
                    </div>
                  </div>
                `,
                style: { backgroundColor: '#0f172a', color: '#ffffff', borderRadius: '8px', border: '1px solid #334155' },
              }
            }
            if (object.from_name) {
              return {
                html: `
                  <div style="font-family: sans-serif; font-size: 12px; padding: 6px; line-height: 1.4;">
                    <div style="font-weight: 800; color: #f59e0b;">Planned Ambulance Relocation</div>
                    <div><strong>🟢 From:</strong> ${object.from_name}</div>
                    <div><strong>🔴 To:</strong> ${object.to_name}</div>
                    <div style="color: #38bdf8; font-weight: 700; margin-top: 2px;">Transit Time: ${object.travel_min} min</div>
                  </div>
                `,
                style: { backgroundColor: '#0f172a', color: '#ffffff', borderRadius: '8px', border: '1px solid #334155' },
              }
            }
            return null
          }}
        >
          <Map mapStyle={MAP_STYLE} />
        </DeckGL>

        {/* 3. Bottom Drawer Telemetry / Analysis Modal */}
        {bottomTab !== 'none' && (
          <div
            style={{
              position: 'absolute',
              bottom: 0,
              left: 0,
              right: 0,
              height: '350px',
              backgroundColor: '#0c1322',
              borderTop: '2px solid #2563eb',
              boxShadow: '0 -10px 30px rgba(0,0,0,0.7)',
              zIndex: 25,
              display: 'flex',
              flexDirection: 'column',
              padding: '16px 24px',
              overflowY: 'auto',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
              <h2 style={{ fontSize: '16px', fontWeight: 800, color: '#f8fafc' }}>
                {bottomTab === 'wards' && 'Municipal Ward & Taluk Response Performance (Sorted Slowest First)'}
                {bottomTab === 'stress' && 'Stress Testing & System Resilience Battery'}
                {bottomTab === 'fleet' && 'Fleet Scaling Curve: How Many Vehicles Does Karnataka Need?'}
                {bottomTab === 'forecast' && 'Demand Forecasting: LightGBM vs 4-Week Moving Average'}
                {bottomTab === 'papers' && 'Research Paper Gaps (arXiv 2508.20953 & arXiv 2507.15385)'}
                {bottomTab === 'sources' && 'Assumptions, Data Sources & Official Citations'}
              </h2>
              <button
                onClick={() => setBottomTab('none')}
                style={{
                  backgroundColor: '#1e293b',
                  border: '1px solid #334155',
                  color: '#94a3b8',
                  padding: '4px 12px',
                  borderRadius: '6px',
                  fontWeight: 700,
                  fontSize: '12px',
                }}
              >
                Close ✕
              </button>
            </div>

            {/* Tab 1: Wards */}
            {bottomTab === 'wards' && currSim?.per_zone && (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', fontSize: '12px', borderCollapse: 'collapse', textAlign: 'left' }}>
                  <thead>
                    <tr style={{ color: '#94a3b8', borderBottom: '1px solid #334155' }}>
                      <th style={{ padding: '8px' }}>Zone Name</th>
                      <th style={{ padding: '8px' }}>28-Day Calls</th>
                      <th style={{ padding: '8px' }}>Within Target % (Plan)</th>
                      <th style={{ padding: '8px' }}>Within Target % (Today)</th>
                      <th style={{ padding: '8px' }}>Median Response</th>
                      <th style={{ padding: '8px' }}>90th Percentile</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(currSim.per_zone)
                      .sort((a: any, b: any) => a[1].within_target_pct - b[1].within_target_pct)
                      .slice(0, 15)
                      .map(([zName, stats]: any) => {
                        const baseStats = baseSim?.per_zone?.[zName]
                        return (
                          <tr key={zName} style={{ borderBottom: '1px solid #1e293b' }}>
                            <td style={{ padding: '8px', fontWeight: 700 }}>{zName}</td>
                            <td style={{ padding: '8px' }}>{stats.calls}</td>
                            <td style={{ padding: '8px', color: stats.within_target_pct >= 70 ? '#10b981' : '#f59e0b', fontWeight: 700 }}>
                              {stats.within_target_pct}%
                            </td>
                            <td style={{ padding: '8px', color: '#94a3b8' }}>
                              {baseStats?.within_target_pct ?? '--'}%
                            </td>
                            <td style={{ padding: '8px' }}>{stats.median_min} min</td>
                            <td style={{ padding: '8px', color: '#ef4444', fontWeight: 700 }}>{stats.p90_min} min</td>
                          </tr>
                        )
                      })}
                  </tbody>
                </table>
              </div>
            )}

            {/* Tab 2: Stress Tests */}
            {bottomTab === 'stress' && scenarios?.stress_tests && (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '12px' }}>
                {scenarios.stress_tests.map((st: any, i: number) => (
                  <div key={i} style={{ backgroundColor: '#131d31', padding: '12px', borderRadius: '8px', border: '1px solid #334155' }}>
                    <div style={{ fontWeight: 800, fontSize: '13px', color: '#38bdf8' }}>{st.name}</div>
                    <div style={{ fontSize: '11px', color: '#94a3b8', margin: '4px 0 8px 0' }}>{st.description}</div>
                    <div style={{ fontSize: '12px', marginTop: '4px' }}>
                      <span style={{ color: '#94a3b8' }}>Today in-time: </span>
                      <strong>{st.baseline_within_pct}%</strong>
                    </div>
                    <div style={{ fontSize: '12px', marginTop: '2px' }}>
                      <span style={{ color: '#94a3b8' }}>GoldenHour: </span>
                      <strong style={{ color: '#10b981' }}>{st.goldenhour_within_pct}%</strong>
                    </div>
                    <div style={{ fontSize: '12px', marginTop: '4px' }}>
                      <span style={{ color: '#94a3b8' }}>90th Percentile: </span>
                      <span style={{ color: '#ef4444' }}>{st.goldenhour_p90}m</span> vs {st.baseline_p90}m
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Tab 3: Fleet Curve */}
            {bottomTab === 'fleet' && scenarios?.fleet_curve && (
              <div>
                <p style={{ fontSize: '12px', color: '#cbd5e1', marginBottom: '10px', lineHeight: 1.5 }}>
                  Simulated expected coverage floor across fleet sizes. In Bengaluru, doubling ambulances from 72 to 144 yields diminishing returns (+4%), whereas in rural Tumakuru, reaching the 10-minute target is mathematically impossible without at least 75+ vehicles.
                </p>
                <div style={{ display: 'flex', gap: '12px' }}>
                  {scenarios.fleet_curve.map((fc: any, i: number) => (
                    <div key={i} style={{ flex: 1, backgroundColor: '#131d31', padding: '12px', borderRadius: '8px', textAlign: 'center', border: '1px solid #334155' }}>
                      <div style={{ fontSize: '12px', color: '#94a3b8' }}>{fc.multiplier}x Fleet</div>
                      <div style={{ fontSize: '20px', fontWeight: 800, margin: '4px 0', color: '#38bdf8' }}>
                        {fc.ambulances} Amb
                      </div>
                      <div style={{ fontSize: '13px', fontWeight: 700, color: '#10b981' }}>
                        {fc.expected_cover_pct}% Cov
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Tab 4: Forecast */}
            {bottomTab === 'forecast' && (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: '16px' }}>
                <div>
                  <div style={{ fontSize: '12px', color: '#cbd5e1', marginBottom: '8px' }}>
                    Demand is modelled using LightGBM Poisson regression on ward history, calendar signals, and 4-week lag features.
                  </div>
                  <div style={{ backgroundColor: '#131d31', padding: '10px', borderRadius: '6px', fontSize: '12px' }}>
                    <div>Baseline 4-Wk MAE: <strong>{forecastData?.[region]?.mae_baseline ?? '--'}</strong></div>
                    <div>LightGBM MAE: <strong style={{ color: '#10b981' }}>{forecastData?.[region]?.mae_lgb ?? '--'}</strong></div>
                    <div style={{ color: '#38bdf8', marginTop: '4px', fontWeight: 700 }}>
                      Error Reduction: {forecastData?.[region]?.mae_improvement_pct ?? '--'}%
                    </div>
                  </div>
                  <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '6px' }}>
                    * Synthetic call log generated from published CAG volume aggregates.
                  </div>
                </div>
                <div>{renderForecastChart()}</div>
              </div>
            )}

            {/* Tab 5: Papers */}
            {bottomTab === 'papers' && (
              <div style={{ fontSize: '12px', color: '#cbd5e1' }}>
                <p style={{ marginBottom: '8px' }}>
                  <strong>Paper 1 (arXiv 2508.20953 - NSGA-II Rostering) & Paper 2 (arXiv 2507.15385 - Transformer MIP Pruning):</strong>
                </p>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                  <div style={{ backgroundColor: '#131d31', padding: '12px', borderRadius: '8px', border: '1px solid #334155' }}>
                    <div style={{ fontWeight: 800, color: '#ef4444' }}>Paper 1 Gap & Our Answer</div>
                    <div style={{ marginTop: '4px' }}>
                      <strong>Gap:</strong> Soft penalties for patient care trade away critical health outcomes for staff satisfaction.
                    </div>
                    <div style={{ marginTop: '4px', color: '#10b981' }}>
                      <strong>Our Answer:</strong> Patient care is a hard constraint floor (50% ward equity floor, critical ALS weighting); crew moves are optimized second.
                    </div>
                  </div>
                  <div style={{ backgroundColor: '#131d31', padding: '12px', borderRadius: '8px', border: '1px solid #334155' }}>
                    <div style={{ fontWeight: 800, color: '#38bdf8' }}>Paper 2 Gap & Our Answer</div>
                    <div style={{ marginTop: '4px' }}>
                      <strong>Gap:</strong> Evaluated on toy 13-node grid; Acc1 only ~64%; tied to fixed bus network.
                    </div>
                    <div style={{ marginTop: '4px', color: '#10b981' }}>
                      <strong>Our Answer:</strong> Candidate sites represented as tokens on 1.7M-node OpenStreetMap road graph; transfers seamlessly between urban Bengaluru and rural Tumakuru.
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* Tab 6: Sources */}
            {bottomTab === 'sources' && (
              <div style={{ fontSize: '12px', color: '#cbd5e1' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
                  <thead>
                    <tr style={{ color: '#94a3b8', borderBottom: '1px solid #334155' }}>
                      <th style={{ padding: '6px' }}>Type</th>
                      <th style={{ padding: '6px' }}>Source Document</th>
                      <th style={{ padding: '6px' }}>Verified Data & Facts</th>
                    </tr>
                  </thead>
                  <tbody>
                    {meta?.assumptions?.map((a: any, i: number) => (
                      <tr key={i} style={{ borderBottom: '1px solid #1e293b' }}>
                        <td style={{ padding: '6px', fontWeight: 800 }}>{a.symbol} {a.label}</td>
                        <td style={{ padding: '6px', color: '#38bdf8' }}>{a.source}</td>
                        <td style={{ padding: '6px' }}>{a.details}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
