import csv

from advisor.provenance import Provenance
from advisor.registry.demo import DEMO_SOURCE, build_demo_registry


def _cells(tmp_path, n=50):
    p = tmp_path / "cells.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["h3_index", "lat", "lon", "pop"])
        for i in range(n):
            w.writerow([f"c{i}", 12.8 + i * 0.01, 77.5 + i * 0.01, 100 + i])
    return p


def test_demo_registry_is_fictional_simulated_and_deterministic(tmp_path, cfg):
    cells = _cells(tmp_path)
    a = build_demo_registry(cfg, cells)
    b = build_demo_registry(cfg, cells)
    assert [h.model_dump() for h in a] == [h.model_dump() for h in b]
    assert len(a) == cfg.v("sim.demo_hospital_count")
    assert len({(h.lat, h.lon) for h in a}) == len(a)  # no two hospitals in one cell
    for h in a:
        assert "fictional" in h.name and h.hospital_id.startswith("demo-")
        assert h.location_tag.provenance == Provenance.SIMULATED and h.location_tag.source == DEMO_SOURCE
        assert h.has("ed_24x7")
        assert all(c.provenance == Provenance.SIMULATED for c in h.capabilities.values())
