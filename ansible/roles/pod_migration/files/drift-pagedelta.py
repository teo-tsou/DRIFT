#!/usr/bin/env python3
"""drift-pagedelta: page-level delta of a CRIU image against a base image.

  drift-pagedelta encode BASE_DIR NEW_DIR [GO_FILE]  > stream   (source)
  drift-pagedelta decode BASE_DIR OUT_DIR             < stream   (target)

Both load and index the base BEFORE they need the new image: start them ahead
of the freeze (encode waits for GO_FILE to appear, decode for the stream), and
the freeze only pays for comparing, compressing and reassembling.

A migration's checkpoint differs from the previous one (the base, on both
nodes) in ~1-2k of its ~16k pages. `tar | zstd --patch-from=<base pages>`
finds them by indexing the whole ~60 MiB base, which costs the vehicle's CPU
~0.5 s inside the freeze. Here the pagemap says where every page lives:
  1. compare each page of the new image with the base page at the SAME virtual
     address (numpy, all pages at once): same / changed / new;
  2. compress only the changed and new pages, with `zstd --patch-from` against
     the OLD versions of the changed pages (a few MiB instead of the whole
     base), so moved or partly rewritten data still matches.
The stream = header, the other (small) image files, one kind byte per page,
the zstd payload. The target rebuilds the reference from its own base copy,
decompresses and reassembles pages-<id>.img. Both ends compute a CRC32 of
their base while warming up and the target refuses a stream made against a
different base, so a wrong base can never restore a wrong image (the payload
itself carries zstd's checksum).

No pycriu: its protobuf import alone costs more than the delta. The pagemap
image is parsed directly (CRIU image = magics, then entries as a 32-bit size +
protobuf message; pagemap head {1: pages_id}, entries {1: vaddr,
2: compat_nr_pages (0 since CRIU 4), 3: in_parent, 4: flags, 5: nr_pages}).
"""
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import zlib

import numpy as np

PAGE = 4096
PE_PRESENT = 4
SAME, CHANGED, NEW = 0, 1, 2
ZSTD_LEVEL = os.environ.get("DRIFT_PAGEDELTA_ZSTD", "3")


def _varint(b, i):
    v = s = 0
    while True:
        c = b[i]; i += 1
        v |= (c & 0x7F) << s
        if c < 0x80:
            return v, i
        s += 7


def _fields(msg):
    i, out = 0, {}
    while i < len(msg):
        key, i = _varint(msg, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            out[num], i = _varint(msg, i)
        elif wt == 1:
            out[num] = struct.unpack_from("<Q", msg, i)[0]; i += 8
        elif wt == 2:
            n, i = _varint(msg, i); out[num] = msg[i:i + n]; i += n
        elif wt == 5:
            out[num] = struct.unpack_from("<I", msg, i)[0]; i += 4
        else:
            raise ValueError(f"wire type {wt}")
    return out


def pages(final):
    """(vaddrs, pages file name): the virtual address of every stored page, in
    pages-file order."""
    if sum(f.startswith("pagemap-") for f in os.listdir(final)) != 1:
        raise SystemExit("drift-pagedelta: one process per image only")
    b = open(os.path.join(final, "pagemap-1.img"), "rb").read()
    i = 8                                    # common magic + pagemap magic
    entries = []
    while i < len(b):
        n = struct.unpack_from("<I", b, i)[0]; i += 4
        entries.append(_fields(b[i:i + n])); i += n
    vas = []
    for e in entries[1:]:
        if e.get(4, PE_PRESENT) & PE_PRESENT and not e.get(3):
            n = e.get(5) or e.get(2, 0)
            vas.append(np.uint64(e[1]) + np.arange(n, dtype=np.uint64) * np.uint64(PAGE))
    va = np.concatenate(vas) if vas else np.zeros(0, np.uint64)
    return va, f"pages-{entries[0].get(1, 1)}.img"


def load(final, name, n):
    a = np.fromfile(os.path.join(final, name), dtype=np.uint8)
    if a.size != n * PAGE:
        raise SystemExit(f"drift-pagedelta: {name} has {a.size} bytes, pagemap says {n} pages")
    return a.reshape(n, PAGE)


def match(base_va, new_va):
    """index into the base pages for each new page, -1 where the base has none."""
    order = np.argsort(base_va)
    pos = np.searchsorted(base_va[order], new_va).clip(0, max(len(base_va) - 1, 0))
    idx = order[pos] if len(base_va) else np.zeros(len(new_va), np.int64)
    hit = (base_va[idx] == new_va) if len(base_va) else np.zeros(len(new_va), bool)
    return np.where(hit, idx, -1)


def same_pages(bp, np_, bi):
    """True where a new page equals its base page, compared in place (no copy of
    the 60 MiB): split the new image into runs that map onto consecutive base
    pages, about one per memory region, and compare each pair of slices as
    64-bit words."""
    same = np.zeros(len(bi), bool)
    hit = bi >= 0
    if not hit.any():
        return same
    # a run breaks where the base index does not continue +1, or a hit starts/ends
    brk = np.flatnonzero((np.diff(bi) != 1) | (hit[1:] != hit[:-1])) + 1
    b64, n64 = bp.view(np.uint64), np_.view(np.uint64)
    for lo, hi in zip(np.r_[0, brk], np.r_[brk, len(bi)]):
        if hit[lo]:
            s0 = bi[lo]
            same[lo:hi] = (b64[s0:s0 + hi - lo] == n64[lo:hi]).all(axis=1)
    return same


def zstd(args, data_in, ref, tmp):
    rf, inf = os.path.join(tmp, "ref"), os.path.join(tmp, "in")
    ref.tofile(rf)
    with open(inf, "wb") as f:
        f.write(data_in)
    return subprocess.run(["zstd", "-q", "-c", "-T0", f"--patch-from={rf}", *args, inf],
                          check=True, stdout=subprocess.PIPE).stdout


def wait_for(path, limit=900):
    t0 = time.monotonic()
    while not os.path.exists(path):
        if time.monotonic() - t0 > limit:
            raise SystemExit(f"drift-pagedelta: {path} never appeared")
        time.sleep(0.005)


def encode(base, new, go=None):
    out = sys.stdout.buffer
    bva, bname = pages(base)
    bp = load(base, bname, len(bva))
    base_crc = zlib.crc32(bp)
    if go:
        wait_for(go)                                   # the dump is complete
    nva, nname = pages(new)
    np_ = load(new, nname, len(nva))
    bi = match(bva, nva)
    kinds = np.full(len(nva), NEW, np.uint8)
    hit = bi >= 0
    same = same_pages(bp, np_, bi)
    kinds[hit & same] = SAME
    kinds[hit & ~same] = CHANGED
    send = kinds != SAME
    ref = bp[bi[kinds == CHANGED]]                      # old versions, in new-file order
    with tempfile.TemporaryDirectory(dir=os.environ.get("DRIFT_PAGEDELTA_TMP")) as tmp:
        payload = zstd([f"-{ZSTD_LEVEL}"], np_[send].tobytes(), ref if len(ref) else np.zeros(1, np.uint8), tmp)
    small = sorted(f for f in os.listdir(new) if f != nname)
    if any(os.path.isdir(os.path.join(new, f)) for f in small):
        raise SystemExit("drift-pagedelta: image has subdirectories; not supported")
    head = {"pages": nname, "npages": int(len(nva)), "base_crc": base_crc,
            "payload": len(payload),
            "files": [[f, os.path.getsize(os.path.join(new, f))] for f in small]}
    hb = json.dumps(head).encode()
    out.write(struct.pack("<I", len(hb))); out.write(hb)
    for f, _ in head["files"]:
        with open(os.path.join(new, f), "rb") as fh:
            out.write(fh.read())
    out.write(kinds.tobytes())
    out.write(payload)
    out.flush()
    sys.stderr.write(f"drift-pagedelta: {len(nva)} pages: {int((kinds == CHANGED).sum())} changed, "
                     f"{int((kinds == NEW).sum())} new; payload {len(payload)} bytes\n")


def _read(f, n):
    b = f.read(n)
    if len(b) != n:
        raise SystemExit("drift-pagedelta: stream ended early")
    return b


def decode(base, outdir):
    f = sys.stdin.buffer
    bva, bname = pages(base)                           # ready before the stream starts
    bp = load(base, bname, len(bva))
    base_crc = zlib.crc32(bp)
    head = json.loads(_read(f, struct.unpack("<I", _read(f, 4))[0]))
    if head["base_crc"] != base_crc:
        raise SystemExit("drift-pagedelta: the stream was made against a different base")
    os.makedirs(outdir, exist_ok=True)
    for name, size in head["files"]:
        with open(os.path.join(outdir, name), "wb") as o:
            o.write(_read(f, size))
    kinds = np.frombuffer(_read(f, head["npages"]), np.uint8)
    payload = _read(f, head["payload"])
    nva, nname = pages(outdir)                       # the new pagemap just arrived
    if len(nva) != len(kinds):
        raise SystemExit("drift-pagedelta: pagemap and stream disagree")
    bi = match(bva, nva)
    if (bi[kinds != NEW] < 0).any():
        raise SystemExit("drift-pagedelta: base lacks pages the stream expects (wrong base?)")
    ref = bp[bi[kinds == CHANGED]]
    with tempfile.TemporaryDirectory(dir=os.environ.get("DRIFT_PAGEDELTA_TMP")) as tmp:
        pf = os.path.join(tmp, "payload")
        with open(pf, "wb") as fh:
            fh.write(payload)
        rf = os.path.join(tmp, "ref")
        (ref if len(ref) else np.zeros(1, np.uint8)).tofile(rf)
        sent = subprocess.run(["zstd", "-q", "-d", "-c", f"--patch-from={rf}", pf],
                              check=True, stdout=subprocess.PIPE).stdout
    out = np.empty((len(nva), PAGE), np.uint8)
    same = kinds == SAME
    out[same] = bp[bi[same]]
    out[~same] = np.frombuffer(sent, np.uint8).reshape(-1, PAGE)
    out.tofile(os.path.join(outdir, head["pages"]))


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["encode"] and len(a) in (3, 4):
        encode(*a[1:])
    elif a[:1] == ["decode"] and len(a) == 3:
        decode(*a[1:])
    else:
        raise SystemExit(__doc__)
