"""
AMS Registration router.
POST   /amsi/v1/app_mobility_services          – register AMS
GET    /amsi/v1/app_mobility_services          – list registered AMS
GET    /amsi/v1/app_mobility_services/{id}     – get AMS info
DELETE /amsi/v1/app_mobility_services/{id}     – deregister AMS
GET    /amsi/v1/app_mobility_services/{id}/adjacent_app_info – query candidate targets
"""

from fastapi import APIRouter, HTTPException, status
from typing import List

from app.models.mec021 import (
    AppMobilityServiceInfo,
    AppMobilityServiceRegistration,
    AdjacentAppInfo,
)
from app.core.store import ams_store
from app.core.drift_controller_client import get_adjacent_app_info, DriftControllerError

router = APIRouter()


@router.post(
    "/app_mobility_services",
    response_model=AppMobilityServiceInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Register an Application Mobility Service (MEC 021 §6.2.2)",
)
async def register_ams(body: AppMobilityServiceRegistration) -> AppMobilityServiceInfo:
    info = AppMobilityServiceInfo(
        registered_app_context=body.registered_app_context,
        service_continuity_mode=body.service_continuity_mode,
        expiry_time=body.expiry_time,
    )
    ams_store[info.app_mobility_service_id] = info
    # O-2: monitor the UE only while an app is registered on its behalf —
    # subscribe to pQoS degradation notifications for the lifetime of this AMS.
    from app.core.pqos_subs import subscribe_for_ams
    await subscribe_for_ams(info.app_mobility_service_id)
    return info


@router.get(
    "/app_mobility_services",
    response_model=List[AppMobilityServiceInfo],
    summary="List all registered Application Mobility Services",
)
async def list_ams() -> List[AppMobilityServiceInfo]:
    return list(ams_store.values())


@router.get(
    "/app_mobility_services/{app_mobility_service_id}",
    response_model=AppMobilityServiceInfo,
    summary="Get a specific Application Mobility Service",
)
async def get_ams(app_mobility_service_id: str) -> AppMobilityServiceInfo:
    _get_or_404(app_mobility_service_id)
    return ams_store[app_mobility_service_id]


@router.delete(
    "/app_mobility_services/{app_mobility_service_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deregister (delete) an Application Mobility Service",
)
async def delete_ams(app_mobility_service_id: str) -> None:
    _get_or_404(app_mobility_service_id)
    del ams_store[app_mobility_service_id]
    # O-2: stop monitoring — deregistration ends the app's mobility session.
    from app.core.pqos_subs import unsubscribe_for_ams
    await unsubscribe_for_ams(app_mobility_service_id)


@router.get(
    "/app_mobility_services/{app_mobility_service_id}/adjacent_app_info",
    response_model=List[AdjacentAppInfo],
    summary="Query candidate target edges for migration (MEC 021 §6.2.6)",
)
async def get_adjacent_app_info_endpoint(app_mobility_service_id: str) -> List[AdjacentAppInfo]:
    """Returns feasible target clusters/nodes with real resource + greenness scores,
    as computed live by the DRIFT Framework's Optimal Edge Selection."""
    _get_or_404(app_mobility_service_id)
    try:
        return await get_adjacent_app_info(app_mobility_service_id)
    except DriftControllerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get_or_404(app_mobility_service_id: str) -> AppMobilityServiceInfo:
    if app_mobility_service_id not in ams_store:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AMS {app_mobility_service_id} not found",
        )
    return ams_store[app_mobility_service_id]
