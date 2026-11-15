"""Fleet advice for the assistant: where calls were late on a day, and which ambulance moves to make today.

  1. Look back. Replay that day's calls through the GoldenHour plan (pipeline/simulate.py) and find where help
     was late, and why (out of reach / no unit planned nearby / nearby unit busy).
  2. Plan moves for one 4-hour block today. Greedy swap search (Teitz-Bart style) on the MEXCLP objective the
     optimiser uses, sum_i w_i (1 - q^k_i): k_i = ambulances within the target of hexagon i, q = the block's busy
     fraction, w_i = forecast calls x priority. Priority is higher where calls were late that day and where a
     12-week black spot sits. A move takes one ambulance from a staffed site to another candidate site at most
     ADVICE_MAX_MOVE_MIN away.
  3. Check. One day is a handful of calls, so the moves are replayed on the 12 weeks before that day, and only
     as many moves are kept as cut late calls in that block there.

Calls are synthetic (108 logs are not public); everything here is a simulation result. The log ends on its last
day, so "yesterday" means the last day in the log when the real yesterday is past it.
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_PATH = Path(__file__).resolve().parent.parent
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

import numpy as np
import pandas as pd

from pipeline.blackspots import CAUSE_INFO, CAUSES, classify_calls, plans_from_scenario
from pipeline.config import (
    AMBULANCE_SPEED_MULTIPLIER,
    BENGALURU_HOURLY_CONGESTION,
    BLACKSPOT_WINDOW_DAYS,
    MAX_AMBULANCES_PER_SITE,
    PLANNING_BLOCKS,
    PRE_TRAVEL_MIN,
    PROCESSED_DIR,
    REGIONS,
    WEB_DATA_DIR,
)
from pipeline.optimize import BlockPlan
from pipeline.simulate import replay_calls

# ≈ our choices
ADVICE_MAX_MOVES = 5            # most moves suggested at once
ADVICE_MAX_MOVE_MIN = 25.0      # longest drive between standby sites for a move (at that block's traffic)
ADVICE_LATE_WEIGHT = 1.0        # priority +1 per late call in the hexagon on the day looked back at
ADVICE_BLACKSPOT_WEIGHT = 2.0   # priority +2 for a 12-week black spot
ADVICE_MAX_PRIORITY = 6.0
ADVICE_MIN_GAIN = 1e-3          # smallest objective gain (weighted calls per hour) worth a move

HOUR_TO_BLOCK = {h: b["id"] for b in PLANNING_BLOCKS for h in b["hours"]}
BLOCK_LABEL = ["12–4 am", "4–8 am", "8 am–12 pm", "12–4 pm", "4–8 pm", "8 pm–12 am"]


class RegionAdvisor:
    """Region data loaded once; advise() is then a few replays and a vectorised search (seconds)."""

    def __init__(self, reg: str):
        self.reg = reg
        self.cfg = REGIONS[reg]
        self.cells = pd.read_parquet(PROCESSED_DIR / f"cells_{reg}.parquet")
        self.sites = pd.read_parquet(PROCESSED_DIR / f"sites_{reg}.parquet")
        self.calls = pd.read_parquet(PROCESSED_DIR / f"calls_{reg}.parquet")
        self.lam = pd.read_parquet(PROCESSED_DIR / f"lam_{reg}.parquet")
        t = np.load(PROCESSED_DIR / f"travel_{reg}.npz")
        self.fwd, self.back, self.ss = t["fwd"], t["back"], t["ss"]
        sc = json.loads((WEB_DATA_DIR / f"scenarios_{reg}.json").read_text(encoding="utf-8"))
        self.plans = plans_from_scenario(sc["goldenhour"], self.sites)
        self.black = np.zeros(len(self.cells), bool)
        self.clusters: List[Dict[str, Any]] = []
        bs = WEB_DATA_DIR / f"blackspots_{reg}.json"
        if bs.exists():
            cur = next(s for s in json.loads(bs.read_text(encoding="utf-8"))["scenarios"] if s["key"] == "current")
            self.black[[r[0] for r in cur["cells"] if r[6] >= 0]] = True
            self.clusters = cur["clusters"]
        self.days = pd.to_datetime(self.calls["date"]).dt.date
        self.first_day, self.last_day = self.days.min(), self.days.max()
        self.doy_of = dict(zip(self.days, self.calls["day_of_year"], strict=False))
        f = BENGALURU_HOURLY_CONGESTION if self.cfg.congestion_enabled else [1.10] * 24
        self.hour_factor = list(f)
        zone_of = dict(zip(self.cells["h3_index"], self.cells["zone"], strict=False))
        self.site_zone = [str(zone_of.get(h, "")) for h in self.sites["h3_index"]]

    # ------------------------------------------------------------------------------------------ helpers

    def resolve_day(self, day: Optional[str], today: Optional[date] = None) -> tuple[date, Optional[str]]:
        """'yesterday' / 'today' / 'YYYY-MM-DD' / None -> a day in the call log, plus a note if it had to move."""
        today = today or datetime.now().date()
        want = today - timedelta(days=1)
        s = (day or "yesterday").strip().lower()
        if s == "today":
            want = today
        elif s not in ("", "yesterday"):
            try:
                want = date.fromisoformat(s[:10])
            except ValueError:
                pass
        if self.first_day <= want <= self.last_day:
            return want, None
        return self.last_day, (f"The modelled call log runs to {self.last_day:%-d %b %Y}, so I looked at that day "
                               f"instead of {want:%-d %b}.")

    def replay(self, plans: List[BlockPlan], days: int, end_day: date) -> pd.DataFrame:
        """Replay `days` days ending on end_day (plus one warm-up day, dropped) and classify late calls."""
        offset = int(self.calls["day_of_year"].max()) - int(self.doy_of[end_day])
        log = replay_calls(self.reg, plans, self.calls, self.cells, self.sites, self.fwd, self.back, self.ss,
                           sim_days=days + 1, end_offset_days=offset)
        log = log[log["day_of_year"] > int(self.doy_of[end_day]) - days]
        return classify_calls(log, self.cfg.target_min)

    def site_info(self, s: int) -> Dict[str, Any]:
        r = self.sites.iloc[s]
        kind = str(r.site_type).replace("_", " ")
        name = str(r["name"]).strip()
        return {"site_id": str(r.site_id), "name": name if name and name.lower() != "nan" else kind.title(),
                "type": kind, "zone": self.site_zone[s], "lat": round(float(r.lat), 5), "lon": round(float(r.lon), 5)}

    # ------------------------------------------------------------------------------------------- advice

    def advise(self, day: Optional[str] = None, hour: Optional[int] = None, max_moves: int = ADVICE_MAX_MOVES,
               today: Optional[date] = None) -> Dict[str, Any]:
        T = self.cfg.target_min
        the_day, day_note = self.resolve_day(day, today)
        hour = datetime.now().hour if hour is None else int(hour) % 24
        b = HOUR_TO_BLOCK[hour]
        hours_b = PLANNING_BLOCKS[b]["hours"]

        # 1. Look back: that day under the current plan
        yday = self.replay(self.plans, 1, the_day)
        late = yday[yday["late"]]
        n_cells = len(self.cells)
        late_by_cell = np.bincount(late["cell_idx"], minlength=n_cells)
        hotspots = []
        for zone, g in yday.groupby("zone"):
            n_late = int(g["late"].sum())
            if n_late:
                idx = g["cell_idx"].unique()
                hotspots.append({"zone": str(zone), "late": n_late, "calls": int(len(g)),
                                 "chronic": bool(self.black[idx].any()),
                                 "cause": str(g.loc[g["late"], "cause"].value_counts().idxmax()),
                                 "lat": round(float(self.cells["lat"].iloc[idx].mean()), 5),
                                 "lon": round(float(self.cells["lon"].iloc[idx].mean()), 5)})
        hotspots.sort(key=lambda h: (-h["late"], -h["chronic"]))

        # 2. Moves for block b today
        plan_b = self.plans[b]
        n_amb = (plan_b.a + plan_b.b).astype(int)
        f_b = float(np.mean([self.hour_factor[h] for h in hours_b]))
        mps = f_b / (AMBULANCE_SPEED_MULTIPLIER * 60.0)
        C = (PRE_TRAVEL_MIN + self.fwd * mps <= T).astype(np.int16)        # sites x cells: reaches in time
        move_ok = self.ss * mps <= ADVICE_MAX_MOVE_MIN                     # sites x sites: short enough move
        q = float(np.clip(plan_b.q, 0.05, 0.9))
        lam_b = self.lam[f"block_{b}"].to_numpy()
        prio = np.minimum(ADVICE_MAX_PRIORITY,
                          1.0 + ADVICE_LATE_WEIGHT * late_by_cell + ADVICE_BLACKSPOT_WEIGHT * self.black)
        w = lam_b * prio
        qpow = q ** np.arange(0, 64)

        def objective(k: np.ndarray) -> np.ndarray:
            return (w * (1.0 - qpow[np.minimum(k, 63)])).sum(axis=-1)

        k = C.T.astype(np.int32) @ n_amb                                   # ambulances within reach per cell
        a_new, b_new = plan_b.a.copy(), plan_b.b.copy()
        moves, used_from, used_to = [], set(), set()
        for _ in range(max_moves):
            base_val = objective(k)
            best = (ADVICE_MIN_GAIN, None, None)
            for s in np.flatnonzero(a_new + b_new):
                if s in used_to:
                    continue
                k_s = k - C[s]
                ok = move_ok[s] & ((a_new + b_new) < MAX_AMBULANCES_PER_SITE)
                ok[s] = False
                ok[list(used_from)] = False
                cand = np.flatnonzero(ok)
                if not len(cand):
                    continue
                gains = objective(k_s[None, :] + C[cand]) - base_val
                j = int(np.argmax(gains))
                if gains[j] > best[0]:
                    best = (float(gains[j]), int(s), int(cand[j]))
            gain, s, t = best
            if s is None:
                break
            kind = "Basic (BLS)" if b_new[s] > 0 else "Advanced (ALS)"   # keep Advanced units where they are
            if b_new[s] > 0:
                b_new[s] -= 1
                b_new[t] += 1
            else:
                a_new[s] -= 1
                a_new[t] += 1
            k_after = k - C[s] + C[t]
            # per-hexagon change in the chance a free ambulance is within reach, weighted by priority
            contrib = w * (qpow[np.minimum(k, 63)] - qpow[np.minimum(k_after, 63)])
            helped = np.flatnonzero(k_after > k)
            thinned = np.flatnonzero(k_after < k)
            moves.append({
                "kind": kind, "from": self.site_info(s), "to": self.site_info(t),
                "drive_min": round(float(self.ss[s, t] * mps), 1),
                "gain": round(gain, 4),
                # hexagons that get one more ambulance within the target, and those that get one fewer
                "helped_cells": int(len(helped)),
                "helped_calls_per_day": round(float(lam_b[helped].sum() * 24), 1),
                "thinned_cells": int(len(thinned)),
                "first_cover_cells": int(((k == 0) & (k_after > 0)).sum()),
                "lost_cover_cells": int(((k > 0) & (k_after == 0)).sum()),
                "zones": _top_zones(self.cells, helped, np.maximum(contrib, 0)),
                "thinned_zones": _top_zones(self.cells, thinned, np.maximum(-contrib, 0)),
                "late_yesterday_cells": int((late_by_cell[helped] > 0).sum()),
                "black_spot_cells": int(self.black[helped].sum()),
                "black_spots": sorted({c["id"] for c in self.clusters if set(c["cells"]) & set(helped.tolist())})[:5],
            })
            k = k_after
            used_from.add(s)
            used_to.add(t)

        # 3. Check: keep the prefix of moves that cuts late calls in this block most over the 12 weeks before
        weeks_end = the_day - timedelta(days=1)
        def with_moves(n: int) -> List[BlockPlan]:
            plans = [BlockPlan(p.block_id, p.a.copy(), p.b.copy(), p.y.copy(), p.q, p.busy_fraction, None, None, None)
                     for p in self.plans]
            for m in moves[:n]:
                s = int(self.sites.index[self.sites["site_id"] == m["from"]["site_id"]][0])
                t = int(self.sites.index[self.sites["site_id"] == m["to"]["site_id"]][0])
                arr = plans[b].b if m["kind"].startswith("Basic") else plans[b].a
                arr[s] -= 1
                arr[t] += 1
            return plans

        def block_stats(log: pd.DataFrame) -> Dict[str, Any]:
            inb = log[log["block"] == b]
            return {"calls": int(len(inb)), "late": int(inb["late"].sum()),
                    "within_pct": round(100.0 * float((~inb["late"]).mean()), 1) if len(inb) else 0.0}

        weeks = BLACKSPOT_WINDOW_DAYS
        checks = []
        if weeks_end >= self.first_day + timedelta(days=weeks):
            for n in range(len(moves) + 1):
                checks.append(block_stats(self.replay(with_moves(n), weeks, weeks_end)))
            keep = int(np.argmin([c["late"] for c in checks]))
        else:
            keep = len(moves)
        y_before = block_stats(yday)
        y_after = block_stats(self.replay(with_moves(keep), 1, the_day)) if keep else y_before

        out = {
            "region": self.reg,
            "region_name": self.cfg.name,
            "target_min": T,
            "day": the_day.isoformat(),
            "day_note": day_note,
            "hour": hour,
            "block": b,
            "block_label": BLOCK_LABEL[b],
            "looked_back": {
                "calls": int(len(yday)), "late": int(len(late)),
                "late_pct": round(100.0 * len(late) / max(1, len(yday)), 1),
                "by_cause": {c: int((late["cause"] == c).sum()) for c in CAUSES},
                "hotspots": hotspots[:6],
                "late_cells": [[str(self.cells["h3_index"].iat[i]), int(late_by_cell[i])] for i in np.flatnonzero(late_by_cell)],
                "in_block": y_before,
                "in_block_after": y_after,
            },
            "moves": moves[:keep],
            "moves_considered": len(moves),
            "check": None if not checks else {
                "weeks": round(weeks / 7), "start": (weeks_end - timedelta(days=weeks - 1)).isoformat(),
                "end": weeks_end.isoformat(), "before": checks[0], "after": checks[keep],
            },
            "causes": {c: CAUSE_INFO[c]["label"] for c in CAUSES},
            "synthetic": True,
        }
        out["reply"] = advice_reply(out)
        return out


def _top_zones(cells: pd.DataFrame, idx: np.ndarray, weight: np.ndarray, n: int = 3) -> List[str]:
    if not len(idx):
        return []
    s = pd.Series(weight[idx], index=cells["zone"].iloc[idx].to_numpy()).groupby(level=0).sum()
    return [str(z) for z in s.sort_values(ascending=False).index[:n]]


def advice_reply(a: Dict[str, Any]) -> str:
    """Short chat reply; the card on screen carries the details."""
    lb = a["looked_back"]
    d = datetime.fromisoformat(a["day"]).strftime("%-d %b")
    worst = ", ".join(h["zone"] for h in lb["hotspots"][:3])
    main = max(lb["by_cause"], key=lb["by_cause"].get) if lb["late"] else None
    parts = []
    if a["day_note"]:
        parts.append(a["day_note"])
    parts.append(f"On {d}, {lb['late']} of {lb['calls']} calls were late ({lb['late_pct']}%)"
                 + (f", worst in {worst}" if worst else "")
                 + (f"; mostly '{a['causes'][main].lower()}'." if main else "."))
    if a["moves"]:
        n = len(a["moves"])
        first = a["moves"][0]
        src = first["from"]["zone"] or first["from"]["name"]
        to = first["to"]["zone"] or first["to"]["name"]
        if src == to:  # same ward or taluk: name the sites
            src, to = first["from"]["name"], f"{first['to']['name']} ({to})"
        msg = f"For {a['block_label']} today, move {n} ambulance{'s' if n > 1 else ''} (first: {src} to {to})."
        c = a["check"]
        if c:
            msg += (f" On the {c['weeks']} weeks before, that block would have {c['after']['late']:,} late calls "
                    f"instead of {c['before']['late']:,}.")
        parts.append(msg)
    elif a["moves_considered"]:
        parts.append(f"I tried {a['moves_considered']} moves for {a['block_label']}, but none cut late calls over the "
                     f"weeks before, so keep the plan; late calls here mostly need more ambulances, not moves.")
    else:
        parts.append(f"No move for {a['block_label']} improves coverage, so keep the current plan.")
    return " ".join(parts)


_ADVISORS: Dict[str, RegionAdvisor] = {}


def advise(region: str, day: Optional[str] = None, hour: Optional[int] = None, **kw) -> Dict[str, Any]:
    if region not in _ADVISORS:
        _ADVISORS[region] = RegionAdvisor(region)
    return _ADVISORS[region].advise(day=day, hour=hour, **kw)


if __name__ == "__main__":
    import argparse
    import time

    ap = argparse.ArgumentParser(description="Fleet advice: yesterday's late calls and today's moves")
    ap.add_argument("--region", default="blr", choices=list(REGIONS))
    ap.add_argument("--day", default="yesterday")
    ap.add_argument("--hour", type=int, default=None)
    a = ap.parse_args()
    t0 = time.time()
    res = advise(a.region, a.day, a.hour)
    print(res["reply"])
    for m in res["moves"]:
        print(f"  {m['kind']}: {m['from']['name']} ({m['from']['zone']}) -> {m['to']['name']} ({m['to']['zone']}), "
              f"{m['drive_min']} min; helps {m['helped_cells']} hex ({', '.join(m['zones'])}), thins "
              f"{m['thinned_cells']} ({', '.join(m['thinned_zones'])}); late that day in {m['late_yesterday_cells']}")
    print(f"  check: {res['check']}\n  {time.time() - t0:.1f}s")
