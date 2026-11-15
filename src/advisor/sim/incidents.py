"""Incident stream for the SimPy replay, from GoldenHour's synthetic year of 108 calls.

PROVENANCE: SIMULATED, always. These are NOT real 108 call logs: 108 logs are not public, and the
GoldenHour data package says so itself (data/raw/goldenhour_data/README.md). The calls are grounded in:
  - where:      WorldPop 2020 population; trauma half population, half OSM major-road length
  - how many:   CAG Report No. 3 of 2020 statewide volume (41.94 lakh trips 2014-19), scaled by population
  - call mix:   GoldenHour config (pregnancy, trauma, cardiac, stroke, respiratory, other)
This loader takes no provenance argument on purpose, so these calls can never be tagged REAL. When real
C-DAC NG-ERSS call logs arrive, add a separate loader for them.

Location is the H3 cell centre (res 8, about 0.74 km^2 in Bengaluru), not the exact scene.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from advisor.config import ROOT_DIR, AdvisorConfig, default_config
from advisor.provenance import Provenance, Tagged

GH_DIR = ROOT_DIR / "data" / "raw" / "goldenhour_data"
CALLS_BLR = GH_DIR / "01_demand" / "calls_blr_synthetic_year.csv"
CELLS_BLR = GH_DIR / "02_geography" / "hexagon_cells_blr.csv"

SYNTHETIC_CALLS_TAG = Tagged(
    provenance=Provenance.SIMULATED,
    source=("GoldenHour synthetic 108 call year 2025-11-01..2026-10-31 (pipeline run 15 Nov 2026): "
            "locations from WorldPop 2020 + OSM major-road exposure, volume scaled from CAG Report No. 3 "
            "of 2020. Not real 108 logs."),
)


class Incident(BaseModel):
    incident_id: str
    when: datetime
    lat: float
    lon: float
    h3_index: str
    zone: str
    call_type: str  # GoldenHour call type
    complaint: str  # advisor complaint (config sim.call_type_to_complaint)


@dataclass
class IncidentLoad:
    incidents: list[Incident]
    tag: Tagged = field(default_factory=lambda: SYNTHETIC_CALLS_TAG.model_copy())
    rows_read: int = 0
    dropped: Counter = field(default_factory=Counter)


def load_cell_centres(cells_csv: Path | str = CELLS_BLR) -> dict[str, tuple[float, float]]:
    with open(cells_csv, newline="") as f:
        return {r["h3_index"]: (float(r["lat"]), float(r["lon"])) for r in csv.DictReader(f)}


def load_goldenhour_calls(
    calls_csv: Path | str = CALLS_BLR,
    cells_csv: Path | str = CELLS_BLR,
    cfg: AdvisorConfig | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> IncidentLoad:
    """Synthetic calls in [start, end), placed at their H3 cell centre. Dropped rows are counted, not hidden."""
    cfg = cfg or default_config()
    mapping: dict[str, str] = cfg.v("sim.call_type_to_complaint")
    centres = load_cell_centres(cells_csv)
    out = IncidentLoad(incidents=[])
    with open(calls_csv, newline="") as f:
        for r in csv.DictReader(f):
            out.rows_read += 1
            when = datetime.fromisoformat(r["timestamp"])
            if (start and when < start) or (end and when >= end):
                continue
            centre = centres.get(r["h3_index"])
            if centre is None:
                out.dropped["unknown_h3_cell"] += 1
                continue
            complaint = mapping.get(r["call_type"])
            if complaint is None:
                out.dropped[f"unmapped_call_type:{r['call_type']}"] += 1
                continue
            out.incidents.append(Incident(
                incident_id=r["call_id"], when=when, lat=centre[0], lon=centre[1], h3_index=r["h3_index"],
                zone=r["zone"], call_type=r["call_type"], complaint=complaint,
            ))
    out.incidents.sort(key=lambda i: i.when)
    return out
