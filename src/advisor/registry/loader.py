"""Hospital registry loader (static tier, spec §5).

Reads two hand-maintained CSVs in registry/:

  hospitals.csv     one row per hospital:
                    hospital_id, name, lat, lon, ownership, empanelment, icu_beds_total,
                    ventilators_total, provenance, source, as_of
  capabilities.csv  long format, one row per hospital x capability:
                    hospital_id, capability, available, hours, provenance, source, as_of

Provenance: every row carries its own provenance/source/as_of; nothing here assigns a tag that the
file does not state. A capability that is not listed for a hospital stays absent from
`Hospital.capabilities`, i.e. "unknown", which `Hospital.has()` treats as absent. No defaults are filled.

Every problem fails loudly with file, line number and field (ProvenanceError for provenance problems,
ValueError for everything else; ProvenanceError is itself a ValueError).
"""

from __future__ import annotations

import csv
import math
from collections import Counter
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ValidationError

from advisor.config import ROOT_DIR
from advisor.models import CAPABILITY_KEYS, Capability, Hospital
from advisor.provenance import Provenance, ProvenanceError, Tagged

REGISTRY_DIR = ROOT_DIR / "registry"
HOSPITALS_CSV = REGISTRY_DIR / "hospitals.csv"
CAPABILITIES_CSV = REGISTRY_DIR / "capabilities.csv"

HOSPITAL_COLUMNS: tuple[str, ...] = (
    "hospital_id", "name", "lat", "lon", "ownership", "empanelment", "icu_beds_total",
    "ventilators_total", "provenance", "source", "as_of",
)
CAPABILITY_COLUMNS: tuple[str, ...] = (
    "hospital_id", "capability", "available", "hours", "provenance", "source", "as_of",
)
OWNERSHIP_VALUES: tuple[str, ...] = ("govt", "private")
HOURS_VALUES: tuple[str, ...] = ("24x7", "daytime", "unknown")
_TRUE = ("true", "yes", "1")
_FALSE = ("false", "no", "0")


class RegistrySummary(BaseModel):
    """Provenance counts for the API and slides. Counts only; derived from the loaded rows."""

    hospitals: int
    location_provenance: dict[str, int]
    capability_rows: int
    capability_provenance: dict[str, int]
    hospitals_without_capabilities: int
    hospitals_file: str
    capabilities_file: str


class RegistryLoad(BaseModel):
    """Loaded registry: hospitals (each with its own tags) plus a provenance summary."""

    hospitals: list[Hospital]
    summary: RegistrySummary


# ---------------------------------------------------------------- row helpers

def _where(path: Path, line: int, field: str | None = None) -> str:
    return f"{path}, line {line}" + (f", field '{field}'" if field else "")


def _read_rows(path: Path, required: tuple[str, ...]) -> Iterator[tuple[int, dict[str, str]]]:
    """Yield (line number, stripped row). Fails if the header is missing any required column."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"registry file not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in required if c not in header]
        if missing:
            raise ValueError(f"{path}: header is missing columns {missing} (found {header})")
        reader.fieldnames = header
        for row in reader:
            if None in row:
                raise ValueError(f"{_where(path, reader.line_num)}: more values than header columns")
            clean = {k: (v or "").strip() for k, v in row.items()}
            if not any(clean.values()):
                continue  # blank line
            yield reader.line_num, clean


def _required(row: dict[str, str], field: str, path: Path, line: int) -> str:
    value = row.get(field, "")
    if not value:
        raise ValueError(f"{_where(path, line, field)}: value is required")
    return value


def _tag(row: dict[str, str], path: Path, line: int) -> Tagged:
    """Build the row's provenance tag. Missing provenance/source, or REAL without as_of, fails loudly."""
    raw_prov = row.get("provenance", "")
    if not raw_prov:
        raise ProvenanceError(f"{_where(path, line, 'provenance')}: provenance is required")
    try:
        prov = Provenance(raw_prov.upper())
    except ValueError:
        raise ProvenanceError(
            f"{_where(path, line, 'provenance')}: '{raw_prov}' is not one of "
            f"{[p.value for p in Provenance]}"
        ) from None
    if not row.get("source"):
        raise ProvenanceError(f"{_where(path, line, 'source')}: source is required")
    as_of: date | None = None
    if row.get("as_of"):
        try:
            as_of = date.fromisoformat(row["as_of"])
        except ValueError:
            raise ValueError(f"{_where(path, line, 'as_of')}: '{row['as_of']}' is not YYYY-MM-DD") from None
    if prov in (Provenance.REAL, Provenance.REAL_DERIVED) and as_of is None:
        raise ProvenanceError(
            f"{_where(path, line, 'as_of')}: {prov.value} rows need a verification/retrieval date"
        )
    return Tagged(provenance=prov, source=row["source"], as_of=as_of)


def _coord(row: dict[str, str], field: str, limit: float, path: Path, line: int) -> float:
    raw = _required(row, field, path, line)
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{_where(path, line, field)}: '{raw}' is not a number") from None
    if not math.isfinite(value) or not -limit <= value <= limit:
        raise ValueError(f"{_where(path, line, field)}: {value} is outside [-{limit}, {limit}]")
    return value


def _optional_count(row: dict[str, str], field: str, path: Path, line: int) -> int | None:
    raw = row.get(field, "")
    if not raw:
        return None  # not known; never defaulted
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{_where(path, line, field)}: '{raw}' is not a whole number") from None
    if value < 0:
        raise ValueError(f"{_where(path, line, field)}: {value} is negative")
    return value


def _parse_bool(row: dict[str, str], field: str, path: Path, line: int) -> bool:
    raw = _required(row, field, path, line).lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{_where(path, line, field)}: '{raw}' is not true/false")


# ---------------------------------------------------------------- loaders

def load_hospitals_csv(path: Path | str = HOSPITALS_CSV) -> list[Hospital]:
    """Load hospitals.csv. Capabilities are empty here; see `load_registry`.

    Location/ownership provenance comes from each row's provenance/source/as_of into `location_tag`.
    """
    path = Path(path)
    hospitals: list[Hospital] = []
    seen: dict[str, int] = {}
    for line, row in _read_rows(path, HOSPITAL_COLUMNS):
        hid = _required(row, "hospital_id", path, line)
        if hid in seen:
            raise ValueError(f"{_where(path, line, 'hospital_id')}: duplicate id '{hid}' (first on line "
                             f"{seen[hid]})")
        seen[hid] = line
        ownership = _required(row, "ownership", path, line).lower()
        if ownership not in OWNERSHIP_VALUES:
            raise ValueError(f"{_where(path, line, 'ownership')}: '{ownership}' is not one of {OWNERSHIP_VALUES}")
        try:
            hospitals.append(Hospital(
                hospital_id=hid,
                name=_required(row, "name", path, line),
                lat=_coord(row, "lat", 90.0, path, line),
                lon=_coord(row, "lon", 180.0, path, line),
                ownership=ownership,
                location_tag=_tag(row, path, line),
                empanelment=[e.strip() for e in row.get("empanelment", "").split(";") if e.strip()],
                icu_beds_total=_optional_count(row, "icu_beds_total", path, line),
                ventilators_total=_optional_count(row, "ventilators_total", path, line),
            ))
        except ValidationError as e:
            raise ValueError(f"{_where(path, line)}: {e}") from None
    return hospitals


def load_capabilities_csv(path: Path | str, hospital_ids: set[str]) -> dict[str, dict[str, Capability]]:
    """Load capabilities.csv into {hospital_id: {capability: Capability}}.

    Each row carries its own provenance. Rows for hospital ids not in `hospital_ids`, capability keys not
    in CAPABILITY_KEYS, or a repeated hospital x capability pair all fail loudly.
    """
    path = Path(path)
    out: dict[str, dict[str, Capability]] = {}
    seen: dict[tuple[str, str], int] = {}
    for line, row in _read_rows(path, CAPABILITY_COLUMNS):
        hid = _required(row, "hospital_id", path, line)
        if hid not in hospital_ids:
            raise ValueError(f"{_where(path, line, 'hospital_id')}: '{hid}' is not in hospitals.csv")
        cap = _required(row, "capability", path, line)
        if cap not in CAPABILITY_KEYS:
            raise ValueError(f"{_where(path, line, 'capability')}: unknown capability '{cap}'; "
                             f"allowed: {list(CAPABILITY_KEYS)}")
        if (hid, cap) in seen:
            raise ValueError(f"{_where(path, line, 'capability')}: duplicate '{cap}' for '{hid}' (first on "
                             f"line {seen[(hid, cap)]})")
        seen[(hid, cap)] = line
        hours = (row.get("hours") or "unknown").lower()
        if hours not in HOURS_VALUES:
            raise ValueError(f"{_where(path, line, 'hours')}: '{hours}' is not one of {HOURS_VALUES}")
        tag = _tag(row, path, line)
        out.setdefault(hid, {})[cap] = Capability(
            provenance=tag.provenance, source=tag.source, as_of=tag.as_of,
            available=_parse_bool(row, "available", path, line), hours=hours,
        )
    return out


def summarize(hospitals: list[Hospital], hospitals_file: str = "", capabilities_file: str = "") -> RegistrySummary:
    """Count rows by provenance tag. Every provenance value is listed, including zeros."""
    loc = Counter(h.location_tag.provenance.value for h in hospitals)
    caps = Counter(c.provenance.value for h in hospitals for c in h.capabilities.values())
    return RegistrySummary(
        hospitals=len(hospitals),
        location_provenance={p.value: loc.get(p.value, 0) for p in Provenance},
        capability_rows=sum(caps.values()),
        capability_provenance={p.value: caps.get(p.value, 0) for p in Provenance},
        hospitals_without_capabilities=sum(1 for h in hospitals if not h.capabilities),
        hospitals_file=hospitals_file,
        capabilities_file=capabilities_file,
    )


def load_registry(
    hospitals_csv: Path | str = HOSPITALS_CSV, capabilities_csv: Path | str = CAPABILITIES_CSV
) -> RegistryLoad:
    """Load both registry files and attach capabilities to hospitals. Returns hospitals + provenance summary.

    The committed registry/ files are header-only templates, so this returns zero hospitals until the
    team fills them from phone/website verification (see registry/README.md).
    """
    hospitals = load_hospitals_csv(hospitals_csv)
    caps = load_capabilities_csv(capabilities_csv, {h.hospital_id for h in hospitals})
    for h in hospitals:
        h.capabilities = caps.get(h.hospital_id, {})
    return RegistryLoad(
        hospitals=hospitals,
        summary=summarize(hospitals, str(hospitals_csv), str(capabilities_csv)),
    )
