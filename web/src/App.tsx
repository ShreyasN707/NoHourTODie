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
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  Layers,
  Pause,
  Play,
  Search,
  X,
  Zap,
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
      const totalTypes = (d.a > 0 ? 1 : 0) + (d.b > 0 ? 1 : 0) + (d.y > 0 ? 1 : 0)

      // ALS Ambulance
      if (d.a > 0) {
        const offX = totalTypes > 1 ? -0.0008 : 0
        const offY = totalTypes > 1 ? 0.0006 : 0
        markers.push({
          ...d,
          vType: 'ALS',
          count: d.a,
          iconUrl: '/icons/icon-als.svg',
          color: [239, 68, 68],
          offsetX: offX,
          offsetY: offY,
        })
        if (d.a > 1) {
          badges.push({
            lat: d.lat + offY + 0.0010,
            lon: d.lon + offX,
            text: `${d.a}× ALS`,
            color: [239, 68, 68],
          })
        }
      }

      // BLS Ambulance
      if (d.b > 0) {
        const offX = totalTypes > 1 ? 0.0008 : 0
        const offY = totalTypes > 1 ? -0.0006 : 0
        markers.push({
          ...d,
          vType: 'BLS',
          count: d.b,
          iconUrl: '/icons/icon-bls.svg',
          color: [59, 130, 246],
          offsetX: offX,
          offsetY: offY,
        })
        if (d.b > 1) {
          badges.push({
            lat: d.lat + offY + 0.0010,
            lon: d.lon + offX,
            text: `${d.b}× BLS`,
            color: [59, 130, 246],
          })
        }
      }

      // Bike Paramedic
      if (d.y > 0) {
        const offX = totalTypes > 2 ? 0 : (totalTypes > 1 ? 0.0008 : 0)
        const offY = totalTypes > 1 ? 0.0012 : 0
        markers.push({
          ...d,
          vType: 'Bike',
          count: d.y,
          iconUrl: '/icons/icon-bike.svg',
          color: [249, 115, 22],
          offsetX: offX,
          offsetY: offY,
        })
        if (d.y > 1) {
          badges.push({
            lat: d.lat + offY + 0.0010,
            lon: d.lon + offX,
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
        lineWidthMinPixels: 0.8,
        getLineColor: [255, 255, 255, 80], // Clean translucent white borders (eliminates dark wireframe)
        getHexagon: (d: any) => d.h3,
        getFillColor: (d: any, { index }: any) => {
          // Layer 1: Reachability
          if (layerMode === 'reach') {
            const reachable = scenarios?.reach?.[index]
            return reachable
              ? [34, 197, 94, 150] // Clear Emerald Green
              : [239, 68, 68, 175]  // Warning Red
          }

          // Layer 2: Demand Density (Multi-Stop Heatmap)
          if (layerMode === 'demand') {
            const lam = d.lam?.[blockId] || 0
            if (lam > 0.10) return [239, 68, 68, 190] // High Demand: Red
            if (lam > 0.05) return [245, 158, 11, 165] // Medium-High: Amber
            if (lam > 0.02) return [59, 130, 246, 140] // Moderate: Blue
            return [148, 163, 184, 75] // Low: Soft Slate
          }

          // Layer 3: Response Time in Minutes
          if (layerMode === 'nearest') {
            const m = currBlockPlan.nearest_min?.[index] ?? 25
            if (m <= targetMin * 0.55) return [34, 197, 94, 150] // < 8 min: Emerald Green
            if (m <= targetMin) return [245, 158, 11, 150]       // 8 - 15 min: Warm Amber
            if (m <= targetMin * 1.5) return [249, 115, 22, 170] // 15 - 22 min: Orange
            return [239, 68, 68, 190]                           // > 22 min: Critical Red
          }

          // Layer 4: Chance In-Time (Expected Cover)
          const cov = currBlockPlan.cover?.[index] ?? 0
          if (cov >= 0.75) return [34, 197, 94, 150] // High Coverage: Emerald Green
          if (cov >= 0.50) return [245, 158, 11, 150] // Moderate: Warm Amber
          return [239, 68, 68, 175]                   // Delayed/Sub-floor: Critical Red
        },
        updateTriggers: {
          getFillColor: [layerMode, blockId, planType, currBlockPlan],
        },
      })
      : null

    // 2. Municipal Ward Boundaries Layer (BBMP Wards / Taluks)
    const zoneLayer =
      showWards && zonesGeo
        ? new GeoJsonLayer({
          id: 'admin-zones',
          data: zonesGeo,
          pickable: false,
          stroked: true,
          filled: false,
          lineWidthMinPixels: 2.0,
          getLineColor: [51, 65, 85, 200], // Solid Slate-700 boundary lines
        })
        : null

    // 3. Crew Move Arcs Layer (Relocations between shifts)
    const movesLayer =
      showArcs && blockMoves.length > 0
        ? new ArcLayer({
          id: 'crew-moves-arcs',
          data: blockMoves,
          pickable: true,
          getWidth: 4,
          getSourcePosition: (d: any) => [d.from_lon, d.from_lat],
          getTargetPosition: (d: any) => [d.to_lon, d.to_lat],
          // Gradient: Green (Origin) ➔ Red (Destination)
          getSourceColor: [34, 197, 94, 240],
          getTargetColor: [239, 68, 68, 250],
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
          filled: true,
          lineWidthMinPixels: 2.5,
          radiusMinPixels: 12,
          radiusMaxPixels: 22,
          getPosition: (d: any) => [d.lon, d.lat],
          getRadius: 300,
          getFillColor: [239, 68, 68, 70],  // Soft red target fill
          getLineColor: [239, 68, 68, 255], // Red destination beacon ring
        })
        : null

    // 5. High-Visibility Vehicle Backing Halos (Crisp white disc for perfect contrast against any map fill)
    const vehicleHaloLayer = showVehicles
      ? new ScatterplotLayer({
          id: 'vehicle-halos',
          data: vehicleMarkers,
          pickable: false,
          stroked: true,
          filled: true,
          lineWidthMinPixels: 1.5,
          radiusMinPixels: 13,
          radiusMaxPixels: 17,
          getPosition: (d: any) => [d.lon + (d.offsetX || 0), d.lat + (d.offsetY || 0)],
          getRadius: 160,
          getFillColor: [255, 255, 255, 240], // Crisp solid white backdrop disc
          getLineColor: [30, 41, 59, 160],    // Crisp dark slate outline
        })
      : null

    // 6. Professional Ambulance & Vehicle Symbols (IconLayer)
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
        getPosition: (d: any) => [d.lon + (d.offsetX || 0), d.lat + (d.offsetY || 0)],
        getSize: (d: any) => (d.count > 1 ? 32 : 26),
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
        getSize: 10,
        getColor: [255, 255, 255, 255],
        getTextAnchor: 'middle',
        getAlignmentBaseline: 'center',
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        fontWeight: 800,
        background: true,
        getBackgroundColor: [15, 23, 42, 240],
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
      <div className="cmd-card">
        <div className="cmd-card-header">
          <span style={{ display: 'flex', alignItems: 'center', gap: '6px', color: '#ffffff' }}>
            <BarChart3 size={13} color="#38bdf8" />
            Response Distribution (0–60 min)
          </span>
          <span style={{ color: '#ef4444', fontWeight: 800 }}>Mandate: {targetMin}m</span>
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
      {/* 1. Left Sidebar: Operator Command Center (Rebuilt) */}
      <div
        className="sidebar-container"
        style={{
          width: '450px',
          minWidth: '420px',
          height: '100%',
          backgroundColor: '#070b14',
          backgroundImage: 'radial-gradient(circle at 10% 20%, rgba(30, 58, 138, 0.12) 0%, transparent 40%)',
          borderRight: '1.5px solid rgba(255, 255, 255, 0.1)',
          zIndex: 10,
          display: 'flex',
          flexDirection: 'column',
          overflowY: 'auto',
          padding: '20px 18px',
          boxShadow: '6px 0 32px rgba(0, 0, 0, 0.75)',
        }}
      >
        {/* Header Bar */}
        <div
          style={{
            marginBottom: '16px',
            paddingBottom: '14px',
            borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <div
                style={{
                  width: '38px',
                  height: '38px',
                  borderRadius: '10px',
                  background: 'linear-gradient(135deg, #ef4444 0%, #b91c1c 100%)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  boxShadow: '0 0 16px rgba(239, 68, 68, 0.5)',
                }}
              >
                <Activity color="#ffffff" size={22} strokeWidth={2.5} />
              </div>
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <h1 style={{ fontSize: '21px', fontWeight: 900, letterSpacing: '-0.4px', color: '#ffffff', lineHeight: 1 }}>
                    GoldenHour
                  </h1>
                  <span
                    style={{
                      fontSize: '10px',
                      padding: '2px 8px',
                      borderRadius: '5px',
                      backgroundColor: 'rgba(56, 189, 248, 0.15)',
                      color: '#38bdf8',
                      fontWeight: 800,
                      border: '1px solid rgba(56, 189, 248, 0.35)',
                      letterSpacing: '0.04em',
                    }}
                  >
                    108 CAD
                  </span>
                </div>
                <div style={{ fontSize: '11px', color: '#94a3b8', fontWeight: 600, marginTop: '3px' }}>
                  Karnataka Emergency Fleet Allocation
                </div>
              </div>
            </div>

            {/* Live Beacon */}
            <div
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '6px',
                padding: '4px 10px',
                borderRadius: '20px',
                backgroundColor: 'rgba(16, 185, 129, 0.15)',
                border: '1px solid rgba(16, 185, 129, 0.4)',
                boxShadow: '0 0 12px rgba(16, 185, 129, 0.2)',
              }}
            >
              <span className="pulse-beacon" />
              <span style={{ fontSize: '11px', color: '#34d399', fontWeight: 800 }}>LIVE</span>
            </div>
          </div>
        </div>

        {/* Region Switcher */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: '1fr 1fr',
            backgroundColor: '#0c1322',
            padding: '4px',
            borderRadius: '10px',
            border: '1px solid rgba(255, 255, 255, 0.08)',
            gap: '6px',
            marginBottom: '14px',
          }}
        >
          <button
            onClick={() => setRegion('blr')}
            style={{
              padding: '9px 10px',
              borderRadius: '7px',
              backgroundColor: region === 'blr' ? '#2563eb' : 'transparent',
              color: region === 'blr' ? '#ffffff' : '#94a3b8',
              fontWeight: 800,
              fontSize: '12px',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              border: region === 'blr' ? '1px solid #60a5fa' : '1px solid transparent',
              boxShadow: region === 'blr' ? '0 2px 10px rgba(37, 99, 235, 0.5)' : 'none',
              cursor: 'pointer',
              transition: 'all 0.15s ease',
            }}
          >
            <span>🏙️ Bengaluru (Urban)</span>
            <span style={{ fontSize: '10px', opacity: 0.8, marginTop: '2px', fontWeight: 600 }}>15m Mandate</span>
          </button>
          <button
            onClick={() => setRegion('tmk')}
            style={{
              padding: '9px 10px',
              borderRadius: '7px',
              backgroundColor: region === 'tmk' ? '#2563eb' : 'transparent',
              color: region === 'tmk' ? '#ffffff' : '#94a3b8',
              fontWeight: 800,
              fontSize: '12px',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              border: region === 'tmk' ? '1px solid #60a5fa' : '1px solid transparent',
              boxShadow: region === 'tmk' ? '0 2px 10px rgba(37, 99, 235, 0.5)' : 'none',
              cursor: 'pointer',
              transition: 'all 0.15s ease',
            }}
          >
            <span>🌾 Tumakuru (Rural)</span>
            <span style={{ fontSize: '10px', opacity: 0.8, marginTop: '2px', fontWeight: 600 }}>10m Mandate</span>
          </button>
        </div>

        {/* Section 1: Strategic Deployment Model */}
        <div
          style={{
            backgroundColor: '#0f172a',
            border: '1px solid rgba(255, 255, 255, 0.08)',
            borderRadius: '10px',
            padding: '13px 14px',
            marginBottom: '13px',
            boxShadow: '0 4px 14px rgba(0, 0, 0, 0.3)',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '9px' }}>
            <span style={{ fontSize: '11px', textTransform: 'uppercase', color: '#94a3b8', fontWeight: 800, letterSpacing: '0.06em', display: 'flex', alignItems: 'center', gap: '5px' }}>
              <Layers size={13} color="#38bdf8" />
              Deployment Strategy
            </span>
            <span style={{ fontSize: '11px', fontWeight: 800, color: planType === 'goldenhour' ? '#34d399' : '#38bdf8' }}>
              {planType === 'goldenhour' ? '★ Optimal Dynamic' : planType === 'static' ? 'Static Fixed' : 'CAG Baseline'}
            </span>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '5px', marginBottom: '9px' }}>
            <button
              onClick={() => setPlanType('baseline')}
              style={{
                padding: '9px 6px',
                borderRadius: '8px',
                backgroundColor: planType === 'baseline' ? '#1e293b' : '#080d1a',
                color: planType === 'baseline' ? '#ffffff' : '#94a3b8',
                border: planType === 'baseline' ? '1.5px solid #64748b' : '1px solid rgba(255, 255, 255, 0.06)',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                cursor: 'pointer',
                textAlign: 'center',
              }}
            >
              <span style={{ fontSize: '11.5px', fontWeight: 800 }}>1. Baseline</span>
              <span style={{ fontSize: '9px', opacity: 0.75 }}>CAG Audit</span>
            </button>
            <button
              onClick={() => setPlanType('static')}
              style={{
                padding: '9px 6px',
                borderRadius: '8px',
                backgroundColor: planType === 'static' ? '#1e3a5f' : '#080d1a',
                color: planType === 'static' ? '#38bdf8' : '#94a3b8',
                border: planType === 'static' ? '1.5px solid #38bdf8' : '1px solid rgba(255, 255, 255, 0.06)',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                cursor: 'pointer',
                textAlign: 'center',
              }}
            >
              <span style={{ fontSize: '11.5px', fontWeight: 800 }}>2. Static</span>
              <span style={{ fontSize: '9px', opacity: 0.75 }}>Fixed POIs</span>
            </button>
            <button
              onClick={() => setPlanType('goldenhour')}
              style={{
                padding: '9px 6px',
                borderRadius: '8px',
                background: planType === 'goldenhour' ? 'linear-gradient(135deg, #064e3b 0%, #065f46 100%)' : '#080d1a',
                color: planType === 'goldenhour' ? '#ffffff' : '#a7f3d0',
                border: planType === 'goldenhour' ? '1.5px solid #10b981' : '1px solid rgba(255, 255, 255, 0.06)',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                cursor: 'pointer',
                textAlign: 'center',
                boxShadow: planType === 'goldenhour' ? '0 0 12px rgba(16, 185, 129, 0.35)' : 'none',
              }}
            >
              <span style={{ fontSize: '11.5px', fontWeight: 900 }}>3. Dynamic ★</span>
              <span style={{ fontSize: '9px', opacity: 0.9 }}>MILP + Bikes</span>
            </button>
          </div>

          {/* Strategy Details Box */}
          <div
            style={{
              padding: '9px 12px',
              borderRadius: '7px',
              backgroundColor: planType === 'goldenhour' ? 'rgba(6, 78, 59, 0.35)' : 'rgba(15, 23, 42, 0.7)',
              border: `1px solid ${planType === 'goldenhour' ? 'rgba(16, 185, 129, 0.4)' : 'rgba(255, 255, 255, 0.08)'}`,
              fontSize: '11.5px',
              color: planType === 'goldenhour' ? '#a7f3d0' : '#cbd5e1',
              lineHeight: 1.45,
            }}
          >
            {planType === 'baseline' && (
              <span><strong>Today (CAG Audit Baseline):</strong> Ambulances parked only at government hospitals based on population. Zero bike first-responders.</span>
            )}
            {planType === 'static' && (
              <span><strong>Best Fixed Hotspots:</strong> Static placement selected once for peak rush hour congestion. No shift relocations.</span>
            )}
            {planType === 'goldenhour' && (
              <span><strong>GoldenHour Dynamic Optimization:</strong> Multi-period rolling relocations (≤25% crew fatigue cap) + 30 bike first-responders for rapid bypass.</span>
            )}
          </div>
        </div>

        {/* Fleet & Mandate Bar */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '10px 14px',
            backgroundColor: '#0c1322',
            borderRadius: '9px',
            border: '1px solid rgba(255, 255, 255, 0.08)',
            marginBottom: '13px',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            <span style={{ color: '#94a3b8', fontSize: '11px', textTransform: 'uppercase', fontWeight: 800 }}>Fleet:</span>
            <span style={{ fontWeight: 900, color: '#ffffff', fontSize: '13px' }}>
              {scenarios?.baseline?.fleet?.ambulances || 0} Ambulances
            </span>
            {region === 'blr' && (
              <span style={{ color: '#f97316', fontWeight: 800, backgroundColor: 'rgba(249, 115, 22, 0.16)', padding: '2px 8px', borderRadius: '5px', fontSize: '11px', border: '1px solid rgba(249, 115, 22, 0.35)' }}>
                + 30 Bikes
              </span>
            )}
          </div>
          <div style={{ borderLeft: '1px solid rgba(255, 255, 255, 0.12)', paddingLeft: '12px', textAlign: 'right' }}>
            <span style={{ color: '#94a3b8', fontSize: '10.5px' }}>Cabinet Target: </span>
            <span style={{ fontWeight: 900, color: '#ef4444', fontSize: '13px' }}>≤ {targetMin} min</span>
          </div>
        </div>

        {/* Section 2: Shift Horizon Stepper with Quick-Jump Pills */}
        <div
          style={{
            backgroundColor: '#0f172a',
            border: '1px solid rgba(255, 255, 255, 0.08)',
            borderRadius: '10px',
            padding: '13px 14px',
            marginBottom: '13px',
            boxShadow: '0 4px 14px rgba(0, 0, 0, 0.3)',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
            <span style={{ fontSize: '11px', textTransform: 'uppercase', color: '#94a3b8', fontWeight: 800, letterSpacing: '0.06em', display: 'flex', alignItems: 'center', gap: '5px' }}>
              <Clock size={13} color="#f59e0b" />
              Shift Horizon (24h)
            </span>
            <span
              style={{
                fontSize: '11px',
                fontWeight: 800,
                padding: '2px 9px',
                borderRadius: '5px',
                backgroundColor: blockId === 4 ? 'rgba(239, 68, 68, 0.25)' : 'rgba(56, 189, 248, 0.14)',
                color: blockId === 4 ? '#f87171' : '#38bdf8',
                border: `1px solid ${blockId === 4 ? 'rgba(239, 68, 68, 0.5)' : 'rgba(56, 189, 248, 0.3)'}`,
              }}
            >
              {blockId === 4 ? '🔥 PEAK RUSH (2.6× Congestion)' : `Shift ${blockId + 1}/6 • Normal Flow`}
            </span>
          </div>

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
            <div style={{ fontSize: '13px', fontWeight: 800, color: '#ffffff' }}>
              {BLOCK_NAMES[blockId]}
            </div>

            {/* Stepper Chevrons & Playback */}
            <div style={{ display: 'flex', gap: '5px' }}>
              <button
                onClick={() => setBlockId((prev) => (prev === 0 ? 5 : prev - 1))}
                style={{
                  backgroundColor: '#162032',
                  border: '1px solid rgba(255, 255, 255, 0.1)',
                  borderRadius: '6px',
                  padding: '5px 8px',
                  color: '#ffffff',
                  display: 'flex',
                  alignItems: 'center',
                  cursor: 'pointer',
                }}
                title="Previous Shift"
              >
                <ChevronLeft size={14} />
              </button>
              <button
                onClick={() => setIsPlaying(!isPlaying)}
                style={{
                  backgroundColor: isPlaying ? '#ef4444' : '#2563eb',
                  borderRadius: '6px',
                  padding: '5px 11px',
                  color: '#ffffff',
                  fontSize: '11px',
                  fontWeight: 800,
                  display: 'flex',
                  alignItems: 'center',
                  gap: '5px',
                  cursor: 'pointer',
                  boxShadow: isPlaying ? '0 0 12px rgba(239,68,68,0.5)' : '0 0 12px rgba(37,99,235,0.4)',
                }}
              >
                {isPlaying ? <Pause size={12} /> : <Play size={12} />}
                {isPlaying ? 'Pause' : 'Play 24h'}
              </button>
              <button
                onClick={() => setBlockId((prev) => (prev + 1) % 6)}
                style={{
                  backgroundColor: '#162032',
                  border: '1px solid rgba(255, 255, 255, 0.1)',
                  borderRadius: '6px',
                  padding: '5px 8px',
                  color: '#ffffff',
                  display: 'flex',
                  alignItems: 'center',
                  cursor: 'pointer',
                }}
                title="Next Shift"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </div>

          {/* 6 Quick-Click Shift Jump Pills */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: '4px', marginBottom: '8px' }}>
            {[
              { id: 0, label: '00-04' },
              { id: 1, label: '04-08' },
              { id: 2, label: '08-12' },
              { id: 3, label: '12-16' },
              { id: 4, label: '16-20 🔥' },
              { id: 5, label: '20-24' },
            ].map((s) => (
              <button
                key={s.id}
                onClick={() => setBlockId(s.id)}
                style={{
                  padding: '5px 2px',
                  borderRadius: '5px',
                  fontSize: '10.5px',
                  fontWeight: blockId === s.id ? 900 : 700,
                  backgroundColor: blockId === s.id ? (s.id === 4 ? '#dc2626' : '#2563eb') : '#0b111e',
                  color: blockId === s.id ? '#ffffff' : '#94a3b8',
                  border: blockId === s.id ? '1px solid #ffffff' : '1px solid rgba(255, 255, 255, 0.08)',
                  cursor: 'pointer',
                  textAlign: 'center',
                }}
              >
                {s.label}
              </button>
            ))}
          </div>

          <input
            type="range"
            min={0}
            max={5}
            step={1}
            value={blockId}
            onChange={(e) => setBlockId(Number(e.target.value))}
            style={{ width: '100%', marginTop: '4px' }}
          />

          {planType === 'goldenhour' && blockMoves.length > 0 && (
            <div style={{ marginTop: '9px', padding: '7px 10px', borderRadius: '6px', background: 'rgba(245, 158, 11, 0.14)', border: '1px solid rgba(245, 158, 11, 0.35)', color: '#fbbf24', fontSize: '11px', fontWeight: 700 }}>
              ➔ {blockMoves.length} crew relocations active between stations this shift.
            </div>
          )}
        </div>

        {/* Section 3: 4 Emergency Response KPI Cards */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', marginBottom: '13px' }}>
          {/* KPI 1 */}
          <div
            style={{
              backgroundColor: '#0f172a',
              border: '1px solid rgba(255, 255, 255, 0.08)',
              borderTop: '3px solid #10b981',
              borderRadius: '10px',
              padding: '12px 13px',
              boxShadow: '0 4px 12px rgba(0, 0, 0, 0.25)',
            }}
          >
            <div style={{ fontSize: '11px', fontWeight: 700, color: '#94a3b8', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span>≤ {targetMin}m Target Met</span>
              <CheckCircle2 size={13} color="#10b981" />
            </div>
            <div style={{ fontSize: '26px', fontWeight: 900, color: '#ffffff', margin: '4px 0 5px 0', lineHeight: 1.1, letterSpacing: '-0.03em' }}>
              {currSim ? currSim.within_target_pct + '%' : '--'}
            </div>
            <div>
              <span className={deltaTarget >= 0 ? 'delta-pill-good' : 'delta-pill-warn'}>
                {deltaTarget >= 0 ? `▲ +${deltaTarget.toFixed(1)}%` : `▼ ${deltaTarget.toFixed(1)}%`} vs Base
              </span>
            </div>
          </div>

          {/* KPI 2 */}
          <div
            style={{
              backgroundColor: '#0f172a',
              border: '1px solid rgba(255, 255, 255, 0.08)',
              borderTop: '3px solid #38bdf8',
              borderRadius: '10px',
              padding: '12px 13px',
              boxShadow: '0 4px 12px rgba(0, 0, 0, 0.25)',
            }}
          >
            <div style={{ fontSize: '11px', fontWeight: 700, color: '#94a3b8', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span>Median Response</span>
              <Clock size={13} color="#38bdf8" />
            </div>
            <div style={{ fontSize: '26px', fontWeight: 900, color: '#ffffff', margin: '4px 0 5px 0', lineHeight: 1.1, letterSpacing: '-0.03em' }}>
              {currSim ? `${currSim.median_min}m` : '--'}
            </div>
            <div>
              <span className={deltaMedian <= 0 ? 'delta-pill-good' : 'delta-pill-warn'}>
                {deltaMedian <= 0 ? `▼ ${Math.abs(deltaMedian).toFixed(1)}m faster` : `▲ +${deltaMedian.toFixed(1)}m`}
              </span>
            </div>
          </div>

          {/* KPI 3 */}
          <div
            style={{
              backgroundColor: '#0f172a',
              border: '1px solid rgba(255, 255, 255, 0.08)',
              borderTop: '3px solid #f59e0b',
              borderRadius: '10px',
              padding: '12px 13px',
              boxShadow: '0 4px 12px rgba(0, 0, 0, 0.25)',
            }}
          >
            <div style={{ fontSize: '11px', fontWeight: 700, color: '#94a3b8', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span>90th-Pctl Wait</span>
              <AlertTriangle size={13} color="#f59e0b" />
            </div>
            <div style={{ fontSize: '26px', fontWeight: 900, color: '#ffffff', margin: '4px 0 5px 0', lineHeight: 1.1, letterSpacing: '-0.03em' }}>
              {currSim ? `${currSim.p90_min}m` : '--'}
            </div>
            <div>
              <span className={deltaP90 <= 0 ? 'delta-pill-good' : 'delta-pill-warn'}>
                {deltaP90 <= 0 ? `▼ ${Math.abs(deltaP90).toFixed(1)}m drop` : `▲ +${deltaP90.toFixed(1)}m`}
              </span>
            </div>
          </div>

          {/* KPI 4 */}
          <div
            style={{
              backgroundColor: '#0f172a',
              border: '1px solid rgba(255, 255, 255, 0.08)',
              borderTop: '3px solid #f97316',
              borderRadius: '10px',
              padding: '12px 13px',
              boxShadow: '0 4px 12px rgba(0, 0, 0, 0.25)',
            }}
          >
            <div style={{ fontSize: '11px', fontWeight: 700, color: '#94a3b8', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span>First Help ≤ 10m</span>
              <Zap size={13} color="#f97316" />
            </div>
            <div style={{ fontSize: '26px', fontWeight: 900, color: '#fb923c', margin: '4px 0 5px 0', lineHeight: 1.1, letterSpacing: '-0.03em' }}>
              {currSim ? `${currSim.first_help_10m_pct}%` : '--'}
            </div>
            <div>
              <span className="delta-pill-orange">
                {deltaBike >= 0 ? `▲ +${deltaBike.toFixed(1)}%` : `${deltaBike.toFixed(1)}%`} Bike
              </span>
            </div>
          </div>
        </div>

        {/* Selected Station / Selected Move Inspector Panel */}
        {selectedStation && (
          <div style={{ backgroundColor: '#131d31', border: '2px solid #3b82f6', borderRadius: '10px', padding: '13px', marginBottom: '13px', position: 'relative' }}>
            <button
              onClick={() => setSelectedStation(null)}
              style={{ position: 'absolute', top: '8px', right: '8px', background: 'transparent', border: 'none', color: '#94a3b8', cursor: 'pointer' }}
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
          <div style={{ backgroundColor: '#131d31', border: '2px solid #ef4444', borderRadius: '10px', padding: '13px', marginBottom: '13px', position: 'relative' }}>
            <button
              onClick={() => setSelectedMove(null)}
              style={{ position: 'absolute', top: '8px', right: '8px', background: 'transparent', border: 'none', color: '#94a3b8', cursor: 'pointer' }}
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

        {/* Section 4: Response Time Distribution Curve */}
        {renderHistogram()}

        {/* Section 5: Telemetry Drawers Grid */}
        <div style={{ marginTop: 'auto', paddingTop: '16px', borderTop: '1px solid rgba(255, 255, 255, 0.08)' }}>
          <div style={{ fontSize: '11px', textTransform: 'uppercase', color: '#94a3b8', fontWeight: 800, marginBottom: '8px', letterSpacing: '0.07em' }}>
            Operational Telemetry Drawers
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px' }}>
            <button
              onClick={() => setBottomTab(bottomTab === 'wards' ? 'none' : 'wards')}
              className={`drawer-btn ${bottomTab === 'wards' ? 'active' : ''}`}
            >
              <span>📊 Wards Leaderboard</span>
            </button>
            <button
              onClick={() => setBottomTab(bottomTab === 'stress' ? 'none' : 'stress')}
              className={`drawer-btn ${bottomTab === 'stress' ? 'active' : ''}`}
            >
              <span>⚡ Stress Testing</span>
            </button>
            <button
              onClick={() => setBottomTab(bottomTab === 'fleet' ? 'none' : 'fleet')}
              className={`drawer-btn ${bottomTab === 'fleet' ? 'active' : ''}`}
            >
              <span>📈 Fleet Sizing</span>
            </button>
            <button
              onClick={() => setBottomTab(bottomTab === 'forecast' ? 'none' : 'forecast')}
              className={`drawer-btn ${bottomTab === 'forecast' ? 'active' : ''}`}
            >
              <span>🧠 Demand ML</span>
            </button>
            <button
              onClick={() => setBottomTab(bottomTab === 'papers' ? 'none' : 'papers')}
              className={`drawer-btn ${bottomTab === 'papers' ? 'active' : ''}`}
            >
              <span>📑 Paper Gaps</span>
            </button>
            <button
              onClick={() => setBottomTab(bottomTab === 'sources' ? 'none' : 'sources')}
              className={`drawer-btn ${bottomTab === 'sources' ? 'active' : ''}`}
            >
              <span>🔍 CAG Audit Sources</span>
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
            backgroundColor: 'rgba(12, 19, 34, 0.94)',
            backdropFilter: 'blur(16px)',
            border: '1px solid rgba(255, 255, 255, 0.1)',
            borderRadius: '10px',
            padding: '12px 14px',
            boxShadow: '0 8px 32px rgba(0,0,0,0.6)',
            maxWidth: '380px',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <Layers size={14} color="#38bdf8" />
              <span style={{ fontSize: '11px', textTransform: 'uppercase', color: '#94a3b8', fontWeight: 800, letterSpacing: '0.5px' }}>
                Map Telemetry Metric
              </span>
            </div>
            <span style={{ fontSize: '10px', color: '#10b981', fontWeight: 700 }}>Deck.gl 9.2</span>
          </div>

          <div
            style={{
              display: 'grid',
              gridTemplateColumns: '1fr 1fr',
              gap: '4px',
              backgroundColor: 'rgba(15, 23, 42, 0.6)',
              padding: '3px',
              borderRadius: '8px',
              border: '1px solid rgba(255, 255, 255, 0.06)',
              marginBottom: '10px',
            }}
          >
            <button
              onClick={() => setLayerMode('cover')}
              style={{
                fontSize: '11.5px',
                padding: '6px 8px',
                borderRadius: '5px',
                border: 'none',
                backgroundColor: layerMode === 'cover' ? '#2563eb' : 'transparent',
                color: layerMode === 'cover' ? '#ffffff' : '#94a3b8',
                fontWeight: layerMode === 'cover' ? 800 : 600,
                boxShadow: layerMode === 'cover' ? '0 2px 6px rgba(37,99,235,0.4)' : 'none',
              }}
            >
              Chance In-Time
            </button>
            <button
              onClick={() => setLayerMode('nearest')}
              style={{
                fontSize: '11.5px',
                padding: '6px 8px',
                borderRadius: '5px',
                border: 'none',
                backgroundColor: layerMode === 'nearest' ? '#2563eb' : 'transparent',
                color: layerMode === 'nearest' ? '#ffffff' : '#94a3b8',
                fontWeight: layerMode === 'nearest' ? 800 : 600,
                boxShadow: layerMode === 'nearest' ? '0 2px 6px rgba(37,99,235,0.4)' : 'none',
              }}
            >
              Response Time
            </button>
            <button
              onClick={() => setLayerMode('demand')}
              style={{
                fontSize: '11.5px',
                padding: '6px 8px',
                borderRadius: '5px',
                border: 'none',
                backgroundColor: layerMode === 'demand' ? '#2563eb' : 'transparent',
                color: layerMode === 'demand' ? '#ffffff' : '#94a3b8',
                fontWeight: layerMode === 'demand' ? 800 : 600,
                boxShadow: layerMode === 'demand' ? '0 2px 6px rgba(37,99,235,0.4)' : 'none',
              }}
            >
              Demand Heatmap
            </button>
            <button
              onClick={() => setLayerMode('reach')}
              style={{
                fontSize: '11.5px',
                padding: '6px 8px',
                borderRadius: '5px',
                border: 'none',
                backgroundColor: layerMode === 'reach' ? '#2563eb' : 'transparent',
                color: layerMode === 'reach' ? '#ffffff' : '#94a3b8',
                fontWeight: layerMode === 'reach' ? 800 : 600,
                boxShadow: layerMode === 'reach' ? '0 2px 6px rgba(37,99,235,0.4)' : 'none',
              }}
            >
              Peak Reachable
            </button>
          </div>

          {/* Visibility Toggles */}
          <div style={{ display: 'flex', justifyContent: 'space-between', borderTop: '1px solid rgba(255, 255, 255, 0.08)', paddingTop: '8px', fontSize: '11px', color: '#cbd5e1' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer', userSelect: 'none' }}>
              <input type="checkbox" checked={showHexagons} onChange={(e) => setShowHexagons(e.target.checked)} />
              <span>Hex Grid</span>
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer', userSelect: 'none' }}>
              <input type="checkbox" checked={showVehicles} onChange={(e) => setShowVehicles(e.target.checked)} />
              <span>Vehicles</span>
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer', userSelect: 'none' }}>
              <input type="checkbox" checked={showArcs} onChange={(e) => setShowArcs(e.target.checked)} />
              <span>Shift Arcs</span>
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', cursor: 'pointer', userSelect: 'none' }}>
              <input type="checkbox" checked={showWards} onChange={(e) => setShowWards(e.target.checked)} />
              <span>Wards</span>
            </label>
          </div>

          {/* Operator Legend & Symbols */}
          <div style={{ borderTop: '1px solid rgba(255, 255, 255, 0.08)', marginTop: '8px', paddingTop: '8px' }}>
            <div style={{ fontSize: '10.5px', textTransform: 'uppercase', color: '#94a3b8', fontWeight: 800, marginBottom: '6px', letterSpacing: '0.5px' }}>
              Fleet Indications
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px', fontSize: '11.5px', color: '#f8fafc', fontWeight: 600 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                <img src="/icons/icon-als.svg" width="18" height="18" alt="ALS" />
                <span style={{ color: '#fca5a5' }}>ALS (ICU)</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                <img src="/icons/icon-bls.svg" width="18" height="18" alt="BLS" />
                <span style={{ color: '#93c5fd' }}>BLS (108)</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                <img src="/icons/icon-bike.svg" width="18" height="18" alt="Bike" />
                <span style={{ color: '#fdba74' }}>Bike</span>
              </div>
            </div>

            {/* Relocation Legend */}
            {blockMoves.length > 0 && planType === 'goldenhour' && showArcs && (
              <div style={{ marginTop: '7px', fontSize: '11px', color: '#cbd5e1', display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(15,23,42,0.6)', padding: '4px 8px', borderRadius: '4px', border: '1px solid rgba(255,255,255,0.06)' }}>
                <span style={{ fontWeight: 800, color: '#f59e0b' }}>Relocation:</span>
                <span style={{ color: '#34d399', fontWeight: 700 }}>🟢 Origin</span>
                <span style={{ color: '#94a3b8' }}>──►</span>
                <span style={{ color: '#f87171', fontWeight: 700 }}>🎯 Target</span>
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
              height: '380px',
              backgroundColor: 'rgba(12, 19, 34, 0.98)',
              backdropFilter: 'blur(20px)',
              borderTop: '2.5px solid #2563eb',
              boxShadow: '0 -12px 40px rgba(0,0,0,0.8)',
              zIndex: 25,
              display: 'flex',
              flexDirection: 'column',
              padding: '16px 24px',
              overflowY: 'auto',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px', flexWrap: 'wrap', gap: '10px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                <h2 style={{ fontSize: '15px', fontWeight: 900, color: '#f8fafc', letterSpacing: '-0.3px' }}>
                  {bottomTab === 'wards' && '📊 Municipal Ward & Taluk Response Performance (Slowest First)'}
                  {bottomTab === 'stress' && '⚡ Stress Testing & System Resilience Battery'}
                  {bottomTab === 'fleet' && '📈 Fleet Scaling Curve: How Many Vehicles Does Karnataka Need?'}
                  {bottomTab === 'forecast' && '🧠 Demand Forecasting: LightGBM vs 4-Week Moving Average'}
                  {bottomTab === 'papers' && '📑 Research Paper Gaps (arXiv 2508.20953 & arXiv 2507.15385)'}
                  {bottomTab === 'sources' && '🔍 Assumptions, Data Sources & Official Citations'}
                </h2>
              </div>

              {/* In-Drawer Quick Nav Switcher & Close Button */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <div style={{ display: 'flex', gap: '3px', backgroundColor: 'rgba(15, 23, 42, 0.7)', padding: '3px', borderRadius: '7px', border: '1px solid rgba(255, 255, 255, 0.08)' }}>
                  {[
                    { id: 'wards', label: 'Wards' },
                    { id: 'stress', label: 'Stress' },
                    { id: 'fleet', label: 'Fleet Curve' },
                    { id: 'forecast', label: 'Forecast' },
                    { id: 'papers', label: 'Papers' },
                    { id: 'sources', label: 'Sources' },
                  ].map((t) => (
                    <button
                      key={t.id}
                      onClick={() => setBottomTab(t.id as any)}
                      style={{
                        padding: '4px 9px',
                        fontSize: '11px',
                        fontWeight: bottomTab === t.id ? 800 : 600,
                        backgroundColor: bottomTab === t.id ? '#2563eb' : 'transparent',
                        color: bottomTab === t.id ? '#ffffff' : '#94a3b8',
                        borderRadius: '4px',
                      }}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>

                <button
                  onClick={() => setBottomTab('none')}
                  style={{
                    backgroundColor: '#1e293b',
                    border: '1px solid #334155',
                    color: '#f8fafc',
                    padding: '5px 12px',
                    borderRadius: '6px',
                    fontWeight: 800,
                    fontSize: '11.5px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                  }}
                >
                  <X size={14} /> Close
                </button>
              </div>
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
