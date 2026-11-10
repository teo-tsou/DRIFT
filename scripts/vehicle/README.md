# Vehicle Jetson: 5G-only networking

The vehicle Jetson (`jetson-nano-01`, cluster `drift-vehicle`) is the 5G UE and the extreme-edge server at once. It is managed and migrated **over 5G only**, even though its LAN port shares a subnet with the Orin. The files here are what is installed on the boards.

## Modem

- Quectel RM530N-GL (`2c7c:0801`) in QMI mode. The `6.8.12-drift2` kernel lacked `cdc-wdm`, `qmi_wwan` and `cdc_mbim`; they were built from the drift2 tree into `/lib/modules/6.8.12-drift2/extra/qmi`. `drift-jetson-kernel.sh` now enables them in `SYMS_OPT`.
- `quectel-CM` (in `~/OAI-URLLC-5G/quectel-ue`) is patched to add its default route with metric 700, so the LAN keeps the default route. The unpatched one took over the default route and cut SSH.
- `drift-quectel-cm.service` keeps the data call up (UE address 10.45.0.45/30 on `wwan0`).

## Reachability

The 5G core NATs the UE (it appears as 10.220.2.50) and does not let inbound connections reach it. So:

- **WireGuard hub `drift5g`** (`wg-drift5g-*.conf`, UDP 51845 on the hub):
  - hub 10.250.0.1, Nano 10.250.0.2 (dials out over `wwan0`), Orin 10.250.0.3 (over the LAN);
  - the hub forwards between peers, and masquerades Nano -> 10.220.2.170 onto the LAN;
  - the Jetsons run `wireguard-go`, since their kernels have no WireGuard;
  - private keys are in `/etc/wireguard/drift-*.key` on each host, not in these files.
- The Nano's K3s node IP is 10.250.0.2, so the backend, Ansible and the node agent all reach it through the tunnel.
- **`drift-5g-route`** (systemd, every 5 s):
  - re-creates table 145 (default via `wwan0`), which carries the tunnel's own packets (fwmark 0x45);
  - re-creates the route to the Orin chosen by `drift-bulk-path`;
  - keeps an `unreachable` fallback for the Orin's address, so a dropped modem never sends that traffic out the LAN port.
- **`drift-bulk-path tunnel|5g`** chooses how the Nano reaches the Orin for bulk checkpoint streams:
  - `tunnel`: through the hub;
  - `5g`: raw `wwan0`, NATed by the core, which is fine because the vehicle always opens these connections.

## Camera stream

The camera stream follows the YOLO pod through a DNAT on the Nano (`demo/emt-yolo/drift-yolo-target`). See `demo/emt-yolo/`.
