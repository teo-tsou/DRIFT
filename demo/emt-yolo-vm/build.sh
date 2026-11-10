#!/bin/sh
# Build the EMT YOLO containerDisk on the arm64 node and side-load it into CRI-O
# (no registry). Run as root in this directory on the Orin.
set -eu
TAG=${TAG:-localhost/drift-emt-yolo-vm:latest}
# the app image lives in CRI-O; Docker builds FROM it
docker image inspect drift-emt-yolo:udp >/dev/null 2>&1 || \
    skopeo copy containers-storage:localhost/drift-emt-yolo:udp docker-daemon:drift-emt-yolo:udp
docker build -t "$TAG" .
skopeo copy docker-daemon:"$TAG" containers-storage:"$TAG"
crictl images | grep drift-emt-yolo-vm
