"""GoldenHour: Travel time matrix computation using Dijkstra on OSM road graph.

Computes:
  - fwd: Site -> Cell travel time (seconds)
  - back: Cell -> Site travel time (seconds, for hospital returns)
  - ss: Site -> Site travel time (seconds, for crew relocation moves)
"""

import sys
import time
from pathlib import Path

# Ensure root directory is on sys.path
ROOT_PATH = Path(__file__).resolve().parent.parent
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

import numpy as np
import pandas as pd
import scipy.sparse as sp
import scipy.sparse.csgraph as csg

from pipeline.config import (
    AMBULANCE_SPEED_MULTIPLIER,
    BENGALURU_HOURLY_CONGESTION,
    PROCESSED_DIR,
    REGIONS,
)

LIMIT_SECONDS = 10800.0  # 3 hours cap for unreachable nodes
CHUNK_SIZE = 100


def get_hourly_factors(reg_code: str) -> list[float]:
    """Get the 24 hourly traffic multipliers for the region."""
    cfg = REGIONS[reg_code]
    if cfg.congestion_enabled:
        return list(BENGALURU_HOURLY_CONGESTION)
    else:
        return [1.10] * 24


def compute_travel_matrices(reg_code: str, adj: sp.csr_matrix, adj_t: sp.csr_matrix):
    """Compute fwd, back, and ss travel time matrices in seconds for a region."""
    print(f"\n[travel] Computing travel matrices for region '{reg_code}'...")
    t0 = time.time()

    df_cells = pd.read_parquet(PROCESSED_DIR / f"cells_{reg_code}.parquet")
    df_sites = pd.read_parquet(PROCESSED_DIR / f"sites_{reg_code}.parquet")

    n_cells = len(df_cells)
    n_sites = len(df_sites)
    cell_nodes = df_cells["node_idx"].values.astype(np.int32)
    site_nodes = df_sites["node_idx"].values.astype(np.int32)

    print(f"[travel] Region {reg_code}: {n_sites} sites, {n_cells} cells.")

    fwd = np.full((n_sites, n_cells), LIMIT_SECONDS, dtype=np.float32)
    ss = np.full((n_sites, n_sites), LIMIT_SECONDS, dtype=np.float32)
    # back is (n_cells, n_sites)
    back = np.full((n_cells, n_sites), LIMIT_SECONDS, dtype=np.float32)

    # 1. Forward & Site-to-Site Dijkstra on adj
    print(f"[travel] Running Forward Dijkstra for {n_sites} sites in chunks of {CHUNK_SIZE}...")
    for i in range(0, n_sites, CHUNK_SIZE):
        batch_end = min(i + CHUNK_SIZE, n_sites)
        batch_sources = site_nodes[i:batch_end]

        dist = csg.dijkstra(
            adj,
            directed=True,
            indices=batch_sources,
            limit=LIMIT_SECONDS,
            return_predecessors=False,
        )
        # Cap unreached
        dist = np.where((dist >= LIMIT_SECONDS) | np.isnan(dist), LIMIT_SECONDS, dist).astype(np.float32)

        fwd[i:batch_end, :] = dist[:, cell_nodes]
        ss[i:batch_end, :] = dist[:, site_nodes]

    print(f"[travel] Forward Dijkstra finished in {time.time() - t0:.1f}s.")

    # 2. Backward Dijkstra (Cell -> Site) on adj_t
    # In adj_t, distance from site to cell equals distance from cell to site in adj!
    t1 = time.time()
    print(f"[travel] Running Backward Dijkstra (on transposed graph) for {n_sites} sites...")
    for i in range(0, n_sites, CHUNK_SIZE):
        batch_end = min(i + CHUNK_SIZE, n_sites)
        batch_sources = site_nodes[i:batch_end]

        dist_t = csg.dijkstra(
            adj_t,
            directed=True,
            indices=batch_sources,
            limit=LIMIT_SECONDS,
            return_predecessors=False,
        )
        dist_t = np.where((dist_t >= LIMIT_SECONDS) | np.isnan(dist_t), LIMIT_SECONDS, dist_t).astype(np.float32)

        # dist_t is (batch_len, n_nodes_total). dist_t[:, cell_nodes] is (batch_len, n_cells).
        # We assign to back[:, i:batch_end] as transpose:
        back[:, i:batch_end] = dist_t[:, cell_nodes].T

    print(f"[travel] Backward Dijkstra finished in {time.time() - t1:.1f}s.")

    out_file = PROCESSED_DIR / f"travel_{reg_code}.npz"
    np.savez_compressed(out_file, fwd=fwd, back=back, ss=ss)
    print(f"[travel] Saved travel matrices to {out_file} ({out_file.stat().st_size / 1e6:.2f} MB).")

    # Diagnostic stats (in minutes)
    fwd_min = fwd / 60.0
    back_min = back / 60.0
    print(
        f"[travel] Stats for {reg_code} (free-flow minutes):\n"
        f"  fwd nearest site mean: {fwd_min.min(axis=0).mean():.1f} min, "
        f"max: {fwd_min.min(axis=0).max():.1f} min\n"
        f"  back nearest site mean: {back_min.min(axis=1).mean():.1f} min, "
        f"max: {back_min.min(axis=1).max():.1f} min\n"
        f"  site-to-site mean: {ss[ss < LIMIT_SECONDS].mean() / 60.0:.1f} min"
    )


def main():
    print("=" * 70)
    print("GoldenHour: Road Network Travel Time Computation")
    print("=" * 70)
    t_start = time.time()

    df_nodes = pd.read_parquet(PROCESSED_DIR / "graph_nodes.parquet")
    df_edges = pd.read_parquet(PROCESSED_DIR / "graph_edges.parquet")

    n_nodes = len(df_nodes)
    print(f"[travel] Loaded graph with {n_nodes:,} nodes and {len(df_edges):,} edges.")

    rows = df_edges["u"].values.astype(np.int32)
    cols = df_edges["v"].values.astype(np.int32)
    weights = df_edges["time_s"].values.astype(np.float32)

    print("[travel] Constructing CSR adjacency matrices...")
    adj = sp.csr_matrix((weights, (rows, cols)), shape=(n_nodes, n_nodes))
    adj_t = adj.transpose().tocsr()

    # Compute for Bengaluru and Tumakuru
    compute_travel_matrices("blr", adj, adj_t)
    compute_travel_matrices("tmk", adj, adj_t)

    print("\n" + "=" * 70)
    print(f"Travel matrix pipeline finished in {time.time() - t_start:.1f}s!")
    print("=" * 70)


if __name__ == "__main__":
    main()
