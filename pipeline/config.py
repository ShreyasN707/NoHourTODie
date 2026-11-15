"""GoldenHour: Karnataka 108 Ambulance Fleet Planner Configuration.

Every assumption is documented with its source.
Symbols:
  ◐ = Government / news / published report source (CAG, Cabinet, TomTom, OpenStreetMap)
  ≈ = Modelled or calibrated estimate derived from published aggregates
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

# Project Paths
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
WEB_DIR = ROOT_DIR / "web"
WEB_DATA_DIR = WEB_DIR / "public" / "data"

# Study Bounding Box: (min_lon, min_lat, max_lon, max_lat)
# Encompasses Greater Bengaluru Area and Tumakuru District
# ◐ Source: Bounding box enclosing BBMP/GBA (12.8-13.2N, 77.4-77.8E) and Tumakuru (12.7-14.4N, 76.3-77.6E)
STUDY_BBOX: Tuple[float, float, float, float] = (76.2, 12.7, 77.95, 14.45)

# Free-flow speeds (km/h) by OpenStreetMap road class
# ≈ Our estimate per OSM road class (under 1% of Indian OSM roads carry a maxspeed tag)
ROAD_SPEEDS_KMH: Dict[str, float] = {
    "motorway": 80.0,
    "trunk": 60.0,
    "primary": 45.0,
    "secondary": 38.0,
    "tertiary": 32.0,
    "unclassified": 28.0,
    "residential": 22.0,
    "living_street": 12.0,
    "service": 15.0,
}

# Bengaluru hourly congestion multipliers (hours 0–23)
# ◐ Source: TomTom Traffic Index 2024/2025 Bengaluru profile (36 min per 10 km, ~14 km/h peak evening rush)
# Morning peak ~2.5x (8-9 AM), evening peak ~2.6x (6-7 PM)
BENGALURU_HOURLY_CONGESTION: List[float] = [
    1.15, 1.10, 1.10, 1.10, 1.15, 1.30, 1.60, 2.00, 2.40, 2.50, 2.20, 2.00,
    1.90, 1.90, 2.00, 2.10, 2.30, 2.50, 2.60, 2.40, 2.10, 1.80, 1.50, 1.30
]

# Hourly call distribution share in percentage (hours 0–23, sums to 100%)
# ≈ Our estimate shaped to the reported 7-9 pm 108 peak (◐ 100-120 calls/hour statewide, news)
HOURLY_CALL_SHARE: List[float] = [
    2.2, 1.8, 1.5, 1.4, 1.5, 2.2, 3.4, 4.6, 5.2, 5.3, 5.2, 5.0,
    4.9, 4.8, 4.9, 5.1, 5.4, 5.8, 6.3, 6.5, 6.0, 4.8, 3.6, 2.6
]

# Planning Blocks: six 4-hour blocks; block 4 (16:00-20:00) is the evening peak
PLANNING_BLOCKS = [
    {"id": 0, "name": "Night (00:00 - 04:00)", "hours": [0, 1, 2, 3]},
    {"id": 1, "name": "Early Morning (04:00 - 08:00)", "hours": [4, 5, 6, 7]},
    {"id": 2, "name": "Morning Peak (08:00 - 12:00)", "hours": [8, 9, 10, 11]},
    {"id": 3, "name": "Afternoon (12:00 - 16:00)", "hours": [12, 13, 14, 15]},
    {"id": 4, "name": "Evening Peak (16:00 - 20:00)", "hours": [16, 17, 18, 19]},
    {"id": 5, "name": "Late Evening (20:00 - 24:00)", "hours": [20, 21, 22, 23]},
]

# Call mix definitions and clinical triage rules
# ≈ Our estimate (EMRI-style national mix); no Karnataka call-type split is published
CALL_MIX = {
    "pregnancy":   {"share": 0.30, "needs_als": False, "bike_helps": False},
    "trauma":      {"share": 0.20, "needs_als": False, "bike_helps": True},
    "cardiac":     {"share": 0.08, "needs_als": True,  "bike_helps": True},
    "stroke":      {"share": 0.04, "needs_als": True,  "bike_helps": False},
    "respiratory": {"share": 0.10, "needs_als": True,  "bike_helps": False},
    "other":       {"share": 0.28, "needs_als": False, "bike_helps": True},
}

# Critical calls requiring Advanced Life Support (ALS): cardiac (0.08) + stroke (0.04) + respiratory (0.10) = 0.22
CRITICAL_CALL_SHARE = 0.22
# Calls where first-responder Bike paramedic provides life-saving stabilization: trauma (0.20) + cardiac (0.08) + other (0.28) = 0.56
BIKE_ELIGIBLE_CALL_SHARE = 0.56

# Operational timing assumptions
# ◐ Source: CAG Report 3 of 2020: Triage exceeded 3 min in 47% of calls; wheels-rolling exceeded 1 min in 85% of calls.
# ≈ Combined pre-travel dispatch & roll-out buffer
PRE_TRAVEL_MIN: float = 3.0

# ≈ Our estimate; on-scene time is not published for Karnataka
ON_SCENE_MEAN_MIN: float = 15.0
# ◐ Source: CAG Report 3 of 2020: Handover delays in government tertiary/district hospitals
HANDOVER_MEAN_MIN: float = 20.0

# Critical calls get the nearest free Advanced (ALS) unit only if it arrives within this many minutes of
# the nearest free unit of any kind; otherwise the nearest unit goes (the C-DAC nearest-free rule). ≈ policy choice
ALS_EXTRA_MIN: float = 5.0

# Fleet Operational Parameters
# ◐ News reports (2025): ~715 ambulances statewide, ~530 on the road (~25% under maintenance)
BASE_AVAILABILITY: float = 0.74

# ≈ Sirens, aggressive lane filtering, and emergency vehicle right-of-way over standard congestion
AMBULANCE_SPEED_MULTIPLIER: float = 1.15

# ≈ Our first-help target for bike responders (call to arrival)
BIKE_TARGET_MIN: float = 10.0

# MILP & Optimization Settings
# ◐ Source: Daskin (1983) Maximum Expected Covering Location Problem (MEXCLP)
MEXCLP_COVER_LEVELS: int = 3
# ≈ Operational policy: at most 25% of vehicles reassigned between consecutive 4-hour shifts to minimize crew fatigue
MOVES_CAP_FRACTION: float = 0.25
# ≈ Equity mandate: minimum 50% coverage probability floor across any administrative ward/taluk
WARD_MIN_COVER_FLOOR: float = 0.50
# Candidate standby locations per demand hexagon (for sparse graph efficiency)
NEAREST_CANDIDATE_SITES: int = 25
# Simulation duration (days)
SIMULATION_DAYS: int = 28
# Global pseudo-random seed for determinism and reproducibility
SEED: int = 7

# Site capacity limits: max vehicles stationed at any single hospital / station
MAX_AMBULANCES_PER_SITE: int = 3
MAX_BIKES_PER_SITE: int = 2


@dataclass
class RegionConfig:
    name: str
    code: str
    h3_res: int
    osm_admin_level: int
    zone_label: str
    target_min: float
    calls_per_day: float
    fleet_ambulances: int
    als_share: float
    fleet_bikes: int
    congestion_enabled: bool
    rural: bool
    description: str


# Regional Configurations
# ◐ Bengaluru (urban): Cabinet target 15-20 min (we test the strict 15); 72 ambulances deployed on NYE 2025 (news); 30 of the 100 relaunched bikes (our assumption).
# ◐ Source: Tumakuru (rural): Cabinet target 10 min response; 30 ambulances (~4% of state fleet 715); 0 bikes.
# ≈ Calls per day: CAG statewide ~2,300 trips/day (41.94 lakh over 5 years) scaled by population share, plus growth.
REGIONS: Dict[str, RegionConfig] = {
    "blr": RegionConfig(
        name="Greater Bengaluru Area",
        code="blr",
        h3_res=8,               # ~0.74 km2 hexagon area
        osm_admin_level=10,     # 369 GBA Wards (reorganized Nov 2025)
        zone_label="Ward",
        target_min=15.0,        # ◐ Cabinet decision Sep 2026: 15-20 min urban target (strict end)
        calls_per_day=520.0,    # ≈ ~23% of statewide volume based on 13.5M metro pop
        fleet_ambulances=72,    # ◐ 72 deployed in the city on NYE 2025 (news); no official district split
        als_share=0.20,         # ≈ 20% Advanced Life Support (ALS) with ventilators/defibs
        fleet_bikes=30,         # ≈ our share of the 100 relaunched bike ambulances (◐ news)
        congestion_enabled=True,
        rural=False,
        description="Bengaluru Urban & BBMP/GBA wards with dense congestion and bike paramedics",
    ),
    "tmk": RegionConfig(
        name="Tumakuru District",
        code="tmk",
        h3_res=7,               # ~5.2 km2 hexagon area
        osm_admin_level=6,      # 10 Taluks
        zone_label="Taluk",
        target_min=10.0,        # ◐ Cabinet decision Sep 2026: 10 min rural target
        calls_per_day=95.0,     # ≈ ~4% of statewide volume based on 2.7M district pop
        fleet_ambulances=30,    # ≈ ~4% of the 715 statewide fleet (population share)
        als_share=0.15,         # ≈ 15% Advanced Life Support
        fleet_bikes=0,          # Rural areas rely on 4-wheel ambulances due to distance
        congestion_enabled=False, # Flat 1.1x highway factor
        rural=True,
        description="Tumakuru District: 10 taluks, rural highways, vast coverage radii",
    ),
}
