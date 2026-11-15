"""The advisor's score (spec §4). Pure functions, lower is better.

    score(h) = travel_time(scene -> h, at that hour)
             + expected_ED_wait(h)
             + P(cannot treat | h) * transfer_delay
             + lambda * coverage_lost(h)

Plain-words version for judges: "minutes until this patient is in front of someone who can treat
them, plus a penalty if we would take the ambulance out of its area for longer than necessary".
"""

from __future__ import annotations

from advisor.models import ScoreBreakdown


def score(
    travel_min: float,
    ed_wait_min: float,
    p_cannot_treat: float,
    transfer_delay_min: float,
    lam: float,
    coverage_lost_min: float,
) -> ScoreBreakdown:
    """Combine the four terms. Inputs are already-resolved minutes; provenance is tracked by the caller."""
    if not 0.0 <= p_cannot_treat <= 1.0:
        raise ValueError(f"p_cannot_treat must be in [0, 1], got {p_cannot_treat}")
    if lam < 0:
        raise ValueError(f"lambda must be >= 0, got {lam}")
    # lambda = 0 means coverage is ignored entirely, even if coverage_lost is large or negative.
    coverage_term = lam * coverage_lost_min if lam else 0.0
    total = travel_min + ed_wait_min + p_cannot_treat * transfer_delay_min + coverage_term
    return ScoreBreakdown(
        travel_min=travel_min,
        ed_wait_min=ed_wait_min,
        p_cannot_treat=p_cannot_treat,
        transfer_delay_min=transfer_delay_min,
        lam=lam,
        coverage_lost_min=coverage_lost_min,
        total=total,
    )


def coverage_lost(
    scene_to_h_min: float, h_to_base_min: float, scene_to_ref_min: float, ref_to_base_min: float
) -> float:
    """Extra minutes the ambulance is unavailable versus the reference (nearest capable) hospital.

    (scene -> h -> base) - (scene -> nearest capable -> base). Simplified standalone version from
    spec §4; replace with the placement model's coverage-loss term if fleet placement is added.
    Can be negative when h sits on the way back to base: that is a real saving, so it is not clamped.
    """
    return (scene_to_h_min + h_to_base_min) - (scene_to_ref_min + ref_to_base_min)
