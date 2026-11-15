"""Pre-alert with accept/reject timer and automatic fallback (spec §4 steps 5-7).

In plain words: alert the top hospital with ETA and patient details. If it rejects or does not
answer within the timeout, move to the next-best hospital. Every answer is logged as an
OutcomeEvent with a reason code, which feeds the hospital's reliability score.

Demo stub: nothing is sent anywhere. In the target state this goes out through the 108 command
centre's existing advance-alert channel (spec §3). Outcomes logged here are SIMULATED by default.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from advisor.config import AdvisorConfig
from advisor.models import OutcomeEvent, Recommendation
from advisor.provenance import Provenance

# Karnataka circular (Sep 2025): hospitals must treat accident victims without advance payment and
# stabilise before transfer. Refusals of trauma patients are flagged against it (spec §4 guardrails).
ACCIDENT_REFUSAL_FLAG = "REFUSAL_ACCIDENT_VICTIM_KA_CIRCULAR_SEP2025"
ACCIDENT_CATEGORIES = ("severe_trauma",)


class PreAlertState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXHAUSTED = "exhausted"  # every ranked hospital rejected or timed out


class Attempt(BaseModel):
    hospital_id: str
    sent_at: datetime
    eta_min: float | None  # None when the crew picked a hospital the advisor did not score
    answered_at: datetime | None = None
    accepted: bool | None = None
    reason_code: str | None = None


class PreAlert(BaseModel):
    prealert_id: str
    case_id: str
    care_category: str
    queue: list[str]  # hospital ids in the order they will be tried
    etas: dict[str, float | None]
    attempts: list[Attempt] = Field(default_factory=list)
    state: PreAlertState = PreAlertState.PENDING
    crew_override: bool = False

    @property
    def current(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts and self.state == PreAlertState.PENDING else None


class PreAlertManager:
    """In-memory pre-alert loop. `events` is the shared accept/reject log used by reliability scoring."""

    def __init__(self, cfg: AdvisorConfig, events: list[OutcomeEvent] | None = None,
                 outcome_provenance: Provenance = Provenance.SIMULATED):
        self.cfg = cfg
        self.events = events if events is not None else []
        self.outcome_provenance = outcome_provenance
        self.alerts: dict[str, PreAlert] = {}
        self._ids = itertools.count(1)

    @property
    def timeout(self) -> timedelta:
        return timedelta(seconds=self.cfg.v("prealert.response_timeout_s"))

    def send(self, rec: Recommendation, now: datetime, crew_choice: str | None = None) -> PreAlert:
        """Alert the first hospital. The advisor suggests, the crew confirms: `crew_choice` goes first."""
        queue = [r.hospital_id for r in rec.ranked]
        if not queue:
            raise ValueError(f"case {rec.case_id}: no hospital to pre-alert ({rec.reason_code})")
        override = crew_choice is not None and crew_choice != queue[0]
        if crew_choice is not None:
            queue = [crew_choice] + [h for h in queue if h != crew_choice]
        etas = {r.hospital_id: r.components.travel_min for r in rec.ranked}
        pa = PreAlert(prealert_id=f"PA{next(self._ids):05d}", case_id=rec.case_id,
                      care_category=rec.care_category, queue=queue, etas=etas, crew_override=override)
        self.alerts[pa.prealert_id] = pa
        self._attempt_next(pa, now)
        return pa

    def respond(self, prealert_id: str, accepted: bool, now: datetime, reason_code: str) -> PreAlert:
        """Hospital's answer. A reject logs the reason and moves on to the next hospital."""
        pa = self.alerts[prealert_id]
        att = pa.current
        if att is None:
            raise ValueError(f"{prealert_id} is not pending (state: {pa.state})")
        self._close(pa, att, accepted, now, reason_code)
        if accepted:
            pa.state = PreAlertState.ACCEPTED
        else:
            self._attempt_next(pa, now)
        return pa

    def check_timeouts(self, now: datetime) -> list[PreAlert]:
        """Treat unanswered alerts past the timer as rejections and fall back. Returns those advanced."""
        advanced = []
        for pa in self.alerts.values():
            att = pa.current
            if att is not None and now - att.sent_at >= self.timeout:
                self._close(pa, att, False, att.sent_at + self.timeout, "TIMEOUT")
                self._attempt_next(pa, att.sent_at + self.timeout)
                advanced.append(pa)
        return advanced

    def _attempt_next(self, pa: PreAlert, now: datetime) -> None:
        tried = {a.hospital_id for a in pa.attempts}
        nxt = next((h for h in pa.queue if h not in tried), None)
        if nxt is None:
            pa.state = PreAlertState.EXHAUSTED
            return
        pa.attempts.append(Attempt(hospital_id=nxt, sent_at=now, eta_min=pa.etas.get(nxt)))

    def _close(self, pa: PreAlert, att: Attempt, accepted: bool, now: datetime, reason_code: str) -> None:
        if not accepted and pa.care_category in ACCIDENT_CATEGORIES:
            reason_code = f"{reason_code}|{ACCIDENT_REFUSAL_FLAG}"
        att.answered_at, att.accepted, att.reason_code = now, accepted, reason_code
        self.events.append(OutcomeEvent(hospital_id=att.hospital_id, timestamp=now, accepted=accepted,
                                        reason_code=reason_code, source=self.outcome_provenance))
