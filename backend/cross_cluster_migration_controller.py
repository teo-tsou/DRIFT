"""
Cross-cluster (decentralized) KubeVirt live migration — SBI orchestration.

Executes a VM live migration from a SOURCE edge cluster to a TARGET edge
cluster using KubeVirt's decentralized live migration. The backend is the only
component holding both kubeconfigs; drift-controller calls this over HTTP and
stays kubeconfig-free.

Prerequisite (one-time per cluster pair, handled by the ansible role
`xcluster_migration`): DecentralizedLiveMigration feature gate on, and a
cross-cluster pod-CIDR route so the clusters can reach each other's sync
endpoint. virt-operator MUST stay running.

Cross-cluster CA trust is NOT a prerequisite: this controller establishes it as
step 0 of every migration (see _ensure_ca_trust). KubeVirt rotates its internal
CA ~hourly, so a mutual peer-CA trust must be fresh at the moment the sync
connection is made. Doing it here — guarded by a freshness check, and before the
sync connection exists — is correct-by-construction (no cold-start gap for a
new pair) and effectively free: the operator's merge overlaps receiver bring-up.

Proven sequence (raw-kubectl equivalent validated on hardware):
  0. ensure mutual CA trust (fresh peer kubevirt-ca merged into local one)
  1. read the source VM spec
  2. create a blank RWX DataVolume + a WaitAsReceiver VM on the target
  3. wait for the receiver VMI to reach WaitingForSync
  4. create the receiver VMIM; read its status.synchronizationAddresses
  5. create the sender VMIM on the source with connectURL = that address
  6. poll both VMIMs to Succeeded (memory transfer + cutover)
  7. post-migration health gate: require the target VMI Running + Ready
  8. if the health gate fails: roll back (reverse migration) to the previous
     edge, so an unhealthy cutover never leaves the service stranded
  on error: delete both VMIMs + the target VM + DV (no auto-retry in KubeVirt)

Proposal objective O-6 (fault tolerance / safe rollback, KPI-4 "rollback
recovery rate"): a Succeeded VMIM only proves the memory transfer + cutover
completed, not that the VM came up healthy on the new edge. Steps 7-8 close
that gap — "fallback to the previous edge if post-migration health checks fail".
"""

import asyncio
import copy
import logging
import os
import re
import time
import uuid
from typing import Callable, Optional

from k8s_client import _k8s_get, _k8s_post, _k8s_delete, _k8s_patch

logger = logging.getLogger(__name__)

KV = "kubevirt.io/v1"
CDI = "cdi.kubevirt.io/v1beta1"

# Warm cross-cluster trust cache, shared across migrations in this process:
#   (source_kubeconfig, target_kubeconfig) -> {"certs": frozenset, "at": epoch}
# After a cold establish the sync-controllers hold the 2-cert trust in memory,
# so a subsequent migration with the SAME peer CAs, within the warm window, can
# skip the slow sync-controller reload + settle. Invalidated on any warm-path
# failure and whenever the peer CAs change (CA rotation).
_CA_WARM: dict = {}


def _kv_ns(ns: str) -> str:
    return f"/apis/{KV}/namespaces/{ns}"


def _vm_path(ns: str, name: str) -> str:
    return f"{_kv_ns(ns)}/virtualmachines/{name}"


def _vmi_path(ns: str, name: str) -> str:
    return f"{_kv_ns(ns)}/virtualmachineinstances/{name}"


def _vmim_coll(ns: str) -> str:
    return f"{_kv_ns(ns)}/virtualmachineinstancemigrations"


def _vmim_path(ns: str, name: str) -> str:
    return f"{_vmim_coll(ns)}/{name}"


def _dv_path(ns: str, name: str) -> str:
    # An empty name means the COLLECTION path (for a create POST); it must NOT
    # have a trailing slash — k8s returns a plaintext "404 page not found" for
    # `.../datavolumes/`, which then fails JSON parsing ("Extra data: ...").
    base = f"/apis/{CDI}/namespaces/{ns}/datavolumes"
    return f"{base}/{name}" if name else base


KUBEVIRT_NS = "kubevirt"


def _cm_path(name: str, ns: str = KUBEVIRT_NS) -> str:
    return f"/api/v1/namespaces/{ns}/configmaps/{name}"


def _pem_certs(bundle: str) -> set:
    """Normalized set of the individual CERTIFICATE blocks in a PEM bundle."""
    blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", bundle or "", re.DOTALL
    )
    return {"".join(b.split()) for b in blocks}


def _union_pem(*bundles: str) -> str:
    """Concatenate the CERTIFICATE blocks across `bundles`, de-duplicated."""
    seen: set = set()
    out: list = []
    for b in bundles:
        for m in re.findall(
            r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", b or "", re.DOTALL
        ):
            key = "".join(m.split())
            if key not in seen:
                seen.add(key)
                out.append(m.strip())
    return "\n".join(out) + "\n"


def _kubevirt_ca_bundle(kubeconfig: str) -> str:
    cm = _k8s_get(kubeconfig, _cm_path("kubevirt-ca"))
    return (cm or {}).get("data", {}).get("ca-bundle", "")


def _build_receiver_vm(source_vm: dict) -> dict:
    """Mirror the source VM as a WaitAsReceiver target VM."""
    spec = source_vm["spec"]
    tmpl = spec["template"]
    annotations = {
        k: v
        for k, v in (source_vm["metadata"].get("annotations") or {}).items()
        if not k.startswith("kubectl.kubernetes.io")
        and not k.startswith("kubevirt.io/latest-observed")
        and not k.startswith("kubevirt.io/storage-observed")
    }
    # Restore the source's run strategy on the target once migration completes.
    annotations["kubevirt.io/restore-run-strategy"] = "Always"
    return {
        "apiVersion": KV,
        "kind": "VirtualMachine",
        "metadata": {
            "name": source_vm["metadata"]["name"],
            "namespace": source_vm["metadata"]["namespace"],
            "annotations": annotations,
        },
        "spec": {
            "runStrategy": "WaitAsReceiver",
            "template": {
                "metadata": tmpl.get("metadata", {}),
                "spec": tmpl["spec"],
            },
        },
    }


def _build_blank_dv(source_dv: dict, ns: str, name: str) -> dict:
    """A blank RWX DataVolume on the target to receive the disk over the wire."""
    src_storage = source_dv["spec"].get("storage") or source_dv["spec"].get("pvc") or {}
    size = (
        src_storage.get("resources", {}).get("requests", {}).get("storage", "20Gi")
    )
    sc = src_storage.get("storageClassName", "longhorn-rwx")
    return {
        "apiVersion": CDI,
        "kind": "DataVolume",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "source": {"blank": {}},
            "storage": {
                "storageClassName": sc,
                "volumeMode": "Filesystem",
                "accessModes": ["ReadWriteMany"],
                "resources": {"requests": {"storage": size}},
            },
        },
    }


def _dv_name_for_vm(source_vm: dict) -> Optional[str]:
    """Find the DataVolume backing the source VM (created as <vm>-dv by DRIFT)."""
    vols = source_vm["spec"]["template"]["spec"].get("volumes", [])
    for v in vols:
        if "dataVolume" in v:
            return v["dataVolume"]["name"]
    return None


class CrossClusterMigration:
    """One cross-cluster migration run. Progress via the `log` callback."""

    def __init__(
        self,
        source_kubeconfig: str,
        target_kubeconfig: str,
        namespace: str,
        vm_name: str,
        log: Optional[Callable[[str, str], None]] = None,
        sync_port: int = 9185,
        enable_rollback: Optional[bool] = None,
        health_timeout: Optional[int] = None,
        force_unhealthy: Optional[bool] = None,
        manage_ca: bool = True,
    ):
        self.src = source_kubeconfig
        self.tgt = target_kubeconfig
        self.ns = namespace
        self.vm = vm_name
        self.sync_port = sync_port
        # O-6: roll back to the previous edge if the target comes up unhealthy.
        # On by default; a reverse (rollback) run sets this False so it can't
        # recurse. Overridable via DRIFT_XC_ROLLBACK / DRIFT_XC_HEALTH_TIMEOUT.
        self.enable_rollback = (
            os.getenv("DRIFT_XC_ROLLBACK", "1").lower() not in ("0", "false", "no")
            if enable_rollback is None else enable_rollback
        )
        self.health_timeout = (
            int(os.getenv("DRIFT_XC_HEALTH_TIMEOUT", "60"))
            if health_timeout is None else health_timeout
        )
        # Fault injection for the O-6 rollback demo (proposal M6: "demonstrate
        # unsuccessful migrations and the triggering of a rollback"). Forces the
        # post-migration health gate to fail on THIS run; never inherited by the
        # reverse (rollback) run, so rollback can still succeed.
        self.force_unhealthy = (
            os.getenv("DRIFT_XC_FORCE_UNHEALTHY", "").lower() in ("1", "true", "yes")
            if force_unhealthy is None else force_unhealthy
        )
        # Cross-cluster CA trust management. The forward run owns it; a reverse
        # (rollback) run sets manage_ca=False because the forward run's operator
        # freeze + union-CA is still in effect when the rollback runs.
        self.manage_ca = manage_ca
        self._ca_frozen = False
        self._used_warm = False
        self._operator_replicas: dict = {}
        self.migration_id = f"drift-xc-{uuid.uuid4().hex[:10]}"
        self._log = log or (lambda level, msg: logger.log(
            getattr(logging, level, logging.INFO), msg))

    def log(self, msg: str, level: str = "INFO"):
        self._log(level, f"[{self.migration_id}] {msg}")

    async def run(self) -> dict:
        try:
            return await self._run()
        except Exception as e:
            self.log(f"migration failed: {e}", "ERROR")
            # If we took the warm fast-path, the loaded trust may have gone
            # stale — drop the cache so the next migration does a cold establish.
            if self._used_warm:
                _CA_WARM.pop(frozenset((self.src, self.tgt)), None)
                self.log("CA trust: warm cache invalidated after failure", "WARNING")
            self._cleanup()
            return {"status": "failed", "migration_id": self.migration_id, "error": str(e)}
        finally:
            # Always un-freeze virt-operator, whatever happened.
            try:
                await self._restore_ca_reconcile()
            except Exception as e:
                self.log(f"virt-operator restore failed: {e}", "ERROR")

    def _clear_stale_vmims(self):
        """Remove leftover migration objects for this VMI on BOTH clusters. A
        completed VMIM from a previous run (e.g. a prior reverse migration)
        otherwise pollutes the sync controller's peer-address state and makes
        it dial a stale endpoint."""
        for kc in (self.src, self.tgt):
            for name in ("migration-source", "migration-target"):
                _k8s_delete(kc, _vmim_path(self.ns, name))

    async def _ensure_ca_trust(self):
        """Establish cross-cluster CA trust that SURVIVES the whole migration.

        The sync channel is mutual-TLS: each sync-controller verifies the peer
        against its kubevirt-ca. Two facts make this hard to hold:
          * virt-operator OWNS kubevirt-ca and reconciles it back to a single
            (own) cert within ~10s — injecting the peer CA via kubevirt-external-ca
            does not persist (the operator clears external-ca without a lasting
            merge), and a direct patch is undone within ~10s.
          * the sync-controller caches its trust at startup; it does not hot-load.
        So we (1) freeze virt-operator on both clusters, (2) write the UNION of
        both kubevirt-ca certs into each cluster's kubevirt-ca (each now trusts
        the peer), (3) restart the sync-controllers so they load the 2-cert
        trust while the operator is frozen. The operator is restored in run()'s
        finally, after the migration — a brief, bounded window with no CA
        reconciliation, which does not affect running VMs.
        """
        src_ca, tgt_ca = _kubevirt_ca_bundle(self.src), _kubevirt_ca_bundle(self.tgt)
        src_certs, tgt_certs = _pem_certs(src_ca), _pem_certs(tgt_ca)
        if not src_certs or not tgt_certs:
            raise RuntimeError("CA trust: kubevirt-ca unreadable on a cluster")
        if tgt_certs <= src_certs and src_certs <= tgt_certs:
            self.log("CA trust: both clusters already trust each other")
            return
        union = _union_pem(src_ca, tgt_ca)
        identity = frozenset(src_certs | tgt_certs)

        # Warm fast-path: if we established exactly this trust recently, the
        # sync-controllers still hold it in memory — skip the reload + settle
        # (the slow part). Peer CA rotation changes `identity` -> not warm. The
        # key is the cluster PAIR (direction-independent): an establish reloads
        # both sync-controllers, so either direction warms the pair.
        pair = frozenset((self.src, self.tgt))
        warm = _CA_WARM.get(pair)
        window = int(os.getenv("DRIFT_XC_WARM_WINDOW", "300"))
        self._used_warm = bool(warm and warm["certs"] == identity
                               and time.time() - warm["at"] < window)

        # 1. freeze virt-operator on both clusters (stop CA reconciliation).
        # Mark frozen up front so a partial freeze is still restored in finally.
        self._ca_frozen = True
        await asyncio.gather(self._freeze_operator(self.src, "source"),
                             self._freeze_operator(self.tgt, "target"))

        # 2. write the union CA into both kubevirt-ca configmaps (keeps trust
        # valid for the whole migration even if a sync-controller hot-reloads).
        for kc, label in ((self.src, "source"), (self.tgt, "target")):
            if _k8s_patch(kc, _cm_path("kubevirt-ca"),
                          {"data": {"ca-bundle": union}}, []) is None:
                raise RuntimeError(f"CA trust: could not write union CA on {label}")
        self.log("CA trust: union CA written to both clusters (operator frozen)")

        if self._used_warm:
            self.log("CA trust: sync-controllers still warm — skipping reload + settle")
            return

        # 3. cold path: restart sync-controllers so they load the 2-cert trust.
        # The deployment reports ready before the cert-manager has fetched certs
        # and the cross-cluster gRPC endpoint is fully live, so settle after.
        await asyncio.gather(self._reload_sync_controller(self.src, "source"),
                             self._reload_sync_controller(self.tgt, "target"))
        settle = int(os.getenv("DRIFT_XC_SYNC_SETTLE", "30"))
        self.log(f"CA trust: letting sync-controllers settle ({settle}s)")
        await asyncio.sleep(settle)
        _CA_WARM[pair] = {"certs": identity, "at": time.time()}

    async def _freeze_operator(self, kc: str, label: str):
        """Scale virt-operator to 0 on `kc` (recording the prior replica count)
        so it stops reconciling kubevirt-ca back to a single cert."""
        dep = "/apis/apps/v1/namespaces/kubevirt/deployments/virt-operator"
        d = _k8s_get(kc, dep) or {}
        self._operator_replicas[kc] = (d.get("spec") or {}).get("replicas", 2)
        if _k8s_patch(kc, dep, {"spec": {"replicas": 0}}, []) is None:
            raise RuntimeError(f"CA trust: could not freeze virt-operator on {label}")
        deadline = time.time() + 60
        while time.time() < deadline:
            if not ((_k8s_get(kc, dep) or {}).get("status", {}).get("replicas")):
                self.log(f"CA trust: virt-operator frozen on {label}")
                return
            await asyncio.sleep(2)
        raise RuntimeError(f"CA trust: virt-operator did not scale down on {label}")

    async def _restore_ca_reconcile(self):
        """Un-freeze virt-operator on both clusters (idempotent)."""
        if not self._ca_frozen:
            return
        dep = "/apis/apps/v1/namespaces/kubevirt/deployments/virt-operator"
        for kc in (self.src, self.tgt):
            _k8s_patch(kc, dep, {"spec": {"replicas": self._operator_replicas.get(kc, 2)}}, [])
        self._ca_frozen = False
        self.log("CA trust: virt-operator restored on both clusters")

    async def _reload_sync_controller(self, kc: str, label: str):
        """Restart virt-synchronization-controller on `kc` so it reloads the
        merged peer CA. Waits for the rollout to complete (new pod ready, old
        gone) so a subsequent sync-address read sees the current pod."""
        dep = ("/apis/apps/v1/namespaces/kubevirt/deployments/"
               "virt-synchronization-controller")
        patch = {"spec": {"template": {"metadata": {"annotations": {
            "drift.io/ca-reloaded-at": str(int(time.time()))}}}}}
        if _k8s_patch(kc, dep, patch, []) is None:
            raise RuntimeError(f"CA trust {label}: could not restart sync-controller")
        deadline = time.time() + 90
        await asyncio.sleep(2)
        while time.time() < deadline:
            d = _k8s_get(kc, dep) or {}
            st, spec = d.get("status", {}), d.get("spec", {})
            want = spec.get("replicas")
            if (st.get("observedGeneration") == d.get("metadata", {}).get("generation")
                    and st.get("updatedReplicas") == want
                    and st.get("readyReplicas") == want
                    and st.get("replicas") == want):
                self.log(f"CA trust {label}: sync-controller reloaded")
                return
            await asyncio.sleep(3)
        raise RuntimeError(f"CA trust {label}: sync-controller reload timed out")

    async def _reset_target(self, dv_name: Optional[str]):
        """Wipe any leftover receiver VM/VMI/DataVolume for this VM on the TARGET
        and wait for teardown, so we always start from a clean, blank receiver.
        Reusing a stale DataVolume from a previous run leaves the receiver VMI
        stuck (never reaches WaitingForSync)."""
        existed = (_k8s_get(self.tgt, _vm_path(self.ns, self.vm)) is not None
                   or (dv_name and _k8s_get(self.tgt, _dv_path(self.ns, dv_name)) is not None))
        if not existed:
            return
        self.log("clearing stale receiver VM/DV on target", "WARNING")
        _k8s_delete(self.tgt, _vm_path(self.ns, self.vm))
        _k8s_delete(self.tgt, _vmi_path(self.ns, self.vm))
        if dv_name:
            _k8s_delete(self.tgt, _dv_path(self.ns, dv_name))
        # Longhorn PVC teardown takes a moment; wait until both are actually gone.
        deadline = time.time() + 120
        while time.time() < deadline:
            if (_k8s_get(self.tgt, _vm_path(self.ns, self.vm)) is None
                    and (not dv_name or _k8s_get(self.tgt, _dv_path(self.ns, dv_name)) is None)):
                self.log("target clean")
                return
            await asyncio.sleep(3)
        raise RuntimeError("target still has a leftover receiver VM/DV after cleanup")

    async def _run(self) -> dict:
        self._clear_stale_vmims()

        # 0. ensure mutual cross-cluster CA trust is fresh (before any sync conn).
        # Skipped by a reverse (rollback) run — the forward run's freeze + union
        # CA is still in effect, so trust is already in place.
        if self.manage_ca:
            await self._ensure_ca_trust()

        # 1. source VM + its DataVolume
        source_vm = _k8s_get(self.src, _vm_path(self.ns, self.vm))
        if not source_vm:
            raise RuntimeError(f"source VM {self.ns}/{self.vm} not found")
        # A DataVolume (persistent RWX disk) gets a blank receiver DV on the
        # target. A containerDisk VM needs none: the target boots from its own
        # copy of the same image, and KubeVirt block-migrates the writable
        # overlay with the memory.
        dv_name = _dv_name_for_vm(source_vm)
        source_dv = None
        if dv_name:
            source_dv = _k8s_get(self.src, _dv_path(self.ns, dv_name))
            if not source_dv:
                raise RuntimeError(f"source DataVolume {dv_name} not found")
        vols = source_vm["spec"]["template"]["spec"].get("volumes", [])
        if not dv_name and not any("containerDisk" in v for v in vols):
            raise RuntimeError("source VM has neither a DataVolume nor a containerDisk")
        self.log(f"source VM ready; disk={dv_name or 'containerDisk'}")

        # 2. reset the target, then create a fresh blank DV (if any) + WaitAsReceiver VM
        await self._reset_target(dv_name)
        err: list = []
        if dv_name and not _k8s_post(self.tgt, _dv_path(self.ns, ""),
                                     _build_blank_dv(source_dv, self.ns, dv_name), err):
            raise RuntimeError(f"failed to create target DataVolume: {err}")
        if not _k8s_post(self.tgt, f"{_kv_ns(self.ns)}/virtualmachines",
                         _build_receiver_vm(source_vm), err):
            raise RuntimeError(f"failed to create receiver VM: {err}")
        self.log("receiver VM (WaitAsReceiver) + blank DV created on target (clean)")

        # 3. wait for the receiver VMI to be WaitingForSync
        await self._wait(
            lambda: (_k8s_get(self.tgt, _vmi_path(self.ns, self.vm)) or {})
            .get("status", {}).get("phase") == "WaitingForSync",
            "receiver VMI WaitingForSync", timeout=180,
        )

        # 4. receiver VMIM -> read its synchronization address
        migration = {
            "apiVersion": KV, "kind": "VirtualMachineInstanceMigration",
            "metadata": {"name": "migration-target", "namespace": self.ns},
            "spec": {"receive": {"migrationID": self.migration_id}, "vmiName": self.vm},
        }
        # a stale VMIM from a previous run would carry a stale address — replace it
        _k8s_delete(self.tgt, _vmim_path(self.ns, "migration-target"))
        if not _k8s_post(self.tgt, _vmim_coll(self.ns), migration, err):
            raise RuntimeError(f"failed to create receiver VMIM: {err}")

        sync_addr = await self._read_sync_address()
        self.log(f"target synchronization address: {sync_addr}")

        # 5. sender VMIM on the source pointed at the target sync address
        sender = {
            "apiVersion": KV, "kind": "VirtualMachineInstanceMigration",
            "metadata": {"name": "migration-source", "namespace": self.ns},
            "spec": {"sendTo": {"connectURL": sync_addr, "migrationID": self.migration_id},
                     "vmiName": self.vm},
        }
        _k8s_delete(self.src, _vmim_path(self.ns, "migration-source"))
        if not _k8s_post(self.src, _vmim_coll(self.ns), sender, err):
            raise RuntimeError(f"failed to create sender VMIM: {err}")
        self.log("sender VMIM created; migration in progress")

        # 6. poll both to Succeeded (memory transfer + cutover)
        result = await self._poll_completion(timeout=600)
        if result != "Succeeded":
            raise RuntimeError(f"migration did not succeed (state={result})")
        self.log("migration cutover complete — VM now running on target cluster")

        # 7. post-migration health gate on the target (O-6 / KPI-4)
        if await self._check_target_health(self.health_timeout):
            self.log("post-migration health check PASSED — VM healthy on target")
            self._fence_source()
            return {
                "status": "succeeded",
                "migration_id": self.migration_id,
                "vm": self.vm,
                "namespace": self.ns,
                "sync_address": sync_addr,
            }

        # 8. unhealthy on the target -> roll back to the previous edge
        self.log("post-migration health check FAILED on target", "ERROR")
        if not self.enable_rollback:
            return {
                "status": "unhealthy",
                "migration_id": self.migration_id,
                "vm": self.vm,
                "namespace": self.ns,
                "error": "target VMI not Ready after cutover; rollback disabled",
            }
        return await self._rollback()

    async def _read_sync_address(self) -> str:
        """Resolve the target's synchronization endpoint.

        Prefer the live virt-synchronization-controller pod IP over the VMIM's
        advertised address: KubeVirt's status.synchronizationAddresses can go
        stale after a sync-controller replica/leader change (it keeps pointing
        at a deleted pod), which makes the sender dial a dead endpoint. With
        KubeVirt infra.replicas=1 there is exactly one sync-controller pod, so
        its pod IP is the unambiguous, current endpoint.
        """
        pod_ip = self._sync_controller_pod_ip()
        if pod_ip:
            return f"{pod_ip}:{self.sync_port}"

        # Fallback: the VMIM-advertised address (multi-replica clusters).
        deadline = time.time() + 60
        while time.time() < deadline:
            vmim = _k8s_get(self.tgt, _vmim_path(self.ns, "migration-target"))
            addrs = (vmim or {}).get("status", {}).get("synchronizationAddresses") or []
            for a in addrs:
                if a.endswith(f":{self.sync_port}"):
                    return a
            await asyncio.sleep(3)
        raise RuntimeError("target never advertised a synchronization address")

    def _sync_controller_pod_ip(self) -> Optional[str]:
        pods = _k8s_get(
            self.tgt,
            "/api/v1/namespaces/kubevirt/pods"
            "?labelSelector=kubevirt.io%3Dvirt-synchronization-controller",
        )
        running = [
            p for p in (pods or {}).get("items", [])
            if p.get("status", {}).get("phase") == "Running"
            and p.get("status", {}).get("podIP")
        ]
        if len(running) == 1:
            return running[0]["status"]["podIP"]
        # Ambiguous (0 or >1 replicas): defer to the advertised address.
        return None

    async def _poll_completion(self, timeout: int) -> str:
        deadline = time.time() + timeout
        # A working sync leaves target=WaitingForSync within seconds of the
        # sender connecting; staying there means the sync connection never
        # established (e.g. cold/failed CA trust). Fail fast on that instead of
        # waiting out the full timeout.
        stall_limit = int(os.getenv("DRIFT_XC_SYNC_STALL", "120"))
        waiting_since = None
        last = ""
        while time.time() < deadline:
            s = (_k8s_get(self.src, _vmim_path(self.ns, "migration-source")) or {}) \
                .get("status", {}).get("phase", "")
            t = (_k8s_get(self.tgt, _vmim_path(self.ns, "migration-target")) or {}) \
                .get("status", {}).get("phase", "")
            state = f"source={s} target={t}"
            if state != last:
                self.log(state)
                last = state
            if s == "Failed" or t == "Failed":
                return f"Failed ({state})"
            if t == "WaitingForSync":
                waiting_since = waiting_since or time.time()
                if time.time() - waiting_since > stall_limit:
                    return f"SyncStalled ({state})"
            else:
                waiting_since = None
            # Completion must be robust to VMIM garbage-collection: after cutover
            # the finalized side's VMIM phase can blank out (the object is GC'd),
            # so requiring BOTH to read "Succeeded" in the same poll races against
            # cleanup and yields a false timeout. Treat "one side reached
            # Succeeded and neither side is still in-flight (Succeeded or gone)"
            # as done. `target=Succeeded` only occurs at completion, so pairing
            # it with an empty source is safe from early false positives.
            if (s == "Succeeded" or t == "Succeeded") \
                    and s in ("Succeeded", "") and t in ("Succeeded", ""):
                return "Succeeded"
            await asyncio.sleep(6)
        return f"Timeout ({last})"

    async def _wait(self, cond: Callable[[], bool], what: str, timeout: int):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                self.log(f"{what}: ready")
                return
            await asyncio.sleep(5)
        raise RuntimeError(f"timed out waiting for {what}")

    def _fence_source(self):
        """Single-writer fence: after a successful move, ensure the SOURCE VM is
        not in a running strategy, so the previous edge cannot dual-run the same
        MAC/IP on the stretched L2. KubeVirt's decentralized migration already
        halts the departed side; this asserts and enforces that invariant and
        clears any lingering source VMI.

        Handles BOTH ways a VM keeps itself running: the newer `spec.runStrategy`
        and the legacy `spec.running: true`. Fencing only runStrategy while the VM
        uses `running: true` is a no-op — the deleted source VMI then respawns and
        you get a split-brain (both edges live on the same IP). So halt whichever
        field is in play BEFORE deleting the VMI."""
        vm = _k8s_get(self.src, _vm_path(self.ns, self.vm)) or {}
        spec = vm.get("spec") or {}
        strat = spec.get("runStrategy")
        running = spec.get("running")
        patch = None
        if strat is not None and strat != "Halted":
            patch = {"spec": {"runStrategy": "Halted"}}
        elif running:                      # legacy `running: true` — must clear it too
            patch = {"spec": {"running": False}}
        if patch:
            self.log(f"fencing previous edge -> {patch['spec']}", "WARNING")
            _k8s_patch(self.src, _vm_path(self.ns, self.vm), patch, [])
        else:
            self.log(f"previous edge already fenced (runStrategy={strat}, running={running})")
        _k8s_delete(self.src, _vmi_path(self.ns, self.vm))

    async def _check_target_health(self, timeout: int) -> bool:
        """Post-migration health gate on the TARGET VMI (proposal O-6 / KPI-4).

        A Succeeded VMIM only proves the memory transfer + cutover completed; it
        does not prove the VM came up healthy on the new edge. We require the
        target VMI to be phase=Running with a Ready condition True within
        `timeout`. If the guest defines a readiness probe, Ready reflects it;
        otherwise it is basic VMI liveness. A VMI that migrated cleanly reaches
        this within seconds, so the happy path is non-disruptive; a target that
        never becomes Ready (or that finalized/stopped) trips the rollback."""
        if self.force_unhealthy:
            self.log("target health: FORCED unhealthy (fault injection)", "WARNING")
            return False
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            status = (_k8s_get(self.tgt, _vmi_path(self.ns, self.vm)) or {}).get("status", {})
            phase = status.get("phase", "")
            ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in status.get("conditions", [])
            )
            state = f"phase={phase} ready={ready}"
            if state != last:
                self.log(f"target health: {state}")
                last = state
            if phase == "Running" and ready:
                return True
            if phase in ("Failed", "Succeeded"):  # VMI stopped on the target
                return False
            await asyncio.sleep(4)
        return False

    async def _rollback(self) -> dict:
        """Restore service on the ORIGINAL edge after a failed post-migration
        health check — the proposal's "fallback to the previous edge" (O-6).

        Runs a reverse decentralized migration target->source, reusing this same
        machinery with the kubeconfigs swapped. The reverse run has rollback
        disabled so a failure cannot recurse; if it also fails, the unhealthy
        instance simply remains on the target and we report rollback_failed."""
        self.log("rolling back: reverse migration target -> previous edge", "WARNING")
        reverse = CrossClusterMigration(
            source_kubeconfig=self.tgt,
            target_kubeconfig=self.src,
            namespace=self.ns,
            vm_name=self.vm,
            log=self._log,
            sync_port=self.sync_port,
            enable_rollback=False,
            force_unhealthy=False,
            manage_ca=False,  # forward run's operator freeze + union CA still active
        )
        rb = await reverse.run()
        if rb.get("status") == "succeeded":
            self.log("ROLLBACK SUCCEEDED — service restored on the previous edge")
            return {
                "status": "rolled_back",
                "migration_id": self.migration_id,
                "rollback_id": reverse.migration_id,
                "vm": self.vm,
                "namespace": self.ns,
                "reason": "target VMI unhealthy after cutover; "
                          "service restored on the original edge",
            }
        self.log("ROLLBACK FAILED — unhealthy VM remains on target", "ERROR")
        return {
            "status": "rollback_failed",
            "migration_id": self.migration_id,
            "rollback_id": reverse.migration_id,
            "vm": self.vm,
            "namespace": self.ns,
            "error": rb.get("error", "reverse migration did not succeed"),
        }

    def _cleanup(self):
        """KubeVirt does not auto-retry failed decentralized migrations."""
        self.log("cleaning up migration artifacts", "WARNING")
        _k8s_delete(self.src, _vmim_path(self.ns, "migration-source"))
        _k8s_delete(self.tgt, _vmim_path(self.ns, "migration-target"))
        _k8s_delete(self.tgt, _vm_path(self.ns, self.vm))
