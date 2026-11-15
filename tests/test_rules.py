"""Decision rules (spec §4). Fictional hospitals; travel times are the spec's placeholder numbers."""

from datetime import timedelta

import pytest
from conftest import T11, T18, FakeTravel, assessment, hospital

from advisor.config import parse_config
from advisor.models import EDStatus, HospitalStatus, OutcomeEvent
from advisor.provenance import Provenance
from advisor.rules.engine import AdvisorContext, recommend
from advisor.rules.triage import classify

ED = ["ed_24x7"]


def _ctx(cfg, hospitals, travel, **kw):
    return AdvisorContext(hospitals=hospitals, travel=travel, cfg=cfg, **kw)


def _status(hid, when=T11, **kw):
    return HospitalStatus(hospital_id=hid, timestamp=when - timedelta(minutes=5),
                          provenance=Provenance.SIMULATED, source="sim", **kw)


# ------------------------------------------------------------------ triage

@pytest.mark.parametrize("complaint,kw,expected", [
    ("cardiac_arrest", {}, "arrest_airway"),
    ("chest_pain", {"ecg_stemi": True}, "stemi"),
    ("chest_pain", {}, "stemi"),  # no ECG -> over-triage
    ("chest_pain", {"ecg_stemi": False}, "low_acuity"),
    ("trauma", {"vitals": {"sbp": 80}}, "severe_trauma"),
    ("trauma", {"vitals": {"sbp": 120, "gcs": 15}}, "low_acuity"),
    ("stroke_symptoms", {}, "stroke"),
    ("obstetric", {}, "obstetric"),
    ("other", {"vitals": {"rr": 35}}, "arrest_airway"),
    ("other", {}, "low_acuity"),
])
def test_classify(cfg, complaint, kw, expected):
    assert classify(assessment(complaint, **kw), cfg)[0] == expected


def test_classify_unknown_complaint(cfg):
    with pytest.raises(ValueError):
        classify(assessment("headache"), cfg)


# ------------------------------------------------------------------ worked examples end to end

def test_stemi_worked_example_picks_pci_hub(cfg):
    a_ = hospital("A", 1.0, ED + ["thrombolysis"])  # nearest, no cath lab
    b_ = hospital("B", 2.0, ED + ["pci_24x7", "thrombolysis"])
    travel = FakeTravel({1.0: 8, 2.0: 18})
    statuses = {"A": _status("A", offload_wait_min=10), "B": _status("B", offload_wait_min=15)}
    rec = recommend(assessment("chest_pain", ecg_stemi=True), _ctx(cfg, [a_, b_], travel, statuses=statuses))
    assert rec.route == "primary_pci"
    assert rec.reason_code == "STEMI_PRIMARY_PCI"
    assert rec.ranked[0].hospital_id == "B"
    assert rec.ranked[0].score == pytest.approx(38)
    ex = {x.hospital_id: x for x in rec.excluded}
    assert ex["A"].reason == "missing:pci_24x7"
    assert ex["A"].components.total == pytest.approx(68)
    assert rec.lambda_used == 0
    assert all(q == cfg.v("scoring.pessimistic_travel_quantile") for _, _, q in travel.calls)


def test_stemi_outside_window_goes_pharmacoinvasive(cfg):
    a_ = hospital("A", 1.0, ED + ["thrombolysis"])
    b_ = hospital("B", 2.0, ED + ["pci_24x7", "thrombolysis"])
    rec = recommend(assessment("chest_pain", ecg_stemi=True, minutes_since_first_contact=100),
                    _ctx(cfg, [a_, b_], FakeTravel({1.0: 8, 2.0: 18})))
    assert rec.route == "pharmacoinvasive"
    assert rec.ranked[0].hospital_id == "A"


def test_low_acuity_load_balances_to_quiet_hospital(cfg):
    c_ = hospital("C", 1.0, ED)
    d_ = hospital("D", 2.0, ED)
    statuses = {"C": _status("C", offload_wait_min=45), "D": _status("D", offload_wait_min=10)}
    rec = recommend(assessment("other"), _ctx(cfg, [c_, d_], FakeTravel({1.0: 6, 2.0: 14}), statuses=statuses))
    assert rec.lambda_used == 0.5
    assert [r.hospital_id for r in rec.ranked] == ["D", "C"]


def test_traffic_flip(cfg):
    near = hospital("N", 1.0, ED)
    far = hospital("F", 2.0, ED)
    travel = FakeTravel({1.0: {11: 10, 18: 28}, 2.0: {11: 20, 18: 24}})
    ctx = _ctx(cfg, [near, far], travel)
    assert recommend(assessment("cardiac_arrest", when=T11), ctx).ranked[0].hospital_id == "N"
    assert recommend(assessment("cardiac_arrest", when=T18), ctx).ranked[0].hospital_id == "F"


# ------------------------------------------------------------------ guardrails and edge cases

def test_trauma_ignores_crowding_and_prefers_capability(cfg):
    full = hospital("T1", 1.0, ED + ["trauma_ot", "blood_bank", "neurosurgery", "orthopaedics"])
    basic = hospital("T2", 2.0, ED + ["trauma_ot", "blood_bank"])
    statuses = {"T1": _status("T1", ed_status=EDStatus.RED, offload_wait_min=90)}
    rec = recommend(assessment("trauma", vitals={"sbp": 70}),
                    _ctx(cfg, [full, basic], FakeTravel({1.0: 25, 2.0: 8}), statuses=statuses))
    assert rec.care_category == "severe_trauma"
    assert rec.ranked[0].hospital_id == "T1"
    assert rec.ranked[0].components.ed_wait_min == 0


def test_diversion_and_down_equipment_respected(cfg):
    a_ = hospital("A", 1.0, ED + ["pci_24x7"])
    b_ = hospital("B", 2.0, ED + ["pci_24x7"])
    c_ = hospital("C", 3.0, ED + ["pci_24x7"])
    statuses = {"A": _status("A", diversion=True), "B": _status("B", down_capabilities=["pci_24x7"])}
    rec = recommend(assessment("chest_pain", ecg_stemi=True),
                    _ctx(cfg, [a_, b_, c_], FakeTravel({1.0: 5, 2.0: 6, 3.0: 30}), statuses=statuses))
    assert [r.hospital_id for r in rec.ranked] == ["C"]
    reasons = {x.hospital_id: x.reason for x in rec.excluded}
    assert reasons == {"A": "diversion", "B": "missing:pci_24x7"}


def test_no_capable_hospital_stabilises_at_nearest_ed(cfg):
    a_ = hospital("A", 1.0, ED)
    b_ = hospital("B", 2.0, ED)
    rec = recommend(assessment("paediatric"), _ctx(cfg, [a_, b_], FakeTravel({1.0: 12, 2.0: 4})))
    assert rec.reason_code == "PAEDIATRIC_NO_CAPABLE_STABILISE_NEAREST"
    assert rec.ranked[0].hospital_id == "B"


def test_stroke_lvo_bypass_disabled_by_default(cfg):
    ct = hospital("CT", 1.0, ED + ["ct_24x7", "thrombolysis"])
    evt = hospital("EVT", 2.0, ED + ["ct_24x7", "thrombolysis", "thrombectomy"])
    ctx = _ctx(cfg, [ct, evt], FakeTravel({1.0: 10, 2.0: 15}))
    rec = recommend(assessment("stroke_symptoms", stroke_score=8), ctx)
    assert rec.route == "ct_thrombolysis" and "disabled" in rec.reason
    assert rec.ranked[0].hospital_id == "CT"


def test_stroke_lvo_bypass_when_configured(cfg):
    import yaml

    from advisor.config import DEFAULT_CONFIG

    raw = yaml.safe_load(open(DEFAULT_CONFIG))
    raw["rules"]["stroke_lvo_score_cutoff"]["value"] = 5
    raw["rules"]["stroke_thrombectomy_extra_travel_limit_min"]["value"] = 10
    tuned = parse_config(raw)
    ct = hospital("CT", 1.0, ED + ["ct_24x7", "thrombolysis"])
    evt = hospital("EVT", 2.0, ED + ["ct_24x7", "thrombolysis", "thrombectomy"])
    ctx = _ctx(tuned, [ct, evt], FakeTravel({1.0: 10, 2.0: 15}))
    rec = recommend(assessment("stroke_symptoms", stroke_score=8), ctx)
    assert rec.route == "lvo_thrombectomy"
    assert rec.ranked[0].hospital_id == "EVT"


def test_reliability_history_changes_ranking(cfg):
    a_ = hospital("A", 1.0, ED)
    b_ = hospital("B", 2.0, ED)
    events = [OutcomeEvent(hospital_id="A", timestamp=T11 - timedelta(days=1), accepted=False, reason_code="NO_BED")
              for _ in range(20)]
    rec = recommend(assessment("obstetric") .model_copy(update={"complaint": "other"}),
                    _ctx(cfg, [a_, b_], FakeTravel({1.0: 10, 2.0: 20}), events=events))
    assert rec.ranked[0].hospital_id == "B"
    assert rec.ranked[1].inputs_provenance["p_cannot_treat"] == Provenance.SIMULATED


def test_provenance_reported(cfg):
    rec = recommend(assessment("other"), _ctx(cfg, [hospital("A", 1.0, ED)], FakeTravel({1.0: 5})))
    assert rec.non_real_inputs_used
    assert rec.inputs_provenance["transfer_delay"] == Provenance.PLACEHOLDER
    assert rec.inputs_provenance["capabilities"] == Provenance.SIMULATED
    assert rec.reason_code and rec.reason
