"""Build a phone/website verification worklist of candidate hospitals from OSM (via GoldenHour).

This is a WORKLIST, not the registry. It is never loaded by the advisor. The team verifies each row
(24x7 ED? cath lab? CT? ...) and copies confirmed hospitals into registry/hospitals.csv and
registry/capabilities.csv with source and verification date.

Locations and names are REAL (OpenStreetMap, ODbL, (c) OpenStreetMap contributors). Caveats:
  - Incomplete: GoldenHour kept at most ONE site per H3 res-8 cell (~0.74 km^2), so hospitals sharing
    a cell with another hospital are missing. Re-extract from the full OSM file for a complete list.
  - OSM amenity=hospital includes clinics, eye/dental/skin centres and nursing homes, many of which
    do not receive emergencies. Ownership is not in this data and is left blank.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from advisor.sim.incidents import GH_DIR

SITES_BLR = GH_DIR / "02_geography" / "standby_sites_blr.csv"
WORKLIST_COLUMNS = ["hospital_id", "name", "lat", "lon", "provenance", "source", "as_of",
                    "verification_status", "ownership", "ed_24x7", "notes"]


def build_worklist(sites_csv: Path | str, out_csv: Path | str, osm_as_of: date) -> int:
    """Write hospital rows from GoldenHour's standby sites. Returns the number of rows written."""
    source = ("OpenStreetMap amenity=hospital via GoldenHour standby_sites (thinned to one site per H3 "
              "res-8 cell, so incomplete)")
    with open(sites_csv, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["site_type"] == "hospital"]
    rows.sort(key=lambda r: r["name"].lower())
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=WORKLIST_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({
                "hospital_id": f"osm-{r['site_id']}", "name": r["name"], "lat": r["lat"], "lon": r["lon"],
                "provenance": "REAL", "source": source, "as_of": osm_as_of.isoformat(),
                "verification_status": "unverified", "ownership": "", "ed_24x7": "", "notes": "",
            })
    return len(rows)


if __name__ == "__main__":
    from advisor.config import ROOT_DIR

    # OSM Southern Zone extract downloaded Nov 2026; pipeline run 15 Nov 2026 (GoldenHour data README).
    n = build_worklist(SITES_BLR, ROOT_DIR / "registry" / "osm_hospital_candidates_blr.csv", date(2026, 11, 15))
    print(f"wrote {n} candidate hospitals")
