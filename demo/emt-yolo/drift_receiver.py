#!/usr/bin/env python3
"""EMT UDP YOLO receiver, made migratable for DRIFT (CRIU).

Reuses the colleague's UDP reception unchanged (UDPYOLOReceiver.receive and the
udp_protocol FrameAssembler) and replaces what a CRIU cross-cluster migration
and the Jetson CPUs need replaced:

  * No open file. The original appends detections to a .jsonl file and keeps
    the fd open. DRIFT carries only the process image, not the container's
    written files, and CRIU restores fds by path, so that file would break the
    restore. Detections go to stdout instead (one JSON line per frame).
  * A /health endpoint (HTTP, port 9000) with processed frames, current FPS and
    the time of the last processed frame, so the dashboard and the KPI scripts
    can see the migration freeze as a gap in processing.
  * YOLOv8n runs as ONNX on ONNX Runtime, with no PyTorch or Ultralytics in the
    process. Pre- and post-processing reproduce Ultralytics' (letterbox to 640,
    confidence 0.25, per-class NMS at IoU 0.7, at most 300 boxes). ONNX
    Runtime is ~3.5x faster than PyTorch on the Orin CPU, and dropping the two
    libraries cuts the memory CRIU must copy at every migration by ~150 MB.
  * JPEG decoding runs on its own thread, overlapping inference.
  * ONNX Runtime's memory arena is off and large buffers are mmap'ed by malloc
    (MALLOC_MMAP_THRESHOLD_ in the image), so freed frame memory goes back to
    the kernel instead of growing the checkpoint.

The UDP socket (port 5005) is restored by CRIU with the process; frames sent
while the pod is frozen are lost, which is the measurable service interruption.
"""
import argparse
import ast
import ctypes
import gc
import http.server
import json
import os
import queue
import signal
import socket
import socketserver
import threading
import time

import cv2
import numpy as np
# Keep ONNX Runtime's built-in 1DS telemetry from starting at all (its thread
# and in-memory SQLite event store were 55% of a migration's delta; the
# disable_telemetry_events() call only stops new events). Read at import.
os.environ.setdefault('ORT_DISABLE_TELEMETRY', '1')
import onnxruntime as ort

from udp_yolo_receiver import UDPYOLOReceiver

CONF, IOU, MAX_DET, MAX_WH = 0.25, 0.7, 300, 7680


class OnnxYolo:
    """YOLOv8 detection on ONNX Runtime, matching Ultralytics' predict()."""

    def __init__(self, path, threads):
        ort.disable_telemetry_events()                 # see ORT_DISABLE_TELEMETRY above
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.enable_cpu_mem_arena = False
        # INFER_SPIN=0 makes idle worker threads sleep instead of busy-waiting:
        # 5.2 instead of 8 cores on the Orin, leaving CPU to the board's other
        # tenants, but 11.3 instead of 12 FPS. Spinning (the default) keeps 12.
        opts.add_session_config_entry('session.intra_op.allow_spinning',
                                      os.environ.get('INFER_SPIN', '1'))
        self.session = ort.InferenceSession(path, opts, providers=['CPUExecutionProvider'])
        inp = self.session.get_inputs()[0]
        self.input_name, self.size = inp.name, int(inp.shape[2])
        meta = self.session.get_modelmeta().custom_metadata_map
        self.names = ast.literal_eval(meta['names']) if 'names' in meta else {}

    def letterbox(self, bgr):
        # Ultralytics LetterBox(new_shape=640, auto=False, scaleup=True, center=True)
        h, w = bgr.shape[:2]
        r = min(self.size / h, self.size / w)
        nw, nh = round(w * r), round(h * r)
        dw, dh = (self.size - nw) / 2, (self.size - nh) / 2
        if (w, h) != (nw, nh):
            bgr = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        top, bottom = round(dh - 0.1), round(dh + 0.1)
        left, right = round(dw - 0.1), round(dw + 0.1)
        return cv2.copyMakeBorder(bgr, top, bottom, left, right, cv2.BORDER_CONSTANT,
                                  value=(114, 114, 114))

    def __call__(self, bgr):
        h0, w0 = bgr.shape[:2]
        img = self.letterbox(bgr)[..., ::-1].transpose(2, 0, 1)        # BGR->RGB, HWC->CHW
        x = np.ascontiguousarray(img, dtype=np.float32)[None] / 255.0
        out = self.session.run(None, {self.input_name: x})[0][0]
        if out.shape[-1] == 6:                                         # end-to-end (YOLO26): (300, 6)
            return self.final_boxes(out, h0, w0)
        pred = out.T                                                   # YOLOv8: (8400, 4 + classes)
        scores = pred[:, 4:]
        cls = scores.argmax(1)
        conf = scores[np.arange(len(cls)), cls]
        keep = conf > CONF
        if not keep.any():
            return []
        b, conf, cls = pred[keep, :4], conf[keep], cls[keep]
        boxes = np.empty_like(b)                                       # xywh -> xyxy
        boxes[:, 0] = b[:, 0] - b[:, 2] / 2
        boxes[:, 1] = b[:, 1] - b[:, 3] / 2
        boxes[:, 2] = b[:, 0] + b[:, 2] / 2
        boxes[:, 3] = b[:, 1] + b[:, 3] / 2
        idx = self.nms(boxes + (cls * MAX_WH)[:, None], conf)[:MAX_DET]  # per-class NMS
        boxes, conf, cls = boxes[idx], conf[idx], cls[idx]
        gain = min(self.size / h0, self.size / w0)                     # Ultralytics scale_boxes
        padx = round((self.size - w0 * gain) / 2 - 0.1)
        pady = round((self.size - h0 * gain) / 2 - 0.1)
        boxes[:, [0, 2]] = ((boxes[:, [0, 2]] - padx) / gain).clip(0, w0)
        boxes[:, [1, 3]] = ((boxes[:, [1, 3]] - pady) / gain).clip(0, h0)
        return [dict(class_id=int(c), class_name=self.names.get(int(c), str(int(c))),
                     confidence=round(float(s), 4), bbox=[round(float(v), 1) for v in bb])
                for bb, s, c in zip(boxes, conf, cls)]

    def final_boxes(self, out, h0, w0):
        """NMS-free models (YOLO26, end2end export) already return the final
        detections: x1 y1 x2 y2 score class in letterboxed input pixels."""
        out = out[out[:, 4] > CONF][:MAX_DET]
        if not len(out):
            return []
        boxes = out[:, :4].copy()
        gain = min(self.size / h0, self.size / w0)
        padx = round((self.size - w0 * gain) / 2 - 0.1)
        pady = round((self.size - h0 * gain) / 2 - 0.1)
        boxes[:, [0, 2]] = ((boxes[:, [0, 2]] - padx) / gain).clip(0, w0)
        boxes[:, [1, 3]] = ((boxes[:, [1, 3]] - pady) / gain).clip(0, h0)
        return [dict(class_id=int(c), class_name=self.names.get(int(c), str(int(c))),
                     confidence=round(float(sc), 4), bbox=[round(float(v), 1) for v in bb])
                for bb, sc, c in zip(boxes, out[:, 4], out[:, 5])]

    @staticmethod
    def nms(boxes, scores):
        order = scores.argsort()[::-1]
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        keep = []
        while order.size:
            i = order[0]
            keep.append(i)
            xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
            yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
            xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
            yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
            inter = (xx2 - xx1).clip(0) * (yy2 - yy1).clip(0)
            iou = inter / (area[i] + area[order[1:]] - inter)
            order = order[1:][iou <= IOU]
        return np.array(keep, dtype=int)


GAP_MIN = float(os.environ.get('GAP_MIN', '1.0'))
# One JSON line per frame on stdout, as the original receiver prints. Off under
# DRIFT (DETECTIONS_LOG=0 in the manifests): nothing reads it, and its strings
# were ~15% of a migration's delta.
DETECTIONS_LOG = os.environ.get('DETECTIONS_LOG', '1') != '0'
VIEW_W = int(os.environ.get('VIEW_WIDTH', '640'))       # viewer frame width
VIEW_HOLD = 5.0                                         # s a viewer keeps frames rendered


# Drop the dead part of idle native threads' stacks before a checkpoint. After
# an inference, each ONNX Runtime worker leaves ~80 kB of garbage below its
# stack pointer, different every time: ~60% of a migration's delta once the
# heap churn was gone (2026-10-22). A worker blocked in a syscall does not touch
# anything below its SP until it is woken, which during a quiesce nothing does
# (no input, no inference); MADV_DONTNEED there makes those pages absent, so
# CRIU does not dump them. Python's own threads are skipped (they wake on
# timeouts), and a margin below the SP is kept. DRIFT_STACK_TRIM=0 turns it off.
STACK_TRIM = os.environ.get('DRIFT_STACK_TRIM', '1') != '0'
STACK_MARGIN = 16 * 1024


def trim_idle_stacks():
    libc = ctypes.CDLL('libc.so.6', use_errno=True)
    libc.madvise.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
    page = os.sysconf('SC_PAGE_SIZE')
    python = {t.native_id for t in threading.enumerate()} | {threading.get_native_id(), os.getpid()}
    regions = []
    with open('/proc/self/maps') as f:
        for line in f:
            parts = line.split()
            if parts[1].startswith('rw') and len(parts) == 5:          # anonymous, writable
                lo, hi = (int(x, 16) for x in parts[0].split('-'))
                regions.append((lo, hi))
    freed = 0
    for tid in os.listdir('/proc/self/task'):
        if int(tid) in python:
            continue
        try:
            sc = open(f'/proc/self/task/{tid}/syscall').read().split()
        except OSError:
            continue
        if len(sc) < 3 or sc[0] in ('running', '-1'):
            continue                                                    # not parked in a syscall
        sp = int(sc[-2], 16)
        for lo, hi in regions:
            if lo <= sp < hi:
                top = (sp - STACK_MARGIN) & ~(page - 1)
                if top > lo and hi - lo >= 1 << 20:                      # a thread stack, not a small map
                    if libc.madvise(lo, top - lo, 4) == 0:              # MADV_DONTNEED
                        freed += top - lo
                break
    return freed


class DriftReceiver(UDPYOLOReceiver):
    def __init__(self, *a, health_port=9000, threads=8, **kw):
        super().__init__(*a, **kw)
        # The parent creates this file only for the compose health check and
        # holds no fd on it; remove it so nothing in the container is written.
        self.ready_file.unlink(missing_ok=True)
        self.health_port = health_port
        self.threads = threads
        self.fps = 0.0
        self.last_processed_at = None
        # Stalls between two processed frames longer than GAP_MIN seconds, as
        # [start, seconds]. Kept in process memory, so a migration's freeze is
        # recorded by the workload itself, across boards (NTP-synced wall clock),
        # whatever the network does to whoever polls /health.
        self.gaps = []
        # Viewer (/stream, /frame): the last processed frame, downscaled with the
        # boxes drawn, as ONE small JPEG, made only while someone watches. A
        # frame buffer kept in memory would ride along in every checkpoint.
        self.view_until = 0.0
        self.view_jpeg = None
        self.view_cond = threading.Condition()
        self.started_at = time.time()
        self.decoded = queue.Queue(maxsize=2)
        self.busy = False          # an inference is running (see quiesce)

    def start(self):
        # Replaces the parent's start(), which loads Ultralytics and PyTorch.
        self.model = OnnxYolo(self.model_name, self.threads)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.socket.bind(self.address)
        self.socket.settimeout(0.5)
        self.last_receive = time.monotonic()
        self.worker = threading.Thread(target=self.receive, daemon=True)
        self.worker.start()
        threading.Thread(target=self.decode, daemon=True).start()
        threading.Thread(target=self.serve_health, daemon=True).start()
        threading.Thread(target=self.trim_heap, daemon=True).start()
        print(f'Model loaded; listening on {self.address}', flush=True)

    def decode(self):
        """Decode complete frames ahead of inference. Blocks when inference
        lags, so the bounded frame queue fills and the receive thread counts
        queue_drops, as in the original."""
        while not self.stopped.is_set():
            try:
                session, fid, data = self.frames.get(timeout=0.5)
            except queue.Empty:
                continue
            bgr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            del data
            if bgr is None:
                print(f'# Frame {fid} failed to decode', flush=True)
                continue
            while not self.stopped.is_set():
                try:
                    self.decoded.put((session, fid, bgr), timeout=0.5)
                    break
                except queue.Full:
                    pass
            # Drop this thread's reference now: otherwise the last decoded frame
            # (1280x720x3, ~2.6 MiB of fresh pixels) stays alive while idle, and
            # every checkpoint ships it in the migration delta.
            del bgr

    def trim_heap(self):
        """Give freed heap pages back to the kernel every TRIM_EVERY seconds.
        Each frame allocates and frees ~100 kB buffers in three threads, and
        glibc keeps the freed pages in its per-thread arenas: dirty memory that
        CRIU checkpoints and the migration ships over the vehicle's 5G uplink.
        TRIM_EVERY=0 turns it off."""
        every = float(os.environ.get('TRIM_EVERY', '2'))
        if every <= 0:
            return
        trim = ctypes.CDLL('libc.so.6').malloc_trim
        while not self.stopped.wait(every):
            trim(0)

    def quiesce(self, cap=2.0):
        """Go idle for a checkpoint: drop every queued or half-assembled frame,
        let the inference in flight finish, then free the per-frame memory.
        DRIFT calls it after stopping the input, so the dump sees an idle,
        small process (~52 MiB instead of ~85) without waiting for the queue to
        drain one inference at a time. Returns the seconds it waited."""
        t0 = time.monotonic()
        def drain(q):
            n = 0
            while True:
                try:
                    q.get_nowait(); n += 1
                except queue.Empty:
                    return n
        while time.monotonic() - t0 < cap:
            dropped = drain(self.frames) + drain(self.decoded)
            pend = self.assembler.pending
            if pend:
                pend.clear(); self.assembler.buffered = 0; dropped += 1
            if not dropped and not self.busy:
                break
            time.sleep(0.02)
        self.view_jpeg = None
        # Trim the idle worker stacks FIRST: reading /proc/self/maps leaves its
        # text in freshly written malloc pages (half of the delta, 2026-10-23).
        # Then free unreachable objects and hand the freed pages back, so the
        # dump sees as few freshly written pages as possible.
        if STACK_TRIM:
            trim_idle_stacks()
        gc.collect()
        ctypes.CDLL('libc.so.6').malloc_trim(0)
        return round(time.monotonic() - t0, 3)

    def receive(self):
        """The original receive loop, minus one leak: its local `frame` kept the
        last completed JPEG (~110 kB) alive while the input is paused, so every
        checkpoint shipped a different one."""
        try:
            while not self.stopped.is_set():
                try:
                    packet, source = self.socket.recvfrom(65535)
                except socket.timeout:
                    self.assembler.expire()
                    continue
                self.last_receive = time.monotonic()
                self.packets += 1
                frame = self.assembler.feed(packet, source)
                del packet
                if frame is not None:
                    try:
                        self.frames.put_nowait(frame)
                    except queue.Full:
                        self.queue_drops += 1
                    del frame
        except Exception as exc:
            self.error = exc
            self.stopped.set()

    def render_view(self, bgr, detections, fid):
        h0, w0 = bgr.shape[:2]
        k = VIEW_W / w0
        img = cv2.resize(bgr, (VIEW_W, round(h0 * k)), interpolation=cv2.INTER_AREA)
        for d in detections:
            x1, y1, x2, y2 = (int(v * k) for v in d['bbox'])
            cv2.rectangle(img, (x1, y1), (x2, y2), (80, 220, 100), 2)
            label = f"{d['class_name']} {d['confidence']:.2f}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
            cv2.rectangle(img, (x1, max(0, y1 - th - 6)), (x1 + tw + 6, y1), (80, 220, 100), -1)
            cv2.putText(img, label, (x1 + 3, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (10, 10, 10), 1, cv2.LINE_AA)
        cv2.putText(img, time.strftime('%H:%M:%S') + f'  frame {fid}', (8, 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
        ok, jpg = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            with self.view_cond:
                self.view_jpeg = jpg.tobytes()
                self.view_cond.notify_all()

    def serve_health(self):
        rx = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith('/quiesce'):
                    body = json.dumps(dict(idle=True, waited_s=rx.quiesce())).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith('/stream'):
                    return self.stream()
                if self.path.startswith('/frame'):
                    rx.view_until = time.monotonic() + VIEW_HOLD
                    jpg = rx.view_jpeg
                    if not jpg:
                        self.send_response(204)
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(jpg)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(jpg)
                    return
                body = json.dumps(dict(
                    ready=True, processed=rx.processed, fps=round(rx.fps, 2),
                    packets=rx.packets, incomplete=rx.assembler.dropped,
                    queue_drops=rx.queue_drops, last_processed_at=rx.last_processed_at,
                    now=time.time(), started_at=rx.started_at, gaps=rx.gaps)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def stream(self):
                """MJPEG of the processed frames, one part per new frame."""
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                last = None
                try:
                    while True:
                        rx.view_until = time.monotonic() + VIEW_HOLD
                        with rx.view_cond:
                            rx.view_cond.wait(timeout=1.0)
                            jpg = rx.view_jpeg
                        if not jpg or jpg is last:
                            continue
                        last = jpg
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        class Srv(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        Srv(("", self.health_port), H).serve_forever()

    def receive_and_process(self, timeout=0):
        stats_started, stats_processed = time.monotonic(), 0
        while not self.stopped.is_set():
            try:
                session, fid, bgr = self.decoded.get(timeout=0.5)
            except queue.Empty:
                continue
            self.busy = True
            try:
                detections = self.model(bgr)
                if time.monotonic() < self.view_until:
                    self.render_view(bgr, detections, fid)
                elif self.view_jpeg is not None:
                    self.view_jpeg = None
                del bgr
                now = time.time()
                if self.last_processed_at and now - self.last_processed_at > GAP_MIN:
                    self.gaps = (self.gaps + [[round(self.last_processed_at, 3),
                                               round(now - self.last_processed_at, 3)]])[-20:]
                self.last_processed_at = now
                if DETECTIONS_LOG:
                    print(json.dumps(dict(session_id=session, frame_id=fid,
                                          processed_at=self.last_processed_at,
                                          detections=detections)), flush=True)
                self.processed += 1
                elapsed = time.monotonic() - stats_started
                if elapsed >= 5:
                    self.fps = (self.processed - stats_processed) / elapsed
                    print(f'# Processed={self.processed} fps={self.fps:.2f} packets={self.packets} '
                          f'incomplete={self.assembler.dropped} queue_drops={self.queue_drops}', flush=True)
                    stats_started, stats_processed = time.monotonic(), self.processed
            except Exception as exc:
                print(f'# Frame {fid} failed: {exc}', flush=True)
            finally:
                self.busy = False
        if self.error:
            raise RuntimeError('UDP receive worker failed') from self.error


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--listen-port', type=int, default=5005)
    p.add_argument('--health-port', type=int, default=9000)
    p.add_argument('--model', default=os.environ.get('MODEL', '/models/yolov8n.onnx'))
    p.add_argument('--threads', type=int, default=int(os.environ.get('INFER_THREADS', '8')))
    a = p.parse_args()
    # Output dir only satisfies the parent's constructor; nothing is written there.
    rx = DriftReceiver('0.0.0.0', a.listen_port, a.model, '/tmp/emt',
                       health_port=a.health_port, threads=a.threads)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: rx.stopped.set())
    try:
        rx.start()
        rx.receive_and_process()
    finally:
        rx.stop()
