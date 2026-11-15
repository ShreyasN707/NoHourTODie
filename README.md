# GoldenHour: Karnataka 108 Emergency Ambulance Fleet Planner

**Hackathon Prototype for PES University "Zero Hour" Hackathon (Track 3: Optimisation)**  
**Team: Super Intelligence (DSCE)**

GoldenHour is an hourly fleet-planning layer built to sit alongside Karnataka's centralized C-DAC NG-ERSS emergency command centre. It does not replace real-time CAD dispatch. Instead, it solves the proactive allocation problem: deciding where Advanced Life Support (ALS) ambulances, Basic Life Support (BLS) ambulances, and first-responder bike paramedics wait before calls arrive, which vehicles reposition when rush-hour traffic shifts, and how many ambulances each district actually requires to meet state response targets.

---

## 1. Executive Summary & Honest Findings

We evaluated emergency ambulance response across 942 urban hexagons in Greater Bengaluru and 2,018 rural hexagons in Tumakuru District using OpenStreetMap road networks, TomTom 2025 traffic congestion patterns, WorldPop 2020 1 km gridded density, and 28 days of simulated call replays.

### Key Findings (simulated; 28 days of calls replayed; about 74% of the fleet on the road)

| Region | Plan | Reached in time | Median | Slowest 1 in 10 | First help within 10 min |
|---|---|---|---|---|---|
| Bengaluru (15 min target, 53 ambulances on the road) | Today (hospitals, by population) | 72.5% | 10.5 min | 23.6 min | 46.4% |
| | GoldenHour (with 22 bikes) | 69.6% | 11.5 min | 23.5 min | 53.6% |
| Tumakuru (10 min target, 22 ambulances on the road) | Today | 13.5% | 22.9 min | 45.8 min | 13.5% |
| | GoldenHour | 17.3% | 21.5 min | 47.8 min | 17.3% |

1. **In Bengaluru, better parking spots alone do not beat parking at hospitals.** The city's 404 hospitals already sit where people live, and every job ends at a hospital anyway.
2. **Bike first responders are the real gain in Bengaluru:** first help within 10 minutes rises from 46% to 54%.
3. **The 10-minute rural target is far out of reach with today's fleet.** In Tumakuru, smarter placement lifts on-time arrivals from 13.5% to 17.3%, but even double the fleet reaches only about 29% (fleet-size tab). Penalties should be set only after this is understood.
4. **Bengaluru needs about 1.25–1.5x today's fleet** to reach 83–88% of calls within 15 minutes.

All numbers come from `web/public/data/` after `make all`. Calls are synthetic (108 call logs are not public), so these are model results, not measurements.

---

## 2. Real-World Context & Verified Facts

Every assumption in GoldenHour is grounded in government records, statutory audit reports, and live traffic measurements:

- **Fleet availability (news reports, 2025):** about 715 ambulances statewide, about 530 (~74%) on the road; ~25% under maintenance.
- **CAG Report No. 3 of 2020 (Government of Karnataka):**
  - Only 72% of urban 108 ambulance trips met the 20-minute target between 2014 and 2019.
  - Triage took over 3 minutes in 47% of calls; wheels-rolling took over 1 minute in 85% of calls.
  - Over 41.94 lakh trips were conducted statewide over 5 years (~2,300 calls per day).
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
   Road network parsed directly from the Geofabrik Southern Zone extract using PyOsmium. We retain the largest strongly connected component (1.73 million nodes and 3.58 million directed edges). Shortest paths are precomputed using `scipy.sparse.csgraph.dijkstra` on CSR matrices and transposed CSR matrices (for hospital returns).
5. **Discrete-Event Simulation:**
   A 28-day trace-driven replay of emergency calls. Dispatches the nearest available ambulance (and bike paramedic when clinically indicated). Units become free at hospital handover and can be re-dispatched while driving back to their standby site (GPS-tracked). Advanced units go to critical calls when within 5 minutes of the nearest unit.
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
- **Our Answer:** We formulate candidate standby sites as general tokens characterized by catchment population, nearby road exposure, and local congestion. Our approach is to make the model work on any set of sites, so it can be trained on one district and used on another, for example between the 1.73-million-node road network of Greater Bengaluru and the rural highway network of Tumakuru, without commercial solver dependencies.

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
- Python 3.12 via `uv`, plus `brew install osmium-tool libomp` on macOS
- Node.js 18+ and `npm`

### Quick Start:
```bash
# 1. Clone repository
git clone https://github.com/ShreyasN707/NoHourTODie.git
cd NoHourTODie

# 2. One-time setup and data download (~580 MB), then the pipeline
make setup
make data
make geo travel demand forecast export
#    ...or skip the download and the hours of compute: unpack the team data package instead
#    make package     (reads GoldenHour_Data.zip; keeps any file a real run already wrote)

# 2b. Black spots for the fleet map (~15 min; make blackspots-quick takes ~3)
make blackspots

# 3. Start the Web Dashboard
cd web
npm install
npm run dev
```

Visit `http://localhost:5173` in your browser to explore the dashboard.

### Live demo: command centre and crew

Run each of these in its own terminal, then open the command centre at `http://localhost:5173` and the crew app at `http://localhost:5173/crew.html` (made for phones: open it on a phone on the same Wi-Fi, or in a narrow browser window).

```bash
make ops            # live incidents shared by both screens, port 8788
make advisor-api    # hospital advisor for the crew, port 8000 (needs make advisor-setup and data/raw/goldenhour_data)
make assistant      # optional chat assistant, port 8787 (needs GEMINI_API_KEY in .env)
cd web && npm run dev
```

What happens during the demo:
1. **A call comes in.** Press "Simulate incoming call" in the Live operations panel. It draws a call for this hour from the modelled call log; in service it would come from the C-DAC system. GoldenHour plans the response straight away: the ambulance (Advanced if the call needs it), a bike if it helps, the road route at this hour's traffic, the nearest hospital, and alerts for the crew, the hospital and the traffic police.
2. **The operator confirms.** Nothing is sent until a person presses "Confirm and alert crew".
3. **The crew takes over, one step at a time.** The crew phone shows a large alert with the call type, the drive time against the target and whether a bike is also going, and two buttons: Accept or Reject. Reject hands the job straight to the next-best free unit. After accepting, the phone shows the live map on one side and only the current step on the other: drive to the patient, then say what is wrong (big buttons, vitals optional), then the hospital advisor's top three with how the minutes add up (drive, emergency-room wait, risk of a second transfer), then the drive to the chosen hospital and the handover. On the demo, vehicles move 8 times faster than real time.
4. **The command centre watches the clock.** Every tap is time-stamped, so each call shows where its minutes went: alert, wheels rolling, drive, time on scene, drive to hospital and handover.
5. **Coverage is kept.** When a unit leaves its standby spot, the command centre shows which areas no longer have an ambulance within the target, and suggests the one move that closes the gap without opening a bigger one elsewhere. It suggests a move only when the gain is at least 0.05 calls an hour.

All of this is a demo: units are assumed free unless already sent in this session, alerts are only logged, and state is kept in memory.

### Black spots: where the ambulance could not reach in time

Open **Fleet plan** and pick **Black spots** in the map colours. On **Live calls**, the **Black spots** button lays them over the live map.

- **Find.** Each plan is replayed call by call over 12 weeks. A hexagon is a black spot when at least 3 of its calls were late and, at one-sided 95% confidence (Wilson lower bound), at least half were late. A dark edge means it was also a black spot in the 12 weeks before. On 4-week windows only about a third of black spots recur, because each hexagon gets few calls. On 12 weeks about half recur in Bengaluru and two thirds in Tumakuru.
- **Explain.** For every late call, the replay also records the drive from the nearest staffed site and from the nearest candidate site of any kind. That gives one cause per call:
  - **out of reach:** no site can reach it in time, so open a post;
  - **no unit planned nearby:** a site could reach it, but none is staffed, so re-place a unit;
  - **nearby unit busy:** a staffed unit was out on another job, so add a unit.
- **Fix, then re-plan.** Each step re-plans with the same MEXCLP MILP and is replayed again:
  1. **Re-place.** The same fleet, with black-spot hexagons weighted up in the objective, in 3 feedback rounds.
  2. **Add posts.** New standby posts go at out-of-reach black spots. Their drive times come from Dijkstra on the OSM graph, and they are chosen by greedy maximum coverage of late calls.
  3. **Add ambulances.**

  The map then shows each fix's units, block by block, with fixed (green) and new (cyan) black spots outlined.
- **Chronic, new, resolved.** Each plan's black spots are compared with the same plan's previous 12 weeks. **Chronic** spots (dark outline) are black in both periods, **new** spots (magenta outline) only now, and **resolved** spots (green outline) only before. For a stage, the comparison can switch to **the stage before it**, for a before-and-after view of each intervention.
- **On the map.** Filter chips show or hide chronic, new and resolved spots, and each cause. Proposed posts are cyan pins. Clicking a hexagon shows its total calls, late calls, late %, confidence score (the Wilson lower bound) and the count of each cause. A stage table compares every stage with the one before (spots, resolved, new, in time), and also appears on the Results page.
- **Held out.** Weights and posts are chosen on the 12 weeks before. Every number shown is from the last 12 weeks, 9 Aug to 31 Oct 2026. Solved plans are cached in `data/processed/blackspots_cache/`, so re-exporting takes seconds; `--no-cache` re-solves.

| 12 weeks, synthetic calls | Black spots | Late calls in them | Reached in time |
|---|---|---|---|
| Bengaluru, Today (hospitals) | 336 | 4,322 | 71.5% |
| Bengaluru, GoldenHour plan | 61 | 1,052 | 68.6% |
| + re-placed for black spots (same fleet) | 45 | 740 | 68.4% |
| + 6 posts (2 staffed; out-of-reach late calls 201 to 50) | 53 | 816 | 68.9% |
| + 7 ambulances (+10%) | 40 | 467 | 74.5% |
| + 18 ambulances (+25%) | 20 | 276 | 81.2% |
| Tumakuru, GoldenHour plan | 960 | 5,327 | 16.4% |
| + 8 ambulances (+25%), with 8 posts | 937 | 5,067 | 19.7% |

**What the numbers say:**
- **Bengaluru.** Today's plan has five times as many black spots as GoldenHour's. Today reaches more calls in time overall, but its late calls pile up in the same places. Re-placing the same fleet removes about a quarter of the remaining black spots. Posts cure the out-of-reach calls, but the optimiser staffs only 2 of the 6, so black spots rise against re-placing alone. Most late calls (about 90%) are busy-unit delays, spread thinly, and only more ambulances fix those.
- **Tumakuru.** Most of the district is out of reach at the 10-minute target. Re-placing does not help there, and posts help little without more vehicles. This is a fleet-size and target question, not a placement one.

Files: `pipeline/blackspots.py`, thresholds `BLACKSPOT_*` in `pipeline/config.py`, map code in `web/src/blackspots/`, tests in `tests/test_blackspots.py`.


---

## 7. Module B: Destination Advisor

After the crew assesses the patient, the advisor recommends the hospital that minimises **expected time to definitive treatment**, not time to the nearest door, and pre-alerts it. It adds a hospital-selection step to Karnataka 108. It does not replace C-DAC dispatch.

```
score(h) = travel(scene -> h, at that hour) + expected_ED_wait(h)
         + P(cannot treat | h) * transfer_delay + lambda * coverage_lost(h)      # lambda = 0 for critical patients
```

### Run it

```bash
make setup && make advisor-setup   # shared .venv, plus module B packages
make advisor-test                  # pytest + ruff
make advisor-api                   # FastAPI on :8000 (loads the OSM road graph if data/processed/graph_*.parquet exist)
cd web && npm run dev              # crew dashboard: http://localhost:5173/crew (or /#crew on a static host)
make advisor-sim                   # SimPy replay + sensitivity sweeps (~1 min), then results/advisor_sim/EVALUATION.md
```

The road graph comes from GoldenHour (`make geo`, or `road_graph_*.parquet` in the team data package copied to `data/processed/graph_*.parquet`). Without it, the API falls back to straight-line travel estimates.

### Simulation result (synthetic data, not a field result)

In a replay of 14,875 synthetic Bengaluru incidents with fictional hospitals, the advisor cut the mean time to definitive treatment for critical patients from **37.2 to 23.7 min** (90th percentile 89.4 to 52.9). Secondary transfers fell from 25.7% to 0.0%. It stayed better in 34 of 34 sensitivity scenarios. The headline assumes a perfect capability registry, and most low-acuity gains come from the simulated crowding model. See [results/advisor_sim/EVALUATION.md](results/advisor_sim/EVALUATION.md) for the full tables and caveats.

| Path | What |
|---|---|
| `config/advisor.yaml` | Every threshold and placeholder, each with provenance and basis. The loader refuses params without them |
| `src/advisor/rules/` | Triage to a care category, then per-category decision rules (STEMI, stroke, trauma, obstetric/paediatric, arrest, low acuity) |
| `src/advisor/scoring/` | Score function, P(cannot treat) with Beta prior and data-age decay (O(1) running counts), ED-wait fallback chain |
| `src/advisor/graph/` | Time-dependent travel times: OSM road graph, haversine fallback, fixed table |
| `src/advisor/registry/` | Verified registry loader (`registry/*.csv`), data.gov.in directory loader, OSM phone-audit worklist, FICTIONAL demo registry |
| `src/advisor/passive/` | 108 gate-dwell log loader and rolling offload index; accept/reject outcome store |
| `src/advisor/prealert/` | Pre-alert, accept/reject timer, automatic fallback, refusal logging (Karnataka accident-victim circular) |
| `src/advisor/api/` | FastAPI endpoints (`docs/advisor_contract.md`) |
| `src/advisor/sim/` | SimPy replay (nearest vs nearest + pre-alert vs advisor), sensitivity sweeps, evaluation report |
| `web/src/advisor/` | Crew dashboard (`/crew`, `/crew/sim`): map, assessment, ranked hospitals with score breakdown and provenance badges, pre-alert, simulation results |

### Data provenance status

- **Hospital capabilities:** `registry/hospitals.csv` is empty until the verified registry is filled from websites, empanelment lists and phone calls. Until then, the API and simulation use a **FICTIONAL demo registry** ("Demo Hospital NN (fictional)", SIMULATED), and the UI shows a banner. Real hospitals are never given invented capabilities.
- **108 call/GPS logs:** none yet. `GoldenHour_Data.zip` holds GoldenHour's **synthetic** call year, not real 108 logs, and it has no hospital, handover or GPS dwell fields. Offload/dwell and accept/reject data therefore stay **SIMULATED**.
- **From the GoldenHour data package:**
  - The OSM road graph (real network, estimated speeds) is used for routing.
  - The synthetic calls feed the replay as **SIMULATED** incidents.
  - 404 OSM hospital locations form an *unverified* phone-audit worklist (`registry/osm_hospital_candidates_blr.csv`). It is incomplete, because GoldenHour kept one site per H3 cell.
- **Travel speeds:** every travel speed is a PLACEHOLDER (GoldenHour's hand-shaped congestion curve). They become REAL_DERIVED once fitted from a week of sampled routing-API travel times. They look optimistic at peak hour compared with press reports.
- **Clinical sign-off:** none of the clinical thresholds or lambda values has health-department or clinical-society sign-off yet.

### Simulated and placeholder parameters (generated from `config/advisor.yaml`)

| Param | Value | Provenance |
|---|---|---|
| `scoring.transfer_delay_min` | 50 | PLACEHOLDER |
| `scoring.ed_wait_by_status_min` | {'green': 5, 'amber': 15, 'red': 30} | PLACEHOLDER |
| `scoring.ed_status_when_unknown` | amber | PLACEHOLDER **TODO: source** |
| `scoring.live_status_max_age_min` | 60 | PLACEHOLDER **TODO: source** |
| `scoring.pessimistic_travel_quantile` | 0.8 | PLACEHOLDER |
| `scoring.lambda_by_category` | see config | PLACEHOLDER |
| `scoring.low_acuity_travel_cap_min` | null (disabled) | PLACEHOLDER **TODO: source** |
| `reliability.prior_rejects` | 1 | PLACEHOLDER |
| `reliability.prior_total` | 10 | PLACEHOLDER **TODO: source** |
| `reliability.decay_half_life_days` | 30 | PLACEHOLDER **TODO: source** |
| `travel.pessimistic_multiplier_p80` | 1.25 | PLACEHOLDER **TODO: source** |
| `travel.detour_factor` | 1.4 | PLACEHOLDER **TODO: source** |
| `travel.fallback_free_flow_kmh` | 38.0 | PLACEHOLDER **TODO: source** |
| `travel.ambulance_speed_multiplier` | 1.15 | PLACEHOLDER **TODO: source** |
| `travel.hourly_congestion` | 24 values | PLACEHOLDER |
| `passive.hour_band_hours` | 4 | PLACEHOLDER |
| `passive.min_dwell_samples` | 5 | PLACEHOLDER **TODO: source** |
| `passive.dwell_window_days` | 28 | PLACEHOLDER **TODO: source** |
| `rules.chest_pain_without_ecg_as_stemi` | True | PLACEHOLDER |
| `rules.stemi_fmc_to_pci_window_min` | 120 | PLACEHOLDER |
| `rules.stroke_lvo_score_cutoff` | null (disabled) | PLACEHOLDER **TODO: source** |
| `rules.stroke_thrombectomy_extra_travel_limit_min` | null (disabled) | PLACEHOLDER **TODO: source** |
| `rules.trauma_max_travel_min` | null (disabled) | PLACEHOLDER **TODO: source** |
| `rules.unstable_vitals` | see config | PLACEHOLDER |
| `sim.call_type_to_complaint` | see config | PLACEHOLDER |
| `sim.seed` | 7 | PLACEHOLDER |
| `sim.demo_hospital_count` | 30 | SIMULATED **TODO: source** |
| `sim.demo_tier_shares` | see config | SIMULATED **TODO: source** |
| `sim.demo_tier_capabilities` | see config | SIMULATED **TODO: source** |
| `sim.replay_window_days` | 28 | PLACEHOLDER |
| `sim.sweep_window_days` | 6 | PLACEHOLDER |
| `sim.warmup_hours` | 24 | PLACEHOLDER |
| `sim.call_to_scene_min` | 13.5 | SIMULATED |
| `sim.on_scene_min` | 15 | SIMULATED |
| `sim.handover_min` | 10 | PLACEHOLDER **TODO: source** |
| `sim.travel_noise_sigma` | 0.25 | PLACEHOLDER **TODO: source** |
| `sim.chest_pain_ecg_shares` | see config | SIMULATED **TODO: source** |
| `sim.stemi_share_without_ecg` | 0.25 | SIMULATED **TODO: source** |
| `sim.trauma_severe_share` | 0.2 | SIMULATED **TODO: source** |
| `sim.stroke_lvo_share` | 0.3 | SIMULATED **TODO: source** |
| `sim.stroke_scores` | {'lvo': 6, 'non_lvo': 2} | SIMULATED **TODO: source** |
| `sim.other_unstable_share` | 0.1 | SIMULATED **TODO: source** |
| `sim.other_paediatric_share` | 0.05 | SIMULATED **TODO: source** |
| `sim.vitals_profiles` | see config | SIMULATED |
| `sim.ed_bays_by_class` | see config | SIMULATED **TODO: source** |
| `sim.ed_stay_mean_min` | 120 | SIMULATED **TODO: source** |
| `sim.background_arrivals_per_hour_by_class` | see config | SIMULATED **TODO: source** |
| `sim.background_hourly_profile` | 24 values | SIMULATED **TODO: source** |
| `sim.background_load_scale` | 1.0 | PLACEHOLDER |
| `sim.ed_status_queue_per_bay` | {'red': 0.25, 'diversion': 0.75} | SIMULATED **TODO: source** |
| `sim.status_report_interval_min` | 30 | PLACEHOLDER **TODO: source** |
| `sim.status_stale_share` | 0.2 | SIMULATED **TODO: source** |
| `sim.status_stale_interval_min` | 240 | SIMULATED **TODO: source** |
| `sim.status_misreport_share` | 0.1 | SIMULATED **TODO: source** |
| `sim.reject_base_rate` | 0.08 | SIMULATED **TODO: source** |
| `sim.reject_status_multiplier` | see config | SIMULATED **TODO: source** |
| `sim.reject_max_prob` | 0.9 | SIMULATED **TODO: source** |
| `sim.max_refusals_per_patient` | 2 | PLACEHOLDER **TODO: source** |
| `sim.refusal_gate_min` | 10 | SIMULATED **TODO: source** |
| `sim.prealert_no_answer_share` | 0.1 | SIMULATED **TODO: source** |
| `sim.registry_error_share` | 0.0 | PLACEHOLDER **TODO: source** |
| `sim.accepted_prealert_refused_on_arrival_share` | 0.0 | PLACEHOLDER **TODO: source** |
| `prealert.response_timeout_s` | 120 | PLACEHOLDER **TODO: source** |
