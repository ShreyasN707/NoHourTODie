"""In-memory server state for the advisor API. One instance per app; nothing is persisted.

Holds:
  - the active hospital registry (verified registry if it has rows, else the FICTIONAL demo registry),
  - the travel-time provider (built lazily: loading the OSM road graph takes ~1 s and ~0.5 GB),
  - ED statuses: empty by default. There is no live ED feed, so the engine uses its
    "unknown status -> assumed amber" fallback. We never invent live statuses here.
  - the last Recommendation per case_id (what /prealert sends),
  - the PreAlertManager and the shared OutcomeEvent list. Pre-alert outcomes recorded through the API
    are SIMULATED: this is a demo stub and nothing is sent to any hospital.

Concurrency: FastAPI runs sync endpoints in a thread pool. The OSM provider's Dijkstra cache and the
pre-alert manager are not thread-safe, so every endpoint that touches them holds `lock` (requests are
serialised; fine for a single-dispatcher demo).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from advisor.config import ROOT_DIR, AdvisorConfig, default_config
from advisor.graph.osm import OsmGraphTravelTime
from advisor.graph.travel import HaversineProfileTravelTime
from advisor.interfaces import TravelTimeProvider
from advisor.models import Assessment, Hospital, HospitalStatus, LatLon, OutcomeEvent, Recommendation
from advisor.prealert.fallback import PreAlertManager
from advisor.provenance import DEMO_BANNER, Provenance
from advisor.registry.loader import RegistrySummary, summarize
from advisor.scoring.reliability import OutcomeLog

log = logging.getLogger("advisor.api")

__all__ = ["DEMO_BANNER", "AdvisorState"]
DEFAULT_SIM_RESULTS = ROOT_DIR / "results" / "advisor_sim" / "sim_results.json"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

# Dijkstra results the OSM provider keeps beyond one per hospital (scene origins). Each cached origin
# is one float64 per graph node (~14 MB on the 1.7 M-node Bengaluru + Tumakuru graph).
SCENE_CACHE_HEADROOM = 16
NON_REAL = (Provenance.SIMULATED, Provenance.PLACEHOLDER)


def local_now() -> datetime:
    """Server clock as naive Bengaluru local time (the contract's datetime convention)."""
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


def to_local_naive(t: datetime | None) -> datetime:
    """None -> now; tz-aware -> converted to Bengaluru time and made naive; naive -> unchanged."""
    if t is None:
        return local_now()
    if t.tzinfo is not None:
        return t.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return t


def provider_name(travel: TravelTimeProvider) -> str:
    """Contract names: "osm_graph" | "haversine". Injected fakes report their class name."""
    if isinstance(travel, OsmGraphTravelTime):
        return "osm_graph"
    if isinstance(travel, HaversineProfileTravelTime):
        return "haversine"
    return type(travel).__name__


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() not in ("0", "false", "no", "")


class AdvisorState:
    """Everything the endpoints share. Expensive parts (registry, road graph) load on first use."""

    def __init__(
        self,
        cfg: AdvisorConfig | None = None,
        hospitals: list[Hospital] | None = None,
        travel: TravelTimeProvider | None = None,
        statuses: dict[str, HospitalStatus] | None = None,
        *,
        is_demo: bool | None = None,
        sim_results_path: Path | str | None = None,
        warmup: bool | None = None,
    ) -> None:
        self.cfg = cfg or default_config()
        self.lock = threading.RLock()
        self._hospitals = hospitals
        self._is_demo = is_demo if is_demo is not None else (False if hospitals is not None else None)
        self._summary: RegistrySummary | None = None
        self._travel = travel
        self._travel_factory: Callable[[], TravelTimeProvider] = self._build_travel
        # No live ED feed: empty -> engine falls back to "unknown -> assumed amber".
        self.statuses: dict[str, HospitalStatus] = statuses if statuses is not None else {}
        self.events: list[OutcomeEvent] = OutcomeLog(float(self.cfg.v("reliability.decay_half_life_days")))
        # Demo stub: outcomes are SIMULATED, nothing is sent anywhere.
        self.prealerts = PreAlertManager(self.cfg, events=self.events, outcome_provenance=Provenance.SIMULATED)
        self.last_rec: dict[str, Recommendation] = {}
        self.last_assessment: dict[str, Assessment] = {}
        self.sim_results_path = Path(sim_results_path) if sim_results_path else DEFAULT_SIM_RESULTS
        # Warm-up only makes sense for the real road graph; injected providers skip it by default.
        self.warmup = warmup if warmup is not None else (travel is None and _env_flag("ADVISOR_API_WARMUP", True))
        self.warm_status: str = "not_started"

    # ------------------------------------------------------------------ registry

    def _load_registry(self) -> None:
        if self._hospitals is not None and self._summary is not None:
            return
        with self.lock:
            if self._hospitals is None:
                from advisor.registry.demo import load_active_registry

                reg, demo = load_active_registry(self.cfg)
                self._hospitals, self._summary, self._is_demo = reg.hospitals, reg.summary, demo
            elif self._summary is None:
                self._summary = summarize(self._hospitals, "injected", "injected")

    @property
    def hospitals(self) -> list[Hospital]:
        self._load_registry()
        assert self._hospitals is not None
        return self._hospitals

    @property
    def summary(self) -> RegistrySummary:
        self._load_registry()
        assert self._summary is not None
        return self._summary

    @property
    def is_demo(self) -> bool:
        self._load_registry()
        return bool(self._is_demo)

    def hospital_by_id(self, hid: str) -> Hospital | None:
        return next((h for h in self.hospitals if h.hospital_id == hid), None)

    def registry_has_non_real(self) -> bool:
        return any(
            h.location_tag.provenance in NON_REAL or any(c.provenance in NON_REAL for c in h.capabilities.values())
            for h in self.hospitals
        )

    # ------------------------------------------------------------------ travel

    def _build_travel(self) -> TravelTimeProvider:
        """Same choice as advisor.graph.default_travel_provider, with the Dijkstra cache sized to hold
        every hospital origin (coverage round trips start at hospitals) plus recent scenes."""
        if OsmGraphTravelTime.available():
            size = len(self.hospitals) + SCENE_CACHE_HEADROOM
            return OsmGraphTravelTime(cfg=self.cfg, max_cached_sources=size)
        return HaversineProfileTravelTime(cfg=self.cfg)

    @property
    def travel(self) -> TravelTimeProvider:
        if self._travel is None:
            with self.lock:
                if self._travel is None:
                    t0 = time.perf_counter()
                    self._travel = self._travel_factory()
                    log.info("travel provider %s ready in %.1f s", provider_name(self._travel),
                             time.perf_counter() - t0)
        return self._travel

    @property
    def travel_name(self) -> str:
        """Provider name without forcing the road graph to load (cheap for /health)."""
        if self._travel is not None:
            return provider_name(self._travel)
        return "osm_graph" if OsmGraphTravelTime.available() else "haversine"

    def warm(self) -> None:
        """Load the provider and pre-compute shortest paths from every hospital, so the coverage term
        (hospital -> base return trip, computed for low-acuity patients) needs no Dijkstra at request
        time. Takes the lock per hospital so requests can interleave."""
        self.warm_status = "running"
        try:
            t0 = time.perf_counter()
            travel = self.travel
            if not isinstance(travel, OsmGraphTravelTime):
                self.warm_status = "skipped"
                return
            when = local_now()
            for h in self.hospitals:
                p = LatLon(lat=h.lat, lon=h.lon)
                with self.lock:
                    travel.minutes(p, p, when)
            self.warm_status = "done"
            log.info("warmed %d hospital origins in %.1f s", len(self.hospitals), time.perf_counter() - t0)
        except Exception:  # warm-up is an optimisation; requests still work (cold) if it fails
            self.warm_status = "failed"
            log.exception("advisor API warm-up failed")

    # ------------------------------------------------------------------ banner

    def banner(self, non_real_inputs_used: bool) -> str | None:
        """DEMO_BANNER whenever the fictional demo registry is in use. With a verified registry, a banner
        still appears if any SIMULATED/PLACEHOLDER input was used, without claiming the hospitals are fictional."""
        if self.is_demo:
            return DEMO_BANNER
        if non_real_inputs_used:
            return ("SIMULATED/PLACEHOLDER inputs in use (e.g. ED status, travel speeds). "
                    "Not live hospital data.")
        return None
