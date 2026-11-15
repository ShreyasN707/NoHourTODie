"""SimPy replay (advisor.sim): tiny synthetic road graph and FICTIONAL hospitals. Never loads the real graph."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta

import numpy as np
import pytest
import scipy.sparse as sp
import yaml
from conftest import hospital

from advisor.config import DEFAULT_CONFIG, parse_config
from advisor.graph.travel import apply_quantile, profile_minutes
from advisor.models import LatLon
from advisor.provenance import Provenance, Sourced, Tagged
from advisor.sim.incidents import Incident
from advisor.sim.patients import can_treat, critical_categories, sample_patient
from advisor.sim.replay import POLICIES, Draws, World, metrics, run_policy
from advisor.sim.sweeps import default_sweeps
from advisor.sim.travel_matrix import SimTravel, compute_tables

LON = 77.6
NODE_LAT = [12.90, 12.91, 12.92, 12.93]
EDGE_S = 300.0  # free-flow seconds between neighbouring nodes
SCENE = (NODE_LAT[0], LON)
WHEN = datetime(2026, 10, 20, 11, 0)
TERTIARY = ["ed_24x7", "ct_24x7", "pci_24x7", "thrombolysis", "trauma_ot", "blood_bank", "obstetric_emergency",
            "nicu", "picu", "neurosurgery", "orthopaedics"]


def _raw(**sim_overrides) -> dict:
    with open(DEFAULT_CONFIG) as f:
        raw = yaml.safe_load(f)
    quiet = {"reject_base_rate": 0.0, "prealert_no_answer_share": 0.0, "background_load_scale": 0.0,
             "status_stale_share": 0.0, "status_misreport_share": 0.0, "travel_noise_sigma": 0.0}
    for k, v in {**quiet, **sim_overrides}.items():
        raw["sim"][k]["value"] = v
    return raw


def _line_graph():
    """Line 0 - 1 - 2 - 3, both directions, EDGE_S seconds per hop."""
    u = [0, 1, 1, 2, 2, 3]
    v = [1, 0, 2, 1, 3, 2]
    adj = sp.csr_matrix((np.full(len(u), EDGE_S), (u, v)), shape=(4, 4))
    return adj, np.array(NODE_LAT), np.full(4, LON)


def _world(cfg, hospitals, complaints, patient_u=None, seed=7):
    adj, lat, lon = _line_graph()
    tables = compute_tables(adj, lat, lon, [(h.lat, h.lon) for h in hospitals], [SCENE], [SCENE])
    incidents = [Incident(incident_id=f"T{k}", when=WHEN + timedelta(minutes=5 * k), lat=SCENE[0], lon=SCENE[1],
                          h3_index="x", zone="test", call_type="test", complaint=c) for k, c in enumerate(complaints)]
    draws = Draws.make(len(incidents), len(hospitals), seed)
    if patient_u is not None:
        draws.patient_u[:] = np.asarray(patient_u, dtype=float)
    return World(incidents=incidents, bases=[SCENE] * len(incidents), hospitals=hospitals,
                 travel=SimTravel(tables, cfg), cfg=cfg, draws=draws, origin=WHEN - timedelta(hours=1), seed=seed)


# ---------------------------------------------------------------- travel tables


def test_tables_and_provider_match_profile():
    cfg = parse_config(_raw())
    a, b = hospital("A", NODE_LAT[1], ["ed_24x7"], lon=LON), hospital("B", NODE_LAT[3], TERTIARY, lon=LON)
    w = _world(cfg, [a, b], ["other"])
    t = w.travel
    assert t.free_flow_s(SCENE, (a.lat, a.lon)) == pytest.approx(EDGE_S)
    assert t.free_flow_s(SCENE, (b.lat, b.lon)) == pytest.approx(3 * EDGE_S)
    assert t.free_flow_s((a.lat, a.lon), (b.lat, b.lon)) == pytest.approx(2 * EDGE_S)
    assert t.free_flow_s((b.lat, b.lon), SCENE) == pytest.approx(3 * EDGE_S)  # hospital -> base
    got = t.minutes(LatLon(lat=SCENE[0], lon=LON), LatLon(lat=b.lat, lon=LON), WHEN, 0.8)
    expected = apply_quantile(Sourced(value=profile_minutes(3 * EDGE_S, WHEN, cfg),
                                      tag=Tagged(provenance=Provenance.PLACEHOLDER, source="x")), 0.8, cfg)
    assert got.value == pytest.approx(expected.value)
    assert got.provenance is Provenance.PLACEHOLDER
    with pytest.raises(KeyError):
        t.minutes(LatLon(lat=1.0, lon=1.0), LatLon(lat=b.lat, lon=LON), WHEN)


# ---------------------------------------------------------------- patients


def test_ground_truth_from_sampled_facts(cfg):
    stemi = sample_patient("chest_pain", [0.05, 0.9, 0.5], cfg)  # ECG shows STEMI
    assert (stemi.true_category, stemi.ecg_stemi) == ("stemi", True)
    no_ecg_not_stemi = sample_patient("chest_pain", [0.9, 0.9, 0.5], cfg)
    assert (no_ecg_not_stemi.ecg_stemi, no_ecg_not_stemi.true_category) == (None, "low_acuity")
    severe = sample_patient("trauma", [0.0, 0.0, 0.0], cfg)
    assert severe.true_category == "severe_trauma" and severe.vitals["sbp"] < 90
    assert sample_patient("other", [0.0, 0.9, 0.0], cfg).complaint == "paediatric"


def test_can_treat_stemi_needs_cath_lab_or_thrombolysis(cfg):
    assert not can_treat(hospital("A", 1.0, ["ed_24x7"]), "stemi", cfg)
    assert can_treat(hospital("B", 2.0, ["ed_24x7", "thrombolysis"]), "stemi", cfg)
    assert can_treat(hospital("C", 3.0, ["ed_24x7", "pci_24x7"]), "stemi", cfg)
    assert "arrest_airway" not in critical_categories(cfg) and "stemi" in critical_categories(cfg)


# ---------------------------------------------------------------- policies


def test_nearest_takes_nearest_ed():
    cfg = parse_config(_raw())
    a, b = hospital("A", NODE_LAT[1], ["ed_24x7"], lon=LON), hospital("B", NODE_LAT[3], TERTIARY, lon=LON)
    w = _world(cfg, [b, a], ["other"], patient_u=[[0.9, 0.9, 0.9]])  # stable 'other' -> low acuity
    [res] = run_policy(w, "nearest")
    assert res.hospitals_visited == ["A"]
    assert res.move_ons == 0 and res.ttt_min is not None and res.first_capable_treated


def test_nearest_secondary_transfer_when_capability_missing():
    cfg = parse_config(_raw())
    a, b = hospital("A", NODE_LAT[1], ["ed_24x7"], lon=LON), hospital("B", NODE_LAT[3], TERTIARY, lon=LON)
    w = _world(cfg, [a, b], ["chest_pain"], patient_u=[[0.05, 0.9, 0.9]])  # ECG STEMI
    [near] = run_policy(w, "nearest")
    assert near.true_category == "stemi"
    assert near.hospitals_visited == ["A", "B"] and near.move_ons == 1
    assert not near.first_capable_treated
    [adv] = run_policy(w, "advisor")
    assert adv.hospitals_visited == ["B"] and adv.move_ons == 0 and adv.first_capable_treated
    assert adv.ttt_min < near.ttt_min  # same draws, no queue: the detour is pure loss


def test_over_triage_is_not_counted_as_definitive_stemi_care():
    cfg = parse_config(_raw())
    a, b = hospital("A", NODE_LAT[1], ["ed_24x7"], lon=LON), hospital("B", NODE_LAT[3], TERTIARY, lon=LON)
    w = _world(cfg, [a, b], ["chest_pain"], patient_u=[[0.9, 0.9, 0.9]])  # no ECG, truly not STEMI
    [adv] = run_policy(w, "advisor")
    assert adv.advisor_category == "stemi" and adv.true_category == "low_acuity"
    m = metrics([adv], list(cfg.care_categories), critical_categories(cfg))
    assert m["time_to_treatment"]["by_category"]["stemi"]["n"] == 0
    assert m["time_to_treatment"]["by_category"]["low_acuity"]["n"] == 1
    assert m["time_to_treatment"]["critical"] == {"n": 0, "mean": None, "p50": None, "p90": None}
    assert m["critical_capable_first_attempt_share"] is None
    assert m["advisor_category_differs_from_truth"] == 1
    json.dumps(m, allow_nan=False)  # no NaN anywhere, the browser must be able to parse it


def test_untreatable_patient_has_no_definitive_care():
    cfg = parse_config(_raw())
    a = hospital("A", NODE_LAT[1], ["ed_24x7"], lon=LON)  # no hospital has a cath lab or thrombolysis
    w = _world(cfg, [a], ["chest_pain"], patient_u=[[0.05, 0.9, 0.9]])
    for policy in POLICIES:
        [res] = run_policy(w, policy)
        assert res.ttt_min is None and res.hospitals_visited == ["A"]


def test_same_seed_same_metrics_with_crowding_and_refusals():
    raw = _raw(reject_base_rate=0.2, prealert_no_answer_share=0.2, background_load_scale=1.0,
               status_stale_share=0.3, status_misreport_share=0.3, travel_noise_sigma=0.3)
    cfg = parse_config(raw)
    hs = [hospital("A", NODE_LAT[1], ["ed_24x7", "obstetric_emergency"], lon=LON),
          hospital("B", NODE_LAT[2], ["ed_24x7", "ct_24x7", "thrombolysis", "obstetric_emergency"], lon=LON),
          hospital("C", NODE_LAT[3], TERTIARY, lon=LON)]
    complaints = ["chest_pain", "trauma", "stroke_symptoms", "obstetric", "other"] * 12
    cats, crit = list(cfg.care_categories), critical_categories(cfg)

    def once(seed):
        w = _world(cfg, hs, complaints, seed=seed)
        return {p: metrics(run_policy(w, p), cats, crit) for p in POLICIES}

    first = once(7)
    assert json.dumps(first, sort_keys=True) == json.dumps(once(7), sort_keys=True)
    assert json.dumps(first, sort_keys=True) != json.dumps(once(8), sort_keys=True)
    assert first["advisor"]["n"] == len(complaints)


def test_advisor_logs_simulated_outcomes():
    """Pre-alert answers become OutcomeEvents (SIMULATED) the reliability score learns from."""
    from advisor.sim.replay import _Replay

    cfg = parse_config(_raw(reject_base_rate=0.5))
    hs = [hospital("A", NODE_LAT[1], ["ed_24x7"], lon=LON), hospital("B", NODE_LAT[3], TERTIARY, lon=LON)]
    r = _Replay(_world(cfg, hs, ["other"] * 10), "advisor")
    r.run()
    assert r.events and all(e.source is Provenance.SIMULATED for e in r.events)
    assert r.dwell_records


# ---------------------------------------------------------------- sweeps


def test_every_sweep_parses_and_changes_something():
    with open(DEFAULT_CONFIG) as f:
        raw = yaml.safe_load(f)
    before = copy.deepcopy(raw)
    for sw in default_sweeps(raw):
        new, cfg = sw.config(raw)
        assert new != raw, sw.label
        assert cfg.params  # parse_config ran its provenance checks
    assert raw == before  # sweeps never mutate the base config


def test_new_sim_params_are_honestly_tagged(cfg):
    for key, p in cfg.params.items():
        if key.startswith("sim."):
            assert p.provenance in (Provenance.SIMULATED, Provenance.PLACEHOLDER), key
