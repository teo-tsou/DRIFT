#!/usr/bin/env bash
#
# drift-pod-migrate — controller-driven CRIU live migration of a Kubernetes pod
# workload container between CRI-O edge nodes, via a shared checkpoint store.
#
# It drives crun directly (crun-drift, dynamic + CRIU), bypassing the kubelet /
# CRI-O restore-from-image path (which is broken on the K3s+CRI-O edge stack:
# time-namespace cold-start, masked-mount and unified-cgroup restore failures).
# Raw crun checkpoint/restore is reliable, so DRIFT orchestrates it itself.
#
# Design (proven on kernel 5.15 / CRI-O 1.32 / crun 1.19.1):
#   * Both the SOURCE and TARGET pods are 2-container pods: a `holder`
#     (sleep infinity) that keeps the pod sandbox + netns + IP alive, and the
#     `work` container that is actually migrated. The holder is why the target
#     netns survives for the restore to rejoin, and why the source can be
#     checkpointed without CRI-O reaping the sandbox.
#   * The TARGET pod is PRE-CREATED (same workload image, `work` running a
#     placeholder) so its bundle already carries the right rootfs + netns.
#     For seamless sockets the target must own the SAME IP as the source —
#     intra-cluster via kube-ovn `ovn.kubernetes.io/ip_address`, cross-cluster
#     via the EVPN stretched-L2 (see the evpn_l2 role).
#   * Optional PRE-COPY: iterative `crun checkpoint --pre-dump` ships memory
#     while the container keeps running; the final freeze then dumps only the
#     dirty delta — keeping the freeze small for a given dirty set.
#   * Optional POST-COPY (--post-copy): the source dumps ONLY non-lazy state and
#     stays alive as a CRIU page-server; the target resumes on that metadata and
#     faults memory in over the network on demand (userfaultfd). Bulk memory
#     never touches the store, so the freeze is INDEPENDENT of workload memory
#     size (measured flat at ~1.2 s for 300 MB and 1 GB). Uses runc (native
#     --lazy-pages); post-copy pods must be runc-managed, not runtimeClassName:
#     crun. Antagonistic to --pre-copy (pre-dumped pages restore eagerly), so
#     --pre-copy is ignored under --post-copy.
#   * The checkpoint images live on a shared store (NFS) mounted at the same
#     path on both nodes, so no per-migration copy sits on the frozen path.
#
# Usage:
#   drift-pod-migrate \
#       --src-host A --src-pod app --dst-host B --dst-pod app-standby \
#       [--namespace default] [--container work] \
#       [--store /srv/drift-ckpt] [--pre-copy 2] [--tcp] [--post-copy] [--keep-source]
#
# --src-host / --dst-host may be a hostname/IP reached over SSH, or "local"
# when the controller runs on the node itself.
#
# Seamless TCP across the hop (--l2-ip): when the workload listens on a floating
# IP carried by a shared-L2 secondary interface (a macvlan on the physical NIC,
# same IP reachable from either node), add:
#       --tcp --l2-ip 10.0.1.222/24 --l2-iface net1 --l2-dummy 10.0.1.223/24
# The source pod owns the floating IP; PRE-CREATE the target pod with the same
# macvlan attachment but a DUMMY ip (--l2-dummy) so it isn't dual-active. The
# controller then fences the source, moves the floating IP onto the target's
# interface, restores, and GARPs — so an established TCP connection survives.
# Requires nsenter + iputils-arping on the destination host.
#
# Bulk stream (--bulk-stream, blocking only): for a hop over a high-latency WAN
# (the vehicle's 5G), where a dump into the shared NFS store pays a round trip
# per write and post-copy one per page. --store is then a NODE-LOCAL dir: the
# source dumps locally and sends the image as one tar|zstd stream straight to the
# target, which restores once it has all of it.
#
# Quiesce (--quiesce-cmd CMD --resume-cmd CMD, run on --follow-host): stop the
# workload's input DRIFT_QUIESCE_S (1.5) seconds before the freeze. An idle YOLO
# receiver frees its per-frame buffers within ~1 s (84 -> 52 MiB measured), so
# the checkpoint shrinks by ~40% for 1.5 s of frames that a frozen process
# would have lost anyway. Any failure after this resumes the input.
#
# Stream follow by DNAT (--follow-host HOST --follow-cmd CMD): for a workload
# whose client sends to an address it never changes (a fixed VIP DNATed on the
# client's node, e.g. the vehicle Jetson's drift-yolo-target), CMD runs on HOST
# the moment the restore is confirmed live, before the post-copy page drain
# (which can take tens of seconds over a 5G uplink).
#
# Seamless TCP INTRA-cluster (--ovn-ip): carry the pod's OWN kube-ovn primary IP
# to the target NODE (same cluster) — no macvlan/EVPN needed, the overlay routes
# it. PRE-CREATE the target pod on the destination node (its own placeholder IP):
#       --ovn-ip 10.42.0.199    [--ovn-iface eth0]
# The controller freezes+checkpoints the source, fences it (frees the IP), then
# re-points the TARGET's OVN logical switch port (ovn-nbctl lsp-set-addresses) to
# the source's MAC+IP and moves the target's iface onto it before restoring; OVN
# binds that port to the target node's chassis, so the IP follows. Implies --tcp.
#   Add --ovn-multichassis for lower downtime: pre-bind the target chassis as an
#   ADDITIONAL binding on the source's port (OVN multi-chassis) BEFORE the freeze,
#   so the target datapath is programmed off the hot path and the cutover is just a
#   primary flip. ~0.96s freeze vs ~1.9s. Reuses the source LSP on the target, so
#   the source pod is kept as the port owner (not fenced).
#   Compute (post-copy | blocking) and network-follow (multichassis | l2) are
#   ORTHOGONAL: --ovn-multichassis --post-copy is the intra-cluster container
#   strategy — memory-INDEPENDENT freeze (source serves pages) AND seamless TCP
#   (IP follows). Post-copy composes only with multichassis (keeps the source
#   alive to serve pages), not single-chassis re-point (which fences it).
set -euo pipefail

# ── defaults ────────────────────────────────────────────────────────────────
NAMESPACE=default
CONTAINER=work
STORE=/srv/drift-ckpt
PRECOPY=0
TCP=0
KEEP_SOURCE=0
# Node transport. Default is the drift-node-agent (a privileged DaemonSet reached
# at <nodeIP>:$NODE_AGENT_PORT) so migrations run THROUGH the cluster, no SSH. Set
# DRIFT_NODE_AGENT_PORT + DRIFT_NODE_AGENT_TOKEN to use it; leave the port empty to
# fall back to SSH (DRIFT_SSH_OPTS).
NODE_AGENT_PORT=${DRIFT_NODE_AGENT_PORT:-}
NODE_AGENT_TOKEN=${DRIFT_NODE_AGENT_TOKEN:-}
# Multiplex SSH (fallback transport): a persistent master connection makes each
# subsequent call ~free (no TCP+auth setup), which matters for the --l2-ip hot path.
SSH_OPTS=${DRIFT_SSH_OPTS:-"-o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=auto -o ControlPath=/tmp/drift-ssh-%r@%h:%p -o ControlPersist=30"}
CRUN=${DRIFT_CRUN:-/usr/local/bin/crun-drift}
CRUN_ROOT=${DRIFT_CRUN_ROOT:-/run/crun}
# Post-copy (lazy-pages) uses runc: libcriu (which crun-drift links) has no
# lazy-pages support, but runc execs the criu binary, which does. Post-copy pods
# must therefore be runc-managed (default runtime, not runtimeClassName: crun).
# The source dumps ONLY non-lazy state and stays alive as a CRIU page-server on
# $LAZY_PORT; anonymous memory streams to the target on demand (never touches the
# store) — so the freeze is memory-INDEPENDENT.
POSTCOPY=0
# Per-run port for the lazy page-server. Left empty here and randomized per
# migration below (unless pinned via DRIFT_LAZY_PORT) so a crashed run that leaks
# a page-server never blocks the next one on a fixed port.
LAZY_PORT=${DRIFT_LAZY_PORT:-}
RUNC=${DRIFT_RUNC:-/usr/libexec/crio/runc}
RUNC_ROOT=${DRIFT_RUNC_ROOT:-/run/runc}
CRICTL=${DRIFT_CRICTL:-"crictl -r unix:///var/run/crio/crio.sock"}
CGROUP_STRIP=${DRIFT_CGROUP_STRIP:-/usr/local/bin/drift-cgroup-strip}
# Cross-host restores fail on the source's cpuset props in cgroup.img, which the
# strip helper removes (~0.35-0.4 s INSIDE the freeze: python + two crit runs).
# "ignore" instead has CRIU leave cgroups alone: runc places the restored task in
# the TARGET container's own cgroup, so there is nothing to strip.
CG_MODE=${DRIFT_CG_MODE:-ignore}
CGFLAG=""; [[ $CG_MODE == soft ]] || CGFLAG="--manage-cgroups-mode $CG_MODE"
KUBECTL=${DRIFT_KUBECTL:-kubectl}
KUBECONFIG_PATH=${DRIFT_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}
L2_IP="" L2_IFACE=net1 L2_DUMMY=""
FOLLOW_HOST="" FOLLOW_CMD="" FOLLOW_AIM_CMD="" AIM_PID=""
QUIESCE_CMD="" RESUME_CMD="" QUIESCED=0 QUIESCE_S=${DRIFT_QUIESCE_S:-0}
BULK=0 BULK_DIAL=src
# --ovn-ip: INTRA-cluster primary-IP follow. Instead of a shared-L2 macvlan
# (--l2-ip, for CROSS-cluster), carry the pod's own kube-ovn primary IP to the
# target node by re-pointing the target's OVN logical switch port — the overlay
# handles routing to whichever node now hosts it (no macvlan/GARP/EVPN needed).
OVN_IP="" OVN_IFACE=eth0 OVN_NS=kube-system
# --ovn-multichassis: zero-downtime variant of --ovn-ip. Pre-binds the target
# node's chassis as an ADDITIONAL binding on the source's logical port (OVN
# multi-chassis, requested-chassis="src,dst") BEFORE the freeze, so the target
# datapath is already programmed; the cutover is then just a primary flip (no
# convergence wait). Reuses the source LSP on the target (iface-id surgery), so
# the source pod is kept as the LSP owner (not fenced).
MC=0
SRC_HOST="" DST_HOST="" SRC_POD="" DST_POD=""

# ── O-6 rollback state ───────────────────────────────────────────────────────
# Armed the moment the source workload is stopped (end of compute_freeze_src)
# and disarmed once the target has restored. If anything fails inside that
# window, die() calls rollback(): restore the checkpoint image BACK onto the
# still-frozen source and re-assert its network identity. This is reliable for
# the production paths (--post-copy keeps the source alive + serving pages, and
# the checkpoint image sits on the shared store), so a failed migration leaves
# the workload running where it started instead of dead on both ends.
ROLLBACK_ARMED=0 ROLLBACK_DONE=0 ROLLBACK_NET_CMD=""

log() { echo "[$(date +%H:%M:%S.%3N)] $*"; }
die() {
  echo "drift-pod-migrate: $*" >&2
  [[ $ROLLBACK_ARMED == 1 ]] && rollback
  bulk_pd_cleanup
  # an aim still in flight must land BEFORE the resume, or it would win
  [[ -n ${AIM_PID:-} ]] && { wait "$AIM_PID" 2>/dev/null || true; }
  if [[ $QUIESCED == 1 && -n $RESUME_CMD ]]; then
    on "$FOLLOW_HOST" "$RESUME_CMD" >/dev/null 2>&1 && echo "drift-pod-migrate: input resumed on $FOLLOW_HOST" >&2
  fi
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --src-host)   SRC_HOST=$2; shift 2;;
    --dst-host)   DST_HOST=$2; shift 2;;
    --src-pod)    SRC_POD=$2; shift 2;;
    --dst-pod)    DST_POD=$2; shift 2;;
    --namespace)  NAMESPACE=$2; shift 2;;
    --container)  CONTAINER=$2; shift 2;;
    --store)      STORE=$2; shift 2;;
    --pre-copy)   PRECOPY=$2; shift 2;;
    --tcp)        TCP=1; shift;;
    --post-copy)  POSTCOPY=1; shift;;
    --keep-source) KEEP_SOURCE=1; shift;;
    --l2-ip)      L2_IP=$2; shift 2;;
    --l2-iface)   L2_IFACE=$2; shift 2;;
    --l2-dummy)   L2_DUMMY=$2; shift 2;;
    --follow-host) FOLLOW_HOST=$2; shift 2;;
    --bulk-stream) BULK=1; shift;;
    --bulk-dial)  BULK_DIAL=$2; shift 2;;
    --follow-cmd) FOLLOW_CMD=$2; shift 2;;
    --follow-aim-cmd) FOLLOW_AIM_CMD=$2; shift 2;;
    --quiesce-cmd) QUIESCE_CMD=$2; shift 2;;
    --resume-cmd) RESUME_CMD=$2; shift 2;;
    --ovn-ip)     OVN_IP=$2; shift 2;;
    --ovn-iface)  OVN_IFACE=$2; shift 2;;
    --ovn-multichassis) MC=1; shift;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown argument: $1";;
  esac
done

[[ -n $SRC_HOST && -n $DST_HOST && -n $SRC_POD && -n $DST_POD ]] || \
  die "required: --src-host --src-pod --dst-host --dst-pod (see --help)"

# run a command on a node's drift-node-agent over HTTP. Body = the command's
# stdout; HTTP 200 iff it exited 0 (so the caller's success/failure check and
# `if on … grep -q …` conditionals behave exactly like SSH). stderr → agent log.
node_agent_exec() {
  local host=${1##*@}; shift
  local resp status
  resp=$(curl -sS --max-time 300 -H "Authorization: Bearer ${NODE_AGENT_TOKEN}" \
         --data-binary "$*" -w $'\n%{http_code}' \
         "http://${host}:${NODE_AGENT_PORT}/exec" 2>/dev/null) || return 1
  status=${resp##*$'\n'}      # trailing line = HTTP status
  resp=${resp%$'\n'*}         # everything before = command stdout
  printf '%s' "$resp"
  [[ $status == 2* ]]
}

# run a command on a node ("local" here; else the node-agent, or SSH if no agent)
on() {
  local host=$1; shift
  if [[ $host == local || $host == localhost ]]; then
    bash -c "$*"
  elif [[ -n $NODE_AGENT_PORT ]]; then
    node_agent_exec "$host" "$*"
  else
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$host" "$*"
  fi
}

# resolve the CRI-O container id of <pod>/<container> on a host. Falls back to an
# already-migrated container so a workload can be migrated MORE THAN ONCE (follow-me
# hops): after a controller-driven restore CRI-O shows the work container Exited
# (crictl ps hides it), while the runc/crun-restored process runs under the SAME id.
# Find it via `ps -a` and accept it only if a runtime root confirms it's running.
cid_of() {
  local host=$1 pod=$2 cid
  cid=$(on "$host" "$CRICTL ps --name $CONTAINER --label io.kubernetes.pod.name=$pod --label io.kubernetes.pod.namespace=$NAMESPACE -q 2>/dev/null | head -1")
  [[ -n $cid ]] && { echo "$cid"; return; }
  cid=$(on "$host" "$CRICTL ps -a --name $CONTAINER --label io.kubernetes.pod.name=$pod --label io.kubernetes.pod.namespace=$NAMESPACE -q 2>/dev/null | head -1")
  [[ -n $cid ]] || return
  on "$host" "$RUNC --root $RUNC_ROOT state $cid 2>/dev/null | grep -q '\"status\": \"running\"' \
      || $CRUN --root $CRUN_ROOT state $cid 2>/dev/null | grep -q '\"status\": \"running\"'" \
    && echo "$cid"
}

# resolve the pod sandbox network namespace path (/var/run/netns/<uuid>) on a host
netns_of() {
  local host=$1 pod=$2
  on "$host" "$CRICTL inspectp \$($CRICTL pods --name $pod --namespace $NAMESPACE -q 2>/dev/null | head -1) 2>/dev/null | grep -oE '/var/run/netns/[a-f0-9-]+' | head -1"
}

# Host with cluster (kubectl) access. In an intra-cluster migration the API +
# kubeconfig live on the control-plane node; k3s AGENTS have no kubeconfig, so
# kubectl/ovn-nbctl must run on $KUBE_HOST (default: the source; override with
# DRIFT_KUBE_HOST when the source is an agent).
KUBE_HOST=""

# delete the source pod (frees its IPs / secondary interfaces)
fence_source() {
  on "$KUBE_HOST" "KUBECONFIG=$KUBECONFIG_PATH $KUBECTL delete pod $SRC_POD -n $NAMESPACE --grace-period=0 --force 2>/dev/null || true"
}

# ── kube-ovn helpers (--ovn-ip intra-cluster primary-IP follow) ───────────────
# run ovn-nbctl inside the ovn-central pod (the OVN northbound lives there).
OVN_CENTRAL=""
nbctl() {
  [[ -n $OVN_CENTRAL ]] || OVN_CENTRAL=$(on "$KUBE_HOST" "KUBECONFIG=$KUBECONFIG_PATH $KUBECTL -n $OVN_NS get pod -l app=ovn-central -o jsonpath='{.items[0].metadata.name}' 2>/dev/null")
  [[ -n $OVN_CENTRAL ]] || die "ovn-central pod not found in ns $OVN_NS — is this a kube-ovn cluster?"
  on "$KUBE_HOST" "KUBECONFIG=$KUBECONFIG_PATH $KUBECTL -n $OVN_NS exec $OVN_CENTRAL -c ovn-central -- ovn-nbctl $*"
}
# run ovn-sbctl inside the ovn-central pod.
sbctl() {
  [[ -n $OVN_CENTRAL ]] || OVN_CENTRAL=$(on "$KUBE_HOST" "KUBECONFIG=$KUBECONFIG_PATH $KUBECTL -n $OVN_NS get pod -l app=ovn-central -o jsonpath='{.items[0].metadata.name}' 2>/dev/null")
  on "$KUBE_HOST" "KUBECONFIG=$KUBECONFIG_PATH $KUBECTL -n $OVN_NS exec $OVN_CENTRAL -c ovn-central -- ovn-sbctl $*"
}
# kube-ovn names a pod's logical switch port <pod>.<namespace>.
lsp_of() { echo "$1.$NAMESPACE"; }
# read the MAC from a pod's LSP addresses ("MAC IP").
ovn_mac_of() { nbctl "get logical_switch_port $(lsp_of "$1") addresses" 2>/dev/null | grep -oE '([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}' | head -1; }
# MAC of an explicit LSP (used when the serving LSP != <pod>.<ns>, e.g. after a hop).
ovn_mac_of_lsp() { nbctl "get logical_switch_port $1 addresses" 2>/dev/null | grep -oE '([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}' | head -1; }
# ── multi-chassis helpers (--ovn-multichassis) ───────────────────────────────
# k8s node name for a host IP (INTERNAL-IP is col 6 of `get nodes -o wide`).
node_of_ip() { on "$KUBE_HOST" "KUBECONFIG=$KUBECONFIG_PATH $KUBECTL get nodes -o wide --no-headers 2>/dev/null | awk -v ip=${1##*@} '\$6==ip{print \$1}'"; }
# the OVN chassis NAME kube-ovn assigned a node (requested-chassis uses the name).
chassis_of_node() { on "$KUBE_HOST" "KUBECONFIG=$KUBECONFIG_PATH $KUBECTL get node $1 -o jsonpath='{.metadata.annotations.ovn\.kubernetes\.io/chassis}' 2>/dev/null"; }
# run ovs-vsctl inside the ovs-ovn pod on a node. Select by spec.nodeName — NOT by
# a column in `-o wide`, whose NODE position shifts when RESTARTS reads "N (Xd ago)".
ovs_on() {
  local node=$1; shift
  local pod
  pod=$(on "$KUBE_HOST" "KUBECONFIG=$KUBECONFIG_PATH $KUBECTL -n $OVN_NS get pod -l app=ovs --field-selector spec.nodeName=$node -o jsonpath='{.items[0].metadata.name}' 2>/dev/null")
  [[ -n $pod ]] || die "ovs-ovn pod not found on node $node"
  on "$KUBE_HOST" "KUBECONFIG=$KUBECONFIG_PATH $KUBECTL -n $OVN_NS exec $pod -c openvswitch -- ovs-vsctl $*"
}
# the local OVS interface name backing a logical port on a node.
ovs_iface_of_lsp() { ovs_on "$1" "--bare --columns=name find interface external_ids:iface-id=$2" 2>/dev/null | tr -d '\r' | head -1; }
# the OVN logical port a pod's veth CURRENTLY presents. Normally <pod>.<ns>, but
# after a prior multi-chassis hop the veth was surgically re-pointed at the port it
# inherited — so resolve it from the live interface, not the pod name. Enables
# chained (multi-hop) follow-me migrations.
ovs_lsp_of_pod() {
  local node=$1 pod=$2 name id
  name=$(ovs_on "$node" "--bare --columns=name find interface external_ids:pod_name=$pod external_ids:pod_namespace=$NAMESPACE" 2>/dev/null | tr -d '\r' | head -1) || true
  [[ -n $name ]] || { echo "$(lsp_of "$pod")"; return 0; }
  # ovs-vsctl `get` takes <table> <record> <column>:<key> (no --columns).
  id=$(ovs_on "$node" "get interface $name external_ids:iface-id" 2>/dev/null | tr -d '\r"' | head -1) || true
  echo "${id:-$(lsp_of "$pod")}"
}

# --ovn-ip (intra-cluster overlay follow) vs --l2-ip (cross-cluster macvlan) are
# alternative IP-follow strategies; --ovn-ip only makes sense for socket survival,
# so it implies --tcp. Compute (post-copy) and network-follow are orthogonal, but
# post-copy needs the SOURCE kept alive to serve pages — so it composes with
# multi-chassis (which keeps the source) but NOT single-chassis re-point (fences it).
if [[ -n $OVN_IP ]]; then
  [[ -z $L2_IP ]] || die "--ovn-ip (intra-cluster overlay) and --l2-ip (cross-cluster macvlan) are mutually exclusive"
  [[ $POSTCOPY == 0 || $MC == 1 ]] || die "--ovn-ip + --post-copy requires --ovn-multichassis (post-copy keeps the source alive to serve pages; single-chassis re-point fences it)"
  [[ $TCP == 1 ]] || { log "note: enabling --tcp (--ovn-ip carries the IP for established-socket survival)"; TCP=1; }
fi
# --l2-ip (cross-cluster macvlan follow) is likewise for session survival, so it
# implies --tcp — otherwise a workload with an established socket fails to checkpoint.
if [[ -n $L2_IP && $TCP == 0 && $BULK == 0 ]]; then
  log "note: enabling --tcp (--l2-ip carries the IP for established-socket survival)"; TCP=1
fi
# A bulk hop (vehicle <-> edge) changes the address the workload's sockets are
# bound to, so no established TCP connection can survive it. Close them instead
# of refusing the dump: a single /health poll in flight failed the checkpoint
# (2026-10-20). runc takes CRIU options only from /etc/criu/runc.conf.
if [[ $BULK == 1 ]]; then
  [[ $TCP == 0 ]] || { log "note: --tcp ignored under --bulk-stream (sockets are closed, not carried)"; TCP=0; }
  for h in "$SRC_HOST" "$DST_HOST"; do
    on "$h" "grep -qx tcp-close /etc/criu/runc.conf 2>/dev/null || echo tcp-close >> /etc/criu/runc.conf" \
      || die "could not enable tcp-close in /etc/criu/runc.conf on $h"
  done
fi
[[ $MC == 0 || -n $OVN_IP ]] || die "--ovn-multichassis requires --ovn-ip"
TCPFLAG=""; [[ $TCP == 1 ]] && TCPFLAG="--tcp-established"
# Runtime: runc for --post-copy (has native --lazy-pages), crun-drift otherwise.
if [[ $POSTCOPY == 1 ]]; then RT=$RUNC; RT_ROOT=$RUNC_ROOT; else RT=$CRUN; RT_ROOT=$CRUN_ROOT; fi
# Address the source page-server binds to / the target dials (strip any user@).
SRC_ADDR=${SRC_HOST##*@}
# Host to run kubectl/ovn-nbctl on (see KUBE_HOST note above). Default: source.
KUBE_HOST=${DRIFT_KUBE_HOST:-$SRC_HOST}
# Randomize the page-server port per run (unless pinned) — see LAZY_PORT above.
[[ -n $LAZY_PORT ]] || LAZY_PORT=$((20000 + RANDOM % 20000))
# Post-copy is antagonistic to pre-copy: pre-dumped pages are written eagerly and
# get restored eagerly (defeating lazy). Under --post-copy we drop pre-copy so the
# freeze stays memory-independent.
if [[ $POSTCOPY == 1 && ${PRECOPY:-0} -gt 0 ]]; then
  log "note: --pre-copy is ignored under --post-copy (pre-dumped pages restore eagerly, defeating lazy)"
  PRECOPY=0
fi
# Post-copy keeps the source frozen and SERVING pages until they drain — fencing it
# mid-drain would break the lazy restore. So post-copy always keeps the source.
[[ $POSTCOPY == 0 ]] || KEEP_SOURCE=1
[[ $BULK == 0 || $POSTCOPY == 0 ]] || die "--bulk-stream is for blocking migrations; drop --post-copy"
ID="${SRC_POD}-$(date +%s)"
CKDIR="$STORE/$ID"

log "migrating $NAMESPACE/$SRC_POD:$CONTAINER ($SRC_HOST) -> $NAMESPACE/$DST_POD:$CONTAINER ($DST_HOST)"
log "store=$CKDIR pre-copy=$PRECOPY tcp=$TCP post-copy=$POSTCOPY runtime=$(basename "$RT")"

SRCCID=$(cid_of "$SRC_HOST" "$SRC_POD"); [[ -n $SRCCID ]] || die "source container not found on $SRC_HOST"
DSTCID=$(cid_of "$DST_HOST" "$DST_POD"); [[ -n $DSTCID ]] || \
  die "target container not found on $DST_HOST — pre-create the target pod (holder + $CONTAINER placeholder) first"
DSTBUNDLE=/run/containers/storage/overlay-containers/$DSTCID/userdata
SRCBUNDLE=/run/containers/storage/overlay-containers/$SRCCID/userdata  # rollback target
log "src cid=$SRCCID  dst cid=$DSTCID"

on "$SRC_HOST" "mkdir -p $CKDIR"

# ── PRE-COPY: iterative pre-dump while the workload keeps running ────────────
parent=""
if [[ $PRECOPY -gt 0 ]]; then
  for ((i=1; i<=PRECOPY; i++)); do
    log "pre-dump $i/$PRECOPY (container stays running)"
    on "$SRC_HOST" "$RT --root $RT_ROOT checkpoint --pre-dump \
        --image-path=$CKDIR/pd$i --work-path=$CKDIR/pd$i $parent $SRCCID"
    parent="--parent-path=../pd$i"
  done
fi

# ── COMPUTE plug (orthogonal to the network-follow plug) ─────────────────────
# Two compute modes share one interface so any network-follow (plain / multi-
# chassis / L2) can drive either. POST-COPY keeps the source frozen and serving
# pages (memory-independent restore); BLOCKING writes the full dump. Both are
# expressed as: compute_freeze_src (source dump, ready to serve) + restore_cmd
# (a target-side command STRING, so it can be BUNDLED with the network claim in
# one round-trip).

# Source-side freeze/dump + cross-host cgroup normalization. Returns once the
# source is frozen and (post-copy) the lazy page-server is ready to serve.
# $1 = optional source-side command bundled into the SAME round-trip after the dump
# (e.g. the network plug's "release the IP") — keeps the blocking freeze at one ssh.
# --bulk-stream: ship $CKDIR (on the source's local --store) to the same path on
# the target as ONE stream. --bulk-dial says which end opens the connection:
# the vehicle's, so the data enters the WireGuard hub through the vehicle's own
# tunnel instead of also crossing the edge's (Orin -> Nano measured 18 Mbit/s
# pulled by the Nano, 12 pushed by the Orin). zstd level DRIFT_BULK_ZSTD (15):
# compression runs while the stream is sent, so any level faster than the link
# is free; -15 is ~12 MB/s on the Nano against a 2.5-4.4 MB/s 5G link and ships
# 14% fewer bytes than -1.
BULK_ZSTD=${DRIFT_BULK_ZSTD:-15}
# Delta (DRIFT_BULK_DELTA=1, default): both nodes keep the last image of this
# workload they sent or received ($STORE/.base-<pod> names it). When both name
# the same image, of the same size, the new image is compressed AGAINST it
# (zstd --patch-from): the same process, model and memory layout, so ~2-3 MiB
# cross the link instead of ~20 MiB (measured 2026-10-20, 64 MiB images). zstd's
# frame checksum fails the restore if the two bases ever differ.
BULK_DELTA=${DRIFT_BULK_DELTA:-1} BULK_DELTA_ZSTD=${DRIFT_BULK_DELTA_ZSTD:-3}
# Page delta (DRIFT_BULK_PAGEDELTA=1, opt-in; needs the tool on both nodes):
# instead of tar | zstd --patch-from=<whole base> after the dump (~0.5 s of the
# Nano's CPU in the freeze), drift-pagedelta compares the new image with the
# base page by page and compresses only the changed pages against their old
# versions. Both ends load the base and open the connection BEFORE the freeze;
# the freeze then pays only for compare + compress + reassemble (~0.3 s).
# Measured 2026-10-22 (4 hops): no faster than --patch-from (Orin -> Nano 1.35/
# 1.40 s vs 1.17/1.28; Nano -> Orin 2.11/2.13 vs 2.04): zstd in C is quick once
# YOLO is frozen, and the Python decoder on the Nano costs ~0.15 s. Kept opt-in.
BULK_PAGEDELTA=${DRIFT_BULK_PAGEDELTA:-0} PAGEDELTA=${DRIFT_PAGEDELTA:-/usr/local/bin/drift-pagedelta} BULK_PD=0
BASE_PTR="$STORE/.base-$SRC_POD"
bulk_base() {   # "<id> <size of its pages-1.img>" on host $1, or nothing
  on "$1" "b=\$(cat $BASE_PTR 2>/dev/null) && [ -n \"\$b\" ] && s=\$(stat -c %s $STORE/\$b/final/pages-1.img 2>/dev/null) && echo \"\$b \$s\"" 2>/dev/null || true
}
# The freeze-critical path makes ONE agent round-trip per side (measured
# 2026-10-21: CRIU dump 0.29 s + restore 0.25 s, the rest of a ~3-4 s freeze
# was a dozen sequential agent calls over the vehicle's 5G tunnel plus polling):
#   bulk_prepare    before the freeze: delta base, target dir, push listener
#   bulk_src_cmd    appended to the dump call: encode, then push or serve
#   bulk_dst_cmd    the target call: receive, unpack, restore, wait for CRIU
#   bulk_report     after the restore: the sizes and transfer time, for the log
# Copy base image $3 from host $1 to host $2 and make it $2's base, BEFORE the
# freeze. The stores are tmpfs, so a reboot of one node drops its base; without
# this the next hop ships the full image inside the freeze (2026-10-31, after an
# Orin reboot: 52.7 MiB, 8.9 s freeze, 107 frames). Staged first, the full image
# crosses the link while the workload still runs, and the hop itself stays a
# delta. Best-effort: on any failure the hop falls back to the full image.
bulk_stage_base() {
  local from=$1 to=$2 id=$3 port=$((40000 + RANDOM % 10000)) t0 t1
  [[ -n $id && -n $STORE && $STORE != / ]] || return 1
  t0=$(date +%s.%N)
  log "bulk: no common base, staging $id from $from to $to before the freeze (the workload keeps running)"
  on "$to" "mkdir -p $STORE && rm -rf $STORE/$id $STORE/$id.staged && setsid sh -c 'timeout 180 nc -l $port | zstd -dcq | tar -C $STORE -xf - && touch $STORE/$id.staged' </dev/null >/dev/null 2>&1 &" \
    || { log "WARN bulk: could not start the base receiver on $to"; return 1; }
  for _ in $(seq 1 50); do on "$to" "ss -ltn | grep -q ':$port '" && break; sleep 0.1; done
  on "$from" "tar -C $STORE -cf - $id | zstd -3 -T0 -q | timeout 170 nc -N ${to##*@} $port" \
    || { log "WARN bulk: sending the base from $from failed"; return 1; }
  for _ in $(seq 1 100); do on "$to" "test -e $STORE/$id.staged" && break; sleep 0.1; done
  on "$to" "test -e $STORE/$id.staged && rm -f $STORE/$id.staged && echo $id > $BASE_PTR" \
    || { log "WARN bulk: the base did not arrive on $to"; return 1; }
  t1=$(date +%s.%N)
  log "bulk: base $id staged on $to in $(awk "BEGIN{printf \"%.1f\", $t1-$t0}") s"
}

bulk_prepare() {
  local bs="" bd=""
  BULK_PORT=$((40000 + RANDOM % 10000)) BULK_REF="" BULK_HOW="full, zstd -$BULK_ZSTD"
  if [[ $BULK_DELTA == 1 ]]; then
    bs=$(bulk_base "$SRC_HOST"); bd=$(bulk_base "$DST_HOST")
    if [[ -n $bs && $bs != "$bd" ]]; then          # the target lacks the source's base
      bulk_stage_base "$SRC_HOST" "$DST_HOST" "${bs%% *}" && bd=$(bulk_base "$DST_HOST")
    elif [[ -z $bs && -n $bd ]]; then              # the source lacks the target's base
      bulk_stage_base "$DST_HOST" "$SRC_HOST" "${bd%% *}" && bs=$(bulk_base "$SRC_HOST")
    fi
    if [[ -n $bs && $bs == "$bd" ]]; then
      BULK_REF="$STORE/${bs%% *}/final/pages-1.img"
      BULK_HOW="delta vs ${bs%% *}, zstd -$BULK_DELTA_ZSTD"
    else
      log "bulk: no common base on both nodes (src '${bs:-none}', dst '${bd:-none}'), sending the full image"
    fi
  fi
  if [[ -n $BULK_REF && $BULK_PAGEDELTA == 1 ]] \
     && on "$SRC_HOST" "test -x $PAGEDELTA" && on "$DST_HOST" "test -x $PAGEDELTA"; then
    BULK_PD=1 BULK_HOW="page delta vs ${bs%% *}"
  fi
  if [[ $BULK_PD == 1 ]]; then
    bulk_prepare_pd; return
  fi
  if [[ -n $BULK_REF ]]; then
    BULK_UNPACK="zstd -dcq --long=27 --patch-from=$BULK_REF | tar -C $STORE -xf - && touch $CKDIR.recv"
  else
    BULK_UNPACK="zstd -dcq | tar -C $STORE -xf - && touch $CKDIR.recv"
  fi
  on "$DST_HOST" "mkdir -p $STORE && rm -rf $CKDIR $CKDIR.recv $CKDIR.xfer" || die "bulk: could not prepare $STORE on $DST_HOST"
  if [[ $BULK_DIAL != dst ]]; then   # push: the target listens from now on
    on "$DST_HOST" "setsid sh -c 'nc -l $BULK_PORT | $BULK_UNPACK' </dev/null >/dev/null 2>&1 &" \
      || die "bulk: could not start the receiver on $DST_HOST"
    for _ in $(seq 1 50); do on "$DST_HOST" "ss -ltn | grep -q ':$BULK_PORT '" && break; sleep 0.1; done
  fi
}

# At the freeze, point the input at the target already (in the background, off
# the critical path): what is sent during the freeze is lost either way, and
# the restored workload then has input the moment it runs, without waiting for
# the follow call. The follow-cmd after the restore only confirms it, and a
# rollback's resume-cmd still restores the old target.
follow_aim() {
  [[ -n $FOLLOW_HOST && -n $FOLLOW_AIM_CMD ]] || return 0
  on "$FOLLOW_HOST" "$FOLLOW_AIM_CMD" >/dev/null 2>&1 &
  AIM_PID=$!
}

# Delete the target placeholder container at the START of the freeze, in parallel
# with the source dump, instead of after the image has arrived: runc delete costs
# ~0.12 s on the Jetsons, all of it on the critical path (measured 2026-10-23).
# The restore waits for $CKDIR.pdel and falls back to deleting it itself.
PREDELETE=${DRIFT_PREDELETE:-1} PDEL_PID=""
predelete_placeholder() {
  [[ $PREDELETE == 1 && $BULK == 1 && $POSTCOPY != 1 ]] || return 0
  on "$DST_HOST" "echo \"pdel-start \$(date +%s.%N)\" > $CKDIR.pd; $RT --root $RT_ROOT delete --force $DSTCID 2>/dev/null; echo \"placeholder-gone \$(date +%s.%N)\" >> $CKDIR.pd; touch $CKDIR.pdel" >/dev/null 2>&1 &
  PDEL_PID=$!
}

# Page-delta ends, started before the freeze. The decoder (target) and the
# encoder (source) load the base now; the source then waits for $CKDIR.go,
# which the dump call creates. Whichever end listens starts first.
bulk_prepare_pd() {
  local base=${BULK_REF%/*} env="DRIFT_PAGEDELTA_TMP=$STORE"
  local dec="zstd -dcq | $env $PAGEDELTA decode $base $CKDIR/final && touch $CKDIR.recv"
  local enc="$env $PAGEDELTA encode $base $CKDIR/final $CKDIR.go | zstd -1 -T0 -q | tee $CKDIR.zst"
  local done_="x1=\$(date +%s.%N); echo \"\$(cat $CKDIR.x0 2>/dev/null) \$x1\" > $CKDIR.xfer; echo \"sent \$x1\" >> $CKDIR.t; stat -c %s $CKDIR.zst > $CKDIR.wire 2>/dev/null"
  on "$DST_HOST" "mkdir -p $STORE && rm -rf $CKDIR $CKDIR.recv $CKDIR.xfer" || die "bulk: could not prepare $STORE on $DST_HOST"
  on "$SRC_HOST" "rm -f $CKDIR.go $CKDIR.x0 $CKDIR.xfer $CKDIR.zst" || true
  if [[ $BULK_DIAL == dst ]]; then   # pull: the source serves, the target dials (until it connects)
    on "$SRC_HOST" "setsid sh -c '$enc | nc -N -l $BULK_PORT; $done_' </dev/null >/dev/null 2>&1 &" \
      || die "bulk: could not start the page-delta encoder on $SRC_HOST"
    for _ in $(seq 1 50); do on "$SRC_HOST" "ss -ltn | grep -q ':$BULK_PORT '" && break; sleep 0.1; done
    on "$DST_HOST" "setsid sh -c '(until nc -d $SRC_ADDR $BULK_PORT; do sleep 0.05; done) | $dec' </dev/null >/dev/null 2>&1 &" \
      || die "bulk: could not start the page-delta decoder on $DST_HOST"
  else                               # push: the target listens, the source connects
    on "$DST_HOST" "setsid sh -c 'nc -l $BULK_PORT | $dec' </dev/null >/dev/null 2>&1 &" \
      || die "bulk: could not start the page-delta decoder on $DST_HOST"
    for _ in $(seq 1 50); do on "$DST_HOST" "ss -ltn | grep -q ':$BULK_PORT '" && break; sleep 0.1; done
    on "$SRC_HOST" "setsid sh -c '$enc | nc -N ${DST_HOST##*@} $BULK_PORT; $done_' </dev/null >/dev/null 2>&1 &" \
      || die "bulk: could not start the page-delta encoder on $SRC_HOST"
  fi
}

# drop page-delta ends still waiting (a migration that died before or during the freeze)
bulk_pd_cleanup() {
  [[ ${BULK_PD:-0} == 1 ]] || return 0
  local h
  for h in "$SRC_HOST" "$DST_HOST"; do
    on "$h" "pkill -f '[d]rift-pagedelta (en|de)code .*$ID' ; pkill -f 'nc .*[ ]$BULK_PORT\$' ; true" >/dev/null 2>&1 || true
  done
}

bulk_src_cmd() {   # runs on the source right after the dump, in the same call
  local pack after
  printf '%s\n' "echo \"dumped \$(date +%s.%N)\" > $CKDIR.t" "du -sb $CKDIR | cut -f1 > $CKDIR.bytes"
  if [[ $BULK_PD == 1 ]]; then       # the encoder is already waiting: start it
    printf '%s\n' "date +%s.%N > $CKDIR.x0; touch $CKDIR.go"; return
  fi
  if [[ -n $BULK_REF ]]; then
    # --patch-from needs the INPUT size, so the image is tarred to a file; the
    # compressed output streams into the connection as zstd makes it (tee keeps
    # a copy only to report its size), so encoding and sending overlap.
    printf '%s\n' "tar -C $STORE -cf $CKDIR.tar $ID"
    pack="zstd -$BULK_DELTA_ZSTD -T0 -q --long=27 --patch-from=$BULK_REF -c $CKDIR.tar | tee $CKDIR.zst"
  else
    pack="tar -C $STORE -cf - $ID | zstd -$BULK_ZSTD -T0 -q"
  fi
  # the full image has no .zst (only a delta keeps one), and the call runs under set -e
  after="echo \"sent \$(date +%s.%N)\" >> $CKDIR.t; { stat -c %s $CKDIR.zst > $CKDIR.wire 2>/dev/null || true; }; rm -f $CKDIR.tar"
  if [[ $BULK_DIAL == dst ]]; then   # pull: serve it; the target connects next
    printf '%s\n' "setsid sh -c '$pack | nc -N -l $BULK_PORT; $after' </dev/null >/dev/null 2>&1 &"
  else                               # push: send it now, timing the transfer
    printf '%s\n' "x0=\$(date +%s.%N); $pack | nc -N ${DST_HOST##*@} $BULK_PORT; x1=\$(date +%s.%N); echo \"\$x0 \$x1\" > $CKDIR.xfer; $after"
  fi
}

bulk_dst_cmd() {   # the target's one call: receive, unpack, restore, wait for CRIU
  if [[ $BULK_PD == 1 ]]; then       # the decoder runs since the prepare: wait for its image
    printf '%s\n' "for i in \$(seq 1 6000); do test -e $CKDIR.recv && break; sleep 0.005; done; echo \"received \$(date +%s.%N)\" > $CKDIR.t"
  elif [[ $BULK_DIAL == dst ]]; then
    printf '%s\n' "x0=\$(date +%s.%N)" \
      "for i in \$(seq 1 60); do nc -d $SRC_ADDR $BULK_PORT | $BULK_UNPACK && break; sleep 0.05; done" \
      "x1=\$(date +%s.%N); echo \"\$x0 \$x1\" > $CKDIR.xfer; echo \"received \$x1\" > $CKDIR.t"
  else
    printf '%s\n' "for i in \$(seq 1 600); do test -e $CKDIR.recv && break; sleep 0.01; done; echo \"received \$(date +%s.%N)\" > $CKDIR.t"
  fi
  printf '%s\n' "test -e $CKDIR.recv || { echo 'bulk: image never arrived' >&2; exit 7; }"
}

# Appended after a restore in the target's call: return once CRIU reports, and
# confirm there that the task runs, so the caller skips wait_restore's polling
# over the tunnel (it prints DRIFT_RESTORE_OK when both hold). When they do not
# hold yet the call must still succeed, so the caller falls back to wait_restore
# instead of rolling back a restore that is merely slow to report "running"
# (seen 2026-10-31: restore finished on the Orin, the 0.3 s check missed it).
restore_wait_cmd() {
  local running="$RT --root $RT_ROOT state $DSTCID 2>/dev/null | grep -qE '\"status\"[[:space:]]*:[[:space:]]*\"running\"'"
  printf '%s\n' "for i in \$(seq 1 1000); do grep -qi 'Restore finished successfully' $CKDIR/final/restore.log 2>/dev/null && break; grep -q 'Error (' $CKDIR/final/restore.log 2>/dev/null && break; sleep 0.01; done" \
    "for i in \$(seq 1 30); do $running && break; sleep 0.01; done" \
    "echo \"restored \$(date +%s.%N)\" >> $CKDIR.t" \
    "echo \"criu-start \$(stat -c %.W $CKDIR/final/restore.log)\" >> $CKDIR.t; echo \"criu-end \$(stat -c %.Y $CKDIR/final/restore.log)\" >> $CKDIR.t" \
    "grep -qi 'Restore finished successfully' $CKDIR/final/restore.log && $running && echo DRIFT_RESTORE_OK || true"
}

bulk_report() {   # off the hot path
  local bytes wire x
  bytes=$(on "$SRC_HOST" "cat $CKDIR.bytes" 2>/dev/null)
  wire=$(on "$SRC_HOST" "cat $CKDIR.wire 2>/dev/null" 2>/dev/null || true)
  if [[ $BULK_DIAL == dst && $BULK_PD != 1 ]]; then x=$(on "$DST_HOST" "cat $CKDIR.xfer" 2>/dev/null); else x=$(on "$SRC_HOST" "cat $CKDIR.xfer" 2>/dev/null); fi
  set -- $x
  [[ -n ${1:-} && -n ${2:-} && -n $bytes ]] || { log "bulk ($BULK_DIAL dials, $BULK_HOW): transfer timing unavailable"; return; }
  local st dt
  st=$(on "$SRC_HOST" "cat $CKDIR.t0 $CKDIR.t 2>/dev/null" 2>/dev/null | tr '\n' ' '); dt=$(on "$DST_HOST" "cat $CKDIR.t" 2>/dev/null | tr '\n' ' ')
  log "timeline from freeze start: $(echo "$st $dt" | awk -v f0="$FREEZE_START" '{for (i=1;i<NF;i+=2) printf "%s +%.2fs  ", $i, $(i+1)-f0}')"
  log "bulk ($BULK_DIAL dials, $BULK_HOW): $(awk "BEGIN{printf \"%.1f MiB of image in %.2fs (~%.1f Mbit/s of image)\", $bytes/1048576, $2-$1, $bytes*8/1e6/($2-$1)}")${wire:+, $(awk "BEGIN{printf \"%.2f MiB on the wire\", $wire/1048576}")} $SRC_HOST -> $DST_HOST"
}

# After a successful bulk hop both nodes hold this image: it becomes the base of
# the next hop, and older images of the workload go (off the hot path).
bulk_rebase() {
  local h
  for h in "$SRC_HOST" "$DST_HOST"; do
    on "$h" "echo $ID > $BASE_PTR && cd $STORE && for d in $SRC_POD-*; do [ \"\$d\" = $ID ] || rm -rf \"\$d\"; done" >/dev/null 2>&1 \
      || log "WARN bulk: could not update the base on $h"
  done
}

# True once CRIU has dumped (and so stopped) the source. A step after the dump
# can still fail (the bulk push to the target, the network plug), and then the
# workload must be restored back onto the source, not left dead on both ends.
src_dumped() {
  on "$SRC_HOST" "grep -qi 'Dumping finished successfully' $CKDIR/final/dump.log" 2>/dev/null
}

compute_freeze_src() {
  local extra="${1:-true}"
  local strip="true"
  [[ $SRC_HOST == "$DST_HOST" || $CG_MODE == ignore ]] || strip="{ test -x $CGROUP_STRIP && $CGROUP_STRIP $CKDIR/final/cgroup.img >/dev/null 2>&1 || true; }"
  if [[ $POSTCOPY == 1 ]]; then
    on "$SRC_HOST" "mkdir -p $CKDIR/final; rm -f $CKDIR/final/lazy-ready
setsid sh -c 'exec 3>$CKDIR/final/lazy-ready; exec $RT --root $RT_ROOT checkpoint --lazy-pages --page-server $SRC_ADDR:$LAZY_PORT --status-fd 3 --image-path=$CKDIR/final --work-path=$CKDIR/final $TCPFLAG $SRCCID' </dev/null >$CKDIR/final/ckpt.out 2>&1 &"
    local ready=0
    for _ in $(seq 1 120); do
      if on "$SRC_HOST" "test -s $CKDIR/final/lazy-ready"; then ready=1; break; fi
      if on "$SRC_HOST" "grep -q 'Error (' $CKDIR/final/dump.log 2>/dev/null || grep -qiE 'error|failed|no such|not found' $CKDIR/final/ckpt.out 2>/dev/null"; then break; fi
      sleep 0.1
    done
    [[ $ready == 1 ]] || die "lazy page-server did not become ready — see $CKDIR/final/{ckpt.out,dump.log} on $SRC_HOST"
    on "$SRC_HOST" "$strip; $extra"
  else
    # one round-trip: checkpoint + verify + cgroup-strip + the network plug's extra.
    on "$SRC_HOST" "set -e
echo \"ckpt-start \$(date +%s.%N)\" > $CKDIR.t0
$RT --root $RT_ROOT checkpoint --image-path=$CKDIR/final --work-path=$CKDIR/final $parent $TCPFLAG $SRCCID
echo \"ckpt-done \$(date +%s.%N)\" >> $CKDIR.t0
grep -qi 'Dumping finished successfully' $CKDIR/final/dump.log
$strip
$extra" || { src_dumped && ROLLBACK_ARMED=1; die "checkpoint/freeze failed — see $CKDIR/final/dump.log on $SRC_HOST"; }
  fi
  # Source is now stopped (blocking) or frozen+serving (post-copy) and its network
  # has been released — from here until the target is live, any failure rolls back.
  ROLLBACK_ARMED=1
}

# Restore as a command STRING (post-copy aware). Emit-only so callers can bundle
# it with the network claim; do_restore runs it standalone. Defaults to the
# TARGET container/bundle; rollback() passes the SOURCE cid+bundle to restore the
# workload back where it started (the same image + page-server serve either end).
restore_cmd() {
  # $3 (work-path) matters: the ROLLBACK restore must not share the target
  # restore's work dir, or its restore.log overwrites the target's failure —
  # destroying the only evidence of why the migration failed.
  local cid=${1:-$DSTCID} bundle=${2:-$DSTBUNDLE} wp=${3:-$CKDIR/final}
  # test hook: suppress ONLY the target restore (no $1) so wait_restore times out
  # and the rollback path (which passes an explicit cid) can be exercised e2e.
  if [[ ${DRIFT_TEST_FAIL_RESTORE:-0} == 1 && -z ${1:-} ]]; then
    printf '%s\n' "true  # DRIFT_TEST_FAIL_RESTORE: target restore suppressed"; return
  fi
  # =2: the target restore fails at once with a CRIU-style error (KPI-4 harness):
  # the waits see "Error (" and roll back without sitting out their timeouts.
  if [[ ${DRIFT_TEST_FAIL_RESTORE:-0} == 2 && -z ${1:-} ]]; then
    printf '%s\n' "mkdir -p $wp; echo 'Error (DRIFT_TEST_FAIL_RESTORE): injected target restore failure' > $wp/restore.log"; return
  fi
  # The restored process inherits runc's stdio, so it must be an ANONYMOUS PIPE —
  # exactly what conmon gives a normal CRI-O container, and what CRIU dumps natively.
  # A host file / host /dev/null instead leaves the workload holding an fd whose
  # mount isn't in the container ns, so the NEXT hop's checkpoint fails ("Can't
  # lookup mount for fd=1"). Pipe stdout+stderr to a discarding `cat`; the whole
  # thing is `setsid …&` + fully redirected so the ssh call returns immediately
  # (the drainer must not keep the ssh stdout pipe open). Diagnostics: restore.log.
  if [[ -z ${1:-} && $PREDELETE == 1 && $BULK == 1 && $POSTCOPY != 1 ]]; then
    printf '%s\n' "for i in \$(seq 1 100); do test -e $CKDIR.pdel && break; sleep 0.005; done" \
      "test -e $CKDIR.pdel && cat $CKDIR.pd >> $CKDIR.t || { $RT --root $RT_ROOT delete --force $cid 2>/dev/null; echo \"placeholder-gone \$(date +%s.%N)\" >> $CKDIR.t; }"
  else
    printf '%s\n' "$RT --root $RT_ROOT delete --force $cid 2>/dev/null || true" "echo \"placeholder-gone \$(date +%s.%N)\" >> $CKDIR.t"
  fi
  if [[ $POSTCOPY == 1 ]]; then
    # criu lazy-pages dials the source page-server ($SRC_ADDR:$LAZY_PORT), injecting
    # pages over UFFD on demand; runc restore --lazy-pages resumes on metadata alone.
    printf '%s\n' "setsid criu lazy-pages --page-server --address $SRC_ADDR --port $LAZY_PORT -D $CKDIR/final -o lazy.log >/dev/null 2>&1 & sleep 0.3" \
      "setsid sh -c '$RT --root $RT_ROOT restore --image-path=$CKDIR/final --work-path=$wp --bundle=$bundle $TCPFLAG $CGFLAG --lazy-pages -d $cid 2>&1 | cat >/dev/null' </dev/null >/dev/null 2>&1 &"
  else
    printf '%s\n' "setsid sh -c '$RT --root $RT_ROOT restore --image-path=$CKDIR/final --work-path=$wp --bundle=$bundle $TCPFLAG $CGFLAG $cid 2>&1 | cat >/dev/null' </dev/null >/dev/null 2>&1 &"
  fi
}
do_restore() { on "$DST_HOST" "$(restore_cmd)"; }

# ── O-6: restore the workload BACK onto the source after a failed migration ───
# Fires from die() only while armed (source stopped, target not yet live). The
# checkpoint image is on the shared store and (post-copy) the source page-server
# is still up, so restoring into the source bundle reconstitutes the workload
# where it started; then re-assert its network identity so sessions can resume.
rollback() {
  [[ $ROLLBACK_DONE == 1 ]] && return; ROLLBACK_DONE=1
  ROLLBACK_ARMED=0   # rollback's own restore must not re-enter on a nested die
  # 1. re-assert the source's network identity (best-effort; branch-specific)
  [[ -n $ROLLBACK_NET_CMD ]] && on "$SRC_HOST" "$ROLLBACK_NET_CMD" >/dev/null 2>&1
  # 1b. multi-chassis: drop the target's additional binding, source stays primary
  [[ $MC == 1 && -n ${SRC_LSP:-} && -n ${SRC_CH:-} ]] && \
    nbctl "set logical_switch_port $SRC_LSP options:requested-chassis=\"$SRC_CH\"" >/dev/null 2>&1 || true

  if [[ $POSTCOPY == 1 ]]; then
    # Post-copy keeps the source task FROZEN-ALIVE serving its pages (KEEP_SOURCE),
    # so its original socket is still bound — restoring the same workload back onto
    # the source would collide on that 4-tuple (EADDRNOTAVAIL), and this runtime
    # can't resume a lazy-checkpoint in place. So we do NOT destroy the frozen
    # source or its page-server: the workload's full state is preserved on the
    # store + in the live page-server for deferred recovery, rather than lost.
    log "ROLLBACK (post-copy): target failed; source kept FROZEN + image preserved at $CKDIR/final on $SRC_HOST"
    log "ROLLBACK: recover with a blocking restore of $CKDIR/final into $SRC_POD once the fault clears"
    return
  fi

  # Blocking: the source was checkpointed AND stopped, so its socket is free — the
  # image is a complete self-contained dump. Restore it back into the source
  # container; the established connection reappears (proven TCP-preserving).
  log "ROLLBACK: migration failed after freeze — restoring $SRC_POD:$CONTAINER on $SRC_HOST"
  on "$SRC_HOST" "mkdir -p $CKDIR/rollback; $(restore_cmd "$SRCCID" "$SRCBUNDLE" "$CKDIR/rollback")" >/dev/null 2>&1
  local ok=0
  for _ in $(seq 1 60); do
    if on "$SRC_HOST" "grep -qi 'Restore finished successfully' $CKDIR/rollback/restore.log 2>/dev/null"; then ok=1; break; fi
    sleep 0.1
  done
  if [[ $ok == 1 ]]; then
    log "ROLLBACK OK: $SRC_POD restored and serving on $SRC_HOST (source never left)"
  else
    log "ROLLBACK INCOMPLETE: source restore not confirmed — see $CKDIR/final/restore.log on $SRC_HOST"
  fi
}
wait_restore() {
  # The wait must scale with the image, not a fixed guess: a 400 MiB-RSS
  # workload's dump+restore start can lag the freeze by minutes on a slow
  # store, and every too-eager rollback we shipped later raced a restore that
  # then ALSO succeeded. So: generous cap (10 min), but fail FAST when the
  # restore has produced a real error — rolling back early is only right when
  # the restore is provably dead, and the source stays frozen either way.
  local ok=0
  for _ in $(seq 1 6000); do
    if on "$DST_HOST" "grep -qi 'Restore finished successfully' $CKDIR/final/restore.log 2>/dev/null"; then ok=1; break; fi
    if on "$DST_HOST" "grep -q 'Error (' $CKDIR/final/restore.log 2>/dev/null || grep -qiE 'error|failed' $CKDIR/restore.out 2>/dev/null"; then
      break   # restore reported a genuine error — roll back now
    fi
    sleep 0.1
  done
  [[ $ok == 1 ]] || die "restore did not report success — see $CKDIR/final/restore.log on $DST_HOST"
  # CRIU can log "Restore finished successfully" and the task STILL die immediately
  # after — classically post-copy faulting on a page-server it can't reach across a
  # cluster boundary. Confirm the restored task is actually RUNNING before we call
  # the migration a success, so the backend never reports a workload that is gone.
  local live=0
  for _ in $(seq 1 30); do
    if on "$DST_HOST" "$RT --root $RT_ROOT state $DSTCID 2>/dev/null | grep -qE '\"status\"[[:space:]]*:[[:space:]]*\"running\"'"; then live=1; break; fi
    sleep 0.1
  done
  [[ $live == 1 ]] || die "restore reported success but $DST_POD:$CONTAINER is NOT running on $DST_HOST — the task died right after restore (post-copy page channel unreachable across clusters?). See $CKDIR/final/{restore.log,lazy.log} on $DST_HOST"
}

# ── NETWORK ACCOUNTING (off hot path) ────────────────────────────────────────
# Approximate bytes-on-the-wire per migration, from the DST host's rx counter on
# its interface toward the source. That one counter sees every migration byte in
# our topology: the checkpoint image traverses the network exactly once (NFS
# write into the store OR restore read out of it — whichever end isn't the NFS
# server), and post-copy lazy pages always stream src->dst. Host-wide counters,
# so concurrent traffic pollutes the number — fine on a quiet testbed, and
# disable with DRIFT_NET_STATS=0 (post-copy waits for the page drain to finish
# so it can report the drain, which delays "migration complete" by the drain).
NET_STATS=${DRIFT_NET_STATS:-1}
NET_IFACE=""
net_rx() { on "$DST_HOST" "cat /sys/class/net/$NET_IFACE/statistics/rx_bytes" 2>/dev/null || echo 0; }
if [[ $NET_STATS == 1 && $SRC_HOST != "$DST_HOST" ]]; then
  NET_IFACE=$(on "$DST_HOST" "ip route get $SRC_ADDR 2>/dev/null | grep -oE 'dev [^ ]+' | head -1 | cut -d' ' -f2" || true)
  [[ $NET_IFACE == lo ]] && NET_IFACE=""
fi
[[ -n $NET_IFACE ]] && NET_RX0=$(net_rx)

# ── FINAL FREEZE + RESTORE ───────────────────────────────────────────────────
[[ $BULK == 1 ]] && bulk_prepare
# L2 follow: resolve both pods' netns now, before the input is paused (two agent
# round-trips, ~0.7 s over the vehicle's tunnel, measured inside the stall).
if [[ -n $L2_IP ]]; then
  SRC_NETNS=$(netns_of "$SRC_HOST" "$SRC_POD"); [[ -n $SRC_NETNS ]] || die "source netns not found on $SRC_HOST"
  DST_NETNS=$(netns_of "$DST_HOST" "$DST_POD"); [[ -n $DST_NETNS ]] || die "target netns not found on $DST_HOST"
fi
if [[ -n $FOLLOW_HOST && -n $QUIESCE_CMD ]]; then
  on "$FOLLOW_HOST" "$QUIESCE_CMD" >/dev/null || die "quiesce on $FOLLOW_HOST failed: $QUIESCE_CMD"
  QUIESCED=1
  log "quiesced input on $FOLLOW_HOST"
  [[ $QUIESCE_S == 0 ]] || sleep "$QUIESCE_S"
fi
FREEZE_START=$(date +%s.%N)
if [[ -n $L2_IP ]]; then
  # Floating-L2 failover, collapsed into TWO ssh round-trips (one bundled script
  # per host) so the client-visible stall is dominated by criu, not orchestration
  # latency. The source owns $L2_IP on a shared-L2 iface ($L2_IFACE, a macvlan);
  # the target is parked on $L2_DUMMY to avoid a dual-active IP that would reset
  # the live connection. netns are resolved up front (off the frozen path).
  IPONLY=${L2_IP%%/*}
  DUMMY_DEL=""; [[ -n $L2_DUMMY ]] && DUMMY_DEL="nsenter --net=$DST_NETNS ip addr del $L2_DUMMY dev $L2_IFACE 2>/dev/null || true"
  # rollback: bring the source's floating IP back and GARP it home
  ROLLBACK_NET_CMD="nsenter --net=$SRC_NETNS ip link set $L2_IFACE up 2>/dev/null || true
nsenter --net=$SRC_NETNS ip addr add $L2_IP dev $L2_IFACE 2>/dev/null || true
nsenter --net=$SRC_NETNS arping -U -c2 -w1 -I $L2_IFACE $IPONLY >/dev/null 2>&1 &"
  log "L2 failover $L2_IFACE=$L2_IP (freeze begins)"
  follow_aim
  predelete_placeholder
  # A (source, one round-trip): COMPUTE freeze/dump (blocking OR post-copy — plug),
  # dropping the floating IP (bring $L2_IFACE down — frees the IP) in the same trip.
  # Over an EVPN-stretched L2 this is the CROSS-cluster follow; on a shared physical
  # L2 it is the local one — identical mechanism.
  compute_freeze_src "nsenter --net=$SRC_NETNS ip link set $L2_IFACE down 2>/dev/null || true
$( [[ $BULK == 1 ]] && bulk_src_cmd )"
  # B (target, one round-trip): [bulk: receive], claim the IP, restore (COMPUTE plug), GARP.
  DST_OUT=$(on "$DST_HOST" "$( [[ $BULK == 1 ]] && bulk_dst_cmd )
nsenter --net=$DST_NETNS ip addr add $L2_IP dev $L2_IFACE 2>/dev/null || true
$DUMMY_DEL
$(restore_cmd)
nsenter --net=$DST_NETNS arping -U -c2 -w1 -I $L2_IFACE $IPONLY >/dev/null 2>&1 &
$( [[ $BULK == 1 ]] && restore_wait_cmd )") || die "receive/restore on $DST_HOST failed"
  [[ $DST_OUT == *DRIFT_RESTORE_OK* ]] || wait_restore
elif [[ -n $OVN_IP && $MC == 1 ]]; then
  # ── ZERO-DOWNTIME intra-cluster follow via OVN MULTI-CHASSIS (kube-ovn's own
  # KubeVirt-live-migration technique). Pre-bind the TARGET node's chassis as an
  # ADDITIONAL binding on the SOURCE's logical port (requested-chassis="src,dst")
  # BEFORE the freeze, so OVN programs the target datapath ahead of time; the
  # cutover is then just a primary flip with no convergence wait. The target veth
  # REUSES the source LSP (iface-id surgery), so the source pod is KEPT as the LSP
  # owner (proper LSP-ownership transfer, as kube-ovn does for VMs, is future work).
  # Resolutions are guarded with `|| true` so a transient/empty result surfaces as a
  # clear die below, not a silent `set -e` exit mid-pipe (grep-no-match etc.).
  IPONLY=${OVN_IP%%/*}
  SRC_NODE=$(node_of_ip "$SRC_HOST" || true); DST_NODE=$(node_of_ip "$DST_HOST" || true)
  [[ -n $SRC_NODE && -n $DST_NODE ]] || die "could not resolve k8s node names for $SRC_HOST/$DST_HOST"
  # SRC_LSP is the port the source veth CURRENTLY presents — normally <pod>.<ns>,
  # but the port it INHERITED if this pod was itself a migration target (multi-hop).
  SRC_LSP=$(ovs_lsp_of_pod "$SRC_NODE" "$SRC_POD" || true); DST_LSP=$(lsp_of "$DST_POD")
  OVN_MAC=$(ovn_mac_of_lsp "$SRC_LSP" || true); [[ -n $OVN_MAC ]] || die "could not read source LSP MAC ($SRC_LSP) from OVN"
  SRC_CH=$(chassis_of_node "$SRC_NODE" || true); DST_CH=$(chassis_of_node "$DST_NODE" || true)
  [[ -n $SRC_CH && -n $DST_CH ]] || die "could not resolve OVN chassis for $SRC_NODE/$DST_NODE"
  SRC_NETNS=$(netns_of "$SRC_HOST" "$SRC_POD" || true); [[ -n $SRC_NETNS ]] || die "source netns not found on $SRC_HOST"
  DST_NETNS=$(netns_of "$DST_HOST" "$DST_POD" || true); [[ -n $DST_NETNS ]] || die "target netns not found on $DST_HOST"
  DST_GW=$(on "$DST_HOST" "nsenter --net=$DST_NETNS ip -o route show default 2>/dev/null | awk '{print \$3; exit}'" || true)
  DST_PLEN=$(on "$DST_HOST" "nsenter --net=$DST_NETNS ip -o -4 addr show dev $OVN_IFACE 2>/dev/null | grep -oE '/[0-9]+' | head -1"); [[ -n $DST_PLEN ]] || DST_PLEN="/16"
  GW_ADD=""; [[ -n $DST_GW ]] && GW_ADD="nsenter --net=$DST_NETNS ip route replace default via $DST_GW dev $OVN_IFACE 2>/dev/null || true"
  # The target's OVS interface can lag pod-Running by a moment (kube-ovn install) —
  # retry so a freshly-created target (e.g. a chained hop) doesn't race the install.
  DST_IFACE=""
  for _ in $(seq 1 30); do DST_IFACE=$(ovs_iface_of_lsp "$DST_NODE" "$DST_LSP" || true); [[ -n $DST_IFACE ]] && break; sleep 0.5; done
  [[ -n $DST_IFACE ]] || die "could not find target OVS interface for $DST_LSP on $DST_NODE (kube-ovn installed?)"
  # PRE-WARM (off hot path, source still serving): reuse the source LSP on the
  # target veth, then bind the source port to both chassis so OVN programs the
  # target datapath ahead of time. Wait until the additional binding appears.
  log "multichassis pre-warm: $DST_IFACE -> $SRC_LSP; requested-chassis=$SRC_NODE,$DST_NODE (off hot path)"
  ovs_on "$DST_NODE" "set interface $DST_IFACE external_ids:iface-id=$SRC_LSP" >/dev/null 2>&1 || die "iface-id surgery failed on $DST_NODE"
  nbctl "set logical_switch_port $SRC_LSP options:requested-chassis=\"$SRC_CH,$DST_CH\"" >/dev/null 2>&1 || die "requested-chassis (multi) set failed"
  WARM=0
  for _ in $(seq 1 80); do
    if sbctl "--bare --columns=additional_chassis find port_binding logical_port=$SRC_LSP" 2>/dev/null | grep -qE '[0-9a-f-]{36}'; then WARM=1; break; fi
    sleep 0.1
  done
  [[ $WARM == 1 ]] || die "target chassis did not bind as additional — see port_binding for $SRC_LSP"
  # ── the hot path proper starts here (pre-warm was free) ──
  FREEZE_START=$(date +%s.%N)
  log "multichassis cutover (freeze begins)"
  # A (source): COMPUTE freeze/dump (blocking OR post-copy page-server — plug),
  # relinquishing $IPONLY on the source side (bundled into the same round-trip) so
  # its node stops delivering the IP locally.
  # rollback: re-add $IPONLY on the source iface (requested-chassis is still
  # "src,dst" until the post-restore promotion, so the source is still primary —
  # rollback() also resets it to "src" to drop the stale additional binding).
  ROLLBACK_NET_CMD="nsenter --net=$SRC_NETNS ip addr add $IPONLY$DST_PLEN dev $OVN_IFACE 2>/dev/null || true"
  compute_freeze_src "nsenter --net=$SRC_NETNS ip addr del $IPONLY$DST_PLEN dev $OVN_IFACE 2>/dev/null || true"
  # B (target): claim $IPONLY/$OVN_MAC on the reused port, then restore (COMPUTE plug),
  # bundled in one round-trip. The target is already bound as ADDITIONAL, so it serves
  # the moment it claims the IP — no primary flip needed on the hot path.
  on "$DST_HOST" "nsenter --net=$DST_NETNS ip addr flush dev $OVN_IFACE 2>/dev/null || true
nsenter --net=$DST_NETNS ip link set $OVN_IFACE down 2>/dev/null || true
nsenter --net=$DST_NETNS ip link set $OVN_IFACE address $OVN_MAC 2>/dev/null || true
nsenter --net=$DST_NETNS ip link set $OVN_IFACE up 2>/dev/null || true
nsenter --net=$DST_NETNS ip addr add $IPONLY$DST_PLEN dev $OVN_IFACE 2>/dev/null || true
$GW_ADD
$(restore_cmd)"
  wait_restore
  FREEZE_END=$(date +%s.%N)   # workload live on the target — end of client-visible downtime
  # OFF the hot path now: promote the target to SOLE primary (drops the source's
  # binding + traffic replication). The target reuses the source LSP, so the source
  # pod owns the identity — never fence it (that would delete the LSP in use).
  nbctl "set logical_switch_port $SRC_LSP options:requested-chassis=\"$DST_CH\"" >/dev/null 2>&1 || log "warn: post-restore primary flip failed (target still serves as additional)"
  KEEP_SOURCE=1
elif [[ -n $OVN_IP ]]; then
  # ── INTRA-cluster primary-IP follow via kube-ovn. Carry the pod's own OVERLAY
  # IP to the target NODE by re-pointing the target's OVN logical switch port to
  # the source's MAC+IP; OVN binds that port to whichever chassis hosts the pod
  # (the target node) and routes $OVN_IP there over geneve — so an established TCP
  # connection survives with no macvlan/GARP/EVPN. Src and dst share ONE cluster.
  # (Proven node084->node089 on a 2-node kube-ovn cluster: a client's single TCP
  # connection carried through with no reset, the port binding moved to the target
  # chassis, and the established socket reappeared on the target node. ~1.9s freeze,
  # dominated by the OVN convergence wait below.)
  IPONLY=${OVN_IP%%/*}
  SRC_LSP=$(lsp_of "$SRC_POD"); DST_LSP=$(lsp_of "$DST_POD")
  OVN_MAC=$(ovn_mac_of "$SRC_POD"); [[ -n $OVN_MAC ]] || die "could not read source LSP MAC ($SRC_LSP) from OVN"
  DST_NETNS=$(netns_of "$DST_HOST" "$DST_POD"); [[ -n $DST_NETNS ]] || die "target netns not found on $DST_HOST"
  # gateway + prefix-len for the target iface (both lost when we flush it) — off hot
  # path. The prefix MUST be preserved: adding $OVN_IP as a bare /32 would drop the
  # connected route to the pod subnet, and CRIU's TCP-established repair (soccr
  # connect-back to the peer) then fails "Network is unreachable".
  DST_GW=$(on "$DST_HOST" "nsenter --net=$DST_NETNS ip -o route show default 2>/dev/null | awk '{print \$3; exit}'")
  DST_PLEN=$(on "$DST_HOST" "nsenter --net=$DST_NETNS ip -o -4 addr show dev $OVN_IFACE 2>/dev/null | grep -oE '/[0-9]+' | head -1")
  [[ -n $DST_PLEN ]] || DST_PLEN="/16"
  GW_ADD=""; [[ -n $DST_GW ]] && GW_ADD="nsenter --net=$DST_NETNS ip route replace default via $DST_GW dev $OVN_IFACE 2>/dev/null || true"
  log "OVN-IP follow $IPONLY (mac $OVN_MAC) $SRC_LSP@$SRC_HOST -> $DST_LSP@$DST_HOST (freeze begins)"
  # A (source): freeze + validate + strip cgroup.
  on "$SRC_HOST" "set -e
$RT --root $RT_ROOT checkpoint --image-path=$CKDIR/final --work-path=$CKDIR/final $parent $TCPFLAG $SRCCID
grep -qi 'Dumping finished successfully' $CKDIR/final/dump.log
{ test -x $CGROUP_STRIP && $CGROUP_STRIP $CKDIR/final/cgroup.img >/dev/null 2>&1 || true; }" \
    || { src_dumped && ROLLBACK_ARMED=1; die "checkpoint/freeze failed on $SRC_HOST — see $CKDIR/final/dump.log"; }
  # Fence the source: frees $OVN_IP in kube-ovn IPAM and removes $SRC_LSP so the
  # address is unique before the target claims it (avoids a dual-active reset).
  fence_source
  # Re-point the target LSP to the source's MAC+IP on the OVN northbound.
  nbctl "lsp-set-addresses $DST_LSP \"$OVN_MAC $IPONLY\"" >/dev/null 2>&1 || die "ovn-nbctl lsp-set-addresses failed for $DST_LSP"
  # B (target): move the primary iface onto $IPONLY$DST_PLEN / $OVN_MAC (OVN's ARP
  # responder now answers $IPONLY with $OVN_MAC, so the iface MAC must match), wait
  # for OVN to bind the re-pointed port to this chassis (the datapath isn't live the
  # instant lsp-set-addresses returns — restore's socket repair needs it reachable),
  # then restore.
  on "$DST_HOST" "nsenter --net=$DST_NETNS ip addr flush dev $OVN_IFACE 2>/dev/null || true
nsenter --net=$DST_NETNS ip link set $OVN_IFACE down 2>/dev/null || true
nsenter --net=$DST_NETNS ip link set $OVN_IFACE address $OVN_MAC 2>/dev/null || true
nsenter --net=$DST_NETNS ip link set $OVN_IFACE up 2>/dev/null || true
nsenter --net=$DST_NETNS ip addr add $IPONLY$DST_PLEN dev $OVN_IFACE 2>/dev/null || true
$GW_ADD
for _ in \$(seq 1 40); do nsenter --net=$DST_NETNS ping -c1 -W1 $DST_GW >/dev/null 2>&1 && break; sleep 0.1; done
$RT --root $RT_ROOT delete --force $DSTCID 2>/dev/null || true
setsid $RT --root $RT_ROOT restore --image-path=$CKDIR/final --work-path=$CKDIR/final --bundle=$DSTBUNDLE $TCPFLAG $CGFLAG $DSTCID >$CKDIR/restore.out 2>&1 &"
  wait_restore
elif [[ $POSTCOPY == 1 ]]; then
  # ── POST-COPY (no network-follow): compute plug does the lazy page-server dump;
  # the target lazy-restores from it. Memory-INDEPENDENT freeze; pages fault in
  # after the workload is live on the target.
  log "post-copy: page-server $SRC_ADDR:$LAZY_PORT, lazy restore into $DST_POD (freeze begins)"
  compute_freeze_src
  do_restore
  wait_restore
else
  # ── plain (no network-follow): blocking dump + restore, both via the compute plug.
  log "final checkpoint (freeze begins)"
  follow_aim
  predelete_placeholder
  if [[ $BULK == 1 ]]; then
    compute_freeze_src "$(bulk_src_cmd)"   # dump, cgroup fix, encode, push/serve
    DST_OUT=$(on "$DST_HOST" "$(bulk_dst_cmd)
$(restore_cmd)
$(restore_wait_cmd)") || die "bulk receive/restore on $DST_HOST failed"
    [[ $DST_OUT == *DRIFT_RESTORE_OK* ]] || wait_restore
  else
    compute_freeze_src   # also normalizes the cgroup image for cross-host restore
    log "restore into $DST_POD on $DST_HOST"
    do_restore
    wait_restore
  fi
fi
FREEZE_END=${FREEZE_END:-$(date +%s.%N)}   # branches may set this earlier (see multichassis)
ROLLBACK_ARMED=0   # target is live — past the rollback window
FREEZE=$(awk "BEGIN{printf \"%.2f\", $FREEZE_END-$FREEZE_START}")
log "RESTORED on $DST_HOST — freeze window ~${FREEZE}s"
[[ -n $AIM_PID ]] && { wait "$AIM_PID" 2>/dev/null || log "WARN stream aim on $FOLLOW_HOST failed (the follow below re-points it)"; }
[[ -n $PDEL_PID ]] && { wait "$PDEL_PID" 2>/dev/null || true; }
if [[ -n $FOLLOW_HOST && -n $FOLLOW_CMD ]]; then
  # Never fails the migration: the workload is already live on the target.
  if on "$FOLLOW_HOST" "$FOLLOW_CMD"; then log "stream follow on $FOLLOW_HOST: $FOLLOW_CMD"; QUIESCED=0
  else log "WARN stream follow on $FOLLOW_HOST failed: $FOLLOW_CMD"; fi
fi

# ── network accounting report (off hot path — the workload is already live) ──
if [[ -n $NET_IFACE ]]; then
  NET_RX1=$(net_rx)
  FRZ_MIB=$(awk "BEGIN{printf \"%.1f\", ($NET_RX1-$NET_RX0)/1048576}")
  FRZ_RATE=$(awk "BEGIN{printf \"%.1f\", ($NET_RX1-$NET_RX0)/1048576/($FREEZE_END-$FREEZE_START)}")
  log "net: freeze+restore moved ~${FRZ_MIB} MiB into $DST_HOST via $NET_IFACE (~${FRZ_RATE} MiB/s; host counters, approx)"
  if [[ $POSTCOPY == 1 ]]; then
    # Post-copy: the bulk of memory crosses AFTER restore. Wait for the lazy-pages
    # daemon to exit (all pages drained) and meter that phase separately.
    # (The [s] char-class keeps pgrep -f from matching its own transport wrapper.)
    DRAIN_START=$(date +%s.%N); DRAINED=0
    for _ in $(seq 1 240); do
      on "$DST_HOST" "pgrep -f 'lazy-page[s].*--port $LAZY_PORT' >/dev/null" || { DRAINED=1; break; }
      sleep 0.5
    done
    NET_RX2=$(net_rx); DRAIN_END=$(date +%s.%N)
    DRAIN_S=$(awk "BEGIN{printf \"%.1f\", $DRAIN_END-$DRAIN_START}")
    DRAIN_MIB=$(awk "BEGIN{printf \"%.1f\", ($NET_RX2-$NET_RX1)/1048576}")
    DRAIN_RATE=$(awk "BEGIN{printf \"%.1f\", ($NET_RX2-$NET_RX1)/1048576/($DRAIN_END-$DRAIN_START)}")
    TOT_MIB=$(awk "BEGIN{printf \"%.1f\", ($NET_RX2-$NET_RX0)/1048576}")
    if [[ $DRAINED == 1 ]]; then
      log "net: post-copy page drain ~${DRAIN_MIB} MiB in ${DRAIN_S}s (~${DRAIN_RATE} MiB/s); migration total ~${TOT_MIB} MiB"
    else
      log "net: post-copy drain still running after ${DRAIN_S}s (~${DRAIN_MIB} MiB so far; total ~${TOT_MIB} MiB and counting)"
    fi
  fi
fi

# clean up the source pod (off the hot path). In --l2-ip mode its floating IP
# was already dropped during the freeze; this removes the now-idle frozen pod.
if [[ $KEEP_SOURCE == 0 ]]; then
  log "fencing source pod $SRC_POD"
  fence_source
fi

[[ $BULK == 1 ]] && { bulk_report; bulk_rebase; }
log "migration complete: $NAMESPACE/$SRC_POD -> $NAMESPACE/$DST_POD (freeze ~${FREEZE}s)"
