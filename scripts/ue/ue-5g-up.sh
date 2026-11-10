#!/bin/bash
# Bring a Pi UE (Quectel RM530N-GL, QMI) onto the testbed 5G SA network (PLMN 999-70).
# Resets the modem (clears an 'illegal' SIM state after a reject), restricts it to 5G NR on
# 999-70 so it never tries a commercial LTE network, starts the data call and configures wwan0.
# Run as root on the UE: sudo bash ue-5g-up.sh
set -u
TTY=${TTY:-/dev/ttyUSB2}; DEV=/dev/cdc-wdm0; IF=${IF:-wwan0}; PLMN=${PLMN:-99970}; APN=${APN:-default}
Q="qmicli -d $DEV --device-open-proxy"
at() { echo -e "$1\r" > "$TTY"; sleep 1; }
echo "== modem reset"
at 'AT+QCFG="usbnet",0'; at 'AT+QCFG="ims",0'; at 'AT+CFUN=1,1'
sleep 8; pkill -x qmi-proxy 2>/dev/null
for i in $(seq 1 30); do [ -e "$DEV" ] && [ -e /sys/class/net/$IF ] && break; sleep 2; done
[ -e "$DEV" ] || { echo "modem did not come back"; exit 1; }
sleep 5
echo "== raw-ip, 5G NR only on $PLMN"
ip link set $IF down; echo Y > /sys/class/net/$IF/qmi/raw_ip
for i in $(seq 1 10); do $Q --nas-set-system-selection-preference=5gnr,manual=$PLMN 2>&1 | grep -qi success && break; sleep 3; done
$Q --uim-get-card-status 2>&1 | grep -m1 "Application state"
echo "== waiting for registration"
for i in $(seq 1 30); do
  s=$($Q --nas-get-serving-system 2>&1)
  echo "$s" | grep -q "Registration state: 'registered'" && { echo "registered"; break; }
  sleep 3
done
echo "$s" | grep -E "Registration state|PS:|MCC|MNC" | head -4
$Q --nas-get-signal-info 2>&1 | sed -n '/5G:/,+3p'
echo "$s" | grep -q "'registered'" || { echo "NOT registered"; exit 2; }
echo "== data call"
$Q --wds-start-network="apn=$APN,ip-type=4" --client-no-release-cid 2>&1 | grep -E "Network started|error|CID|handle"
cs=$($Q --wds-get-current-settings 2>&1)
ip4=$(echo "$cs" | awk -F': ' '/IPv4 address/ {print $2}'); mask=$(echo "$cs" | awk -F': ' '/IPv4 subnet mask/ {print $2}')
gw=$(echo "$cs" | awk -F': ' '/IPv4 gateway address/ {print $2}'); mtu=$(echo "$cs" | awk -F': ' '/MTU/ {print $2}')
[ -n "$ip4" ] || { echo "no address"; echo "$cs" | head; exit 3; }
pfx=$(python3 -c "import ipaddress;print(ipaddress.IPv4Network('0.0.0.0/$mask').prefixlen)")
ip addr flush dev $IF; ip addr add $ip4/$pfx dev $IF; [ -n "$mtu" ] && ip link set $IF mtu $mtu; ip link set $IF up
echo "== $IF $ip4/$pfx gw $gw mtu $mtu"
ping -c 3 -W 2 -I $IF 10.220.2.170 | tail -2
