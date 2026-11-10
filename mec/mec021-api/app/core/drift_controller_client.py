"""
Client for the DRIFT Framework controller (drift-controller).

The NBI never touches Kubernetes or kubeconfigs — candidate-target queries
are answered by the controller's live, resource-aware edge selection.
"""

import httpx

from app.core.config import settings
from app.core.store import ams_store
from app.models.mec021 import AdjacentAppInfo, ResourceInfo


class DriftControllerError(Exception):
    pass


async def get_adjacent_app_info(app_mobility_service_id: str) -> list[AdjacentAppInfo]:
    ams = ams_store[app_mobility_service_id]
    ctx = ams.registered_app_context

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(
                f"{settings.drift_controller_url}/candidates",
                params={
                    "vm_name": ctx.vm_name,
                    "source_cluster": ctx.source_cluster,
                    "source_namespace": ctx.source_namespace,
                },
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise DriftControllerError(f"Failed to fetch candidate edges: {exc}") from exc

    data = r.json()
    return [
        AdjacentAppInfo(
            cluster_id=c["cluster_id"],
            node_name=c["node_name"],
            available_resources=ResourceInfo(
                cpu_available=f"{c['available_cpu_m']}m",
                memory_available=f"{c['available_mem_mi']}Mi",
                greenness_score=c["greenness"],
            ),
        )
        for c in data.get("candidates", [])
        if c.get("feasible")
    ]
