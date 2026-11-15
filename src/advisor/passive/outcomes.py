"""Accept/reject outcome history (passive tier, spec §5): a small JSONL-backed store of `OutcomeEvent`.

Used by reliability scoring and the pre-alert fallback. Provenance: every stored event states its own
`source` (REAL / REAL_DERIVED / SIMULATED). When loading a file, a record without an explicit `source`
fails loudly rather than taking the model's SIMULATED default, so nothing gets a tag it did not carry.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from advisor.models import OutcomeEvent
from advisor.provenance import Provenance, ProvenanceError

OUTCOME_FIELDS: tuple[str, ...] = ("hospital_id", "timestamp", "accepted", "reason_code", "source")


def _event(raw: dict, where: str) -> OutcomeEvent:
    if not raw.get("source"):
        raise ProvenanceError(f"{where}, field 'source': outcome events must state their provenance")
    if raw["source"] == Provenance.PLACEHOLDER.value:
        raise ProvenanceError(f"{where}, field 'source': an outcome is data, not a PLACEHOLDER")
    try:
        return OutcomeEvent.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"{where}: {e}") from None


def load_outcomes_csv(path: Path | str) -> list[OutcomeEvent]:
    """Read events from a CSV with columns hospital_id, timestamp (ISO 8601), accepted, reason_code, source."""
    path = Path(path)
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in OUTCOME_FIELDS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: header is missing columns {missing}")
        return [_event({k: (v or "").strip() for k, v in row.items()}, f"{path}, line {reader.line_num}")
                for row in reader]


class OutcomeStore:
    """In-memory list of outcome events, optionally persisted to an append-only JSONL file."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else None
        self._events: list[OutcomeEvent] = []
        if self.path is not None and self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                for n, line in enumerate(f, start=1):
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError as e:
                        raise ValueError(f"{self.path}, line {n}: not valid JSON ({e.msg})") from None
                    self._events.append(_event(raw, f"{self.path}, line {n}"))

    def append(self, event: OutcomeEvent) -> None:
        """Add an event in memory and, if the store has a file, append it there too."""
        if event.source is Provenance.PLACEHOLDER:
            raise ProvenanceError("an outcome is data, not a PLACEHOLDER")
        self._events.append(event)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")

    def extend(self, events: list[OutcomeEvent]) -> None:
        for e in events:
            self.append(e)

    def events(self, hospital_id: str | None = None, since: datetime | None = None,
               until: datetime | None = None) -> list[OutcomeEvent]:
        """Events in time order, optionally for one hospital and within [since, until)."""
        out = [e for e in self._events
               if (hospital_id is None or e.hospital_id == hospital_id)
               and (since is None or e.timestamp >= since)
               and (until is None or e.timestamp < until)]
        return sorted(out, key=lambda e: e.timestamp)

    def provenance_counts(self) -> dict[str, int]:
        c = Counter(e.source.value for e in self._events)
        return {p.value: c.get(p.value, 0) for p in Provenance}

    def __len__(self) -> int:
        return len(self._events)
