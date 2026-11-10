"""
Mobility Procedures router.
POST   /app_mobility_services/{id}/mobility_procedures          – initiate relocation
GET    /app_mobility_services/{id}/mobility_procedures/{mpId}   – get procedure status
PUT    /app_mobility_services/{id}/mobility_procedures/{mpId}   – trigger context transfer
POST   /app_mobility_services/{id}/mobility_procedures/{mpId}/activate – activate relocation
DELETE /app_mobility_services/{id}/mobility_procedures/{mpId}   – cancel
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, status

from app.models.mec021 import (
    MobilityProcedureInfo,
    MobilityProcedureInitiateRequest,
    MobilityProcedureState,
    ContextTransferState,
    MobilityStatus,
)
from app.core.store import ams_store, procedure_store
from app.core.drift_client import trigger_migration, cancel_migration
from app.core.notifier import notify_subscribers

router = APIRouter()


@router.post(
    "/app_mobility_services/{app_mobility_service_id}/mobility_procedures",
    response_model=MobilityProcedureInfo,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Initiate a service relocation (MEC 021 §6.2.4)",
)
async def initiate_relocation(
    app_mobility_service_id: str,
    body: MobilityProcedureInitiateRequest,
    background_tasks: BackgroundTasks,
) -> MobilityProcedureInfo:
    _assert_ams_exists(app_mobility_service_id)
    proc = MobilityProcedureInfo(
        app_mobility_service_id=app_mobility_service_id,
        state=MobilityProcedureState.INITIALIZING,
        requested_target_cluster=body.target_cluster,
    )
    procedure_store[proc.mobility_procedure_id] = proc
    background_tasks.add_task(
        _run_migration,
        proc.mobility_procedure_id,
        app_mobility_service_id,
    )
    return proc


@router.get(
    "/app_mobility_services/{app_mobility_service_id}/mobility_procedures/{mobility_procedure_id}",
    response_model=MobilityProcedureInfo,
    summary="Get mobility procedure status",
)
async def get_procedure(
    app_mobility_service_id: str,
    mobility_procedure_id: str,
) -> MobilityProcedureInfo:
    _assert_ams_exists(app_mobility_service_id)
    return _get_procedure_or_404(mobility_procedure_id)


@router.put(
    "/app_mobility_services/{app_mobility_service_id}/mobility_procedures/{mobility_procedure_id}",
    response_model=MobilityProcedureInfo,
    summary="Trigger context transfer to target (MEC 021 §6.2.4)",
)
async def trigger_context_transfer(
    app_mobility_service_id: str,
    mobility_procedure_id: str,
) -> MobilityProcedureInfo:
    proc = _get_procedure_or_404(mobility_procedure_id)
    if proc.state != MobilityProcedureState.PREPARING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Procedure must be in PREPARING state, current: {proc.state}",
        )
    proc.context_transfer_state = ContextTransferState.IN_PROGRESS
    proc.state = MobilityProcedureState.TRANSFERRING
    return proc


@router.post(
    "/app_mobility_services/{app_mobility_service_id}/mobility_procedures/{mobility_procedure_id}/activate",
    response_model=MobilityProcedureInfo,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Activate relocation – resume service on target (MEC 021 §6.2.4)",
)
async def activate_relocation(
    app_mobility_service_id: str,
    mobility_procedure_id: str,
) -> MobilityProcedureInfo:
    proc = _get_procedure_or_404(mobility_procedure_id)
    if proc.state not in (MobilityProcedureState.TRANSFERRING, MobilityProcedureState.PREPARING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot activate from state: {proc.state}",
        )
    proc.state = MobilityProcedureState.ACTIVATING
    return proc


@router.delete(
    "/app_mobility_services/{app_mobility_service_id}/mobility_procedures/{mobility_procedure_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel an ongoing migration (MEC 021 §6.2.4)",
)
async def cancel_procedure(
    app_mobility_service_id: str,
    mobility_procedure_id: str,
) -> None:
    proc = _get_procedure_or_404(mobility_procedure_id)
    if proc.state in (MobilityProcedureState.COMPLETED, MobilityProcedureState.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Procedure already completed or cancelled",
        )
    await cancel_migration(proc.mobility_procedure_id)
    proc.state = MobilityProcedureState.CANCELLED
    await notify_subscribers(
        app_mobility_service_id,
        mobility_procedure_id,
        MobilityStatus.INTERHOST_MOVEOUT_FAILED,
    )


# ---------------------------------------------------------------------------
# Background migration task (full DRIFT migration loop)
# ---------------------------------------------------------------------------

async def _run_migration(mobility_procedure_id: str, app_mobility_service_id: str) -> None:
    proc = procedure_store[mobility_procedure_id]
    ams = ams_store[app_mobility_service_id]
    try:
        proc.state = MobilityProcedureState.PREPARING

        result = await trigger_migration(
            vm_name=ams.registered_app_context.vm_name,
            source_cluster=ams.registered_app_context.source_cluster,
            source_namespace=ams.registered_app_context.source_namespace,
            procedure_id=mobility_procedure_id,
            preferred_target_cluster=proc.requested_target_cluster,
            resource_kind=getattr(ams.registered_app_context, "resource_kind", "vm"),
            on_progress=lambda st: _progress(proc, st),
        )

        proc.decision = result.get("decision")
        proc.target_cluster = result.get("target_cluster")
        proc.target_node = result.get("target_node")
        proc.decision_reason = result.get("reason")
        proc.migration_latency_ms = result.get("migration_latency_ms")

        if result["state"] == "COMPLETED":
            proc.state = MobilityProcedureState.COMPLETED
            proc.context_transfer_state = ContextTransferState.COMPLETED
            # Keep the AMS registry pointing at the workload's CURRENT cluster,
            # so the next (possibly autonomous) procedure starts from reality.
            if proc.decision in ("cross_cluster",) and proc.target_cluster:
                ams.registered_app_context.source_cluster = proc.target_cluster
            await notify_subscribers(
                app_mobility_service_id,
                mobility_procedure_id,
                MobilityStatus.INTERHOST_MOVEOUT_COMPLETED,
            )
        else:
            proc.state = MobilityProcedureState.FAILED
            proc.error_details = result.get("error") or result.get("reason")
            await notify_subscribers(
                app_mobility_service_id,
                mobility_procedure_id,
                MobilityStatus.INTERHOST_MOVEOUT_FAILED,
            )
    except Exception as exc:
        proc.state = MobilityProcedureState.FAILED
        proc.error_details = str(exc)
        await notify_subscribers(
            app_mobility_service_id,
            mobility_procedure_id,
            MobilityStatus.INTERHOST_MOVEOUT_FAILED,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _progress(proc: MobilityProcedureInfo, status: dict) -> None:
    """Mirror the controller's phase while the relocation runs, so a client
    polling the procedure sees SELECTING -> TRANSFERRING, not PREPARING throughout."""
    try:
        proc.state = MobilityProcedureState(status.get("state"))
    except ValueError:
        return
    proc.decision = status.get("decision") or proc.decision
    proc.target_cluster = status.get("target_cluster") or proc.target_cluster
    proc.decision_reason = status.get("reason") or proc.decision_reason
    if proc.state == MobilityProcedureState.TRANSFERRING:
        proc.context_transfer_state = ContextTransferState.IN_PROGRESS


def _assert_ams_exists(app_mobility_service_id: str) -> None:
    if app_mobility_service_id not in ams_store:
        raise HTTPException(status_code=404, detail="AMS not found")


def _get_procedure_or_404(mobility_procedure_id: str) -> MobilityProcedureInfo:
    if mobility_procedure_id not in procedure_store:
        raise HTTPException(status_code=404, detail="Mobility procedure not found")
    return procedure_store[mobility_procedure_id]
