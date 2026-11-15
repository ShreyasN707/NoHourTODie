"""Simulated patient details for each synthetic call, and the ground truth the replay scores against.

PROVENANCE: SIMULATED. Every share used here lives in config/advisor.yaml under `sim.` and is marked
"TODO: source" unless cited there.

Why a ground truth separate from triage: the advisor only sees what an EMT could record (complaint,
vitals, ECG if done, stroke score). The replay samples what is actually wrong with the patient, and
"definitive treatment" is judged against that. So when the advisor over-triages a chest-pain patient with
no ECG as STEMI, the patient still counts as low acuity in the results, and the advisor gets no credit
for taking them to a cath lab.

What the sampled ground truth needs, per care category (config `care_categories`):
  stemi:      required + (primary_pci OR pharmacoinvasive), i.e. a cath lab or thrombolysis
  stroke:     required (24x7 ED, CT, thrombolysis). Thrombectomy is not scored separately.
  others:     required
"""

from __future__ import annotations

from dataclasses import dataclass, field

from advisor.config import AdvisorConfig
from advisor.models import Hospital

# Draws per patient (uniforms in [0, 1)). Kept fixed so changing one share in a sweep does not
# re-shuffle every other patient (common random numbers).
N_PATIENT_DRAWS = 3


@dataclass(frozen=True)
class Patient:
    """What the EMT records (complaint, vitals, ecg_stemi, stroke_score) plus the hidden truth."""

    complaint: str
    true_category: str
    vitals: dict[str, float] = field(default_factory=dict)
    ecg_stemi: bool | None = None
    stroke_score: float | None = None


def _pick(u: float, shares: dict[str, float]) -> str:
    """Pick a key with probability equal to its share, using one uniform draw."""
    total = sum(shares.values())
    if total <= 0:
        raise ValueError(f"shares must sum to a positive number, got {shares}")
    acc = 0.0
    for key, share in shares.items():
        acc += share / total
        if u < acc:
            return key
    return key  # u rounding at the top end


def sample_patient(complaint: str, u: list[float], cfg: AdvisorConfig) -> Patient:
    """Turn a call's complaint and three uniform draws into a simulated patient.

    chest_pain:      ECG shows STEMI / no STEMI / no ECG. No ECG -> true STEMI with
                     sim.stemi_share_without_ecg, otherwise not STEMI (low acuity).
    trauma:          severe (unstable vitals) with sim.trauma_severe_share, else low acuity.
    stroke_symptoms: always stroke; LVO with sim.stroke_lvo_share sets the stroke score.
    obstetric:       obstetric as is.
    other:           paediatric emergency with sim.other_paediatric_share (complaint becomes
                     'paediatric'); otherwise unstable with sim.other_unstable_share (arrest_airway),
                     else low acuity.
    """
    vit = cfg.v("sim.vitals_profiles")
    stable = dict(vit["stable"])
    if complaint == "chest_pain":
        ecg = _pick(u[0], cfg.v("sim.chest_pain_ecg_shares"))
        if ecg == "stemi":
            return Patient(complaint, "stemi", stable, ecg_stemi=True)
        if ecg == "negative":
            return Patient(complaint, "low_acuity", stable, ecg_stemi=False)
        truth = "stemi" if u[1] < cfg.v("sim.stemi_share_without_ecg") else "low_acuity"
        return Patient(complaint, truth, stable, ecg_stemi=None)
    if complaint == "trauma":
        if u[0] < cfg.v("sim.trauma_severe_share"):
            return Patient(complaint, "severe_trauma", dict(vit["unstable_trauma"]))
        return Patient(complaint, "low_acuity", stable)
    if complaint == "stroke_symptoms":
        scores = cfg.v("sim.stroke_scores")
        score = scores["lvo"] if u[0] < cfg.v("sim.stroke_lvo_share") else scores["non_lvo"]
        return Patient(complaint, "stroke", stable, stroke_score=float(score))
    if complaint == "obstetric":
        return Patient(complaint, "obstetric", stable)
    if complaint == "other":
        if u[0] < cfg.v("sim.other_paediatric_share"):
            return Patient("paediatric", "paediatric", stable)
        if u[1] < cfg.v("sim.other_unstable_share"):
            return Patient(complaint, "arrest_airway", dict(vit["unstable_medical"]))
        return Patient(complaint, "low_acuity", stable)
    raise ValueError(f"no patient model for complaint '{complaint}'")


def capability_options(category: str, cfg: AdvisorConfig) -> list[list[str]]:
    """Capability sets that each count as definitive care for a TRUE category (any one is enough)."""
    cat = cfg.care_categories[category]
    if cat.rule == "stemi":
        return [cat.required + cat.primary_pci, cat.required + cat.pharmacoinvasive]
    return [list(cat.required)]


def can_treat(h: Hospital, category: str, cfg: AdvisorConfig) -> bool:
    """True if this hospital has a capability set that is definitive care for the true category."""
    return any(h.has_all(opt) for opt in capability_options(category, cfg))


def critical_categories(cfg: AdvisorConfig) -> list[str]:
    """Care categories with lambda 0 (time-critical), except arrest_airway (stabilise at nearest ED)."""
    lam = cfg.v("scoring.lambda_by_category")
    return sorted(c for c, v in lam.items() if float(v) == 0.0 and c != "arrest_airway")
