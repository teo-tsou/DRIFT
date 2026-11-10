"""
HTTP client for the DRIFT backend (SBI infrastructure layer).

The DRIFT Framework (this service) never opens a kubeconfig or talks to
Kubernetes directly — it reuses the backend's existing, already
resource-aware REST surface:
  - GET  /api/v1/sites
  - GET  /api/v1/edge-clusters?status=active
  - GET  /api/v1/edge-clusters/{id}/edge-nodes
  - GET  /api/v1/edge-clusters/{id}/all-vms
  - POST /api/v1/edge-clusters/{id}/all-vms/{ns}/{vm}/migrate
"""

from typing import Optional

import httpx

from app.core.config import settings


class DriftBackendError(Exception):
    """Raised when the DRIFT backend is unreachable or returns an error."""


def _base_url() -> str:
    return f"{settings.drift_backend_url}{settings.drift_backend_api_prefix}"


async def get_ue_ip(imsi: str) -> Optional[str]:
    """Current 5G (wwan) address of the UE with this IMSI, probed live by the
    backend; the address changes across registrations, so it is never cached."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(f"{_base_url()}/ues")
            r.raise_for_status()
            ues = r.json()
            ues = ues.get("ues", ues) if isinstance(ues, dict) else ues
            ue = next((u for u in ues if u.get("imsi") == imsi), None)
            if not ue:
                return None
            r = await client.get(f"{_base_url()}/ues/{ue['id']}/status")
            r.raise_for_status()
            return ((r.json() or {}).get("wwan") or {}).get("ip") or None
    except Exception:
        return None


async def list_sites() -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(f"{_base_url()}/sites")
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise DriftBackendError(f"Failed to list sites: {exc}") from exc
    return r.json().get("sites", [])


async def list_active_clusters() -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(f"{_base_url()}/edge-clusters", params={"status": "active"})
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise DriftBackendError(f"Failed to list edge clusters: {exc}") from exc
    return r.json().get("clusters", [])


async def get_cluster_nodes(cluster_id: str, exclude: Optional[str] = None) -> list[dict]:
    params = {"exclude": exclude} if exclude else {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(
                f"{_base_url()}/edge-clusters/{cluster_id}/edge-nodes", params=params
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise DriftBackendError(
                f"Failed to list nodes for cluster {cluster_id}: {exc}"
            ) from exc
    return r.json().get("nodes", [])


async def get_vm(cluster_id: str, namespace: str, vm_name: str) -> Optional[dict]:
    """Returns the VM dict from all-vms (includes phase/node/cpu/memory/migration_state)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(f"{_base_url()}/edge-clusters/{cluster_id}/all-vms")
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise DriftBackendError(f"Failed to fetch VMs for cluster {cluster_id}: {exc}") from exc
    for vm in r.json().get("namespaces", {}).get(namespace, []):
        if vm.get("name") == vm_name:
            return vm
    return None


async def trigger_intra_cluster_migration(
    cluster_id: str, namespace: str, vm_name: str, target_node: str
) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                f"{_base_url()}/edge-clusters/{cluster_id}/all-vms/{namespace}/{vm_name}/migrate",
                json={"target_node": target_node},
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json().get("detail", exc.response.text) if exc.response.text else str(exc)
            raise DriftBackendError(f"Migration trigger rejected: {detail}") from exc
        except httpx.HTTPError as exc:
            raise DriftBackendError(f"Failed to trigger migration: {exc}") from exc
    return r.json()


async def trigger_cross_cluster_migration(
    source_cluster: str, target_cluster: str, namespace: str, vm_name: str
) -> str:
    """Kick off a decentralized cross-cluster migration; returns the migration_id."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                f"{_base_url()}/edge-clusters/{source_cluster}/all-vms/{namespace}/{vm_name}/cross-migrate",
                json={"target_cluster_id": target_cluster},
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json().get("detail", exc.response.text) if exc.response.text else str(exc)
            raise DriftBackendError(f"Cross-cluster migration rejected: {detail}") from exc
        except httpx.HTTPError as exc:
            raise DriftBackendError(f"Failed to trigger cross-cluster migration: {exc}") from exc
    return r.json()["migration_id"]


async def get_cross_cluster_migration(migration_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(f"{_base_url()}/cross-migrations/{migration_id}")
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise DriftBackendError(f"Failed to poll cross-cluster migration: {exc}") from exc
    return r.json()


# ── Pods (the container twin of the VM calls) ─────────────────────────────────

async def get_pod(cluster_id: str, namespace: str, pod_name: str) -> Optional[dict]:
    """Returns the pod dict from all-pods (node, cpu_m, mem_mi, migratable, …)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(
                f"{_base_url()}/edge-clusters/{cluster_id}/all-pods",
                params={"name": pod_name, "namespace": namespace},  # single-pod fast path
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise DriftBackendError(f"Failed to fetch pods for cluster {cluster_id}: {exc}") from exc
    for pod in r.json().get("namespaces", {}).get(namespace, []):
        if pod.get("name") == pod_name:
            return pod
    return None


async def trigger_intra_cluster_pod_migration(
    cluster_id: str, namespace: str, pod_name: str, target_node: str
) -> str:
    """In-cluster pod migration (CRIU post-copy + OVN multi-chassis); returns migration_id."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                f"{_base_url()}/edge-clusters/{cluster_id}/all-pods/{namespace}/{pod_name}/migrate",
                json={"target_node": target_node},
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json().get("detail", exc.response.text) if exc.response.text else str(exc)
            raise DriftBackendError(f"Pod migration rejected: {detail}") from exc
        except httpx.HTTPError as exc:
            raise DriftBackendError(f"Failed to trigger pod migration: {exc}") from exc
    return r.json()["migration_id"]


async def trigger_cross_cluster_pod_migration(
    source_cluster: str, target_cluster: str, namespace: str, pod_name: str
) -> str:
    """Cross-cluster pod migration (CRIU post-copy + EVPN-stretched-L2); returns migration_id."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                f"{_base_url()}/edge-clusters/{source_cluster}/all-pods/{namespace}/{pod_name}/cross-migrate",
                json={"target_cluster_id": target_cluster},
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json().get("detail", exc.response.text) if exc.response.text else str(exc)
            raise DriftBackendError(f"Cross-cluster pod migration rejected: {detail}") from exc
        except httpx.HTTPError as exc:
            raise DriftBackendError(f"Failed to trigger cross-cluster pod migration: {exc}") from exc
    return r.json()["migration_id"]


async def get_pod_migration(migration_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(f"{_base_url()}/pod-migrations/{migration_id}")
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise DriftBackendError(f"Failed to poll pod migration: {exc}") from exc
    return r.json()
