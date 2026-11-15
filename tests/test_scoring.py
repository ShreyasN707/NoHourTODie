"""Spec §4 worked examples and scoring building blocks. All numbers are spec placeholders."""

from datetime import datetime, timedelta

import pytest

from advisor.models import EDStatus, HospitalStatus, OutcomeEvent
from advisor.provenance import Provenance, Sourced, Tagged
from advisor.scoring.ed_wait import expected_ed_wait
from advisor.scoring.reliability import decay_weight, decayed_counts, p_cannot_treat
from advisor.scoring.score import coverage_lost, score

NOW = datetime(2026, 11, 16, 11, 0)


def test_worked_example_stemi():
    a = score(travel_min=8, ed_wait_min=10, p_cannot_treat=1.0, transfer_delay_min=50, lam=0, coverage_lost_min=0)
    b = score(travel_min=18, ed_wait_min=15, p_cannot_treat=0.1, transfer_delay_min=50, lam=0, coverage_lost_min=0)
    assert a.total == pytest.approx(68)
    assert b.total == pytest.approx(38)
    assert b.total < a.total


def test_worked_example_low_acuity():
    c = score(6, 45, 0.0, 50, 0.5, 12)
    d = score(14, 10, 0.0, 50, 0.5, 28)
    assert c.total == pytest.approx(57)
    assert d.total == pytest.approx(38)


def test_lambda_zero_ignores_coverage():
    assert score(10, 5, 0.0, 50, 0.0, 999).total == 15


def test_score_rejects_bad_inputs():
    with pytest.raises(ValueError):
        score(1, 1, 1.5, 50, 0, 0)
    with pytest.raises(ValueError):
        score(1, 1, 0.5, 50, -1, 0)


def test_coverage_lost_signed():
    assert coverage_lost(20, 15, 10, 10) == 15
    assert coverage_lost(12, 3, 10, 10) == -5  # on the way back to base: a real saving


def test_p_cannot_treat_prior_matches_worked_example(cfg):
    p = p_cannot_treat(True, 0, 0, cfg.v("reliability.prior_rejects"), cfg.v("reliability.prior_total"))
    assert p == pytest.approx(0.1)
    assert p_cannot_treat(False, 0, 0, 1, 10) == 1.0


def test_decay_halves_old_events():
    assert decay_weight(30, 30) == pytest.approx(0.5)
    events = [
        OutcomeEvent(hospital_id="H", timestamp=NOW - timedelta(days=30), accepted=False, reason_code="X"),
        OutcomeEvent(hospital_id="H", timestamp=NOW, accepted=True, reason_code="OK"),
        OutcomeEvent(hospital_id="H", timestamp=NOW + timedelta(days=1), accepted=False, reason_code="FUTURE"),
        OutcomeEvent(hospital_id="other", timestamp=NOW, accepted=False, reason_code="X"),
    ]
    rejects, total = decayed_counts(events, "H", NOW, 30)
    assert rejects == pytest.approx(0.5)
    assert total == pytest.approx(1.5)


def test_frequent_rejecter_scores_worse(cfg):
    events = [OutcomeEvent(hospital_id="H", timestamp=NOW - timedelta(days=1), accepted=False, reason_code="NO_BED")
              for _ in range(10)]
    r, t = decayed_counts(events, "H", NOW, cfg.v("reliability.decay_half_life_days"))
    assert p_cannot_treat(True, r, t, 1, 10) > 0.4


class FixedDwell:
    def __init__(self, v):
        self.v = v

    def mean_offload_min(self, hospital_id, when):
        return self.v


def _status(**kw):
    base = dict(hospital_id="H", timestamp=NOW - timedelta(minutes=5), provenance=Provenance.SIMULATED,
                source="sim")
    return HospitalStatus(**(base | kw))


def test_ed_wait_prefers_dwell_logs(cfg):
    dwell = Sourced(value=22.0, tag=Tagged(provenance=Provenance.REAL, source="108 logs", as_of=NOW.date()))
    w = expected_ed_wait("H", NOW, _status(offload_wait_min=5), FixedDwell(dwell), cfg)
    assert (w.value, w.provenance) == (22.0, Provenance.REAL)


def test_ed_wait_fallback_chain(cfg):
    assert expected_ed_wait("H", NOW, _status(offload_wait_min=45), FixedDwell(None), cfg).value == 45
    red = expected_ed_wait("H", NOW, _status(ed_status=EDStatus.RED), None, cfg)
    assert (red.value, red.provenance) == (30, Provenance.PLACEHOLDER)
    stale = _status(ed_status=EDStatus.GREEN, timestamp=NOW - timedelta(hours=5))
    assert expected_ed_wait("H", NOW, stale, None, cfg).value == 15  # stale ignored -> assume amber
    assert expected_ed_wait("H", NOW, None, None, cfg).value == 15


def test_outcome_log_matches_exact_scan():
    import random

    from advisor.scoring.reliability import OutcomeLog

    rng = random.Random(3)
    events = [OutcomeEvent(hospital_id=rng.choice("ABC"), timestamp=NOW + timedelta(hours=i * 7 + rng.random()),
                           accepted=rng.random() < 0.7, reason_code="X") for i in range(200)]
    events.insert(150, OutcomeEvent(hospital_id="B", timestamp=NOW, accepted=False, reason_code="LATE_LOG"))
    log = OutcomeLog(30, events)
    for when in (NOW + timedelta(days=d) for d in (0, 10, 30, 59, 90)):  # includes queries in the past
        for h in "ABCZ":
            fast = log.counts(h, when)
            exact = decayed_counts(events, h, when, 30)
            assert fast == pytest.approx(exact, rel=1e-9, abs=1e-12)
            assert log.sources(h, when) == {e.source for e in events if e.hospital_id == h and e.timestamp <= when}
