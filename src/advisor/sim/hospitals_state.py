"""Simulated emergency departments: offload queues, true ED status, refusals, and what hospitals report.

PROVENANCE: SIMULATED. No hospital in Bengaluru publishes its ED queue; every number here comes from
config `sim.*` (mostly "TODO: source") and is swept in the sensitivity analysis.

The model, in plain words:
  * Each hospital's ED is a SimPy Resource with a number of bays (by size class). A patient holds a
    bay for an exponential stay. 108 ambulances are only part of the load: background patients
    (walk-ins, private vehicles, referrals) arrive as a Poisson stream with an hour-of-day profile.
  * An ambulance patient waits at the gate until a bay is free: that wait is the offload delay.
  * True ED status comes from the queue: green = a bay is free; amber = all bays busy, short queue;
    red = long queue; diversion = very long queue.
  * The chance a hospital refuses a patient rises with its TRUE status.
  * What the advisor sees is different: each hospital REPORTS its status every N minutes. Some
    hospitals report late (stale), and some always say green (misreport), as spec §5 warns.

Size class is read from capabilities so the same rule works for a verified registry later:
cath lab (pci_24x7) -> tertiary, CT -> secondary, otherwise primary.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import simpy

from advisor.config import AdvisorConfig
from advisor.models import EDStatus, Hospital, HospitalStatus
from advisor.provenance import Provenance

STATUS_SOURCE = "simulated ED status report (SimPy replay; hospital self-report model, sim.status_*)"


def size_class(h: Hospital) -> str:
    """tertiary / secondary / primary, from capabilities (see module docstring)."""
    if h.has("pci_24x7"):
        return "tertiary"
    if h.has("ct_24x7"):
        return "secondary"
    return "primary"


def background_arrivals(rng: np.random.Generator, size: str, start_min: float, end_min: float,
                        origin: datetime, cfg: AdvisorConfig) -> tuple[np.ndarray, np.ndarray]:
    """Non-108 arrival times (minutes since origin) and their bay stays, by Poisson thinning.

    Rate at time t = background_arrivals_per_hour_by_class[size] x background_load_scale
                     x hourly_profile[hour of t] / mean(hourly_profile).
    """
    base = float(cfg.v("sim.background_arrivals_per_hour_by_class")[size]) * float(cfg.v("sim.background_load_scale"))
    prof = np.asarray(cfg.v("sim.background_hourly_profile"), dtype=float)
    if len(prof) != 24 or (prof < 0).any() or prof.sum() <= 0:
        raise ValueError("sim.background_hourly_profile needs 24 non-negative values")
    prof = prof / prof.mean()
    rate_max = base * prof.max() / 60.0  # per minute
    if rate_max <= 0:
        return np.empty(0), np.empty(0)
    n_max = int((end_min - start_min) * rate_max * 1.5 + 50)
    gaps = rng.exponential(1.0 / rate_max, size=n_max)
    keep_u = rng.random(n_max)
    stays = rng.exponential(float(cfg.v("sim.ed_stay_mean_min")), size=n_max)
    times = start_min + np.cumsum(gaps)
    inside = times < end_min
    times, keep_u, stays = times[inside], keep_u[inside], stays[inside]
    hours = np.array([(origin + timedelta(minutes=float(t))).hour for t in times], dtype=int)
    accept = keep_u < prof[hours] / prof.max()
    return times[accept], stays[accept]


class HospitalED:
    """One hospital's ED: bays, background load, true status and refusal probability."""

    def __init__(self, env: simpy.Environment, hospital: Hospital, cfg: AdvisorConfig):
        self.env = env
        self.hospital = hospital
        self.cfg = cfg
        self.size = size_class(hospital)
        self.bays = int(cfg.v("sim.ed_bays_by_class")[self.size])
        self.res = simpy.Resource(env, capacity=self.bays)
        thresholds = cfg.v("sim.ed_status_queue_per_bay")
        self._red_q = float(thresholds["red"]) * self.bays
        self._div_q = float(thresholds["diversion"]) * self.bays

    def status(self) -> str:
        """True status now: green / amber / red / diversion."""
        if self.res.count < self.bays:
            return "green"
        q = len(self.res.queue)
        if q < self._red_q:
            return "amber"
        if q < self._div_q:
            return "red"
        return "diversion"

    def reject_prob(self) -> float:
        """Chance this hospital refuses a patient right now (rises with true crowding)."""
        mult = float(self.cfg.v("sim.reject_status_multiplier")[self.status()])
        return min(float(self.cfg.v("sim.reject_base_rate")) * mult, float(self.cfg.v("sim.reject_max_prob")))

    def stay(self, req, minutes: float):
        """Hold a granted bay for `minutes`, then free it."""
        yield self.env.timeout(minutes)
        self.res.release(req)

    def _visit(self, minutes: float):
        req = self.res.request()
        yield req
        yield from self.stay(req, minutes)

    def background(self, times: np.ndarray, stays: np.ndarray):
        """SimPy process: feed pre-sampled background patients into the ED."""
        for t, s in zip(times, stays, strict=True):
            if t > self.env.now:
                yield self.env.timeout(float(t) - self.env.now)
            self.env.process(self._visit(float(s)))


class StatusBoard:
    """What the advisor can see: the latest REPORTED status per hospital (SIMULATED).

    Reporter kinds, fixed for the whole run and drawn with the seed:
      honest    reports true status every sim.status_report_interval_min
      stale     reports true status only every sim.status_stale_interval_min
      misreport always reports green and no diversion
    """

    def __init__(self, env: simpy.Environment, eds: list[HospitalED], origin: datetime, cfg: AdvisorConfig,
                 rng: np.random.Generator):
        self.env, self.eds, self.origin, self.cfg = env, eds, origin, cfg
        n = len(eds)
        order = rng.permutation(n)
        n_stale = round(float(cfg.v("sim.status_stale_share")) * n)
        n_mis = min(n - n_stale, round(float(cfg.v("sim.status_misreport_share")) * n))
        self.kind = ["honest"] * n
        for j in order[:n_stale]:
            self.kind[int(j)] = "stale"
        for j in order[n_stale:n_stale + n_mis]:
            self.kind[int(j)] = "misreport"
        self.statuses: dict[str, HospitalStatus] = {}

    def _report(self, j: int) -> None:
        ed = self.eds[j]
        true = ed.status()
        if self.kind[j] == "misreport":
            shown, diversion = EDStatus.GREEN, False
        else:
            shown = EDStatus.RED if true == "diversion" else EDStatus(true)
            diversion = true == "diversion"
        self.statuses[ed.hospital.hospital_id] = HospitalStatus(
            hospital_id=ed.hospital.hospital_id, timestamp=self.origin + timedelta(minutes=self.env.now),
            ed_status=shown, diversion=diversion, provenance=Provenance.SIMULATED, source=STATUS_SOURCE,
        )

    def run(self, end_min: float):
        """SimPy process: report on schedule until `end_min`."""
        every = float(self.cfg.v("sim.status_report_interval_min"))
        stale_every = float(self.cfg.v("sim.status_stale_interval_min"))
        k = 0
        while self.env.now <= end_min:
            t = k * every
            for j in range(len(self.eds)):
                if self.kind[j] != "stale" or (t % stale_every) < every:
                    self._report(j)
            k += 1
            yield self.env.timeout(every)
