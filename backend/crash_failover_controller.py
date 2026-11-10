"""
Crash-failover for cross-cluster VMs — proposal O-6 "failover decisions".

Cross-cluster LIVE migration (cross_cluster_migration_controller) handles a
PLANNED move: the source stays alive to transfer memory + disk over the wire.
This module handles the UNPLANNED case — a home edge cluster (or the node
hosting the VM) dies, so there is no live source to migrate from. Recovery is
therefore a COLD restart of the VM on a designated failover cluster, plus
fencing so the home cluster cannot dual-run the VM when it comes back.

Warm-standby model
------------------
KubeVirt's decentralized migration leaves the *departed* cluster with the VM
object present but **Halted** (verified on hardware). That halted VM — same
spec, same secondary-NIC MAC, backed by a DataVolume on the standby's storage —
is a warm standby. Crash-failover is then simply: fence the dead home, and
flip the standby VM Halted -> Always. Because the EVPN address is carried by
the guest (static, matched by MAC), the VM reclaims its IP on boot and clients
reconnect — no re-addressing.

Honest limits (documented, not hidden)
--------------------------------------
* Data freshness: the standby's DataVolume is only as current as the last time
  the disk was replicated to it (a migration, or an out-of-band Longhorn
  backup/restore / snapshot-ship). A crash loses everything written since. For
  low RPO you must keep the standby disk current out of band; this controller
  relaunches from whatever the standby holds.
* Cold restart: in-memory / session state is lost (there is no live source to
  copy it from). Live migration is the zero-downtime path; this is the
  survive-a-crash path.
* Fencing without STONITH: we cannot power-fence a dead node. The guard is a
  software fence — best-effort halt of the home VM while reachable, plus a
  reconcile that halts the stale home VM the moment the cluster returns, before
  it can dual-claim the EVPN IP. A node that is up but partitioned from the
  controller yet still on the L2 is the residual split-brain risk STONITH would
  close.
"""

import asyncio
import logging
import time
from typing import Callable, Optional

from k8s_client import _k8s_get, _k8s_delete, _k8s_patch

logger = logging.getLogger(__name__)

KUBEVIRT_API = "/apis/kubevirt.io/v1/namespaces"


def _vm_path(ns: str, name: str) -> str:
    return f"{KUBEVIRT_API}/{ns}/virtualmachines/{name}"


def _vmi_path(ns: str, name: str) -> str:
    return f"{KUBEVIRT_API}/{ns}/virtualmachineinstances/{name}"


class CrashFailoverController:
    """Guards ONE VM: home cluster (normally active) + a warm-standby failover
    cluster. Probe the home; on sustained loss, fail over to the standby."""

    def __init__(
        self,
        home_kubeconfig: str,
        failover_kubeconfig: str,
        namespace: str,
        vm_name: str,
        log: Optional[Callable[[str, str], None]] = None,
        health_timeout: int = 120,
        force_home_down: bool = False,
    ):
        self.home = home_kubeconfig
        self.failover = failover_kubeconfig
        self.ns = namespace
        self.vm = vm_name
        self.health_timeout = health_timeout
        # Demo/test affordance: treat the home as down without killing it, so the
        # failover path (fence home -> start standby) can be exercised safely.
        self.force_home_down = force_home_down
        self._log = log or (lambda level, msg: logger.log(
            getattr(logging, level, logging.INFO), msg))

    def log(self, msg: str, level: str = "INFO"):
        self._log(level, f"[failover {self.ns}/{self.vm}] {msg}")

    # ── probes ────────────────────────────────────────────────────────────────
    def _cluster_up(self, kc: str) -> bool:
        return _k8s_get(kc, "/version") is not None

    def _vmi_ready(self, kc: str) -> bool:
        status = (_k8s_get(kc, _vmi_path(self.ns, self.vm)) or {}).get("status", {})
        if status.get("phase") != "Running":
            return False
        return any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in status.get("conditions", []))

    def home_down(self) -> bool:
        """Home is considered down if forced, or its API is unreachable, or the
        API is up but the VMI is not running (node/kubelet gone but etcd up)."""
        if self.force_home_down:
            return True
        if not self._cluster_up(self.home):
            return True
        # API reachable but the VM's instance is gone -> the hosting node failed.
        vm = _k8s_get(self.home, _vm_path(self.ns, self.vm)) or {}
        if (vm.get("spec") or {}).get("runStrategy") in ("Always", "RerunOnFailure"):
            return not self._vmi_ready(self.home)
        return False

    def status(self) -> dict:
        return {
            "vm": self.vm,
            "namespace": self.ns,
            "home_cluster_up": self._cluster_up(self.home),
            "home_vmi_ready": self._vmi_ready(self.home),
            "failover_vmi_ready": self._vmi_ready(self.failover),
            "home_down": self.home_down(),
        }

    # ── fencing ─────────────────────────────────────────────────────────────
    def _fence(self, kc: str, label: str) -> bool:
        """Software fence: halt the VM on `kc` and delete its VMI so it cannot
        run / dual-claim the EVPN IP. Best-effort — a truly dead cluster is
        unreachable, which is itself a fence until it returns."""
        if not self._cluster_up(kc):
            self.log(f"fence {label}: cluster unreachable (already fenced by being down)")
            return False
        self.log(f"fence {label}: halting VM (runStrategy -> Halted)", "WARNING")
        _k8s_patch(kc, _vm_path(self.ns, self.vm), {"spec": {"runStrategy": "Halted"}}, [])
        _k8s_delete(kc, _vmi_path(self.ns, self.vm))
        return True

    # ── failover ────────────────────────────────────────────────────────────
    async def fail_over(self) -> dict:
        self.log("CRASH FAILOVER: home considered down — recovering on standby", "WARNING")

        # 1. fence the home so it cannot dual-run when it returns
        self._fence(self.home, "home")

        # 2. the standby must hold the VM object + a bound disk (warm standby)
        standby_vm = _k8s_get(self.failover, _vm_path(self.ns, self.vm))
        if not standby_vm:
            self.log("standby has no VM object to start — cannot fail over", "ERROR")
            return {"status": "failover_failed", "vm": self.vm, "namespace": self.ns,
                    "error": "standby cluster has no pre-staged VM object (warm standby required)"}

        # 3. start the standby: Halted/WaitAsReceiver -> Always
        strat = (standby_vm.get("spec") or {}).get("runStrategy")
        if strat != "Always":
            self.log(f"starting standby VM (runStrategy {strat} -> Always)")
            err: list = []
            if _k8s_patch(self.failover, _vm_path(self.ns, self.vm),
                          {"spec": {"runStrategy": "Always"}}, err) is None:
                return {"status": "failover_failed", "vm": self.vm, "namespace": self.ns,
                        "error": f"could not start standby VM: {err}"}
        else:
            self.log("standby VM already Always")

        # 4. wait for the recovered VM to come up healthy on the standby
        deadline = time.time() + self.health_timeout
        while time.time() < deadline:
            if self._vmi_ready(self.failover):
                self.log("FAILOVER COMPLETE — VM recovered and Ready on standby "
                         "(EVPN IP reclaimed on boot)")
                return {"status": "failed_over", "vm": self.vm, "namespace": self.ns,
                        "recovered_on": "failover_cluster"}
            await asyncio.sleep(4)
        self.log("standby VM did not become Ready within timeout", "ERROR")
        return {"status": "failover_failed", "vm": self.vm, "namespace": self.ns,
                "error": "standby VM did not reach Ready"}

    async def reconcile_recovered_home(self) -> dict:
        """When a previously-down home returns while the VM is active on the
        standby, halt the stale home VM (the crash-recovery fence) so it cannot
        dual-claim the EVPN IP on the stretched L2."""
        if not self._cluster_up(self.home):
            return {"status": "home_still_down"}
        if not self._vmi_ready(self.failover):
            return {"status": "not_active_on_standby"}
        home_vm = _k8s_get(self.home, _vm_path(self.ns, self.vm)) or {}
        strat = (home_vm.get("spec") or {}).get("runStrategy")
        if strat in ("Always", "RerunOnFailure"):
            self.log("recovered home would restart a stale VM — fencing it", "WARNING")
            self._fence(self.home, "recovered home")
            return {"status": "home_fenced"}
        return {"status": "home_already_fenced", "runStrategy": strat}
