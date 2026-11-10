"""
DRIFT Framework controller — Context-Awareness, Optimal Edge Selection and
Migration Control, sitting between the MEC 021 NBI and the DRIFT backend
(SBI infrastructure layer). Holds no kubeconfigs; all cluster/VM state and
migration execution goes through the DRIFT backend's REST API.
"""

from fastapi import FastAPI
from prometheus_client import make_asgi_app

from app.routers import candidates, migrate
from app.core.config import settings
from app.modules.context import display_max_age, fetch_ue_context

app = FastAPI(
    title="DRIFT Framework Controller",
    description="Context-awareness, optimal edge selection and migration control for DRIFT",
    version="1.0.0",
)

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

app.include_router(migrate.router, tags=["Migration"])
app.include_router(candidates.router, tags=["Edge Selection"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "drift-controller"}


@app.get("/context", tags=["Dashboard"])
async def context():
    """Live UE QoS context (ENVELOPE PQoS predictions) for the configured UE —
    surfaced for the Follow-Me dashboard."""
    # Display only: a cached context is fine here and keeps the dashboard from
    # blocking ~6 s on the AF. Edge selection re-reads with max_age_s=0.
    ctx = await fetch_ue_context(settings.ue_imsi or "demo", max_age_s=display_max_age())
    return {
        "ue_imsi": settings.ue_imsi or None,
        "predicted_throughput_mbps": ctx.predicted_throughput_mbps,
        "predicted_ul_mbps": ctx.predicted_ul_mbps,
        "predicted_latency_ms": ctx.predicted_latency_ms,
        "qos_degradation_expected": ctx.qos_degradation_expected,
        "pqos_metrics": ctx.pqos_metrics,
        # metrics reused from the previous poll because the AF returned nothing
        # for them this time; empty on a clean read
        "carried_forward": ctx.carried_forward or None,
        "ue_lat": ctx.ue_lat,
        "ue_lon": ctx.ue_lon,
    }


@app.get("/ue", tags=["Dashboard"])
async def get_ue():
    return {"imsi": settings.ue_imsi or None, "ue_ip": settings.qod_ue_ip or None}


@app.post("/ue", tags=["Dashboard"])
async def set_ue(body: dict):
    """Switch the UE that pQoS is read for and QoD is requested on (the DRIFT
    backend calls this when the dashboard's active UE profile changes)."""
    from app.modules import context as _ctx
    if body.get("imsi") is not None:
        settings.ue_imsi = str(body["imsi"]).strip()
    if body.get("ue_ip") is not None:
        settings.qod_ue_ip = str(body["ue_ip"]).strip()
    _ctx._CACHE.update(at=0.0, ctx=None)     # never show the previous UE's context
    return await get_ue()


@app.get("/pqos-thresholds", tags=["Dashboard"])
async def pqos_thresholds():
    """The numeric pQoS degradation thresholds (operational knobs, distinct from
    the CAMARA application-profile requirements)."""
    return {"dl_min_mbps": settings.pqos_dl_min_mbps,
            "ul_min_mbps": settings.pqos_ul_min_mbps,
            "lat_max_ms": settings.pqos_lat_max_ms}


@app.post("/pqos-thresholds", tags=["Dashboard"])
async def update_pqos_thresholds(body: dict):
    """Set the numeric pQoS thresholds live (demo lever). dl/ul_min_mbps compare
    against the H-PQOS-I capacity predictions; lat_max_ms against the SIMPLE
    E2E_LATENCY prediction, whose healthy range through the model is ~50-120 ms
    — a lat_max_ms below that floor reads as permanently degraded."""
    if body.get("dl_min_mbps") is not None:
        settings.pqos_dl_min_mbps = float(body["dl_min_mbps"])
    if body.get("ul_min_mbps") is not None:
        settings.pqos_ul_min_mbps = float(body["ul_min_mbps"])
    if body.get("lat_max_ms") is not None:
        settings.pqos_lat_max_ms = float(body["lat_max_ms"])
    return {"dl_min_mbps": settings.pqos_dl_min_mbps,
            "ul_min_mbps": settings.pqos_ul_min_mbps,
            "lat_max_ms": settings.pqos_lat_max_ms}


@app.get("/decisions", tags=["Dashboard"])
async def decisions():
    """Recent edge-selection decisions (most recent first) — for the dashboard."""
    out = []
    for d in reversed(list(migrate._migrations.values())):
        out.append({
            "procedure_id": d.get("procedure_id"),
            "workload": d.get("vm_name"),
            "state": d.get("state"),
            "decision": d.get("decision"),
            "target_cluster": d.get("target_cluster"),
            "target_node": d.get("target_node"),
            "reason": d.get("reason"),
            "migration_latency_ms": d.get("migration_latency_ms"),
        })
    return out[:20]
