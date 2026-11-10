# DRIFT node requirements for migration services

Scope: what a node must provide for DRIFT to relocate a live workload onto or off
it. Written for the final testing nodes, including ARM64 (NVIDIA Jetson / L4T),
where several of these are kernel build-time options rather than packages.

Verify any node with:

```bash
sudo ./scripts/drift-preflight.sh check <peer_ip>
```

It prints a pass/warn/fail table for every gate below and names the exact kernel
symbols to add if a rebuild is needed.

DRIFT runs two independent migration engines with different requirements:

| Engine | What moves | Needs |
|---|---|---|
| CRIU container migration | a running container, with its TCP sessions | kernel checkpoint/restore support, CRI-O, CRIU |
| KubeVirt live migration | a full VM | hardware virtualisation (`/dev/kvm`), RWX storage |

A node can serve one, both, or neither. On Jetson the container engine is the
realistic target and the VM engine is the problematic one (section 4).

## 0. Summary: what requires a kernel rebuild

Measured on jetson-orin-01 (L4T R39.2.1), the stock kernel is missing
`CHECKPOINT_RESTORE`, `UNIX_DIAG`, `INET_UDP_DIAG`, `PACKET_DIAG`,
`NETLINK_DIAG`, `USERFAULTFD`, `OPENVSWITCH` and `ISCSI_TCP`. So the rebuild is
confirmed necessary, not precautionary. `USERFAULTFD` is the critical one: it
carries post-copy, and since ARM64 cannot do pre-copy either (section 3.1), a
kernel without it leaves only a blocking checkpoint whose freeze grows with the
workload's memory.

On a stock JetPack / L4T kernel, expect to rebuild for:

1. `CONFIG_CHECKPOINT_RESTORE` and the socket diag family (section 2.1). Without
   these CRIU cannot checkpoint a container at all.
2. `CONFIG_OPENVSWITCH` and `CONFIG_NF_TABLES` (section 2.2), for kube-ovn and
   for CRIU's network lock during the freeze.
3. `CONFIG_NFS_V4` and `CONFIG_ISCSI_TCP` (section 2.3), for the shared
   checkpoint store and for Longhorn RWX volumes.
4. A device tree patch, only if the VM path is wanted on these nodes (section 4).

Two requirements cannot be met by rebuilding, and the design has to absorb them
instead (section 3): iterative pre-copy does not exist on ARM64, and a workload
cannot migrate between an x86 node and an ARM node.

## 1. Target topology and CPU architecture

Agreed topology for final testing:

* **Central Hub: x86_64 or ARM64.** The hub runs only Docker containers and
  never hosts a migrated workload, so its architecture is free. Its images must
  build on whichever it is, which the repo now does (section 8).
* **All edge nodes: ARM64 (Jetson).** No x86 edge nodes.

A CRIU checkpoint is machine code, register state and raw memory pages, so it
restores only on a node with the same CPU architecture and the same page size.
The same applies to KubeVirt live migration. With an all-ARM edge this is
satisfied by construction, and it also means:

* Any edge node can be the migration peer of any other edge node. There is no
  pairing rule to enforce at the selector, and no mixed-architecture trap.
* Container migration still needs **two edge nodes**, since a migration has a
  source and a target.
* Page size must match across the pair. L4T uses 4 KB pages, so a uniform
  JetPack estate is consistent. Check with `getconf PAGESIZE` if any edge node
  ever runs a non-L4T ARM distro, since some ARM server images use 64 KB pages
  and would be incompatible even with another ARM node.
* The trap that remains is the *hub* being x86 while people assume it can stand
  in as a migration endpoint during a demo. It cannot, for the reason above.

## 2. Kernel configuration

Check what a running node has:

```bash
zcat /proc/config.gz | grep -E '^CONFIG_(CHECKPOINT_RESTORE|UNIX_DIAG|OPENVSWITCH)='
# or, if /proc/config.gz is absent:
grep -E '^CONFIG_...' /boot/config-$(uname -r)
```

If neither exists, the kernel was built without `CONFIG_IKCONFIG_PROC`; add it in
the same rebuild so the nodes stay auditable.

### 2.1 Checkpoint/restore (CRIU container path)

All required. `=y` or `=m` both acceptable unless noted.

| Symbol | Why it is needed |
|---|---|
| `CHECKPOINT_RESTORE` | the core feature. Often hidden behind `CONFIG_EXPERT` in menuconfig |
| `NAMESPACES`, `PID_NS`, `NET_NS`, `UTS_NS`, `IPC_NS` | a container is a set of namespaces; CRIU recreates them |
| `CGROUPS` | cgroup state is dumped and restored |
| `FHANDLE`, `EVENTFD`, `EPOLL`, `INOTIFY_USER` | file descriptor state that real workloads hold |
| `UNIX_DIAG`, `INET_DIAG`, `INET_TCP_DIAG`, `INET_UDP_DIAG`, `PACKET_DIAG`, `NETLINK_DIAG` | socket enumeration. Without these, any workload holding an open connection cannot be checkpointed, which is every workload we migrate |
| `NETFILTER_XT_MARK` | CRIU locks the network during the freeze so in-flight packets do not corrupt the dump |
| `TUN` | tun/tap device state |
| `USERFAULTFD` | required for post-copy, which is the migration mode used on the extreme edge |

### 2.2 Dataplane

| Symbol | Why |
|---|---|
| `VETH`, `BRIDGE` | pod networking |
| `VXLAN` | EVPN stretched L2 across clusters (VNI 10666, UDP 4789) |
| `MACVLAN` | the floating IP that follows the workload and keeps TCP alive |
| `OPENVSWITCH` | kube-ovn datapath |
| `NF_TABLES` | CRIU network lock backend on current CRIU versions |

### 2.3 Storage

| Symbol | Why |
|---|---|
| `NFS_FS`, `NFS_V4` | the shared checkpoint store (`/srv/drift-ckpt`) is NFSv4 on every node |
| `ISCSI_TCP` | Longhorn volumes, only if the node hosts VMs or PVC-backed workloads |

### 2.4 Rebuilding on Jetson

Use `scripts/drift-jetson-kernel.sh`, which does this and verifies each stage:

```bash
./scripts/drift-jetson-kernel.sh plan                     # read-only: what is missing
./scripts/drift-jetson-kernel.sh config  <kernel_src_dir>
./scripts/drift-jetson-kernel.sh build   <kernel_src_dir>
sudo ./scripts/drift-jetson-kernel.sh install <kernel_src_dir>            # lists steps
sudo ./scripts/drift-jetson-kernel.sh install <kernel_src_dir> --next     # one step
sudo reboot
sudo ./scripts/drift-jetson-kernel.sh confirm             # verify, then disarm revert
```

`<kernel_src_dir>` is NVIDIA's kernel source tree for the flashed L4T release.
Run `./scripts/drift-jetson-kernel.sh sources` on the node: it finds an already
extracted tree, or prints how to fetch the right one. In short, download the
Driver Package (BSP) Sources for the release from the [Jetson Linux
archive](https://developer.nvidia.com/embedded/jetson-linux-archive), then:

```bash
tar xf public_sources.tbz2
cd Linux_for_Tegra/source && tar xf kernel_src.tbz2
# tree lands in: Linux_for_Tegra/source/kernel/kernel-<base>
```

`<base>` is `noble` on JetPack 7 (L4T R38/R39) and `jammy` on JetPack 6 (L4T
R36). jetson-orin-01 reports L4T R39.2.1, which is JetPack 7.2, so the tree is
`Linux_for_Tegra/source/kernel/kernel-noble`. The BSP also ships
`source_sync.sh`, which fetches the matching sources over git and cannot get the
version wrong. Do not mix releases: module ABI will not match.

**Out-of-tree modules are mandatory, not optional.** This is the single biggest
trap in the whole procedure, and it is not about the GPU. On JetPack the
**network drivers are out-of-tree**: wired `end0` is `nvethernet` (plus `nvpps`,
which it depends on) and the Realtek WiFi is `rtl8822ce`. They live in
`/lib/modules/<release>/updates/`, not in the kernel tree. A kernel-only rebuild
therefore boots perfectly and comes up **with no network at all**, which on a
remote node is indistinguishable from a kernel that never booted. On
jetson-orin-01 the kernel-only build produced 1347 modules against the stock
1468; the 121 missing ones included every way of reaching the box.

Build them against the same tree, after the kernel is built and before
installing the Image:

```bash
cd Linux_for_Tegra/source
tar xf kernel_oot_modules_src.tbz2
# 'make modules' also builds the display drivers and aborts without their
# sources, even on a headless node:
mkdir -p nvdisplay unifiedgpudisp
tar xf nvidia_kernel_display_driver_source_without_root_dir.tbz2 -C nvdisplay
tar xf nvidia_unified_gpu_display_driver_source_without_root_dir.tbz2 -C unifiedgpudisp

export KERNEL_HEADERS=$PWD/kernel/kernel-noble KERNEL_OUTPUT=$PWD/kernel/kernel-noble
make modules kernel_name=noble
sudo -E make modules_install kernel_name=noble INSTALL_MOD_PATH=/tmp/oot-stage
```

`kernel_name=noble` is **required** and is not auto-detected. The build selects
`nvidia-oot/configs/Makefile.config.$(kernel_name)`; left empty it loads only
`Makefile.config.l4t` and misses the noble-specific
`NV_OOT_{IVC_EXT,TEGRA_HV,TEGRA_BPMP,SPI_TEGRA210_QUAD}_SKIP_BUILD=y`. Without
them the build dies with dozens of `modpost: '<sym>' exported twice. Previous
export was in vmlinux`, because those symbols are in-tree on this kernel
(`CONFIG_TEGRA_IVC=y`, `CONFIG_TEGRA_HV_DRIVER=y`) and the OOT copies duplicate
them. `KBUILD_MODPOST_WARN=1` does **not** help: `-w` only downgrades
unresolved-symbol errors, while duplicate exports are unconditionally fatal.
Use `jammy` instead of `noble` on JetPack 6.

Install via a staging directory and check where it landed before copying into
place. If it resolves to the **stock** release it will overwrite the stock
`updates/` modules, which the fallback boot entry depends on, destroying the
recovery path with no visible symptom until a reboot fails:

```bash
ls /tmp/oot-stage/lib/modules/          # must be the NEW release, and only that
sudo cp -a /tmp/oot-stage/lib/modules/<new>/updates /lib/modules/<new>/
sudo depmod -a <new>                    # must be silent
```

Verify before rebooting. Existing on disk, resolving by name, and being bound at
boot are three different things, and only the last one matters:

```bash
modprobe -S <new> --dry-run --show-depends nvethernet   # lists nvpps too
modprobe -S <new> --dry-run --show-depends rtl8822ce    # lists cfg80211, rfkill
grep rtl8822ce /lib/modules/<new>/modules.alias         # udev matches on this
```

The last check is not redundant: udev binds by device alias, never by module
name, so a module with no `modules.alias` entry is present and unreachable. Note
the driver name and module name differ here (the card binds as `rtl88x2ce` but
ships as `rtl8822ce.ko`), so search by file, not by `modinfo <driver>`.

`drift-jetson-kernel.sh` now enforces all of this: the `image` step refuses to
install a kernel that cannot drive the interfaces the node is currently reachable
on. Override with `DRIFT_ALLOW_NET_LOSS=1` only with a console attached.

Two properties worth knowing. It seeds the new `.config` from the **running**
kernel rather than from `tegra_defconfig`, so the rebuild differs from NVIDIA's
kernel only by our symbols. And it verifies the **built** config before
installing anything, because `make olddefconfig` silently drops options whose
dependencies are unmet, so enabling a symbol is not evidence that it is on.

Recovery is built in: the stock `Image` is preserved once at
`/boot/Image.drift-stock` and added to `extlinux.conf` as a selectable
`drift-stock` entry, and a dead-man's-switch restores it if nobody runs
`confirm` after the reboot. `revert` undoes the change on demand.

The fallback entry is the only protection against a kernel that panics before
userspace; the dead-man's-switch cannot help there, because it needs systemd to
start. Selecting it requires a console. So either attach one before rebooting, or
accept that an early panic means a site visit.

## 3. What a rebuild cannot fix

### 3.1 No iterative pre-copy on ARM64

CRIU's iterative pre-copy (`criu pre-dump`) finds the pages that changed since
the last pass by reading the soft-dirty PTE bit. ARM64 has never supported
soft-dirty upstream: `HAVE_ARCH_SOFT_DIRTY` is not selected by `arch/arm64`, so
`CONFIG_MEM_SOFT_DIRTY` cannot be enabled. Patches were proposed in 2024 and
were not merged, the maintainers preferring userfaultfd write-protect instead.

Impact and mitigation: run the Jetson migrations in **post-copy** mode, which is
the memory-independent path and does not use soft-dirty at all. It relies on
`USERFAULTFD` (section 2.1), which ARM64 does support. Concretely, pass
`--post-copy` and do not pass `--pre-copy N` to `drift-pod-migrate`. DRIFT
already disables pre-copy automatically when post-copy is selected, since
pre-dumped pages would be restored eagerly and defeat laziness.

`drift-preflight.sh` reports this as a warning rather than a failure on ARM,
because it is expected there and does not block the migration path we use.

### 3.2 No cross-architecture migration

See section 1. This is a property of checkpoint images, not a missing feature.

## 4. Hardware virtualisation (only for the VM path)

**Measured on the actual hardware (jetson-orin-01, L4T R39.2.1): `/dev/kvm` is
present.** The EL2 hurdle is therefore already cleared on this board, which is
better than the general Jetson picture below suggested. What remains unverified
is whether a guest actually boots and runs, since the known Tegra gap is in the
device tree's GICv3 description rather than in KVM initialisation. Test that
directly with a minimal `qemu-system-aarch64 -M virt -cpu host -enable-kvm` boot
before assuming the VM path is available.

ARM64 has no `vmx`/`svm` flag. KVM works only if the bootloader entered the
kernel at EL2. General status on Jetson:

* NVIDIA's position is that hypervisor mode is **not supported on JetPack**. It
  is a software/BSP limitation rather than a hardware lock, and modifying the
  BSP is outside their tested configuration.
* On Orin the kernel does initialise KVM (it logs `VHE mode initialized
  successfully`), but the shipped device tree omits the GICv3 interrupt
  declarations, so guests do not run until the DTB is patched and the kernel and
  DTB rebuilt.

**This is now a decision point, not a footnote.** The earlier plan was to keep
the VM path on the x86 edges and the container path on the Jetsons. With an
all-ARM edge (section 1) there are no x86 edges left to host it, so one of the
following has to be chosen before final testing:

1. **Container path only at the edge.** KubeVirt VM migration is demonstrated
   only where x86 hardware still exists, or dropped from the edge scenario. This
   is the low-risk option and matches the project status notes, which already
   casts the Jetsons as the extreme-edge CRIU hosts.
2. **Patch the Jetsons for KVM.** Patch the device tree, rebuild the kernel and
   DTB, and accept an NVIDIA-unsupported configuration on the nodes used for
   final testing. Budget this as its own work item with its own risk, since a
   bad DTB can prevent boot.

A `FAIL` on the virtualisation row of the preflight disables only the VM engine.
It does not affect container migration, which is unaffected by any of this.

## 5. Userspace

| Component | Version / source | Note |
|---|---|---|
| Kernel | >= 5.15 | already a DRIFT prerequisite. JetPack 6 ships 5.15, JetPack 5 ships 5.10 and is below the line |
| CRIU | 4.2.1 | `ppa:criu/ppa` publishes arm64 for jammy at exactly 4.2.1, the same version as the validated x86 nodes, so the Jetsons install from the PPA like everything else and no source build or version divergence is involved. Confirmed against the Launchpad package index |
| `crit` | ships with CRIU | DRIFT rewrites the cgroup image with it before restore |
| CRI-O | v1.31 (v1.30 to v1.34 probed) | from `pkgs.k8s.io`, which publishes arm64. K3s embedded containerd can checkpoint but cannot restore, which is why CRI-O is required |
| `runc` | provided by CRI-O | post-copy runs on runc, which execs the `criu` binary and supports lazy pages |
| `crun-drift` | crun 1.19.1, built from source per node | CRI-O's bundled crun is static and cannot `dlopen` libcriu. Build is architecture-portable, so the node needs the build toolchain |
| K3s | v1.31.5+k3s1 or later | installer auto-detects architecture |
| Packages | `open-iscsi`, `nfs-common`, `bridge-utils`, `vlan`, `iputils-arping` | installed by the `common` role |

## 6. Host configuration

* **cgroup v1.** The CRIU cross-host restore path is validated on v1. Set
  `systemd.unified_cgroup_hierarchy=0` on the kernel command line. On x86 that is
  GRUB; on Jetson it is the `APPEND` line in `/boot/extlinux/extlinux.conf`.
  `drift-preflight.sh cgroup-v1` now handles both, with a 15 minute auto-revert
  in case the node does not come back.
* **Swap off.** kubelet refuses to start with swap active. L4T enables zram swap
  by default, so on Jetson also disable it: `systemctl disable nvzramconfig`.
* **sysctls:** `net.ipv4.ip_forward=1`,
  `net.bridge.bridge-nf-call-iptables=1`, `net.bridge.bridge-nf-call-ip6tables=1`.
* **Modules:** `br_netfilter`, `8021q` loaded at boot.
* **Shared checkpoint store:** every node mounts the same NFSv4 export at the
  same path, `/srv/drift-ckpt`, exported `no_root_squash`.
* **Clock sync.** Several Jetson models have no RTC battery. A node that boots
  with a wrong clock fails TLS and Kubernetes certificate validation, which
  presents as unrelated errors. Make sure NTP is up before joining.
* **Power mode.** For reproducible migration timings, pin the power profile
  (`nvpmodel -m 0`) and run `jetson_clocks`, otherwise DVFS makes the KPI numbers
  noisy between runs.

## 7. Network fabric

* **A second MAC must reach the gateway.** The workload's IP is carried over an
  extra MAC on the NIC, so the fabric must not block MAC spoofing. Measured on
  jetson-orin-01 this passes even over Wi-Fi (`wlP1p1s0`), which is better than
  the textbook expectation that 802.11 binds one MAC per station. Do not rely on
  it without re-testing on the AP that will be used, and prefer wired Ethernet
  for the migration interface anyway: the VXLAN overlay, its 1400 byte MTU and
  the page-server transfer all behave more predictably there.
* Shared L2 between migration peers. The fabric must not block MAC spoofing. The
  preflight tests this directly by fabricating a MAC and pinging the gateway,
  which requires running it with `sudo`.
* UDP 4789 (VXLAN) and UDP 6081 (Geneve) open between nodes. The preflight has a
  paired `udp-listen` / `udp-probe` mode to prove it.
* TCP 179 between nodes for the FRR BGP EVPN session.
* **Post-copy page server:** the target dials the source on a port in the range
  20000 to 40000, chosen per migration. This must be open between the migration
  peers, and it is the one firewall rule that is easy to miss because it is not a
  fixed port. Pin it with `DRIFT_LAZY_PORT` if the firewall needs a single port.
* Overlay MTU 1400 on the stretched L2.

## 8. Repository changes needed for ARM64 nodes

These are on the DRIFT side, listed so nobody spends time debugging them on the
nodes themselves:

Done:

* The CNI plugin downloads in the `pod_migration` and `evpn_l2` roles now
  resolve the architecture on the node itself. They are shell tasks and two of
  the playbooks that reach them run with `gather_facts: false`, so this is
  derived from `dpkg --print-architecture` rather than `ansible_architecture`.
* `backend/Dockerfile` resolves the `kubectl` architecture the same way, which
  is what lets the hub image build on an ARM hub.

Still open:

* VM guest images are all x86 and one is BIOS boot
  (`backend/main.py:1433`, `demo/video-server/vm-video.yaml:27`,
  `frontend/cluster.html:311-314`). ARM VMs need arm64 cloud images and UEFI.
* `demo/yolo/yolo-pod.yaml:27` uses `ultralytics:latest-cpu`; the arm64 variant
  needs verification, and on Jetson a CUDA build would need an L4T base image.
  Note that CRIU cannot checkpoint GPU state, so the migrated workload stays
  CPU-only regardless. On Jetson this is worth stating plainly to anyone
  expecting the GPU to be the point of the node: the migrated container runs on
  the CPU cores.
