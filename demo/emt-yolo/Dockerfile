# DRIFT build of the EMT UDP YOLO workload: one image for the receiver (the
# migrating pod) and the sender (the dashcam). Build it for the node's
# architecture (arm64 on the Jetsons). The model is baked in because the
# migration target gets only this image, no volumes or downloads.
# Build context: the workload repo's udp_*.py plus this directory's files.

# Stage 1: export the models to ONNX. PyTorch and Ultralytics stay in this stage.
# YOLO26n (the default, MODEL below) is NMS-free: its graph ends in the final
# boxes, so the receiver skips its own NMS. YOLOv8n is kept for comparison.
FROM python:3.11-slim AS export
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir ultralytics onnx onnxslim
WORKDIR /models
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt').export(format='onnx', imgsz=640, simplify=True)"
# end2end=True: the one-to-one head, output (1, 300, 6) = x1 y1 x2 y2 score class.
# Without it Ultralytics 8.4 exports the (1, 84, 8400) one-to-many head (v8-style).
RUN python -c "from ultralytics import YOLO; YOLO('yolo26n.pt').export(format='onnx', imgsz=640, simplify=True, end2end=True)" \
    && python -c "import onnx; m=onnx.load('yolo26n.onnx'); print('yolo26n outputs:', [(o.name, [d.dim_value for d in o.type.tensor_type.shape.dim]) for o in m.graph.output])"

# Stage 2: the runtime. ONNX Runtime + OpenCV only: no PyTorch in the process,
# so ~150 MB less memory for CRIU to copy at every migration.
FROM python:3.11-slim
RUN pip install --no-cache-dir onnxruntime opencv-python-headless numpy pillow
COPY --from=export /models/yolov8n.onnx /models/yolo26n.onnx /models/
COPY udp_protocol.py udp_yolo_receiver.py udp_streaming_server.py drift_receiver.py drift_sender.py /app/
WORKDIR /app
# Large buffers (decoded frames) are mmap'ed, so freeing one returns it to the
# kernel instead of leaving the heap, and the checkpoint, larger.
ENV MODEL=/models/yolo26n.onnx
ENV ORT_DISABLE_TELEMETRY=1 DETECTIONS_LOG=0
ENV PYTHONUNBUFFERED=1 MALLOC_MMAP_THRESHOLD_=65536 MALLOC_TRIM_THRESHOLD_=1048576 MALLOC_ARENA_MAX=2
CMD ["python3", "/app/drift_receiver.py"]
