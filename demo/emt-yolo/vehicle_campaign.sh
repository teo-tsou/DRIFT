#!/usr/bin/env bash
# Vehicle <-> edge YOLO migration campaign: MIGRATIONS hops alternating Nano <-> Orin
# through the DRIFT backend, one CSV row per migration.
#
# Frames lost come from the receiver itself: it records every stall >1 s between
# two processed frames (/health "gaps", kept in process memory so it migrates
# with the workload), and the stream runs at 12 FPS. The backend gives the
# freeze (drift-pod-migrate) and the bulk-stream line (image size, time, rate).
#
#   vehicle_campaign.sh [MIGRATIONS] [OUT_DIR]   # run on the hub
# Each migration moves YOLO to the other side of wherever drift-yolo-target
# points now, so a campaign can resume after an interruption.
set -euo pipefail
MIGRATIONS=${1:-6}
OUT=${2:-runs/$(date +%F)-vehicle-migration}
API=${DRIFT_API:-http://localhost:8000/api/v1}
VEHICLE=${VEHICLE_CLUSTER:-4e77453e-d2f0-43fc-9e7b-5d8392961058}   # drift-vehicle
EDGE=${EDGE_CLUSTER:-5b362986-8f67-4c8d-9fd4-0b0644d8553b}         # drift-edge-agx
NANO=(ssh -i /root/.ssh/id_rsa -o HostKeyAlias=10.220.2.171 ecb@10.250.0.2)
SETTLE=${SETTLE:-60}
SENDER_FPS=12
mkdir -p "$OUT"
CSV=$OUT/migrations.csv
[[ -f $CSV ]] || echo "when,direction,migration_id,status,compute_mode,freeze_s,image_mib,transfer_s,image_mbit_s,wire_mib,gap_s,frames_lost,fps_after,freeze_start,t_dumped,t_sent,t_received,t_restored" > "$CSV"

# /health of wherever the DNAT points, read on the Nano (retries: the edge's
# floating IP is polled over 5G).
health() {
  "${NANO[@]}" 'T=$(cut -d" " -f2 /run/drift-yolo-target); for i in 1 2 3 4 5; do curl -s --max-time 4 http://$T:9000/health && exit 0; sleep 2; done; exit 1' 2>/dev/null
}

migrate() {   # $1 source cluster, $2 target cluster, $3 label
  local before mid row
  local st=$OUT/.status.json out=$OUT/.health.json
  before=$(date +%s)   # gaps count for this migration only if they start after this
  mid=$(curl -s --max-time 30 -X POST "$API/edge-clusters/$1/all-pods/default/emt-yolo/cross-migrate" \
        -H 'Content-Type: application/json' -d "{\"target_cluster_id\":\"$2\"}" |
        python3 -c 'import sys,json; print(json.load(sys.stdin)["migration_id"])')
  echo "$(date +%T) $3 started ($mid)"
  for _ in $(seq 1 200); do
    curl -s --max-time 10 "$API/pod-migrations/$mid" > "$st"
    python3 -c 'import sys,json; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("completed") or d.get("failed") else 1)' "$st" && break
    sleep 3
  done
  sleep "$SETTLE"
  health > "$out" || echo '{}' > "$out"
  row=$(python3 - "$3" "$mid" "$before" "$SENDER_FPS" "$st" "$out" <<'EOF'
import json, re, sys, time
label, mid, t0, fps = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
st = json.load(open(sys.argv[5]))
h = json.load(open(sys.argv[6]))
m = re.search(r"([0-9.]+) MiB of image in ([0-9.]+)s \(~([0-9.]+) Mbit/s", st.get("output") or st.get("error") or "")
# a gap is recorded only when the next frame is processed, so match it to this
# migration by its START (the quiesce, a few seconds after the request)
new = [g for g in (h.get("gaps") or []) if t0 - 5 <= g[0] <= time.time()]
gap = max((g[1] for g in new), default=None)
print(",".join(str(x) for x in [time.strftime("%F %T"), label, mid,
      "completed" if st.get("completed") else "failed", st.get("compute_mode", ""),
      st.get("freeze_s", ""), *(m.groups() if m else ("", "", "")),
      (re.search(r"([0-9.]+) MiB on the wire", st.get("output") or "") or [None, ""])[1],
      gap if gap is not None else "", round(gap * fps) - 1 if gap else "", h.get("fps", ""),
      # when the freeze began (backend clock) and the stage offsets from it
      (re.search(r"\[([0-9:.]+)\][^\n]*\(freeze begins\)", st.get("output") or "") or [None, ""])[1],
      *[(re.search(k + r" \+([0-9.]+)s", st.get("output") or "") or [None, ""])[1]
        for k in ("dumped", "sent", "received", "restored")]]))
EOF
)
  echo "$row" >> "$CSV"
  echo "  $row"
  cp "$st" "$OUT/$mid.json"
}

for _ in $(seq 1 "$MIGRATIONS"); do
  if "${NANO[@]}" 'grep -q ^edge /run/drift-yolo-target' 2>/dev/null; then
    migrate "$EDGE" "$VEHICLE" "orin->nano"
  else
    migrate "$VEHICLE" "$EDGE" "nano->orin"
  fi
done
echo "done: $CSV"
