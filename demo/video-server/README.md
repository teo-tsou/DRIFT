# DRIFT demo video-server

A CRIU-migratable pod that serves a video **from RAM** so the Follow-Me demo shows
the migration preserving **memory + network state**: on a cross-cluster blocking
checkpoint the target re-downloads nothing — it restores the same in-memory clip and
the preserved TCP socket, and playback resumes on the new edge exactly where it froze.

    Big Buck Bunny (loaded once into RAM) -> my-app pod (.101:9000) -> UE over 5G -> phone <video>

## Why RAM (not disk / NFS)
CRIU migrates the **process image (memory + fds + sockets), not the container
filesystem**. The migration target is a **fresh `python:3-slim` placeholder** — it
does *not* have any file the source wrote to local disk. And cross-**cluster** there
is no shared storage (each cluster has its own NFS; that's also why VMs need
DataVolume replication). So a file on disk/NFS wouldn't be present on the target,
and it would demonstrate *shared storage*, not CRIU state transfer. Keeping the clip
in RAM is what makes "the state moved with the workload" true and visible. (Disk/NFS
only helps for *intra*-cluster moves that share a mount.)

At startup the base `python:3-slim` downloads the clip into memory (stdlib
`urllib`), then serves it with HTTP range from RAM. Keep it modest (default ~30 MB,
240p, ~10 min) — CRIU carries that RAM in the checkpoint, so a bloated clip just
inflates the freeze. Change the source with `VIDEO_URL` (mp4 or webm); there are
built-in fallbacks, and a browser User-Agent is sent (test-videos.co.uk 403s the
default urllib UA). In the dashboard use **🎬 Video file** mode.

## Deploy on drift-edge-1 (just apply)
```bash
kubectl apply -f my-app-video.yaml
kubectl get pod my-app -w        # 2/2 Running (work downloads the clip then serves :9000)
```

## Optional: a baked image instead of in-memory download
If the edges have no internet (can't download the clip at startup), use the
baked-image variant: `Dockerfile` generates a clip with ffmpeg at build time and
`serve.py` serves it from disk. Build it, make it available on both edges
(registry push, or side-load via `skopeo copy docker-archive:... containers-storage:...`
on each node), and point the `work` image at it. The in-memory manifest above is
simpler and preferred when the edges have outbound internet.

## Point the demo at it
- UE proxy (Pi): `socat TCP-LISTEN:8080,fork,reuseaddr TCP:10.220.3.101:9000`
  (its outbound to 10.220.3.101 routes over wwan0/5G).
- Follow-Me → smartphone → Stream URL: `http://10.220.2.187:8080/loop.mp4`
- Trigger a migration (MEC page). The frame counter in the video **freezes** at
  the cutover, then jumps forward as it resumes on the new edge — the floating
  IP `10.220.3.101` followed the pod, so the URL never changed.

Keep the clip modest (640×360@15 here) so the 5G link plays it and the migration
freeze is the visible stall, not a permanently starved stream.

## Show BOTH pod and VM (both migration mechanisms)
To demonstrate that **both** CRIU (pod) and KubeVirt (VM) preserve the session,
run both video servers on **distinct floating IPs** on the shared L2:

| Workload | Migration | Floating IP | Manifest |
|---|---|---|---|
| `my-app` (pod) | CRIU (blocking) | `10.220.3.101:9000` | `my-app-video.yaml` |
| `demo-vm` (VM) | KubeVirt decentralized | `10.220.3.102:9000` | `vm-video.yaml` |

```bash
kubectl apply -f my-app-video.yaml      # pod
kubectl apply -f vm-video.yaml          # VM (wait: kubectl get dv -w -> Succeeded, then vmi Running)
```
Both appear in the Follow-Me **topology** at their edge (POD/VM badges), and each
is reachable by the UE over 5G at its own IP. Point a UE proxy at whichever you're
demoing:
- pod:  `socat TCP-LISTEN:8080,fork,reuseaddr TCP:10.220.3.101:9000`
- VM:   `socat TCP-LISTEN:8081,fork,reuseaddr TCP:10.220.3.102:9000`
…and set the smartphone Stream URL to `http://10.220.2.187:8080/loop.mp4` (pod)
or `:8081/loop.mp4` (VM).

Differences you'll see:
- **Pod (CRIU):** the frame counter freezes ~the blocking-checkpoint freeze
  (seconds), then resumes — a clear, visible stall.
- **VM (KubeVirt):** the cutover is **sub-second**, so the freeze is much shorter
  — but the *total* migration takes minutes (disk replication) beforehand while
  the video keeps playing normally.

Notes for the VM path:
- The VM clip is downloaded at boot (the disk is replicated on migration, so it
  persists — no baked image needed like the pod).
- The `python3 -m http.server` in the guest serves progressively (no HTTP range);
  over the slow 5G link that's fine and the migration stall still shows.
- Validate the **macvlan-l2-over-VM + IP-follow** on the testbed — it's the less-
  exercised part; the pod path is the reliable primary.
