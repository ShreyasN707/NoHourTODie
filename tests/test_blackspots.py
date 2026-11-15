"""Black spots (pipeline/blackspots.py): detection, cause attribution, clustering, and the simulator split it relies on."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

h3 = pytest.importorskip("h3")

from pipeline.blackspots import (  # noqa: E402
    CAUSES,
    cell_stats,
    classify_calls,
    cluster_black_spots,
    plans_from_scenario,
    wilson_lower,
)
from pipeline.config import BLACKSPOT_MIN_LATE, PROCESSED_DIR, WEB_DATA_DIR  # noqa: E402

TARGET = 15.0


def _log(rows):
    """rows: (cell_idx, response_min, planned_min, best_min)."""
    return pd.DataFrame(rows, columns=["cell_idx", "response_min", "planned_min", "best_min"])


def test_wilson_lower_bound():
    lo = wilson_lower(np.array([0, 4, 10, 50, 3]), np.array([0, 4, 10, 100, 0]))
    assert lo[0] == 0.0 and lo[4] == 0.0  # no calls
    assert 0.55 < lo[1] < 0.65  # 4 of 4 late is not yet "confidently over 60%"
    assert lo[2] > lo[1]  # more evidence, tighter bound
    assert 0.40 < lo[3] < 0.50


def test_causes_follow_the_counterfactual_drive_times():
    log = classify_calls(_log([
        (0, 10.0, 8.0, 5.0),   # in time
        (0, 30.0, 25.0, 20.0),  # even the nearest candidate site is over target
        (0, 30.0, 20.0, 9.0),   # a site could make it, none staffed within reach
        (0, 30.0, 9.0, 9.0),    # staffed within reach, but the unit was out
    ]), TARGET)
    assert list(log["late"]) == [False, True, True, True]
    assert list(log["cause"]) == ["", "unreachable", "uncovered", "busy"]
    assert log["excess_min"].tolist() == [0.0, 15.0, 15.0, 15.0]


def test_black_spot_needs_volume_and_confidence():
    rows = []
    rows += [(0, 40.0, 30.0, 30.0)] * 8  # cell 0: 8/8 late, unreachable -> black, severe (p90 > 2x)
    rows += [(1, 20.0, 10.0, 10.0)] * 2  # cell 1: 2/2 late -> too few late calls
    rows += [(2, 20.0, 20.0, 10.0)] * 4 + [(2, 5.0, 5.0, 5.0)] * 6  # cell 2: 4/10 late -> share too low
    st = cell_stats(classify_calls(_log(rows), TARGET), n_cells=4, target_min=TARGET)
    assert st.loc[0, "black"] and st.loc[0, "cause"] == "unreachable" and st.loc[0, "severity"] == "severe"
    assert not st.loc[1, "black"] and st.loc[1, "late"] < BLACKSPOT_MIN_LATE
    assert not st.loc[2, "black"]
    assert st.loc[3, "calls"] == 0 and not st.loc[3, "black"] and st.loc[3, "severity"] == ""
    assert {f"n_{c}" for c in CAUSES} <= set(st.columns)


def test_clusters_join_touching_hexagons_only():
    centre = h3.latlng_to_cell(12.97, 77.59, 8)
    neighbour = next(c for c in h3.grid_disk(centre, 1) if c != centre)
    far = h3.latlng_to_cell(13.10, 77.70, 8)
    cells = pd.DataFrame({
        "h3_index": [centre, neighbour, far],
        "lat": [12.97, 12.975, 13.10],
        "lon": [77.59, 77.595, 77.70],
        "zone": ["A", "B", "C"],
    })
    rows = [(i, 40.0, 30.0, 30.0) for i in (0, 1, 2) for _ in range(6)] + [(2, 40.0, 30.0, 30.0)] * 6
    st = cell_stats(classify_calls(_log(rows), TARGET), n_cells=3, target_min=TARGET)
    clusters = cluster_black_spots(st, cells)
    assert sorted(len(c["cells"]) for c in clusters) == [1, 2]
    # worst (most minutes late) first; ids follow that order
    assert clusters[0]["excess_min"] >= clusters[1]["excess_min"] and [c["id"] for c in clusters] == [1, 2]
    assert all(c["fix"] and c["cause"] == "unreachable" for c in clusters)


@pytest.mark.skipif(not (WEB_DATA_DIR / "scenarios_tmk.json").exists() or not (PROCESSED_DIR / "calls_tmk.parquet").exists(),
                    reason="needs `make package`")
def test_replay_log_matches_dashboard_summary():
    """The per-call replay must aggregate to exactly what simulate_plan reports for the dashboard plan."""
    import json

    from pipeline.simulate import replay_calls, simulate_plan, summarize

    reg = "tmk"
    cells = pd.read_parquet(PROCESSED_DIR / f"cells_{reg}.parquet")
    sites = pd.read_parquet(PROCESSED_DIR / f"sites_{reg}.parquet")
    calls = pd.read_parquet(PROCESSED_DIR / f"calls_{reg}.parquet")
    t = np.load(PROCESSED_DIR / f"travel_{reg}.npz")
    plans = plans_from_scenario(json.loads((WEB_DATA_DIR / f"scenarios_{reg}.json").read_text())["goldenhour"], sites)
    args = (reg, plans, calls, cells, sites, t["fwd"], t["back"], t["ss"])
    log = replay_calls(*args)
    assert summarize(log, plans, 10.0) == simulate_plan(*args)
    # counterfactuals are bounds: nothing beats the nearest candidate site, and free units start at planned sites
    assert (log["best_min"] <= log["planned_min"] + 1e-4).all()
    assert (log["response_min"] >= log["best_min"] - 1e-4).all()
    # the earlier window is disjoint and directly before
    earlier = replay_calls(*args, sim_days=28, end_offset_days=28)
    assert earlier["day_of_year"].max() + 1 == log["day_of_year"].min()


@pytest.mark.parametrize("reg", ["blr", "tmk"])
def test_exported_payload_is_consistent(reg):
    """Chronic/new/resolved and the stage before/after counts must add up with the black-spot sets they describe."""
    import json

    path = WEB_DATA_DIR / f"blackspots_{reg}.json"
    if not path.exists():
        pytest.skip("needs `make blackspots`")
    d = json.loads(path.read_text())
    if "stages" not in d:
        pytest.skip("old payload; re-run `make blackspots`")
    by = {s["key"]: s for s in d["scenarios"]}
    black = {k: {r[0] for r in s["cells"] if r[6] >= 0} for k, s in by.items()}
    for k, s in by.items():
        assert len(black[k]) == s["kpis"]["black_spots"]
        before = set(s["before_black"])
        assert s["vs_before"] == {"chronic": len(black[k] & before), "new": len(black[k] - before),
                                  "resolved": len(before - black[k])}
        for r in s["cells"]:
            assert len(r) == 11 and 0.0 <= r[7] <= 1.0 and sum(r[8:11]) == r[2]  # causes add up to late calls
            assert (r[6] >= 0) == (r[2] >= BLACKSPOT_MIN_LATE and r[7] >= 0.5)
    assert {"fleet10", "fleet25"} <= set(by)
    for st in d["stages"]:
        if st["prev"]:
            a, b = black[st["prev"]], black[st["key"]]
            assert st["vs_prev"] == {"resolved": len(a - b), "remaining": len(a & b), "new": len(b - a)}


@pytest.mark.skipif(not (WEB_DATA_DIR / "scenarios_tmk.json").exists() or not (PROCESSED_DIR / "calls_tmk.parquet").exists(),
                    reason="needs `make package`")
def test_fleet_advice_moves_are_feasible_and_checked():
    """The assistant's fleet advice: a day in the log, moves between real sites within the drive limit,
    and only moves that do not add late calls on the weeks before are kept."""
    from datetime import date

    from pipeline.advise import ADVICE_MAX_MOVE_MIN, ADVICE_MAX_MOVES, RegionAdvisor

    adv = RegionAdvisor("tmk")
    a = adv.advise(day="yesterday", hour=9, today=date(2026, 11, 15))
    assert a["day"] == adv.last_day.isoformat() and a["day_note"]  # past the log: falls back, and says so
    assert a["block"] == 2 and a["block_label"] == "8 am–12 pm"
    lb = a["looked_back"]
    assert lb["late"] == sum(lb["by_cause"].values()) == sum(n for _, n in lb["late_cells"])
    assert len(a["moves"]) <= ADVICE_MAX_MOVES
    ids = set(adv.sites["site_id"])
    for m in a["moves"]:
        assert m["from"]["site_id"] in ids and m["to"]["site_id"] in ids and m["from"]["site_id"] != m["to"]["site_id"]
        assert m["drive_min"] <= ADVICE_MAX_MOVE_MIN
    if a["check"]:
        assert a["check"]["after"]["late"] <= a["check"]["before"]["late"]
        assert a["check"]["after"]["calls"] == a["check"]["before"]["calls"]
    # a day inside the log is used as asked
    assert adv.resolve_day("2026-10-01", today=date(2026, 11, 15)) == (date(2026, 10, 1), None)
    assert "late" in a["reply"] or "keep" in a["reply"]
