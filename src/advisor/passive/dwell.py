"""Ambulance gate-dwell (offload proxy) from 108 GPS logs: CSV loader and rolling index (spec §5).

Provenance: the logs' provenance is a required argument, never inferred. Pass REAL (with the 108 file
name as source and the extract date as as_of) only for the real 108 GPS logs; anything synthetic is
SIMULATED. The rolling means returned by `RollingDwellIndex` carry that same provenance, with the sample
count and window in the source string.

The 108 log format is not known yet, so the column names and timestamp format are a parameter
(`DwellColumns`). Invalid rows are dropped and counted by reason in `DwellLoad.dropped`; they are never
silently fixed.
"""

from __future__ import annotations

import csv
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from advisor.config import AdvisorConfig, default_config
from advisor.models import DwellRecord
from advisor.provenance import Provenance, ProvenanceError, Sourced, Tagged

DROP_REASONS: tuple[str, ...] = (
    "missing_field", "bad_timestamp", "mixed_timezone", "unknown_hospital", "departed_before_arrived",
    "zero_duration", "over_max_duration",
)


@dataclass(frozen=True)
class DwellColumns:
    """Column mapping for a gate-dwell CSV. Defaults are our own names, not the 108 format (unknown yet).

    timestamp_format: a `datetime.strptime` format, or None for ISO 8601 (`datetime.fromisoformat`).
    """

    hospital_id: str = "hospital_id"
    arrived: str = "arrived"
    departed: str = "departed"
    timestamp_format: str | None = None


class DwellLoad(BaseModel):
    """Valid dwell records, the logs' provenance, and how many rows were dropped for which reason."""

    records: list[DwellRecord]
    tag: Tagged
    rows_read: int
    dropped: dict[str, int]
    max_minutes: float | None

    @property
    def dropped_total(self) -> int:
        return sum(self.dropped.values())


def _check_tag(tag: Tagged) -> None:
    if not isinstance(tag, Tagged):
        raise ProvenanceError("dwell logs need a provenance Tagged (REAL + source + as_of for the 108 logs)")
    if tag.provenance is Provenance.PLACEHOLDER:
        raise ProvenanceError("dwell logs are data, not a tunable: tag them REAL, REAL_DERIVED or SIMULATED")


def load_dwell_csv(
    path: Path | str,
    tag: Tagged,
    max_minutes: float | None,
    columns: DwellColumns | None = None,
    known_hospital_ids: set[str] | None = None,
) -> DwellLoad:
    """Load gate-dwell records from a CSV, dropping and counting invalid rows.

    tag:         provenance of the whole file (required). REAL only for the real 108 GPS logs.
    max_minutes: dwell longer than this is treated as absurd (e.g. a parked vehicle) and dropped.
                 No default on purpose: there is no sourced cut-off yet (TODO: source). Pass None to
                 keep every positive duration.
    known_hospital_ids: if given, rows for other hospital ids are dropped and counted.
    """
    _check_tag(tag)
    if max_minutes is not None and max_minutes <= 0:
        raise ValueError(f"max_minutes must be positive or None, got {max_minutes}")
    cols = columns or DwellColumns()
    path = Path(path)
    dropped: Counter[str] = Counter({r: 0 for r in DROP_REASONS})
    records: list[DwellRecord] = []
    rows_read = 0

    def parse(raw: str) -> datetime:
        return datetime.strptime(raw, cols.timestamp_format) if cols.timestamp_format else \
            datetime.fromisoformat(raw)

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = [h.strip() for h in (reader.fieldnames or [])]
        needed = [cols.hospital_id, cols.arrived, cols.departed]
        missing = [c for c in needed if c not in header]
        if missing:
            raise ValueError(f"{path}: dwell column mapping does not match the file; missing {missing}. "
                             f"Found columns: {header}")
        reader.fieldnames = header
        for row in reader:
            rows_read += 1
            hid, a_raw, d_raw = ((row.get(c) or "").strip() for c in needed)
            if not (hid and a_raw and d_raw):
                dropped["missing_field"] += 1
                continue
            if known_hospital_ids is not None and hid not in known_hospital_ids:
                dropped["unknown_hospital"] += 1
                continue
            try:
                arrived, departed = parse(a_raw), parse(d_raw)
            except ValueError:
                dropped["bad_timestamp"] += 1
                continue
            if (arrived.tzinfo is None) != (departed.tzinfo is None):
                dropped["mixed_timezone"] += 1
                continue
            minutes = (departed - arrived).total_seconds() / 60.0
            if minutes < 0:
                dropped["departed_before_arrived"] += 1
            elif minutes == 0:
                dropped["zero_duration"] += 1
            elif max_minutes is not None and minutes > max_minutes:
                dropped["over_max_duration"] += 1
            else:
                records.append(DwellRecord(hospital_id=hid, arrived=arrived, departed=departed))
    return DwellLoad(records=records, tag=tag, rows_read=rows_read, dropped=dict(dropped),
                     max_minutes=max_minutes)


class RollingDwellIndex:
    """`DwellIndex` implementation: rolling mean gate-dwell per hospital x hour band.

    Hour band = arrival hour // passive.hour_band_hours. For a query at `when`, a record counts if it
    arrived in the same hour band, no earlier than passive.dwell_window_days before `when`, and departed
    at or before `when` (so a replay never sees the future). Fewer than passive.min_dwell_samples
    matching records -> None, and the caller falls back to the ED-status lookup.
    The returned value carries the logs' provenance; REAL only if the logs were REAL.
    """

    def __init__(self, records: Iterable[DwellRecord], tag: Tagged, cfg: AdvisorConfig | None = None):
        _check_tag(tag)
        cfg = cfg or default_config()
        self.tag = tag
        self.band_hours = self._param(cfg, "passive.hour_band_hours")
        self.min_samples = self._param(cfg, "passive.min_dwell_samples")
        self.window_days = self._param(cfg, "passive.dwell_window_days")
        if not 1 <= self.band_hours <= 24:
            raise ValueError(f"passive.hour_band_hours must be in 1..24, got {self.band_hours}")
        self._by_key: dict[tuple[str, int], list[DwellRecord]] = defaultdict(list)
        self._aware: bool | None = None
        for r in records:
            aware = r.arrived.tzinfo is not None
            if self._aware is None:
                self._aware = aware
            elif aware != self._aware:
                raise ValueError("dwell records mix timezone-aware and naive timestamps")
            self._by_key[(r.hospital_id, self.band(r.arrived))].append(r)
        for recs in self._by_key.values():
            recs.sort(key=lambda r: r.arrived)
        self._arrivals = {k: [r.arrived for r in v] for k, v in self._by_key.items()}

    @classmethod
    def from_load(cls, load: DwellLoad, cfg: AdvisorConfig | None = None) -> RollingDwellIndex:
        return cls(load.records, load.tag, cfg)

    @staticmethod
    def _param(cfg: AdvisorConfig, key: str) -> int:
        value = cfg.v(key)
        if value is None:
            raise ValueError(f"config param '{key}' is null; the rolling dwell index needs it set")
        if int(value) != value or value < 1:
            raise ValueError(f"config param '{key}' must be a positive whole number, got {value}")
        return int(value)

    def band(self, t: datetime) -> int:
        return t.hour // self.band_hours

    def samples(self, hospital_id: str, when: datetime) -> list[DwellRecord]:
        """Records that count towards the mean for this hospital at `when` (see class docstring)."""
        if self._aware is not None and (when.tzinfo is not None) != self._aware:
            raise ValueError("`when` and the dwell records differ in timezone awareness")
        key = (hospital_id, self.band(when))
        recs = self._by_key.get(key)
        if not recs:
            return []
        start = when - timedelta(days=self.window_days)
        lo = bisect_left(self._arrivals[key], start)
        hi = bisect_left(self._arrivals[key], when)
        return [r for r in recs[lo:hi] if r.departed <= when]

    def mean_offload_min(self, hospital_id: str, when: datetime) -> Sourced[float] | None:
        recs = self.samples(hospital_id, when)
        if len(recs) < self.min_samples:
            return None
        mean = sum(r.minutes for r in recs) / len(recs)
        b = self.band(when)
        h0, h1 = b * self.band_hours, min(24, (b + 1) * self.band_hours)
        start = when - timedelta(days=self.window_days)
        source = (f"{self.tag.source}; mean gate-dwell of n={len(recs)} records at {hospital_id}, "
                  f"arrivals {h0:02d}:00-{h1:02d}:00, window {start.isoformat()} to {when.isoformat()} "
                  f"({self.window_days} days)")
        return Sourced[float](
            value=mean,
            tag=Tagged(provenance=self.tag.provenance, source=source, as_of=self.tag.as_of),
        )
