"""
DRIFT – ETSI GS MEC 021 Application Mobility Service API
NBI (Northbound Interface) implemented with FastAPI.

Spec reference: ETSI GS MEC 021 v2.2.1
Base path: /amsi/v1
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from app.routers import ams, mobility_procedures, subscriptions, pqos_events
from app.core.config import settings
from app.core import pqos_subs

app = FastAPI(
    title="DRIFT – ETSI MEC 021 Application Mobility Service",
    description="Standards-aligned MEC 021 AMS northbound interface for DRIFT edge migration",
    version="1.0.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

app.include_router(ams.router, prefix="/amsi/v1", tags=["Application Mobility Services"])
app.include_router(mobility_procedures.router, prefix="/amsi/v1", tags=["Mobility Procedures"])
app.include_router(subscriptions.router, prefix="/amsi/v1", tags=["Subscriptions"])
# O-2: pQoS degradation events land here (outside /amsi/v1 — it is DRIFT's own
# sink for the ENVELOPE notification API, not part of the MEC-021 surface).
app.include_router(pqos_events.router, tags=["O-2 predictive trigger"])


# O-2 note: the pQoS subscription is PER-AMS (created on registration, removed
# on deregistration — see app/core/pqos_subs.py), so a UE is only monitored
# while an application is registered for mobility on its behalf. An idle,
# unmonitored UE therefore raises no events at all.


@app.on_event("startup")
async def _start_poll_trigger():
    import asyncio
    from app.routers.pqos_events import poll_loop
    app.state.poll_task = asyncio.create_task(poll_loop())


@app.get("/auto-trigger", tags=["O-2 predictive trigger"])
async def get_auto_trigger():
    """Whether pQoS events may initiate migrations, and the admission limits."""
    return {"enabled": settings.auto_trigger_enabled,
            "mode": settings.pqos_trigger_mode,
            "max_concurrent": settings.auto_trigger_max_concurrent,
            "cooldown_s": settings.auto_trigger_cooldown_s}


@app.post("/auto-trigger", tags=["O-2 predictive trigger"])
async def set_auto_trigger(body: dict):
    """Arm or disarm the autonomous trigger at runtime.

    Needed because the alternative is a restart, and a restart drops the
    in-memory AMS registry — which loses the CorrelationIDs of live
    subscriptions while the AF keeps delivering to them. Disarming here leaves
    the registrations and subscriptions intact and simply stops acting on
    events, which is what you want between demo acts."""
    if "enabled" in body:
        settings.auto_trigger_enabled = bool(body["enabled"])
    if body.get("max_concurrent") is not None:
        settings.auto_trigger_max_concurrent = int(body["max_concurrent"])
    if body.get("cooldown_s") is not None:
        settings.auto_trigger_cooldown_s = float(body["cooldown_s"])
    if body.get("mode") in ("notify", "poll", "both"):
        settings.pqos_trigger_mode = body["mode"]
    return {"enabled": settings.auto_trigger_enabled,
            "mode": settings.pqos_trigger_mode,
            "max_concurrent": settings.auto_trigger_max_concurrent,
            "cooldown_s": settings.auto_trigger_cooldown_s}


@app.get("/pqos-thresholds", tags=["O-2 predictive trigger"])
async def get_pqos_threshold():
    """The thresholds currently registered with the AF subscriptions."""
    return {"threshold_dl_mbps": settings.pqos_threshold_dl_mbps,
            "threshold_lat_ms": settings.pqos_threshold_lat_ms,
            "subscriptions": [
                {"qos_type": "E2E_LATENCY", "matching": "ABOVE",
                 "method": settings.pqos_method_latency,
                 "threshold_ms": settings.pqos_threshold_lat_ms},
                {"qos_type": "THROUGHPUT_DL", "matching": "BELOW",
                 "method": settings.pqos_method,
                 "threshold_mbps": settings.pqos_threshold_dl_mbps},
            ],
            "active_types": settings.pqos_sub_types}


@app.post("/pqos-thresholds", tags=["O-2 predictive trigger"])
async def set_pqos_threshold(body: dict):
    """Re-register the AF subscriptions against new thresholds, so the values the
    NETWORK watches track the values the platform is judging against."""
    dl = body.get("dl_min_mbps", body.get("threshold_dl_mbps"))
    lat = body.get("lat_max_ms", body.get("threshold_lat_ms"))
    if dl is None and lat is None:
        return {"changed": False, "reason": "no dl_min_mbps or lat_max_ms in body"}
    return await pqos_subs.resubscribe_all(
        dl_min_mbps=None if dl is None else float(dl),
        lat_max_ms=None if lat is None else float(lat))


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "service": "drift-mec021-api"}
