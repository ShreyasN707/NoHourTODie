"""Offline place search over every named OpenStreetMap feature in the study area (no external requests).

Build once (also built automatically by the assistant on first start):
    .venv/bin/python backend/places.py
"""
import json, re, subprocess, sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.config import PROCESSED_DIR, RAW_DIR

OUT = PROCESSED_DIR / "places.parquet"
# How useful a feature is as a pickup point; higher wins ties
RANK = {"hospital": 9, "clinic": 8, "college": 8, "university": 8, "school": 7, "place": 7, "police": 6,
        "fire_station": 6, "townhall": 6, "place_of_worship": 5, "shop": 4, "office": 4, "building": 4,
        "amenity": 4, "bus_stop": 3, "street": 2}


def kind_of(p: dict) -> str:
    if p.get("amenity") in RANK:
        return p["amenity"]
    if "place" in p:
        return "place"
    if p.get("highway") == "bus_stop":
        return "bus_stop"
    if "highway" in p:
        return "street"
    for k in ("shop", "office", "building", "amenity"):
        if k in p:
            return k
    return "other"


def build():
    tmp_pbf, tmp_geo = PROCESSED_DIR / "named.osm.pbf", PROCESSED_DIR / "named.geojsonseq"
    subprocess.run(["osmium", "tags-filter", str(RAW_DIR / "area.osm.pbf"), "nwr/name", "-o", str(tmp_pbf), "--overwrite"], check=True)
    subprocess.run(["osmium", "export", str(tmp_pbf), "-f", "geojsonseq", "-o", str(tmp_geo), "--overwrite"], check=True)
    rows = []
    for line in open(tmp_geo):
        f = json.loads(line.lstrip("\x1e"))
        p, g = f["properties"], f["geometry"]
        if not p.get("name"):
            continue
        pts = np.array(_coords(g["coordinates"]), float).reshape(-1, 2)
        k = kind_of(p)
        if k == "other":
            continue
        area = " ".join(x for x in (p.get("addr:street"), p.get("addr:suburb"), p.get("loc_name")) if x)
        rows.append(dict(name=p["name"], name_kn=p.get("name:kn", ""), kind=k, rank=RANK.get(k, 1), area=area,
                         housenumber=p.get("addr:housenumber", ""), emergency=p.get("emergency", ""), lon=float(pts[:, 0].mean()), lat=float(pts[:, 1].mean())))
    df = pd.DataFrame(rows)
    df["key"] = df.name.map(norm)
    df = df.sort_values("rank", ascending=False).drop_duplicates(["key", "kind"]).reset_index(drop=True)
    df.to_parquet(OUT)
    tmp_pbf.unlink(); tmp_geo.unlink()
    print(f"[places] {len(df):,} named places -> {OUT}")


def _coords(c):
    return [c] if isinstance(c[0], (int, float)) else [x for y in c for x in _coords(y)]


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(s).lower()).strip()


STOP = {"of", "the", "and", "in", "at", "near", "opp", "opposite", "behind", "road", "rd", "no", "house", "karnataka", "india"}


def search(df: pd.DataFrame, query: str, k: int = 3) -> list:
    """Rank named places by how many query words they contain (name beats street/area), then usefulness.
    Partial matches are allowed, so "Dayananda Sagar College of Engineering (DSCE)" still finds
    "Dayananda Sagar College"."""
    words = [w for w in norm(re.sub(r"\(.*?\)", " ", query)).split() if w not in STOP and len(w) > 1]
    words = [w for w in words if not w.isdigit()] or words  # house numbers are rarely mapped; match the street/area
    words = [w[:max(5, len(w) - 2)] if len(w) >= 7 else w for w in words]  # tolerate spelling tails: Dayananda ~ Dayanand
    if not words:
        return []
    key = " " + df.key
    hay = key + " " + df.area.str.lower()
    in_name = [key.str.contains(" " + w, regex=False).values for w in words]   # word-start match: "4th" != "24th"
    in_any = [hay.str.contains(" " + w, regex=False).values for w in words]
    idf = [np.log(len(df) / (1 + x.sum())) for x in in_any]                    # rare words (localities) count more
    total = sum(idf) or 1.0
    name_w = sum(w * x for w, x in zip(idf, in_name)) / total
    any_w = sum(w * x for w, x in zip(idf, in_any)) / total
    m = any_w >= 0.6  # at least 60% of the query's information matched
    if not m.any():
        return []
    hit = df[m]
    score = (name_w[m] * 100 + any_w[m] * 40 + hit["rank"].values * 3
             - np.clip(np.abs(hit.key.str.len().values - len(" ".join(words))), 0, 60) / 6)
    return [dict(label=("" if r.nw > 0.99 else "near ") + f"{r.name} ({r.kind.replace('_', ' ')})", lat=r.lat, lon=r.lon,
                 precision="exact place" if r.nw > 0.99 else "approximate", source="OpenStreetMap (offline)")
            for r in hit.assign(s=score, nw=name_w[m]).sort_values("s", ascending=False).head(k).itertuples()]


if __name__ == "__main__":
    build()
    df = pd.read_parquet(OUT)
    for q in ("Dayananda Sagar College", "Sadashivanagar", "Victoria Hospital"):
        print(q, "->", search(df, q, 2))
