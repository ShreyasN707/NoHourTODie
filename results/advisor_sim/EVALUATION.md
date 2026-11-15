# Destination Advisor: simulation evaluation

> **SIMULATED DATA: fictional demo hospitals, simulated ED status and placeholder travel speeds. Not live hospital data.**
> These are simulation results, not field results. Nothing here has been measured on real patients, real hospitals or real 108 data.

Generated 2026-11-15T10:49:46 from `results/advisor_sim/sim_results.json` (seed 7). Base window 2026-10-04 to 2026-11-01: 14,875 synthetic incidents (GoldenHour's synthetic call year, Bengaluru).

## Headline (base scenario)

Compared with taking every patient to the nearest hospital, the advisor cut the mean time to definitive treatment for critical patients by **13.5 min** (37.2 to 23.7). Pre-alerting the nearest hospitals without the advisor's ranking accounts for 2.5 min of that; the rest comes from choosing a capable, less crowded hospital.

| Metric | Nearest hospital (today) | Nearest + pre-alert | Destination Advisor |
|---|---|---|---|
| Critical patients: mean time to treatment (min) | 37.2 | 34.7 | 23.7 |
| Critical patients: 90th percentile (min) | 89.4 | 85.4 | 52.9 |
| All patients: mean time to treatment (min) | 39.0 | 35.7 | 23.2 |
| Secondary transfers (moved on after refusal or missing capability) | 3,817 (25.7%) | 886 (6.0%) | 0 (0.0%) |
| Critical patients at a capable hospital on first attempt | 67.8% | 86.1% | 100.0% |
| Ambulance-hours (scene to back at base) | 14,580 | 13,771 | 11,158 |
| Pre-alert rejections / fallbacks | 0 / 0 | 4,059 / 6,186 | 2,260 / 4,141 |

Ambulance-hours saved over the window: **3,422** (23.5%).

## By care category (mean / 90th percentile, minutes)

| Category | n | Nearest | Advisor |
|---|---|---|---|
| stemi | 373 | 31.7 / 59.9 | 26.3 / 50.9 |
| stroke | 594 | 32.3 / 72.5 | 23.9 / 53.2 |
| severe_trauma | 593 | 37.0 / 82.4 | 28.3 / 56.6 |
| obstetric | 4,518 | 38.2 / 101.1 | 22.6 / 52.1 |
| paediatric | 275 | 38.5 / 65.7 | 26.4 / 54.7 |
| arrest_airway | 516 | 38.8 / 89.5 | 18.9 / 55.8 |
| low_acuity | 8,006 | 40.4 / 104.4 | 23.1 / 58.1 |

## Sensitivity: is the result robust?

The advisor had the lower critical-patient mean in **34 of 34** sweep scenarios. Its smallest margin was 8.1 min, under "EDs 30% less busy (background arrivals x0.7)". Sweeps run on a 6-day window, so their baseline differs slightly from the 28-day headline.

| Scenario | Nearest | Nearest + pre-alert | Advisor | Advisor - nearest |
|---|---|---|---|---|
| EDs 30% less busy (background arrivals x0.7) | 23.2 | 21.9 | 15.1 | -8.1 |
| 30% of the advisor's capability records are wrong | 37.7 | 34.0 | 28.1 | -9.6 |
| 25% of accepted pre-alerts refused on arrival anyway | 37.7 | 39.2 | 28.1 | -9.6 |
| 60% of hospitals always report green | 37.7 | 34.0 | 25.1 | -12.6 |
| 15% of the advisor's capability records are wrong | 37.7 | 34.0 | 25.1 | -12.6 |
| 10% of accepted pre-alerts refused on arrival anyway | 37.7 | 36.6 | 24.5 | -13.2 |
| lambda for low acuity 0 (no load balancing) | 37.7 | 34.0 | 24.3 | -13.4 |
| 30% of pre-alerts go unanswered | 37.7 | 33.8 | 24.1 | -13.6 |
| advisor's ED-wait lookup about halved (2/8/15 min) | 37.7 | 34.0 | 24.0 | -13.7 |
| per-trip refusal rate at a green ED 0.15 (base 0.08) | 34.6 | 27.7 | 20.6 | -14.1 |
| advisor's transfer delay 90 min | 37.7 | 34.0 | 23.5 | -14.2 |
| 30% of hospitals always report green | 37.7 | 34.0 | 23.4 | -14.3 |
| 5% of the advisor's capability records are wrong | 37.7 | 34.0 | 23.4 | -14.3 |
| 40% of trauma is severe | 37.1 | 33.9 | 22.5 | -14.5 |
| more tertiary hospitals (30% / 40% / 30%) | 32.8 | 29.7 | 18.1 | -14.7 |
| half of hospitals' status reports go stale | 37.7 | 34.0 | 22.9 | -14.8 |
| 50% of no-ECG chest pain is truly STEMI | 37.4 | 33.9 | 22.5 | -14.8 |
| traffic multipliers x1.25 (heavier traffic) | 40.7 | 37.3 | 25.8 | -14.9 |
| no hospital misreports its status | 37.7 | 34.0 | 22.5 | -15.2 |
| base config on the sweep window | 37.7 | 34.0 | 22.3 | -15.4 |
| 10% of trauma is severe | 38.1 | 34.9 | 22.6 | -15.5 |
| advisor's ED-wait lookup doubled (10/30/60 min) | 37.7 | 34.0 | 22.2 | -15.5 |
| travel-time noise sigma 0.10 | 37.5 | 34.1 | 22.0 | -15.5 |
| travel-time noise sigma 0.50 (very unpredictable) | 37.9 | 34.6 | 22.3 | -15.6 |
| traffic multipliers x0.75 (lighter traffic) | 34.7 | 31.0 | 18.9 | -15.7 |
| no hospital's status report goes stale | 37.7 | 34.0 | 21.9 | -15.7 |
| most chest pain gets an ECG (20% without) | 37.6 | 33.9 | 21.9 | -15.8 |
| advisor's transfer delay 20 min | 37.7 | 34.0 | 21.9 | -15.8 |
| lambda for low acuity 1.0 | 37.7 | 34.0 | 21.9 | -15.8 |
| 10% of no-ECG chest pain is truly STEMI | 38.2 | 34.3 | 22.3 | -15.9 |
| per-trip refusal rate at a green ED 0.3 (base 0.08) | 37.3 | 24.7 | 19.4 | -17.9 |
| fewer tertiary hospitals (10% / 40% / 50%) | 46.3 | 39.9 | 25.8 | -20.5 |
| EDs 30% busier (background arrivals x1.3) | 80.0 | 71.6 | 49.5 | -30.5 |
| per-trip refusal rate at a green ED 0.02 (base 0.08) | 76.1 | 75.2 | 24.1 | -52.1 |

## How to read this honestly

- **Everything is simulated except the road network.** The incidents are GoldenHour's synthetic calls, and the hospitals are a fictional demo registry. ED crowding, refusals and status reports are simulated. Travel speeds are placeholders. No real 108 logs were available.
- **The headline assumes a perfect capability registry.** The base scenario gives the advisor exact capabilities. With 5.0%, 15.0%, 30.0% of capability records wrong, the advisor's critical mean rises to 23.4, 25.1, 28.1 min (nearest: 37.7 min).
  That is why the verified registry (phone and website audit) comes before any pilot.
- **Much of the gain for low-acuity and obstetric patients comes from avoiding crowded EDs.** It depends on the simulated crowding model. The real-world evidence for crowding-aware routing is weak: FDNY's load-balancing rule raised transport times in simulation and was recommended only for non-critical patients. Capability-first routing (STEMI, stroke, trauma) is the guideline-backed part.
- **Refusals act as load-shedding in this model.** In the 'refusal rate 0.02' sweep, nearest-hospital routing gets much worse, because ambulances queue at the overloaded nearest ED instead of being sent on. That is a property of the queue model, not a finding about real EDs.
- **Definitive care is defined generously for today's practice.** A STEMI counts as treated at any thrombolysis-capable hospital, and the onward PCI transfer is not charged. This favours nearest-hospital routing.
- **The advisor only depends on dispatch being on time.** Karnataka's fleet gap (response times of 45-60 min reported) limits what destination choice can do. The advisor complements faster dispatch; it does not replace it.
- **Next step is a stepped pilot, not more simulation.** A scoping review found only one dynamic ambulance-relocation study evaluated prospectively in real time.

## Simplifications in the model

- No fleet model: every incident has its own ambulance; dispatch and response are module A's job.
- Scene location is the H3 cell centre of the synthetic call.
- An accepted pre-alert is honoured on arrival, except for sim.accepted_prealert_refused_on_arrival_share (0 in the base case, swept).
- Hospitals answer a pre-alert instantly or not at all; an unanswered one costs prealert.response_timeout_s while the ambulance waits at the scene.
- If every pre-alerted hospital rejects, the crew drives unannounced to the advisor's top choice.
- Refusal chance depends only on the hospital's true ED status (sim.reject_*), the same with or without a pre-alert; after sim.max_refusals_per_patient refusals the next capable hospital must take the patient.
- ED offload queue is first-come first-served (no triage priority at the gate).
- STEMI counts as treated at a cath lab or a thrombolysis centre; onward PCI transfer is not modelled.
- Stroke counts as treated at CT + thrombolysis; thrombectomy is not scored separately.
- Gate dwell fed to the advisor's passive index = arrival to departure at the gate (offload wait + handover, or time to a refusal), as 108 GPS would see it.
- Travel uses free-flow shortest paths on the real OSM road graph with placeholder speeds and hourly congestion; actual drives add lognormal noise the advisor does not see.
- Hospital size class (ED bays, background load) is read from capabilities: PCI -> tertiary, CT -> secondary, else primary.

## Inputs and their provenance

| Input | Provenance |
|---|---|
| advisor_scoring_params | PLACEHOLDER |
| ed_crowding | SIMULATED |
| ed_status_reports | SIMULATED |
| gate_dwell | SIMULATED |
| hospitals | SIMULATED |
| incidents | SIMULATED |
| outcome_events | SIMULATED |
| patient_mix | SIMULATED |
| rejections | SIMULATED |
| road_network | REAL |
| travel_noise | PLACEHOLDER |
| travel_speeds | PLACEHOLDER |

Config parameters still marked `TODO: source`: 44. They are listed in the README table and in `config/advisor.yaml`.
