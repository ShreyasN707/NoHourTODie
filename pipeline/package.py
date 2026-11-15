"""GoldenHour: seed the pipeline from the team data package instead of re-running it from raw OSM/WorldPop.

`make geo travel demand forecast export` needs the 580 MB OSM extract and takes hours. The GoldenHour data
package (`GoldenHour_Data.zip`, unpacked into data/raw/goldenhour_data/) holds every intermediate result of
that run, so this script turns it back into the files the later stages read:

  data/processed/  cells_{r}.parquet, sites_{r}.parquet, lam_{r}.parquet, calls_{r}.parquet,
                   travel_{r}.npz, graph_nodes.parquet, graph_edges.parquet
  web/public/data/ cells_{r}.json, sites_{r}.json, scenarios_{r}.json, meta.json, zones.geojson, forecast.json

Existing files are kept unless --force is given, so a real pipeline run is never overwritten by the package.
The calls in the package are synthetic (see its README); nothing here makes them real.
"""

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT_PATH = Path(__file__).resolve().parent.parent
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

import pandas as pd

from pipeline.config import PROCESSED_DIR, RAW_DIR, REGIONS, ROOT_DIR, WEB_DATA_DIR

PKG_DIR = RAW_DIR / "goldenhour_data"
PKG_ZIP = ROOT_DIR / "GoldenHour_Data.zip"
PKG_FOLDERS = ["01_demand", "02_geography", "03_road_network_and_travel_times", "04_results"]


def ensure_unpacked() -> None:
    """Unpack any package folder that is missing (the zip nests everything under GoldenHour_Data/)."""
    missing = [f for f in PKG_FOLDERS if not (PKG_DIR / f).is_dir()]
    if not missing:
        return
    if not PKG_ZIP.exists():
        raise SystemExit(f"[package] {', '.join(missing)} missing from {PKG_DIR} and no {PKG_ZIP.name} to unpack.")
    print(f"[package] Unpacking {', '.join(missing)} from {PKG_ZIP.name}...")
    with zipfile.ZipFile(PKG_ZIP) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts
            if len(parts) < 3 or parts[0] != "GoldenHour_Data" or parts[1] not in missing or info.is_dir():
                continue
            dest = PKG_DIR.joinpath(*parts[1:])
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def _write(path: Path, force: bool, writer) -> None:
    if path.exists() and not force:
        print(f"[package] keep   {path.relative_to(ROOT_DIR)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    writer(path)
    print(f"[package] wrote  {path.relative_to(ROOT_DIR)}")


def _copy(src: Path, dest: Path, force: bool) -> None:
    _write(dest, force, lambda p: shutil.copyfile(src, p))


def processed_region(reg: str, force: bool) -> None:
    geo, dem = PKG_DIR / "02_geography", PKG_DIR / "01_demand"
    cells = pd.read_csv(geo / f"hexagon_cells_{reg}.csv")
    sites = pd.read_csv(geo / f"standby_sites_{reg}.csv", keep_default_na=False)
    sites["name"] = sites["name"].astype(str)

    # The optimiser reads lam_{r} positionally, so it must follow the cell order exactly.
    lam = pd.read_csv(dem / f"forecast_calls_per_hour_by_cell_{reg}.csv").set_index("h3_index")
    lam = lam.reindex(cells["h3_index"]).reset_index()
    if lam.isna().any().any():
        raise SystemExit(f"[package] forecast for {reg} does not cover every hexagon")

    _write(PROCESSED_DIR / f"cells_{reg}.parquet", force, lambda p: cells.to_parquet(p, index=False))
    _write(PROCESSED_DIR / f"sites_{reg}.parquet", force, lambda p: sites.to_parquet(p, index=False))
    _write(PROCESSED_DIR / f"lam_{reg}.parquet", force, lambda p: lam.to_parquet(p, index=False))

    def write_calls(p: Path) -> None:
        calls = pd.read_csv(dem / f"calls_{reg}_synthetic_year.csv", parse_dates=["timestamp", "date"])
        for col in ("needs_als", "bike_helps"):
            calls[col] = calls[col].astype(bool)
        calls.to_parquet(p, index=False)

    _write(PROCESSED_DIR / f"calls_{reg}.parquet", force, write_calls)
    _copy(PKG_DIR / "03_road_network_and_travel_times" / f"travel_times_{reg}.npz",
          PROCESSED_DIR / f"travel_{reg}.npz", force)


def web_region(reg: str, force: bool) -> None:
    from pipeline.export import write_cells_json, write_sites_json

    _write(WEB_DATA_DIR / f"cells_{reg}.json", force, lambda p: write_cells_json(reg, p))
    _write(WEB_DATA_DIR / f"sites_{reg}.json", force, lambda p: write_sites_json(reg, p))
    _copy(PKG_DIR / "04_results" / f"dashboard_scenarios_{reg}.json", WEB_DATA_DIR / f"scenarios_{reg}.json", force)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="overwrite files that already exist")
    args = ap.parse_args()

    ensure_unpacked()
    net = PKG_DIR / "03_road_network_and_travel_times"
    _copy(net / "road_graph_nodes.parquet", PROCESSED_DIR / "graph_nodes.parquet", args.force)
    _copy(net / "road_graph_edges.parquet", PROCESSED_DIR / "graph_edges.parquet", args.force)
    for reg in REGIONS:
        processed_region(reg, args.force)
        web_region(reg, args.force)

    _copy(PKG_DIR / "02_geography" / "wards_and_taluks.geojson", WEB_DATA_DIR / "zones.geojson", args.force)
    _copy(PKG_DIR / "01_demand" / "forecast_metrics.json", WEB_DATA_DIR / "forecast.json", args.force)

    def write_meta(p: Path) -> None:
        from pipeline.export import build_metadata
        p.write_text(json.dumps(build_metadata(), indent=2), encoding="utf-8")

    _write(WEB_DATA_DIR / "meta.json", args.force, write_meta)
    print("[package] Done. Next: make blackspots (or make export to re-plan from scratch).")


if __name__ == "__main__":
    main()
