"""Map an EMT assessment to a care category (spec §4 step 2). Works from EMT input alone.

Order of checks, in plain words:
  1. Cardiac arrest or airway problem       -> arrest_airway (nearest ED to stabilise)
  2. Trauma with unstable vitals            -> severe_trauma; other trauma -> low_acuity
  3. Chest pain with STEMI on ECG           -> stemi. No ECG: stemi if config says so (over-triage)
  4. Stroke symptoms                        -> stroke (LVO bypass is decided later by the engine)
  5. Obstetric / paediatric                 -> obstetric / paediatric (capability first)
  6. Anything else: unstable vitals -> arrest_airway, otherwise low_acuity

Unstable vitals use the US field-triage physiologic criteria in config (PLACEHOLDER, not validated
for India). All of this needs clinical sign-off before use.
"""

from __future__ import annotations

from advisor.config import AdvisorConfig
from advisor.models import Assessment

COMPLAINTS = (
    "chest_pain", "stroke_symptoms", "trauma", "obstetric", "paediatric", "cardiac_arrest", "airway", "other",
)


def unstable_vitals(vitals: dict[str, float], cfg: AdvisorConfig) -> list[str]:
    """Names of the physiologic criteria met. Missing vitals are not counted as abnormal."""
    t = cfg.v("rules.unstable_vitals")
    hits = []
    if (gcs := vitals.get("gcs")) is not None and gcs <= t["gcs_max"]:
        hits.append(f"gcs<={t['gcs_max']}")
    if (sbp := vitals.get("sbp")) is not None and sbp < t["sbp_min"]:
        hits.append(f"sbp<{t['sbp_min']}")
    if (rr := vitals.get("rr")) is not None and (rr < t["rr_min"] or rr > t["rr_max"]):
        hits.append(f"rr outside {t['rr_min']}-{t['rr_max']}")
    return hits


def classify(a: Assessment, cfg: AdvisorConfig) -> tuple[str, str]:
    """Return (care_category, plain-words reason)."""
    if a.complaint not in COMPLAINTS:
        raise ValueError(f"unknown complaint '{a.complaint}'; expected one of {COMPLAINTS}")
    unstable = unstable_vitals(a.vitals, cfg)

    if a.complaint in ("cardiac_arrest", "airway"):
        return "arrest_airway", f"{a.complaint}: stabilise at nearest ED"
    if a.complaint == "trauma":
        if unstable:
            return "severe_trauma", f"trauma with unstable vitals ({', '.join(unstable)})"
        return "low_acuity", "trauma without physiologic triage criteria"
    if a.complaint == "chest_pain":
        if a.ecg_stemi:
            return "stemi", "chest pain, STEMI on ECG"
        if a.ecg_stemi is None and cfg.v("rules.chest_pain_without_ecg_as_stemi"):
            return "stemi", "chest pain, no ECG available: treated as suspected STEMI (over-triage)"
        return "low_acuity", "chest pain, ECG negative for STEMI"
    if a.complaint == "stroke_symptoms":
        return "stroke", "suspected stroke"
    if a.complaint in ("obstetric", "paediatric"):
        return a.complaint, f"{a.complaint} emergency: capability first"
    if unstable:
        return "arrest_airway", f"unstable vitals ({', '.join(unstable)}): stabilise at nearest ED"
    return "low_acuity", "no time-critical criteria met"
