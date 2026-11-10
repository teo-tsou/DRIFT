"""
Migration router — the DRIFT Framework's entry point, called by the MEC 021
API (NBI) to relocate a VM.

POST   /migrate          – run edge selection and (if warranted) migrate
GET    /migrate/{id}     – get migration status
DELETE /migrate/{id}     – cancel a migration that hasn't started executing yet
"""

import time
from typing import Dict, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings
from app.modules.edge_selector import select_optimal_edge
from app.modules.health_check import assert_vm_running
from app.modules.migration_client import (
    start_intra_cluster_migration,
    wait_for_migration,
    run_cross_cluster_migration,
    run_intra_cluster_pod_migration,
    run_cross_cluster_pod_migration,
)

router = APIRouter()

_migrations: Dict[str, dict] = {}
_cooldowns: Dict[str, float] = {}  # vm_name -> last migration timestamp


class MigrationRequest(BaseModel):
    vm_name: str          # the workload name (VM or pod)
    source_cluster: str
    source_namespace: str = "default"
    procedure_id: str
    # "vm" (KubeVirt live-migration) or "pod" (CRIU post-copy + OVN/EVPN). The
    # edge-selection algorithm is identical; only execution differs.
    resource_kind: str = "vm"
    # Optional hint from the caller (ETSI MEC 021 target_cluster). If omitted,
    # DRIFT's Optimal Edge Selection picks the target on its own.
    preferred_target_cluster: Optional[str] = None


class MigrationStatus(BaseModel):
    procedure_id: str
    vm_name: str
    state: str
    decision: Optional[str] = None
    target_cluster: Optional[str] = None
    target_node: Optional[str] = None
    migration_latency_ms: Optional[float] = None
    reason: Optional[str] = None
    error: Optional[str] = None


@router.post("/migrate", response_model=MigrationStatus, status_code=status.HTTP_202_ACCEPTED)
async def start_migration(req: MigrationRequest, background_tasks: BackgroundTasks) -> MigrationStatus:
    # The anti-flap cooldown guards AUTONOMOUS relocations from oscillating
    # between edges. An explicitly REQUESTED target is a deliberate operator
    # action, so it bypasses the cooldown (consistent with honoring a requested
    # target over an optimal 'stay') — otherwise a forward hop would block the
    # reverse for the whole window.
    if not req.preferred_target_cluster:
        last = _cooldowns.get(req.vm_name, 0)
        if time.time() - last < settings.migration_cooldown_s:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Migration cooldown active for {req.vm_name}. Wait {settings.migration_cooldown_s}s.",
            )

    _migrations[req.procedure_id] = {
        "procedure_id": req.procedure_id,
        "vm_name": req.vm_name,
        "source_cluster": req.source_cluster,
        "source_namespace": req.source_namespace,
        "state": "PREPARING",
        "decision": None,
        "target_cluster": None,
        "target_node": None,
        "migration_latency_ms": None,
        "reason": None,
        "error": None,
    }

    background_tasks.add_task(_run_migration, req)
    return MigrationStatus(**_migrations[req.procedure_id])


@router.get("/migrate/{procedure_id}", response_model=MigrationStatus)
async def get_migration(procedure_id: str) -> MigrationStatus:
    if procedure_id not in _migrations:
        raise HTTPException(status_code=404, detail="Migration not found")
    return MigrationStatus(**_migrations[procedure_id])


@router.delete("/migrate/{procedure_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_migration(procedure_id: str) -> None:
    if procedure_id not in _migrations:
        raise HTTPException(status_code=404, detail="Migration not found")
    state = _migrations[procedure_id]["state"]
    if state not in ("PREPARING", "SELECTING"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot cancel migration in state '{state}' — the DRIFT backend has no "
                "in-flight cancellation hook for a live migration already handed off to KubeVirt."
            ),
        )
    _migrations[procedure_id]["state"] = "CANCELLED"


async def _run_migration(req: MigrationRequest) -> None:
    state = _migrations[req.procedure_id]
    try:
        state["state"] = "PREPARING"
        if req.resource_kind == "vm":
            await assert_vm_running(req.source_cluster, req.source_namespace, req.vm_name)

        state["state"] = "SELECTING"
        selection = await select_optimal_edge(
            vm_name=req.vm_name,
            source_cluster=req.source_cluster,
            source_namespace=req.source_namespace,
            preferred_cluster=req.preferred_target_cluster,
            kind=req.resource_kind,
        )
        state["decision"] = selection.decision
        state["target_cluster"] = selection.target_cluster_id
        state["target_node"] = selection.target_node
        state["reason"] = selection.reason

        if selection.decision == "stay":
            state["state"] = "COMPLETED"
            state["migration_latency_ms"] = 0.0
            return

        if selection.decision == "no_candidates":
            state["state"] = "FAILED"
            state["error"] = selection.reason
            return

        is_pod = req.resource_kind == "pod"

        if selection.decision == "cross_cluster":
            # Optimal edge is a different cluster — decentralized cross-cluster
            # live migration, executed by the DRIFT backend (SBI).
            t_start = time.monotonic()
            state["state"] = "TRANSFERRING"
            if is_pod:
                await run_cross_cluster_pod_migration(
                    source_cluster=req.source_cluster,
                    target_cluster=selection.target_cluster_id,
                    namespace=req.source_namespace,
                    pod_name=req.vm_name,
                )
            else:
                await run_cross_cluster_migration(
                    source_cluster=req.source_cluster,
                    target_cluster=selection.target_cluster_id,
                    namespace=req.source_namespace,
                    vm_name=req.vm_name,
                )
            elapsed_ms = (time.monotonic() - t_start) * 1000
            state["state"] = "COMPLETED"
            state["migration_latency_ms"] = round(elapsed_ms, 1)
            _cooldowns[req.vm_name] = time.time()
            return

        # decision == "intra_cluster"
        t_start = time.monotonic()
        state["state"] = "TRANSFERRING"
        if is_pod:
            # the pod migrate endpoint is a background job; run+wait polls it.
            await run_intra_cluster_pod_migration(
                req.source_cluster, req.source_namespace, req.vm_name, selection.target_node
            )
        else:
            await start_intra_cluster_migration(
                req.source_cluster, req.source_namespace, req.vm_name, selection.target_node
            )
            state["state"] = "ACTIVATING"
            await wait_for_migration(
                req.source_cluster, req.source_namespace, req.vm_name, selection.target_node
            )

        elapsed_ms = (time.monotonic() - t_start) * 1000
        state["state"] = "COMPLETED"
        state["migration_latency_ms"] = round(elapsed_ms, 1)
        _cooldowns[req.vm_name] = time.time()

    except Exception as exc:
        state["state"] = "FAILED"
        state["error"] = str(exc)
