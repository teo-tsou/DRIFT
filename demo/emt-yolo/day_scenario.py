#!/usr/bin/env python3
"""day_scenario.py POLICY OUT_DIR: one compressed day of the vehicle YOLO under a
placement policy, for the paper's day-scenario comparison.

24 hourly slots of SLOT_S seconds each. Each slot sets the scenario clock,
applies the policy, and samples every SAMPLE_S where YOLO runs and its
processed-frame counter (process memory: it survives migrations). Run on the hub.

Edge greenness: ECB's forecast for the 24 hours, fetched once into
OUT_DIR/edge_snapshot.json (reused by the other policies of the same OUT_DIR) and
frozen in the backend, so every policy sees the same day. Vehicle greenness:
VEHICLE=ecb (default) takes the EV's real green share from ECB (getEVGreenness);
VEHICLE=configured uses VEH_GREEN (the 2026-10-22 run).

Policies:
  always-vehicle | always-edge   static placement, never migrates
  greedy         edge whenever its greenness beats the vehicle battery's green
                 share, ignoring the network
  drift          DRIFT's autopilot (backend engine): edge if greener than the
                 vehicle by MARGIN and pQoS OK; on the edge, a predicted
                 degradation triggers QoD + verification, else back to the vehicle

Emulated afternoon congestion (CONGEST hours, all policies): the camera flow
(UDP dport 5005) leaving the vehicle's wwan0 is limited to CONGEST_MBIT; it only
matters while YOLO is on the edge. The pQoS prediction of it is emulated with the
engine's degradation lever (drift only; the others ignore pQoS). Both are
removed at the end, even on failure.
"""
import base64
import csv
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

API = os.environ.get("DRIFT_API", "http://localhost:8000/api/v1/extreme-edge")
SLOT_S, SAMPLE_S = float(os.environ.get("SLOT_S", 60)), 5.0
VEH_GREEN, MARGIN = 0.75, 0.05
VEHICLE = os.environ.get("VEHICLE", "ecb")
CONGEST = {int(h) for h in os.environ.get("CONGEST", "18,19").split(",")}
CONGEST_MBIT = float(os.environ.get("CONGEST_MBIT", 5))
# HOURS=17-20 replays only those hours (e.g. the congested window with another limit)
H0, H1 = (int(x) for x in os.environ.get("HOURS", "0-23").split("-"))

TC_ON = ("tc qdisc del dev wwan0 root 2>/dev/null; tc qdisc add dev wwan0 root handle 1: htb default 10; "
         "tc class add dev wwan0 parent 1: classid 1:10 htb rate 1000mbit; "
         f"tc class add dev wwan0 parent 1: classid 1:20 htb rate {CONGEST_MBIT}mbit ceil {CONGEST_MBIT}mbit burst 15k; "
         "tc filter add dev wwan0 parent 1: protocol ip prio 1 u32 match ip protocol 17 0xff "
         "match ip dport 5005 0xffff flowid 1:20")
TC_OFF = "tc qdisc del dev wwan0 root 2>/dev/null; true"


def api(path, body=None, timeout=30):
    req = urllib.request.Request(f"{API}/{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def edge_snapshot(out):
    """ECB's forecast greenness of the ARM edge for each hour of today (GET only,
    with the workload's energy), saved once per OUT_DIR."""
    path = f"{out}/edge_snapshot.json"
    if os.path.exists(path):
        return json.load(open(path))["greenness"]
    env = {}
    for line in open("/root/DRIFT/.env"):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.rstrip("\n").split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
    auth = "Basic " + base64.b64encode(f"{env['ECB_API_USER']}:{env['ECB_API_PASSWORD']}".encode()).decode()
    g, raw = [], []
    for h in range(24):
        q = urllib.parse.urlencode({"start_HH": h, "end_HH": h + 1, "workload_energy_kwh": 0.0137,
                                    "edge_location": "agx-orin", "model": "yolo26s"})
        req = urllib.request.Request(f"{env['ECB_API_URL']}/getGreenness?{q}", headers={"Authorization": auth})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        raw.append(d); g.append(round(float(d["greenness_percentage"]) / 100.0, 4))
    json.dump({"fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "date": raw[0].get("date"),
               "greenness": g, "responses": raw}, open(path, "w"), indent=1)
    return g


def nano(cmd):
    """Run a command as root on the vehicle, over its management tunnel."""
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=8", "-o", "LogLevel=ERROR", "ecb@10.250.0.2", f"sudo -n sh -c '{cmd}'"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode not in (0, 2):
        print(f"    vehicle command failed ({r.returncode}): {r.stderr.strip()[:120]}", flush=True)


def state():
    for _ in range(3):
        try:
            return api("state")
        except Exception:
            time.sleep(2)
    return {}


def move(to):
    try:
        api("move", {"to": to})
    except Exception as e:
        print(f"    move to {to} refused: {e}", flush=True)


def settle(to, limit=180):
    """Bring YOLO to `to` before the day starts."""
    t0 = time.time()
    while time.time() - t0 < limit:
        s = state()
        if s.get("serving") == to and not s.get("in_flight"):
            return
        if not s.get("in_flight"):
            move(to)
        time.sleep(10)
    raise SystemExit(f"could not bring YOLO to the {to}")


def main():
    policy, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    api("engine", {"enabled": False, "degrade": False})
    snap = edge_snapshot(out)
    print("edge greenness by hour:", " ".join(f"{h}:{g:.2f}" for h, g in enumerate(snap)), flush=True)
    scn = {"edge_source": "snapshot", "edge_snapshot": snap, "hour": H0, "vehicle_source": VEHICLE}
    if VEHICLE == "configured":
        scn["battery_green_pct"] = VEH_GREEN * 100
    api("scenario", scn)
    nano(TC_OFF)
    settle("edge" if policy == "always-edge" else "vehicle")
    if policy == "drift":
        api("engine", {"green_min": 0.2, "margin": MARGIN, "verify_s": 10, "cooldown_s": 20, "enabled": True})
    rows = []
    try:
        for hour in range(H0, H1 + 1):
            api("scenario", {"hour": hour})
            congested = hour in CONGEST
            nano(TC_ON if congested else TC_OFF)
            if policy == "drift":
                api("engine", {"degrade": congested})
            s = state()
            g = ((s.get("energy") or {}).get("edge") or {}).get("greenness")
            vg = ((s.get("energy") or {}).get("vehicle") or {}).get("greenness")
            if policy == "greedy" and not s.get("in_flight"):
                want = "edge" if (g or 0) > (vg if vg is not None else VEH_GREEN) else "vehicle"
                if s.get("serving") != want:
                    move(want)
            print(f"{policy} {hour:02d}:00 edge green {g} vehicle green {vg} congested {congested} on {s.get('serving')}", flush=True)
            t_end = time.time() + SLOT_S
            while time.time() < t_end:
                s = state()
                h = s.get("health") or {}
                rows.append({"t": round(time.time(), 2), "hour": hour, "edge_green": g, "veh_green": vg,
                             "congested": int(congested),
                             "serving": "migrating" if s.get("in_flight") else s.get("serving"),
                             "processed": h.get("processed"), "incomplete": h.get("incomplete"), "fps": h.get("fps")})
                time.sleep(max(0.0, SAMPLE_S - 1.0))
    finally:
        nano(TC_OFF)
        api("engine", {"enabled": False, "degrade": False})
        with open(f"{out}/{policy}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["t"])
            w.writeheader(); w.writerows(rows)
        if policy == "drift":
            with open(f"{out}/drift-engine-log.json", "w") as f:
                json.dump(api("engine")["log"], f, indent=1)
        mig = [m for m in state().get("migrations", []) if rows and (m.get("started_at") or 0) >= rows[0]["t"]]
        with open(f"{out}/{policy}-migrations.json", "w") as f:
            json.dump(mig, f, indent=1)
        print(f"{policy}: {len(rows)} samples, {len(mig)} migrations", flush=True)


if __name__ == "__main__":
    main()
