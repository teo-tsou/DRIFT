"""
DRIFT Framework controller client – the MEC 021 API delegates the actual
edge-selection + migration work here.
"""

from typing import Optional

import httpx
from app.core.config import settings


async def trigger_migration(
    vm_name: str,
    source_cluster: str,
    source_namespace: str,
    procedure_id: str,
    preferred_target_cluster: Optional[str] = None,
    resource_kind: str = "vm",
    on_progress=None,
) -> dict:
    """POST to the DRIFT Framework controller and return the resulting status dict."""
    payload = {
        "vm_name": vm_name,
        "source_cluster": source_cluster,
        "source_namespace": source_namespace,
        "procedure_id": procedure_id,
        "resource_kind": resource_kind,
        "preferred_target_cluster": preferred_target_cluster,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{settings.drift_controller_url}/migrate", json=payload)
        resp.raise_for_status()
        accepted = resp.json()

    # /migrate returns 202 immediately (PREPARING); poll until terminal state.
    return await _poll_until_terminal(procedure_id, on_progress=on_progress)


async def _poll_until_terminal(procedure_id: str, timeout_s: int = 300, interval_s: float = 1.0,
                               on_progress=None) -> dict:
    import asyncio
    import time

    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=10.0) as client:
        while time.monotonic() < deadline:
            resp = await client.get(f"{settings.drift_controller_url}/migrate/{procedure_id}")
            resp.raise_for_status()
            status = resp.json()
            if status["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
                return status
            if on_progress:
                on_progress(status)       # SELECTING / TRANSFERRING / ... as it happens
            await asyncio.sleep(interval_s)
    raise TimeoutError(f"Migration {procedure_id} did not reach a terminal state within {timeout_s}s")


async def cancel_migration(procedure_id: str) -> None:
    """DELETE to cancel an in-progress migration."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(f"{settings.drift_controller_url}/migrate/{procedure_id}")
        resp.raise_for_status()
