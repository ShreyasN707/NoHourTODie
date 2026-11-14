"""GoldenHour: Geographic data processing pipeline.

Extracts OSM road network, administrative boundaries, sites (POIs),
H3 hexagon cells, WorldPop populations, and road exposure metrics.
"""

import json
import math
import subprocess
import sys
import time
from pathlib import Path

# Ensure root directory is on sys.path
ROOT_PATH = Path(__file__).resolve().parent.parent
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

from typing import Dict, List, Set, Tuple

import geopandas as gpd
import h3
import numpy as np
import osmium
import pandas as pd
import rasterio
from scipy.spatial import cKDTree
import scipy.sparse as sp
import scipy.sparse.csgraph as csg
from shapely.geometry import Point, shape

from pipeline.config import (
    DATA_DIR,
    PROCESSED_DIR,
    RAW_DIR,
    REGIONS,
    ROAD_SPEEDS_KMH,
    STUDY_BBOX,
    WEB_DATA_DIR,
)

# Site selection priority for thinning: 1 per H3 cell
POI_PRIORITY = {
    "hospital": 1,
    "fire": 2,
    "police": 3,
    "clinic": 4,
    "health_centre": 4,
    "doctors": 4,
    "fuel": 5,
}

MAJOR_ROAD_TYPES = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in meters between two coordinates."""
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    )
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def step1_extract_bbox(raw_osm_path: Path, area_pbf_path: Path) -> None:
    """Extract study area bounding box using osmium CLI."""
    if area_pbf_path.exists() and area_pbf_path.stat().st_size > 10_000_000:
        print(f"[geo] {area_pbf_path} already exists ({area_pbf_path.stat().st_size / 1e6:.1f} MB). Skipping extract.")
        return

    bbox_str = f"{STUDY_BBOX[0]},{STUDY_BBOX[1]},{STUDY_BBOX[2]},{STUDY_BBOX[3]}"
    cmd = [
        "osmium",
        "extract",
        "-b",
        bbox_str,
        "-o",
        str(area_pbf_path),
        str(raw_osm_path),
        "--overwrite",
    ]
    print(f"[geo] Running osmium extract: {' '.join(cmd)}")
    t0 = time.time()
    subprocess.run(cmd, check=True)
    print(f"[geo] Extracted area PBF in {time.time() - t0:.1f}s")


class RoadAndPOIHandler(osmium.SimpleHandler):
    """Parses OSM ways and nodes to extract drivable roads and candidate standby sites."""

    def __init__(self):
        super().__init__()
        self.node_coords: Dict[int, Tuple[float, float]] = {}  # osm_id -> (lat, lon)
        self.raw_edges: List[Tuple[int, int, float]] = []  # (u, v, time_s)
        self.raw_pois: List[Tuple[str, str, float, float]] = []  # (type, name, lat, lon)
        self.major_segments: List[Tuple[float, float, float]] = []  # (lat, lon, length_km)

    def _check_poi_tags(self, tags) -> str | None:
        amenity = tags.get("amenity")
        healthcare = tags.get("healthcare")
        if amenity == "hospital":
            return "hospital"
        if amenity == "fire_station":
            return "fire"
        if amenity == "police":
            return "police"
        if amenity in ("clinic", "doctors"):
            return "clinic"
        if healthcare in ("centre", "clinic"):
            return "health_centre"
        if amenity == "fuel":
            return "fuel"
        return None

    def node(self, n):
        ptype = self._check_poi_tags(n.tags)
        if ptype:
            name = n.tags.get("name", "")
            self.raw_pois.append((ptype, name, n.location.lat, n.location.lon))

    def way(self, w):
        # Check if way represents a POI area/building
        ptype = self._check_poi_tags(w.tags)
        if ptype and len(w.nodes) > 0:
            try:
                lats = [n.lat for n in w.nodes]
                lons = [n.lon for n in w.nodes]
                name = w.tags.get("name", "")
                self.raw_pois.append(
                    (ptype, name, sum(lats) / len(lats), sum(lons) / len(lons))
                )
            except Exception:
                pass

        # Check highway
        hw = w.tags.get("highway")
        if not hw:
            return
        base_hw = hw[:-5] if hw.endswith("_link") else hw
        if base_hw not in ROAD_SPEEDS_KMH:
            return
        access = w.tags.get("access")
        if access in ("private", "no"):
            return

        speed_ms = (ROAD_SPEEDS_KMH[base_hw] / 3.6)
        oneway = w.tags.get("oneway")
        junction = w.tags.get("junction")

        is_oneway = (
            base_hw == "motorway"
            or junction == "roundabout"
            or oneway in ("yes", "1", "true")
        )
        is_reverse = oneway == "-1"

        nodes = [n for n in w.nodes]
        if len(nodes) < 2:
            return

        is_major = hw in MAJOR_ROAD_TYPES

        for i in range(len(nodes) - 1):
            n1, n2 = nodes[i], nodes[i + 1]
            try:
                lat1, lon1 = n1.lat, n1.lon
                lat2, lon2 = n2.lat, n2.lon
            except Exception:
                continue

            u, v = n1.ref, n2.ref
            self.node_coords[u] = (lat1, lon1)
            self.node_coords[v] = (lat2, lon2)

            d = haversine_m(lat1, lon1, lat2, lon2)
            t = d / speed_ms

            if is_major:
                mid_lat = (lat1 + lat2) / 2.0
                mid_lon = (lon1 + lon2) / 2.0
                self.major_segments.append((mid_lat, mid_lon, d / 1000.0))

            if is_reverse:
                self.raw_edges.append((v, u, t))
            elif is_oneway:
                self.raw_edges.append((u, v, t))
            else:
                self.raw_edges.append((u, v, t))
                self.raw_edges.append((v, u, t))


def build_road_graph(
    node_coords: Dict[int, Tuple[float, float]],
    raw_edges: List[Tuple[int, int, float]],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """De-duplicate edges, find largest strongly connected component, and return nodes & edges."""
    print(f"[geo] De-duplicating {len(raw_edges)} edges across {len(node_coords)} nodes...")
    t0 = time.time()

    # Map node IDs to 0..N-1
    unique_node_ids = sorted(node_coords.keys())
    node_id_to_idx = {nid: i for i, nid in enumerate(unique_node_ids)}
    n_nodes = len(unique_node_ids)

    # De-duplicate parallel edges keeping minimum time
    edge_dict: Dict[Tuple[int, int], float] = {}
    for u, v, t in raw_edges:
        if u == v:
            continue
        key = (node_id_to_idx[u], node_id_to_idx[v])
        if key not in edge_dict or t < edge_dict[key]:
            edge_dict[key] = t

    print(f"[geo] De-duplicated down to {len(edge_dict)} unique directed edges in {time.time() - t0:.1f}s")

    # Build CSR matrix for connected components
    rows = np.array([k[0] for k in edge_dict.keys()], dtype=np.int32)
    cols = np.array([k[1] for k in edge_dict.keys()], dtype=np.int32)
    data = np.array(list(edge_dict.values()), dtype=np.float32)

    adj = sp.csr_matrix((data, (rows, cols)), shape=(n_nodes, n_nodes))

    print("[geo] Finding largest strongly connected component...")
    t1 = time.time()
    n_components, labels = csg.connected_components(adj, directed=True, connection="strong")
    unique_labels, counts = np.unique(labels, return_counts=True)
    largest_label = unique_labels[np.argmax(counts)]
    largest_size = counts[np.argmax(counts)]
    print(
        f"[geo] Found {n_components} components in {time.time() - t1:.1f}s. "
        f"Largest strongly connected component size: {largest_size} nodes ({largest_size / n_nodes * 100:.1f}%)"
    )

    in_largest = labels == largest_label
    valid_old_indices = np.where(in_largest)[0]
    old_to_new_idx = {old: new for new, old in enumerate(valid_old_indices)}

    # Filter nodes
    scc_nodes_list = []
    for old_idx in valid_old_indices:
        nid = unique_node_ids[old_idx]
        lat, lon = node_coords[nid]
        scc_nodes_list.append({
            "node_idx": old_to_new_idx[old_idx],
            "osm_id": nid,
            "lat": lat,
            "lon": lon,
        })
    df_nodes = pd.DataFrame(scc_nodes_list)

    # Filter edges
    scc_edges_list = []
    for (u_old, v_old), t in edge_dict.items():
        if u_old in old_to_new_idx and v_old in old_to_new_idx:
            scc_edges_list.append({
                "u": old_to_new_idx[u_old],
                "v": old_to_new_idx[v_old],
                "time_s": t,
            })
    df_edges = pd.DataFrame(scc_edges_list)

    print(f"[geo] SCC Graph: {len(df_nodes):,} nodes and {len(df_edges):,} edges.")
    return df_nodes, df_edges


def extract_admin_zones() -> gpd.GeoDataFrame:
    """Extract administrative boundaries (admin_level 10 for blr, admin_level 6 for tmk)."""
    admin_pbf = RAW_DIR / "admin.osm.pbf"
    admin_seq = RAW_DIR / "admin.geojsonseq"
    area_pbf = RAW_DIR / "area.osm.pbf"

    if not admin_seq.exists() or admin_seq.stat().st_size == 0:
        print("[geo] Filtering admin boundaries with osmium tags-filter...")
        subprocess.run(
            [
                "osmium",
                "tags-filter",
                str(area_pbf),
                "r/boundary=administrative",
                "-o",
                str(admin_pbf),
                "--overwrite",
            ],
            check=True,
        )
        print("[geo] Exporting admin boundaries to geojsonseq...")
        subprocess.run(
            [
                "osmium",
                "export",
                "-f",
                "geojsonseq",
                "--geometry-types=polygon",
                "-o",
                str(admin_seq),
                str(admin_pbf),
                "--overwrite",
            ],
            check=True,
        )

    records = []
    with open(admin_seq, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                feat = json.loads(line)
                props = feat.get("properties", {})
                al = props.get("admin_level")
                if al in ("6", "10"):
                    geom = shape(feat.get("geometry"))
                    if geom.is_valid and not geom.is_empty:
                        records.append({
                            "name": props.get("name", "Unnamed"),
                            "admin_level": int(al),
                            "geometry": geom,
                        })
            except Exception:
                continue

    gdf_zones = gpd.GeoDataFrame(records, crs="EPSG:4326")
    gdf_dissolved = gdf_zones.dissolve(by=["name", "admin_level"], as_index=False)
    print(f"[geo] Parsed and dissolved {len(gdf_dissolved)} administrative zones.")
    return gdf_dissolved


def sample_population(
    cells: List[str], res: int, raster_path: Path
) -> np.ndarray:
    """Sample WorldPop 1km population using center and child-cell centroids."""
    coords = []
    for c in cells:
        clat, clon = h3.cell_to_latlng(c)
        coords.append((clon, clat))
        children = h3.cell_to_children(c, res + 1)
        for ch in children:
            chlat, chlon = h3.cell_to_latlng(ch)
            coords.append((chlon, chlat))

    with rasterio.open(raster_path) as src:
        vals = [v[0] for v in src.sample(coords)]
        vals = np.array(vals, dtype=np.float32)
        vals[vals < 0] = 0.0

    # Reshape: each cell has 1 (center) + len(children) samples
    n_samples_per_cell = 1 + len(h3.cell_to_children(cells[0], res + 1))
    means = vals.reshape(len(cells), n_samples_per_cell).mean(axis=1)

    # Pixel area at 13 deg latitude for 30 arc-sec (1/120 deg) raster is ~0.832 km2
    pixel_area_km2 = 0.83208
    density = means / pixel_area_km2
    hex_area_km2 = h3.average_hexagon_area(res, "km^2")
    cell_pop = density * hex_area_km2
    return cell_pop


def process_region(
    reg_code: str,
    geom_region,
    df_nodes: pd.DataFrame,
    node_tree: cKDTree,
    all_pois: List[Tuple[str, str, float, float]],
    major_segments: List[Tuple[float, float, float]],
    gdf_zones: gpd.GeoDataFrame,
    pop_raster_path: Path,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Process H3 cells, POIs, population, and major road length for a specific region."""
    cfg = REGIONS[reg_code]
    print(f"\n[geo] Processing region '{reg_code}' ({cfg.name})...")

    # 1. Generate H3 cells
    cells = list(h3.geo_to_cells(geom_region, cfg.h3_res))
    print(f"[geo] Region {reg_code}: {len(cells)} H3 cells at resolution {cfg.h3_res}.")

    # Compute cell centers
    cell_coords = [h3.cell_to_latlng(c) for c in cells]
    cell_lats = np.array([c[0] for c in cell_coords])
    cell_lons = np.array([c[1] for c in cell_coords])

    # 2. Population sampling
    print(f"[geo] Sampling population for {reg_code}...")
    cell_pops = sample_population(cells, cfg.h3_res, pop_raster_path)
    total_pop = cell_pops.sum()
    print(f"[geo] Total estimated population for {reg_code}: {total_pop / 1e6:.2f} M")

    # 3. Major road length per cell
    print(f"[geo] Calculating major road length per cell for {reg_code}...")
    major_km_per_cell = {c: 0.0 for c in cells}
    cells_set = set(cells)
    for mlat, mlon, seg_km in major_segments:
        c = h3.latlng_to_cell(mlat, mlon, cfg.h3_res)
        if c in cells_set:
            major_km_per_cell[c] += seg_km

    # 4. Administrative Zone Assignment (admin_level 10 for blr, admin_level 6 for tmk)
    print(f"[geo] Assigning administrative zones for {reg_code}...")
    zones_reg = gdf_zones[gdf_zones["admin_level"] == cfg.osm_admin_level].copy()
    if len(zones_reg) == 0:
        # Fallback to any matching zones
        zones_reg = gdf_zones.copy()

    # Create GeoDataFrame of cell center points
    cell_pts = gpd.GeoDataFrame(
        {"h3": cells},
        geometry=[Point(lon, lat) for lat, lon in zip(cell_lats, cell_lons)],
        crs="EPSG:4326",
    )
    # Spatial join nearest
    joined = gpd.sjoin_nearest(cell_pts, zones_reg[["name", "geometry"]], how="left")
    # In case of duplicates from boundary ties, drop duplicates
    joined = joined.drop_duplicates(subset=["h3"])
    cell_to_zone = dict(zip(joined["h3"], joined["name"].fillna("Unknown")))

    # 5. Snap cells to graph nodes
    print(f"[geo] Snapping cells to graph nodes for {reg_code}...")
    cos13 = math.cos(math.radians(13.0))
    cell_pts_proj = np.column_stack([cell_lats, cell_lons * cos13])
    _, cell_node_indices = node_tree.query(cell_pts_proj)

    df_cells = pd.DataFrame({
        "h3_index": cells,
        "lat": cell_lats,
        "lon": cell_lons,
        "pop": cell_pops,
        "major_km": [major_km_per_cell[c] for c in cells],
        "zone": [cell_to_zone.get(c, "Unknown") for c in cells],
        "node_idx": cell_node_indices,
    })

    # 6. Candidate Standby Sites (POIs) with thinning: 1 per H3 cell
    print(f"[geo] Thinning standby sites for {reg_code}...")
    cell_pois: Dict[str, Tuple[str, str, float, float]] = {}

    for ptype, name, plat, plon in all_pois:
        c = h3.latlng_to_cell(plat, plon, cfg.h3_res)
        if c in cells_set:
            if c not in cell_pois or POI_PRIORITY[ptype] < POI_PRIORITY[cell_pois[c][0]]:
                cell_pois[c] = (ptype, name, plat, plon)

    site_rows = []
    for site_idx, (c, (ptype, name, plat, plon)) in enumerate(cell_pois.items()):
        site_rows.append({
            "site_id": f"{reg_code}_s{site_idx:04d}",
            "h3_index": c,
            "site_type": ptype,
            "name": name if name else f"{ptype.title()} ({c[:8]})",
            "lat": plat,
            "lon": plon,
        })
    df_sites = pd.DataFrame(site_rows)

    # Snap sites to graph nodes
    site_pts_proj = np.column_stack([df_sites["lat"].values, df_sites["lon"].values * cos13])
    _, site_node_indices = node_tree.query(site_pts_proj)
    df_sites["node_idx"] = site_node_indices

    print(
        f"[geo] {reg_code}: {len(df_sites)} candidate sites thinned from raw POIs. "
        f"Site types: {df_sites['site_type'].value_counts().to_dict()}"
    )

    return df_cells, df_sites


def main():
    print("=" * 70)
    print("GoldenHour: Geographic Ingestion and Road Network Preparation")
    print("=" * 70)
    t_start = time.time()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)

    raw_osm = RAW_DIR / "southern-zone-latest.osm.pbf"
    area_pbf = RAW_DIR / "area.osm.pbf"
    pop_raster = RAW_DIR / "ind_ppp_2020_1km_Aggregated.tif"

    # Step 1: Extract BBOX
    step1_extract_bbox(raw_osm, area_pbf)

    # Step 2: Pyosmium handler for roads and POIs
    print(f"\n[geo] Parsing road network and candidate sites from {area_pbf}...")
    t0 = time.time()
    handler = RoadAndPOIHandler()
    handler.apply_file(str(area_pbf), locations=True)
    print(
        f"[geo] Parsing completed in {time.time() - t0:.1f}s. "
        f"Raw roads: {len(handler.raw_edges):,}, Nodes: {len(handler.node_coords):,}, "
        f"POIs: {len(handler.raw_pois):,}, Major road segments: {len(handler.major_segments):,}"
    )

    # Step 3 & 4: Deduplicate and Strongly Connected Component
    df_nodes, df_edges = build_road_graph(handler.node_coords, handler.raw_edges)

    # Save Graph
    graph_nodes_path = PROCESSED_DIR / "graph_nodes.parquet"
    graph_edges_path = PROCESSED_DIR / "graph_edges.parquet"
    print(f"[geo] Saving road graph to {graph_nodes_path} and {graph_edges_path}...")
    df_nodes.to_parquet(graph_nodes_path, index=False)
    df_edges.to_parquet(graph_edges_path, index=False)

    # Build cKDTree for graph snapping
    cos13 = math.cos(math.radians(13.0))
    node_pts_proj = np.column_stack([df_nodes["lat"].values, df_nodes["lon"].values * cos13])
    node_tree = cKDTree(node_pts_proj)

    # Step 8: Administrative boundaries
    gdf_zones = extract_admin_zones()

    # Step 7: Geocode study regions with osmnx
    import osmnx as ox
    print("[geo] Geocoding Bengaluru and Tumakuru region boundaries...")
    gdf_blr = ox.geocode_to_gdf("Bengaluru, Karnataka, India")
    geom_blr = gdf_blr.geometry.iloc[0]

    gdf_tmk = ox.geocode_to_gdf("Tumakuru district, Karnataka, India")
    geom_tmk = gdf_tmk.geometry.iloc[0]

    # Process Bengaluru
    df_cells_blr, df_sites_blr = process_region(
        "blr",
        geom_blr,
        df_nodes,
        node_tree,
        handler.raw_pois,
        handler.major_segments,
        gdf_zones,
        pop_raster,
    )
    df_cells_blr.to_parquet(PROCESSED_DIR / "cells_blr.parquet", index=False)
    df_sites_blr.to_parquet(PROCESSED_DIR / "sites_blr.parquet", index=False)

    # Process Tumakuru
    df_cells_tmk, df_sites_tmk = process_region(
        "tmk",
        geom_tmk,
        df_nodes,
        node_tree,
        handler.raw_pois,
        handler.major_segments,
        gdf_zones,
        pop_raster,
    )
    df_cells_tmk.to_parquet(PROCESSED_DIR / "cells_tmk.parquet", index=False)
    df_sites_tmk.to_parquet(PROCESSED_DIR / "sites_tmk.parquet", index=False)

    # Save simplified zones GeoJSON for frontend
    print("[geo] Simplifying and saving zones GeoJSON...")
    gdf_zones_simplified = gdf_zones.copy()
    gdf_zones_simplified["geometry"] = gdf_zones_simplified["geometry"].simplify(
        0.0005, preserve_topology=True
    )
    zones_geojson_path = PROCESSED_DIR / "zones.geojson"
    gdf_zones_simplified.to_file(zones_geojson_path, driver="GeoJSON")

    # Also copy zones.geojson to web public data directory
    gdf_zones_simplified.to_file(WEB_DATA_DIR / "zones.geojson", driver="GeoJSON")

    print("\n" + "=" * 70)
    print(f"Geo pipeline finished successfully in {time.time() - t_start:.1f}s!")
    print(f"Outputs written to {PROCESSED_DIR} and {WEB_DATA_DIR}")
    print("=" * 70)


if __name__ == "__main__":
    main()
