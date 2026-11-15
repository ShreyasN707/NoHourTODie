"""Loader for the data.gov.in "Hospital Directory (National Health Portal)" CSV.

Provenance: rows are tagged REAL with source = the dataset URL and as_of = the retrieval date, which the
caller must supply. Only name, location and (where the category text allows) ownership are taken.
No capabilities come from this source: the directory is self-reported and does not say what a hospital
can do around the clock, so capabilities stay unknown until verified into registry/capabilities.csv.

The column names in `NhpColumns` are ASSUMED from the published dataset description and have NOT been
checked against the real file. Check them (and the ownership category texts) when the file is
downloaded, and pass a corrected `NhpColumns` if they differ. Any missing mapped column fails loudly.

Many records lack coordinates (spec §5); those rows are skipped and counted, never guessed.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pydantic import BaseModel

from advisor.models import Hospital
from advisor.provenance import Provenance, Tagged

NHP_DATASET_URL = "https://data.gov.in/catalog/hospital-directory-national-health-portal"
OWNERSHIP_UNKNOWN = "unknown"


@dataclass(frozen=True)
class NhpColumns:
    """Column mapping for the NHP CSV. Defaults are assumptions; verify against the real file.

    Coordinates come either from one combined "lat, lon" column (`coordinates`) or from separate
    `lat` / `lon` columns; set the unused option to None.
    `ownership_terms` maps a lower-case substring of the category text to govt/private; the first
    matching term wins. No match -> ownership "unknown" (counted in the result).
    """

    name: str = "Hospital_Name"
    coordinates: str | None = "Location_Coordinates"
    lat: str | None = None
    lon: str | None = None
    ownership: str | None = "Hospital_Category"
    state: str | None = "State"
    record_id: str | None = "Sr_No"
    ownership_terms: tuple[tuple[str, str], ...] = field(
        default=(("government", "govt"), ("public", "govt"), ("private", "private"))
    )

    def required_columns(self) -> list[str]:
        cols = [self.name, self.coordinates, self.lat, self.lon, self.ownership, self.state, self.record_id]
        return [c for c in cols if c]


class NhpLoad(BaseModel):
    """Hospitals mapped from the NHP directory, plus counts of what was kept, skipped and why."""

    hospitals: list[Hospital]
    rows_read: int
    skipped_other_state: int
    skipped_no_coordinates: int
    skipped_bad_coordinates: int
    skipped_no_name: int
    ownership_unknown: int
    source: str
    as_of: date


def _parse_coords(row: dict[str, str], cols: NhpColumns) -> tuple[float, float] | None:
    """Return (lat, lon); None if blank. Raises ValueError if present but unparseable/out of range."""
    if cols.coordinates:
        raw = row.get(cols.coordinates, "").strip()
        if not raw:
            return None
        parts = [p.strip() for p in raw.replace(";", ",").split(",")]
        if len(parts) != 2:
            raise ValueError(raw)
        lat_s, lon_s = parts
    else:
        lat_s, lon_s = row.get(cols.lat or "", "").strip(), row.get(cols.lon or "", "").strip()
        if not lat_s and not lon_s:
            return None
    lat, lon = float(lat_s), float(lon_s)
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"{lat},{lon}")
    if lat == 0.0 and lon == 0.0:
        return None  # 0,0 is a "no value" sentinel, not a location in India
    return lat, lon


def _ownership(text: str, cols: NhpColumns) -> str:
    low = text.lower()
    for term, value in cols.ownership_terms:
        if term in low:
            return value
    return OWNERSHIP_UNKNOWN


def load_nhp_directory(
    path: Path | str,
    as_of: date,
    columns: NhpColumns | None = None,
    state: str | None = None,
    id_prefix: str = "nhp-",
) -> NhpLoad:
    """Map a local copy of the NHP hospital directory CSV to `Hospital` objects tagged REAL.

    path:    local CSV downloaded from NHP_DATASET_URL.
    as_of:   date the file was retrieved (required; REAL data must be dated).
    columns: column mapping; defaults are unverified assumptions (see module docstring).
    state:   keep only rows whose state column equals this (case-insensitive), e.g. "Karnataka".
    Ownership is "govt"/"private" when the category text matches `columns.ownership_terms`, otherwise
    "unknown". Hospitals have no capabilities. Skipped rows are counted by reason in the result.
    """
    if not isinstance(as_of, date):
        raise TypeError("as_of must be the retrieval date (datetime.date)")
    cols = columns or NhpColumns()
    if cols.coordinates is None and not (cols.lat and cols.lon):
        raise ValueError("NhpColumns needs either `coordinates` or both `lat` and `lon`")
    if state is not None and not cols.state:
        raise ValueError("state filter requested but NhpColumns.state is None")
    path = Path(path)
    tag = Tagged(provenance=Provenance.REAL, source=NHP_DATASET_URL, as_of=as_of)

    hospitals: list[Hospital] = []
    seen: dict[str, int] = {}
    counts = dict(rows_read=0, skipped_other_state=0, skipped_no_coordinates=0, skipped_bad_coordinates=0,
                  skipped_no_name=0, ownership_unknown=0)
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in cols.required_columns() if c not in header]
        if missing:
            raise ValueError(f"{path}: NHP column mapping does not match the file; missing {missing}. "
                             f"Found columns: {header}")
        reader.fieldnames = header
        for row in reader:
            row = {k: (v or "") for k, v in row.items() if k is not None}
            counts["rows_read"] += 1
            line = reader.line_num
            if state is not None and row[cols.state or ""].strip().lower() != state.strip().lower():
                counts["skipped_other_state"] += 1
                continue
            name = row[cols.name].strip()
            if not name:
                counts["skipped_no_name"] += 1
                continue
            try:
                coords = _parse_coords(row, cols)
            except ValueError:
                counts["skipped_bad_coordinates"] += 1
                continue
            if coords is None:
                counts["skipped_no_coordinates"] += 1
                continue
            rec = row[cols.record_id].strip() if cols.record_id else ""
            hid = f"{id_prefix}{rec}" if rec else f"{id_prefix}line{line}"
            if hid in seen:
                raise ValueError(f"{path}, line {line}, field '{cols.record_id}': duplicate id '{hid}' "
                                 f"(first on line {seen[hid]})")
            seen[hid] = line
            ownership = _ownership(row[cols.ownership], cols) if cols.ownership else OWNERSHIP_UNKNOWN
            if ownership == OWNERSHIP_UNKNOWN:
                counts["ownership_unknown"] += 1
            hospitals.append(Hospital(
                hospital_id=hid, name=name, lat=coords[0], lon=coords[1], ownership=ownership,
                location_tag=tag.model_copy(),
            ))
    return NhpLoad(hospitals=hospitals, source=NHP_DATASET_URL, as_of=as_of, **counts)
