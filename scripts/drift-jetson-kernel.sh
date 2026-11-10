#!/usr/bin/env bash
#
# drift-jetson-kernel.sh — add the checkpoint/restore options DRIFT needs to a
# Jetson (L4T) kernel and install it without risking an unbootable node.
#
# WHY this exists: a vendor L4T kernel is built for appliances, so it ships
# without CONFIG_CHECKPOINT_RESTORE, without most of the socket *_DIAG family and
# — the one that actually decides the migration story — without CONFIG_USERFAULTFD.
# No userfaultfd means no post-copy, and arm64 cannot do pre-copy at all (no
# soft-dirty upstream), which would leave only a blocking checkpoint whose freeze
# grows with the workload's memory. So this rebuild is not a tuning exercise; it
# is what makes live migration work on these nodes.
#
# The risk being managed is bricking a node you may not have physical access to.
# Hence: nothing is installed until the BUILT config has been verified to contain
# every symbol (Kconfig silently drops options whose dependencies are unmet, so
# "I enabled it" is not evidence); the stock Image is preserved and added to
# extlinux as a selectable entry; and a dead-man's-switch restores it if the new
# kernel boots but the node never comes back to confirm. Same contract as
# drift-preflight.sh cgroup-v1.
#
# MODES (run in this order)
#   plan                    read-only: which symbols the RUNNING kernel lacks.
#   sources                 find an extracted kernel tree on this node, or print
#                           where to get the right one for this L4T release.
#   config   <src_dir>      enable them in <src_dir>/.config, then verify.
#   build    <src_dir>      build Image + modules (native, or set CROSS_COMPILE).
#   install  <src_dir>              list the install steps and their status.
#   install  <src_dir> --step NAME  run ONE step (verify, backup, bootentry,
#                                   modules, image, watchdog). Refuses to run
#                                   ahead of an unfinished prerequisite.
#   install  <src_dir> --next       run just the next pending step.
#   install  <src_dir> --all        run every remaining step in order.
#   confirm                 after the reboot: verify + disarm. Makes it permanent.
#   revert                  restore the stock kernel and reboot.
#
# <src_dir> is an extracted L4T kernel tree (the kernel-*-src directory from
# NVIDIA's public_sources for YOUR L4T release — this script deliberately does
# not guess a download URL, since the layout differs between releases).
#
# This does NOT touch the device tree. The GICv3/DTB patch that KVM guests need
# on Tegra is a separate change; /dev/kvm being present already is not affected
# by anything here.

set -uo pipefail

if [[ -t 1 ]]; then G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[1m'; Z=$'\e[0m'
else G=""; R=""; Y=""; B=""; Z=""; fi
say()  { printf "  %b%-5s%b  %s\n" "$2" "$1" "$Z" "$3"; }
ok()   { say OK   "$G" "$1"; }
warn() { say WARN "$Y" "$1"; }
die()  { say FAIL "$R" "$1"; exit 1; }
need_root() { [[ $EUID -eq 0 ]] || die "run as root (sudo)"; }

KREL=$(uname -r)
ARCH=$(uname -m)
BOOT_IMG=/boot/Image
BOOT_BAK=/boot/Image.drift-stock          # the stock kernel; written ONCE, never overwritten
EXTLINUX=/boot/extlinux/extlinux.conf
EXTLINUX_BAK=/boot/extlinux/extlinux.conf.drift-stock
CONFIRM_FLAG=/etc/drift-kernel-confirmed
WD_SVC=/etc/systemd/system/drift-kernel-watchdog.service
WD_SH=/usr/local/sbin/drift-kernel-watchdog.sh
WD_WAIT=${DRIFT_WD_WAIT:-900}
MOD_DIR=${DRIFT_MOD_DIR:-/lib/modules}   # overridable so the step flow can be tested off-target
BOOT_INITRD=/boot/initrd
INITRD_BAK=/boot/initrd.drift-stock
LOCALVER=${DRIFT_LOCALVERSION:--drift}

# What the rest of the DRIFT stack needs inside netfilter/OVS/tc, found when
# kube-ovn was first installed on a Jetson (2026-10-13): the September list
# checked only top-level features, and the L4T kernel lacks the conntrack
# zones/marks/labels that OVN programs (ovs-vswitchd: NXBAC_CT_DATAPATH_SUPPORT,
# node stuck NotReady), the xt/nft matches that kube-proxy, kube-ovn and Multus
# use through iptables-nft, netem for the latency emulation, and vhost for
# KubeVirt. Derived from the x86 edges' generic kernel, minus what DRIFT never
# uses (IPsec, IPVS, device-mapper extras).
SYMS_K8S="NF_CONNTRACK_ZONES NF_CONNTRACK_MARK NF_CONNTRACK_LABELS
      NF_CONNTRACK_TIMEOUT NF_CONNTRACK_TIMESTAMP NF_CONNTRACK_BRIDGE
      NETFILTER_NETLINK_LOG NETFILTER_NETLINK_QUEUE NETFILTER_NETLINK_ACCT
      NETFILTER_XT_CONNMARK NETFILTER_XT_TARGET_CT NETFILTER_XT_TARGET_MARK
      NETFILTER_XT_TARGET_CONNMARK NETFILTER_XT_TARGET_TCPMSS NETFILTER_XT_TARGET_NFLOG
      NETFILTER_XT_TARGET_NFQUEUE NETFILTER_XT_TARGET_TPROXY NETFILTER_XT_TARGET_NETMAP
      NETFILTER_XT_TARGET_HMARK NETFILTER_XT_TARGET_DSCP
      NETFILTER_XT_MATCH_MARK NETFILTER_XT_MATCH_CONNMARK NETFILTER_XT_MATCH_STATE
      NETFILTER_XT_MATCH_PHYSDEV NETFILTER_XT_MATCH_PKTTYPE NETFILTER_XT_MATCH_OWNER
      NETFILTER_XT_MATCH_IPRANGE NETFILTER_XT_MATCH_LENGTH NETFILTER_XT_MATCH_SOCKET
      NETFILTER_XT_MATCH_CONNLIMIT NETFILTER_XT_MATCH_CONNLABEL NETFILTER_XT_MATCH_CONNBYTES
      NETFILTER_XT_MATCH_TCPMSS NETFILTER_XT_MATCH_HELPER NETFILTER_XT_MATCH_DSCP
      NETFILTER_XT_MATCH_QUOTA NETFILTER_XT_MATCH_MAC NETFILTER_XT_MATCH_NFACCT
      NFT_CT NFT_FIB NFT_FIB_INET NFT_FIB_IPV4 NFT_FIB_IPV6 NFT_REJECT NFT_REJECT_INET
      NFT_LIMIT NFT_LOG NFT_HASH NFT_REDIR NFT_QUOTA NFT_NUMGEN NFT_SOCKET NFT_QUEUE
      NF_TABLES_BRIDGE NFT_BRIDGE_META
      IP6_NF_RAW IP6_NF_MATCH_IPV6HEADER IP6_NF_MATCH_RPFILTER IP_NF_TARGET_NETMAP
      NF_FLOW_TABLE NET_IPGRE
      NET_SCH_NETEM NET_SCH_PRIO NET_CLS_MATCHALL NET_ACT_POLICE NET_ACT_CT
      NET_ACT_TUNNEL_KEY NET_ACT_PEDIT NET_ACT_SKBEDIT NET_ACT_CSUM NET_ACT_VLAN
      OPENVSWITCH_GENEVE OPENVSWITCH_GRE NET_IPGRE_DEMUX
      IPVLAN VHOST_NET VHOST_VSOCK CGROUP_NET_PRIO CGROUP_NET_CLASSID"
# Everything DRIFT's two migration engines need from the kernel. Enabling a
# symbol that is already on is a no-op, so this list is the full requirement
# rather than just this node's gaps — it makes the script correct on any Jetson.
# EXPERT comes first because CHECKPOINT_RESTORE is hidden behind it in menuconfig.
SYMS="EXPERT CHECKPOINT_RESTORE USERFAULTFD
      NAMESPACES PID_NS NET_NS UTS_NS IPC_NS CGROUPS
      FHANDLE EVENTFD EPOLL INOTIFY_USER
      UNIX_DIAG INET_DIAG INET_TCP_DIAG INET_UDP_DIAG PACKET_DIAG NETLINK_DIAG
      NETFILTER_XT_MARK TUN
      VETH BRIDGE VXLAN GENEVE MACVLAN OPENVSWITCH NF_TABLES
      NFS_FS NFS_V4 ISCSI_TCP
      $SYMS_K8S"
# Nice to have, not worth a rebuild on their own: they expose the running config
# at /proc/config.gz. A kernel that ships /boot/config-$(uname -r) is just as
# auditable, so these are enabled while we are rebuilding but never demand one.
# The QMI/MBIM drivers serve the vehicle Jetson's Quectel modem (quectel-CM needs
# /dev/cdc-wdm0 + wwan0); 6.8.12-drift2 got them as out-of-tree builds instead.
SYMS_OPT="IKCONFIG IKCONFIG_PROC USB_WDM USB_NET_QMI_WWAN USB_NET_CDC_MBIM"

running_config() {
  if   [[ -r /proc/config.gz ]];  then echo /proc/config.gz
  elif [[ -r /boot/config-$KREL ]]; then echo "/boot/config-$KREL"
  else echo ""; fi
}
cfg_val() {  # cfg_val <SYMBOL> <file> -> y|m|n
  local line
  if [[ $2 == *.gz ]]; then line=$(zcat "$2" 2>/dev/null | grep -m1 -E "^CONFIG_$1=")
  else                      line=$(grep -m1 -E "^CONFIG_$1=" "$2" 2>/dev/null); fi
  [[ -n $line ]] && echo "${line#*=}" || echo n
}
missing_in() {  # missing_in <config file> -> prints absent symbols
  local f=$1 s
  for s in $SYMS; do [[ $(cfg_val "$s" "$f") == n ]] && printf '%s ' "$s"; done
}
src_config() { echo "${1%/}/.config"; }
check_src() {
  local d=${1:-}
  [[ -n $d ]] || die "usage: $0 $MODE <kernel_src_dir>"
  [[ -f ${d%/}/Makefile && -f ${d%/}/init/main.c ]] || \
    die "$d is not a full kernel source tree (a linux-headers directory is not enough) — run '$0 sources'"
}

do_plan() {
  local f; f=$(running_config)
  echo "${B}DRIFT kernel plan — $(hostname): $KREL ($ARCH)${Z}"
  [[ -r /etc/nv_tegra_release ]] && echo "  $(head -1 /etc/nv_tegra_release)"
  echo
  [[ -n $f ]] || die "no /proc/config.gz and no /boot/config-$KREL — cannot read the running config"
  local miss; miss=$(missing_in "$f")
  if [[ -z $miss ]]; then ok "running kernel already has every symbol — no rebuild needed"; return 0; fi
  warn "rebuild needed. Missing from the running kernel:"
  local s; for s in $miss; do printf "        CONFIG_%s\n" "$s"; done
  echo
  echo "  Post-copy depends on USERFAULTFD; without it the only option left on arm64"
  echo "  is a blocking checkpoint (no pre-copy exists on arm64 either)."
  echo
  echo "  Next: $0 config <kernel_src_dir>"
  return 2
}

# Where <src_dir> comes from. NVIDIA ships the kernel sources per L4T release,
# and the directory name tracks the Ubuntu base (kernel-jammy on R36 / JetPack 6,
# kernel-noble on R38-R39 / JetPack 7), which is why this is looked up rather
# than hardcoded.
do_sources() {
  echo "${B}kernel source for $KREL${Z}"
  [[ -r /etc/nv_tegra_release ]] && echo "  $(head -1 /etc/nv_tegra_release)"
  echo
  local found=0 c
  while read -r c; do
    [[ -n $c ]] || continue
    [[ -f $c/Makefile && -f $c/init/main.c && -x $c/scripts/config ]] || continue
    ok "usable tree: $c"
    found=1
  done < <(find /usr/src /opt /home /root /srv -maxdepth 7 -type d \
             \( -name 'kernel-*' -o -name 'linux-*' \) 2>/dev/null)
  if [[ $found == 1 ]]; then
    echo
    echo "  Pass one of the above as <src_dir>, e.g.:  $0 plan && $0 config <src_dir>"
    return 0
  fi
  warn "no extracted kernel tree found on this node"
  # Build the download URL from THIS node's release line, so nobody has to match
  # a version by eye on the archive page and end up with a mismatched tree.
  local rel rev url=""
  if [[ -r /etc/nv_tegra_release ]]; then
    rel=$(awk 'NR==1{print tolower($2)}' /etc/nv_tegra_release)                 # r39
    rev=$(awk 'NR==1{for(i=1;i<=NF;i++) if($i=="REVISION:"){gsub(/,/,"",$(i+1)); print $(i+1); exit}}' /etc/nv_tegra_release)
    [[ -n $rel && -n $rev ]] && url="https://developer.nvidia.com/downloads/embedded/l4t/${rel}_release_v${rev}/sources/public_sources.tbz2"
  fi
  if [[ -n $url ]]; then
    echo
    echo "  ${B}Exact source bundle for this node:${Z}"
    echo "    $url"
    echo
    echo "    curl -L -o public_sources.tbz2 '$url'"
    echo "    tar xf public_sources.tbz2"
    echo "    cd Linux_for_Tegra/source && tar xf kernel_src.tbz2"
    echo "    # then <src_dir> = \$PWD/kernel/kernel-noble   (kernel-jammy on JetPack 6)"
  fi
  # JetPack 7 (L4T R38/R39) ships a Canonical-packaged kernel
  # (linux-noble-nvidia-tegra, 6.8.x) instead of NVIDIA's own tree, so the
  # public_sources route below is the WRONG one there: the distro packaging is
  # both easier and safer, because it rebuilds the initrd, keeps /boot layout
  # consistent and is undone with `apt install --reinstall`.
  local pkg=""
  [[ -e /boot/vmlinuz-$KREL ]] && pkg=$(dpkg -S "/boot/vmlinuz-$KREL" 2>/dev/null | cut -d: -f1)
  [[ -n $pkg ]] || pkg=$(dpkg-query -W -f'${Package}\n' 'linux-image-*' 2>/dev/null | grep -F "$KREL" | head -1)
  if [[ -n $pkg ]]; then
    cat <<APTROUTE

  ${B}This kernel came from apt${Z} (package: $pkg), so use the DISTRO route, not
  NVIDIA's public_sources — it rebuilds the initrd and keeps /boot consistent:

    # enable deb-src for the repo that provides it, then:
    sudo apt install dpkg-dev fakeroot libncurses-dev flex bison libssl-dev bc
    apt-get source $pkg          # lands a source tree in the current directory

  Point this script's config/build steps at that unpacked tree, OR build it the
  Ubuntu way (fakeroot debian/rules binary) and install the resulting .deb, which
  is the better option on a packaged kernel: reverting is then just
  'apt install --reinstall $pkg'.

  Before installing either way, check what this node actually boots:
    ls -la /boot/ ; cat /boot/extlinux/extlinux.conf
  The install steps in this script assume /boot/Image + extlinux. A packaged
  kernel usually boots /boot/vmlinuz-<rel> with an initrd, in which case use the
  .deb route and skip 'install' here entirely.
APTROUTE
    return 2
  fi
  cat <<'HOWTO'

  Get it for THIS L4T release (do not mix releases — module ABI will not match):

    1. Jetson Linux archive:  https://developer.nvidia.com/embedded/jetson-linux-archive
       Pick the entry matching the release printed above, download
       "Driver Package (BSP) Sources" / public_sources.tbz2.
    2. tar xf public_sources.tbz2
       cd Linux_for_Tegra/source
       tar xf kernel_src.tbz2
    3. The tree lands in:  Linux_for_Tegra/source/kernel/kernel-<base>
       where <base> is 'noble' on JetPack 7 (L4T R38/R39) and 'jammy' on
       JetPack 6 (L4T R36). That directory is <src_dir>.

  Alternative that cannot get the version wrong: the BSP ships
  Linux_for_Tegra/source/source_sync.sh — run it with the release tag
  (./source_sync.sh -k -t <tag>) and it fetches the matching kernel via git.

  NOTE on NVIDIA's out-of-tree modules (GPU, camera, hwpm...). They live in the
  same public_sources bundle and are built separately from the kernel. This
  script rebuilds the KERNEL only. Changing the config can change symbol
  versions, so plan to rebuild those modules against the same tree
  (NVIDIA's own flow: make -C kernel, then the *_oot module targets) if the
  node needs the GPU stack. The DRIFT migration workload is CPU-only, so the
  container path does not depend on them.
HOWTO
  return 2
}

do_config() {
  check_src "${1:-}"
  local d=${1%/} cfg; cfg=$(src_config "$d")
  # Start from the kernel this node is actually running, so the rebuild differs
  # from the vendor kernel ONLY by our symbols. Starting from tegra_defconfig
  # instead would silently drop whatever NVIDIA changed after it.
  if [[ ! -f $cfg ]]; then
    local rc; rc=$(running_config)
    [[ -n $rc ]] || die "no .config in $d and no running config to seed from"
    if [[ $rc == *.gz ]]; then zcat "$rc" > "$cfg"; else cp -a "$rc" "$cfg"; fi
    ok "seeded $cfg from the running kernel config ($rc)"
  else
    cp -a "$cfg" "$cfg.drift-pre"
    ok "existing .config backed up to $cfg.drift-pre"
  fi
  [[ -x $d/scripts/config ]] || die "$d/scripts/config missing — is this a full kernel tree?"
  local s
  for s in $SYMS $SYMS_OPT; do "$d/scripts/config" --file "$cfg" --enable "$s"; done
  # Distinct release string, so modules land in their own /lib/modules directory
  # and the stock kernel's modules are never touched. That is what makes 'revert'
  # a true revert rather than a stock kernel running rebuilt modules.
  "$d/scripts/config" --file "$cfg" --disable LOCALVERSION_AUTO
  "$d/scripts/config" --file "$cfg" --set-str LOCALVERSION "$LOCALVER"
  ok "release suffix requested: '$LOCALVER'"
  warn "verify after building that the release really changed: some vendor trees
         (the Canonical linux-nvidia-tegra one does) ignore CONFIG_LOCALVERSION.
         A release that differs from the stock one is what keeps /lib/modules and
         the initrd separate; check with: cat $d/include/config/kernel.release"
  ok "requested ${B}$(echo $SYMS $SYMS_OPT | wc -w)${Z} symbols"
  # olddefconfig resolves dependencies — and is exactly where an option whose
  # deps are unmet gets dropped again, which is why the verify below is the
  # step that matters, not the enable above.
  ( cd "$d" && make ARCH=arm64 olddefconfig >/dev/null 2>&1 ) || die "make olddefconfig failed in $d"
  local miss; miss=$(missing_in "$cfg")
  [[ -z $miss ]] || die "olddefconfig dropped: ${miss}— unmet dependencies; resolve with 'make ARCH=arm64 menuconfig' in $d"
  ok "all symbols present in $cfg after olddefconfig"
  echo "  Next: $0 build $d"
}

do_build() {
  check_src "${1:-}"
  local d=${1%/} cfg; cfg=$(src_config "$d")
  [[ -f $cfg ]] || die "no .config in $d — run '$0 config $d' first"
  local miss; miss=$(missing_in "$cfg")
  [[ -z $miss ]] || die "config is missing: ${miss}— run '$0 config $d' first"
  # A full arm64 kernel + modules build needs roughly 20-25 GB of objects. Dying
  # on ENOSPC an hour in is avoidable, and on these nodes the tree is supposed to
  # live on the external SSD rather than the device's internal storage — so say
  # out loud which filesystem is about to absorb it.
  local dev mnt freek freeg
  dev=$(df -P "$d" | awk 'NR==2{print $1}')
  mnt=$(df -P "$d" | awk 'NR==2{print $NF}')
  freek=$(df -Pk "$d" | awk 'NR==2{print $4}')
  freeg=$(( freek / 1024 / 1024 ))
  echo "  build tree on ${B}$dev${Z} (mounted $mnt), ${freeg} GB free"
  if [[ $freeg -lt ${DRIFT_MIN_GB:-25} ]]; then
    die "only ${freeg} GB free on $mnt — a kernel build needs ~25 GB. Move the source tree to the SSD (or set DRIFT_MIN_GB to override)"
  fi
  case $dev in
    *mmcblk*) warn "$dev looks like internal eMMC — the build is meant to go on the external SSD";;
  esac
  local j=${DRIFT_JOBS:-$(nproc)}
  echo "  building in $d with -j$j ${CROSS_COMPILE:+(CROSS_COMPILE=$CROSS_COMPILE)}"
  ( cd "$d" && make ARCH=arm64 -j"$j" Image modules ) || die "kernel build failed"
  [[ -f $d/arch/arm64/boot/Image ]] || die "build reported success but $d/arch/arm64/boot/Image is missing"
  ok "built $(ls -la "$d/arch/arm64/boot/Image" | awk '{print $5}') byte Image"
  echo "  Next: sudo $0 install $d"
}

# ── install, one step at a time ───────────────────────────────────────────────
# Each step is separately runnable, idempotent, and refuses to run before its
# prerequisites. Status is derived from the filesystem rather than a marker file,
# so it stays truthful if a step was done by hand, or a previous run died halfway,
# or the node rebooted in between.
INSTALL_STEPS="verify backup bootentry modules initrd image watchdog"
step_help() {
  case $1 in
    verify)    echo "check the BUILT config really contains every symbol";;
    backup)    echo "save the stock Image + extlinux.conf (once, never overwritten)";;
    bootentry) echo "add a selectable 'drift-stock' entry to extlinux.conf";;
    modules)   echo "make modules_install for the new kernel";;
    initrd)    echo "regenerate /boot/initrd for the new kernel release";;
    image)     echo "replace /boot/Image with the new kernel";;
    watchdog)  echo "arm the ${WD_WAIT}s auto-revert (disarmed by 'confirm')";;
  esac
}
new_kernel_release() { local d=${1%/}; [[ -f $d/include/config/kernel.release ]] && cat "$d/include/config/kernel.release" || echo ""; }
step_done() {
  local st=$1 d=${2%/} rel
  case $st in
    verify)    [[ -f $(src_config "$d") && -z $(missing_in "$(src_config "$d")") ]];;
    backup)    [[ -f $BOOT_BAK ]] && { [[ ! -f $BOOT_INITRD || -f $INITRD_BAK ]]; };;
    bootentry) grep -q '^LABEL drift-stock' "$EXTLINUX" 2>/dev/null;;
    modules)   rel=$(new_kernel_release "$d"); [[ -n $rel && -d $MOD_DIR/$rel ]] && modules_complete "$d" "$rel";;
    initrd)    rel=$(new_kernel_release "$d"); [[ -n $rel && -f /boot/initrd.img-$rel ]] \
                 && cmp -s "/boot/initrd.img-$rel" "$BOOT_INITRD";;
    image)     cmp -s "$d/arch/arm64/boot/Image" "$BOOT_IMG";;
    watchdog)  systemctl is-enabled drift-kernel-watchdog.service >/dev/null 2>&1;;
  esac
}

step_verify() {
  local d=${1%/} cfg; cfg=$(src_config "$d")
  [[ -f $d/arch/arm64/boot/Image ]] || die "no built Image in $d — run '$0 build $d' first"
  local miss; miss=$(missing_in "$cfg")
  [[ -z $miss ]] || die "built config is missing: ${miss}— do NOT install this kernel"
  [[ -f $EXTLINUX ]] || die "$EXTLINUX not found — this is not an extlinux/U-Boot boot chain"
  ok "built config verified; every required symbol present"
}
initrd_has_release() {  # <initrd> <release> -> does it carry that kernel's modules?
  local f=$1 r=$2 listing
  command -v lsinitramfs >/dev/null 2>&1 || return 0      # cannot tell; do not block
  listing=$(lsinitramfs "$f" 2>/dev/null) || return 0
  [[ -n $listing ]] || return 0
  grep -q "modules/$r/" <<<"$listing"
}
step_backup() {
  need_root
  if [[ -f $BOOT_BAK ]]; then ok "stock kernel already saved at $BOOT_BAK (left untouched)"
  else cp -a "$BOOT_IMG" "$BOOT_BAK" || die "could not back up $BOOT_IMG"; ok "stock kernel saved to $BOOT_BAK"; fi
  [[ -f $EXTLINUX_BAK ]] || cp -a "$EXTLINUX" "$EXTLINUX_BAK"
  if [[ -f $BOOT_INITRD && ! -f $INITRD_BAK ]]; then
    # Back up an initrd that can actually boot the STOCK kernel. If a previous
    # run already replaced $BOOT_INITRD with one built for another release,
    # copying it here would enshrine a fallback that cannot mount root — a
    # fallback that looks present and is useless.
    local run src_ird=""; run=$(uname -r)
    if initrd_has_release "$BOOT_INITRD" "$run"; then src_ird=$BOOT_INITRD
    elif [[ -f /boot/initrd.img-$run ]] && initrd_has_release "/boot/initrd.img-$run" "$run"; then
      src_ird=/boot/initrd.img-$run
    elif [[ -d $MOD_DIR/$run ]] && command -v update-initramfs >/dev/null 2>&1; then
      update-initramfs -c -k "$run" >/dev/null 2>&1 || update-initramfs -u -k "$run" >/dev/null 2>&1
      [[ -f /boot/initrd.img-$run ]] && src_ird=/boot/initrd.img-$run
    fi
    [[ -n $src_ird ]] || die "cannot find or rebuild an initrd for the running kernel '$run' — without it the fallback entry cannot boot the stock kernel; fix this before going further"
    cp -a "$src_ird" "$INITRD_BAK"; ok "stock initrd saved to $INITRD_BAK (from $src_ird)"
  fi
  ok "extlinux.conf backed up to $EXTLINUX_BAK"
}
step_bootentry() {
  need_root
  if grep -q '^LABEL drift-stock' "$EXTLINUX"; then ok "'drift-stock' entry already present"; return; fi
  { echo
    echo "LABEL drift-stock"
    echo "      MENU LABEL Stock kernel (pre-DRIFT)"
    sed -n '/^[[:space:]]*LINUX/,$p' "$EXTLINUX_BAK" | sed -n '1,/^[[:space:]]*APPEND/p' \
      | sed -e "s#^\([[:space:]]*LINUX\).*#\1 $BOOT_BAK#" \
            -e "s#^\([[:space:]]*INITRD\).*#\1 $INITRD_BAK#"
  } >> "$EXTLINUX"
  grep -q '^LABEL drift-stock' "$EXTLINUX" || die "failed to add the fallback entry"
  ok "added 'drift-stock' boot entry pointing at $BOOT_BAK"
}
step_modules() {
  need_root
  local d=${1%/} rel; rel=$(new_kernel_release "$d")
  [[ -n $rel ]] || die "cannot determine the built kernel release"
  # modules_install writes to /lib/modules/<release>. If the built release equals
  # the RUNNING one it overwrites the stock kernel's own modules, and then
  # restoring /boot/Image is no longer a real revert: the stock kernel comes back
  # up against rebuilt modules. Keep the releases distinct.
  if [[ $rel == "$KREL" && ${DRIFT_ALLOW_SAME_RELEASE:-0} != 1 ]]; then
    die "built release '$rel' is identical to the running kernel — modules_install would overwrite the stock /lib/modules/$KREL and break revert. Set a different CONFIG_LOCALVERSION (or DRIFT_ALLOW_SAME_RELEASE=1 if you really mean it)"
  fi
  # Output goes to a log, not /dev/null: a modules_install that stopped partway
  # once left 391 of 1347 modules installed (no cfg80211, so no Wi-Fi) and the
  # node booted unreachable.
  local log=/tmp/drift-modules-install.log
  ( cd "$d" && make ARCH=arm64 modules_install ) >"$log" 2>&1 || { tail -20 "$log"; die "modules_install failed (full log: $log)"; }
  [[ -d $MOD_DIR/$rel ]] || die "modules_install reported success but $MOD_DIR/$rel is missing"
  modules_complete "$d" "$rel" || { tail -20 "$log"; die "only $(count_ko "$MOD_DIR/$rel/kernel") of $(count_ko "$d") built modules are installed in $MOD_DIR/$rel/kernel (log: $log)"; }
  ok "all $(count_ko "$d") modules installed for $rel (stock $MOD_DIR/$KREL untouched)"
}
count_ko() { find "$1" -name '*.ko*' 2>/dev/null | wc -l; }
modules_complete() {  # <src_dir> <release> -> every module the build produced is installed
  local built inst
  built=$(find "${1%/}" -name '*.ko' -not -path '*/.tmp*' 2>/dev/null | wc -l)
  inst=$(count_ko "$MOD_DIR/$2/kernel")
  [[ $built -gt 0 && $inst -ge $built ]]
}
step_initrd() {
  need_root
  local d=${1%/} rel; rel=$(new_kernel_release "$d")
  [[ -n $rel ]] || die "cannot determine the new kernel release"
  command -v update-initramfs >/dev/null 2>&1 || \
    die "update-initramfs not present — regenerate the initrd for $rel by hand before installing the Image"
  if [[ -f /boot/initrd.img-$rel ]]; then update-initramfs -u -k "$rel" >/dev/null 2>&1
  else update-initramfs -c -k "$rel" >/dev/null 2>&1; fi
  [[ -f /boot/initrd.img-$rel ]] || die "update-initramfs produced no /boot/initrd.img-$rel"
  # Existing is not the same as working. The one thing this initrd MUST be able
  # to do is mount root, so check the drivers the kernel command line implies are
  # either built in or actually present inside it. Getting this wrong is what
  # strands the board with no console.
  local cfg; cfg=$(src_config "$d")
  local rootdev rootfs need=""
  rootdev=$(grep -o 'root=[^ ]*' "$EXTLINUX" 2>/dev/null | head -1)
  rootfs=$(grep -o 'rootfstype=[^ ]*' "$EXTLINUX" 2>/dev/null | head -1 | cut -d= -f2)
  case $rootdev in *nvme*) need+=" BLK_DEV_NVME:nvme";; *mmcblk*) need+=" MMC_BLOCK:mmc_block";; *sd[a-z]*) need+=" BLK_DEV_SD:sd_mod";; esac
  case $rootfs in ext4) need+=" EXT4_FS:ext4";; esac
  local missing="" listing=""
  # Take the listing ONCE into a variable. Piping it into `grep -q` looks
  # equivalent but is not: grep -q exits on the first match, lsinitramfs then
  # takes SIGPIPE, and `set -o pipefail` reports the successful match as status
  # 141 — i.e. every module would be declared missing. That false alarm is worse
  # than no check, because it blocks a kernel that would have booted fine.
  if command -v lsinitramfs >/dev/null 2>&1; then
    listing=$(lsinitramfs "/boot/initrd.img-$rel" 2>/dev/null) || listing=""
  fi
  for pair in $need; do
    local sym=${pair%%:*} mod=${pair##*:}
    [[ $(cfg_val "$sym" "$cfg") == y ]] && continue          # built in, no module needed
    [[ -n $listing ]] || continue                            # cannot inspect: do not guess
    grep -q "/${mod}\.ko" <<<"$listing" || missing+="$mod "
  done
  [[ -z $missing ]] || die "the new initrd is missing the driver(s) needed to mount root: ${missing}— do NOT install this kernel, it would not boot and could only be recovered from a console"
  # Independent of step ordering: overwriting $BOOT_INITRD without a stock copy
  # aside leaves the primary entry pairing the stock kernel with a foreign
  # initrd, which does not boot.
  [[ -f $INITRD_BAK ]] || die "refusing to overwrite $BOOT_INITRD: no stock initrd saved at $INITRD_BAK — run '--step backup' first"
  install -m 0644 "/boot/initrd.img-$rel" "$BOOT_INITRD" || die "could not install $BOOT_INITRD"
  ok "initrd regenerated for $rel and contains what root needs (stock kept at $INITRD_BAK)"
}
step_image() {
  need_root
  local d=${1%/} rel; rel=$(new_kernel_release "$d")
  [[ -n $rel ]] || die "cannot determine the built kernel release"
  # The initrd carries the modules needed to mount root (NVMe here). If it was
  # built for a different release its vermagic will not match, root never
  # mounts, systemd never starts, and the auto-revert cannot save you: the node
  # is then only recoverable from a console. So refuse rather than risk it.
  if ! cmp -s "/boot/initrd.img-$rel" "$BOOT_INITRD" 2>/dev/null; then
    die "no initrd for '$rel' installed at $BOOT_INITRD — run '--step initrd' first. Installing this Image would make the node unbootable and UNRECOVERABLE without a console."
  fi
  ok "initrd matches the built release ($rel)"
  # The fallback is the ONLY thing standing between a bad kernel and a site
  # visit, so verify it rather than assume the earlier step worked.
  grep -q '^LABEL drift-stock' "$EXTLINUX" || die "no drift-stock entry in $EXTLINUX — run '--step bootentry' first"
  local fb_img fb_ird
  fb_img=$(awk '/^LABEL drift-stock/{f=1} f&&/^[[:space:]]*LINUX/{print $2; exit}' "$EXTLINUX")
  fb_ird=$(awk '/^LABEL drift-stock/{f=1} f&&/^[[:space:]]*INITRD/{print $2; exit}' "$EXTLINUX")
  [[ -s $fb_img ]] || die "fallback entry points at '$fb_img' which does not exist — refusing to install"
  [[ -z $fb_ird || -s $fb_ird ]] || die "fallback entry points at initrd '$fb_ird' which does not exist — refusing to install"
  local fb_append
  fb_append=$(awk '/^LABEL drift-stock/{f=1} f&&/^[[:space:]]*APPEND/{print; exit}' "$EXTLINUX")
  [[ $fb_append == *root=* ]] \
    || die "fallback entry has no root= in its APPEND — it would not boot; fix $EXTLINUX before installing"
  ok "fallback verified: $fb_img${fb_ird:+ + $fb_ird}, with root= present"
  # "Can it mount root" and "can I still reach it" are different questions, and
  # both failures look identical from a remote shell: the board goes quiet. On
  # JetPack the NIC drivers are out-of-tree (nvethernet, rtl8822ce), so a
  # kernel-only build boots fine and strands you with no network.
  local lost; lost=$(check_net_drivers "$rel")
  if [[ -n $lost ]]; then
    if [[ ${DRIFT_ALLOW_NET_LOSS:-0} == 1 ]]; then
      warn "no driver for ${lost}under '$rel' — proceeding because DRIFT_ALLOW_NET_LOSS=1"
    else
      die "the new kernel has no usable driver for: ${lost}— it would boot with no network and you could not log in. Build the out-of-tree modules against this kernel ('make modules kernel_name=<noble|jammy>' in Linux_for_Tegra/source), then 'depmod -a $rel'. Set DRIFT_ALLOW_NET_LOSS=1 only with a console attached."
    fi
  fi
  install -m 0644 "$d/arch/arm64/boot/Image" "$BOOT_IMG" || die "could not install $BOOT_IMG"
  ok "new kernel installed as $BOOT_IMG (stock still at $BOOT_BAK)"
}
check_net_drivers() {  # <new release> -> prints "iface:module " for each NIC the new kernel cannot bind
  local rel=$1 missing="" ifc name drv mod alias
  for ifc in /sys/class/net/*; do
    name=$(basename "$ifc")
    [[ $name == lo ]] && continue
    [[ -e $ifc/device/driver ]] || continue        # virtual: bridges, veth, tun, wireguard, tailscale
    drv=$(basename "$(readlink -f "$ifc/device/driver")")
    # The module name is NOT always the driver name: the Realtek card binds as
    # driver 'rtl88x2ce' but ships as rtl8822ce.ko. Resolve through the device's
    # modalias, which is what udev itself uses at boot.
    # Reset per interface: a stale alias from the previous NIC made a USB gadget
    # (driver configfs-gadget.l4t, no modalias) look like a missing module.
    mod="" alias=""
    if [[ -r $ifc/device/modalias ]]; then
      alias=$(<"$ifc/device/modalias")
      mod=$(modprobe -R "$alias" 2>/dev/null | head -1)
    fi
    [[ -n $mod ]] || mod=$drv
    # If the RUNNING kernel has no module for it, it is built in (or, like a
    # configfs gadget, not a module at all); assume the same of the new kernel
    # rather than reporting a false positive.
    if [[ -n $alias ]]; then modprobe -R "$alias" >/dev/null 2>&1 || continue
    else modinfo -k "$KREL" "$mod" >/dev/null 2>&1 || continue; fi
    # Resolve against the NEW release's modules.dep, and require an alias entry:
    # a module that exists but has no alias never gets auto-loaded.
    if ! modprobe -S "$rel" --dry-run --show-depends "$mod" >/dev/null 2>&1; then
      missing+="$name:$mod "
    elif [[ -n ${alias:-} ]] && ! grep -q -- "$mod\$" "$MOD_DIR/$rel/modules.alias" 2>/dev/null; then
      missing+="$name:$mod(no-alias) "
    fi
  done
  echo "$missing"
}
step_watchdog() {
  need_root
  cat > "$WD_SH" <<'WDEOF'
#!/bin/bash
# Restore the stock kernel unless someone confirms this one is healthy.
# Logs to disk, so a revert can be diagnosed afterwards without a console:
# which kernel actually booted (the UEFI launcher may silently fall back to the
# flash kernel) and whether the network came up.
[ -f /etc/drift-kernel-confirmed ] && exit 0
LOG=/var/log/drift-kernel-watchdog.log
snap() { { echo "== $(date -Is) +${1}s  kernel=$(uname -r)"; ip -br addr 2>&1
           command -v tailscale >/dev/null && tailscale status 2>&1 | head -3; } >> "$LOG"; sync; }
snap 0
sleep 60;  snap 60
sleep 240; snap 300
T=$((WD_WAIT_PLACEHOLDER - 300)); [ $T -gt 60 ] && { sleep $((T - 60)); snap $((WD_WAIT_PLACEHOLDER - 60)); sleep 60; } || sleep $T
[ -f /etc/drift-kernel-confirmed ] && { echo "== confirmed, no revert" >> "$LOG"; exit 0; }
echo "== $(date -Is) not confirmed after WD_WAIT_PLACEHOLDERs, reverting (kernel=$(uname -r))" >> "$LOG"; sync
cp -a /boot/Image.drift-stock /boot/Image
[ -f /boot/initrd.drift-stock ] && cp -a /boot/initrd.drift-stock /boot/initrd
systemctl disable drift-kernel-watchdog.service
reboot
WDEOF
  sed -i "s/WD_WAIT_PLACEHOLDER/$WD_WAIT/g" "$WD_SH"; chmod +x "$WD_SH"
  cat > "$WD_SVC" <<'WDSEOF'
[Unit]
Description=Revert to the stock Jetson kernel unless confirmed healthy
# Deliberately NOT After=network-online.target: a kernel that comes up with no
# working NIC is one of the main things this watchdog exists to undo, so it must
# not order itself behind the very thing that is broken.
DefaultDependencies=no
After=sysinit.target local-fs.target
Conflicts=shutdown.target
Before=shutdown.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/drift-kernel-watchdog.sh
[Install]
WantedBy=multi-user.target
WDSEOF
  systemctl enable drift-kernel-watchdog.service >/dev/null 2>&1 || die "could not enable the watchdog"
  ok "auto-revert armed: stock kernel returns in ${WD_WAIT}s after boot unless you confirm"
}

install_status() {
  local d=$1 st mark nextstep=""
  echo "${B}install steps — $d${Z}"
  for st in $INSTALL_STEPS; do
    if step_done "$st" "$d"; then mark="${G}done   ${Z}"
    else mark="${Y}pending${Z}"; [[ -n $nextstep ]] || nextstep=$st; fi
    printf "  %b %-10s %s\n" "$mark" "$st" "$(step_help "$st")"
  done
  echo
  if [[ -n $nextstep ]]; then
    echo "  next:  sudo $0 install $d --step $nextstep"
    [[ $nextstep == watchdog ]] && echo "  (then reboot, and afterwards: sudo $0 confirm)"
  else
    echo "  all steps done. ${B}Reboot${Z}, then: sudo $0 confirm"
    echo "  If it never comes back, do nothing — it reverts itself and reboots."
  fi
}
next_step() { local d=$1 st; for st in $INSTALL_STEPS; do step_done "$st" "$d" || { echo "$st"; return; }; done; echo ""; }

run_step() {
  local st=$1 d=$2 force=${3:-0} prev
  # refuse to jump ahead: a kernel installed before its fallback entry exists is
  # exactly the state that turns a bad build into a site visit.
  if [[ $force != 1 ]]; then
    for prev in $INSTALL_STEPS; do
      [[ $prev == "$st" ]] && break
      step_done "$prev" "$d" || die "step '$prev' has not run yet — run it first (or --force to override)"
    done
  fi
  case $st in
    verify)    step_verify "$d";;
    backup)    step_backup;;
    bootentry) step_bootentry;;
    modules)   step_modules "$d";;
    initrd)    step_initrd "$d";;
    image)     step_image "$d";;
    watchdog)  step_watchdog;;
    *) die "unknown step '$st' (steps: $INSTALL_STEPS)";;
  esac
}

do_install() {
  local d="" flag="" want="" force=0
  while [[ $# -gt 0 ]]; do
    case $1 in
      --step) flag=step; want=${2:-}; shift 2;;
      --next|--list|--all) flag=${1#--}; shift;;
      --force) force=1; shift;;
      *) d=$1; shift;;
    esac
  done
  check_src "$d"; d=${d%/}
  case ${flag:-list} in
    list) install_status "$d";;
    step) [[ -n $want ]] || die "--step needs a step name ($INSTALL_STEPS)"
          run_step "$want" "$d" "$force"; echo; install_status "$d";;
    next) local n; n=$(next_step "$d"); [[ -n $n ]] || { ok "nothing pending"; return; }
          run_step "$n" "$d"; echo; install_status "$d";;
    all)  local st; for st in $INSTALL_STEPS; do step_done "$st" "$d" && continue; run_step "$st" "$d"; done
          echo; install_status "$d";;
  esac
}

do_confirm() {
  need_root
  local f; f=$(running_config)
  [[ -n $f ]] || die "cannot read the running config to verify"
  local miss; miss=$(missing_in "$f")
  [[ -z $miss ]] || die "running kernel still missing: ${miss}— NOT confirming; let it revert"
  ok "running kernel $KREL has every required symbol"
  if command -v criu >/dev/null 2>&1; then
    criu check >/dev/null 2>&1 && ok "criu check passes" || warn "criu check still reports gaps — inspect before relying on it"
    criu check --feature uffd-noncoop >/dev/null 2>&1 \
      && ok "post-copy (uffd-noncoop) available — the reason for this rebuild" \
      || die "uffd-noncoop still unavailable — NOT confirming"
  else
    warn "criu not installed yet, so post-copy is unverified (apt install criu, then re-run confirm)"
  fi
  touch "$CONFIRM_FLAG"
  systemctl disable drift-kernel-watchdog.service >/dev/null 2>&1
  ok "confirmed; auto-revert disarmed, new kernel permanent"
  echo "  Stock kernel kept at $BOOT_BAK and as the 'drift-stock' boot entry."
}

do_revert() {
  need_root
  [[ -f $BOOT_BAK ]] || die "no $BOOT_BAK — nothing to revert to"
  cp -a "$BOOT_BAK" "$BOOT_IMG" || die "could not restore $BOOT_IMG"
  [[ -f $INITRD_BAK ]] && { cp -a "$INITRD_BAK" "$BOOT_INITRD" && ok "stock initrd restored"; }
  rm -f "$CONFIRM_FLAG"
  systemctl disable drift-kernel-watchdog.service >/dev/null 2>&1
  ok "stock kernel restored; rebooting"
  sleep 2; reboot
}

MODE=${1:-plan}
case "$MODE" in
  plan)    do_plan;;
  sources) do_sources;;
  config)  shift; do_config "${1:-}";;
  build)   shift; do_build "${1:-}";;
  install) shift; do_install "$@";;
  confirm) do_confirm;;
  revert)  do_revert;;
  -h|--help|help) sed -n '2,40p' "$0";;
  *) echo "unknown mode: $MODE (plan | sources | config | build | install | confirm | revert)"; exit 1;;
esac
