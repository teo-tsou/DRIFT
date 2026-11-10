#!/usr/bin/env python3
"""
KPI-4 harness — "rollback recovery rate" (proposal O-6).

The proposal defines KPI-4 as the *success rate of reversion to the previous
edge if a migration fails*, target 95-100%. This drives the DRIFT backend's
cross-cluster migration API repeatedly, injecting a post-migration health
failure on a configurable fraction of runs, and measures how reliably the
controller rolls the service back to the original edge.

Each faulted run is expected to end `rolled_back`; each clean run `succeeded`.
The harness tracks the VM's current cluster (a clean success flips it, a
rollback keeps it) so it always migrates from where the VM actually is.

  python3 kpi4_rollback.py \
      --backend http://localhost:8000 \
      --cluster-a <A-id> --cluster-b <B-id> \
      --vm evpn-test-vm --iterations 10 --fault-rate 1.0

Report: per-iteration outcome + duration, and the KPI-4 rollback recovery rate
(rolled_back / faulted runs), plus overall migration success.
"""
import argparse
import random
import sys
import time
import urllib.request
import json

TERMINAL = {"succeeded", "rolled_back", "rollback_failed", "unhealthy", "failed"}


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def run_once(backend, api, src, dst, ns, vm, inject_fault, poll=10, timeout=900):
    """Fire one cross-migration src->dst (optionally faulted) and wait for a
    terminal status. Returns (status, duration_s)."""
    body = {"target_cluster_id": dst, "force_unhealthy": bool(inject_fault)}
    url = f"{backend}{api}/edge-clusters/{src}/all-vms/{ns}/{vm}/cross-migrate"
    start = time.time()
    mid = _post(url, body)["migration_id"]
    deadline = start + timeout
    status = "running"
    while time.time() < deadline:
        state = _get(f"{backend}{api}/cross-migrations/{mid}")
        status = state.get("status", "running")
        if status in TERMINAL:
            break
        time.sleep(poll)
    return status, time.time() - start, mid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="http://localhost:8000")
    ap.add_argument("--api-prefix", default="/api/v1")
    ap.add_argument("--cluster-a", required=True, help="cluster id where the VM starts")
    ap.add_argument("--cluster-b", required=True, help="the other cluster id")
    ap.add_argument("--vm", required=True)
    ap.add_argument("--namespace", default="default")
    ap.add_argument("--iterations", type=int, default=10)
    ap.add_argument("--fault-rate", type=float, default=1.0,
                    help="fraction of runs that inject a post-migration health failure")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    a, b = args.cluster_a, args.cluster_b
    current = a  # where the VM currently lives
    rows = []
    print(f"KPI-4 rollback-recovery harness: {args.iterations} iters, "
          f"fault-rate={args.fault_rate}\n")
    print(f"{'#':>2}  {'dir':^7}  {'fault':^5}  {'outcome':<15}  {'dur(s)':>7}")
    print("-" * 48)
    for i in range(1, args.iterations + 1):
        dst = b if current == a else a
        fault = random.random() < args.fault_rate
        status, dur, mid = run_once(
            args.backend, args.api_prefix, current, dst, args.namespace, args.vm, fault)
        dir_lbl = f"{'A' if current==a else 'B'}->{'B' if dst==b else 'A'}"
        print(f"{i:>2}  {dir_lbl:^7}  {str(fault):^5}  {status:<15}  {dur:>7.0f}")
        rows.append({"i": i, "fault": fault, "status": status, "dur": dur, "mid": mid})
        # update location: a clean success moves the VM; a rollback keeps it.
        if status == "succeeded":
            current = dst
        # rolled_back / rollback_failed / unhealthy / failed -> VM stays at `current`
        # (rollback_failed actually leaves it on dst, but that is the failure we count)
        if status == "rollback_failed":
            current = dst

    faulted = [r for r in rows if r["fault"]]
    recovered = [r for r in faulted if r["status"] == "rolled_back"]
    clean = [r for r in rows if not r["fault"]]
    clean_ok = [r for r in clean if r["status"] == "succeeded"]

    print("\n== KPI-4 report ==")
    print(f"iterations               : {len(rows)}")
    print(f"faulted (rollback tested): {len(faulted)}")
    if faulted:
        rate = 100.0 * len(recovered) / len(faulted)
        print(f"rolled_back              : {len(recovered)}/{len(faulted)}")
        print(f"KPI-4 rollback recovery  : {rate:.1f}%   (target 95-100%)")
        avg = sum(r['dur'] for r in faulted) / len(faulted)
        print(f"avg faulted-run time     : {avg:.0f}s  (forward + rollback)")
    if clean:
        print(f"clean migrations         : {len(clean_ok)}/{len(clean)} succeeded")
    # non-zero exit if any faulted run failed to recover
    return 0 if len(recovered) == len(faulted) else 1


if __name__ == "__main__":
    sys.exit(main())
