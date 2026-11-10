#!/usr/bin/env python3
# drift-cgroup-strip — normalize a CRIU cgroup.img for cross-HOST restore.
#
# criu records the source container's full cgroup layout, including the cpuset
# controller's special properties (cpuset.cpus/mems). On the SAME host these
# restore fine (the source pod's cgroup path still exists). On a DIFFERENT host
# (a real migration) criu recreates the source pod's cgroup chain from scratch
# and then fails writing cpuset.cpus to the container leaf — the fresh parent
# chain isn't a valid cpuset context — with EACCES, aborting the whole restore
# (-52). Because the restored process is placed in the TARGET container's own
# cgroup (set up by crun with a correct cpuset) anyway, the source's cpuset
# props are redundant. Dropping the cpuset controller from the checkpoint lets
# criu skip it entirely and the restore succeeds.
#
# Usage: drift-cgroup-strip <path/to/cgroup.img>
import json
import os
import subprocess
import sys

STRIP = {"cpuset"}  # controllers whose host-specific props break cross-host restore


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: drift-cgroup-strip <cgroup.img>", file=sys.stderr)
        return 2
    img = sys.argv[1]
    if not os.path.exists(img):
        print(f"no such file: {img}", file=sys.stderr)
        return 1
    js = img + ".json"
    subprocess.run(["crit", "decode", "-i", img, "-o", js], check=True)
    with open(js) as fh:
        doc = json.load(fh)
    removed = 0
    for entry in doc.get("entries", []):
        keep = []
        for ctl in entry.get("controllers", []):
            if STRIP & set(ctl.get("cnames") or []):
                removed += 1
            else:
                keep.append(ctl)
        entry["controllers"] = keep
        for cg_set in entry.get("sets", []):
            cg_set["ctls"] = [
                c for c in cg_set.get("ctls", []) if (c.get("name") or "") not in STRIP
            ]
    with open(js, "w") as fh:
        json.dump(doc, fh)
    subprocess.run(["crit", "encode", "-i", js, "-o", img], check=True)
    os.remove(js)
    print(f"drift-cgroup-strip: removed {removed} controller(s) {sorted(STRIP)} from {img}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
