#!/usr/bin/env bash
#
# drift-preflight.sh — validate an "Envelope VM" (or any host) for DRIFT, and
# safely switch it to cgroup v1 for parity with the validated stack.
#
# DRIFT relocates edge workloads live — KubeVirt VMs and CRIU/post-copy
# containers — across clusters. That rests on a few host capabilities that are
# easy to get wrong on a fresh VM: hardware virt for KubeVirt, userfaultfd for
# the memory-independent (post-copy) container freeze, the ability to put an
# extra MAC on the NIC (the floating-IP follow + EVPN stretch that keep TCP
# sessions alive), reachability between nodes for the overlay, and a cgroup mode
# the CRIU cross-host restore path was validated on (v1).
#
# ARM64 / Jetson: a vendor (L4T) kernel is configured for appliances, not for
# checkpoint/restore, so the gates that a cloud VM passes for free are exactly
# the ones a Jetson fails — and each missing symbol costs a kernel rebuild and a
# reflash, not an apt install. This script therefore also reads the kernel
# .config directly: it is cheaper to learn the rebuild list once, here, than one
# failed migration at a time. Two ARM facts it will report and cannot fix:
# hardware virt depends on the bootloader entering the kernel at EL2 (Tegra does
# not, by default), and soft-dirty page tracking does not exist on arm64
# upstream, so iterative --pre-copy is unavailable there for good. Neither stops
# the post-copy container path, which is the one the extreme edge runs.
#
# MODES
#   check [peer_ip ...]     (default) read-only readiness table; add peer IPs to
#                           test VM<->VM on the shared L2 (the floating-IP path).
#   cgroup-v1               watchdog-protected switch to cgroup v1 (reboots).
#   confirm                 after that reboot: verify healthy, make v1 permanent,
#                           disarm the auto-revert. Refuses if the node isn't OK.
#   udp-listen [port]       start a UDP echo listener (default 4789) for the peer
#                           overlay-reachability test. Run on one node.
#   udp-probe ip [port]     from another node, probe that listener; PASS = the
#                           overlay port (VXLAN 4789 / Geneve 6081) gets through.
#
# The mutating modes (cgroup-v1, confirm) are the ONLY ones that change anything;
# `check` and the udp-* helpers are safe to run anywhere, anytime.
#
# Exit code in `check`: 0 = all gates green, 1 = a hard gate failed, 2 = warnings.

set -uo pipefail

# ── pretty ───────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[1m'; Z=$'\e[0m'
else G=""; R=""; Y=""; B=""; Z=""; fi
PASS_N=0; WARN_N=0; FAIL_N=0
row() { # row <label> <PASS|WARN|FAIL> <detail>
  local s=$2 c
  case $s in PASS) c=$G; ((PASS_N++));; WARN) c=$Y; ((WARN_N++));; FAIL) c=$R; ((FAIL_N++));; esac
  printf "  %b%-5s%b  %-30s %s\n" "$c" "$s" "$Z" "$1" "${3:-}"
}
need_root() { [[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }; }

GRUB=/etc/default/grub
GRUB_BAK=/etc/default/grub.pre-cgroup-v1
EXTLINUX=/boot/extlinux/extlinux.conf          # U-Boot boot chain (Jetson, most SBCs)
EXTLINUX_BAK=/boot/extlinux/extlinux.conf.pre-cgroup-v1
CONFIRM_FLAG=/etc/cgroup-v1-confirmed
WD_SVC=/etc/systemd/system/cgroup-watchdog.service
WD_SH=/usr/local/sbin/cgroup-watchdog.sh
REVERT_SH=/usr/local/sbin/cgroup-revert.sh     # bootloader-specific undo, written at switch time
OVERLAY_PORTS_DEFAULT="4789 6081"   # EVPN VXLAN, kube-ovn Geneve

ARCH=$(uname -m)
# Jetson identifies itself two ways; either is enough to tailor the advice.
JETSON=""
if [[ -r /etc/nv_tegra_release ]]; then
  JETSON=$(awk 'NR==1{print "L4T " $2 "." $5}' /etc/nv_tegra_release 2>/dev/null | tr -d ',')
elif [[ -r /proc/device-tree/model ]] && grep -qi tegra /proc/device-tree/model 2>/dev/null; then
  JETSON=$(tr -d '\0' < /proc/device-tree/model)
fi

# Kernel symbols CRIU actually needs. This list is not aspirational: it is the
# set present on the validated x86 node, minus the ones CRIU never exercises.
# CHECKPOINT_RESTORE is the one vendor kernels most often omit (it is hidden
# behind CONFIG_EXPERT), and the *_DIAG family is what lets CRIU dump sockets —
# without them a workload with an open connection cannot be checkpointed at all.
KCONF_CRIU="CHECKPOINT_RESTORE NAMESPACES PID_NS NET_NS UTS_NS IPC_NS CGROUPS
            FHANDLE EVENTFD EPOLL INOTIFY_USER UNIX_DIAG INET_DIAG INET_TCP_DIAG
            INET_UDP_DIAG PACKET_DIAG NETLINK_DIAG NETFILTER_XT_MARK TUN USERFAULTFD"
# Dataplane: kube-ovn (OPENVSWITCH), EVPN stretch (VXLAN/BRIDGE), the floating-IP
# follow (MACVLAN), pod networking (VETH), CRIU's network lock (NF_TABLES).
KCONF_NET="VETH BRIDGE VXLAN GENEVE MACVLAN OPENVSWITCH NF_TABLES NF_CONNTRACK_ZONES NF_CONNTRACK_MARK NF_CONNTRACK_LABELS NFT_CT NETFILTER_XT_MATCH_MARK NETFILTER_XT_MATCH_PHYSDEV NET_SCH_NETEM"
# GENEVE: kube-ovn start-ovs.sh requires the module even with VXLAN tunnels.
# NF_CONNTRACK_ZONES/MARK/LABELS: OVN programs ct zones/marks/labels; without them
# ovs-vswitchd rejects its flows (NXBAC_CT_DATAPATH_SUPPORT) and the node stays
# NotReady. NFT_CT / xt mark / physdev: kube-proxy and kube-ovn rules via
# iptables-nft. NET_SCH_NETEM: the controlled latency emulation (FR4).
# Storage: the checkpoint store is NFS, and Longhorn RWX needs iSCSI + an NFSv4
# client in-kernel. Vendor kernels routinely ship neither.
KCONF_STORE="NFS_FS NFS_V4 ISCSI_TCP"

kconfig_src() {
  if   [[ -r /proc/config.gz ]];           then echo /proc/config.gz
  elif [[ -r /boot/config-$(uname -r) ]];  then echo "/boot/config-$(uname -r)"
  else echo ""; fi
}
kconfig_val() {  # kconfig_val <SYMBOL> <src> -> y | m | n
  local line
  if [[ $2 == *.gz ]]; then line=$(zcat "$2" 2>/dev/null | grep -m1 -E "^CONFIG_$1=")
  else                      line=$(grep -m1 -E "^CONFIG_$1=" "$2" 2>/dev/null); fi
  [[ -n $line ]] && echo "${line#*=}" || echo n
}
kconfig_missing() {  # kconfig_missing <src> <symbols...> -> prints the absent ones
  local src=$1; shift
  for s in $*; do [[ $(kconfig_val "$s" "$src") == n ]] && printf '%s ' "$s"; done
}

primary_nic() { ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++)if($i=="dev"){print $(i+1);exit}}'; }
nic_cidr()    { ip -o -4 addr show dev "$1" 2>/dev/null | awk '{print $4; exit}'; }        # a.b.c.d/pp
nic_gw()      { ip route show default dev "$1" 2>/dev/null | awk '{print $3; exit}'; }

# pick a probably-free host IP on the NIC's /24 (probe .240..250, fall back .99)
pick_spare_ip() {
  local cidr=$1 net pfx b3
  net=${cidr%/*}; pfx=${cidr#*/}; b3=${net%.*}
  local ip
  for h in 240 241 242 243 244 245 246 247 248 249 250; do
    ip="$b3.$h"
    ping -c1 -W1 "$ip" >/dev/null 2>&1 || { echo "$ip/$pfx"; return; }
  done
  echo "$b3.99/$pfx"
}

# ── individual checks (used by `check`) ──────────────────────────────────────
check_cgroup() {
  local t; t=$(stat -fc %T /sys/fs/cgroup 2>/dev/null)
  if [[ $t == tmpfs ]]; then row "cgroup mode" PASS "v1 (tmpfs) — parity with validated stack"
  elif [[ $t == cgroup2fs ]]; then row "cgroup mode" WARN "v2 — run '$0 cgroup-v1' for parity (CRIU cross-host path is v1-validated)"
  else row "cgroup mode" WARN "unknown ($t)"; fi
}
check_kvm() {
  # ARM has no vmx/svm flag to grep: KVM is available only if the bootloader
  # entered the kernel at EL2. Tegra boots Orin at EL2 (the kernel logs "VHE mode
  # initialized") but ships an incomplete GICv3 description, so /dev/kvm can be
  # present while guests still fail to start until the device tree is patched and
  # the kernel rebuilt — and NVIDIA does not support that configuration. A FAIL
  # here disables the VM migration path only; the CRIU container path is
  # unaffected, which is what the extreme-edge nodes run.
  if [[ $ARCH != x86_64 ]]; then
    local msg; msg=$(dmesg 2>/dev/null | grep -iE 'kvm.*(VHE mode|HYP mode|hyp mode)' | tail -1)
    msg=${msg##*] }
    if [[ -e /dev/kvm ]]; then
      row "hardware virt (KubeVirt)" PASS "/dev/kvm present${msg:+ — $msg}"
    elif [[ -n $JETSON ]]; then
      row "hardware virt (KubeVirt)" WARN "no /dev/kvm on Tegra${msg:+ ($msg)} — VM path off; containers unaffected"
    else
      row "hardware virt (KubeVirt)" FAIL "no /dev/kvm${msg:+ — $msg} — CPU not entered at EL2"
    fi
    return
  fi
  local flags; flags=$(grep -Ec '(vmx|svm)' /proc/cpuinfo)
  if [[ $flags -gt 0 && -e /dev/kvm ]]; then
    row "hardware virt (KubeVirt)" PASS "$flags vCPU expose vmx/svm, /dev/kvm present"
  elif [[ $flags -gt 0 ]]; then
    row "hardware virt (KubeVirt)" WARN "flags present but /dev/kvm missing — modprobe kvm_intel/kvm_amd"
  else
    row "hardware virt (KubeVirt)" FAIL "no vmx/svm — hypervisor isn't passing virt (VM path needs nesting)"
  fi
}
check_kernel_config() {
  local src; src=$(kconfig_src)
  if [[ -z $src ]]; then
    row "kernel config (CRIU)" WARN "no /proc/config.gz nor /boot/config-$(uname -r) — rebuild with CONFIG_IKCONFIG_PROC to verify"
    return
  fi
  local miss
  miss=$(kconfig_missing "$src" "$KCONF_CRIU")
  if [[ -z $miss ]]; then row "kernel config (CRIU)" PASS "all C/R symbols present ($(basename "$src"))"
  else row "kernel config (CRIU)" FAIL "REBUILD NEEDED, missing: ${miss}"; fi
  miss=$(kconfig_missing "$src" "$KCONF_NET")
  if [[ -z $miss ]]; then row "kernel config (dataplane)" PASS "veth/bridge/vxlan/geneve/macvlan/ovs/nftables + conntrack zones/marks/labels, nft ct, netem present"
  else row "kernel config (dataplane)" FAIL "REBUILD NEEDED, missing: ${miss}"; fi
  miss=$(kconfig_missing "$src" "$KCONF_STORE")
  if [[ -z $miss ]]; then row "kernel config (storage)" PASS "NFS client + iSCSI present"
  else row "kernel config (storage)" WARN "missing: ${miss}— checkpoint store (NFS) / Longhorn RWX affected"; fi
}
check_precopy() {
  # Soft-dirty PTE tracking is what makes `criu pre-dump` incremental. arm64 has
  # never selected HAVE_ARCH_SOFT_DIRTY upstream — the 2024 patches were dropped
  # in favour of uffd-wp — so this is a permanent no on ARM, not a config gap a
  # rebuild can close. It costs nothing here: post-copy is the memory-independent
  # path and DRIFT already drops pre-copy when --post-copy is set.
  if criu check --feature mem_dirty_track >/dev/null 2>&1; then
    row "pre-copy (soft-dirty)" PASS "memory tracking on — iterative --pre-copy N usable"
  elif [[ $ARCH == aarch64 ]]; then
    row "pre-copy (soft-dirty)" WARN "absent on arm64 upstream — migrate with --post-copy, omit --pre-copy (no rebuild fixes this)"
  else
    row "pre-copy (soft-dirty)" WARN "off — set CONFIG_MEM_SOFT_DIRTY for iterative --pre-copy"
  fi
}
check_swap() {
  # kubelet refuses to start with swap on (unless failSwapOn=false), and L4T
  # enables zram swap by default via nvzramconfig.service — so a fresh Jetson
  # fails the k3s join that the same playbook passes on a cloud VM.
  local on; on=$(awk 'NR>1{printf "%s ", $1}' /proc/swaps 2>/dev/null)
  if [[ -z $on ]]; then row "swap off (kubelet)" PASS "no active swap"
  else row "swap off (kubelet)" FAIL "active: ${on}— swapoff -a${JETSON:+ && systemctl disable nvzramconfig}"; fi
}
check_arch() {
  # A CRIU image is machine code, register state and raw pages: it restores only
  # on the same architecture AND the same page size. A mixed x86/ARM estate is
  # fine for DRIFT as a whole, but each container can only migrate between peers
  # that match on both — the selector must never pair an x86 edge with a Jetson.
  row "arch / page size" PASS "$ARCH, $(getconf PAGESIZE)B pages${JETSON:+, $JETSON} — CRIU peers must match both"
}
check_criu() {
  if ! command -v criu >/dev/null 2>&1; then
    row "CRIU present" FAIL "not installed — add-apt-repository ppa:criu/ppa && apt install criu"
    return
  fi
  local ver; ver=$(criu --version 2>/dev/null | awk 'NR==1{print $2}')
  if criu check >/dev/null 2>&1; then row "CRIU checkpoint/restore" PASS "v${ver:-?} — 'Looks good.'"
  else row "CRIU checkpoint/restore" FAIL "v${ver:-?} — 'criu check' failed (kernel missing C/R features)"; fi
  # post-copy (memory-independent freeze) depends on non-cooperative userfaultfd
  if criu check --feature uffd-noncoop >/dev/null 2>&1; then
    row "post-copy (uffd-noncoop)" PASS "supported — memory-independent freeze available"
  else
    row "post-copy (uffd-noncoop)" WARN "unsupported — pod path falls back to blocking (freeze scales with RAM)"
  fi
}
check_macvlan_egress() {
  local nic=$1 cidr gw spare mv=drift_pfck
  # Without root `ip link add` just fails, which reads as "module missing" when
  # nothing is wrong — so say which it is. Wireless is NOT pre-judged here: the
  # textbook answer is that 802.11 binds one MAC per station and the AP drops the
  # second one, but a Jetson on Wi-Fi measured otherwise on a real AP, so the
  # ping decides and wireless only qualifies the verdict.
  if [[ $EUID -ne 0 ]]; then
    row "extra-MAC egress (macvlan)" WARN "needs root — rerun with sudo (not tested)"; return
  fi
  local wifi=""
  [[ -d /sys/class/net/$nic/wireless || $nic == wl* ]] && wifi=1
  cidr=$(nic_cidr "$nic"); gw=$(nic_gw "$nic")
  if [[ -z $cidr || -z $gw ]]; then row "extra-MAC egress (macvlan)" WARN "could not derive CIDR/gateway on $nic"; return; fi
  spare=${SPARE_IP:-$(pick_spare_ip "$cidr")}
  ip link add "$mv" link "$nic" type macvlan mode bridge 2>/dev/null || { row "extra-MAC egress (macvlan)" WARN "cannot create macvlan (module?)"; return; }
  trap 'ip link del '"$mv"' 2>/dev/null || true' RETURN
  ip link set "$mv" address 02:00:de:ad:be:ef 2>/dev/null
  ip addr add "$spare" dev "$mv" 2>/dev/null
  ip link set "$mv" up 2>/dev/null
  if ping -c2 -W2 -I "$mv" "$gw" >/dev/null 2>&1; then
    row "extra-MAC egress (macvlan)" PASS "fabricated MAC + $spare reached gw $gw (EVPN/l2-ip viable)${wifi:+ — over Wi-Fi, so re-test on the demo AP}"
  else
    row "extra-MAC egress (macvlan)" FAIL "BLOCKED — ${wifi:+802.11 is dropping the 2nd MAC; move to wired Ethernet. Otherwise }hypervisor anti-spoof; session continuity at risk"
  fi
  ip link del "$mv" 2>/dev/null || true; trap - RETURN
}
check_peer_l2() { # VM<->VM on the shared L2 from a second MAC (the floating-IP path)
  local nic=$1; shift
  local cidr gw spare mv=drift_pfl2
  if [[ $EUID -ne 0 ]]; then
    row "VM<->VM on shared L2" WARN "needs root — rerun with sudo (not tested)"; return
  fi
  cidr=$(nic_cidr "$nic"); spare=${SPARE_IP:-$(pick_spare_ip "$cidr")}
  ip link add "$mv" link "$nic" type macvlan mode bridge 2>/dev/null || { row "VM<->VM on shared L2" WARN "cannot create macvlan"; return; }
  trap 'ip link del '"$mv"' 2>/dev/null || true' RETURN
  ip link set "$mv" address 02:00:de:ad:be:f0 2>/dev/null
  ip addr add "$spare" dev "$mv" 2>/dev/null; ip link set "$mv" up 2>/dev/null
  local ok=1 miss=""
  for p in "$@"; do ping -c2 -W2 -I "$mv" "$p" >/dev/null 2>&1 || { ok=0; miss+="$p "; }; done
  if [[ $ok == 1 ]]; then row "VM<->VM on shared L2" PASS "peers reachable from a 2nd MAC: $*"
  else row "VM<->VM on shared L2" FAIL "unreachable from 2nd MAC: ${miss}— L2 isolation? floating-IP hop won't work"; fi
  ip link del "$mv" 2>/dev/null || true; trap - RETURN
}

do_check() {
  local nic; nic=$(primary_nic)
  echo "${B}DRIFT preflight — $(hostname) (${nic:-?} $(nic_cidr "${nic:-lo}"))${Z}"
  [[ $EUID -eq 0 ]] || echo "  ${Y}note${Z}  not root: macvlan/L2 gates and the dmesg virt detail are skipped — rerun with sudo for a full verdict"
  echo
  check_arch
  check_kvm
  check_kernel_config
  check_criu
  check_precopy
  check_swap
  check_macvlan_egress "$nic"
  check_cgroup
  if [[ $# -gt 0 ]]; then check_peer_l2 "$nic" "$@"
  else row "VM<->VM on shared L2" WARN "no peers given — rerun: $0 check <peer_ip> [peer_ip...]"; fi
  row "overlay port reachability" WARN "paired test — '$0 udp-listen' on one node, '$0 udp-probe <ip>' on another (4789 & 6081)"
  echo
  echo "  ${G}$PASS_N pass${Z}  ${Y}$WARN_N warn${Z}  ${R}$FAIL_N fail${Z}"
  [[ $FAIL_N -gt 0 ]] && return 1; [[ $WARN_N -gt 0 ]] && return 2; return 0
}

# ── cgroup-v1 safe switch (watchdog-protected; user cannot manage the infra) ─
do_cgroup_v1() {
  need_root
  if [[ $(stat -fc %T /sys/fs/cgroup) == tmpfs ]]; then echo "already cgroup v1 (tmpfs) — nothing to do"; exit 0; fi

  # Two boot chains in the estate: GRUB on the x86 edges, U-Boot+extlinux on the
  # Jetsons. Same contract either way — back the config up, verify the flag
  # actually landed before rebooting, and leave a revert script the watchdog can
  # run without knowing which bootloader it is on.
  if [[ -f $GRUB ]] && command -v update-grub >/dev/null 2>&1; then
    cp -a "$GRUB" "$GRUB_BAK"
    grep -q 'systemd.unified_cgroup_hierarchy=0' "$GRUB" || \
      sed -i 's/\(GRUB_CMDLINE_LINUX="[^"]*\)"/\1 systemd.unified_cgroup_hierarchy=0"/' "$GRUB"
    if ! update-grub; then echo "${R}update-grub FAILED — restoring backup, NOT rebooting${Z}"; cp -a "$GRUB_BAK" "$GRUB"; exit 1; fi
    if [[ $(grep -c 'unified_cgroup_hierarchy=0' /boot/grub/grub.cfg) -lt 1 ]]; then
      echo "${R}flag not in grub.cfg — restoring backup, NOT rebooting${Z}"; cp -a "$GRUB_BAK" "$GRUB"; update-grub; exit 1; fi
    printf '#!/bin/bash\ncp -a %s %s\nupdate-grub\n' "$GRUB_BAK" "$GRUB" > "$REVERT_SH"
  elif [[ -f $EXTLINUX ]]; then
    # No GRUB here: the kernel cmdline is the APPEND line of each LABEL block.
    # Every block gets the flag, so a fallback entry can't silently boot back to
    # cgroup v2 and leave CRIU restores failing for a reason nobody looks at.
    cp -a "$EXTLINUX" "$EXTLINUX_BAK"
    grep -q 'systemd.unified_cgroup_hierarchy=0' "$EXTLINUX" || \
      sed -i '/^[[:space:]]*APPEND/ s/$/ systemd.unified_cgroup_hierarchy=0/' "$EXTLINUX"
    if ! grep -q 'systemd.unified_cgroup_hierarchy=0' "$EXTLINUX"; then
      echo "${R}no APPEND line edited in $EXTLINUX — restoring backup, NOT rebooting${Z}"; cp -a "$EXTLINUX_BAK" "$EXTLINUX"; exit 1; fi
    printf '#!/bin/bash\ncp -a %s %s\n' "$EXTLINUX_BAK" "$EXTLINUX" > "$REVERT_SH"
  else
    echo "${R}no supported bootloader config found ($GRUB or $EXTLINUX) — refusing${Z}"; exit 1
  fi
  chmod +x "$REVERT_SH"

  # dead-man's-switch: if the node isn't confirmed healthy within 15 min of the
  # next boot, it reverts grub and reboots back to the known-good config. Covers
  # "boots but I can't reach it" without any console access.
  cat > "$WD_SH" <<'EOF'
#!/bin/bash
[ -f /etc/cgroup-v1-confirmed ] && exit 0
sleep 900
[ -f /etc/cgroup-v1-confirmed ] && exit 0
/usr/local/sbin/cgroup-revert.sh
systemctl disable cgroup-watchdog.service; reboot
EOF
  chmod +x "$WD_SH"
  cat > "$WD_SVC" <<'EOF'
[Unit]
Description=Auto-revert cgroup change unless confirmed healthy
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/cgroup-watchdog.sh
[Install]
WantedBy=multi-user.target
EOF
  systemctl enable cgroup-watchdog.service
  echo "${G}verified.${Z} bootloader config backed up, 15-min auto-revert armed ($REVERT_SH)."
  echo "Rebooting. After it returns, run:  ${B}$0 confirm${Z}"
  echo "If you can't get back in, do nothing — it self-reverts to the current healthy state."
  sleep 2; reboot
}

do_confirm() {
  need_root
  local ok=1
  [[ $(stat -fc %T /sys/fs/cgroup) == tmpfs ]] || { echo "${R}cgroup is not v1 (tmpfs) — refusing${Z}"; ok=0; }
  if command -v criu >/dev/null 2>&1; then criu check >/dev/null 2>&1 || { echo "${R}criu check failed on this kernel — refusing${Z}"; ok=0; }; fi
  if [[ $ok == 1 ]]; then
    touch "$CONFIRM_FLAG"; systemctl disable cgroup-watchdog.service 2>/dev/null || true
    echo "${G}confirmed healthy on cgroup v1 ($(uname -r)); auto-revert disarmed, change permanent.${Z}"
  else
    echo "${Y}NOT confirming — leave it; the watchdog will auto-revert to the healthy config.${Z}"; exit 1
  fi
}

# ── overlay-port paired reachability (VXLAN 4789 / Geneve 6081) ──────────────
do_udp_listen() {
  local port=${1:-4789}
  echo "UDP echo listener on :$port  (Ctrl-C to stop)"
  python3 - "$port" <<'PY'
import socket,sys
p=int(sys.argv[1]); s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("0.0.0.0",p))
print(f"listening udp/{p}")
while True:
    d,a=s.recvfrom(2048); print("got from",a); s.sendto(b"DRIFT-OK",a)
PY
}
do_udp_probe() {
  local ip=$1 port=${2:-4789}
  python3 - "$ip" "$port" <<'PY'
import socket,sys
ip,p=sys.argv[1],int(sys.argv[2])
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(3)
s.sendto(b"DRIFT-PROBE",(ip,p))
try:
    d,_=s.recvfrom(2048)
    print(f"PASS udp/{p} to {ip} — reply {d!r}"); sys.exit(0)
except socket.timeout:
    print(f"FAIL udp/{p} to {ip} — no reply (blocked, or no listener there)"); sys.exit(1)
PY
}

# ── dispatch ─────────────────────────────────────────────────────────────────
case "${1:-check}" in
  check)      shift; do_check "$@";;
  cgroup-v1)  do_cgroup_v1;;
  confirm)    do_confirm;;
  udp-listen) shift; do_udp_listen "$@";;
  udp-probe)  shift; [[ $# -ge 1 ]] || { echo "usage: $0 udp-probe <ip> [port]"; exit 1; }; do_udp_probe "$@";;
  -h|--help|help) sed -n '2,40p' "$0";;
  *) echo "unknown mode: $1 (try: check | cgroup-v1 | confirm | udp-listen | udp-probe | help)"; exit 1;;
esac
