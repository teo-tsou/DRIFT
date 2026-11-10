#!/usr/bin/env python3
"""
drift-node-agent — the per-node executor for DRIFT pod live-migration.

Runs as a privileged DaemonSet pod on every node and exposes a tiny token-authed
HTTP API. drift-pod-migrate calls it INSTEAD of SSH, so migrations are driven
entirely through the cluster (native RBAC/service-account reachability, no SSH
keys, scales with the DaemonSet). Every request runs the command in the HOST
namespaces (nsenter -t 1 …) so it behaves exactly like a shell on the node —
crun/runc/criu, crictl, nsenter into pod netns, kubectl, all just work.

  POST /exec   body = shell command; header Authorization: Bearer <token>
               → runs `bash -c <cmd>` on the host; body = stdout,
                 HTTP 200 if it exited 0, else 500. stderr → agent log.
  GET  /healthz

SECURITY: this is host-root command execution — it MUST stay on the internal
network, gated by the shared token (a K8s Secret). Production hardening: mTLS, a
NetworkPolicy restricting callers, and ideally a *typed* migration API instead of
a generic /exec. Do not expose it outside the cluster.
"""
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("DRIFT_AGENT_TOKEN", "")
PORT = int(os.environ.get("DRIFT_AGENT_PORT", "8099"))
# enter the host's mount/uts/ipc/net/pid namespaces, then run the command
NSENTER = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "bash", "-c"]


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok")
        self._send(404)

    def do_POST(self):
        if self.path != "/exec":
            return self._send(404)
        if TOKEN and self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, b"unauthorized")
        n = int(self.headers.get("Content-Length", "0"))
        cmd = self.rfile.read(n).decode("utf-8", "replace")
        try:
            p = subprocess.run(NSENTER + [cmd], capture_output=True, timeout=300)
            out, rc, err = p.stdout, p.returncode, p.stderr
        except subprocess.TimeoutExpired:
            out, rc, err = b"", 124, b"drift-node-agent: command timed out"
        line = f"[exec rc={rc}] {' '.join(cmd.split())[:140]}"
        if err:
            line += f"\n  stderr: {err.decode('utf-8','replace')[:800]}"
        print(line, flush=True)
        # 200 => the command exited 0; anything else => non-zero (caller checks this)
        self._send(200 if rc == 0 else 500, out)

    def log_message(self, *args):
        pass  # quiet; we log execs explicitly


if __name__ == "__main__":
    print(f"drift-node-agent listening on :{PORT} (auth={'on' if TOKEN else 'OFF'})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
