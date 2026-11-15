"""FastAPI app for the Destination Advisor. Endpoints and shapes: docs/advisor_contract.md.

    PYTHONPATH=src .venv/bin/uvicorn advisor.api.app:app --port 8000

Importing this module is cheap: the registry and the OSM road graph load on startup (in a background
thread, with hospital-origin warm-up) or on first use. Set ADVISOR_API_WARMUP=0 to skip the warm-up.

Data honesty: with the current empty verified registry every response that uses hospitals carries
DEMO_BANNER. ED status has no live feed (statuses default to {} -> engine assumes amber), travel speeds
are PLACEHOLDER, and pre-alert accept/reject outcomes are SIMULATED (nothing is sent to any hospital).
"""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from advisor.api.state import AdvisorState, to_local_naive
from advisor.config import AdvisorConfig
from advisor.interfaces import TravelTimeProvider
from advisor.models import Assessment, Hospital, HospitalStatus, LatLon, Recommendation
from advisor.prealert.fallback import PreAlert
from advisor.rules.engine import AdvisorContext, recommend
from advisor.rules.triage import COMPLAINTS

CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
SIM_RESULTS_MISSING = "no simulation results yet; run make advisor-sim"


# ---------------------------------------------------------------- request bodies (contract)

class PreAlertRequest(BaseModel):
    case_id: str
    crew_choice: str | None = None
    now: datetime | None = None


class PreAlertResponseRequest(BaseModel):
    accepted: bool
    reason_code: str
    now: datetime | None = None


class TimeoutRequest(BaseModel):
    now: datetime | None = None


# ---------------------------------------------------------------- factory

def create_app(
    cfg: AdvisorConfig | None = None,
    hospitals: list[Hospital] | None = None,
    travel: TravelTimeProvider | None = None,
    statuses: dict[str, HospitalStatus] | None = None,
    *,
    is_demo: bool | None = None,
    sim_results_path: Path | str | None = None,
    warmup: bool | None = None,
) -> FastAPI:
    """Build the app. Every argument is optional so tests can inject fakes.

    hospitals: defaults to the active registry (verified if non-empty, else FICTIONAL demo). When given,
        `is_demo` says whether to treat them as the demo registry (default False; the banner still shows
        if any hospital tag is SIMULATED/PLACEHOLDER).
    travel: defaults to the OSM road graph if its files exist, else haversine; built lazily.
    statuses: live ED statuses. Default {}: there is no live feed, so the engine assumes amber.
    sim_results_path: where GET /sim/results reads from (default results/advisor_sim/sim_results.json).
    warmup: pre-compute Dijkstra from every hospital at startup (default: on for the real provider).
    """
    state = AdvisorState(cfg, hospitals, travel, statuses, is_demo=is_demo,
                         sim_results_path=sim_results_path, warmup=warmup)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if state.warmup:
            threading.Thread(target=state.warm, name="advisor-warmup", daemon=True).start()
        yield

    app = FastAPI(
        title="Destination Advisor (module B)",
        description="Recommends a destination hospital for a Karnataka 108 ambulance. Demo: see banner.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.advisor = state
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])

    # ------------------------------------------------------------ read-only

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "registry_is_demo": state.is_demo, "travel_provider": state.travel_name}

    @app.get("/hospitals")
    def hospitals_() -> dict[str, Any]:
        return {
            "is_demo": state.is_demo,
            "banner": state.banner(state.registry_has_non_real()),
            "summary": state.summary.model_dump(mode="json"),
            "hospitals": [h.model_dump(mode="json") for h in state.hospitals],
        }

    @app.get("/config/params")
    def config_params() -> dict[str, Any]:
        return {"params": [
            {"key": k, "value": p.value, "provenance": p.provenance.value, "basis": p.basis,
             "needs_source": p.needs_source}
            for k, p in sorted(state.cfg.params.items())
        ]}

    @app.get("/sim/results")
    def sim_results() -> Response:
        """Pass-through of the SimPy replay output (written by `make advisor-sim`). Not validated here."""
        path = state.sim_results_path
        if not path.is_file():
            raise HTTPException(status_code=404, detail=SIM_RESULTS_MISSING)
        text = path.read_text(encoding="utf-8")
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail=f"{path.name} is not valid JSON: {e}") from None
        return Response(content=text, media_type="application/json")

    # ------------------------------------------------------------ recommend

    @app.post("/recommend")
    def recommend_(a: Assessment) -> dict[str, Any]:
        if a.complaint not in COMPLAINTS:
            raise HTTPException(status_code=422,
                                detail=f"unknown complaint '{a.complaint}'; expected one of: {', '.join(COMPLAINTS)}")
        a = a.model_copy(update={"when": to_local_naive(a.when)})
        travel = state.travel  # may load the road graph on first use
        with state.lock:
            ctx = AdvisorContext(hospitals=state.hospitals, travel=travel, cfg=state.cfg,
                                 statuses=state.statuses, events=state.events)
            rec = recommend(a, ctx)
            state.last_rec[a.case_id] = rec
            state.last_assessment[a.case_id] = a
        ids = [r.hospital_id for r in rec.ranked] + [x.hospital_id for x in rec.excluded]
        by_id = {h.hospital_id: h for h in state.hospitals}
        return {
            "recommendation": rec.model_dump(mode="json"),
            "is_demo_registry": state.is_demo,
            "banner": state.banner(rec.non_real_inputs_used),
            "travel_provider": state.travel_name,
            "hospitals": {i: {"name": by_id[i].name, "lat": by_id[i].lat, "lon": by_id[i].lon}
                          for i in ids if i in by_id},
            "todo_params": state.cfg.todo_params(),
        }

    # ------------------------------------------------------------ pre-alert (demo stub, SIMULATED outcomes)

    @app.post("/prealert")
    def prealert(body: PreAlertRequest) -> dict[str, Any]:
        """Start the pre-alert loop for the case's last recommendation. Demo stub: nothing is sent to any
        hospital; accept/reject answers come from the caller and are logged as SIMULATED OutcomeEvents."""
        now = to_local_naive(body.now)
        with state.lock:
            rec = state.last_rec.get(body.case_id)
            if rec is None:
                raise HTTPException(status_code=404,
                                    detail=f"no recommendation for case '{body.case_id}'; POST /recommend first")
            eta = _crew_choice_eta(state, rec, body.crew_choice, now)
            try:
                pa = state.prealerts.send(rec, now, crew_choice=body.crew_choice)
            except ValueError as e:  # nothing ranked to alert
                raise HTTPException(status_code=409, detail=str(e)) from None
            if eta is not None:  # crew chose a hospital the advisor did not rank: fill in its ETA
                pa.etas[body.crew_choice] = eta  # type: ignore[index]
                pa.attempts[0].eta_min = eta
            return pa.model_dump(mode="json")

    @app.post("/prealert/check_timeouts")
    def check_timeouts(body: TimeoutRequest | None = None) -> list[dict[str, Any]]:
        """Unanswered alerts past prealert.response_timeout_s count as rejections (SIMULATED) and fall back."""
        now = to_local_naive(body.now if body else None)
        with state.lock:
            return [pa.model_dump(mode="json") for pa in state.prealerts.check_timeouts(now)]

    @app.post("/prealert/{prealert_id}/response")
    def prealert_response(prealert_id: str, body: PreAlertResponseRequest) -> dict[str, Any]:
        """Record a hospital's (SIMULATED) answer. A reject moves on to the next-ranked hospital."""
        now = to_local_naive(body.now)
        with state.lock:
            pa: PreAlert | None = state.prealerts.alerts.get(prealert_id)
            if pa is None:
                raise HTTPException(status_code=404, detail=f"unknown pre-alert '{prealert_id}'")
            if pa.current is None:
                raise HTTPException(status_code=409,
                                    detail=f"pre-alert '{prealert_id}' is not pending (state: {pa.state.value})")
            pa = state.prealerts.respond(prealert_id, body.accepted, now, body.reason_code)
            return pa.model_dump(mode="json")

    return app


def _crew_choice_eta(state: AdvisorState, rec: Recommendation, choice: str | None, now: datetime) -> float | None:
    """ETA for a crew choice the advisor did not rank (PreAlertManager only knows ranked ETAs and would
    record NaN). None when no patch is needed; 422 if the id is not in the registry."""
    if choice is None or any(r.hospital_id == choice for r in rec.ranked):
        return None
    h = state.hospital_by_id(choice)
    if h is None:
        raise HTTPException(status_code=422, detail=f"crew_choice '{choice}' is not a hospital in the registry")
    ex = next((x for x in rec.excluded if x.hospital_id == choice and x.components is not None), None)
    if ex is not None and ex.components is not None:
        return ex.components.travel_min
    a = state.last_assessment[rec.case_id]  # excluded without a score (diversion, travel cap): compute it
    return state.travel.minutes(LatLon(lat=a.scene_lat, lon=a.scene_lon), LatLon(lat=h.lat, lon=h.lon), now).value


app = create_app()
