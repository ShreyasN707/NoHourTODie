from datetime import timedelta

from conftest import T11, FakeTravel, assessment, hospital

from advisor.prealert.fallback import ACCIDENT_REFUSAL_FLAG, PreAlertManager, PreAlertState
from advisor.rules.engine import AdvisorContext, recommend

ED = ["ed_24x7"]


def _rec(cfg, complaint="other", **kw):
    hs = [hospital("A", 1.0, ED + ["trauma_ot", "blood_bank"]), hospital("B", 2.0, ED + ["trauma_ot", "blood_bank"])]
    return recommend(assessment(complaint, **kw), AdvisorContext(hs, FakeTravel({1.0: 5, 2.0: 9}), cfg))


def test_reject_falls_back_and_logs(cfg):
    m = PreAlertManager(cfg)
    pa = m.send(_rec(cfg), T11)
    assert pa.current.hospital_id == "A"
    m.respond(pa.prealert_id, False, T11 + timedelta(seconds=30), "NO_BED")
    assert pa.current.hospital_id == "B"
    m.respond(pa.prealert_id, True, T11 + timedelta(seconds=60), "ACCEPTED")
    assert pa.state == PreAlertState.ACCEPTED
    assert [(e.hospital_id, e.accepted) for e in m.events] == [("A", False), ("B", True)]


def test_timeout_falls_back_then_exhausts(cfg):
    m = PreAlertManager(cfg)
    pa = m.send(_rec(cfg), T11)
    later = T11 + m.timeout
    assert m.check_timeouts(later - timedelta(seconds=1)) == []
    m.check_timeouts(later)
    assert pa.current.hospital_id == "B"
    m.check_timeouts(later + m.timeout)
    assert pa.state == PreAlertState.EXHAUSTED
    assert all(e.reason_code == "TIMEOUT" for e in m.events)


def test_crew_override_goes_first(cfg):
    pa = PreAlertManager(cfg).send(_rec(cfg), T11, crew_choice="B")
    assert pa.crew_override and pa.current.hospital_id == "B"


def test_accident_refusal_flagged(cfg):
    m = PreAlertManager(cfg)
    pa = m.send(_rec(cfg, "trauma", vitals={"sbp": 70}), T11)
    m.respond(pa.prealert_id, False, T11, "NO_ICU_BED")
    assert ACCIDENT_REFUSAL_FLAG in m.events[0].reason_code
