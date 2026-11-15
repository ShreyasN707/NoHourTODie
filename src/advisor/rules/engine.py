"""Recommend a destination hospital (spec §4 flow, steps 2-4).

    classify -> pick the capability set the rule needs -> filter -> score -> rank

Rules by care category (all thresholds in config/advisor.yaml; clinical sign-off pending):
  score     (obstetric, paediatric, low_acuity): preferred-capability tier first, then score.
  nearest   (arrest_airway): rank by travel time only. Stabilise first.
  trauma    (severe_trauma): highest-capability tier first, then score with ED crowding ignored.
            Trauma is never load-balanced.
  stemi:    PCI hub if first-contact-to-PCI fits the window, else thrombolysis (pharmacoinvasive).
  stroke:   CT + thrombolysis by default. LVO thrombectomy bypass only when both locally tuned
            thresholds are configured. They are null by default (RACECAT: no outcome difference).
If no hospital has the needed capability, fall back to the nearest 24x7 ED to stabilise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from advisor.config import AdvisorConfig, CareCategory
from advisor.interfaces import DwellIndex, TravelTimeProvider
from advisor.models import (
    Assessment,
    ExcludedHospital,
    Hospital,
    HospitalStatus,
    LatLon,
    OutcomeEvent,
    RankedHospital,
    Recommendation,
    ScoreBreakdown,
)
from advisor.provenance import Provenance, Sourced
from advisor.rules.triage import classify
from advisor.scoring.ed_wait import expected_ed_wait, is_fresh
from advisor.scoring.reliability import OutcomeLog, decayed_counts, p_cannot_treat
from advisor.scoring.score import coverage_lost, score

# Least trustworthy last. "Weakest" provenance of a set = the one furthest down this list.
TRUST_ORDER = (Provenance.REAL, Provenance.REAL_DERIVED, Provenance.SIMULATED, Provenance.PLACEHOLDER)
NON_REAL = (Provenance.SIMULATED, Provenance.PLACEHOLDER)


def weakest(*provs: Provenance) -> Provenance:
    return max(provs, key=TRUST_ORDER.index)


@dataclass
class AdvisorContext:
    """Everything the engine needs besides the assessment. Swap providers without touching rules."""

    hospitals: list[Hospital]
    travel: TravelTimeProvider
    cfg: AdvisorConfig
    statuses: dict[str, HospitalStatus] = field(default_factory=dict)  # latest per hospital
    events: list[OutcomeEvent] = field(default_factory=list)
    dwell: DwellIndex | None = None


@dataclass
class _Eval:
    """Per-hospital inputs, resolved once."""

    h: Hospital
    status: HospitalStatus | None  # only if fresh
    travel: Sourced[float]
    ed_wait: Sourced[float]
    p: float
    p_prov: Provenance

    def has(self, cap: str) -> bool:
        down = self.status.down_capabilities if self.status else []
        return self.h.has(cap) and cap not in down

    def missing(self, caps: list[str]) -> list[str]:
        return [c for c in caps if not self.has(c)]

    @property
    def diverted(self) -> bool:
        return bool(self.status and self.status.diversion)

    def caps_prov(self, caps: list[str]) -> Provenance:
        tags = [self.h.capabilities[c].provenance for c in caps if c in self.h.capabilities]
        return weakest(*tags) if tags else Provenance.PLACEHOLDER


def _evaluate(h: Hospital, a: Assessment, ctx: AdvisorContext, quantile: float | None) -> _Eval:
    cfg = ctx.cfg
    st = ctx.statuses.get(h.hospital_id)
    st = st if is_fresh(st, a.when, cfg.v("scoring.live_status_max_age_min")) else None
    scene = LatLon(lat=a.scene_lat, lon=a.scene_lon)
    travel = ctx.travel.minutes(scene, LatLon(lat=h.lat, lon=h.lon), a.when, quantile)
    arrival = a.when + timedelta(minutes=travel.value)
    wait = expected_ed_wait(h.hospital_id, arrival, st, ctx.dwell, cfg)
    half_life = cfg.v("reliability.decay_half_life_days")
    if isinstance(ctx.events, OutcomeLog) and ctx.events.half_life_days == half_life:
        rejects, total = ctx.events.counts(h.hospital_id, a.when)  # O(1) running sums
        provs = ctx.events.sources(h.hospital_id, a.when)
    else:
        rejects, total = decayed_counts(ctx.events, h.hospital_id, a.when, half_life)
        provs = {e.source for e in ctx.events if e.hospital_id == h.hospital_id and e.timestamp <= a.when}
    p = p_cannot_treat(True, rejects, total, cfg.v("reliability.prior_rejects"), cfg.v("reliability.prior_total"))
    p_prov = weakest(*provs) if provs else Provenance.PLACEHOLDER  # no history: prior only
    return _Eval(h=h, status=st, travel=travel, ed_wait=wait, p=p, p_prov=p_prov)


def _choose_route(
    cat: CareCategory, a: Assessment, evals: list[_Eval], cfg: AdvisorConfig
) -> tuple[list[str], str | None, str]:
    """Which capabilities this patient needs right now, the route name, and why."""
    base = list(cat.required)
    if cat.rule == "stemi":
        window = cfg.v("rules.stemi_fmc_to_pci_window_min")
        hubs = [e for e in evals if not e.missing(base + cat.primary_pci) and not e.diverted]
        if hubs:
            best = min(a.minutes_since_first_contact + e.travel.value + e.ed_wait.value for e in hubs)
            if best <= window:
                return base + cat.primary_pci, "primary_pci", (
                    f"PCI hub reachable: first-contact-to-PCI ~{best:.0f} min <= {window} min window")
            why = f"best first-contact-to-PCI ~{best:.0f} min exceeds {window} min window"
        else:
            why = "no PCI hub available"
        return base + cat.pharmacoinvasive, "pharmacoinvasive", f"{why}: thrombolysis, then transfer for PCI"

    if cat.rule == "stroke":
        cutoff = cfg.v("rules.stroke_lvo_score_cutoff")
        limit = cfg.v("rules.stroke_thrombectomy_extra_travel_limit_min")
        if a.stroke_score is None:
            return base, "ct_thrombolysis", "no stroke score: nearest CT + thrombolysis centre"
        if cutoff is None or limit is None:
            return base, "ct_thrombolysis", "LVO bypass disabled (thresholds not configured): CT + thrombolysis"
        if a.stroke_score < cutoff:
            return base, "ct_thrombolysis", f"stroke score {a.stroke_score} < LVO cut-off {cutoff}"
        ct = [e.travel.value for e in evals if not e.missing(base) and not e.diverted]
        evt = [e.travel.value for e in evals if not e.missing(base + cat.lvo_target) and not e.diverted]
        if ct and evt and min(evt) - min(ct) <= limit:
            return base + cat.lvo_target, "lvo_thrombectomy", (
                f"suspected LVO (score {a.stroke_score} >= {cutoff}); thrombectomy centre adds "
                f"{min(evt) - min(ct):.0f} min <= {limit} min limit")
        return base, "ct_thrombolysis", "suspected LVO but no thrombectomy centre within extra-travel limit"

    return base, None, ""


def _apply_cap(cands: list[_Eval], cap: float | None, label: str,
               excluded: list[ExcludedHospital], notes: list[str]) -> list[_Eval]:
    """Drop candidates beyond a travel cap, unless that would drop all of them."""
    if cap is None:
        return cands
    within = [e for e in cands if e.travel.value <= cap]
    if not within:
        notes.append(f"no capable hospital within {label} of {cap} min; cap not applied")
        return cands
    excluded += [ExcludedHospital(hospital_id=e.h.hospital_id, name=e.h.name, reason=f"over_{label}")
                 for e in cands if e not in within]
    return within


def recommend(a: Assessment, ctx: AdvisorContext) -> Recommendation:
    cfg = ctx.cfg
    category, triage_why = classify(a, cfg)
    cat = cfg.care_categories[category]
    lam = cfg.lambda_for(category)
    critical = lam == 0.0
    quantile = cfg.v("scoring.pessimistic_travel_quantile") if critical else None
    transfer = float(cfg.v("scoring.transfer_delay_min"))

    evals = [_evaluate(h, a, ctx, quantile) for h in ctx.hospitals]
    need, route, route_why = _choose_route(cat, a, evals, cfg)
    rule = cat.rule
    excluded: list[ExcludedHospital] = []
    notes: list[str] = []

    # Filter by capability. Excluded ones are still scored with P(cannot treat) = 1 for comparison.
    cands = []
    for e in evals:
        miss = e.missing(need)
        if miss:
            comp = score(e.travel.value, e.ed_wait.value, 1.0, transfer, 0.0, 0.0)
            excluded.append(ExcludedHospital(hospital_id=e.h.hospital_id, name=e.h.name,
                                             reason="missing:" + ",".join(miss), components=comp))
        else:
            cands.append(e)

    open_ = [e for e in cands if not e.diverted]
    if cands and not open_:
        notes.append("every capable hospital reports diversion; diversion ignored")
    else:
        excluded += [ExcludedHospital(hospital_id=e.h.hospital_id, name=e.h.name, reason="diversion")
                     for e in cands if e.diverted]
        cands = open_

    if rule == "trauma":
        cands = _apply_cap(cands, cfg.v("rules.trauma_max_travel_min"), "trauma_travel_limit", excluded, notes)
    if category == "low_acuity":
        cands = _apply_cap(cands, cfg.v("scoring.low_acuity_travel_cap_min"), "travel_cap", excluded, notes)

    reason_code = category.upper() + (f"_{route.upper()}" if route else "")
    reason = "; ".join(x for x in (triage_why, route_why) if x)

    if not cands:  # nothing capable: stabilise at nearest 24x7 ED
        cands = [e for e in evals if not e.missing(["ed_24x7"])]
        excluded = [x for x in excluded if x.hospital_id not in {e.h.hospital_id for e in cands}]
        rule, route = "nearest", "stabilise_nearest"
        reason_code = f"{category.upper()}_NO_CAPABLE_STABILISE_NEAREST"
        reason += f"; no hospital with {', '.join(need)}: stabilise at nearest 24x7 ED, then transfer"
        if not cands:
            return _build(a, category, route, [], excluded, "NO_HOSPITAL_AVAILABLE",
                          reason + "; no 24x7 ED found either", lam, cfg, [])

    # Coverage reference: nearest candidate. Only computed when lambda > 0 (never for critical patients).
    ref_cov: dict[str, float] = {}
    if lam > 0:
        base = LatLon(lat=a.base_lat, lon=a.base_lon)

        def round_trip(e: _Eval) -> tuple[float, float]:
            depart = a.when + timedelta(minutes=e.travel.value + e.ed_wait.value)
            back = ctx.travel.minutes(LatLon(lat=e.h.lat, lon=e.h.lon), base, depart, None).value
            return e.travel.value, back

        ref = min(cands, key=lambda e: e.travel.value)
        r_out, r_back = round_trip(ref)
        for e in cands:
            out, back = round_trip(e)
            ref_cov[e.h.hospital_id] = coverage_lost(out, back, r_out, r_back)

    ranked: list[tuple[tuple, RankedHospital]] = []
    for e in cands:
        h_notes = list(notes)
        wait = e.ed_wait.value
        if rule == "trauma":
            wait = 0.0
            h_notes.append("ED crowding ignored: trauma is never load-balanced")
        comp: ScoreBreakdown = score(e.travel.value, wait, e.p, transfer, lam, ref_cov.get(e.h.hospital_id, 0.0))
        missing_pref = len(e.missing(cat.preferred)) if rule != "nearest" else 0
        if missing_pref:
            h_notes.append("missing preferred: " + ",".join(e.missing(cat.preferred)))
        if rule == "nearest":
            key, shown = (e.travel.value,), e.travel.value
            h_notes.append("ranked by travel time only (stabilise at nearest ED)")
        else:
            key, shown = (missing_pref, comp.total), comp.total
        prov = {
            "travel": e.travel.provenance,
            "ed_wait": e.ed_wait.provenance,
            "capabilities": e.caps_prov(need),
            "p_cannot_treat": e.p_prov,
        }
        ranked.append((key, RankedHospital(hospital_id=e.h.hospital_id, name=e.h.name, score=shown,
                                           components=comp, capable=True, notes=h_notes,
                                           inputs_provenance=prov)))
    ranked.sort(key=lambda kv: kv[0])
    return _build(a, category, route, [r for _, r in ranked], excluded, reason_code, reason, lam, cfg,
                  [r.inputs_provenance for _, r in ranked])


def _build(a: Assessment, category: str, route: str | None, ranked: list[RankedHospital],
           excluded: list[ExcludedHospital], reason_code: str, reason: str, lam: float,
           cfg: AdvisorConfig, per_h: list[dict[str, Provenance]]) -> Recommendation:
    agg: dict[str, Provenance] = {}
    for d in per_h:
        for k, v in d.items():
            agg[k] = weakest(agg[k], v) if k in agg else v
    agg["transfer_delay"] = cfg.p("scoring.transfer_delay_min").provenance
    agg["lambda"] = cfg.p("scoring.lambda_by_category").provenance
    agg["triage_thresholds"] = cfg.p("rules.unstable_vitals").provenance
    return Recommendation(
        case_id=a.case_id, care_category=category, route=route, ranked=ranked, excluded=excluded,
        reason_code=reason_code, reason=reason, lambda_used=lam, inputs_provenance=agg,
        non_real_inputs_used=any(v in NON_REAL for v in agg.values()),
    )

