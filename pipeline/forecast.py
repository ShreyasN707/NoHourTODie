"""GoldenHour: Demand forecasting pipeline using native LightGBM Poisson regression.

Predicts 4-hour block demand per administrative zone:
  - Features: zone (categorical, int32), block (0-5), day of week, month,
    lag7, lag14, rolling_mean28, zone population.
  - Model: native lgb.train Poisson objective, 400 rounds, learning_rate=0.05.
  - Evaluated on the last 8 weeks vs 4-week historical block baseline.
  - Downscales zone forecasts to H3 cells using historical spatial shares.
  - Outputs lam_{region}.parquet and forecast.json.
"""

import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

# Ensure root directory is on sys.path
ROOT_PATH = Path(__file__).resolve().parent.parent
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

import lightgbm as lgb
import numpy as np
import pandas as pd

from pipeline.config import (
    PLANNING_BLOCKS,
    PROCESSED_DIR,
    REGIONS,
    SEED,
    WEB_DATA_DIR,
)

TEST_DAYS = 56  # 8 weeks


def build_zone_panel(reg_code: str) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, float]]:
    """Build complete (zone, day, block) panel with lag and rolling features."""
    df_calls = pd.read_parquet(PROCESSED_DIR / f"calls_{reg_code}.parquet")
    df_cells = pd.read_parquet(PROCESSED_DIR / f"cells_{reg_code}.parquet")

    # Zone populations
    zone_pops = df_cells.groupby("zone")["pop"].sum().to_dict()

    # Aggregate calls to zone x day x block
    agg = (
        df_calls.groupby(["zone", "day_of_year", "block"])
        .size()
        .reset_index(name="calls")
    )

    zones = sorted(df_cells["zone"].unique())
    n_days = 365
    n_blocks = 6

    # Create full multi-index cartesian product
    idx = pd.MultiIndex.from_product(
        [zones, range(n_days), range(n_blocks)],
        names=["zone", "day_of_year", "block"],
    )
    panel = pd.DataFrame(index=idx).reset_index()

    # Merge aggregated counts
    panel = panel.merge(agg, on=["zone", "day_of_year", "block"], how="left")
    panel["calls"] = panel["calls"].fillna(0).astype(np.float32)

    # Sort to create time series per (zone, block)
    panel = panel.sort_values(["zone", "block", "day_of_year"]).reset_index(drop=True)

    # Date / Calendar features
    # Day 0 is 2025-11-01 (Saturday -> dow=5)
    start_date = pd.Timestamp("2025-11-01")
    panel["date"] = start_date + pd.to_timedelta(panel["day_of_year"], unit="D")
    panel["day_of_week"] = panel["date"].dt.dayofweek.astype(np.int32)
    panel["month"] = panel["date"].dt.month.astype(np.int32)
    panel["zone_pop"] = panel["zone"].map(zone_pops).fillna(1000.0).astype(np.float32)

    # Shift features within each (zone, block) group
    grouped = panel.groupby(["zone", "block"])["calls"]
    panel["lag7"] = grouped.shift(7).astype(np.float32)
    panel["lag14"] = grouped.shift(14).astype(np.float32)
    panel["lag21"] = grouped.shift(21).astype(np.float32)
    panel["lag28"] = grouped.shift(28).astype(np.float32)

    # 4-week historical average baseline (mean of lags 7, 14, 21, 28)
    panel["baseline_pred"] = (
        panel[["lag7", "lag14", "lag21", "lag28"]].mean(axis=1).astype(np.float32)
    )

    # Rolling mean 28 days (excluding current day, min_periods=7)
    panel["rolling_mean28"] = (
        grouped.transform(
            lambda s: s.shift(1).rolling(window=28, min_periods=7).mean()
        ).astype(np.float32)
    )

    # Ensure zone is categorical with safe integer codes (avoids int8 overflow)
    panel["zone"] = panel["zone"].astype("category")
    # Cast cat codes safely
    panel["zone_code"] = panel["zone"].cat.codes.astype(np.int32)

    # Cell-level historical call shares
    cell_calls = df_calls.groupby(["zone", "h3_index"]).size().reset_index(name="cell_calls")
    zone_tot_calls = df_calls.groupby("zone").size().to_dict()
    cell_calls["zone_tot"] = cell_calls["zone"].map(zone_tot_calls)
    cell_calls["cell_share"] = cell_calls["cell_calls"] / cell_calls["zone_tot"]
    cell_share_map = dict(zip(cell_calls["h3_index"], cell_calls["cell_share"]))

    return panel, df_cells, cell_share_map


def train_and_evaluate(reg_code: str):
    """Train LightGBM Poisson model and compute test set metrics."""
    panel, df_cells, cell_share_map = build_zone_panel(reg_code)

    # Train / Test split
    split_day = 365 - TEST_DAYS  # Day 309
    train_mask = (panel["day_of_year"] >= 28) & (panel["day_of_year"] < split_day)
    test_mask = panel["day_of_year"] >= split_day

    feature_cols = [
        "zone",
        "block",
        "day_of_week",
        "month",
        "lag7",
        "lag14",
        "rolling_mean28",
        "zone_pop",
    ]

    train_data = panel[train_mask].dropna(subset=["lag7", "lag14", "rolling_mean28"])
    test_data = panel[test_mask].dropna(subset=["lag7", "lag14", "rolling_mean28", "baseline_pred"])

    X_train = train_data[feature_cols].copy()
    y_train = train_data["calls"].values

    X_test = test_data[feature_cols].copy()
    y_test = test_data["calls"].values
    baseline_pred = test_data["baseline_pred"].values

    print(f"[forecast] Region {reg_code}: {len(X_train)} train rows, {len(X_test)} test rows.")

    # LightGBM Dataset
    dtrain = lgb.Dataset(
        X_train,
        label=y_train,
        categorical_feature=["zone"],
        free_raw_data=False,
    )
    dtest = lgb.Dataset(
        X_test,
        label=y_test,
        categorical_feature=["zone"],
        reference=dtrain,
        free_raw_data=False,
    )

    params = {
        "objective": "poisson",
        "metric": ["poisson", "mae"],
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_child_samples": 20,
        "feature_fraction": 0.9,
        "seed": SEED,
        "verbose": -1,
    }

    bst = lgb.train(
        params,
        dtrain,
        num_boost_round=400,
        valid_sets=[dtest],
    )

    # Predictions
    lgb_pred = bst.predict(X_test)

    # Evaluation metrics
    mae_lgb = float(np.mean(np.abs(y_test - lgb_pred)))
    mae_base = float(np.mean(np.abs(y_test - baseline_pred)))
    rmse_lgb = float(np.sqrt(np.mean((y_test - lgb_pred) ** 2)))
    rmse_base = float(np.sqrt(np.mean((y_test - baseline_pred) ** 2)))
    mae_improvement = float((mae_base - mae_lgb) / mae_base * 100.0)

    print(
        f"[forecast] Evaluation for {reg_code} (Ward/Taluk x Block):\n"
        f"  Baseline 4-wk MAE: {mae_base:.4f}, RMSE: {rmse_base:.4f}\n"
        f"  LightGBM MAE:     {mae_lgb:.4f}, RMSE: {rmse_lgb:.4f}\n"
        f"  Error Reduction:   {mae_improvement:.2f}%"
    )

    # Downscale to H3 cells for planning lambda
    # Compute mean forecast per zone x block across test horizon
    test_data = test_data.copy()
    test_data["lgb_pred"] = lgb_pred
    zone_block_means = (
        test_data.groupby(["zone", "block"])["lgb_pred"].mean().unstack(level="block")
    )

    # Map to cells
    cell_rows = []
    for _, crow in df_cells.iterrows():
        c = crow["h3_index"]
        z = crow["zone"]
        share = cell_share_map.get(c, 0.0)
        if share <= 0:
            # Fallback by population share within zone
            z_cells = df_cells[df_cells["zone"] == z]
            z_pop = z_cells["pop"].sum()
            share = crow["pop"] / z_pop if z_pop > 0 else (1.0 / len(z_cells))

        # Lambda per hour = (block forecast * cell share) / 4 hours
        if z in zone_block_means.index:
            block_rates = [
                float(zone_block_means.loc[z, b] * share / 4.0) for b in range(6)
            ]
        else:
            block_rates = [0.01] * 6

        all_day_mean = float(np.mean(block_rates))
        cell_rows.append({
            "h3_index": c,
            "block_0": block_rates[0],
            "block_1": block_rates[1],
            "block_2": block_rates[2],
            "block_3": block_rates[3],
            "block_4": block_rates[4],
            "block_5": block_rates[5],
            "all_day_mean": all_day_mean,
        })

    df_lam = pd.DataFrame(cell_rows)
    df_lam.to_parquet(PROCESSED_DIR / f"lam_{reg_code}.parquet", index=False)
    print(
        f"[forecast] Saved cell demand lambda to {PROCESSED_DIR / f'lam_{reg_code}.parquet'}. "
        f"Total calls/hour: {df_lam['all_day_mean'].sum():.2f} (daily: {df_lam['all_day_mean'].sum() * 24:.1f})"
    )

    # Prepare sample timeline for frontend chart
    # Average across all zones by day for test horizon
    test_timeline = (
        test_data.groupby("date")[["calls", "lgb_pred", "baseline_pred"]]
        .sum()
        .reset_index()
    )
    test_timeline["date_str"] = test_timeline["date"].dt.strftime("%Y-%m-%d")

    timeline_data = [
        {
            "date": row["date_str"],
            "actual": round(float(row["calls"]), 1),
            "forecast": round(float(row["lgb_pred"]), 1),
            "baseline": round(float(row["baseline_pred"]), 1),
        }
        for _, row in test_timeline.iterrows()
    ]

    return {
        "reg_code": reg_code,
        "mae_lgb": round(mae_lgb, 4),
        "mae_baseline": round(mae_base, 4),
        "rmse_lgb": round(rmse_lgb, 4),
        "rmse_baseline": round(rmse_base, 4),
        "mae_improvement_pct": round(mae_improvement, 2),
        "timeline": timeline_data[-28:],  # last 4 weeks of test set for SVG chart
    }


def main():
    print("=" * 70)
    print("GoldenHour: Demand Forecasting with LightGBM Poisson")
    print("=" * 70)
    t_start = time.time()

    metrics_blr = train_and_evaluate("blr")
    metrics_tmk = train_and_evaluate("tmk")

    out_json = {
        "blr": metrics_blr,
        "tmk": metrics_tmk,
        "method": "LightGBM Poisson vs 4-Week Lag Baseline",
        "description": "Demand forecast trained on 44 weeks, evaluated on final 8 weeks. Downscaled to H3 cells for MILP optimization.",
    }

    forecast_json_path = PROCESSED_DIR / "forecast.json"
    with open(forecast_json_path, "w", encoding="utf-8") as f:
        json.dump(out_json, f, indent=2)

    with open(WEB_DATA_DIR / "forecast.json", "w", encoding="utf-8") as f:
        json.dump(out_json, f, indent=2)

    print("\n" + "=" * 70)
    print(f"Forecasting pipeline completed in {time.time() - t_start:.1f}s!")
    print(f"Metrics written to {forecast_json_path} and {WEB_DATA_DIR / 'forecast.json'}")
    print("=" * 70)


if __name__ == "__main__":
    main()
