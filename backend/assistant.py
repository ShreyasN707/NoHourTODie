"""GoldenHour dispatch assistant: one Gemini call per message.

Gemini only decides WHAT the user wants (dispatch to a place / a stats question / plain answer) and fills
in the arguments. Everything else is plain, testable code, and the reply after a tool is a template, so a
request never needs a second model call:
  dispatch        -> offline place search (OpenStreetMap index), nearest suitable ambulance (+ bike) from the
                     current GoldenHour plan, fastest road route at this hour's traffic, nearest hospital,
                     DRAFT alerts. Alerts are only "sent" (logged) when the dispatcher presses Confirm.
  region_summary / ward_stats -> numbers straight from the simulation results
Token budget per call: short system prompt + 3 compact tools + the last few text turns; minimal thinking;
output capped. Tool results are never sent back to the model.

Run:  .venv/bin/python backend/assistant.py      (needs GEMINI_API_KEY in .env and `make all` outputs)
"""
import json, os, sys, threading, uuid, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import h3
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
from google import genai
from google.genai import types

import places
from pipeline.config import (ALS_EXTRA_MIN, AMBULANCE_SPEED_MULTIPLIER, BENGALURU_HOURLY_CONGESTION, CALL_MIX,
                             PLANNING_BLOCKS, PRE_TRAVEL_MIN, PROCESSED_DIR, REGIONS, WEB_DATA_DIR)

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")  # cheapest current Gemini this key can use
PORT = int(os.environ.get("ASSISTANT_PORT", "8787"))
IST = timezone(timedelta(hours=5, minutes=30))
LIMIT_S = 3 * 3600


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# ---------------------------------------------------------------- data (loaded once)
print("[assistant] loading road graph and plans...")
NODES = pd.read_parquet(PROCESSED_DIR / "graph_nodes.parquet").sort_values("node_idx")
EDGES = pd.read_parquet(PROCESSED_DIR / "graph_edges.parquet")
N = len(NODES)
G = csr_matrix((EDGES.time_s.values, (EDGES.u.values, EDGES.v.values)), shape=(N, N))
GT = G.T.tocsr()
LAT, LON = NODES.lat.values, NODES.lon.values
TREE = cKDTree(np.c_[LAT, LON * np.cos(np.radians(13))])
REG = {}
for r, cfg in REGIONS.items():
    cells = pd.read_parquet(PROCESSED_DIR / f"cells_{r}.parquet")
    sites = pd.read_parquet(PROCESSED_DIR / f"sites_{r}.parquet").set_index("site_id", drop=False)
    sc = json.load(open(WEB_DATA_DIR / f"scenarios_{r}.json"))
    REG[r] = dict(cfg=cfg, cells=set(cells.h3_index), sites=sites, sc=sc,
                  hospitals=sites[sites.site_type == "hospital"])
if not places.OUT.exists():
    places.build()
PLACES = pd.read_parquet(places.OUT)
if "emergency" not in PLACES:  # index from an older build
    places.build(); PLACES = pd.read_parquet(places.OUT)
ER_NAMES = set(PLACES[(PLACES.kind == "hospital") & (PLACES.emergency == "yes")].key)
_H = None


def all_hospitals() -> pd.DataFrame:
    """Every mapped hospital (not only the thinned standby sites), snapped to the road graph once."""
    global _H
    if _H is None:
        h = PLACES[PLACES.kind == "hospital"].copy()
        h["node_idx"] = TREE.query(np.c_[h.lat, h.lon * np.cos(np.radians(13))])[1]
        h["region"] = [region_of(a, b) for a, b in zip(h.lat, h.lon)]
        _H = h
    return _H
NOT_ER = r"eye|dental|dent |fertility|ivf|piles|skin|derma|ayurved|unani|homeo|physio|diagnostic|lab|clinic|nursing home|vet|psychiat|rehab"
MAJOR = r"medical college|general hospital|district hospital|government|govt|victoria|bowring|ramaiah|manipal|narayana|st john|nimhans|trauma"


def destination_hospitals(r: str, call_type: str) -> pd.DataFrame:
    """Where to take the patient: hospitals with an emergency department (OSM tag) or major/government
    hospitals; never eye/dental/fertility-type clinics; maternity only for pregnancy calls."""
    h = all_hospitals()
    h = h[h.region == r]
    name = h.name.fillna("").str.lower()
    bad = name.str.contains(NOT_ER, regex=True) | (name.str.contains("maternity|women", regex=True) & (call_type != "pregnancy"))
    good = name.map(places.norm).isin(ER_NAMES) | name.str.contains(MAJOR, regex=True)
    if call_type == "pregnancy":
        good |= name.str.contains("maternity|women|mother|matru", regex=True)
    pick = h[good & ~bad]
    return pick if len(pick) else h[~bad] if (~bad).any() else h
BUSY: set = set()          # vehicle ids already sent in this demo session
PENDING: dict = {}         # dispatch_id -> draft
SENT: list = []            # confirmed alerts
LOCK = threading.Lock()
print(f"[assistant] ready: {N:,} road nodes, model {MODEL}")


def now_hour():
    return datetime.now(IST).hour


def block_of(hour):
    return next(b["id"] for b in PLANNING_BLOCKS if hour in b["hours"])


def region_of(lat, lon):
    for r, res in (("blr", 8), ("tmk", 7)):
        if h3.latlng_to_cell(lat, lon, res) in REG[r]["cells"]:
            return r
    return None


def snap(lat, lon):
    return int(TREE.query([lat, lon * np.cos(np.radians(13))])[1])


def factor(r, hour):
    return BENGALURU_HOURLY_CONGESTION[hour] if REG[r]["cfg"].congestion_enabled else 1.10


def path_from_tree(pred, start, end):
    """Follow a Dijkstra predecessor tree from start until end; returns [[lon, lat], ...]."""
    out, n = [], start
    for _ in range(200_000):
        out.append([round(float(LON[n]), 5), round(float(LAT[n]), 5)])
        if n == end or pred[n] < 0:
            break
        n = pred[n]
    return out


# ---------------------------------------------------------------- tools
def find_place(query: str) -> list:
    """Offline OpenStreetMap index first; Nominatim only if nothing matches."""
    hits = places.search(PLACES, query)
    if hits:
        return hits
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(dict(
        q=query, format="jsonv2", limit=3, countrycodes="in", viewbox="76.2,14.45,77.95,12.7", bounded=1))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GoldenHour-hackathon-demo/0.1"})
        res = json.load(urllib.request.urlopen(req, timeout=8))
    except Exception:
        return []
    return [dict(label=x["display_name"][:120], lat=float(x["lat"]), lon=float(x["lon"]),
                 precision="approximate", source="OpenStreetMap Nominatim") for x in res]


def dispatch(place: str, call_type: str = "other", hour: int | None = None) -> dict:
    cands = find_place(place)
    if not cands:
        return dict(reply=f"I couldn't find \"{place}\" in Bengaluru or Tumakuru. Could you add a landmark or area name?")
    c = cands[0]
    d = plan_dispatch(c["lat"], c["lon"], c["label"], call_type, hour)
    if "error" in d:
        return dict(reply=d["error"])
    a, h = d["ambulance"], d["hospital"]
    ok = "within" if a["within_target"] else "beyond"
    where = c["label"] if c["label"].startswith("near ") else f"at {c['label']}"
    txt = (f"Patient {where}. Nearest {a['kind']} ambulance is at {a['from_site']}, about {a['eta_min']} min away "
           f"({ok} the {d['target_min']}-min target at {d['hour']:02d}:00 traffic).")
    if d["bike"]:
        txt += f" A bike first responder from {d['bike']['from_site']} can reach in about {d['bike']['eta_min']} min."
    txt += f" Nearest hospital: {h['name']}, {h['eta_min']} min from the patient. Press Confirm to send the alerts."
    if len(cands) > 1:
        txt += f" (Wrong place? I also found: {cands[1]['label']}.)"
    return dict(reply=txt, dispatch=d)


def plan_dispatch(lat: float, lon: float, place_label: str, call_type: str = "other", hour: int | None = None) -> dict:
    r = region_of(lat, lon)
    if r is None:
        return dict(error="This location is outside the Bengaluru and Tumakuru areas GoldenHour covers.")
    hour = now_hour() if hour is None else int(hour) % 24
    b, f = block_of(hour), factor(r, hour)
    call_type = call_type if call_type in CALL_MIX else "other"
    needs_als, bike_helps = CALL_MIX[call_type]["needs_als"], CALL_MIX[call_type]["bike_helps"]
    patient = snap(lat, lon)
    # one search towards the patient (reverse graph) gives every unit's drive time and route
    to_pat, pred_to = dijkstra(GT, indices=patient, limit=LIMIT_S, return_predecessors=True)
    units = []
    for d in REG[r]["sc"]["goldenhour"]["plans"][b]["deployments"]:
        node = int(REG[r]["sites"].loc[d["site_id"], "node_idx"])
        for kind, n in (("Advanced (ALS)", d["a"]), ("Basic (BLS)", d["b"]), ("Bike", d["y"])):
            for k in range(n):
                vid = f"{d['site_id']}:{kind}:{k}"
                if vid in BUSY or not np.isfinite(to_pat[node]):
                    continue
                sec = float(to_pat[node])
                eta = PRE_TRAVEL_MIN + sec * (f / AMBULANCE_SPEED_MULTIPLIER if kind != "Bike" else 1 + 0.4 * (f - 1)) / 60
                units.append(dict(id=vid, kind=kind, site=d["name"] or d["site_type"], node=node, eta_min=round(eta, 1)))
    amb = sorted([u for u in units if u["kind"] != "Bike"], key=lambda u: u["eta_min"])
    if not amb:
        return dict(error="No free ambulance in the current plan can reach this location.")
    pick = amb[0]
    if needs_als:  # C-DAC rule is nearest free unit; prefer Advanced only if it is at most ALS_EXTRA_MIN later
        als = [u for u in amb if u["kind"].startswith("Advanced")]
        if als and als[0]["eta_min"] <= pick["eta_min"] + ALS_EXTRA_MIN:
            pick = als[0]
    bikes = sorted([u for u in units if u["kind"] == "Bike"], key=lambda u: u["eta_min"])
    bike = bikes[0] if (bike_helps and bikes and bikes[0]["eta_min"] <= 15) else None
    # nearest hospital by road from the patient
    from_pat, pred_from = dijkstra(G, indices=patient, limit=LIMIT_S, return_predecessors=True)
    hosp = destination_hospitals(r, call_type)
    hn = hosp.node_idx.values.astype(int)
    hi = int(np.argmin(from_pat[hn]))
    h_node, h_row = hn[hi], hosp.iloc[hi]
    h_eta = round(float(from_pat[h_node]) * f / AMBULANCE_SPEED_MULTIPLIER / 60, 1)
    h_path = path_from_tree(pred_from, h_node, patient)[::-1]
    target = REG[r]["cfg"].target_min
    did = uuid.uuid4().hex[:8]
    draft = dict(
        dispatch_id=did, region=r, hour=hour, call_type=call_type, target_min=target,
        patient=dict(label=place_label, lat=lat, lon=lon),
        ambulance=dict(id=pick["id"], kind=pick["kind"], from_site=pick["site"], eta_min=pick["eta_min"],
                       within_target=pick["eta_min"] <= target, path=path_from_tree(pred_to, pick["node"], patient)),
        bike=None if not bike else dict(id=bike["id"], from_site=bike["site"], eta_min=bike["eta_min"],
                                        path=path_from_tree(pred_to, bike["node"], patient)),
        hospital=dict(name=str(h_row["name"]) or "Hospital", lat=float(h_row.lat), lon=float(h_row.lon), eta_min=h_eta, path=h_path),
        alerts=[
            dict(to=f"{pick['kind']} ambulance crew at {pick['site']}",
                 text=f"{call_type.title()} call at {place_label}. Go now. ETA {pick['eta_min']} min. Then to {h_row['name'] or 'nearest hospital'}."),
            dict(to=f"Emergency department, {h_row['name'] or 'nearest hospital'}",
                 text=f"Incoming {call_type} patient from {place_label}. Expected in about {round(pick['eta_min'] - PRE_TRAVEL_MIN + 15 + h_eta)} min."),
            dict(to="Bengaluru Traffic Police control room" if r == "blr" else "Tumakuru district police control room",
                 text=f"Ambulance corridor requested: {pick['site']} to {place_label}, then to {h_row['name'] or 'hospital'}."),
        ] + ([dict(to=f"Bike first responder at {bike['site']}",
                    text=f"First help needed at {place_label}. ETA {bike['eta_min']} min.")] if bike else []),
        note="Demo assumption: every unit in the current GoldenHour plan is free unless already sent in this session. "
             "In production, live unit status and the final dispatch come from the C-DAC command centre.",
    )
    with LOCK:
        PENDING[did] = draft
    return draft


def region_summary(region: str) -> dict:
    r = "tmk" if "tum" in region.lower() or region.lower() == "tmk" else "blr"
    sc = REG[r]["sc"]
    keys = ("within_target_pct", "median_min", "p90_min", "first_help_10m_pct")
    t, g = sc["baseline"]["sim"], sc["goldenhour"]["sim"]
    best = sc["fleet_curve"][-1]
    return dict(reply=(f"{REG[r]['cfg'].name}, {sc['target_min']:.0f}-min target (simulated, 28 days of synthetic calls): "
                       f"today {t['within_target_pct']}% in time, GoldenHour {g['within_target_pct']}%. "
                       f"First help within 10 min: {t['first_help_10m_pct']}% today vs {g['first_help_10m_pct']}%. "
                       f"Slowest 1 in 10 wait: {t['p90_min']} vs {g['p90_min']} min. "
                       f"With {best['ambulances']} ambulances it would be {best['within_target_pct']}% in time."))


def ward_stats(name: str) -> dict:
    q = name.lower().strip()
    for r in REG:
        zones = REG[r]["sc"]["goldenhour"]["sim"]["per_zone"]
        m = [z for z in zones if q in z.lower()]
        if m:
            z = m[0]
            g, t = zones[z], REG[r]["sc"]["baseline"]["sim"]["per_zone"].get(z, {})
            return dict(reply=(f"{z} ({REG[r]['cfg'].name}, simulated): {g['within_target_pct']}% of calls reached in time "
                               f"with GoldenHour vs {t.get('within_target_pct', '?')}% today; median {g['median_min']} min, "
                               f"slowest 1 in 10 {g['p90_min']} min ({g['calls']} calls in 28 days)."))
    return dict(reply=f"I couldn't find a ward or taluk called \"{name}\".")


TOOLS = {f.__name__: f for f in (dispatch, region_summary, ward_stats)}
DECLS = [
    types.FunctionDeclaration(
        name="dispatch",
        description="Send help to a patient or show the route to a place in Bengaluru/Tumakuru. Plans the unit, route, hospital and draft alerts.",
        parameters_json_schema={"type": "object", "properties": {
            "place": {"type": "string", "description": "Place, landmark or address. Expand abbreviations (DSCE -> Dayananda Sagar College)."},
            "call_type": {"type": "string", "enum": list(CALL_MIX)},
            "hour": {"type": "integer", "description": "0-23, only if the user gives a time"}},
            "required": ["place"]}),
    types.FunctionDeclaration(
        name="region_summary", description="Response-time results for Bengaluru or Tumakuru.",
        parameters_json_schema={"type": "object", "properties": {"region": {"type": "string"}}, "required": ["region"]}),
    types.FunctionDeclaration(
        name="ward_stats", description="Response-time results for one ward or taluk.",
        parameters_json_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
]
SYSTEM = ("You are the GoldenHour assistant in Karnataka's 108 ambulance control room (Bengaluru, Tumakuru). "
          "Patient location or route request -> call dispatch (guess call_type: pregnancy, trauma/accident, cardiac/chest pain, "
          "stroke, respiratory/breathing, else other). Performance questions -> region_summary or ward_stats. "
          "Otherwise answer in 1-3 short sentences. Never invent numbers. Reply in the user's language.")


# ---------------------------------------------------------------- Gemini: one call per message
load_env()
CLIENT = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM, tools=[types.Tool(function_declarations=DECLS)],
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
    max_output_tokens=256)
HISTORY_TURNS = 6          # only the last few text turns are resent
SESSIONS: dict = {}
USAGE = dict(calls=0, input_tokens=0, output_tokens=0)


def chat(session: str, message: str) -> dict:
    hist = SESSIONS.setdefault(session, [])
    contents = hist[-HISTORY_TURNS:] + [types.Content(role="user", parts=[types.Part(text=message)])]
    resp = CLIENT.models.generate_content(model=MODEL, contents=contents, config=CONFIG)
    u = resp.usage_metadata
    with LOCK:
        USAGE["calls"] += 1
        USAGE["input_tokens"] += (u.prompt_token_count or 0) if u else 0
        USAGE["output_tokens"] += ((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else 0
    calls = resp.function_calls or []
    if calls:
        c = calls[0]
        try:
            out = TOOLS[c.name](**dict(c.args or {}))
        except Exception as e:
            out = dict(reply=f"Sorry, that failed ({type(e).__name__}). Please try again.")
    else:
        out = dict(reply=(resp.text or "").strip() or "Sorry, I didn't catch that.")
    # keep plain text only: tool calls and results never go back to the model
    hist += [types.Content(role="user", parts=[types.Part(text=message)]),
             types.Content(role="model", parts=[types.Part(text=out["reply"])])]
    del hist[:-HISTORY_TURNS]
    return dict(reply=out["reply"], dispatch=out.get("dispatch"))


def confirm(dispatch_id: str) -> dict:
    with LOCK:
        d = PENDING.pop(dispatch_id, None)
        if d is None:
            return dict(error="This dispatch was already confirmed or has expired.")
        BUSY.add(d["ambulance"]["id"])
        if d["bike"]:
            BUSY.add(d["bike"]["id"])
        at = datetime.now(IST).strftime("%H:%M:%S")
        sent = [dict(a, at=at, status="sent (simulated)") for a in d["alerts"]]
        SENT.extend(sent)
    return dict(alerts=sent)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/health":
            return self._json(200, dict(ok=True, model=MODEL))
        if self.path == "/api/alerts":
            return self._json(200, dict(alerts=SENT))
        if self.path == "/api/usage":
            return self._json(200, USAGE)
        self._json(404, dict(error="not found"))

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/chat":
                msg = str(body.get("message", ""))[:2000]
                return self._json(200, chat(str(body.get("session", "default"))[:64], msg))
            if self.path == "/api/confirm":
                return self._json(200, confirm(str(body.get("dispatch_id", ""))))
            if self.path == "/api/reset":
                with LOCK:
                    BUSY.clear(); PENDING.clear(); SENT.clear(); SESSIONS.clear()
                return self._json(200, dict(ok=True))
            self._json(404, dict(error="not found"))
        except Exception as e:
            self._json(500, dict(error=f"{type(e).__name__}: {e}"))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"[assistant] listening on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
