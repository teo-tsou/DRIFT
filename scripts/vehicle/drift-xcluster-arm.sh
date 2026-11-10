#!/bin/bash
# Make the two ARM clusters' pod networks reach each other through the drift5g
# WireGuard tunnel, for KubeVirt cross-cluster (decentralized) VM live
# migration: KubeVirt's synchronization controllers and migration streams dial
# PEER POD IPs, and the 5G core blocks inbound to the vehicle, so only the
# tunnel can carry the edge -> vehicle direction.
#   Orin cluster pods 10.46.0.0/16 (peer 10.250.0.3)
#   Nano cluster pods 10.48.0.0/16 (peer 10.250.0.2)
# Additive only: existing AllowedIPs and routes stay. Each change is applied
# live (`wg set`, no tunnel restart) and saved to the config, after a backup.
# Run on the hub (10.220.3.20) as root; it reaches the Jetsons over SSH.
set -euo pipefail
NANO_PUB=qr33nwvfAPYkSa+R4GpkcF4xGXS30vEigJv4xAhMsHI=
ORIN_PUB=axsCcwyAZRVnD7mStXoNGYfw2bFg8JPhjGZPs98gRjc=
HUB_PUB=ykanA4vu5P8Gcca6MslhjEa//iOgW1MEwZTn3aiUs1s=
ORIN=ecb@10.220.2.170
NANO="-o HostKeyAlias=10.220.2.171 ecb@10.250.0.2"
KEY="-i /root/.ssh/id_rsa"
TS=$(date +%Y%m%d-%H%M)

# addline FILE OLD NEW: replace one exact line, refuse if it is not there once
addline='import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
if new in s:
    sys.exit(0)                      # already applied
assert s.count(old) == 1, f"{p}: expected one line {old!r}"
open(p, "w").write(s.replace(old, new))'

echo "== hub"
cp /etc/wireguard/drift5g.conf /etc/wireguard/drift5g.conf.bak-$TS
wg set drift5g peer $NANO_PUB allowed-ips 10.250.0.2/32,10.48.0.0/16
wg set drift5g peer $ORIN_PUB allowed-ips 10.250.0.3/32,10.46.0.0/16
ip route replace 10.48.0.0/16 dev drift5g
ip route replace 10.46.0.0/16 dev drift5g
python3 -c "$addline" /etc/wireguard/drift5g.conf "AllowedIPs = 10.250.0.2/32" "AllowedIPs = 10.250.0.2/32, 10.48.0.0/16"
python3 -c "$addline" /etc/wireguard/drift5g.conf "AllowedIPs = 10.250.0.3/32" "AllowedIPs = 10.250.0.3/32, 10.46.0.0/16"
wg show drift5g allowed-ips

echo "== orin"
ssh $KEY $ORIN "sudo -n bash -s" <<EOF
set -e
cp /etc/wireguard/drift5g.conf /etc/wireguard/drift5g.conf.bak-$TS
wg set drift5g peer $HUB_PUB allowed-ips 10.250.0.0/24,10.48.0.0/16
ip route replace 10.48.0.0/16 dev drift5g
python3 -c '$addline' /etc/wireguard/drift5g.conf "AllowedIPs = 10.250.0.0/24" "AllowedIPs = 10.250.0.0/24, 10.48.0.0/16"
wg show drift5g allowed-ips
EOF

echo "== nano (over the tunnel itself: the change only adds to it)"
ssh $KEY $NANO "sudo -n bash -s" <<EOF
set -e
cp /etc/wireguard/drift5g.conf /etc/wireguard/drift5g.conf.bak-$TS
wg set drift5g peer $HUB_PUB allowed-ips 10.250.0.0/24,10.220.2.170/32,10.46.0.0/16
ip route replace 10.46.0.0/16 dev drift5g
python3 -c '$addline' /etc/wireguard/drift5g.conf "AllowedIPs = 10.250.0.0/24, 10.220.2.170/32" "AllowedIPs = 10.250.0.0/24, 10.220.2.170/32, 10.46.0.0/16"
wg show drift5g allowed-ips
EOF

echo "== check: each side reaches the other's KubeVirt API pod"
ssh $KEY $ORIN "sudo -n kubectl -n kubevirt get pod -l kubevirt.io=virt-api -o jsonpath='{.items[0].status.podIP}'" > /tmp/orin-virt-api
ssh $KEY $NANO "sudo -n kubectl -n kubevirt get pod -l kubevirt.io=virt-api -o jsonpath='{.items[0].status.podIP}'" > /tmp/nano-virt-api
ssh $KEY $ORIN "timeout 5 bash -c 'cat </dev/null >/dev/tcp/$(cat /tmp/nano-virt-api)/8443' && echo 'orin -> nano pod OK'"
ssh $KEY $NANO "timeout 5 bash -c 'cat </dev/null >/dev/tcp/$(cat /tmp/orin-virt-api)/8443' && echo 'nano -> orin pod OK'"
