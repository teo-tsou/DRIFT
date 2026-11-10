"""
Migration dispatch — delegates execution to the DRIFT backend's existing
KubeVirt-native live migration endpoint and polls completion via the
migration_state the backend now attaches to each VM in /all-vms.
"""

import asyncio
import time

from app.core.drift_backend_client import (
    get_vm,
    trigger_intra_cluster_migration,
    trigger_cross_cluster_migration,
    get_cross_cluster_migration,
    trigger_intra_cluster_pod_migration,
    trigger_cross_cluster_pod_migration,
    get_pod_migration,
)


async def start_intra_cluster_migration(
    cluster_id: str, namespace: str, vm_name: str, target_node: str
) -> dict:
    return await trigger_intra_cluster_migration(cluster_id, namespace, vm_name, target_node)


async def run_cross_cluster_migration(
    source_cluster: str,
    target_cluster: str,
    namespace: str,
    vm_name: str,
    timeout_s: int = 600,
    poll_interval_s: int = 4,
) -> dict:
    """Trigger a decentralized cross-cluster migration on the backend and poll
    the backend's /cross-migrations/{id} until it reaches a terminal state."""
    migration_id = await trigger_cross_cluster_migration(
        source_cluster, target_cluster, namespace, vm_name
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = await get_cross_cluster_migration(migration_id)
        if state["status"] == "succeeded":
            return state
        if state["status"] == "failed":
            raise RuntimeError(
                (state.get("result") or {}).get("error", "cross-cluster migration failed")
            )
        await asyncio.sleep(poll_interval_s)
    raise TimeoutError(
        f"Cross-cluster migration {migration_id} did not complete within {timeout_s}s"
    )


async def wait_for_migration(
    cluster_id: str,
    namespace: str,
    vm_name: str,
    target_node: str,
    timeout_s: int = 180,
    poll_interval_s: int = 3,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        vm = await get_vm(cluster_id, namespace, vm_name)
        if vm:
            mig = vm.get("migration_state")
            if mig and mig.get("failed"):
                raise RuntimeError(f"Live migration of '{vm_name}' failed")
            if (
                mig
                and mig.get("completed")
                and vm.get("phase") == "Running"
                and vm.get("node") == target_node
            ):
                return
        await asyncio.sleep(poll_interval_s)
    raise TimeoutError(f"Migration of '{vm_name}' to '{target_node}' did not complete within {timeout_s}s")


# ── Pods (the container twin) ─────────────────────────────────────────────────
# Pod migrations run as a backend background job (migration_id); both intra- and
# cross-cluster complete by polling /pod-migrations/{id} to a terminal state.

async def _wait_pod_migration(migration_id: str, timeout_s: int, poll_interval_s: int) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        st = await get_pod_migration(migration_id)
        if st.get("completed"):
            return st
        if st.get("failed"):
            # The backend error is the log TAIL and the die() reason is at its
            # end, so keep the tail — [:400] would show the opening lines and
            # cut off the actual failure message.
            raise RuntimeError((st.get("error") or "pod migration failed")[-1500:])
        await asyncio.sleep(poll_interval_s)
    raise TimeoutError(f"Pod migration {migration_id} did not complete within {timeout_s}s")


async def run_intra_cluster_pod_migration(
    cluster_id: str, namespace: str, pod_name: str, target_node: str,
    timeout_s: int = 240, poll_interval_s: int = 3,
) -> dict:
    mig_id = await trigger_intra_cluster_pod_migration(cluster_id, namespace, pod_name, target_node)
    return await _wait_pod_migration(mig_id, timeout_s, poll_interval_s)


async def run_cross_cluster_pod_migration(
    source_cluster: str, target_cluster: str, namespace: str, pod_name: str,
    timeout_s: int = 600, poll_interval_s: int = 1,
) -> dict:
    mig_id = await trigger_cross_cluster_pod_migration(source_cluster, target_cluster, namespace, pod_name)
    return await _wait_pod_migration(mig_id, timeout_s, poll_interval_s)
