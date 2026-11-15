# Destination Advisor: API and simulation-results contract

This is the single source of truth shared by the FastAPI app (`src/advisor/api/`), the SimPy replay (`src/advisor/sim/`) and the web screen (`web/src/advisor/`). Change it only together with all three.

All JSON is produced from the pydantic models in `src/advisor/models.py` with `model_dump(mode="json")`. Datetimes are ISO 8601 local Bengaluru time with no timezone (naive), matching the synthetic call data.

## Data-honesty banner

Whenever a response uses the fictional demo registry, or any SIMULATED/PLACEHOLDER input, it carries a `banner` string, and the UI must show it in a visible strip.

```
DEMO_BANNER = "SIMULATED DATA: fictional demo hospitals, simulated ED status and placeholder travel speeds. Not live hospital data."
```

## API (FastAPI, default http://localhost:8000, CORS allows http://localhost:5173)

| Method and path | Body | Response |
|---|---|---|
| `GET /health` | none | `{ok: true, registry_is_demo: bool, travel_provider: "osm_graph" \| "haversine"}` |
| `GET /hospitals` | none | `{is_demo: bool, banner: str \| null, summary: RegistrySummary, hospitals: Hospital[]}` |
| `GET /config/params` | none | `{params: [{key, value, provenance, basis, needs_source}]}` |
| `POST /recommend` | `Assessment` | `RecommendResponse` (below) |
| `POST /prealert` | `{case_id, crew_choice?: str, now?: datetime}` | `PreAlert` (from `advisor.prealert.fallback`) |
| `POST /prealert/{prealert_id}/response` | `{accepted: bool, reason_code: str, now?: datetime}` | `PreAlert` |
| `POST /prealert/check_timeouts` | `{now?: datetime}` | `PreAlert[]` |
| `GET /sim/results` | none | contents of `results/advisor_sim/sim_results.json`, or HTTP 404 `{detail: "no simulation results yet; run make advisor-sim"}` |

Reason codes for `/prealert/{id}/response`: `ACCEPTED` when accepting, and `NO_BED`, `NO_SPECIALIST`, `EQUIPMENT_DOWN` or `OTHER` when rejecting. A timeout is logged as `TIMEOUT`. A rejected trauma case also gets `|REFUSAL_ACCIDENT_VICTIM_KA_CIRCULAR_SEP2025` appended.

`now` defaults to the server clock. `/prealert` uses the last recommendation stored for that `case_id` by `/recommend`. If there is none, it returns 404.

```jsonc
// RecommendResponse
{
  "recommendation": Recommendation,       // models.Recommendation
  "is_demo_registry": bool,
  "banner": str | null,
  "travel_provider": "osm_graph" | "haversine",
  "hospitals": { "<hospital_id>": {"name": str, "lat": float, "lon": float} },  // every ranked + excluded id
  "todo_params": [str]                     // config params still marked "TODO: source"
}
```

Traffic-flip demo: the client sends the same Assessment twice, with `when` at 11:00 and then 18:00 on the same day. No special endpoint.

## Simulation results file: `results/advisor_sim/sim_results.json`

```jsonc
{
  "generated_at": "2026-11-16T10:00:00",
  "seed": 7,
  "banner": DEMO_BANNER,
  "inputs_provenance": { "incidents": "SIMULATED", "hospitals": "SIMULATED", "road_network": "REAL",
                         "travel_speeds": "PLACEHOLDER", "ed_crowding": "SIMULATED", "rejections": "SIMULATED", ... },
  "window": {"start": "...", "end": "...", "n_incidents": int},
  "base": Scenario,
  "sensitivity": [ SensitivityRow ]
}
// Scenario
{ "name": str, "overrides": {param_key: value},
  "policies": { "nearest": Metrics, "advisor": Metrics } }
// Metrics (minutes unless stated)
{ "n": int,
  "time_to_treatment": { "overall": Stat, "by_category": { "<care_category>": Stat } },   // Stat = {n, mean, p50, p90}
  "secondary_transfers": int, "secondary_transfer_rate": float,
  "ambulance_hours": float,
  "critical_capable_first_attempt_share": float,   // critical = care categories with lambda 0, excluding arrest_airway
  "prealert_rejects": int, "fallbacks": int }
// SensitivityRow
{ "param": str, "value": any, "label": str,
  "nearest_mean_ttt_critical": float, "advisor_mean_ttt_critical": float,
  "delta_mean_ttt_critical": float,     // advisor - nearest (negative = advisor better)
  "advisor_better": bool }
```

The simulation also writes `results/advisor_sim/summary.csv`, one row per scenario x policy, for slides.
