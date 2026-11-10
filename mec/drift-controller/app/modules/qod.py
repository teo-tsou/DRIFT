"""
Quality on Demand (QoD) — request a dedicated QoS flow for the UE.

An access-link (RAN) degradation is a NETWORK problem, not an edge-placement
problem: both edges sit behind the same radio link, so migrating the workload
between edges cannot fix it. QoD is the right tool when it actually protects the
flow ("QoD extends edge operation" in the DRIFT use case). Migration is only a
QoS remedy when a CLOSER edge shortens the UE<->edge path (mobility).

Talks to the CAMARA QoD API v0.10+ (`/quality-on-demand/v1/sessions`), which
identifies the device by its IP address, not by IMSI. The UE's 5G address
changes across registrations, so it is resolved live from the DRIFT backend.

Measured on the ENVELOPE testbed (October 2026): every profile
enforces its MAXIMUM rate as a hard policer, and under uplink contention QOS_L
lifted the UE from its 11.7 Mbps share to 18 Mbps (the guaranteed minimum held).
So the profile must have a maximum ABOVE the UE's contended rate, otherwise the
session only throttles it: QOS_E (max 5) would cut a healthy UE from ~110 to 5
Mbps. Hence the QOS_L default, never QOS_E.
"""

import logging
from typing import Optional

import httpx

from app.core.config import settings
from app.core.drift_backend_client import get_ue_ip

log = logging.getLogger(__name__)

_SESSIONS = "/quality-on-demand/v1/sessions"


def _device(ip: str) -> dict:
    return {"ipv4Address": {"publicAddress": ip, "privateAddress": ip}}


async def _active_session(client: httpx.AsyncClient, ip: str) -> Optional[dict]:
    """An active session for this device, so repeated degradations reuse it
    instead of stacking sessions on the same flow."""
    try:
        r = await client.post(f"{settings.camara_qod_url}/quality-on-demand/v1/retrieve_sessions",
                              json={"device": _device(ip)})
        if r.status_code != 200:
            return None
        for s in r.json() or []:
            if s.get("qosStatus") not in ("UNAVAILABLE", None) and \
                    s.get("qosProfile") == settings.qod_profile:
                return s
    except Exception:
        return None
    return None


async def request_qod(ue_imsi: Optional[str]) -> bool:
    """Best-effort: create (or reuse) a QoD session for the UE's flow towards the
    application server. Returns True if the network accepted it."""
    if not settings.camara_qod_url or not ue_imsi:
        return False
    ip = settings.qod_ue_ip or await get_ue_ip(ue_imsi)
    if not ip:
        log.warning("QoD skipped: no 5G address known for UE %s", ue_imsi)
        return False
    async with httpx.AsyncClient(timeout=8.0) as client:
        if await _active_session(client, ip):
            return True
        try:
            r = await client.post(
                f"{settings.camara_qod_url}{_SESSIONS}",
                json={"device": _device(ip),
                      "applicationServer": {"ipv4Address": settings.qod_app_server},
                      "qosProfile": settings.qod_profile,
                      "duration": settings.qod_duration_s},
            )
        except Exception as exc:
            log.warning("QoD request failed: %s", exc)
            return False
        if r.status_code not in (200, 201):
            log.warning("QoD request rejected: HTTP %s %s", r.status_code, r.text[:200])
            return False
        status = (r.json() or {}).get("qosStatus")
        log.info("QoD session %s for %s (%s): %s", (r.json() or {}).get("sessionId"),
                 ip, settings.qod_profile, status)
        return status != "UNAVAILABLE"


async def release_qod(ue_imsi: Optional[str]) -> None:
    """Delete the UE's active DRIFT session, e.g. once the workload has moved."""
    if not settings.camara_qod_url or not ue_imsi:
        return
    ip = settings.qod_ue_ip or await get_ue_ip(ue_imsi)
    if not ip:
        return
    async with httpx.AsyncClient(timeout=8.0) as client:
        s = await _active_session(client, ip)
        if s:
            try:
                await client.delete(f"{settings.camara_qod_url}{_SESSIONS}/{s['sessionId']}")
            except Exception as exc:
                log.warning("QoD release failed: %s", exc)
