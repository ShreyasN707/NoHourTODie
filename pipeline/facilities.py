"""Which OpenStreetMap "hospitals" can actually receive a 108 emergency patient.

OSM tags dental clinics, eye hospitals, labs, Ayurveda pharmacies and vet clinics as amenity=hospital. Those
must not be today's ambulance bases or handover hospitals. The Gemini decision model (gemini-decision-1) asks
two typed questions per name: a Noul, P(a 108 ambulance would bring an emergency patient here), and a Choice for
the facility type. Answers are calibrated probabilities and are cached, so re-running after `make geo` makes no
API calls. Below DROP_BELOW the facility is relabelled `clinic` (still a candidate standby site, no longer a
hospital); the band up to 0.5 stays a hospital and is listed for a person to review (fail open: a missing
hospital is worse than an extra one).

Run after geo, before travel:  .venv/bin/python pipeline/facilities.py   (needs GEMINI_API_KEY in .env)
"""
import json, os, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.config import PROCESSED_DIR, REGIONS

CACHE = PROCESSED_DIR / "facility_check.json"
MODEL = "models/gemini-decision-1"
DROP_BELOW = 0.2  # ponytail: hand-picked threshold; set it from a teammate's blind labels of ~60 names
QUESTIONS = {
    "emergency": {
        "type": "noul",
        "instructions": "Would a 108 ambulance take an emergency patient (accident, chest pain, stroke, breathing "
                        "trouble, labour) to the facility named in `facility.name`? Judge from the name only.",
        "criteria": {"true_description": "general, multispeciality or government hospital, PHC/CHC, trauma or cardiac "
                                         "centre, maternity or women's hospital (108 carries many labour cases)",
                     "false_description": "eye, dental, diagnostics/lab, blood bank, pharmacy, Ayurveda/homeopathy/"
                                          "Unani, IVF-only, skin/hair, physiotherapy, veterinary, not a health facility"}},
    "category": {
        "type": "choice",
        "instructions": "What kind of facility is named in `facility.name`?",
        "criteria": {"general": "general, multispeciality or government hospital, PHC/CHC",
                     "maternity": "maternity, women's or children's hospital",
                     "trauma_or_cardiac": "trauma, accident, heart or emergency centre",
                     "specialty_non_emergency": "eye, dental, skin, IVF, physiotherapy, piles, other single-speciality clinic",
                     "traditional": "Ayurveda, homeopathy, Unani, Siddha, naturopathy",
                     "lab_or_pharmacy": "diagnostics, lab, scan centre, blood bank, pharmacy",
                     "veterinary": "animal hospital",
                     "not_health": "not a health facility",
                     "unclear": "the name does not say what kind of facility it is"}},
}


def judge(rows: list[dict]) -> dict:
    from google import genai
    for line in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    def one(row):
        state = {"context": "Karnataka 108 emergency ambulance service. A map feature tagged as a hospital in "
                            "OpenStreetMap; the decision is whether ambulances should bring emergency patients here.",
                 "facility": {"name": row["name"], "district": row["district"]}}
        try:
            a = client.decisions.create(api_version="v1beta", model=MODEL, questions=QUESTIONS,
                                        input=json.dumps(state, ensure_ascii=False), timeout=30).answers
            return row["id"], dict(p_emergency=round(a["emergency"].noul, 3), category=a["category"].choice,
                                   category_conf=round(a["category"].confidence, 3))
        except Exception as e:  # unjudged stays a hospital; reported, never guessed
            print(f"[facilities] unjudged {row['name']!r}: {type(e).__name__}")
            return row["id"], None

    with ThreadPoolExecutor(8) as pool:
        out = {k: v for k, v in pool.map(one, rows) if v}
    print(f"[facilities] judged {len(out)}/{len(rows)}")
    return out


def judge_place_index(cache: dict):
    """The assistant picks destinations from every mapped hospital (backend/places.py index), not only the
    thinned standby sites, so judge those names too. Same cache and keys; sites are not touched."""
    import h3
    path = PROCESSED_DIR / "places.parquet"
    if not path.exists():
        return
    h = pd.read_parquet(path).query("kind == 'hospital'")
    for r, res in (("blr", 8), ("tmk", 7)):
        cells = set(pd.read_parquet(PROCESSED_DIR / f"cells_{r}.parquet").h3_index)
        names = {n for n, a, b in zip(h.name, h.lat, h.lon) if h3.latlng_to_cell(a, b, res) in cells}
        todo = [dict(id=f"{r}|{n}", name=n, district=REGIONS[r].name) for n in sorted(names) if f"{r}|{n}" not in cache]
        if todo:
            cache.update(judge(todo))
            CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))


def main():
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    judge_place_index(cache)
    for r in REGIONS:
        path = PROCESSED_DIR / f"sites_{r}.parquet"
        sites = pd.read_parquet(path)
        hosp = sites[sites.site_type == "hospital"]
        key = lambda name: f"{r}|{name}"  # same name in another region may be a different place
        todo = [dict(id=key(n), name=n, district=REGIONS[r].name) for n in hosp["name"].unique() if key(n) not in cache]
        if todo:
            cache.update(judge(todo))
            CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
        p = hosp["name"].map(lambda n: cache.get(key(n), {}).get("p_emergency", 1.0))
        sites.loc[p[p < DROP_BELOW].index, "site_type"] = "clinic"
        sites.to_parquet(path, index=False)
        review = sorted(set(hosp["name"][(p >= DROP_BELOW) & (p < 0.5)]))
        print(f"[facilities] {r}: {len(hosp)} OSM hospitals -> {int((p >= DROP_BELOW).sum())} kept, "
              f"{int((p < DROP_BELOW).sum())} relabelled clinic; review band ({len(review)}): {review}")


if __name__ == "__main__":
    main()
