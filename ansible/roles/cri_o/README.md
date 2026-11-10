# cri_o role — CRI-O runtime with container checkpoint/restore

Installs CRI-O (from pkgs.k8s.io, version-matched to Kubernetes) configured for
CRIU checkpoint/restore — the runtime capability K3s's embedded containerd lacks
(it can checkpoint but not restore). Basis for same-architecture pod
live-migration. Installed **alongside** the existing runtime; wiring K3s's
kubelet to CRI-O (`--container-runtime-endpoint`) is a separate, disruptive step.

Requires the `criu` role (CRIU on the node).

## kube-ovn on CRI-O — WORKS (verified)

A full K3s edge cluster runs on CRI-O with **kube-ovn pod IP connectivity**
(node Ready, all pods Running, pods get 10.42.x IPs, DNS + service routing work)
once the role's `14-drift-compat.conf` is applied. Two CRI-O-vs-containerd gaps
had to be closed (both now in the role):
  * **default_capabilities must include NET_RAW.** The iptables binary carries
    file-caps (`cap_net_admin,cap_net_raw,cap_sys_module=eip`); if NET_RAW is not
    in the process capability *bounding set*, `execve` of iptables fails with
    EPERM — even as root — and kube-ovn's ovs-ovn/cni pods crash-loop.
  * **apparmor_profile = "unconfined"** (CRI-O applies a default profile K3s
    does not).
Gotcha seen during bring-up (NOT a CRI-O bug): restarting the ovs-ovn pod
repeatedly desynced the ovn0 interface MAC from the OVN NB logical-switch-port
MAC, so the LR answered ARP/ICMP to a MAC the host didn't have → gateway ping
failed → cni-server never served its socket → no pod IPs. Fix:
`ovn-nbctl lsp-set-addresses node-<name> "<ovn0-mac> 100.64.0.2"`. A clean
install (no manual restarts) does not hit this.

## Status (verified on node084: x86_64, Ubuntu 20.04, kernel 5.4)

| Capability | State |
|---|---|
| CRI-O install (1.31.5) + criu support | ✅ works — this role |
| Container **checkpoint** (`crictl checkpoint`) | ✅ works — real memory dump (601 KB tar with `core-*.img`/`pages-*` for a debian counter) |
| Container **restore** (`crictl`-manual) | 🟡 CRI-O engages the restore (sets `restored: true`) but the manual `crictl create` path did not run criu restore (cold start). Real restore is the **kubelet-driven** path: create a pod from a checkpoint OCI image → needs K3s wired to CRI-O. |

## Config the role applies (each was required to get checkpoint working)

1. `enable_criu_support = true` (`10-drift-criu.conf`).
2. **runc, not the bundled crun** (`11-drift-runc.conf`). CRI-O's bundled crun is
   *statically linked*, and a static binary's `dlopen("libcriu.so.2")` fails
   (`could not load libcriu.so.2`) even though the library loads fine
   system-wide. runc (dynamically linked) execs the criu *binary* instead — no
   dlopen — and under CRI-O uses the host's GNU tar (unlike K3s's containerd,
   whose bundled busybox `tar` broke criu's tmpfs dump).
3. **Unset the signature policy** (`12-drift-nosigpolicy.conf`). CRI-O refuses
   restore when a namespaced signature policy is set, even a permissive one.

## Still required at the workload level (kernel 5.4)

- `env: GLIBC_TUNABLES=glibc.pthread.rseq=0` — kernel 5.4 lacks `get_rseq_conf`,
  so criu can't dump the glibc rseq region. A newer kernel removes this need.
- `automountServiceAccountToken: false` (or otherwise avoid the projected-token
  tmpfs), unless using the kubelet path which handles pod mounts.

## Next steps for full pod live-migration

1. Wire K3s's kubelet to CRI-O (`--container-runtime-endpoint unix:///var/run/crio/crio.sock`)
   — disruptive; re-provisions the node's runtime (KubeVirt/CDI re-pull + restart).
2. Use the kubelet checkpoint API + restore-from-checkpoint-OCI-image flow
   (buildah to package the checkpoint, pod annotation to restore).
3. Preserve the pod's network identity on restore (same stretched-L2 problem as
   the VM path) and wire it behind the MEC 021 API.
