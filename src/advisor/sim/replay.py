"""SimPy replay: today's nearest-hospital routing vs the Destination Advisor, on the same incidents.

PROVENANCE: every output of this module is a SIMULATION RESULT. Incidents are GoldenHour's synthetic calls,
hospitals are the fictional demo registry, ED crowding, refusals and patient details are simulated, and
travel speeds are placeholders. Nothing here is a field measurement.

Both policies see identical incidents and identical random draws (common random numbers): the same patient
facts, the same travel noise for the same leg, the same refusal draw for the same hospital, the same
background ED arrivals. So a difference between them comes from the routing, not from luck.

Policies
--------
nearest   Today's practice. Drive to the nearest hospital with a 24x7 ED by expected travel time, no
          pre-alert. On arrival the hospital may refuse (more likely when crowded) or lack what the patient
          needs; the crew then moves on to the nearest hospital that has it (a secondary transfer).
advisor   Call `recommend()`, pre-alert the ranked list with the PreAlertManager. Each simulated hospital
          accepts or rejects based on its TRUE crowding at that moment; unanswered alerts time out after
          prealert.response_timeout_s while the ambulance waits at the scene. Drive to the hospital that
          accepted. Every accept/reject is logged as an OutcomeEvent (SIMULATED) that reliability scoring
          learns from, and every gate dwell feeds a RollingDwellIndex tagged SIMULATED.
nearest_prealert  Reference only (not a proposal): pre-alert the 24x7 EDs in order of expected travel with the
          same simulated answers, no advisor triage or ranking. Shows how much of the advisor's gain is just
          from pre-alerting.

Measured
--------
time to treatment   from the moment the crew is ready to leave the scene (before any pre-alert wait) until
                    the patient gets an ED bay at a hospital that can treat their TRUE care category
                    (patients.can_treat). If that never happens, the patient has no definitive care.
ambulance minutes   from the same moment until the ambulance is back at base (nearest standby site).

Simplifications (also written into the results file)
-------------------------------------------------------
* No fleet: each incident has its own ambulance; dispatch and response are module A's job.
* An accepted pre-alert is honoured on arrival, except for the swept share
  sim.accepted_prealert_refused_on_arrival_share (0 in the base case).
* If every pre-alerted hospital rejects, the crew drives unannounced to the advisor's top choice.
* Hospitals answer pre-alerts instantly or not at all (timeout).
* ED offload queue is first-come first-served for everyone (no triage priority at the gate).
* After sim.max_refusals_per_patient refusals, the next capable hospital must take the patient.
* STEMI counts as treated at a cath lab OR a thrombolysis centre; onward PCI transfer is not modelled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import simpy

from advisor.config import AdvisorConfig
from advisor.models import (
    CAPABILITY_KEYS,
    Assessment,
    Capability,
    DwellRecord,
    Hospital,
    OutcomeEvent,
    RankedHospital,
    Recommendation,
)
from advisor.passive.dwell import RollingDwellIndex
from advisor.prealert.fallback import PreAlertManager, PreAlertState
from advisor.provenance import Provenance, Tagged
from advisor.rules.engine import AdvisorContext, recommend
from advisor.scoring.reliability import OutcomeLog
from advisor.scoring.score import score
from advisor.sim.hospitals_state import HospitalED, StatusBoard, background_arrivals
from advisor.sim.incidents import Incident
from advisor.sim.patients import N_PATIENT_DRAWS, Patient, can_treat, sample_patient
from advisor.sim.travel_matrix import Point

POLICIES = ("nearest", "nearest_prealert", "advisor")
DWELL_TAG = Tagged(provenance=Provenance.SIMULATED,
                   source="simulated ambulance gate dwell (SimPy replay, advisor world). Not 108 GPS logs.")
DWELL_REBUILD_MIN = 60.0  # how often the rolling dwell index is rebuilt from new records (sim minutes)

# Named random streams, so adding a draw in one place never shifts another.
_STREAM_CASES, _STREAM_BOARD, _STREAM_REGISTRY, _STREAM_BACKGROUND = 1, 2, 3, 1000


@dataclass
class Draws:
    """Per-incident random numbers, shared by both policies (common random numbers).

    Indexed [incident, hospital] where the draw belongs to a hospital, so the same hospital meets the same
    patient with the same luck under either policy.
    """

    patient_u: np.ndarray  # (n, N_PATIENT_DRAWS) uniforms -> patient facts
    noise_scene: np.ndarray  # (n, H) standard normals: scene -> hospital drive
    noise_leg: np.ndarray  # (n, H) standard normals: k-th hospital -> hospital move
    noise_return: np.ndarray  # (n,) standard normals: drive back to base
    u_reject: np.ndarray  # (n, H) uniforms: does this hospital refuse (pre-alert or on arrival)
    u_noanswer: np.ndarray  # (n, H) uniforms: does this hospital leave the pre-alert unanswered
    u_arrival: np.ndarray  # (n, H) uniforms: is an accepted pre-alert dishonoured on arrival
    ed_stay: np.ndarray  # (n,) unit exponentials, x sim.ed_stay_mean_min

    @classmethod
    def make(cls, n: int, n_hosp: int, seed: int) -> Draws:
        rng = np.random.default_rng([seed, _STREAM_CASES])
        return cls(
            patient_u=rng.random((n, N_PATIENT_DRAWS)), noise_scene=rng.standard_normal((n, n_hosp)),
            noise_leg=rng.standard_normal((n, n_hosp)), noise_return=rng.standard_normal(n),
            u_reject=rng.random((n, n_hosp)), u_noanswer=rng.random((n, n_hosp)),
            u_arrival=rng.random((n, n_hosp)), ed_stay=rng.exponential(1.0, n),
        )

    def subset(self, idx: np.ndarray) -> Draws:
        return Draws(**{k: v[idx] for k, v in self.__dict__.items()})


@dataclass
class World:
    """Everything that is the same for both policies in one scenario."""

    incidents: list[Incident]
    bases: list[Point]  # per incident: nearest standby site to the scene
    hospitals: list[Hospital]
    travel: object  # TravelTimeProvider with expected_min(origin, dest, depart) (sim.travel_matrix.SimTravel)
    cfg: AdvisorConfig
    draws: Draws
    origin: datetime  # simulation clock zero (window start minus warm-up)
    seed: int
    # What the advisor's registry SAYS. Defaults to the truth; registry_view() adds errors (sim.registry_error_share).
    advisor_hospitals: list[Hospital] | None = None

    @property
    def registry(self) -> list[Hospital]:
        return self.advisor_hospitals if self.advisor_hospitals is not None else self.hospitals

    def patients(self) -> list[Patient]:
        return [sample_patient(inc.complaint, list(self.draws.patient_u[i]), self.cfg)
                for i, inc in enumerate(self.incidents)]


@dataclass
class CaseResult:
    """What happened to one patient under one policy."""

    incident_id: str
    true_category: str
    advisor_category: str | None = None
    ttt_min: float | None = None  # None = never reached definitive care
    offload_min: float = 0.0  # wait at the final hospital's gate for an ED bay
    move_ons: int = 0  # secondary transfers (refused or not capable, moved on)
    first_capable_treated: bool = False
    ambulance_min: float = 0.0
    prealert_rejects: int = 0
    prealert_timeouts: int = 0
    fallbacks: int = 0
    hospitals_visited: list[str] = field(default_factory=list)


class _Replay:
    """One policy's run over one World. Holds the SimPy environment and the advisor's learning state."""

    def __init__(self, world: World, policy: str):
        if policy not in POLICIES:
            raise ValueError(f"unknown policy '{policy}'")
        self.w, self.policy, self.cfg = world, policy, world.cfg
        cfg = world.cfg
        self.env = simpy.Environment()
        self.eds = [HospitalED(self.env, h, cfg) for h in world.hospitals]
        self.hpt: list[Point] = [(h.lat, h.lon) for h in world.hospitals]
        self.hidx = {h.hospital_id: j for j, h in enumerate(world.hospitals)}
        self.has_ed = [h.has("ed_24x7") for h in world.hospitals]
        self.patients = world.patients()
        self.results = [CaseResult(inc.incident_id, p.true_category)
                        for inc, p in zip(world.incidents, self.patients, strict=True)]
        to_ready = float(cfg.v("sim.call_to_scene_min")) + float(cfg.v("sim.on_scene_min"))
        self.ready_min = [self._mins(inc.when) + to_ready
                          for inc in world.incidents]
        self.end_min = (max(self.ready_min) if self.ready_min else 0.0) + 12 * 60
        self.sigma = float(cfg.v("sim.travel_noise_sigma"))
        # advisor learning state
        # running decayed counts per hospital: keeps reliability lookups O(1) (see OutcomeLog)
        self.events = OutcomeLog(float(cfg.v("reliability.decay_half_life_days")))
        self.dwell_records: list[DwellRecord] = []
        self._dwell_index: RollingDwellIndex | None = None
        self._dwell_built_at, self._dwell_built_n = -math.inf, -1
        board_rng = np.random.default_rng([world.seed, _STREAM_BOARD])
        self.board = StatusBoard(self.env, self.eds, world.origin, cfg, board_rng)

    # ---------------------------------------------------------------- helpers

    def _mins(self, t: datetime) -> float:
        return (t - self.w.origin).total_seconds() / 60.0

    def _dt(self, m: float | None = None) -> datetime:
        return self.w.origin + timedelta(minutes=self.env.now if m is None else m)

    def _noisy(self, expected: float, z: float) -> float:
        """Actual minutes = expected x lognormal noise with mean 1 (the advisor never sees z)."""
        return expected * math.exp(self.sigma * z - 0.5 * self.sigma ** 2)

    def _nearest(self, origin: Point, candidates: list[int]) -> int | None:
        now = self._dt()
        best = min(((self.w.travel.expected_min(origin, self.hpt[j], now), j) for j in candidates), default=None)
        return best[1] if best else None

    def _dwell(self) -> RollingDwellIndex | None:
        if not self.dwell_records:
            return None
        if self.env.now - self._dwell_built_at >= DWELL_REBUILD_MIN and len(self.dwell_records) != self._dwell_built_n:
            self._dwell_index = RollingDwellIndex(self.dwell_records, DWELL_TAG, self.cfg)
            self._dwell_built_at, self._dwell_built_n = self.env.now, len(self.dwell_records)
        return self._dwell_index

    def _log(self, j: int, accepted: bool, reason: str) -> None:
        if self.policy == "advisor":
            self.events.append(OutcomeEvent(hospital_id=self.w.hospitals[j].hospital_id, timestamp=self._dt(),
                                            accepted=accepted, reason_code=reason, source=Provenance.SIMULATED))

    # ---------------------------------------------------------------- choosing the first hospital

    def _choose_advisor(self, i: int, res: CaseResult):
        """Recommend, pre-alert down the ranked list, return (hospital index, announced?)."""
        cfg, inc, p = self.cfg, self.w.incidents[i], self.patients[i]
        now = self._dt()
        base = self.w.bases[i]
        a = Assessment(case_id=inc.incident_id, scene_lat=inc.lat, scene_lon=inc.lon, base_lat=base[0],
                       base_lon=base[1], complaint=p.complaint, vitals=p.vitals, ecg_stemi=p.ecg_stemi,
                       stroke_score=p.stroke_score, minutes_since_first_contact=float(cfg.v("sim.on_scene_min")),
                       when=now)
        ctx = AdvisorContext(hospitals=self.w.registry, travel=self.w.travel, cfg=cfg,
                             statuses=dict(self.board.statuses), events=self.events, dwell=self._dwell())
        rec = recommend(a, ctx)
        res.advisor_category = rec.care_category
        if not rec.ranked:
            return self._nearest((inc.lat, inc.lon), [j for j in range(len(self.eds)) if self.has_ed[j]]), False
        return (yield from self._prealert(i, res, rec, self.events))

    def _choose_nearest_prealert(self, i: int, res: CaseResult):
        """Reference policy: pre-alert 24x7 EDs in order of expected travel, no advisor ranking or triage.

        Separates the gain from pre-alerting itself from the gain from the advisor's ranking. Its accept/reject
        answers are not fed to anyone (nothing learns from them).
        """
        inc = self.w.incidents[i]
        now = self._dt()
        order = sorted((self.w.travel.expected_min((inc.lat, inc.lon), self.hpt[j], now), j)
                       for j in range(len(self.eds)) if self.has_ed[j])
        ranked = [RankedHospital(hospital_id=self.w.hospitals[j].hospital_id, name=self.w.hospitals[j].name,
                                 score=t, components=score(t, 0.0, 0.0, 0.0, 0.0, 0.0), capable=True)
                  for t, j in order]
        rec = Recommendation(case_id=inc.incident_id, care_category=self.patients[i].complaint, ranked=ranked,
                             reason_code="SIM_NEAREST_PREALERT", reason="reference policy: nearest 24x7 EDs in order",
                             lambda_used=0.0, inputs_provenance={}, non_real_inputs_used=True)
        return (yield from self._prealert(i, res, rec, []))

    def _prealert(self, i: int, res: CaseResult, rec: Recommendation, events: list[OutcomeEvent]):
        """Pre-alert down `rec.ranked` with the PreAlertManager; return (hospital index, announced?).

        Each simulated hospital answers from its TRUE crowding now (same refusal draw as on arrival), or does
        not answer (timeout, the ambulance waits at the scene). Answers are logged into `events`.
        """
        cfg = self.cfg
        mgr = PreAlertManager(cfg, events=events, outcome_provenance=Provenance.SIMULATED)
        pa = mgr.send(rec, self._dt())
        no_answer = float(cfg.v("sim.prealert_no_answer_share"))
        while pa.state == PreAlertState.PENDING:
            att = pa.current
            j = self.hidx[att.hospital_id]
            if self.w.draws.u_noanswer[i, j] < no_answer:
                res.prealert_timeouts += 1
                yield self.env.timeout(mgr.timeout.total_seconds() / 60.0)
                mgr.check_timeouts(att.sent_at + mgr.timeout)
            else:
                reject = bool(self.w.draws.u_reject[i, j] < self.eds[j].reject_prob())
                res.prealert_rejects += reject
                mgr.respond(pa.prealert_id, not reject, att.sent_at, "SIM_ED_CROWDING" if reject else "SIM_ACCEPT")
        res.fallbacks = len(pa.attempts) - 1
        if pa.state == PreAlertState.ACCEPTED:
            return self.hidx[pa.attempts[-1].hospital_id], True
        return self.hidx[rec.ranked[0].hospital_id], False  # everyone said no: go unannounced to the top choice

    # ---------------------------------------------------------------- one case

    def case(self, i: int):
        """SimPy process for one incident."""
        cfg, d = self.cfg, self.w.draws
        res, p = self.results[i], self.patients[i]
        inc = self.w.incidents[i]
        yield self.env.timeout(max(0.0, self.ready_min[i] - self.env.now))
        t_ready = self.env.now
        scene: Point = (inc.lat, inc.lon)
        if self.policy == "nearest":
            cur, announced = self._nearest(scene, [j for j in range(len(self.eds)) if self.has_ed[j]]), False
        elif self.policy == "nearest_prealert":
            cur, announced = yield from self._choose_nearest_prealert(i, res)
        else:
            cur, announced = yield from self._choose_advisor(i, res)

        gate = float(cfg.v("sim.refusal_gate_min"))
        max_ref = int(cfg.v("sim.max_refusals_per_patient"))
        dishonour = float(cfg.v("sim.accepted_prealert_refused_on_arrival_share"))
        pos, leg, refusals = scene, 0, 0
        tried: list[int] = []
        capable_all = [j for j, h in enumerate(self.w.hospitals) if can_treat(h, p.true_category, cfg)]
        while True:
            expected = self.w.travel.expected_min(pos, self.hpt[cur], self._dt())
            first_leg = pos == scene and not tried
            z = d.noise_scene[i, cur] if first_leg else d.noise_leg[i, min(leg, d.noise_leg.shape[1] - 1)]
            yield self.env.timeout(self._noisy(expected, float(z)))
            tried.append(cur)
            res.hospitals_visited.append(self.w.hospitals[cur].hospital_id)
            arrived = self.env.now
            ed = self.eds[cur]
            capable = cur in capable_all
            refuse = False
            if capable and refusals < max_ref:
                if announced:
                    refuse = bool(d.u_arrival[i, cur] < dishonour)
                else:
                    refuse = bool(d.u_reject[i, cur] < ed.reject_prob())
            alternatives = [j for j in capable_all if j not in tried]
            if (refuse or not capable) and alternatives:
                if refuse:
                    refusals += 1
                    self._log(cur, False, "SIM_REFUSED_ON_ARRIVAL")
                yield self.env.timeout(gate)
                self._gate(cur, arrived)
                nxt = self._nearest(self.hpt[cur], alternatives)
                res.move_ons += 1
                pos, cur, announced = self.hpt[cur], nxt, False
                leg += 1
                continue
            break

        # admitted: wait for a bay (offload delay), hand over, drive back to base
        if capable and not announced:
            self._log(cur, True, "SIM_ACCEPTED_ON_ARRIVAL")
        req = ed.res.request()
        t_gate = self.env.now
        yield req
        res.offload_min = self.env.now - t_gate
        if capable:
            res.ttt_min = self.env.now - t_ready
        res.first_capable_treated = capable and len(tried) == 1
        self.env.process(ed.stay(req, float(d.ed_stay[i]) * float(cfg.v("sim.ed_stay_mean_min"))))
        yield self.env.timeout(float(cfg.v("sim.handover_min")))
        self._gate(cur, arrived)
        back = self.w.travel.expected_min(self.hpt[cur], self.w.bases[i], self._dt())
        res.ambulance_min = self.env.now + self._noisy(back, float(d.noise_return[i])) - t_ready

    def _gate(self, j: int, arrived: float) -> None:
        """Record an ambulance gate dwell (what 108 GPS would see) for the advisor's passive signal."""
        if self.policy == "advisor" and self.env.now > arrived:
            self.dwell_records.append(DwellRecord(hospital_id=self.w.hospitals[j].hospital_id,
                                                  arrived=self._dt(arrived), departed=self._dt()))

    # ---------------------------------------------------------------- run

    def run(self) -> list[CaseResult]:
        cfg = self.cfg
        for j, ed in enumerate(self.eds):
            rng = np.random.default_rng([self.w.seed, _STREAM_BACKGROUND + j])
            times, stays = background_arrivals(rng, ed.size, 0.0, self.end_min, self.w.origin, cfg)
            self.env.process(ed.background(times, stays))
        self.env.process(self.board.run(self.end_min))
        for i in range(len(self.w.incidents)):
            self.env.process(self.case(i))
        self.env.run()
        return self.results


def registry_view(hospitals: list[Hospital], error_share: float, seed: int) -> list[Hospital]:
    """The advisor's registry with a share of capability records wrong (spec §5: registry data goes stale/wrong).

    Each (hospital, capability) record except ed_24x7 is flipped with probability `error_share`: a capability the
    hospital has is recorded as absent, or one it lacks is recorded as present (the dangerous case: the advisor
    sends a patient to a hospital that cannot treat them). Ground truth for outcomes stays `hospitals`.
    """
    if error_share <= 0:
        return hospitals
    rng = np.random.default_rng([seed, _STREAM_REGISTRY])
    out = []
    for h in hospitals:
        caps = dict(h.capabilities)
        for key in CAPABILITY_KEYS:
            if key == "ed_24x7" or rng.random() >= error_share:
                continue
            if h.has(key):
                caps.pop(key)
            else:
                caps[key] = Capability(available=True, hours="24x7", provenance=Provenance.SIMULATED,
                                       source="simulated registry error (claimed capability the hospital lacks)")
        out.append(h.model_copy(update={"capabilities": caps}))
    return out


def run_policy(world: World, policy: str) -> list[CaseResult]:
    """Replay every incident in `world` under one policy. Deterministic for a given world."""
    return _Replay(world, policy).run()


# ---------------------------------------------------------------- metrics


def _stat(values: list[float]) -> dict:
    """{n, mean, p50, p90}; statistics are null (not NaN) when there are no values."""
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p90": None}
    a = np.asarray(values, dtype=float)
    return {"n": len(values), "mean": round(float(a.mean()), 3), "p50": round(float(np.percentile(a, 50)), 3),
            "p90": round(float(np.percentile(a, 90)), 3)}


def metrics(cases: list[CaseResult], categories: list[str], critical: list[str]) -> dict:
    """Contract `Metrics` for one policy (docs/advisor_contract.md), plus a few extra counts.

    Extra keys beyond the contract: time_to_treatment.critical, offload_wait (minutes at the final gate),
    prealert_timeouts, no_definitive_care, advisor_category_differs_from_truth.
    """
    n = len(cases)
    crit = [c for c in cases if c.true_category in critical]
    ttt = [c.ttt_min for c in cases if c.ttt_min is not None]
    moved = sum(1 for c in cases if c.move_ons > 0)
    return {
        "n": n,
        "time_to_treatment": {
            "overall": _stat(ttt),
            "critical": _stat([c.ttt_min for c in crit if c.ttt_min is not None]),
            "by_category": {cat: _stat([c.ttt_min for c in cases if c.true_category == cat and c.ttt_min is not None])
                            for cat in categories},
        },
        "offload_wait": _stat([c.offload_min for c in cases]),
        "secondary_transfers": moved,
        "secondary_transfer_rate": round(moved / n, 5) if n else None,
        "ambulance_hours": round(sum(c.ambulance_min for c in cases) / 60.0, 3),
        "critical_capable_first_attempt_share": (
            round(sum(c.first_capable_treated for c in crit) / len(crit), 5) if crit else None),
        "prealert_rejects": sum(c.prealert_rejects for c in cases),
        "fallbacks": sum(c.fallbacks for c in cases),
        "prealert_timeouts": sum(c.prealert_timeouts for c in cases),
        "no_definitive_care": sum(1 for c in cases if c.ttt_min is None),
        "advisor_category_differs_from_truth": sum(
            1 for c in cases if c.advisor_category is not None and c.advisor_category != c.true_category),
    }
