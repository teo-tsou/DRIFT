"""
Subscriptions router – event notifications (MEC 021 §6.2.7).
POST   /amsi/v1/subscriptions        – subscribe to mobility events
GET    /amsi/v1/subscriptions        – list subscriptions
DELETE /amsi/v1/subscriptions/{id}   – unsubscribe
"""

from fastapi import APIRouter, HTTPException, status
from typing import List

from app.models.mec021 import MobilityProcedureSubscription
from app.core.store import subscription_store

router = APIRouter()


@router.post(
    "/subscriptions",
    response_model=MobilityProcedureSubscription,
    status_code=status.HTTP_201_CREATED,
    summary="Subscribe to mobility procedure events (MEC 021 §6.2.7)",
)
async def create_subscription(body: MobilityProcedureSubscription) -> MobilityProcedureSubscription:
    subscription_store[body.subscription_id] = body
    return body


@router.get(
    "/subscriptions",
    response_model=List[MobilityProcedureSubscription],
    summary="List all active subscriptions",
)
async def list_subscriptions() -> List[MobilityProcedureSubscription]:
    return list(subscription_store.values())


@router.delete(
    "/subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unsubscribe from mobility events",
)
async def delete_subscription(subscription_id: str) -> None:
    if subscription_id not in subscription_store:
        raise HTTPException(status_code=404, detail="Subscription not found")
    del subscription_store[subscription_id]
