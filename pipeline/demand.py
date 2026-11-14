"""GoldenHour: Synthetic emergency call generation pipeline.

Generates 1 year of realistic synthetic calls (2025-11-01 to 2026-10-31):
  - Daily volume with weekday factor, 8% yearly trend, and Gamma(20) noise.
  - Hidden ward effects (lognormal, sigma=0.25).
  - Poisson calls per type and hour matching CAG 2020 call mix.
  - Population weighting for medical; 50% pop + 50% major road length for trauma.
  - Monsoon trauma boost (x1.12 Jun-Sep) and weekend late-night trauma boost (x1.5).
  - Produces ~202k calls for Bengaluru and ~36k calls for Tumakuru.
"""

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Ensure root directory is on sys.path
ROOT_PATH = Path(__file__).resolve().parent.parent
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

import numpy as np
import pandas as pd

from pipeline.config import (
    CALL_MIX,
    HOURLY_CALL_SHARE,
    PLANNING_BLOCKS,
    PROCESSED_DIR,
    REGIONS,
    SEED,
)

# Day of week multiplier (Mon=0, Tue=1, ..., Sun=6)
# Emergency calls tend to peak slightly on Fri/Sat and drop slightly mid-week
WEEKDAY_MULTIPLIERS = [1.02, 0.98, 0.98, 0.99, 1.03, 1.04, 0.96]


def get_block_id(hour: int) -> int:
    return hour // 4


def generate_calls_for_region(reg_code: str, rng: np.random.Generator) -> pd.DataFrame:
    cfg = REGIONS[reg_code]
    print(f"\n[demand] Generating synthetic calls for region '{reg_code}' ({cfg.name})...")

    df_cells = pd.read_parquet(PROCESSED_DIR / f"cells_{reg_code}.parquet")
    n_cells = len(df_cells)
    h3_cells = df_cells["h3_index"].values
    zones = df_cells["zone"].values
    pops = df_cells["pop"].values.astype(np.float64)
    major_kms = df_cells["major_km"].values.astype(np.float64)

    # 1. Hidden ward/taluk effects (Lognormal with sigma=0.25, mean=1.0)
    unique_zones = np.unique(zones)
    sigma = 0.25
    mu = -(sigma**2) / 2.0  # so that exp(mu + sigma^2/2) = 1.0
    zone_effects = {z: rng.lognormal(mu, sigma) for z in unique_zones}
    cell_zone_mult = np.array([zone_effects[z] for z in zones])

    # 2. Cell spatial probabilities
    # Medical: pop * zone_mult
    med_weights = pops * cell_zone_mult
    if med_weights.sum() > 0:
        med_probs = med_weights / med_weights.sum()
    else:
        med_probs = np.full(n_cells, 1.0 / n_cells)

    # Trauma: 0.5 * pop_norm + 0.5 * road_norm, scaled by zone_mult
    pop_norm = pops / (pops.sum() if pops.sum() > 0 else 1.0)
    road_norm = major_kms / (major_kms.sum() if major_kms.sum() > 0 else 1.0)
    trauma_weights = (0.5 * pop_norm + 0.5 * road_norm) * cell_zone_mult
    if trauma_weights.sum() > 0:
        trauma_probs = trauma_weights / trauma_weights.sum()
    else:
        trauma_probs = med_probs

    # 3. Simulate 365 days
    start_date = datetime(2025, 11, 1)
    hourly_shares = np.array(HOURLY_CALL_SHARE) / 100.0  # sums to 1.0

    call_records = []
    call_counter = 0

    for day_idx in range(365):
        cur_date = start_date + timedelta(days=day_idx)
        dow = cur_date.weekday()
        month = cur_date.month

        # Base daily calls
        trend_factor = 1.0 + 0.08 * (day_idx / 365.0)
        dow_factor = WEEKDAY_MULTIPLIERS[dow]
        # Gamma noise with shape 20, scale 1/20 (mean=1.0, variance=0.05)
        gamma_noise = rng.gamma(shape=20.0, scale=1.0 / 20.0)

        daily_rate = cfg.calls_per_day * dow_factor * trend_factor * gamma_noise

        is_monsoon = month in (6, 7, 8, 9)
        is_weekend = dow in (4, 5, 6)  # Fri, Sat, Sun

        for hour in range(24):
            hour_share = hourly_shares[hour]
            base_hour_rate = daily_rate * hour_share
            block_id = get_block_id(hour)
            is_late_night = hour in (21, 22, 23, 0, 1, 2, 3)

            for ctype, info in CALL_MIX.items():
                c_rate = base_hour_rate * info["share"]

                # Apply trauma boosts
                if ctype == "trauma":
                    if is_monsoon:
                        c_rate *= 1.12
                    if is_weekend and is_late_night:
                        c_rate *= 1.50

                # Sample Poisson call count
                n_calls = rng.poisson(c_rate)
                if n_calls <= 0:
                    continue

                # Sample cell locations
                probs = trauma_probs if ctype == "trauma" else med_probs
                chosen_cells = rng.choice(n_cells, size=n_calls, p=probs)

                # Sample minute/second within hour
                minute_offsets = rng.uniform(0, 3600, size=n_calls)

                for k in range(n_calls):
                    call_counter += 1
                    cell_i = chosen_cells[k]
                    call_ts = cur_date + timedelta(hours=hour, seconds=float(minute_offsets[k]))

                    call_records.append({
                        "call_id": f"{reg_code}_{call_counter:07d}",
                        "timestamp": call_ts.strftime("%Y-%m-%d %H:%M:%S"),
                        "date": cur_date.strftime("%Y-%m-%d"),
                        "day_of_year": day_idx,
                        "day_of_week": dow,
                        "month": month,
                        "hour": hour,
                        "block": block_id,
                        "region": reg_code,
                        "h3_index": h3_cells[cell_i],
                        "zone": zones[cell_i],
                        "call_type": ctype,
                        "needs_als": info["needs_als"],
                        "bike_helps": info["bike_helps"],
                    })

    df_calls = pd.DataFrame(call_records)
    print(
        f"[demand] Generated {len(df_calls):,} calls for {reg_code}.\n"
        f"  Calls per day: {len(df_calls) / 365.0:.1f} (target: ~{cfg.calls_per_day})\n"
        f"  Call mix: {df_calls['call_type'].value_counts(normalize=True).round(3).to_dict()}\n"
        f"  ALS required: {df_calls['needs_als'].mean() * 100:.1f}%\n"
        f"  Bike eligible: {df_calls['bike_helps'].mean() * 100:.1f}%"
    )

    out_path = PROCESSED_DIR / f"calls_{reg_code}.parquet"
    df_calls.to_parquet(out_path, index=False)
    print(f"[demand] Saved to {out_path} ({out_path.stat().st_size / 1e6:.1f} MB).")
    return df_calls


def main():
    print("=" * 70)
    print("GoldenHour: Synthetic Emergency Demand Generation (1 Year)")
    print("=" * 70)
    t_start = time.time()

    rng = np.random.default_rng(SEED)

    df_blr = generate_calls_for_region("blr", rng)
    df_tmk = generate_calls_for_region("tmk", rng)

    print("\n" + "=" * 70)
    print(
        f"Demand generation completed in {time.time() - t_start:.1f}s!\n"
        f"Bengaluru: {len(df_blr):,} calls (target: ~202k)\n"
        f"Tumakuru: {len(df_tmk):,} calls (target: ~36k)"
    )
    print("=" * 70)


if __name__ == "__main__":
    main()
