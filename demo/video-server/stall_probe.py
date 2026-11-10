#!/usr/bin/env python3
"""UE-side stall probe for the follow-me demo (KPI-2 continuity, KPI-3 downtime).

Streams the video over one TCP connection at a player-like rate and logs every
gap between successful reads. A migration cutover shows up as the largest gap;
a connection reset counts as a session-continuity failure (KPI-2).

Run it at the measurement vantage point (hub for the wired baseline, the Pi —
bound out over wwan0 — for the UE-over-5G number) while a migration runs:

    python3 stall_probe.py --host 10.220.3.21 --port 9000 --path /loop.webm \
        --rate-kbps 3200 --gap-ms 250 --out stall_log.csv

Stdlib only. Ctrl-C prints the summary.
"""
import argparse, csv, socket, sys, time
from datetime import datetime, timezone


def iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(),
                                  tz=timezone.utc).isoformat(timespec="milliseconds")


def stream_once(args, writer, stats):
    """One GET over one fresh TCP connection, read at the target rate.
    Returns 'eof' (clean end of body), 'reset' (RST/abort), or 'timeout'."""
    chunk = 16384
    interval = chunk / (args.rate_kbps * 1024 / 8)  # seconds per chunk at target rate
    s = socket.create_connection((args.host, args.port), timeout=args.conn_timeout)
    if args.bind:
        pass  # bind must happen pre-connect; kept for doc — use --bind-addr instead
    s.settimeout(args.read_timeout)
    req = (f"GET {args.path} HTTP/1.1\r\nHost: {args.host}\r\n"
           "User-Agent: drift-stall-probe\r\nConnection: close\r\n\r\n")
    s.sendall(req.encode())
    # carry the gap timer across passes so a stall hiding in a reconnect
    # between two GETs is still measured
    last = stats.get("last") or time.monotonic()
    header_done = False
    body_bytes = 0
    outcome = "eof"
    try:
        while True:
            try:
                data = s.recv(chunk)
            except socket.timeout:
                gap = time.monotonic() - last
                print(f"{iso()} READ-TIMEOUT after {gap:.1f}s silence", flush=True)
                stats["gaps"].append((time.time(), gap, "timeout"))
                writer.writerow([iso(), f"{gap*1000:.0f}", "timeout", body_bytes])
                outcome = "timeout"
                break
            except ConnectionResetError:
                outcome = "reset"
                break
            now = time.monotonic()
            gap = now - last
            last = stats["last"] = now
            if not data:
                break  # clean EOF (end of clip)
            if not header_done:
                header_done = True  # first packet carries the response header
            body_bytes += len(data)
            stats["bytes"] += len(data)
            if gap >= args.gap_ms / 1000.0:
                print(f"{iso()} GAP {gap*1000:.0f} ms  ({body_bytes} bytes into pass)",
                      flush=True)
                stats["gaps"].append((time.time(), gap, "gap"))
                writer.writerow([iso(), f"{gap*1000:.0f}", "gap", body_bytes])
            time.sleep(interval)
    finally:
        try:
            s.close()
        except OSError:
            pass
    stats["passes"] += 1
    if outcome == "reset":
        stats["resets"] += 1
        print(f"{iso()} CONNECTION RESET after {body_bytes} bytes "
              f"(session-continuity failure)", flush=True)
        writer.writerow([iso(), "", "reset", body_bytes])
    return outcome


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--path", default="/loop.webm")
    ap.add_argument("--rate-kbps", type=float, default=3200,
                    help="player-like consumption rate (default 3.2 Mbps)")
    ap.add_argument("--gap-ms", type=float, default=250,
                    help="log gaps at or above this (default 250 ms)")
    ap.add_argument("--read-timeout", type=float, default=30,
                    help="give up on a silent socket after this many s")
    ap.add_argument("--conn-timeout", type=float, default=10)
    ap.add_argument("--duration", type=float, default=0,
                    help="stop after N seconds (0 = until Ctrl-C)")
    ap.add_argument("--out", default="stall_log.csv")
    ap.add_argument("--bind", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    stats = {"gaps": [], "resets": 0, "passes": 0, "bytes": 0}
    t0 = time.monotonic()
    print(f"{iso()} probe start -> {args.host}:{args.port}{args.path} "
          f"@ {args.rate_kbps} kbps, gap threshold {args.gap_ms} ms", flush=True)
    with open(args.out, "a", newline="") as f:
        writer = csv.writer(f)
        if f.tell() == 0:
            writer.writerow(["utc", "gap_ms", "kind", "bytes_into_pass"])
        try:
            while True:
                if args.duration and time.monotonic() - t0 > args.duration:
                    break
                try:
                    stream_once(args, writer, stats)
                except OSError as e:
                    print(f"{iso()} CONNECT FAILED: {e} — retrying in 1s", flush=True)
                    stats["gaps"].append((time.time(), None, "connect-fail"))
                    writer.writerow([iso(), "", "connect-fail", 0])
                    time.sleep(1)
                f.flush()
        except KeyboardInterrupt:
            pass

    gaps = [g for _, g, k in stats["gaps"] if g is not None]
    print("\n===== SUMMARY =====")
    print(f"passes={stats['passes']} bytes={stats['bytes']} resets={stats['resets']} "
          f"connect_failures={sum(1 for *_ , k in stats['gaps'] if k=='connect-fail')}")
    if gaps:
        print(f"gaps>= {args.gap_ms} ms: n={len(gaps)} max={max(gaps)*1000:.0f} ms "
              f"(this max is the measured client stall / KPI-3)")
    else:
        print("no gaps above threshold — stall below "
              f"{args.gap_ms} ms at this vantage point")
    print(f"log: {args.out}")


if __name__ == "__main__":
    main()
