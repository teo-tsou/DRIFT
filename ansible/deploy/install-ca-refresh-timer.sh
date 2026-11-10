#!/bin/bash
# OPTIONAL / belt-and-suspenders. The DRIFT backend now establishes fresh
# cross-cluster CA trust as step 0 of every migration (see
# backend/cross_cluster_migration_controller.py::_ensure_ca_trust), guarded by a
# freshness check. This hub-side timer is no longer required; install it only if
# you want CA trust kept warm out-of-band as well.
#
# Install the hub-side DRIFT cross-cluster CA-refresh systemd timer.
# Run on the Central Hub node (where the drift-backend container runs).
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -m 0644 "$HERE/drift-xcluster-ca-refresh.service" /etc/systemd/system/
install -m 0644 "$HERE/drift-xcluster-ca-refresh.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now drift-xcluster-ca-refresh.timer
echo "Installed. Next runs:"
systemctl list-timers drift-xcluster-ca-refresh.timer --no-pager
