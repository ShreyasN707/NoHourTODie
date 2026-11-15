"""GoldenHour live operations: one incident record shared by the command centre and the crew dashboard.

Flow:  call in  ->  auto-plan (assistant.plan_dispatch: unit, bike, route, hospital, alerts)
       ->  command centre confirms  ->  crew dashboard sees the job  ->  crew taps through the stages
       ->  unit is free again. Every stage gets a timestamp, so the command centre sees where the minutes go.
When a unit leaves its standby spot, the command centre also gets the coverage gap it leaves and a
suggested cover move.

Both screens poll GET /ops/state (about once a second). No database: state lives in memory for the demo.

Run:  .venv/bin/python backend/ops.py     (port 8788; loads the same road graph as the assistant)
"""
import json, random, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assistant as A  # noqa: E402  (loads graph, plans and places; no Gemini call is made here)

import h3  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.sparse.csgraph import dijkstra  # noqa: E402

PORT = 8788
MIN_GAIN = 0.05  # calls/hour (about one call a day) below which a cover move is not worth a crew's drive
# Order of stages after the command centre confirms. The crew moves one step at a time.
STAGES = ["planned", "alerted", "accepted", "en_route", "on_scene", "transporting", "at_hospital", "closed"]
INCIDENTS: dict = {}
STATE = dict(version=0)
LOCK = threading.Lock()
CALLS = {r: pd.read_parquet(A.PROCESSED_DIR / f"calls_{r}.parquet", columns=["h3_index", "zone", "call_type", "hour"])
         for r in A.REG}
CELLS = {r: pd.read_parquet(A.PROCESSED_DIR / f"cells_{r}.parquet").merge(
    pd.read_parquet(A.PROCESSED_DIR / f"lam_{r}.parquet"), on="h3_index", how="left").fillna(0) for r in A.REG}


def bump():
    STATE["version"] += 1


def stamp():
    return time.time()


# ---------------------------------------------------------------- calls
def simulate_call(region: str = "blr") -> dict:
    """Draw one synthetic call for this hour from the modelled call log, at a random point in its H3 cell."""
    hour = A.now_hour()
    pool = CALLS[region]
    pool = pool[pool.hour == hour] if (pool.hour == hour).any() else pool
    c = pool.sample(1, random_state=random.randrange(1 << 30)).iloc[0]
    lat, lon = h3.cell_to_latlng(c.h3_index)
    edge = h3.average_hexagon_edge_length(h3.get_resolution(c.h3_index), unit="km") / 111
    lat, lon = lat + random.uniform(-.5, .5) * edge, lon + random.uniform(-.5, .5) * edge
    return dict(lat=lat, lon=lon, label=f"Caller in {c.zone}", call_type=c.call_type)


def new_incident(lat: float, lon: float, label: str, call_type: str, source: str) -> dict:
    t = stamp()
    draft = A.plan_dispatch(lat, lon, label, call_type)
    if "error" in draft:
        return draft
    inc = dict(id=draft["dispatch_id"], source=source, status="planned", unit_id=draft["ambulance"]["id"],
               times=dict(call=t, planned=stamp()), draft=draft, destination=None, cover=None, log=[])
    with LOCK:
        INCIDENTS[inc["id"]] = inc
        bump()
    return inc


# ---------------------------------------------------------------- coverage gap and cover move
def coverage_gap(inc: dict) -> dict:
    """After a unit leaves its standby site S: which calls near S now have no free ambulance within the target,
    and which free ambulance should move to S to fill the gap.
    ponytail: greedy one-move suggestion (nearest site that still keeps a unit); a full re-solve of the
    MEXCLP block with the busy units removed is the upgrade path."""
    d = inc["draft"]
    r, hour = d["region"], d["hour"]
    f = A.factor(r, hour) / A.AMBULANCE_SPEED_MULTIPLIER
    limit_s = (d["target_min"] - A.PRE_TRAVEL_MIN) * 60 / f
    site_id = inc["unit_id"].split(":")[0]
    sites = A.REG[r]["sites"]
    s_node = int(sites.loc[site_id, "node_idx"])
    free = {}  # site_id -> free ambulances left there
    for dep in A.REG[r]["sc"]["goldenhour"]["plans"][A.block_of(hour)]["deployments"]:
        for kind, n in (("Advanced (ALS)", dep["a"]), ("Basic (BLS)", dep["b"])):
            k = sum(f"{dep['site_id']}:{kind}:{i}" not in A.BUSY for i in range(n))
            if k:
                free[dep["site_id"]] = free.get(dep["site_id"], 0) + k
    cells = CELLS[r]
    lam = cells[f"block_{A.block_of(hour)}"].values
    from_s = dijkstra(A.G, indices=s_node, limit=limit_s * 1.01)[cells.node_idx.values]
    nodes = [int(sites.loc[s, "node_idx"]) for s in free]
    from_free = (dijkstra(A.G, indices=nodes, min_only=True, limit=limit_s * 1.01)[cells.node_idx.values]
                 if nodes else np.full(len(cells), np.inf))
    lost = (from_s <= limit_s) & (from_free > limit_s)
    out = dict(site=str(sites.loc[site_id, "name"] or sites.loc[site_id, "site_type"]),
               cells_lost=int(lost.sum()), calls_per_hour_lost=round(float(lam[lost].sum()), 2),
               zones=sorted(set(cells.zone[lost]))[:6], move=None, approved=False)
    if not lost.any():
        return out
    # Candidates: the 8 free units nearest to S. Moving one to S restores the whole gap but may open a gap
    # where it was; pick the smallest such loss and suggest the move only if it is a net gain.
    to_s = dijkstra(A.GT, indices=s_node, limit=3 * 3600)
    node_of = {s: int(sites.loc[s, "node_idx"]) for s in free if s != site_id}
    gain, best = float(lam[lost].sum()), None
    for s in sorted(node_of, key=lambda s: to_s[node_of[s]])[:8]:
        others = [n for t, n in node_of.items() if t != s or free[t] > 1] + [s_node]  # S is covered after the move
        d_s = dijkstra(A.G, indices=node_of[s], limit=limit_s * 1.01)[cells.node_idx.values]
        d_o = dijkstra(A.G, indices=others, min_only=True, limit=limit_s * 1.01)[cells.node_idx.values]
        loss = float(lam[(d_s <= limit_s) & (d_o > limit_s)].sum())
        if gain - loss >= MIN_GAIN and (best is None or loss < best[1]):
            best = (s, loss)
    if best:
        s = best[0]
        out["move"] = dict(from_site=str(sites.loc[s, "name"] or sites.loc[s, "site_type"]), to_site=out["site"],
                           drive_min=round(float(to_s[node_of[s]]) * f / 60, 1),
                           net_calls_per_hour=round(gain - best[1], 2))
    return out


# ---------------------------------------------------------------- transitions
def confirm(iid: str) -> dict:
    with LOCK:
        inc = INCIDENTS.get(iid)
        if not inc or inc["status"] != "planned":
            return dict(error="Not waiting for confirmation.")
        res = A.confirm(iid)  # marks the units busy and logs the alerts (shared assistant bookkeeping)
        if "error" in res:
            return res
        inc["status"], inc["times"]["alerted"], inc["alerts"] = "alerted", stamp(), res["alerts"]
    gap = coverage_gap(inc)  # outside the lock: Dijkstra takes a moment
    with LOCK:
        inc["cover"] = gap
        bump()
    return inc


def hospital_route(d: dict, name: str, lat: float, lon: float) -> dict:
    """Road route from the patient to the hospital the crew chose (same graph and traffic as dispatch)."""
    p, h = A.snap(d["patient"]["lat"], d["patient"]["lon"]), A.snap(lat, lon)
    dist, pred = dijkstra(A.G, indices=p, limit=3 * 3600, return_predecessors=True)
    eta = float(dist[h]) * A.factor(d["region"], d["hour"]) / A.AMBULANCE_SPEED_MULTIPLIER / 60
    return dict(name=name, lat=lat, lon=lon, eta_min=round(eta, 1), path=A.path_from_tree(pred, h, p)[::-1])


def reject(iid: str) -> dict:
    """Crew can't take the job: hand it straight to the next-best free unit (the operator already confirmed the call)."""
    with LOCK:
        inc = INCIDENTS.get(iid)
        if not inc or inc["status"] != "alerted":
            return dict(error="Only a new job can be rejected.")
        old = inc["draft"]
        p = old["patient"]
        new = A.plan_dispatch(p["lat"], p["lon"], p["label"], old["call_type"], old["hour"])  # old unit is still busy
        A.BUSY.discard(old["ambulance"]["id"])
        if old["bike"]:
            A.BUSY.discard(old["bike"]["id"])
        if "error" in new:  # nobody else free: the same unit stays on the job
            A.BUSY.add(old["ambulance"]["id"])
            inc["log"].append(dict(at=stamp(), text=f"{old['ambulance']['kind']} at {old['ambulance']['from_site']} declined; no other unit free."))
            bump()
            return inc
        A.confirm(new["dispatch_id"])
        inc["log"].append(dict(at=stamp(), text=f"{old['ambulance']['kind']} at {old['ambulance']['from_site']} declined; "
                                                 f"sent to {new['ambulance']['kind']} at {new['ambulance']['from_site']}."))
        inc["draft"], inc["unit_id"] = new, new["ambulance"]["id"]
        inc["times"]["alerted"] = stamp()
        bump()
    gap = coverage_gap(inc)
    with LOCK:
        inc["cover"] = gap
        bump()
    return inc


def advance(iid: str, status: str, destination=None) -> dict:
    if status == "rejected":
        return reject(iid)
    route = None
    if isinstance(destination, dict) and status == "transporting":  # outside the lock: Dijkstra
        inc = INCIDENTS.get(iid)
        if inc:
            route = hospital_route(inc["draft"], str(destination["name"]), float(destination["lat"]), float(destination["lon"]))
    with LOCK:
        inc = INCIDENTS.get(iid)
        if not inc:
            return dict(error="Unknown incident.")
        cur = STAGES.index(inc["status"])
        if status not in STAGES or STAGES.index(status) != cur + 1 or inc["status"] == "planned":
            return dict(error=f"Can't go from {inc['status']} to {status}.")
        inc["status"] = status
        inc["times"][status] = stamp()
        if route:
            inc["draft"]["hospital"], inc["destination"] = route, route["name"]
        elif destination:
            inc["destination"] = str(destination)
        if status == "closed":  # unit free again
            d = inc["draft"]
            A.BUSY.discard(d["ambulance"]["id"])
            if d["bike"]:
                A.BUSY.discard(d["bike"]["id"])
        bump()
        return inc


def approve_move(iid: str) -> dict:
    with LOCK:
        inc = INCIDENTS.get(iid)
        if not inc or not (inc.get("cover") or {}).get("move"):
            return dict(error="No cover move to approve.")
        inc["cover"]["approved"] = True  # demo: logged only; the standby plan itself is not rewritten
        bump()
        return inc


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, default=float).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/ops/state"):
            with LOCK:
                incs = sorted(INCIDENTS.values(), key=lambda i: -i["times"]["call"])
                return self._json(200, dict(version=STATE["version"], now=stamp(), incidents=incs))
        if self.path == "/ops/health":
            return self._json(200, dict(ok=True))
        self._json(404, dict(error="not found"))

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            p = self.path.rstrip("/").split("/")  # ['', 'ops', ...]
            if p[2:] == ["calls"]:
                if "lat" in body:
                    c = dict(lat=float(body["lat"]), lon=float(body["lon"]), label=str(body.get("label", "Caller location")),
                             call_type=str(body.get("call_type", "other")))
                elif body.get("place"):
                    hits = A.find_place(str(body["place"]))
                    if not hits:
                        return self._json(200, dict(error=f"Couldn't find {body['place']}."))
                    h = hits[0]
                    c = dict(lat=h["lat"], lon=h["lon"], label=h["label"],
                             call_type=str(body.get("call_type", "other")))
                else:
                    c = simulate_call(body.get("region", "blr"))
                return self._json(200, new_incident(c["lat"], c["lon"], c["label"], c["call_type"],
                                                    "simulated" if "lat" not in body and not body.get("place") else "operator"))
            if len(p) == 5 and p[2] == "incidents":
                iid, action = p[3], p[4]
                if action == "confirm":
                    return self._json(200, confirm(iid))
                if action == "status":
                    return self._json(200, advance(iid, str(body.get("status")), body.get("destination")))
                if action == "approve_move":
                    return self._json(200, approve_move(iid))
            if p[2:] == ["reset"]:
                with LOCK:
                    for inc in INCIDENTS.values():
                        A.BUSY.discard(inc["draft"]["ambulance"]["id"])
                        if inc["draft"]["bike"]:
                            A.BUSY.discard(inc["draft"]["bike"]["id"])
                    INCIDENTS.clear()
                    bump()
                return self._json(200, dict(ok=True))
            self._json(404, dict(error="not found"))
        except Exception as e:
            self._json(500, dict(error=f"{type(e).__name__}: {e}"))

    def log_message(self, *a):
        pass


def selfcheck():
    """Call -> plan -> confirm -> every crew stage -> unit free again; illegal jumps refused."""
    inc = new_incident(**{k: v for k, v in simulate_call("blr").items()}, source="test")
    assert inc["status"] == "planned", inc
    assert "error" in advance(inc["id"], "accepted")  # crew can't act before the command centre confirms
    confirm(inc["id"])
    assert inc["draft"]["ambulance"]["id"] in A.BUSY and inc["cover"] is not None
    assert "error" in advance(inc["id"], "on_scene")  # no skipping stages
    first = inc["unit_id"]
    advance(inc["id"], "rejected")  # crew declines -> next unit gets it, first unit free again
    assert inc["status"] == "alerted" and (inc["unit_id"] != first) and first not in A.BUSY, inc["log"]
    for s in STAGES[2:]:
        dest = dict(name="Test Hospital", lat=12.9767, lon=77.5713) if s == "transporting" else None
        assert advance(inc["id"], s, dest)["status"] == s
    assert inc["draft"]["hospital"]["name"] == "Test Hospital" and len(inc["draft"]["hospital"]["path"]) > 1
    assert inc["draft"]["ambulance"]["id"] not in A.BUSY
    INCIDENTS.clear()
    print("[ops] selfcheck ok; cover:", inc["cover"])


if __name__ == "__main__":
    if "--check" in sys.argv:
        selfcheck()
        sys.exit()
    print(f"[ops] listening on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
