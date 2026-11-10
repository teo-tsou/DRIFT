#!/usr/bin/env python3
"""Frames lost across a YOLO migration, from the receiver's own /health.

Polls every URL given (the YOLO pod on the vehicle and the edge's floating IP)
at --hz and keeps the newest answer. The receiver's counters live in process
memory, so they migrate with it: a migration shows as a stall in `processed`
and in `last_processed_at`. Run on the vehicle Jetson's host, which reaches
both its own pod IP and the edge floating IP.

  measure_gap.py --duration 120 --csv run.csv http://10.48.0.22:9000 http://10.220.2.244:9000
  measure_gap.py --duration 200 --follow-target          # poll wherever the DNAT points

--follow-target reads drift-yolo-target's state file each poll, so it finds a
YOLO pod whose address is only known after the migration created it.

Reports the longest gap between processed frames and the frames lost in it
(gap x sender FPS - 1), next to the steady-state processing rate.
"""
import argparse
import csv
import json
import time
import urllib.request


def health(url, timeout):
    try:
        with urllib.request.urlopen(url.rstrip('/') + '/health', timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('urls', nargs='*')
    p.add_argument('--follow-target', action='store_true')
    p.add_argument('--target-file', default='/run/drift-yolo-target')
    p.add_argument('--duration', type=float, default=120)
    p.add_argument('--hz', type=float, default=10)
    p.add_argument('--sender-fps', type=float, default=12)
    p.add_argument('--csv')
    a = p.parse_args()

    rows, last = [], None
    end = time.time() + a.duration
    while time.time() < end:
        t = time.time()
        urls = list(a.urls)
        if a.follow_target:
            try:
                urls.append(f"http://{open(a.target_file).read().split()[1]}:9000")
            except (OSError, IndexError):
                pass
        urls = list(dict.fromkeys(urls))
        answers = []
        for u in urls:                     # request time = the app's response latency from here
            t_req = time.time()
            h = health(u, 0.8 / a.hz * max(1, len(urls)))
            if h:
                answers.append((u, h, round(1000 * (time.time() - t_req), 1)))
        if answers:
            url, h, rtt = max(answers, key=lambda x: x[1]['processed'])
            row = dict(t=round(t, 3), url=url, rtt_ms=rtt, processed=h['processed'],
                       last_processed_at=h.get('last_processed_at'), fps=h.get('fps'),
                       packets=h.get('packets'), incomplete=h.get('incomplete'))
        else:
            row = dict(t=round(t, 3), url='', rtt_ms=None, processed=None, last_processed_at=None,
                       fps=None, packets=None, incomplete=None)
        rows.append(row)
        if row['url'] != (last or {}).get('url'):
            print(f"{time.strftime('%H:%M:%S')} serving from {row['url'] or 'NOWHERE'}", flush=True)
        last = row
        time.sleep(max(0, 1 / a.hz - (time.time() - t)))

    if a.csv:
        with open(a.csv, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)

    # Gap = longest stretch where `processed` did not move, between two polls
    # that both answered (a stall seen from outside, wherever the pod lived).
    seen = [r for r in rows if r['processed'] is not None]
    gap, gap_at, start = 0.0, None, seen[0] if seen else None
    for prev, cur in zip(seen, seen[1:]):
        if cur['processed'] != prev['processed']:
            if cur['t'] - start['t'] > gap:
                gap, gap_at = cur['t'] - start['t'], start['t']
            start = cur
    span = seen[-1]['t'] - seen[0]['t'] if len(seen) > 1 else 0
    done = seen[-1]['processed'] - seen[0]['processed'] if len(seen) > 1 else 0
    print(json.dumps(dict(
        polls=len(rows), answered=len(seen),
        steady_fps=round((done) / span, 2) if span else None,
        longest_gap_s=round(gap, 2),
        gap_started=time.strftime('%H:%M:%S', time.localtime(gap_at)) if gap_at else None,
        frames_lost=max(0, round(gap * a.sender_fps) - 1),
        incomplete_delta=(seen[-1]['incomplete'] - seen[0]['incomplete']) if len(seen) > 1 else None)))


if __name__ == '__main__':
    main()
