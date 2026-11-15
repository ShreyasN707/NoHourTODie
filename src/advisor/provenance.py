"""Provenance tags (spec §2). Every dataset, field and parameter carries one.

REAL          From a named public source or our own manual/phone verification.
REAL_DERIVED  Computed from real data; the method is stored in `source`.
SIMULATED     No real source accessible to us; grounded in a cited study or audit.
PLACEHOLDER   Tunable value awaiting calibration or clinical sign-off; lives in config.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, model_validator

T = TypeVar("T")

# Shown by the API, the web screen and the simulation results whenever demo or simulated data is in use
# (docs/advisor_contract.md). One definition so the wording never drifts.
DEMO_BANNER = (
    "SIMULATED DATA: fictional demo hospitals, simulated ED status and placeholder travel speeds. "
    "Not live hospital data."
)


class ProvenanceError(ValueError):
    """Raised when data arrives without the provenance the honesty rules require."""


class Provenance(StrEnum):
    REAL = "REAL"
    REAL_DERIVED = "REAL_DERIVED"
    SIMULATED = "SIMULATED"
    PLACEHOLDER = "PLACEHOLDER"


class Tagged(BaseModel):
    """Provenance metadata. REAL and REAL_DERIVED rows must say when they were retrieved."""

    provenance: Provenance
    source: str
    as_of: date | None = None

    @model_validator(mode="after")
    def _check(self) -> Tagged:
        if not self.source or not self.source.strip():
            raise ProvenanceError("provenance source is empty")
        if self.provenance in (Provenance.REAL, Provenance.REAL_DERIVED) and self.as_of is None:
            raise ProvenanceError(f"{self.provenance.value} data needs an as_of date (source: {self.source})")
        return self


class Sourced(BaseModel, Generic[T]):
    """A value plus its provenance. Loaders return these, never bare values."""

    value: T
    tag: Tagged

    @property
    def provenance(self) -> Provenance:
        return self.tag.provenance
