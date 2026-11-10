# pod_migration — controller-driven CRIU pod live-migration

Live-migrates a running pod's workload container between CRI-O edge nodes with
a **sub-second freeze** and **surviving TCP connections**, by driving `crun`
checkpoint/restore directly from a controller — **not** through the kubelet /
CRI-O restore-from-image path.

## Why not the kubelet path

The Kubernetes forensic checkpoint/restore-from-image path is pre-alpha and, on
this K3s + CRI-O edge stack (verified across kernel 5.4→5.15, CRI-O 1.31→1.32,
crun 1.14→1.19, runc 1.2), is broken in several independent ways: it silently
cold-starts checkpoints that carry a **time namespace** (crun), fails on
**masked-path mounts** like `/proc/scsi` (runc), and hits **unified-cgroup**
move errors — inconsistently, depending on which runtime made the checkpoint.

Raw `crun`/`criu` checkpoint/restore, by contrast, is reliable. So DRIFT
orchestrates it itself: checkpoint on the source, ship the images over a shared
store, restore into a pre-created target pod. This is the same relationship the
VM path has with KubeVirt — DRIFT owns the migration, the runtime owns the
primitive.

## How it works (proven on kernel 5.15 / CRI-O 1.32 / crun 1.19.1)

```
 SOURCE node A                 shared store (NFS)            TARGET node B
 ┌──────────────┐              /srv/drift-ckpt              ┌──────────────┐
 │ pod: app     │                    │                      │ pod: app-sb  │
 │  holder(sleep)│  crun checkpoint  │   crun restore       │  holder(sleep)│
 │  work  ───────┼──►  pre-dump*  ──►│──► into work's ─────►│  work  (state)│
 │  (workload)  │      + final dump  │    bundle/netns      │  (resumed)   │
 └──────────────┘                    │                      └──────────────┘
```

Key design points:

- **2-container pods.** A `holder` (`sleep infinity`) keeps the pod sandbox —
  and therefore its **netns + IP** — alive while the `work` container is
  checkpointed. Without it, a single-container pod's sandbox is reaped the
  moment the workload dies and the restore has no namespace to rejoin.
- **Pre-created target pod.** The target pod is created ahead of time with the
  same workload image (its `work` container running a placeholder). Its bundle
  already has the correct **rootfs + netns**, so we restore straight into it.
- **Same IP = seamless sockets.** CRIU restores established TCP connections
  (`--tcp-established`); they survive only if the target owns the **same IP**.
  Intra-cluster: pin it with the kube-ovn `ovn.kubernetes.io/ip_address`
  annotation on the target pod. Cross-cluster: the EVPN stretched-L2 (evpn_l2
  role) carries the IP, exactly as for the VM path.
- **Pre-copy for low downtime.** `--pre-copy N` runs N iterative
  `crun checkpoint --pre-dump` passes that ship memory *while the container
  runs*; the final freeze then dumps only the dirty delta. This keeps the
  freeze sub-second regardless of memory size (the CRIU analog of QEMU
  pre-copy). A shared store keeps the transfer off the frozen path.
- **Cross-host cgroup normalization.** criu records the source container's
  cpuset properties; on a *different* host it recreates the source pod's cgroup
  chain and fails writing `cpuset.cpus` to the leaf (EACCES → whole restore
  aborts). Since the restored process lands in the TARGET container's own
  cgroup (correct cpuset) regardless, the controller runs `drift-cgroup-strip`
  on the checkpoint to drop the cpuset controller before a cross-host restore
  (no-op same-host). Without this, cross-node restore fails at the cgroup step.

Measured on a same-node proof: pure freeze ≈ **0.12 s** (dump 0.067 s +
restore 0.057 s); a live host→pod TCP client streamed across the
checkpoint/restore with no disconnect and no data loss.

## What this role installs

1. **crun-drift** — crun built *dynamically* with `+CRIU` (CRI-O's bundled crun
   is static and cannot `dlopen(libcriu)`). The cri_o role's
   `17-drift-crun.conf` points the `crun` runtime handler here.
2. **Shared checkpoint store** — an NFS client mount at `{{ ckpt_store_path }}`,
   identical on every node.
3. **`drift-pod-migrate`** — the migration controller CLI.
4. **`drift-node-agent`** — a privileged DaemonSet (deployed on control-plane
   nodes) that is the migration **transport** (see below).

## How migrations reach the nodes (the node-agent, not SSH)

The checkpoint/restore/netns operations are node-local (no K8s API exists for
them), so `drift-pod-migrate` needs to run commands *on* each node. It does that
through **`drift-node-agent`** — a privileged DaemonSet pod on every node exposing
a token-authed `POST /exec` at `<nodeIP>:8099`. Each request runs the command in
the host namespaces (`nsenter -t 1 …`), so it behaves exactly like a node shell.

This means migrations are driven **through the cluster**, not over SSH: native
reachability, a shared-secret token (a `kube-system` Secret, generated once), and
it scales with the DaemonSet. Every node action funnels through the controller's
one `on()` helper, which calls the agent when `DRIFT_NODE_AGENT_PORT` +
`DRIFT_NODE_AGENT_TOKEN` are set (the backend passes them, reading the token from
the Secret) and falls back to SSH (`DRIFT_SSH_OPTS`) otherwise.

> **Security:** `/exec` is host-root command execution — keep it internal-only,
> gated by the token. Production hardening: mTLS, a NetworkPolicy restricting
> callers, and ideally a *typed* migration API instead of a generic `/exec`.

## Prerequisites

- Kernel **>= 5.15** on every node (older kernels lack checkpoint features and
  need the `GLIBC_TUNABLES=glibc.pthread.rseq=0` workaround; 5.15 removes it).
- The `criu` and `cri_o` roles applied (CRIU present; CRI-O with the `crun`
  RuntimeClass and `enable_criu_support`).
- Same CPU architecture on source and target (CRIU is same-arch only).
- Pods use `runtimeClassName: crun` and `restartPolicy: Never`.

## Usage

Provision the nodes:

```bash
ansible-playbook -i inventories/xcluster/hosts.yml \
    -e ckpt_nfs_server=<hub-ip> playbooks/setup_pod_migration.yml
```

Pre-create the target pod (holder + `work` placeholder, same image, pinned IP),
then migrate:

```bash
drift-pod-migrate \
    --src-host nodeA --src-pod app \
    --dst-host nodeB --dst-pod app-standby \
    --container work --store /srv/drift-ckpt \
    --pre-copy 2 --tcp
```

`--src-host`/`--dst-host` accept a hostname/IP (over SSH) or `local`. See
`drift-pod-migrate --help` for all flags.

### Seamless TCP across the hop (`--l2-ip`)

When the workload listens on a **floating IP carried by a shared-L2 secondary
interface** (a macvlan on the physical NIC — the same IP is reachable from
either node because they share the L2), an established TCP connection survives
the migration. Requires multus + a macvlan NAD and the `static` CNI plugin in
`/opt/cni/bin` (see below).

Give the source pod the macvlan with the floating IP, and PRE-CREATE the target
pod with the same macvlan but a **dummy IP** (`--l2-dummy`) — a second live copy
of the floating IP would be dual-active and reset the connection. Then:

```bash
drift-pod-migrate \
    --src-host nodeA --src-pod app --dst-host nodeB --dst-pod app-standby \
    --container work --tcp \
    --l2-ip 10.0.1.222/24 --l2-iface net1 --l2-dummy 10.0.1.223/24
```

The controller checkpoints (`--tcp-established`), drops the floating IP from the
source (frees it), **moves the IP** onto the target's interface, restores, and
**GARPs** so the switch/peers relearn it on the new node. The hot path is
collapsed into two bundled SSH round-trips (one script per host) over a
multiplexed connection, so the client-visible stall is criu-bound. Proven
node084→node089 across two clusters: a client streamed a counter with no reset
and no data loss, **~0.7 s workload downtime** (sub-second). macvlan needs a
shared L2; for L3-separated sites stretch it with EVPN (evpn_l2 role) instead.

Macvlan pod attachment (once per cluster):

```bash
kubectl apply -f - <<EOF
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata: {name: macvlan-l2}
spec:
  config: '{"cniVersion":"0.3.1","type":"macvlan","master":"eth0","mode":"bridge","ipam":{"type":"static"}}'
EOF
# and drop the `static` CNI plugin into /opt/cni/bin (containernetworking-plugins).
```

### Seamless TCP intra-cluster (`--ovn-ip`)

Within **one** cluster you don't need a macvlan or EVPN at all — kube-ovn already
spans every node with a single overlay, so the pod's *own* primary IP can follow
it to another node. `--ovn-ip` carries it by **re-pointing the target's OVN
logical switch port** to the source's MAC+IP; OVN binds that port to whichever
chassis runs the pod (the destination node) and routes the IP there over geneve.
This is the overlay analog of the `--l2-ip` GARP failover — an `ovn-nbctl
lsp-set-addresses` instead of a macvlan move + GARP.

Pre-create the target pod on the destination node (its own placeholder IP), then:

```bash
drift-pod-migrate \
    --src-host nodeA --src-pod app --dst-host nodeB --dst-pod app-standby \
    --container work --ovn-ip 10.42.0.199    # --ovn-iface defaults to eth0
```

The controller checkpoints (`--tcp-established`, implied), fences the source
(freeing the IP + its LSP), re-points the target LSP to the source's MAC+IP,
moves the target's `eth0` onto it (the iface MAC must match, since OVN's ARP
responder now answers the IP with that MAC), and restores. Requires `ovn-nbctl`
reachable via the `ovn-central` pod (default ns `kube-system`) and `nsenter` on
the destination.

Cross vs intra — pick the IP-follow strategy by topology, not by mode:

| nodes share… | strategy | flag |
|--------------|----------|------|
| one kube-ovn cluster | re-point the OVN logical port | `--ovn-ip` |
| a physical L2 (separate clusters) | macvlan floating IP + GARP | `--l2-ip` |
| only L3 (separate sites) | EVPN stretched-L2 (evpn_l2 role) | `--l2-ip` over the stretch |

**Status — proven end-to-end** on a 2-node kube-ovn cluster (node084→node089): a
client held a **single** TCP connection to the workload while it migrated to the
other node; the counter stream stayed unbroken (no skipped values, no reset), the
OVN port binding for the IP moved to the target node's chassis, and the same
established socket (`10.42.0.150:9000 ↔ client`) reappeared on the target. Freeze
≈ **1.9 s**, dominated by waiting for OVN to program the re-pointed port before the
restore's TCP-repair runs. Notes: add the IP with the pod-subnet **prefix** (a bare
`/32` drops the connected route and CRIU's `soccr` connect-back fails "Network is
unreachable"); on a k3s **agent** target, `ovn-nbctl`/`kubectl` must run on the
control-plane node (`DRIFT_KUBE_HOST`); kube-ovn's IPAM bookkeeping lags the manual
re-point (the carried IP isn't formally re-reserved — fine for a controlled window).

#### Lower downtime with multi-chassis (`--ovn-multichassis`)

`--ovn-ip` alone spends most of its ~1.9 s freeze *waiting for OVN to program the
re-pointed port*. `--ovn-multichassis` removes that wait using OVN **multi-chassis
port binding** — the same technique kube-ovn uses for KubeVirt live migration:

- **Pre-warm (off the hot path, source still serving):** the target veth is made
  to reuse the source's logical port (`ovs-vsctl set interface … iface-id=<src
  LSP>`), then the source port is bound to *both* chassis via
  `options:requested-chassis="<src>,<dst>"`. OVN programs the target datapath as
  an **additional** binding while the source keeps running — zero downtime so far.
- **Hot path:** checkpoint the source and drop its IP; the target — already bound
  as *additional* — starts serving the moment it claims the IP and restores. No
  primary flip on the critical path.
- **Off the hot path:** promote the target to sole primary
  (`requested-chassis="<dst>"`), which drops the source binding + replication.

```bash
drift-pod-migrate --src-host nodeA --src-pod app --dst-host nodeB --dst-pod app-standby \
    --container work --ovn-ip 10.42.0.150 --ovn-multichassis
```

Measured node084→node089: freeze **~0.96 s** (vs ~1.9 s for plain `--ovn-ip`),
client TCP connection unbroken, port binding moved to the target chassis, then the
additional binding cleaned up. **Limitation:** because the target *reuses* the
source's logical port, the source pod is kept as the port's owner (never fenced) —
deleting it would remove the port the target now serves. Proper LSP-ownership
transfer to the target pod (as kube-ovn does internally for VMs) is the remaining
step for a fully self-contained migration. Requires `ovs-vsctl` reachable via the
per-node `ovs-ovn` pods and the OVN version supporting multi-chassis (24.03 here).

#### Composing with post-copy (memory-independent + seamless)

Network-follow and compute are **orthogonal plugs** — a compute plug
(`compute_freeze_src` + `restore_cmd`, either *blocking* or *post-copy*) and a
network plug (multi-chassis / L2). So multi-chassis composes with post-copy:

```bash
drift-pod-migrate --src-host nodeA --src-pod app --dst-host nodeB --dst-pod app-standby \
    --container work --ovn-ip 10.42.0.150 --ovn-multichassis --post-copy
```

This is the **intra-cluster strategy for containers**: the source serves pages
(post-copy → memory-independent freeze) *and* the IP follows via multi-chassis
(→ seamless TCP). Proven node084→node089 on a 313 MB workload: **700 KB** written
to the store (the 300 MB never touched disk), the full RSS faulted back in over
UFFD on the target, the client's TCP connection unbroken, freeze ~1.5 s
*independent of workload memory*. Post-copy needs the source kept alive to serve
pages, so it composes only with multi-chassis (which keeps the source), not the
single-chassis re-point (which fences it) — the controller enforces this.

### Memory-independent freeze (`--post-copy`)

Pre-copy shrinks the freeze but the target restore still reads the whole working
set before the process runs, so downtime grows with memory. **Post-copy** inverts
this: the source freezes, dumps **only non-lazy state** (a few hundred KB), and
stays alive as a CRIU **page-server**; the target resumes on that metadata alone
and **faults each memory page in over the network on demand** (userfaultfd). The
bulk of memory **never touches the store** — so the freeze is *independent of
workload memory size*.

```bash
drift-pod-migrate \
    --src-host nodeA --src-pod app --dst-host nodeB --dst-pod app-standby \
    --container work --store /srv/drift-ckpt --post-copy
```

Measured node084→node089 across two clusters, identical freeze regardless of RSS:

| workload RSS | freeze window | restore resume | bytes written to store |
|--------------|--------------|----------------|------------------------|
| 300 MB       | ~1.24 s      | 0.13 s         | 688 KB                 |
| 1 GB         | ~1.24 s      | 0.13 s         | 688 KB                 |

Implementation notes / constraints:

- **runc, not crun.** libcriu (which `crun-drift` links) has no lazy-pages
  setter, but `runc` execs the `criu` binary which does. So `--post-copy` pods
  must be **runc-managed** — the default runtime, *not* `runtimeClassName: crun`.
- **Flow.** Source: `runc checkpoint --lazy-pages --page-server <src>:<port>
  --status-fd` (backgrounded — it keeps serving pages, signals ready via the
  status fd). Target: `criu lazy-pages --page-server --address <src> --port
  <port> -D <img>` daemon, then `runc restore --lazy-pages`. The page-server
  `<port>` is randomized per run so a crashed run that leaks a server never
  blocks the next (`DRIFT_LAZY_PORT` pins it).
- **Not combinable with pre-copy.** `--pre-copy` is ignored under `--post-copy`:
  pre-dumped pages are written *eagerly* and get restored eagerly, which defeats
  lazy (measured: pre-copy+lazy = ~2.9 s vs lazy-only 0.13 s).
- **Trade-off vs pre-copy.** Post-copy moves the memory cost *after* resume: the
  first accesses to not-yet-migrated pages block on a network fault, so a
  latency-critical workload sees a brief warm-up until its working set is local.
  The source must also stay reachable until the pages drain. Pre-copy (`--tcp`
  `--l2-ip`) keeps a warm target but its freeze grows with the dirty set; choose
  post-copy when *freeze* must stay flat as memory grows, pre-copy when
  *post-resume* latency must be flat.

## Roadmap

- **MEC 021 wiring:** expose migrate as the container twin of the VM
  `cross_cluster_migration_controller.py`, with the O-6 health-gate/rollback.
