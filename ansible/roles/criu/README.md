# criu role — CRIU + container checkpoint/restore on the edge

Installs CRIU (from the upstream PPA, since Ubuntu 20.04 dropped it) and verifies
checkpoint/restore readiness. First step toward same-architecture **pod
live-migration** (checkpoint a running container, ship it, restore it elsewhere).

## Status on the current edge nodes (x86_64, Ubuntu 20.04, kernel 5.4, K3s 1.31)

| Capability | State |
|---|---|
| CRIU install (`criu 4.0`, `criu check` OK) | ✅ works — this role |
| Kernel checkpoint/restore support (`CONFIG_CHECKPOINT_RESTORE=y`) | ✅ present |
| Container **checkpoint** (`ctr -n k8s.io tasks checkpoint`) | ✅ works, with the workarounds below |
| Container **restore** | ❌ **not available** — K3s's containerd build exposes neither the CRI `CheckpointContainer`/restore path nor an `ctr` restore flag |

**Conclusion:** these nodes can *checkpoint* but not *restore*, so full CRIU pod
live-migration is **not** achievable on this K3s/containerd build as-is. It needs
a runtime that implements CRI checkpoint/restore (**CRI-O**, or a containerd
compiled with the checkpoint/restore feature) and ideally a newer kernel. The
extreme-edge Jetsons (CRIU-enabled kernels) are the intended target. On the x86
edges, **KubeVirt VM live-migration** remains the working migration mechanism.

## Blockers found (and how each was cleared) for the checkpoint side

1. **CRI `CheckpointContainer` not implemented** in K3s's containerd —
   `crictl checkpoint` returns `Unimplemented`. Use the lower-level containerd
   task API instead: `k3s ctr -n k8s.io tasks checkpoint <cid>` (runs runc→criu).

2. **Kernel 5.4 lacks `get_rseq_conf`** — criu 4.0 can't dump the glibc `rseq`
   region (that kernel API landed in ~5.13). Disable rseq for the workload:
   `env: GLIBC_TUNABLES=glibc.pthread.rseq=0`. (A newer kernel removes this need.)

3. **Projected serviceaccount-token tmpfs** (with its `..data` symlinks) trips
   criu's tmpfs dump — set `automountServiceAccountToken: false` on the pod (or
   otherwise avoid that mount) for a checkpointable workload.

4. **K3s bundles busybox `tar` first in the containerd/runc PATH**
   (`/var/lib/rancher/k3s/data/current/bin/tar -> busybox`), and criu's tmpfs
   dump calls `tar --no-unquote`, which busybox tar rejects
   (`unrecognized option: no-unquote`) — so every tmpfs dump (`/dev`, `/dev/shm`,
   masked `/sys/*`) fails. Point that symlink at GNU tar:
   `ln -sf /bin/tar /var/lib/rancher/k3s/data/current/bin/tar`. This is the key
   fix; note the `current` path is versioned and reset on K3s upgrade.

With 2–4 in place, `ctr tasks checkpoint` produces a checkpoint image
successfully (verified on node084 with a debian-slim counter pod).

## Notes

- Container image needs GNU tar available too if it has its own; distroless/scratch
  images have no tar. A debian/ubuntu base works; busybox/alpine do not.
- Same-architecture only (x86 ↔ x86, ARM ↔ ARM) — a checkpoint is machine code
  + register state.
