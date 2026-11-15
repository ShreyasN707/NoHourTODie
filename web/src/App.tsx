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
import { FlyToInterpolator, WebMercatorViewport } from '@deck.gl/core'
import Assistant, { dispatchLayers, type Dispatch } from './Assistant'
import LiveOps from './ops/LiveOps'
import 'maplibre-gl/dist/maplibre-gl.css'
import { ChevronLeft, ChevronRight, Pause, Play, Search, X } from 'lucide-react'
import { SimResultsView } from './advisor/SimResults'
import { BlackSpotPanel, StageTable } from './blackspots/BlackSpotPanel'
import { ALL_FILTER, STATUS_LABEL, blackSpotLayers, classify, effectiveMode, fixDeployments, spotTooltipHtml, type CompareMode, type SpotColor, type SpotFilter } from './blackspots/layers'
import { CAUSE_RGB, CHANGE_RGB, SEVERITY_LABEL, SEVERITY_RGB, causeLabel, css } from './blackspots/palette'
import { CAUSE_KEYS, SEVERITIES, type BlackSpotData, type Cluster, type FixKey, type ScenarioKey } from './blackspots/types'
import './views.css'

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

export type View = 'live' | 'plan' | 'evidence'

const PLAN_TEXT: Record<'baseline' | 'static' | 'goldenhour', [string, string]> = {
  baseline: ["Today's practice", 'Ambulances wait at hospitals, shared out by population, the same all day. No bikes. This is how the 2020 CAG audit found them.'],
  static: ['Best fixed spots', 'The best single set of standby spots for the whole day, chosen by the optimiser. No bikes, no moves.'],
  goldenhour: ['GoldenHour', 'Spots change every 4 hours with demand and traffic, bikes give first help, and at most a quarter of ambulances move between blocks.'],
}
type LayerMode = 'cover' | 'nearest' | 'demand' | 'reach' | 'spots'
const LAYER_TEXT: Record<LayerMode, string> = {
  cover: 'In time',
  nearest: 'Minutes away',
  demand: 'Calls',
  reach: 'Reachable',
  spots: 'Black spots',
}
// Black-spot results (pipeline/blackspots.py) for each dashboard plan
const PLAN_TO_SPOTS: Record<'baseline' | 'static' | 'goldenhour', ScenarioKey> = { baseline: 'today', static: 'static', goldenhour: 'current' }
const SPOT_ZOOM: Record<'blr' | 'tmk', number> = { blr: 12.6, tmk: 10.4 }
const STRESS_LABEL: Record<string, string> = {
  'Standard Operations': 'A normal day',
  'Demand Surge (+20%)': '20% more calls',
  'Severe Traffic Congestion (+20%)': 'Traffic 20% slower',
  'Fleet Breakdown (60% Available)': 'Only 60% of the fleet on the road',
}
const BLOCK_HOURS = ['12–4 am', '4–8 am', '8 am–12 pm', '12–4 pm', '4–8 pm', '8 pm–12 am']

export default function App({ view, region, onRegion }: { view: View; region: 'blr' | 'tmk'; onRegion: (r: 'blr' | 'tmk') => void }) {
  const setRegion = onRegion
  const [planType, setPlanType] = useState<'baseline' | 'static' | 'goldenhour'>('goldenhour')
  const [blockId, setBlockId] = useState<number>(4) // Default to Evening Peak (16:00-20:00)
  const [layerMode, setLayerMode] = useState<LayerMode>('cover')

  // Operator usability state
  const [isPlaying, setIsPlaying] = useState<boolean>(false)
  const [searchQuery, setSearchQuery] = useState<string>('')
  const [showSearchResults, setShowSearchResults] = useState<boolean>(false)
  const [selectedStation, setSelectedStation] = useState<any | null>(null)
  const [selectedMove, setSelectedMove] = useState<any | null>(null)
  const [viewState, setViewState] = useState<any>(INITIAL_VIEW_STATES.blr)
  const [activeDispatch, setActiveDispatch] = useState<Dispatch | null>(null)

  // Black spots: where the ambulance could not reach in time, and the re-plans that fix them
  const [spotLoaded, setSpotLoaded] = useState<{ region: string; data: BlackSpotData | null } | null>(null)
  const spotData = spotLoaded?.region === region ? spotLoaded.data : null
  const [spotFix, setSpotFix] = useState<FixKey | null>(null)
  const [spotColor, setSpotColor] = useState<SpotColor>('cause')
  const [spotMode, setSpotMode] = useState<CompareMode>('before')
  const [spotFilter, setSpotFilter] = useState<SpotFilter>(ALL_FILTER)
  const [spotCluster, setSpotCluster] = useState<number | null>(null)
  const [spotHex, setSpotHex] = useState<number | null>(null)
  const [liveSpots, setLiveSpots] = useState(false)

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
    // keep the camera on an assistant dispatch that caused this region switch
    if (!(activeDispatch && activeDispatch.region === region)) setViewState(INITIAL_VIEW_STATES[region])
    setSelectedStation(null)
    setSelectedMove(null)
    setSearchQuery('')

    Promise.all([
      fetch(`${import.meta.env.BASE_URL}data/cells_${region}.json`).then((r) => r.json()),
      fetch(`${import.meta.env.BASE_URL}data/sites_${region}.json`).then((r) => r.json()),
      fetch(`${import.meta.env.BASE_URL}data/scenarios_${region}.json`).then((r) => r.json()),
      fetch(`${import.meta.env.BASE_URL}data/zones.geojson`).then((r) => r.json()).catch(() => null),
      fetch(`${import.meta.env.BASE_URL}data/forecast.json`).then((r) => r.json()).catch(() => null),
      fetch(`${import.meta.env.BASE_URL}data/meta.json`).then((r) => r.json()).catch(() => null),
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

  // Black-spot data is optional: the map works without it (run `make blackspots` to create it)
  useEffect(() => {
    let live = true
    const done = (data: BlackSpotData | null) => {
      if (!live) return
      setSpotLoaded({ region, data })
      setSpotFix(null)
      setSpotCluster(null)
      setSpotHex(null)
      setSpotMode('before')
    }
    fetch(`${import.meta.env.BASE_URL}data/blackspots_${region}.json`)
      .then((r) => (r.ok ? r.json() : null))
      .then(done, () => done(null))
    return () => {
      live = false
    }
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

  // Black spots: the selected plan's, or a fix's (fixes start from the GoldenHour plan)
  const spotsMode = view === 'plan' && layerMode === 'spots'
  const fixActive = spotsMode && spotFix !== null
  const spotScenario = spotData?.scenarios.find((x) => x.key === (fixActive ? spotFix : PLAN_TO_SPOTS[planType])) ?? null
  const spotCurrent = spotData?.scenarios.find((x) => x.key === 'current') ?? null

  const spotCls = useMemo(
    () => (spotData && spotScenario ? classify(spotData, spotScenario, spotsMode ? spotMode : 'before') : null),
    [spotData, spotScenario, spotsMode, spotMode],
  )
  const spotLabels = STATUS_LABEL[spotScenario ? effectiveMode(spotScenario, spotsMode ? spotMode : 'before') : 'before']

  const chooseFix = (f: FixKey | null) => {
    setSpotFix(f)
    setSpotCluster(null)
    setSpotHex(null)
    if (f) {
      setPlanType('goldenhour')
      setSpotMode('stage') // a stage is shown before/after against the stage before it
    } else setSpotMode('before')
  }
  const focusCluster = (c: Cluster | null) => {
    setSpotCluster(c?.id ?? null)
    setSpotHex(null)
    if (!c) return
    setViewState((v: any) => ({
      ...v,
      longitude: c.lon,
      latitude: c.lat,
      zoom: Math.max(v.zoom, SPOT_ZOOM[region]),
      transitionDuration: 900,
      transitionInterpolator: new FlyToInterpolator(),
    }))
  }

  // Units on the map: the plan's, or the black-spot fix's for this block
  const shownDeployments = useMemo(
    () => (fixActive && spotData && spotScenario ? fixDeployments(spotData, spotScenario, blockId, sites) : currBlockPlan?.deployments),
    [fixActive, spotData, spotScenario, blockId, sites, currBlockPlan],
  )

  // Moves for current block in GoldenHour (a fix has its own units, so no GoldenHour moves then)
  const blockMoves = useMemo(() => {
    if (fixActive || planType !== 'goldenhour' || !currScenario?.moves) return []
    const m = currScenario.moves.find((mv: any) => mv.from_block === blockId)
    return m ? m.moves : []
  }, [fixActive, planType, currScenario, blockId])

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
    if (!shownDeployments) {
      return { vehicleMarkers: [], vehicleBadges: [], moveEndpoints: [] }
    }

    const markers: any[] = []
    const badges: any[] = []

    shownDeployments.forEach((d: any) => {
      const totalTypes = (d.a > 0 ? 1 : 0) + (d.b > 0 ? 1 : 0) + (d.y > 0 ? 1 : 0)

      // ALS Ambulance
      if (d.a > 0) {
        const offX = totalTypes > 1 ? -0.0008 : 0
        const offY = totalTypes > 1 ? 0.0006 : 0
        markers.push({
          ...d,
          vType: 'ALS',
          count: d.a,
          iconUrl: `${import.meta.env.BASE_URL}icons/icon-als.svg`,
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
          iconUrl: `${import.meta.env.BASE_URL}icons/icon-bls.svg`,
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
          iconUrl: `${import.meta.env.BASE_URL}icons/icon-bike.svg`,
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
  }, [shownDeployments, blockMoves])

  // Deck.gl Layers Definition
  const layers = useMemo(() => {
    if (!cells.length || !currBlockPlan) return []

    // 1. High-Contrast Hexagon Layer
    const hexLayer = showHexagons && !spotsMode
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
    spotsMode,
  ])

  const spotLayers = useMemo(() => {
    if (!spotData || !spotScenario || !spotCurrent || !cells.length) return []
    if (!spotCls) return []
    const base = { data: spotData, scenario: spotScenario, cls: spotCls, cells, color: spotColor }
    if (spotsMode) return blackSpotLayers({ ...base, mode: 'full', filter: spotFilter, selectedCluster: spotCluster, selectedHex: spotHex })
    if (view === 'live' && liveSpots)
      return blackSpotLayers({ ...base, color: 'cause', mode: 'overlay', filter: ALL_FILTER, selectedCluster: null, selectedHex: null })
    return []
  }, [spotData, spotScenario, spotCurrent, spotCls, cells, spotColor, spotsMode, spotFilter, spotCluster, spotHex, view, liveSpots])

  // Response-time distribution: today (grey) vs the selected plan (green within target, amber after)
  const renderHistogram = () => {
    if (!currSim?.histogram || !baseSim?.histogram) return null
    const bins = 50
    const maxVal = Math.max(...currSim.histogram.slice(0, bins), ...baseSim.histogram.slice(0, bins), 1)
    const t = Math.round(targetMin)
    return (
      <figure className="hist" aria-label="Response times over 4 simulated weeks">
        <svg viewBox="0 0 300 80" preserveAspectRatio="none" role="img">
          {baseSim.histogram.slice(0, bins).map((v: number, i: number) => (
            <rect key={`b${i}`} x={i * 6} y={70 - (v / maxVal) * 66} width={5} height={(v / maxVal) * 66} fill="var(--line-strong)" />
          ))}
          {currSim.histogram.slice(0, bins).map((v: number, i: number) => (
            <rect key={`p${i}`} x={i * 6 + 1.5} y={70 - (v / maxVal) * 66} width={2.5} height={(v / maxVal) * 66}
              fill={i < t ? 'var(--ok)' : 'var(--risk)'} />
          ))}
          <line x1={t * 6} x2={t * 6} y1={0} y2={70} stroke="var(--critical)" strokeDasharray="3 3" />
        </svg>
        <figcaption>
          <span>0</span><span style={{ color: 'var(--critical)' }}>{t} min target</span><span>50+ min</span>
        </figcaption>
        <p className="key"><i style={{ background: 'var(--line-strong)' }} />Today <i style={{ background: 'var(--ok)' }} />{PLAN_TEXT[planType][0]}</p>
      </figure>
    )
  }

  // Daily calls: actual vs LightGBM vs the 4-week average
  const renderForecastChart = () => {
    const tl = forecastData?.[region]?.timeline || []
    if (!tl.length) return <p className="muted">No forecast data yet.</p>
    const maxVal = Math.max(...tl.map((d: any) => Math.max(d.actual, d.forecast, d.baseline)), 1)
    const pts = (k: string) => tl.map((d: any, i: number) => `${(i / (tl.length - 1)) * 600},${150 - (d[k] / maxVal) * 135}`).join(' ')
    return (
      <figure className="chart">
        <svg viewBox="0 0 600 160" preserveAspectRatio="none" role="img" aria-label="Daily calls: actual, forecast and 4-week average">
          <polyline points={pts('baseline')} fill="none" stroke="var(--line-strong)" strokeWidth="1.5" strokeDasharray="4 3" />
          <polyline points={pts('actual')} fill="none" stroke="var(--ink)" strokeWidth="1.5" />
          <polyline points={pts('forecast')} fill="none" stroke="var(--action)" strokeWidth="2.5" />
        </svg>
        <p className="key"><i style={{ background: 'var(--ink)' }} />Actual calls <i style={{ background: 'var(--action)' }} />LightGBM forecast <i style={{ background: 'var(--line-strong)' }} />4-week average</p>
      </figure>
    )
  }

  const showDispatch = (d: Dispatch | null) => {
    setActiveDispatch(d)
    if (!d) return
    if (d.region !== region) setRegion(d.region as 'blr' | 'tmk')
    // fit the whole response (units, patient, hospital) into the part of the map not covered by panels
    const pts = [...d.ambulance.path, ...(d.bike?.path ?? []), ...d.hospital.path]
    const lons = pts.map((p) => p[0]), lats = pts.map((p) => p[1])
    const width = Math.max(400, window.innerWidth - 400), height = window.innerHeight - 56
    const wide = width > 900
    const fit = new WebMercatorViewport({ width, height }).fitBounds(
      [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
      { padding: { top: 90, bottom: 90, left: 60, right: wide ? 470 : 60 }, maxZoom: 15 },
    )
    setViewState((v: any) => ({ ...v, longitude: fit.longitude, latitude: fit.latitude, zoom: fit.zoom, pitch: 0, bearing: 0,
      transitionDuration: 1500, transitionInterpolator: new FlyToInterpolator() }))
  }

  // The assistant orb and panel sit left of the docked panel
  useEffect(() => {
    document.documentElement.style.setProperty('--dock-w', view === 'evidence' ? '0px' : '400px')
  }, [view])

  const tip = (html: string) => ({
    html: `<div style="font: 13px/1.45 var(--font-sans); padding: 8px 10px; max-width: 260px;">${html}</div>`,
    style: { background: '#FFFFFF', color: '#17202B', borderRadius: '8px', border: '1px solid #DCE1E7', boxShadow: '0 4px 14px rgba(23,32,43,.12)' },
  })
  const getTooltip = ({ object }: any) => {
    if (!object) return null
    const spotHtml = spotTooltipHtml(object, targetMin, spotLabels)
    if (spotHtml) return tip(spotHtml)
    if (object.h3) {
      const lam = object.lam?.[blockId] || 0
      return tip(`<b>${object.zone}</b><br/>${object.pop.toLocaleString()} people<br/>${lam.toFixed(2)} calls per hour now`)
    }
    if (object.site_id) {
      return tip(`<b>${object.name}</b><br/>Advanced ${object.a}, Basic ${object.b}, bikes ${object.y}`)
    }
    if (object.from_name) {
      return tip(`<b>Planned move</b><br/>${object.from_name} → ${object.to_name}<br/>${object.travel_min} min drive`)
    }
    return null
  }

  // Live calls: quieter map (coverage faint, no plan moves) so incoming calls and routes stand out
  const shownLayers = (() => {
    const quiet = view === 'live' || activeDispatch
    // Black spots sit just above the coverage hexagons, under borders, moves and vehicles
    const withSpots = (ls: any[]) => {
      const i = ls.findIndex((l: any) => l.id === 'h3-hexagons')
      return [...ls.slice(0, i + 1), ...spotLayers, ...ls.slice(i + 1)]
    }
    if (!quiet) return withSpots(layers)
    return [
      ...withSpots(
        layers
          .filter((l: any) => !['admin-zones', 'crew-moves-arcs', 'move-arrival-rings'].includes(l.id))
          .map((l: any) => (l.id === 'h3-hexagons' ? l.clone({ opacity: activeDispatch || spotLayers.length ? 0.12 : 0.22 }) : l)),
      ),
      ...dispatchLayers(activeDispatch),
    ]
  })()

  const legend = () => {
    if (activeDispatch)
      return (
        <div className="legend">
          <span><i className="ln" style={{ background: 'var(--critical)' }} />Ambulance</span>
          {activeDispatch.bike && <span><i className="ln" style={{ background: 'var(--bike)' }} />Bike</span>}
          <span><i className="ln" style={{ background: 'var(--ok)' }} />To hospital</span>
          <button className="btn small" onClick={() => setActiveDispatch(null)}>Clear route</button>
        </div>
      )
    const scale: Record<string, [string, string][]> = {
      cover: [['var(--ok)', 'Likely in time'], ['var(--risk)', 'At risk'], ['var(--critical)', 'Likely late']],
      nearest: [['var(--ok)', `Under ${Math.round(targetMin * 0.55)} min`], ['var(--risk)', `Up to ${targetMin} min`], ['var(--critical)', 'Over target']],
      demand: [['var(--critical)', 'Busiest'], ['var(--risk)', 'Busy'], ['var(--action)', 'Moderate']],
      reach: [['var(--ok)', 'Target reachable'], ['var(--critical)', 'Not reachable']],
    }
    const spotKey = (spotsMode || (view === 'live' && liveSpots)) && spotScenario ? (
      <>
        {spotColor === 'severity' && spotsMode
          ? SEVERITIES.slice().reverse().map((v) => <span key={v}><i style={{ background: css(SEVERITY_RGB[v]) }} />{SEVERITY_LABEL[v]}</span>)
          : CAUSE_KEYS.map((c) => <span key={c}><i style={{ background: css(CAUSE_RGB[c]) }} />{causeLabel(c)}</span>)}
        {spotsMode && <span><i className="bs-ring" style={{ borderColor: css(CHANGE_RGB.chronic) }} />{spotLabels.chronic}</span>}
        {spotsMode && <span><i className="bs-ring" style={{ borderColor: css(CHANGE_RGB.new) }} />New</span>}
        {spotsMode && <span><i className="bs-ring" style={{ borderColor: css(CHANGE_RGB.resolved) }} />Resolved</span>}
        {spotsMode && <span><i style={{ background: 'rgba(91,102,117,.45)' }} />Not a black spot</span>}
        {fixActive && spotScenario?.posts_used && <span><i className="bs-dot" style={{ background: css(CHANGE_RGB.post) }} />Proposed post</span>}
      </>
    ) : null
    return (
      <div className="legend">
        {view === 'plan' && layerMode !== 'spots' && scale[layerMode].map(([c, t]) => <span key={t}><i style={{ background: c }} />{t}</span>)}
        {spotKey}
        <span><img src={`${import.meta.env.BASE_URL}icons/icon-als.svg`} alt="" />Advanced</span>
        <span><img src={`${import.meta.env.BASE_URL}icons/icon-bls.svg`} alt="" />Basic</span>
        {region === 'blr' && <span><img src={`${import.meta.env.BASE_URL}icons/icon-bike.svg`} alt="" />Bike</span>}
      </div>
    )
  }

  const delta = (v: number, unit: 'pts' | 'min', lowerBetter = false) => {
    if (!baseSim || planType === 'baseline') return null
    const good = lowerBetter ? v < 0 : v > 0
    const s = `${v > 0 ? '+' : ''}${v.toFixed(1)} ${unit}`
    return <span className={Math.abs(v) < 0.05 ? 'flat' : good ? 'up' : 'down'}>{s} vs today</span>
  }

  const planPanel = (
    <aside className="dock" aria-label="Fleet plan">
      {(selectedStation || selectedMove) && (
        <section className="sec sel">
          <button className="x" aria-label="Close" onClick={() => { setSelectedStation(null); setSelectedMove(null) }}><X size={16} /></button>
          {selectedStation && (
            <>
              <h3>{selectedStation.name}</h3>
              <p className="muted">{selectedStation.site_type || 'hospital'}</p>
              <p>Advanced {selectedStation.a || 0}, Basic {selectedStation.b || 0}, bikes {selectedStation.y || 0} wait here in this block.</p>
            </>
          )}
          {selectedMove && (
            <>
              <h3>Planned move</h3>
              <p>{selectedMove.from_name} → {selectedMove.to_name}</p>
              <p className="muted">{selectedMove.travel_min} min drive</p>
            </>
          )}
        </section>
      )}
      <section className="sec">
        <h2>Plan</h2>
        <div className="choices" role="radiogroup" aria-label="Plan">
          {(['baseline', 'static', 'goldenhour'] as const).map((p) => (
            <button key={p} role="radio" aria-checked={planType === p} className={planType === p && !fixActive ? 'on' : ''}
              onClick={() => { setPlanType(p); setSpotFix(null); setSpotCluster(null); setSpotHex(null); setSpotMode('before') }}>
              <b>{PLAN_TEXT[p][0]}</b>
              <span>{PLAN_TEXT[p][1]}</span>
            </button>
          ))}
        </div>
        <p className="muted small">
          {currScenario?.fleet?.ambulances ?? 0} ambulances{(currScenario?.fleet?.bikes ?? 0) > 0 ? ` and ${currScenario.fleet.bikes} bikes` : ''} on the road
          (about 74% of the fleet). Target: {targetMin} minutes from call to arrival.
        </p>
        {view === 'plan' && layerMode !== 'spots' && spotData && (
          <button className="btn small bs-open" onClick={() => setLayerMode('spots')}>
            Show black spots: where the ambulance could not reach in time
          </button>
        )}
      </section>

      {spotsMode && spotData && spotScenario && spotCurrent && spotCls && (
        <BlackSpotPanel
          data={spotData}
          scenario={spotScenario}
          current={spotCurrent}
          cls={spotCls}
          cells={cells}
          fix={spotFix}
          onFix={chooseFix}
          color={spotColor}
          onColor={setSpotColor}
          mode={spotMode}
          onMode={setSpotMode}
          filter={spotFilter}
          onFilter={setSpotFilter}
          selectedCluster={spotCluster}
          onSelectCluster={focusCluster}
          selectedHex={spotHex}
          onSelectHex={setSpotHex}
        />
      )}
      {spotsMode && !spotData && (
        <section className="sec">
          <h2>Black spots</h2>
          <p className="small">
            No black-spot results for this region yet. From the repo root run <code>make package</code> once, then{' '}
            <code>make blackspots</code> (about 15 minutes; <code>make blackspots-quick</code> takes about 3).
          </p>
        </section>
      )}

      <section className="sec">
        <div className="row between">
          <h2>{BLOCK_HOURS[blockId]}</h2>
          <div className="row gap4">
            <button className="btn icon" aria-label="Previous block" onClick={() => setBlockId((p) => (p === 0 ? 5 : p - 1))}><ChevronLeft size={16} /></button>
            <button className="btn" onClick={() => setIsPlaying(!isPlaying)}>
              {isPlaying ? <Pause size={14} /> : <Play size={14} />} {isPlaying ? 'Pause' : 'Play the day'}
            </button>
            <button className="btn icon" aria-label="Next block" onClick={() => setBlockId((p) => (p + 1) % 6)}><ChevronRight size={16} /></button>
          </div>
        </div>
        <input type="range" min={0} max={5} step={1} value={blockId} onChange={(e) => setBlockId(Number(e.target.value))} aria-label="Time of day" />
        <div className="ticks">{['12 am', '4 am', '8 am', '12 pm', '4 pm', '8 pm'].map((t) => <span key={t}>{t}</span>)}</div>
        <p className="small">
          {fixActive && <>Units shown are the {spotScenario?.label.toLowerCase()} plan for this block. </>}
          {!fixActive && currSim?.per_block?.[blockId] && baseSim?.per_block?.[blockId] && (
            <>{currSim.per_block[blockId].within_target_pct}% of calls in this block are reached in time (today: {baseSim.per_block[blockId].within_target_pct}%). </>
          )}
          About {Math.round((currBlockPlan?.q ?? 0) * 100)}% of ambulances are busy at any moment.
          {planType === 'goldenhour' && !fixActive && blockMoves.length > 0 && <> {blockMoves.length} ambulances move to new spots at the start of this block.</>}
        </p>
      </section>

      {!spotsMode && <section className="sec">
        <h2>Over 4 simulated weeks</h2>
        <dl className="stats">
          <div><dt>Reached within {targetMin} min</dt><dd>{currSim ? `${currSim.within_target_pct}%` : '–'}</dd>{delta(deltaTarget, 'pts')}</div>
          <div><dt>Typical wait</dt><dd>{currSim ? `${currSim.median_min} min` : '–'}</dd>{delta(deltaMedian, 'min', true)}</div>
          <div><dt>Slowest 1 in 10 wait over</dt><dd>{currSim ? `${currSim.p90_min} min` : '–'}</dd>{delta(deltaP90, 'min', true)}</div>
          <div><dt>First help within 10 min</dt><dd>{currSim ? `${currSim.first_help_10m_pct}%` : '–'}</dd>{delta(deltaBike, 'pts')}</div>
        </dl>
        {renderHistogram()}
      </section>}
    </aside>
  )

  const evidence = () => {
    const fc = (scenarios?.fleet_curve ?? []) as any[]
    const today = fc.find((x) => x.multiplier === 1) ?? fc[0]
    const best = fc[fc.length - 1]
    const hit = fc.find((x) => x.within_target_pct >= 90)
    const zone = region === 'blr' ? 'ward' : 'taluk'
    const fcd = forecastData?.[region]
    const sections: [string, string][] = [
      ['fleet', 'How many ambulances are needed'],
      ['stress', 'When things get harder'],
      ['wards', `Slowest ${zone}s`],
      ['forecast', 'Demand forecast'],
      ['hospital', 'Right hospital, not nearest'],
      ['spots', 'Black spots'],
      ['sources', 'Sources'],
    ]
    return (
      <div className="evidence">
        <nav className="toc" aria-label="On this page">
          {sections.map(([id, t]) => <a key={id} href={`#${id}`} onClick={(e) => { e.preventDefault(); document.getElementById(`ev-${id}`)?.scrollIntoView({ behavior: 'smooth' }) }}>{t}</a>)}
        </nav>
        <article className="doc">
          <p className="lede">
            {region === 'blr' ? 'Bengaluru' : 'Tumakuru district'}. Every number below comes from replaying 4 weeks of modelled 108 calls on real roads,
            population and hospitals. Calls are generated, because real call logs are not public.
          </p>

          <section id="ev-fleet">
            <h2>How many ambulances are needed</h2>
            {today && best && (
              <p>
                With today's {today.ambulances} ambulances ({today.on_road} on the road), {today.within_target_pct}% of calls are reached within {targetMin} minutes.
                With {best.ambulances}, it is {best.within_target_pct}%.{' '}
                {hit ? `About ${hit.ambulances} ambulances are needed to reach 90% of calls in time.` : `Even ${best.ambulances} ambulances do not reach 90% in time, so this target needs more than extra vehicles.`}
              </p>
            )}
            <div className="bars">
              {fc.map((x: any) => (
                <div key={x.multiplier} className={`bar-row ${x.multiplier === 1 ? 'now' : ''}`}>
                  <span className="lbl">{x.ambulances} ambulances{x.multiplier === 1 ? ' (today)' : ''}</span>
                  <span className="track"><span style={{ width: `${x.within_target_pct}%` }} /></span>
                  <span className="val">{x.within_target_pct}%</span>
                </div>
              ))}
            </div>
          </section>

          <section id="ev-stress">
            <h2>When things get harder</h2>
            <p>The same plans under harder conditions. When fewer vehicles are on the road, both plans are re-made with what is left.</p>
            <table>
              <thead><tr><th>Condition</th><th>Today, in time</th><th>GoldenHour, in time</th><th>Slowest 1 in 10, today</th><th>GoldenHour</th></tr></thead>
              <tbody>
                {(scenarios?.stress_tests ?? []).map((st: any) => (
                  <tr key={st.name}>
                    <td><b>{STRESS_LABEL[st.name] ?? st.name}</b><br /><span className="muted small">{st.description}</span></td>
                    <td>{st.baseline_within_pct}%</td>
                    <td className={st.goldenhour_within_pct >= st.baseline_within_pct ? 'up' : 'down'}>{st.goldenhour_within_pct}%</td>
                    <td>{st.baseline_p90} min</td>
                    <td className={st.goldenhour_p90 <= st.baseline_p90 ? 'up' : 'down'}>{st.goldenhour_p90} min</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section id="ev-wards">
            <h2>Slowest {zone}s</h2>
            <p>The {zone}s where help arrives late most often with the {PLAN_TEXT[planType][0]} plan. These are where a fixed target hurts most and where extra vehicles help most.</p>
            <table>
              <thead><tr><th>{zone[0].toUpperCase() + zone.slice(1)}</th><th>Calls in 4 weeks</th><th>In time, this plan</th><th>In time, today</th><th>Typical wait</th><th>Slowest 1 in 10</th></tr></thead>
              <tbody>
                {Object.entries(currSim?.per_zone ?? {})
                  .sort((a: any, b: any) => a[1].within_target_pct - b[1].within_target_pct)
                  .slice(0, 15)
                  .map(([z, st]: any) => (
                    <tr key={z}>
                      <td><b>{z}</b></td>
                      <td>{st.calls}</td>
                      <td className={st.within_target_pct >= (baseSim?.per_zone?.[z]?.within_target_pct ?? 0) ? 'up' : 'down'}>{st.within_target_pct}%</td>
                      <td>{baseSim?.per_zone?.[z]?.within_target_pct ?? '–'}%</td>
                      <td>{st.median_min} min</td>
                      <td>{st.p90_min} min</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </section>

          <section id="ev-forecast">
            <h2>Demand forecast</h2>
            <p>
              A LightGBM model predicts calls for each {zone} and 4-hour block. On the 8 weeks it never saw, its average error is {fcd?.mae_lgb ?? '–'} calls,
              against {fcd?.mae_baseline ?? '–'} for the "same block, last 4 weeks" rule a control room might use ({fcd?.mae_improvement_pct ?? '–'}% better).
              Calls are modelled, so this shows the method works, not real-world accuracy.
            </p>
            {renderForecastChart()}
          </section>

          <section id="ev-hospital">
            <h2>Right hospital, not nearest</h2>
            <p>Crews pick the hospital in the crew app. This replay compares taking each patient to the nearest hospital with taking them to one that can treat them. It uses fictional demo hospitals.</p>
            <div className="adv-root" data-theme="light"><SimResultsView /></div>
          </section>

          <section id="ev-spots">
            <h2>Black spots: where help arrives late</h2>
            {spotData ? (
              <>
                <p>
                  Hexagons where the ambulance repeatedly took over {targetMin} minutes ({spotData.windows.test.start} to {spotData.windows.test.end},
                  12 simulated weeks). Each stage is re-planned with the optimiser and replayed, then compared with the stage before it.
                  Fixes were chosen on the 12 weeks before, so these are weeks they never saw. Open Fleet plan, Black spots to see them on the map.
                </p>
                <StageTable data={spotData} />
              </>
            ) : (
              <p className="muted">No black-spot results yet: run <code>make blackspots</code>.</p>
            )}
          </section>

          <section id="ev-sources">
            <h2>Sources</h2>
            <table>
              <thead><tr><th>What</th><th>Source</th><th>Details</th></tr></thead>
              <tbody>
                {(meta?.assumptions ?? []).map((a: any, i: number) => (
                  <tr key={i}><td><b>{a.label}</b><br /><span className="muted small">{a.symbol === '≈' ? 'Our estimate' : 'Published source'}</span></td><td>{a.source}</td><td>{a.details}</td></tr>
                ))}
              </tbody>
            </table>
          </section>
        </article>
      </div>
    )
  }

  if (view === 'evidence') return <div className="app">{evidence()}</div>

  return (
    <div className="app">
      <div className="map">
        <div className="map-tools">
          <div className="search">
            <Search size={15} aria-hidden="true" />
            <input
              type="search"
              placeholder={region === 'blr' ? 'Find a ward or hospital' : 'Find a taluk or hospital'}
              value={searchQuery}
              onChange={(e) => { setSearchQuery(e.target.value); setShowSearchResults(true) }}
              onFocus={() => setShowSearchResults(true)}
              aria-label="Find a place"
            />
            {showSearchResults && searchResults.length > 0 && (
              <ul className="results">
                {searchResults.map((r: any, i: number) => (
                  <li key={i}><button onClick={() => handleSelectSearch(r)}><b>{r.name}</b><span>{r.type === 'Hospital' ? 'Standby site' : r.type}</span></button></li>
                ))}
              </ul>
            )}
          </div>
          {view === 'live' && spotData && (
            <button className="btn bs-toggle" aria-pressed={liveSpots} onClick={() => setLiveSpots((v) => !v)}
              title="Hexagons where the GoldenHour plan repeatedly could not reach a caller in time (simulated)">
              Black spots{liveSpots && spotScenario ? ` · ${spotScenario.kpis.black_spots}` : ''}
            </button>
          )}
          {view === 'plan' && (
            <div className="seg" role="group" aria-label="Map colours">
              {(Object.keys(LAYER_TEXT) as (keyof typeof LAYER_TEXT)[]).map((k) => (
                <button key={k} aria-pressed={layerMode === k} onClick={() => setLayerMode(k)}>{LAYER_TEXT[k]}</button>
              ))}
            </div>
          )}
          {view === 'plan' && (
            <details className="show">
              <summary>Show</summary>
              <label><input type="checkbox" checked={showHexagons} onChange={(e) => setShowHexagons(e.target.checked)} /> Coverage</label>
              <label><input type="checkbox" checked={showVehicles} onChange={(e) => setShowVehicles(e.target.checked)} /> Vehicles</label>
              <label><input type="checkbox" checked={showArcs} onChange={(e) => setShowArcs(e.target.checked)} /> Planned moves</label>
              <label><input type="checkbox" checked={showWards} onChange={(e) => setShowWards(e.target.checked)} /> {region === 'blr' ? 'Ward' : 'Taluk'} borders</label>
            </details>
          )}
        </div>
        <DeckGL
          viewState={viewState}
          onViewStateChange={({ viewState: vs }: any) => setViewState(vs)}
          controller={true}
          layers={shownLayers}
          getTooltip={getTooltip}
          onClick={({ object }: any) => {
            if (!spotsMode || !spotScenario) return
            if (object?.spot) {
              // any hexagon with black-spot data: show its card (and its cluster outline stays off)
              setSpotCluster(null)
              setSpotHex(object.idx)
            } else if (!object) {
              setSpotCluster(null)
              setSpotHex(null)
            }
          }}
        >
          <Map mapStyle={MAP_STYLE} />
        </DeckGL>
        {legend()}
      </div>
      {view === 'live' ? (
        <aside className="dock"><LiveOps region={region} onShow={showDispatch} /></aside>
      ) : (
        planPanel
      )}
      <Assistant onDispatch={showDispatch} />
    </div>
  )
}
