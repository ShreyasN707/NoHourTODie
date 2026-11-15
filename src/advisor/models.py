"""Shared data models (spec §8). Every module codes against these; change them only in this file.

Capability keys used across the advisor (must match config/advisor.yaml and registry/capabilities.csv):
  ed_24x7, ct_24x7, pci_24x7, thrombolysis, thrombectomy, trauma_ot, neurosurgery, orthopaedics,
  blood_bank, obstetric_emergency, nicu, picu, burns_unit
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from advisor.provenance import Provenance, Tagged

CAPABILITY_KEYS: tuple[str, ...] = (
    "ed_24x7", "ct_24x7", "pci_24x7", "thrombolysis", "thrombectomy", "trauma_ot", "neurosurgery",
    "orthopaedics", "blood_bank", "obstetric_emergency", "nicu", "picu", "burns_unit",
)


class LatLon(BaseModel):
    lat: float
    lon: float


# ---------------------------------------------------------------- hospital tiers (spec §5)

class Capability(Tagged):
    available: bool
    hours: str = "unknown"  # "24x7" | "daytime" | "unknown"


class Hospital(BaseModel):
    """Static tier. Location/ownership provenance in `location_tag`; each capability carries its own."""

    hospital_id: str
    name: str
    lat: float
    lon: float
    ownership: str  # govt | private | unknown (directory-seeded rows whose category text is ambiguous)
    location_tag: Tagged
    capabilities: dict[str, Capability] = Field(default_factory=dict)
    empanelment: list[str] = Field(default_factory=list)
    icu_beds_total: int | None = None
    ventilators_total: int | None = None

    def has(self, cap: str) -> bool:
        """True only if the capability is recorded as available. Unknown counts as absent."""
        c = self.capabilities.get(cap)
        return bool(c and c.available)

    def has_all(self, caps: list[str]) -> bool:
        return all(self.has(c) for c in caps)


class EDStatus(StrEnum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class HospitalStatus(Tagged):
    """Per-shift / live tier. SIMULATED until hospitals report (spec §5)."""

    hospital_id: str
    timestamp: datetime
    ed_status: EDStatus | None = None
    free_icu: int | None = None
    free_ventilators: int | None = None
    diversion: bool = False
    offload_wait_min: float | None = None
    down_capabilities: list[str] = Field(default_factory=list)  # e.g. ["pci_24x7"] when cath lab is down


class OutcomeEvent(BaseModel):
    """Passive tier: one accept/reject from a pre-alert or crew log. Feeds reliability scoring."""

    hospital_id: str
    timestamp: datetime
    accepted: bool
    reason_code: str
    source: Provenance = Provenance.SIMULATED


class DwellRecord(BaseModel):
    """Passive tier: ambulance GPS dwell at a hospital gate (offload proxy). From 108 logs when provided."""

    hospital_id: str
    arrived: datetime
    departed: datetime

    @property
    def minutes(self) -> float:
        return (self.departed - self.arrived).total_seconds() / 60.0


# ---------------------------------------------------------------- request / response

class Assessment(BaseModel):
    """Structured EMT assessment. Must work from EMT input alone (spec §4 step 1).

    complaint: chest_pain | stroke_symptoms | trauma | obstetric | paediatric | cardiac_arrest |
               airway | other
    vitals keys (all optional): gcs, sbp, rr, hr, spo2
    """

    case_id: str
    scene_lat: float
    scene_lon: float
    base_lat: float
    base_lon: float
    complaint: str
    vitals: dict[str, float] = Field(default_factory=dict)
    onset_minutes: int | None = None
    minutes_since_first_contact: float = 0.0
    ecg_stemi: bool | None = None
    stroke_score: float | None = None
    when: datetime


class ScoreBreakdown(BaseModel):
    """score = travel + ed_wait + p_cannot_treat * transfer_delay + lam * coverage_lost (spec §4)."""

    travel_min: float
    ed_wait_min: float
    p_cannot_treat: float
    transfer_delay_min: float
    lam: float
    coverage_lost_min: float
    total: float


class RankedHospital(BaseModel):
    hospital_id: str
    name: str
    score: float
    components: ScoreBreakdown
    capable: bool
    notes: list[str] = Field(default_factory=list)
    inputs_provenance: dict[str, Provenance] = Field(default_factory=dict)


class ExcludedHospital(BaseModel):
    """Not recommended, with why. `components` is filled when we could still score it, so the demo can
    show e.g. 'nearest hospital, no cath lab: 68 min' next to the recommended PCI hub."""

    hospital_id: str
    name: str
    reason: str  # e.g. "missing:pci_24x7", "diversion", "over_travel_cap"
    components: ScoreBreakdown | None = None


class Recommendation(BaseModel):
    case_id: str
    care_category: str
    route: str | None = None  # e.g. "primary_pci", "pharmacoinvasive", "lvo_thrombectomy"
    ranked: list[RankedHospital]
    excluded: list[ExcludedHospital] = Field(default_factory=list)
    reason_code: str  # machine-readable, logged for audit (spec §4 guardrails)
    reason: str  # the same in plain words, shown to the crew
    lambda_used: float
    inputs_provenance: dict[str, Provenance]  # weakest provenance seen per input across the ranking
    non_real_inputs_used: bool  # True if any input was SIMULATED or PLACEHOLDER -> UI shows the banner
