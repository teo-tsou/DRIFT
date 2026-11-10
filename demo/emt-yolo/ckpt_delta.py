#!/usr/bin/env python3
"""ckpt_delta.py BASE_DIR NEW_DIR: what changed between two CRIU images of the
same process, page by page, grouped by memory region.

The delta a migration ships (zstd --patch-from the previous image) is roughly
the pages whose content changed, so this tells what to free or keep stable in
/quiesce to make it smaller. Regions are named from the image's mm (VMAs) and
reg-files (file mappings); anonymous ones show their flags and size.
"""
import collections
import os
import sys

import pycriu

PAGE = 4096
PE_PRESENT = 4                      # pagemap flags: data is in pages-N.img
VMA_STACK, VMA_HEAP = 1 << 1, 1 << 5
VMA_FILE_PRIVATE, VMA_FILE_SHARED = 1 << 6, 1 << 7


def load(d, name):
    with open(os.path.join(d, name), "rb") as f:
        return pycriu.images.load(f)["entries"]


def pages_of(d, pid):
    """{vaddr: bytes} for every dumped page of pid (pagemap order = pages file order)."""
    pm = load(d, f"pagemap-{pid}.img")
    pages_id = pm[0]["pages_id"]
    out = {}
    with open(os.path.join(d, f"pages-{pages_id}.img"), "rb") as f:
        for e in pm[1:]:
            va, n = e["vaddr"], e["nr_pages"]
            if not e.get("flags", 0) & PE_PRESENT:
                continue            # lazy / parent-only entries carry no data here
            for i in range(n):
                out[va + i * PAGE] = f.read(PAGE)
    return out


def regions(d, pid):
    """[(start, end, label)] from mm-<pid>.img, labelled with file names."""
    files = {}
    try:
        for e in load(d, "files.img"):
            reg = e.get("reg")
            if reg:
                files[e["id"]] = reg.get("name", "?")
    except FileNotFoundError:
        pass
    out = []
    for v in load(d, f"mm-{pid}.img")[0]["vmas"]:
        s, e, status = v["start"], v["end"], v.get("status", 0)
        if status & VMA_HEAP:
            label = "[heap]"
        elif status & VMA_STACK:
            label = "[stack]"
        elif status & (VMA_FILE_PRIVATE | VMA_FILE_SHARED):
            label = os.path.basename(files.get(v.get("shmid"), "file?"))
        else:
            label = f"anon {(e - s) >> 20} MiB" if e - s >= 1 << 20 else "anon <1 MiB"
        out.append((s, e, label))
    return sorted(out)


def label_for(regs, va):
    lo, hi = 0, len(regs)
    while lo < hi:
        mid = (lo + hi) // 2
        if regs[mid][1] <= va:
            lo = mid + 1
        else:
            hi = mid
    if lo < len(regs) and regs[lo][0] <= va < regs[lo][1]:
        return regs[lo][2]
    return "unmapped?"


def main():
    base, new = sys.argv[1], sys.argv[2]
    pids = sorted(int(f[8:-4]) for f in os.listdir(new) if f.startswith("pagemap-") and f.endswith(".img"))
    total = collections.Counter()
    for pid in pids:
        nb = pages_of(new, pid)
        try:
            bb = pages_of(base, pid)
        except FileNotFoundError:
            bb = {}
        regs = regions(new, pid)
        for va, data in nb.items():
            old = bb.get(va)
            if old is None:
                total[("new page", label_for(regs, va))] += 1
            elif old != data:
                if data == b"\0" * PAGE:
                    total[("zeroed", label_for(regs, va))] += 1
                else:
                    total[("changed", label_for(regs, va))] += 1
        gone = len(set(bb) - set(nb))
        if gone:
            total[("freed since base", "-")] += gone
        print(f"pid {pid}: {len(nb)} pages now, {len(bb)} in base")
    kinds = collections.Counter()
    for (kind, _), n in total.items():
        kinds[kind] += n
    print("\nby kind:", {k: f"{v} pages = {v * PAGE / 1048576:.2f} MiB" for k, v in kinds.items()})
    print("\ntop regions (changed + new pages):")
    per = collections.Counter()
    for (kind, lab), n in total.items():
        if kind in ("changed", "new page"):
            per[lab] += n
    for lab, n in per.most_common(15):
        print(f"  {n * PAGE / 1048576:7.2f} MiB  {lab}")


if __name__ == "__main__":
    main()
