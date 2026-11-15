"""FastAPI advisor API (docs/advisor_contract.md). Fake travel and FICTIONAL hospitals; no road graph is loaded."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from conftest import T11, T18, FakeTravel, hospital
from fastapi.testclient import TestClient

from advisor.api.app import SIM_RESULTS_MISSING, create_app
from advisor.api.state import DEMO_BANNER
from advisor.provenance import Provenance

ED = ["ed_24x7"]
# P1, P2: PCI hubs. T: thrombolysis only. N: nearest, ED only (no cath lab).
# P1 is fast at 11:00 and stuck in evening traffic at 18:00 (FakeTravel keys on destination lat and hour).
TRAVEL = {1.0: {11: 30, 18: 130}, 2.0: {11: 40, 18: 140}, 3.0: 12, 4.0: 5}


def _hospitals():
    return [
        hospital("P1", 1.0, ED + ["pci_24x7"]),
        hospital("P2", 2.0, ED + ["pci_24x7"]),
        hospital("T", 3.0, ED + ["thrombolysis"]),
        hospital("N", 4.0, ED),
    ]


@pytest.fixture
def make_client(cfg, tmp_path):
    def _make(**kw) -> TestClient:
        kw.setdefault("sim_results_path", tmp_path / "sim_results.json")
        app = create_app(cfg=cfg, hospitals=_hospitals(), travel=FakeTravel(TRAVEL), is_demo=True, **kw)
        return TestClient(app)
    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    with make_client() as c:
        yield c


def _assessment(when=T11, case_id="C1", **kw) -> dict:
    body = {"case_id": case_id, "scene_lat": 12.9716, "scene_lon": 77.5946, "base_lat": 12.98, "base_lon": 77.60,
            "complaint": "chest_pain", "ecg_stemi": True, "when": when.isoformat()}
    return body | kw


# ------------------------------------------------------------------ read-only endpoints

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "registry_is_demo": True, "travel_provider": "FakeTravel"}


def test_hospitals_demo_banner(client):
    body = client.get("/hospitals").json()
    assert body["is_demo"] is True
    assert body["banner"] == DEMO_BANNER
    assert body["summary"]["hospitals"] == 4
    assert {h["hospital_id"] for h in body["hospitals"]} == {"P1", "P2", "T", "N"}


def test_hospitals_non_demo_still_flags_simulated_tags(cfg, tmp_path):
    app = create_app(cfg=cfg, hospitals=_hospitals(), travel=FakeTravel(TRAVEL), sim_results_path=tmp_path / "x")
    body = TestClient(app).get("/hospitals").json()
    assert body["is_demo"] is False
    assert body["banner"] and "fictional" not in body["banner"]  # SIMULATED fixture tags, not the demo registry


def test_config_params(client, cfg):
    params = client.get("/config/params").json()["params"]
    keys = {p["key"] for p in params}
    assert keys == set(cfg.params)
    p = next(p for p in params if p["key"] == "rules.stemi_fmc_to_pci_window_min")
    assert p == {"key": "rules.stemi_fmc_to_pci_window_min", "value": 120, "provenance": "PLACEHOLDER",
                 "basis": cfg.p("rules.stemi_fmc_to_pci_window_min").basis, "needs_source": False}
    assert any(p["needs_source"] for p in params)


# ------------------------------------------------------------------ recommend

def test_recommend_stemi_pci_hub(client, cfg):
    r = client.post("/recommend", json=_assessment())
    assert r.status_code == 200
    body = r.json()
    rec = body["recommendation"]
    assert rec["care_category"] == "stemi" and rec["route"] == "primary_pci"
    assert [h["hospital_id"] for h in rec["ranked"]] == ["P1", "P2"]
    top = rec["ranked"][0]
    assert top["components"]["travel_min"] == 30
    assert top["inputs_provenance"]["travel"] == "PLACEHOLDER"
    assert rec["non_real_inputs_used"] is True
    assert rec["inputs_provenance"]["capabilities"] == "SIMULATED"
    # The nearest hospital has no cath lab: excluded, but still scored for comparison.
    excl = {x["hospital_id"]: x for x in rec["excluded"]}
    assert excl["N"]["reason"] == "missing:pci_24x7" and excl["N"]["components"]["travel_min"] == 5
    assert body["is_demo_registry"] is True
    assert body["banner"] == DEMO_BANNER
    assert body["travel_provider"] == "FakeTravel"
    assert set(body["hospitals"]) == {"P1", "P2", "T", "N"}
    assert body["hospitals"]["P1"] == {"name": "Hospital P1 (fictional)", "lat": 1.0, "lon": 77.6}
    assert body["todo_params"] == cfg.todo_params()


def test_recommend_no_live_status_assumes_amber(client):
    """No ED feed: the API serves no statuses, so ED wait comes from the unknown-status fallback."""
    rec = client.post("/recommend", json=_assessment()).json()["recommendation"]
    assert rec["ranked"][0]["inputs_provenance"]["ed_wait"] in (Provenance.SIMULATED, Provenance.PLACEHOLDER)


def test_traffic_flip(client):
    morning = client.post("/recommend", json=_assessment(when=T11)).json()["recommendation"]
    evening = client.post("/recommend", json=_assessment(when=T18)).json()["recommendation"]
    assert (morning["route"], morning["ranked"][0]["hospital_id"]) == ("primary_pci", "P1")
    assert (evening["route"], evening["ranked"][0]["hospital_id"]) == ("pharmacoinvasive", "T")


def test_recommend_unknown_complaint_422(client):
    r = client.post("/recommend", json=_assessment(complaint="headache"))
    assert r.status_code == 422
    assert "unknown complaint 'headache'" in r.json()["detail"]


def test_recommend_bad_body_422(client):
    assert client.post("/recommend", json={"case_id": "C1"}).status_code == 422


# ------------------------------------------------------------------ pre-alert (SIMULATED outcomes)

def test_prealert_reject_fallback_accept(client):
    client.post("/recommend", json=_assessment())
    now = T11 + timedelta(minutes=1)
    pa = client.post("/prealert", json={"case_id": "C1", "now": now.isoformat()}).json()
    assert pa["state"] == "pending" and pa["queue"] == ["P1", "P2"]
    assert [a["hospital_id"] for a in pa["attempts"]] == ["P1"]
    pid = pa["prealert_id"]

    pa = client.post(f"/prealert/{pid}/response", json={"accepted": False, "reason_code": "CATH_LAB_BUSY",
                                                         "now": (now + timedelta(seconds=30)).isoformat()}).json()
    assert pa["state"] == "pending"
    assert [a["hospital_id"] for a in pa["attempts"]] == ["P1", "P2"]  # fell back to the next-best
    assert pa["attempts"][0]["accepted"] is False

    pa = client.post(f"/prealert/{pid}/response", json={"accepted": True, "reason_code": "OK"}).json()
    assert pa["state"] == "accepted" and pa["attempts"][-1]["accepted"] is True

    r = client.post(f"/prealert/{pid}/response", json={"accepted": True, "reason_code": "OK"})
    assert r.status_code == 409
    assert "not pending" in r.json()["detail"]

    events = client.app.state.advisor.events
    assert [(e.hospital_id, e.accepted) for e in events] == [("P1", False), ("P2", True)]
    assert all(e.source == Provenance.SIMULATED for e in events)


def test_prealert_rejects_feed_reliability(client):
    """A logged reject raises P(cannot treat) for that hospital on the next recommendation."""
    before = client.post("/recommend", json=_assessment()).json()["recommendation"]["ranked"][0]
    pid = client.post("/prealert", json={"case_id": "C1", "now": T11.isoformat()}).json()["prealert_id"]
    client.post(f"/prealert/{pid}/response", json={"accepted": False, "reason_code": "NO_BED",
                                                   "now": T11.isoformat()})
    after = client.post("/recommend", json=_assessment(when=T11 + timedelta(minutes=1))).json()["recommendation"]
    p1 = next(h for h in after["ranked"] if h["hospital_id"] == "P1")
    assert p1["components"]["p_cannot_treat"] > before["components"]["p_cannot_treat"]


def test_prealert_timeout_falls_back(client, cfg):
    client.post("/recommend", json=_assessment())
    pid = client.post("/prealert", json={"case_id": "C1", "now": T11.isoformat()}).json()["prealert_id"]
    early = client.post("/prealert/check_timeouts", json={"now": (T11 + timedelta(seconds=5)).isoformat()})
    assert early.json() == []
    late = T11 + timedelta(seconds=cfg.v("prealert.response_timeout_s") + 1)
    advanced = client.post("/prealert/check_timeouts", json={"now": late.isoformat()}).json()
    assert [p["prealert_id"] for p in advanced] == [pid]
    assert advanced[0]["attempts"][0]["reason_code"] == "TIMEOUT"
    assert advanced[0]["attempts"][-1]["hospital_id"] == "P2"


def test_prealert_crew_choice_outside_ranking_gets_eta(client):
    client.post("/recommend", json=_assessment())
    pa = client.post("/prealert", json={"case_id": "C1", "crew_choice": "N", "now": T11.isoformat()}).json()
    assert pa["crew_override"] is True and pa["queue"][0] == "N"
    assert pa["attempts"][0]["eta_min"] == 5
    r = client.post("/prealert", json={"case_id": "C1", "crew_choice": "nope"})
    assert r.status_code == 422


def test_prealert_unknown_case_404(client):
    r = client.post("/prealert", json={"case_id": "never-recommended"})
    assert r.status_code == 404
    assert "never-recommended" in r.json()["detail"]


def test_prealert_unknown_id_404(client):
    r = client.post("/prealert/PA99999/response", json={"accepted": True, "reason_code": "OK"})
    assert r.status_code == 404


# ------------------------------------------------------------------ sim results

def test_sim_results_missing_404(client):
    r = client.get("/sim/results")
    assert r.status_code == 404
    assert r.json() == {"detail": SIM_RESULTS_MISSING}
    assert SIM_RESULTS_MISSING == "no simulation results yet; run make advisor-sim"


def test_sim_results_passthrough(make_client, tmp_path):
    path = tmp_path / "sim_results.json"
    payload = {"seed": 7, "banner": DEMO_BANNER, "anything": [1, 2, 3]}
    path.write_text(json.dumps(payload))
    with make_client(sim_results_path=path) as c:
        r = c.get("/sim/results")
    assert r.status_code == 200 and r.json() == payload


def test_cors_vite_origin(client):
    for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
        r = client.options("/recommend", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
        assert r.headers.get("access-control-allow-origin") == origin
