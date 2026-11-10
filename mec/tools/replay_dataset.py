#!/usr/bin/env python3
"""
Replay a UE mobility trace (the ENVELOPE pQoS dataset) into DRIFT and watch the
edge-selection decision follow the UE.

What it does, step by step (simplest version — location-driven):
  1. Places the two edge SITES at the two ends of the trace's GPS path, so a
     crossover exists (edge A near the start, edge B near the end).
  2. For each row in the trace: pushes the UE's GPS (and pQoS) into the mocks,
     then asks the DRIFT controller /candidates and prints the decision.
  As the UE moves from A's end toward B's end, the nearest edge flips and the
  decision goes  stay → cross_cluster.  With --migrate it fires the real
  migration the first time it flips.

Usage:
  python3 replay_dataset.py --source-cluster <A id> --target-cluster <B id> \
      [--csv sample_trace.csv] [--scenario 13kph_road_1] [--vm migrate-test-vm] \
      [--step 1.0] [--migrate]
"""
import argparse
import csv
import math
import time
import sys
import urllib.request
import json

BACKEND = "http://localhost:8000/api/v1"
CONTROLLER = "http://localhost:8011"
LOCATION_MOCK = "http://localhost:9003"
PQOS_MOCK = "http://localhost:9002"


def _req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read() or "{}")


def haversine_km(la1, lo1, la2, lo2):
    r = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(a))


def site_id_of(cluster_id):
    c = _req("GET", f"{BACKEND}/edge-clusters/{cluster_id}")
    return c["site_id"]


def set_site(site_id, lat, lon):
    s = _req("GET", f"{BACKEND}/sites/{site_id}")
    s["latitude"], s["longitude"] = lat, lon
    _req("PUT", f"{BACKEND}/sites/{site_id}",
         {"name": s["name"], "location": s.get("location"),
          "latitude": lat, "longitude": lon, "meta_data": s.get("meta_data", {})})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="sample_trace.csv")
    ap.add_argument("--scenario", default=None, help="filter by meas_run")
    ap.add_argument("--vm", default="migrate-test-vm")
    ap.add_argument("--kind", default="vm", choices=["vm", "pod"])
    ap.add_argument("--namespace", default="default")
    ap.add_argument("--source-cluster", required=True)
    ap.add_argument("--target-cluster", required=True)
    ap.add_argument("--step", type=float, default=1.0, help="seconds between rows")
    ap.add_argument("--migrate", action="store_true", help="fire real migration on flip")
    args = ap.parse_args()

    rows = []
    with open(args.csv) as f:
        for row in csv.DictReader(f):
            if args.scenario and row.get("meas_run") != args.scenario:
                continue
            try:
                rows.append((float(row["gpsd_lat"]), float(row["gpsd_long"]),
                             float(row.get("ue_throughput_dl") or 0),
                             float(row.get("ott_ping_rtt_seconds") or 0)))
            except (ValueError, KeyError):
                continue
    if len(rows) < 2:
        sys.exit("need >=2 rows in the trace")

    # 1) place edge A at the first GPS point, edge B at the last
    site_a, site_b = site_id_of(args.source_cluster), site_id_of(args.target_cluster)
    set_site(site_a, rows[0][0], rows[0][1])
    set_site(site_b, rows[-1][0], rows[-1][1])
    print(f"edge A(site {site_a[:8]}) @ start {rows[0][0]:.5f},{rows[0][1]:.5f}")
    print(f"edge B(site {site_b[:8]}) @ end   {rows[-1][0]:.5f},{rows[-1][1]:.5f}\n")

    hdr = f"{'t':>3} {'UE lat,lon':>20} {'dist→A':>7} {'dist→B':>7} {'dl Mbps':>7} {'decision':>14}"
    print(hdr); print("-"*len(hdr))

    fired = False
    for i, (lat, lon, dl, rtt) in enumerate(rows):
        # push UE state into the mocks
        _req("POST", f"{LOCATION_MOCK}/ue/{args.vm}", {"latitude": lat, "longitude": lon})
        _req("POST", f"{PQOS_MOCK}/ue/{args.vm}",
             {"predictedLatencyMs": rtt*1000, "predictedThroughputMbps": dl,
              "degradationExpected": dl < 50, "confidence": 0.9})
        # ask the controller
        c = _req("GET", f"{CONTROLLER}/candidates?vm_name={args.vm}"
                        f"&source_cluster={args.source_cluster}&kind={args.kind}")
        dA = haversine_km(lat, lon, rows[0][0], rows[0][1])
        dB = haversine_km(lat, lon, rows[-1][0], rows[-1][1])
        print(f"{i:>3} {lat:>10.5f},{lon:<9.5f} {dA:>7.3f} {dB:>7.3f} {dl:>7.1f} {c['decision']:>14}")

        if args.migrate and c["decision"] == "cross_cluster" and not fired:
            print(f"\n>>> flipped — firing migration to {c['target_cluster_id'][:8]} ...")
            _req("POST", f"{CONTROLLER}/migrate",
                 {"vm_name": args.vm, "source_cluster": args.source_cluster,
                  "source_namespace": args.namespace,
                  "procedure_id": f"replay-{int(time.time())}",
                  "preferred_target_cluster": c["target_cluster_id"]})
            fired = True
        time.sleep(args.step)


if __name__ == "__main__":
    main()
