#!/usr/bin/env python3
"""KPI-7 between the x86 edge clusters: time from the ETSI MEC 021 trigger to the
start of the migration on the backend.

  kpi7_edge.py N OUT.csv [--target]   run on the hub

Registers the video-server pod (my-app) with the MEC 021 API, then runs N
mobility procedures one after the other (the pod moves back and forth between
drift-edge-1 and drift-edge-2). Each procedure is timed from the container logs:

  t_trigger   mec021-api: POST .../mobility_procedures
  t_selected  drift-controller: POST .../cross-migrate leaves the controller
  t_initiated drift-backend: POST .../cross-migrate (the migration has started)

KPI-7 = t_initiated - t_trigger. Without --target the controller chooses the
target itself (Optimal Edge Selection, as for a pQoS-triggered procedure);
with --target the request names the other cluster.
"""
import csv, json, re, subprocess, sys, time, urllib.request
from datetime import datetime

MEC = "http://localhost:8010/amsi/v1"
CLUSTERS = {"drift-edge-1": "84a2f30e-d863-4f7d-a355-67f89c2a99e9",
            "drift-edge-2": "05c9c40d-fccf-49b0-8438-5d993c5a5436"}
NAMES = {v: k for k, v in CLUSTERS.items()}


def http(method, url, body=None):
    req = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def log_times(container, since, pattern):
    out = subprocess.run(["docker", "logs", "--timestamps", "--since", since, container],
                         capture_output=True, text=True).stdout + \
          subprocess.run(["docker", "logs", "--timestamps", "--since", since, container],
                         capture_output=True, text=True).stderr
    ts = []
    for line in out.splitlines():
        if re.search(pattern, line):
            t = line.split(" ", 1)[0]
            ts.append(datetime.fromisoformat(t[:26] + "+00:00").timestamp())
    return sorted(ts)


def where():
    for name in CLUSTERS:
        r = subprocess.run(["docker", "exec", "drift-backend", "kubectl", "--kubeconfig",
                            f"/app/kubeconfigs/{name}-kubeconfig.yaml", "get", "pod", "my-app",
                            "-o", "jsonpath={.status.phase}"], capture_output=True, text=True)
        if r.stdout.strip() == "Running":
            return name
    return None


def main():
    n, out = int(sys.argv[1]), sys.argv[2]
    with_target = "--target" in sys.argv
    src = where()
    if not src:
        sys.exit("my-app is not running on either edge cluster")
    ams = http("POST", f"{MEC}/app_mobility_services", {
        "registered_app_context": {"associated_app_id": "kpi7-video", "source_cluster": CLUSTERS[src],
                                   "source_namespace": "default", "vm_name": "my-app",
                                   "resource_kind": "pod", "autonomous_trigger": False},
        "service_continuity_mode": 1})
    ams_id = ams["app_mobility_service_id"]
    rows = []
    for i in range(1, n + 1):
        src = where()
        dst = "drift-edge-2" if src == "drift-edge-1" else "drift-edge-1"
        since = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        time.sleep(1.1)
        body = {"target_cluster": CLUSTERS[dst]} if with_target else {}
        p = http("POST", f"{MEC}/app_mobility_services/{ams_id}/mobility_procedures", body)
        pid = p["mobility_procedure_id"]
        state = None
        for _ in range(300):
            state = http("GET", f"{MEC}/app_mobility_services/{ams_id}/mobility_procedures/{pid}")
            if state["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
                break
            time.sleep(1)
        trig = log_times("drift-mec021-api", since, r"POST /amsi/v1/app_mobility_services/.*/mobility_procedures")
        sel = log_times("drift-controller", since, r"cross-migrate")
        ini = log_times("drift-backend", since, r"POST /api/v1/edge-clusters/.*/cross-migrate")
        t0 = trig[0] if trig else None
        t2 = ini[0] if ini else None
        row = dict(round=i, procedure_id=pid, source=src, target=NAMES.get(state.get("target_cluster"), state.get("target_cluster")),
                   target_given=with_target, state=state["state"], decision=state.get("decision"),
                   t_trigger=t0, t_initiated=t2,
                   trigger_to_initiated_s=round(t2 - t0, 3) if t0 and t2 else None,
                   migration_s=round((state.get("migration_latency_ms") or 0) / 1000, 2))
        print(json.dumps(row), flush=True)
        rows.append(row)
        if state["state"] != "COMPLETED":
            print("procedure did not complete:", state.get("error_details"), flush=True)
        time.sleep(10)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)


if __name__ == "__main__":
    main()
