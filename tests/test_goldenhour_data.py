"""GoldenHour data-package loaders. Tiny fictional fixtures written to tmp_path."""

import csv
from datetime import date, datetime

from advisor.graph import default_travel_provider
from advisor.interfaces import TravelTimeProvider
from advisor.provenance import Provenance
from advisor.registry.osm_worklist import build_worklist
from advisor.sim.incidents import load_goldenhour_calls


def _write(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def test_synthetic_calls_always_simulated(tmp_path, cfg):
    cells = _write(tmp_path / "cells.csv", ["h3_index", "lat", "lon"], [["c1", 12.9, 77.6]])
    calls = _write(tmp_path / "calls.csv", ["call_id", "timestamp", "h3_index", "zone", "call_type"], [
        ["x1", "2026-10-05 08:00:00", "c1", "Z", "cardiac"],
        ["x2", "2026-10-05 07:00:00", "c1", "Z", "respiratory"],
        ["x3", "2026-10-05 09:00:00", "missing", "Z", "trauma"],
        ["x4", "2026-10-05 09:30:00", "c1", "Z", "unknown_type"],
        ["x5", "2026-09-01 09:30:00", "c1", "Z", "trauma"],
    ])
    ld = load_goldenhour_calls(calls, cells, cfg, start=datetime(2026, 10, 1))
    assert ld.tag.provenance == Provenance.SIMULATED
    assert "Not real 108 logs" in ld.tag.source
    assert [(i.incident_id, i.complaint) for i in ld.incidents] == [("x2", "other"), ("x1", "chest_pain")]
    assert ld.dropped == {"unknown_h3_cell": 1, "unmapped_call_type:unknown_type": 1}


def test_worklist_only_hospitals_unverified(tmp_path):
    sites = _write(tmp_path / "sites.csv", ["site_id", "h3_index", "site_type", "name", "lat", "lon", "node_idx"], [
        ["s1", "c1", "hospital", "Zeta Hospital", 12.9, 77.6, 1],
        ["s2", "c2", "fuel", "Pump", 12.8, 77.5, 2],
        ["s3", "c3", "hospital", "alpha Hospital", 12.7, 77.4, 3],
    ])
    out = tmp_path / "worklist.csv"
    assert build_worklist(sites, out, date(2026, 11, 15)) == 2
    rows = list(csv.DictReader(open(out)))
    assert [r["name"] for r in rows] == ["alpha Hospital", "Zeta Hospital"]
    assert all(r["verification_status"] == "unverified" and r["ed_24x7"] == "" for r in rows)
    assert all(r["provenance"] == "REAL" and "incomplete" in r["source"] for r in rows)


def test_default_travel_provider(cfg):
    assert isinstance(default_travel_provider(cfg), TravelTimeProvider)
