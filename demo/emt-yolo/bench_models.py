#!/usr/bin/env python3
"""YOLOv8n vs YOLO26n on this board, through the receiver's own OnnxYolo
(ONNX Runtime, CPU): ms per frame (model call, incl. YOLOv8's NMS), anonymous
memory after the run, and the detections per frame. Each model runs in a fresh
process so memory is not shared.

  bench_models.py <frames dir> [n_frames]        # run in the YOLO image
"""
import json
import os
import subprocess
import sys
import time

MODELS = {"yolov8n": "/models/yolov8n.onnx", "yolo26n": "/models/yolo26n.onnx"}


def anon_mib():
    for line in open("/proc/self/smaps_rollup"):
        if line.startswith("Anonymous:"):
            return int(line.split()[1]) / 1024


def one(model, frames_dir, n):
    sys.path.insert(0, "/app")
    import cv2
    import drift_receiver as dr
    paths = sorted(os.path.join(r, f) for r, _, fs in os.walk(frames_dir) for f in fs if f.endswith(".jpg"))[:n + 5]
    frames = [cv2.imread(p) for p in paths]
    m = dr.OnnxYolo(MODELS[model], int(os.environ.get("INFER_THREADS", "8")))
    for f in frames[:5]:
        m(f)                                                   # warm-up
    t, dets, classes = [], [], {}
    for f in frames[5:]:
        s = time.perf_counter(); d = m(f); t.append(time.perf_counter() - s)
        dets.append(len(d))
        for x in d:
            classes[x["class_name"]] = classes.get(x["class_name"], 0) + 1
    t.sort()
    return {"model": model, "ms_median": round(1000 * t[len(t) // 2], 1), "ms_p90": round(1000 * t[int(len(t) * .9)], 1),
            "fps_model_only": round(1 / t[len(t) // 2], 1), "anon_mib": round(anon_mib(), 1),
            "dets_per_frame": round(sum(dets) / len(dets), 2),
            "top_classes": dict(sorted(classes.items(), key=lambda kv: -kv[1])[:5])}


if __name__ == "__main__":
    if len(sys.argv) > 3:
        print(json.dumps(one(sys.argv[3], sys.argv[1], int(sys.argv[2]))))
        sys.exit()
    frames_dir, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 60
    for model in MODELS:
        out = subprocess.run([sys.executable, __file__, frames_dir, str(n), model], capture_output=True, text=True)
        print(out.stdout.strip() or out.stderr.strip()[-400:], flush=True)
