# GoldenHour: Karnataka 108 Emergency Ambulance Fleet Planner

**Hackathon Prototype for PES University "Zero Hour" Hackathon (Track 3: Optimisation)**  
**Team: Super Intelligence (DSCE)**

GoldenHour is an hourly fleet-planning layer built to sit alongside Karnataka's centralized C-DAC NG-ERSS emergency command centre. It does not replace real-time CAD dispatch. Instead, it solves the proactive allocation problem: deciding where Advanced Life Support (ALS) ambulances, Basic Life Support (BLS) ambulances, and first-responder bike paramedics wait before calls arrive, which vehicles reposition when rush-hour traffic shifts, and how many ambulances each district actually requires to meet state response targets.

---

## 1. Executive Summary & Honest Findings

We evaluated emergency ambulance response across 942 urban hexagons in Greater Bengaluru and 2,018 rural hexagons in Tumakuru District using OpenStreetMap road networks, TomTom 2025 traffic congestion patterns, WorldPop 2020 1 km gridded density, and 28 days of simulated call replays.

### Key Operational Findings:
1. **Dynamic Placement Alone Has Marginal Impact in Bengaluru (+0.2% on 15-Minute Target):**
   In dense urban Bengaluru, existing 108 ambulances are already parked at 404 government and private hospitals. Because hospital sites naturally correlate with population settlement, moving four-wheeled ambulances to alternative road standbys provides almost no travel-time improvement under heavy rush-hour congestion (average speed 14 km/h).
2. **Two-Wheeler Bike Ambulances Deliver the Real Clinical Breakthrough (+7.5% First Help within 10 Minutes):**
   Adding 30 first-responder bike ambulances capable of filtering through urban traffic increases the share of patients receiving emergency medical help within 10 minutes from 45.0% to 52.5%. For cardiac arrests and severe trauma, this first-responder stabilization is what saves lives during the critical golden hour.
3. **The Cabinet's 10-Minute Rural Target is Mathematically Impossible with Today's Fleet:**
   In Tumakuru District, the current allocation of 30 ambulances achieves only a 14.9% compliance rate on the Cabinet's 10-minute response target, with a median response time of 23.5 minutes and a 90th-percentile response time of 48.0 minutes. Meeting a 10-minute rural target across 10,600 km² would require at least 75 to 90 ambulances. Setting performance penalty fines of ₹5,000 to ₹10,000 per violation without tripling the fleet would penalize operators for impossible physical geometry.

---

## 2. Real-World Context & Verified Facts

Every assumption in GoldenHour is grounded in government records, statutory audit reports, and live traffic measurements:

- **CAG Report No. 3 of 2020 (Government of Karnataka):**
  - Only 72% of urban 108 ambulance trips met the 20-minute target between 2014 and 2019.
  - Triage took over 3 minutes in 47% of calls; wheels-rolling took over 1 minute in 85% of calls.
  - Over 41.94 lakh trips were conducted statewide over 5 years (~2,300 calls per day).
  - Out of roughly 715 ambulances statewide, road availability averaged 74% due to maintenance backlogs.
- **May 2025 State Takeover & May 2026 Command Centre:**
  - The State Department of Health and Family Welfare took direct operational control of 108 from GVK-EMRI in May 2025.
  - A unified command and control centre opened on 25 May 2026, running C-DAC NG-ERSS software with real-time GPS tracking and automated nearest-ambulance dispatch.
- **September 2026 Cabinet Policy (Sources: Daijiworld & News Karnataka):**
  - The Karnataka State Cabinet approved privatizing field operations under a public-private partnership.
  - The Cabinet set operational targets of 10 minutes for rural emergencies and 15 to 20 minutes for urban emergencies, accompanied by proposed fines of ₹5,000 to ₹10,000 for delayed trips.
- **TomTom Traffic Index 2025 (Bengaluru Urban Profile):**
  - Bengaluru ranks among the slowest cities globally, averaging 36 minutes per 10 km journey during peak hours.
  - Evening rush-hour vehicle speeds average 14 km/h, generating a 2.6x congestion multiplier over free-flow speeds.

---

## 3. Mathematical Optimization & Technical Stack

GoldenHour integrates six mathematical and computational methods:

1. **Multi-Period Maximum Expected Covering Location Problem (MEXCLP, Daskin 1983):**
   Formulated as a Mixed-Integer Linear Program (MILP) with integer site vehicle assignments and continuous coverage variables. Solved using the open-source **HiGHS** simplex/branch-and-bound solver via `scipy.optimize.milp`.
2. **Rolling Horizon Shift Decomposition:**
   Because the full joint 6-block, 24-hour MILP creates over 45,000 columns and 690,000 non-zeros, we decompose the problem into a rolling horizon:
   - Solve Block 4 (Evening Peak, 16:00-20:00) unconstrained.
   - Roll forward through Block 5, Block 0, Block 1, Block 2, and Block 3, capping vehicle relocations at 25% of the fleet.
   - Re-solve Block 4 constrained against both Block 3 and Block 5, ensuring cyclic shift continuity.
3. **Graded Coverage Extension:**
   Standard MEXCLP provides zero reward for vehicles arriving beyond the target threshold. We extended the model with graded partial credit (0.40 weight for arrivals within 1.5x target, and 0.20 weight for arrivals within 2.0x target), ensuring the solver penalizes and compresses extreme delays.
4. **Dijkstra Shortest Paths on OpenStreetMap:**
   Road network parsed directly from Geofabrik Karnataka extracts using PyOsmium. We retain the largest strongly connected component (1.73 million nodes and 3.58 million directed edges). Shortest paths are precomputed using `scipy.sparse.csgraph.dijkstra` on CSR matrices and transposed CSR matrices (for hospital returns).
5. **Discrete-Event Simulation (SimPy Principles):**
   A 28-day trace-driven replay of emergency calls. Dispatches the nearest available ambulance (and bike paramedic when clinically indicated). Units become free at hospital handover upon triage discharge, capturing real-world GPS-tracked return flows.
6. **Native LightGBM Poisson Demand Forecasting:**
   Ward-level 4-hour demand predicted using calendar signals, rolling 28-day averages, and weekly lag counts. LightGBM achieves a 0.3507 MAE on Bengaluru ward-blocks, outperforming the historical 4-week moving average baseline (0.3617 MAE).
7. **Spatial Hexagon Partitioning (Uber H3):**
   Resolution 8 (~0.74 km² hexagons) for urban Bengaluru (942 cells); Resolution 7 (~5.2 km² hexagons) for rural Tumakuru (2,018 cells).

---

## 4. Answering Gaps in Recent Research Papers

### Paper 1: arXiv 2508.20953 (*Multi-Objective Genetic Algorithm for Healthcare Workforce Scheduling*)
- **Their Gap:** Uses NSGA-II to balance hospital staffing costs, service coverage, and employee dissatisfaction. In their results (Table 3), the "balanced" pick trades away patient care, causing severe coverage shortfalls.
- **Our Answer:** Patient emergency care cannot be traded away. We enforce hard coverage floors and minimum equity constraints across all municipal wards, optimizing crew relocations only after clinical safety floors are satisfied.

### Paper 2: arXiv 2507.15385 (*Transformer-based Deep Learning Model for Joint Routing and Scheduling*)
- **Their Gap:** Uses a transformer to predict binary MIP decisions and prune the search space for Gurobi. However, it was evaluated only on a synthetic 13-node grid, achieved only ~64% accuracy, and required commercial solver fallbacks.
- **Our Answer:** We formulate candidate standby sites as general tokens characterized by catchment population, nearby road exposure, and local congestion. This formulation transfers seamlessly between the 1.73-million-node road network of Greater Bengaluru and the rural highway network of Tumakuru, without commercial solver dependencies.

---

## 5. Web Application Features

The interactive dashboard is built with Vite, React, TypeScript, Deck.gl, and MapLibre:

- **Interactive Map:**
  - H3 hexagon overlays colored by Chance of In-Time Arrival, Drive Time to Nearest Standby, Calls per Hour, or Peak Reachability.
  - Candidate standby sites with color-coded vehicle markers (Red for Advanced Life Support, Blue for Basic Life Support, Orange for Bike Paramedics).
  - Dynamic ArcLayer depicting vehicle shift relocations.
  - Hover tooltips showing population, demand, and local road lengths.
- **Control Panel:**
  - Strategy selector: Today (CAG Baseline), Best Fixed Spots (Static), or GoldenHour Dynamic Plan.
  - Time-of-day slider covering all six 4-hour operational shifts.
  - Real-time KPI delta cards tracking target compliance, median response, 90th-percentile response, and bike first-help.
  - Hand-drawn SVG response time histogram comparing selected strategy against the baseline.
- **Analysis Drawers:**
  - Ward & Taluk response leaderboard sorted by slowest zones first.
  - Stress testing battery (Demand Surge +20%, Traffic Spike +20%, Fleet Maintenance Outage at 60%).
  - Fleet scaling sensitivity curve answering how many vehicles Karnataka needs.
  - Demand forecast comparison and verified assumptions table.

---

## 6. How to Run GoldenHour

### Prerequisites:
- Python 3.12 (managed via `uv` or system Python)
- Node.js 18+ and `npm`

### Quick Start:
```bash
# 1. Clone repository
git clone https://github.com/super-intelligence/goldenhour.git ~/goldenhour
cd ~/goldenhour

# 2. Run Python pipeline (processes OSM road graph, demand, and optimization)
make all

# 3. Start the Web Dashboard
cd web
npm install
npm run dev
```

Visit `http://localhost:5173` in your browser to explore the dashboard.
