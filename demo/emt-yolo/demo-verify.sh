#!/usr/bin/env bash
# Live, read-only evidence for the vehicle <-> edge migration demo, in tmux.
# Each pane checks one claim of the GUI from an independent source:
#
#   +------------------------------+------------------------------+
#   | pods: the pod and the YOLO   | power: CPU and power of the  |
#   | process on each node         | vehicle Nano and the edge    |
#   |                              | Orin, one line per second    |
#   +------------------------------+------------------------------+
#   | stream: inside the vehicle, where the camera goes, and that |
#   | YOLO's frames/fps                                           |
#   +-------------------------------------------------------------+
#   | log: drift-pod-migrate log of the latest migration + MEC 021|
#   +-------------------------------------------------------------+
#
# Usage: demo-verify.sh            start (or reattach to) the tmux session
#        demo-verify.sh <pane>     run one pane: pods | stream | power [nano|orin] | log | api [all]
# Runs on the hub as root. Nothing here changes any state.
set -u

SESSION=drift-verify
API=${DRIFT_API:-http://localhost:8000/api/v1}
POD=emt-yolo
KC=/app/kubeconfigs
NANO=(ssh -o HostKeyAlias=10.220.2.171 -o ServerAliveInterval=5 ecb@10.250.0.2)
ORIN=(ssh -o ServerAliveInterval=5 ecb@10.220.2.170)
B=$'\e[1m' G=$'\e[32m' Y=$'\e[33m' C=$'\e[36m' R=$'\e[31m' N=$'\e[0m'
# every pane in Athens time, as the GUI and the Jetsons show it (the hub and the
# migration log run in UTC)
export TZ=Europe/Athens
UTC_OFF=$(( 10#$(date +%z | cut -c2-3) ))

# kubectl alone misleads here: after a restore the pod reads "1/2 Error",
# because DRIFT kills the pod's placeholder "work" container and CRIU restores
# YOLO into the pod's sandbox (the "holder"), outside kubelet's view. So show
# the pod object AND the YOLO process actually running on each node.
site() {
  local label=$1 kc=$2; shift 2
  local pod proc
  pod=$(docker exec drift-backend kubectl --kubeconfig "$KC/$kc" get pod $POD --no-headers \
        -o custom-columns='IP:.status.podIP,NODE:.spec.nodeName,AGE:.metadata.creationTimestamp' 2>&1)
  proc=$("$@" 'p=$(pgrep -f "[d]rift_receiver.py" | head -1); [ -n "$p" ] && ps -o pid=,etime=,rss=,args= -p $p' 2>/dev/null)
  printf '%s%s%s\n' "$C" "$label" "$N"
  if [[ $pod == *NotFound* ]]; then printf '  pod     %s(none)%s\n' "$Y" "$N"
  else printf '  pod     %s %s\n' "$POD" "$pod"; fi
  if [[ -n $proc ]]; then
    read -r pid up rss args <<<"$proc"
    printf '  YOLO    %sRUNNING%s pid %s, up %s, %s MiB  (%s)\n' "$G" "$N" "$pid" "$up" "$((rss / 1024))" "$args"
  else printf '  YOLO    %snot running here%s\n' "$Y" "$N"; fi
}

pods() {
  while :; do
    local a b
    a=$(site "VEHICLE  drift-vehicle, Jetson Orin Nano (over 5G)" drift-vehicle-kubeconfig.yaml "${NANO[@]}")
    b=$(site "EDGE     drift-edge-agx, Jetson AGX Orin (PV site)" drift-edge-agx-kubeconfig.yaml "${ORIN[@]}")
    printf '\e[H\e[2J%s%s  where emt-yolo lives (kubectl + the process on each node)%s\n\n%s\n\n%s\n' \
      "$B" "$(date +%T)" "$N" "$a" "$b"
    sleep 1
  done
}

stream() {
  echo "${B}Inside the vehicle${N}: camera stream target (drift-yolo-target) and that YOLO's /health"
  # one long-lived ssh session: the probe loop runs on the vehicle itself
  "${NANO[@]}" 'while :; do t=$(sudo -n drift-yolo-target show 2>/dev/null); ip=${t#* };
      h=$(curl -s --max-time 0.8 http://$ip:9000/health 2>/dev/null | tr -d "\n");
      echo "$(date +%T.%N | cut -c1-12)|$t|${h:-{\}}"; sleep 1; done' |
  while IFS='|' read -r ts t h; do
    local where=${t%% *} ip=${t#* } p fps
    case $where in edge) where="EDGE   ";; local) where="VEHICLE";; esac
    [[ $t != "${prev_t:-}" && -n ${prev_t:-} ]] && echo "${Y}${B}>>> stream re-aimed: ${prev_t} -> ${t}${N}"
    prev_t=$t
    p=$(jq -r '.processed // empty' <<<"$h" 2>/dev/null); fps=$(jq -r '.fps // empty' <<<"$h" 2>/dev/null)
    if [[ -n $p ]]; then
      printf '%s  camera -> %s%s%s %-13s  YOLO %s%5s fps%s  frames %s (+%s)\n' "$ts" "$C" "$where" "$N" "$ip" \
        "$G" "$fps" "$N" "$p" "$(( p - ${prev_p:-$p} ))"
      prev_p=$p
    else
      printf '%s  camera -> %s%s%s %-13s  %sYOLO not answering (frozen / restoring)%s\n' "$ts" "$C" "$where" "$N" "$ip" "$R" "$N"
    fi
  done
}

power() {
  local name=$1; local -a ssh_cmd
  if [[ $name == nano ]]; then ssh_cmd=("${NANO[@]}"); label="VEHICLE Jetson Orin Nano"; else ssh_cmd=("${ORIN[@]}"); label="EDGE Jetson AGX Orin"; fi
  echo "${B}${label}${N}: tegrastats (YOLO runs on the CPU)"
  "${ssh_cmd[@]}" 'sudo -n tegrastats --interval 1000' | awk -v name="$name" '
    { cpu = 0; n = 0; tot = 0
      for (i = 1; i <= NF; i++) {
        if ($i == "CPU") { s = $(i + 1); gsub(/[\[\]]/, "", s); n = split(s, a, ","); for (j = 1; j <= n; j++) cpu += a[j] + 0 }
        if ($i ~ /^(VDD_IN|VDD_GPU_SOC|VDD_CPU_CV|VIN_SYS_5V0)$/) { v = $(i + 1); sub(/mW.*/, "", v); w[$i] = v / 1000 }
      }
      cpu = n ? cpu / n : 0
      tot = (name == "nano") ? w["VDD_IN"] : w["VDD_GPU_SOC"] + w["VDD_CPU_CV"] + w["VIN_SYS_5V0"]
      bar = ""; for (k = 0; k < cpu / 10; k++) bar = bar "#"
      printf "%s  CPU %3.0f%% %-10s %5.1f W\n", $2, cpu, bar, tot; fflush() }'
}

# Both devices on one line per second: the load and power visibly change sides.
power_both() {
  local d; d=$(mktemp -d)
  # Background jobs of a script ignore Ctrl+C, so the two ssh/tegrastats pipes
  # would outlive the pane: on any exit, take down the whole process group.
  trap 'trap - EXIT INT TERM HUP; rm -f "$d/nano" "$d/orin"; rmdir "$d" 2>/dev/null; kill 0' EXIT INT TERM HUP
  power nano > "$d/nano" 2>/dev/null &
  power orin > "$d/orin" 2>/dev/null &
  echo "${B}CPU and power${N} (tegrastats; YOLO runs on the CPU, so the CPU load moves with it)"
  local a b
  while sleep 1; do
    a=$(grep " W$" "$d/nano" 2>/dev/null | tail -n1 | cut -c11-)
    b=$(grep " W$" "$d/orin" 2>/dev/null | tail -n1 | cut -c11-)
    printf '%s  %sVEHICLE%s %-30s  %sEDGE%s %s\n' "$(date +%T)" "$C" "$N" "${a:-...}" "$C" "$N" "${b:-...}"
  done
}

log() {
  echo "${B}drift-pod-migrate${N}: live log of the latest vehicle<->edge migration, and its MEC 021 procedure"
  local last="" n=0 ms="" st id mec out total done_=1 t=0
  while :; do
    if (( done_ || t % 4 == 0 )); then          # discover new migrations every 2 s
      st=$(curl -s -m 8 "$API/extreme-edge/state") || { sleep 1; continue; }
      mec=$(jq -r '.mec | select(.procedure_id) | "\(.procedure_id[0:8]) \(.state) \(.decision // "")"' <<<"$st" 2>/dev/null)
      if [[ -n $mec && $mec != "$ms" ]]; then echo "${C}MEC 021 mobility procedure ${mec}${N}"; ms=$mec; fi
      id=$(jq -r '.migrations[0].id // empty' <<<"$st" 2>/dev/null)
      if [[ -n $id && $id != "$last" ]]; then
        echo; echo "${Y}${B}=== migration ${id:0:8}: $(jq -r '.migrations[0] | "\(.from) -> \(.to)"' <<<"$st") ===${N}"
        last=$id; n=0
      fi
    fi
    if [[ -n $last ]]; then
      out=$(curl -s -m 5 "$API/pod-migrations/$last") || { sleep 1; continue; }
      done_=$(jq -r 'if (.completed or .failed) then 1 else 0 end' <<<"$out")
      out=$(jq -r '.output // .error // ""' <<<"$out")
      total=$(printf '%s\n' "$out" | wc -l)
      if (( total > n )); then
        printf '%s\n' "$out" | tail -n +$((n + 1)) |
          awk -v off="$UTC_OFF" 'substr($0,1,1) == "[" && substr($0,4,1) == ":" {
            $0 = sprintf("[%02d%s", (substr($0,2,2) + off) % 24, substr($0,4)) } { print; fflush() }' | sed -E "s/(freeze[^,)]*)/${G}\1${N}/; s/(RESTORED|migration complete)/${G}${B}\1${N}/; s/(FAILED|ERROR|rollback)/${R}${B}\1${N}/I"
        n=$total
      fi
    fi
    t=$((t + 1)); sleep 0.5
  done
}

api() {
  local all=${1:-}                     # "all": also the calls that did not succeed
  echo "${B}ENVELOPE APIs${N}: calls DRIFT makes to the pQoS AF and the CAMARA QoD API, request and answer"
  echo "(the page re-polls both every 15 s: a repeated call with the same answer is counted, not printed)"
  local seq=0 epoch="" d e row k st line quiet=0 tq=$SECONDS
  declare -A last=()
  while :; do
    d=$(curl -s -m 5 "$API/extreme-edge/api-log?after=$seq") || { sleep 1; continue; }
    e=$(jq -r '.epoch' <<<"$d" 2>/dev/null) || { sleep 1; continue; }
    if [[ -n $epoch && $e != "$epoch" ]]; then echo "${Y}--- log cleared ---${N}"; seq=0; last=(); epoch=$e; continue; fi
    epoch=$e
    while IFS= read -r row; do
      [[ -z $row ]] && continue
      seq=$(jq -r .seq <<<"$row")
      [[ $all == all ]] || jq -e '.status != null and .status >= 200 and .status < 300' <<<"$row" >/dev/null || continue
      # one slot per endpoint (and pQoS metric); print only when its answer changes
      k=$(jq -r '"\(.api) \(.method) \(.path) \(.request.QosType // "")"' <<<"$row")
      if [[ ${last[$k]:-} == "$(jq -c '[.status, .request, .response]' <<<"$row")" ]]; then quiet=$((quiet + 1)); continue; fi
      last[$k]=$(jq -c '[.status, .request, .response]' <<<"$row")
      st=$(jq -r '.status // "ERR"' <<<"$row")
      line=$(jq -r '"\(.t | strflocaltime("%H:%M:%S"))  \(.api)  \(.method) \(.path)"' <<<"$row")
      if [[ $st =~ ^2 ]]; then st="$G$st$N"; else st="$R$st$N"; fi
      printf '%s%s%s  %s  %s ms\n' "$C" "$line" "$N" "$st" "$(jq -r .ms <<<"$row")"
      jq -r '"    request  \(.request | tojson)", "    response \(.response | tojson | .[0:400])"' <<<"$row"
    done < <(jq -c '.rows[]' <<<"$d")
    if (( SECONDS - tq >= 60 && quiet > 0 )); then
      echo "$(date +%T)  ... $quiet repeated poll(s), answers unchanged"; quiet=0; tq=$SECONDS
    fi
    sleep 1
  done
}

case ${1:-} in
  pods|stream|log) "$1" ;;
  api) api "${2:-}" ;;
  power) if [[ -n ${2:-} ]]; then power "$2"; else power_both; fi ;;
  "")
    me=$(readlink -f "$0")
    if ! tmux has-session -t $SESSION 2>/dev/null; then
      tmux new-session -d -s $SESSION -x 220 -y 55 "$me pods"
      tmux split-window -h -t $SESSION:0.0 "$me power"
      tmux split-window -v -t $SESSION:0.0 -f "$me stream"
      tmux split-window -v -t $SESSION:0.2 -f "$me log"
      tmux set -t $SESSION mouse on >/dev/null
    fi
    exec tmux attach -t $SESSION ;;
  *) sed -n '2,/^set -u/p' "$0" | sed '$d'; exit 1 ;;
esac
