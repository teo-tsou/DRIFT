"""
Event notifier – sends MEC 021 mobility event notifications to subscriber callback URLs.
"""

import httpx
from datetime import datetime, timezone

from app.models.mec021 import MobilityStatus, MobilityProcedureNotification
from app.core.store import subscription_store
from app.core.config import settings


async def notify_subscribers(
    app_mobility_service_id: str,
    mobility_procedure_id: str,
    mobility_status: MobilityStatus,
) -> None:
    """Fire-and-forget HTTP POST to all matching subscriber callback references."""
    payload = MobilityProcedureNotification(
        subscription_id="",
        app_mobility_service_id=app_mobility_service_id,
        mobility_procedure_id=mobility_procedure_id,
        mobility_status=mobility_status,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    async with httpx.AsyncClient(timeout=settings.notification_callback_timeout) as client:
        for sub in subscription_store.values():
            if sub.app_mobility_service_id and sub.app_mobility_service_id != app_mobility_service_id:
                continue
            if sub.filter_criteria and mobility_status not in sub.filter_criteria:
                continue
            payload.subscription_id = sub.subscription_id
            try:
                await client.post(sub.callback_reference, json=payload.model_dump())
            except Exception:
                pass  # Best-effort delivery
