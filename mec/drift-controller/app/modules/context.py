"""
Context-Awareness module – polls the ENVELOPE PQoS service for the UE-centric
signals used by Optimal Edge Selection:

  * THROUGHPUT_DL / THROUGHPUT_UL (H-PQOS-I) -> the link CAPACITY the device
    can use in each direction, independent of its current traffic. Absolute
    thresholds: capacity below what the app needs = real degradation, with no
    idle-UE floor and no traffic-relative calibration.
  * E2E_LATENCY (SIMPLE-QS-PQOS-I) -> traffic-following latency prediction.
    H-PQOS-I has no latency support, and this is the one signal that sees
    cell congestion (capacity prediction is per-device-as-if-alone).

All calls are BEST-EFFORT: an empty PQoS Report (idle UE / no model data)
degrades gracefully and edge selection proceeds on energy/distance alone.

Per-candidate-edge signals (resource headroom, distance, energy/greenness) are
gathered in edge_selector.py, which needs the full candidate list first.
"""

import os
import time

from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.core.config import settings


@dataclass
class UeContext:
    predicted_latency_ms: Optional[float] = None
    predicted_throughput_mbps: Optional[float] = None
    predicted_ul_mbps: Optional[float] = None
    qos_degradation_expected: bool = False
    # True when the numeric pQoS predictions crossed the thresholds — the
    # signal that can be re-verified after a QoD grant.
    pqos_degraded: bool = False
    # Metrics reused from the previous poll because the AF returned nothing for
    # them this time. Empty on a clean read.
    carried_forward: list = field(default_factory=list)
    # Per-metric numeric verdicts {metric: {value, threshold, met}} — surfaced
    # so the dashboard can show WHY the network quality is degrading.
    pqos_metrics: Optional[dict] = None
    ue_lat: Optional[float] = None
    ue_lon: Optional[float] = None


async def _pqos_value(client: httpx.AsyncClient, qos_type: str,
                      method: str) -> Optional[float]:
    """Mean predicted value for our UE from ENVELOPE PQoS, or None if no data.
    Throughput is in Mbps; latency (E2E_LATENCY) comes back in seconds."""
    try:
        r = await client.post(
            f"{settings.pqos_url}/pqos_inf/req",
            json={"Method": method, "QosType": qos_type,
                  "ApplicationServerAddress": settings.pqos_app_server,
                  "Horizon": 10, "ApplicationFilterID": "DRIFT"},
        )
        # the real ENVELOPE AF answers 201 on /pqos_inf/req (the mock says 200)
        if r.status_code not in (200, 201):
            return None
        for rep in r.json().get("Report", []):
            if rep.get("UeID") == settings.ue_imsi:
                vals = rep.get("QosValue") or []
                if vals:
                    return sum(vals) / len(vals)
    except Exception:
        pass
    return None


async def _ue_location(client: httpx.AsyncClient, vm_name: str):
    """(lat, lon) for the device, or None. Best-effort: location is a nicety and
    must never hold up the QoS numbers the decision actually uses."""
    try:
        r = await client.get(f"{settings.camara_location_url}/location/{vm_name}")
        if r.status_code == 200:
            d = r.json()
            return d.get("latitude"), d.get("longitude")
    except Exception:
        pass
    return None


# The AF takes ~6 s to answer three metrics, and the dashboard polls this on
# every refresh — so the page moved at the AF's speed and the UE/edge arrows
# re-located seconds late. Readers that only DISPLAY the context can accept one
# that is a few seconds old; the horizon is ten seconds, so a 15 s cache changes
# nothing meaningful. Edge selection passes max_age_s=0 and always re-reads,
# because a migration decision should not be made on a stale prediction.
_CACHE: dict = {"at": 0.0, "ctx": None, "took": 0.0}
# Adaptive: the cache exists to protect the dashboard from a SLOW AF, not to
# hide a fast one. A flat 15 s made the UE QoS card update in visible 15 s steps
# once the AF recovered to ~0.2 s per metric (measured 2026-09-23). So: match the
# dashboard's 3 s poll while fresh reads are quick, and back off to 15 s only
# when the last read was slow.
CACHE_TTL_S = 15.0
CACHE_TTL_FAST_S = float(os.getenv("DRIFT_PQOS_CACHE_TTL_S", "3"))
SLOW_READ_S = 2.0


def display_max_age() -> float:
    """How old a cached context the dashboard may be shown."""
    return CACHE_TTL_S if _CACHE.get("took", 0.0) > SLOW_READ_S else CACHE_TTL_FAST_S
# How long a value may be reused when the AF goes quiet for that metric.
# Measured 2026-09-23: the AF returned our UE in only 2 of 6 consecutive calls,
# the rest being an entirely empty Report. At a 15 s poll that is a >90 s gap
# every few minutes, which emptied the UE QoS card mid-demo. 300 s bridges it.
# This is cosmetic: the value shown may be minutes old, and `carried_forward`
# names every metric that is. It does not affect decisions, which re-read.
CARRY_FORWARD_S = float(os.getenv("DRIFT_PQOS_CARRY_FORWARD_S", "300"))


# Circuit breaker. When the AF is down it still answers — HTTP 201 with an empty
# Report — but takes ~20 s to do it, so three metric lookups hang /context for a
# minute, the dashboard's own call times out, and the UE card loses even the
# fields that never needed the AF (the IMSI). Measured 2026-09-23. After a few
# consecutive empty reads, stop asking for a while and answer instantly from
# whatever we last knew; one probe re-opens it.
_BREAKER = {"fails": 0, "open_until": 0.0}
BREAKER_AFTER = 3
BREAKER_COOLDOWN_S = 60.0


async def fetch_ue_context(vm_name: str, max_age_s: float = 0.0) -> UeContext:
    if max_age_s > 0 and _CACHE["ctx"] is not None:
        if (time.monotonic() - _CACHE["at"]) < max_age_s:
            return _CACHE["ctx"]
    if time.monotonic() < _BREAKER["open_until"]:
        # AF known-down: answer immediately rather than hang the caller.
        prev = _CACHE.get("ctx")
        if prev is not None and (time.monotonic() - _CACHE["at"]) < CARRY_FORWARD_S:
            return prev
        return UeContext()
    ctx = UeContext()
    t_start = time.monotonic()
    if not settings.ue_imsi:
        return ctx  # no UE configured -> energy/distance-only selection

    async with httpx.AsyncClient(timeout=6.0) as client:
        # ENVELOPE PQoS — numeric predictions (best-effort).
        # SEQUENTIAL on purpose. Issuing these together was tried on 2026-09-23
        # and made things worse: the AF serialises internally, so three
        # concurrent requests each took the full ~6 s, hit the client timeout and
        # returned None for every metric. The AF is simply slow; the fix is to
        # stop the dashboard waiting on it (see the cache below), not to ask it
        # for more at once.
        dl = await _pqos_value(client, "THROUGHPUT_DL", settings.pqos_method)
        ul = await _pqos_value(client, "THROUGHPUT_UL", settings.pqos_method)
        lat_s = await _pqos_value(client, "E2E_LATENCY", settings.pqos_method_latency)
        loc = await _ue_location(client, vm_name)
        if dl is not None:
            ctx.predicted_throughput_mbps = round(dl, 2)
        if ul is not None:
            ctx.predicted_ul_mbps = round(ul, 2)
        # H-PQOS-I has no E2E_LATENCY support — latency stays on its own method.
        if lat_s is not None:
            ctx.predicted_latency_ms = round(lat_s * 1000.0, 2)
        if loc:
            ctx.ue_lat, ctx.ue_lon = loc

        # The AF intermittently answers one metric and not another — an empty
        # Report for that QosType, not a timeout. Dropping the value makes the
        # UE QoS card empty out a row at a time and look dead, when the link is
        # fine and the previous reading is seconds old. So carry the last good
        # value forward briefly and mark it, rather than showing nothing.
        prev = _CACHE.get("ctx")
        if prev is not None and (time.monotonic() - _CACHE["at"]) < CARRY_FORWARD_S:
            for attr in ("predicted_throughput_mbps", "predicted_ul_mbps",
                         "predicted_latency_ms"):
                if getattr(ctx, attr) is None and getattr(prev, attr) is not None:
                    setattr(ctx, attr, getattr(prev, attr))
                    ctx.carried_forward.append(attr)

        # Per-metric verdicts against the runtime thresholds. A threshold of 0
        # disables that metric; a missing prediction yields no verdict (None).
        metrics = {}
        if settings.pqos_dl_min_mbps > 0 and ctx.predicted_throughput_mbps is not None:
            metrics["downlink_capacity"] = {
                "value": ctx.predicted_throughput_mbps, "unit": "Mbps",
                "threshold": settings.pqos_dl_min_mbps, "kind": "min",
                "met": ctx.predicted_throughput_mbps >= settings.pqos_dl_min_mbps}
        if settings.pqos_ul_min_mbps > 0 and ctx.predicted_ul_mbps is not None:
            metrics["uplink_capacity"] = {
                "value": ctx.predicted_ul_mbps, "unit": "Mbps",
                "threshold": settings.pqos_ul_min_mbps, "kind": "min",
                "met": ctx.predicted_ul_mbps >= settings.pqos_ul_min_mbps}
        if settings.pqos_lat_max_ms > 0 and ctx.predicted_latency_ms is not None:
            metrics["latency"] = {
                "value": ctx.predicted_latency_ms, "unit": "ms",
                "threshold": settings.pqos_lat_max_ms, "kind": "max",
                "met": ctx.predicted_latency_ms <= settings.pqos_lat_max_ms}
        ctx.pqos_metrics = metrics or None

        if any(not m["met"] for m in metrics.values()):
            ctx.pqos_degraded = True
            ctx.qos_degradation_expected = True

    # Cache SUCCESS only. Caching a failed read would serve the empty result for
    # the whole TTL and suppress the retry that would have fixed it — which, at
    # the AF's measured ~1-in-3 answer rate, turns one miss into 15 s of blank
    # card. A miss therefore leaves the previous good value in place, and the
    # next poll tries again.
    if ctx.predicted_throughput_mbps is not None or ctx.predicted_ul_mbps is not None \
       or ctx.predicted_latency_ms is not None:
        _CACHE.update(at=time.monotonic(), ctx=ctx, took=time.monotonic() - t_start)
        _BREAKER.update(fails=0, open_until=0.0)
    else:
        _BREAKER["fails"] += 1
        if _BREAKER["fails"] >= BREAKER_AFTER:
            _BREAKER["open_until"] = time.monotonic() + BREAKER_COOLDOWN_S
            print(f"[pqos] AF returned nothing {_BREAKER['fails']}x — "
                  f"pausing lookups for {BREAKER_COOLDOWN_S:.0f}s", flush=True)
    return ctx
