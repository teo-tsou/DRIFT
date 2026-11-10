#!/usr/bin/env python3
"""
Range-capable HTTP video server — stdlib only, so it is CRIU-clean:
no runtime downloads, no pip, no external libs. The clip it serves is baked
into the image at build time (see Dockerfile), so the migration target — a
fresh copy of the SAME image — already has the file, and the restored process
keeps serving it. The pod's floating IP follows across migrations, so the
UE/proxy keeps hitting the same URL and the browser <video> just stalls briefly
at the cutover, then resumes.
"""
import http.server
import os
import re
import socketserver

VIDEO = os.environ.get("VIDEO_PATH", "/media/loop.mp4")
PORT = int(os.environ.get("PORT", "9000"))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):  # quiet
        pass

    def do_GET(self):
        try:
            size = os.path.getsize(VIDEO)
        except OSError:
            self.send_error(404, "no video")
            return
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            end = int(m.group(2)) if m and m.group(2) else size - 1
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            start, end = 0, size - 1
            self.send_response(200)
        length = end - start + 1
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Type", "video/mp4")
        self.end_headers()
        with open(VIDEO, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    Server(("", PORT), Handler).serve_forever()
