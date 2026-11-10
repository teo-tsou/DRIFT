# EMT YOLO workload (DRIFT pod)

The EMT dataset workload, from `drift-emt-dataset-workload`, packaged as DRIFT pods:

- `emt-yolo-pod.yaml` is the receiver. It runs YOLOv8n on the CPU and migrates between edges.
- `emt-sender-pod.yaml` is the sender. It plays the vehicle camera, replays the dataset over UDP at 1280x720 (`drift_sender.py`) and does not migrate.

`drift_receiver.py` wraps the original receiver without changing it, so that CRIU can migrate it:

- Detections are written to stdout instead of an open `.jsonl` file.
- A `/health` endpoint on port 9000 reports FPS, frame counters and the time of the last processed frame.
- The model runs as ONNX on ONNX Runtime, with no PyTorch or Ultralytics in the process. Pre- and post-processing reproduce Ultralytics'.
- JPEG decoding runs on its own thread.
- ONNX Runtime's arena is off and malloc returns large buffers, which keeps the checkpoint small.
- The thread count comes from `INFER_THREADS` (8 by default).

## Build and side-load (no registry)

There is no registry, so the image is built on a node and copied into CRI-O on every node that may host the pod. The migration target gets only the image, so the code and the weights are baked in.

```sh
# Build context: the workload repo's udp_*.py plus drift_receiver.py and this Dockerfile.
sudo docker build -t localhost/drift-emt-yolo:udp <ctx>
sudo skopeo copy docker-daemon:localhost/drift-emt-yolo:udp containers-storage:localhost/drift-emt-yolo:udp
# Copy the image to another node:
sudo skopeo copy docker-daemon:... docker-archive:/tmp/emt.tar
# then, on that node:
sudo skopeo copy docker-archive:/tmp/emt.tar containers-storage:localhost/drift-emt-yolo:udp
```

When installing skopeo, keep the existing `/etc/containers/policy.json` (`dpkg --force-confold`). CRI-O relies on it.

## Node requirement

`net.core.rmem_max >= 8 MB`. The pod_migration role sets it. With the default limit, almost every frame arrives incomplete.

## Measured on the AGX Orin (2026-10-14, CPU only)

**Detections.** The lean receiver gives the same output as Ultralytics on 40 dataset frames: 133 of 133 boxes at full size and 143 of 143 at 720p, identical confidences, box IoU at least 0.994.

**Throughput and memory** (one 90 s stream test per row, 12 FPS sent):

| Receiver | Frames sent | FPS | Memory CRIU copies (anonymous) |
|---|---|---|---|
| Original stack (PyTorch) | 2560x1440 | 3.4 | |
| Ultralytics + ONNX Runtime | 2560x1440 | 12 | 458 MB |
| Lean (ONNX Runtime only, malloc settings) | 2560x1440 (1008 kB, ~97 Mbps) | 12 | 71 MB |
| **Lean, default** | **1280x720 (111 kB, ~11 Mbps)** | **12** | **87 MB** |
| Lean, `INFER_SPIN=0` | 1280x720 | 11.3 | 102 MB |

- The image is 602 MB, down from 2.4 GB.
- At 12 FPS the receiver uses about 8 cores. With `INFER_SPIN=0` it uses about 5.
- The memory saving comes from the lean receiver and the malloc settings. The 720p sender is needed only for the 5G uplink.
- On the 6-core Nano, set `INFER_THREADS=6` and remeasure.

## Workload power (2026-10-14, onboard INA3221 sensors via tegrastats)

The stream (1280x720, 12 FPS sent from the hub) uses the colleague's original sender, so the board runs only the receiver. Each row is 90 s idle, 120 s with the workload, then 90 s idle again.

| Board | Idle | With workload | Workload power | FPS | Energy per frame |
|---|---|---|---|---|---|
| AGX Orin (8 threads; rails GPU_SOC + CPU_CV + SYS_5V0) | 8.7 W | 22.4 W | **13.7 W** | 12.0 | 1.14 J |
| Orin Nano (6 threads; VDD_IN) | 5.0 W | 10.0 W | **5.0 W** | 6.6 (its maximum; 45% of frames dropped) | 0.76 J |

The controller uses these values in `ECB_EDGE_MAP`.
