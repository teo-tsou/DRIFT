#!/usr/bin/env python3
"""ckpt_churn.py BASE_DIR NEW_DIR: which memory regions cost the migration delta.

For every region (VMA) of the new image: pages changed vs the base at the same
address, how many bytes inside those pages differ (a few = refcount/pointer
churn, most = a rewritten buffer), and the zstd --patch-from size of the
changed pages against their old versions, i.e. that region's share of the bytes
on the wire. Plus the longest readable strings in the top regions' changed
pages, to tell what lives there. Needs numpy and the zstd CLI.
"""
import os
import re
import subprocess
import sys
import tempfile
import types

import numpy as np

PD = types.ModuleType("pd")
exec(open("/usr/local/bin/drift-pagedelta").read().split("if __name__")[0], PD.__dict__)
PAGE = 4096
VMA_STACK, VMA_HEAP, VMA_FILE = 1 << 1, 1 << 5, (1 << 6) | (1 << 7)


def vmas(final):
    """[(start, end, label)] from mm-1.img (parsed like drift-pagedelta does)."""
    b = open(os.path.join(final, "mm-1.img"), "rb").read()
    import struct
    n = struct.unpack_from("<I", b, 8)[0]
    mm = PD._fields(b[12:12 + n])
    out = []
    # field 14 of mm_entry = repeated vma_entry (start 1, end 2, shmid 4, status 7)
    i, raw = 0, b[12:12 + n]
    while i < len(raw):
        key, i = PD._varint(raw, i)
        num, wt = key >> 3, key & 7
        if wt == 2:
            ln, i = PD._varint(raw, i)
            if num == 14:
                v = PD._fields(raw[i:i + ln])
                st = v.get(7, 0)
                lab = "[heap]" if st & VMA_HEAP else "[stack]" if st & VMA_STACK else \
                      "file" if st & VMA_FILE else f"anon {(v[2] - v[1]) >> 20} MiB"
                out.append((v[1], v[2], lab))
            i += ln
        elif wt == 0:
            _, i = PD._varint(raw, i)
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
    return sorted(out)


def zsize(new, old):
    with tempfile.TemporaryDirectory(dir="/var/lib/drift-ckpt") as t:
        r, n = os.path.join(t, "r"), os.path.join(t, "n")
        (old if len(old) else np.zeros(1, np.uint8)).tofile(r); new.tofile(n)
        return len(subprocess.run(["zstd", "-3", "-q", "-c", f"--patch-from={r}", n],
                                  stdout=subprocess.PIPE, check=True).stdout)


def main():
    base, new = sys.argv[1:3]
    bva, bname = PD.pages(base); bp = PD.load(base, bname, len(bva))
    nva, nname = PD.pages(new); npg = PD.load(new, nname, len(nva))
    bi = PD.match(bva, nva)
    same = PD.same_pages(bp, npg, bi)
    regs = vmas(new)
    starts = np.array([r[0] for r in regs], np.uint64)
    ri = np.searchsorted(starts, nva, side="right") - 1
    rows = []
    for k, (s, e, lab) in enumerate(regs):
        sel = (ri == k) & ~same
        if not sel.any():
            continue
        chg = sel & (bi >= 0)
        new_pages = npg[sel]
        old = bp[bi[chg]]
        nb = (npg[chg] != old).sum(axis=1) if chg.any() else np.zeros(0)
        rows.append((zsize(new_pages, old), lab, hex(s), int(sel.sum()), int(chg.sum()),
                     float(np.median(nb)) if len(nb) else 0, int((nb > PAGE // 2).sum()), k))
    rows.sort(reverse=True)
    tot = sum(r[0] for r in rows)
    print(f"{'wire':>9} {'share':>6} {'pages':>6} {'chg':>5} {'med diff B':>10} {'>50%':>5}  region")
    for z, lab, s, n, c, med, full, _ in rows[:14]:
        print(f"{z:9d} {100 * z / tot:5.1f}% {n:6d} {c:5d} {med:10.0f} {full:5d}  {lab} @{s}")
    print(f"total (per region, sum) {tot} bytes")
    for z, lab, s, n, c, med, full, k in rows[:4]:
        sel = (ri == k) & ~same
        blob = npg[sel].tobytes()
        strs = sorted(set(m.group().decode() for m in re.finditer(rb"[ -~]{12,}", blob)), key=len, reverse=True)
        print(f"\n-- {lab} @{s}: sample strings ({len(strs)} distinct):")
        for t in strs[:8]:
            print("   ", t[:110])


if __name__ == "__main__":
    main()
