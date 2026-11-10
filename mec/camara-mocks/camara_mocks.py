"""
ENVELOPE CAMARA API Mock Servers – for local development before real ENVELOPE access.
Implements minimal stubs for:
  - LF CAMARA Quality-on-Demand (QoD)       :9001
  - LF CAMARA Connectivity Insights (pQoS)  :9002
  - LF CAMARA Device Location               :9003
  - Energy Telemetry (ECB 6G-LORE stub)     :9004

Run one app per port, e.g.:
  uvicorn camara_mocks:qod_app      --host 0.0.0.0 --port 9001
  uvicorn camara_mocks:pqos_app     --host 0.0.0.0 --port 9002
  uvicorn camara_mocks:location_app --host 0.0.0.0 --port 9003
  uvicorn camara_mocks:energy_app   --host 0.0.0.0 --port 9004
"""

import hashlib
import random
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# QoD Mock – port 9001
# ---------------------------------------------------------------------------
qod_app = FastAPI(title="CAMARA QoD Mock", version="0.1.0")


class QoSProfile(BaseModel):
    sessionId: str
    status: str = "AVAILABLE"
    maxUpstreamBandwidth: Optional[str] = "100Mbps"
    maxDownstreamBandwidth: Optional[str] = "100Mbps"


@qod_app.get("/qod/v0/sessions/{session_id}", response_model=QoSProfile)
async def get_qod_session(session_id: str):
    return QoSProfile(sessionId=session_id, status="AVAILABLE")


@qod_app.post("/qod/v0/sessions", response_model=QoSProfile, status_code=201)
async def create_qod_session(body: dict):
    return QoSProfile(sessionId="mock-session-001", status="AVAILABLE")


# Same paths and shapes as the real CAMARA QoD v1 API (10.220.2.50:8584), so the
# controller uses one code path against the mock and the network.
_MOCK_SESSIONS: dict = {}


@qod_app.post("/quality-on-demand/v1/sessions", status_code=201)
async def create_qod_session_v1(body: dict):
    import uuid
    sid = str(uuid.uuid4())
    s = {**body, "sessionId": sid, "qosStatus": "AVAILABLE"}
    _MOCK_SESSIONS[sid] = s
    return s


@qod_app.post("/quality-on-demand/v1/retrieve_sessions")
async def retrieve_qod_sessions_v1(body: dict):
    dev = (body.get("device") or {}).get("ipv4Address")
    return [s for s in _MOCK_SESSIONS.values() if (s.get("device") or {}).get("ipv4Address") == dev]


@qod_app.delete("/quality-on-demand/v1/sessions/{session_id}", status_code=204)
async def delete_qod_session_v1(session_id: str):
    _MOCK_SESSIONS.pop(session_id, None)


@qod_app.get("/health")
async def qod_health():
    return {"status": "ok", "mock": "QoD"}


# ---------------------------------------------------------------------------
# pQoS / Connectivity Insights Mock – port 9002
# ---------------------------------------------------------------------------
pqos_app = FastAPI(title="CAMARA Connectivity Insights Mock", version="0.1.0")


class ConnectivityInsight(BaseModel):
    deviceId: str
    predictedLatencyMs: float
    predictedThroughputMbps: float
    degradationExpected: bool
    confidence: float


# A replay driver can push the current predicted QoS via POST /ue/{device_id}.
_PQOS_STATE: dict = {}


class SetPQoS(BaseModel):
    predictedLatencyMs: float
    predictedThroughputMbps: float
    degradationExpected: bool = False
    confidence: float = 0.9


@pqos_app.post("/ue/{device_id}")
async def set_pqos(device_id: str, body: SetPQoS):
    _PQOS_STATE[device_id] = body.model_dump()
    return {"ok": True, "deviceId": device_id, **body.model_dump()}


@pqos_app.get("/connectivity-insights/{device_id}", response_model=ConnectivityInsight)
async def get_connectivity_insight(device_id: str):
    if device_id in _PQOS_STATE:
        return ConnectivityInsight(deviceId=device_id, **_PQOS_STATE[device_id])
    latency = random.uniform(5.0, 80.0)
    return ConnectivityInsight(
        deviceId=device_id,
        predictedLatencyMs=round(latency, 1),
        predictedThroughputMbps=round(random.uniform(10.0, 100.0), 1),
        degradationExpected=latency > 60.0,
        confidence=round(random.uniform(0.7, 0.99), 2),
    )


@pqos_app.get("/health")
async def pqos_health():
    return {"status": "ok", "mock": "pQoS/ConnectivityInsights"}


# ---------------------------------------------------------------------------
# Device Location Mock – port 9003
# ---------------------------------------------------------------------------
location_app = FastAPI(title="CAMARA Device Location Mock", version="0.1.0")


class DeviceLocation(BaseModel):
    deviceId: str
    latitude: float
    longitude: float
    accuracy: float  # metres


# Simulated UE positions (Athens area for the Greek ENVELOPE site).
# A replay driver can push an exact position via POST /ue/{device_id}; then the
# GET returns it verbatim (no jitter) so a dataset trajectory drives decisions.
_UE_POSITIONS = {
    "default": {"lat": 37.9838, "lon": 23.7275, "exact": False},
}


class SetUELocation(BaseModel):
    latitude: float
    longitude: float


@location_app.post("/ue/{device_id}")
async def set_ue_location(device_id: str, body: SetUELocation):
    _UE_POSITIONS[device_id] = {"lat": body.latitude, "lon": body.longitude, "exact": True}
    return {"ok": True, "deviceId": device_id, "latitude": body.latitude, "longitude": body.longitude}


@location_app.get("/location/{device_id}", response_model=DeviceLocation)
async def get_device_location(device_id: str):
    pos = _UE_POSITIONS.get(device_id, _UE_POSITIONS["default"])
    jitter = 0.0 if pos.get("exact") else random.uniform(-0.001, 0.001)
    return DeviceLocation(
        deviceId=device_id,
        latitude=pos["lat"] + jitter,
        longitude=pos["lon"] + jitter,
        accuracy=10.0,
    )


@location_app.get("/health")
async def location_health():
    return {"status": "ok", "mock": "DeviceLocation"}


# ---------------------------------------------------------------------------
# Energy Telemetry Mock – port 9004 (ECB 6G-LORE stub)
# ---------------------------------------------------------------------------
energy_app = FastAPI(title="Energy Telemetry Mock", version="0.1.0")


def _deterministic_unit_floats(seed: str, count: int) -> list[float]:
    """Stable pseudo-random floats in [0, 1) derived from a hash of `seed`,
    so the same cluster id always reports a consistent profile within a run
    instead of needing a hardcoded cluster1/cluster2 list."""
    digest = hashlib.sha256(seed.encode()).digest()
    return [b / 255.0 for b in digest[:count]]


# Demo/steering hook: pin a cluster's energy profile so Optimal Edge Selection
# can be driven deterministically (make an edge the greenest and a MEC trigger
# relocates the workload to it — the autonomous "follow-me to the best edge"
# demo). Set with PUT /energy/{cluster_id}, clear with DELETE; unset ids keep
# the stable hash-derived profile. Overrides are read live by the controller on
# the next selection, so no restart is needed after setting them.
_ENERGY_OVERRIDE: dict = {}


@energy_app.get("/energy")
async def get_energy(ids: str = ""):
    """Real DRIFT cluster ids are UUIDs, not a fixed 'cluster1'/'cluster2' pair —
    this generates a plausible, stable-per-id energy profile for whatever ids
    the caller passes (?ids=<id1>,<id2>,...). A PUT override wins over the hash."""
    cluster_ids = [c for c in ids.split(",") if c]
    nodes = {}
    for cid in cluster_ids:
        g, e, p = _deterministic_unit_floats(cid, 3)
        node = {
            "greenness": round(g, 2),
            "energy_pct": round(0.3 + e * 0.7, 2),  # keep energy headroom realistic (30-100%)
            "power_watts": round(30 + p * 270, 1),
        }
        node.update(_ENERGY_OVERRIDE.get(cid, {}))  # pinned values win
        nodes[cid] = node
    return {"nodes": nodes}


@energy_app.put("/energy/{cluster_id}")
async def set_energy(cluster_id: str, body: dict):
    """Pin greenness / energy_pct (0..1) for a cluster to steer edge selection."""
    ov = {}
    if "greenness" in body:
        ov["greenness"] = round(max(0.0, min(1.0, float(body["greenness"]))), 2)
    if "energy_pct" in body:
        ov["energy_pct"] = round(max(0.0, min(1.0, float(body["energy_pct"]))), 2)
    _ENERGY_OVERRIDE[cluster_id] = {**_ENERGY_OVERRIDE.get(cluster_id, {}), **ov}
    return {"ok": True, "cluster_id": cluster_id, **_ENERGY_OVERRIDE[cluster_id]}


@energy_app.delete("/energy/{cluster_id}")
async def clear_energy(cluster_id: str):
    """Clear a cluster's pinned profile (revert to the hash-derived default)."""
    _ENERGY_OVERRIDE.pop(cluster_id, None)
    return {"ok": True, "cluster_id": cluster_id, "cleared": True}


@energy_app.get("/health")
async def energy_health():
    return {"status": "ok", "mock": "EnergyTelemetry"}
