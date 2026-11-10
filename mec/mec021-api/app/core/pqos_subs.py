"""O-2: pQoS subscription lifecycle, tied to the AMS registration.

Two subscriptions are created per AMS, in priority order:

  * E2E_LATENCY / ABOVE  — the trigger. Latency is what relocation can actually
    fix: moving the application to a closer or less loaded edge reduces the
    UE->edge round trip. It is also the only signal that sees cell congestion,
    because H-PQOS-I predicts capacity per-device-as-if-alone.
  * THROUGHPUT_DL / BELOW — a backstop for coverage collapse, where the link
    genuinely cannot carry the application at all.

A traffic-following prediction model makes an idle UE look permanently
degraded (it predicts 0 Mbps), so DRIFT only monitors a UE WHILE an
application is registered for mobility on its behalf: subscribe on AMS
creation, unsubscribe on AMS deletion. "No traffic because nobody registered"
then means "nothing monitored" instead of "permanent alarm".

UNVERIFIED AGAINST A LIVE AF. As of 2026-09-22 the ENVELOPE AF accepts
subscriptions (201 + CorrelationID) but emits nothing: a packet capture on the
hub during two live subscriptions, one per method, recorded zero inbound
connection attempts from 10.220.2.49. Everything here is therefore written from
the API document, not from observed behaviour. On the day the notifier returns,
confirm before trusting: that E2E_LATENCY subscriptions are supported at all,
that QosThreshold for latency is in SECONDS, and that Matching=ABOVE fires on
the crossing we expect.
"""

import httpx

from app.core.config import settings

# ams_id -> [pQoS subscription CorrelationID, ...]
_subs: dict = {}
# CorrelationID -> {"ams": ams_id, "qos": QosType}. The AF quotes the
# CorrelationID back in the notification URL, and that is the ONLY thing in a
# notification that identifies it: the body is a raw NWDAF QOS_SUSTAINABILITY
# event whose value field is always called "ranUeThrouThd" and always suffixed
# "Mbps", whatever metric was actually subscribed to. So without this map a
# latency notification is indistinguishable from a throughput one.
_owner: dict = {}


def _wanted() -> set:
    return {t.strip().lower() for t in settings.pqos_sub_types.split(",") if t.strip()}


def requirements(ams_id: str) -> tuple:
    """(min_dl_mbps, max_latency_ms) for this AMS.

    An application declares what IT needs at registration; anything it leaves
    unset falls back to the platform-wide threshold, so existing registrations
    behave exactly as before."""
    dl, lat = settings.pqos_threshold_dl_mbps, settings.pqos_threshold_lat_ms
    try:
        from app.core.store import ams_store          # local: avoids an import cycle
        ctx = ams_store[ams_id].registered_app_context
        if getattr(ctx, "min_dl_mbps", None) is not None:
            dl = float(ctx.min_dl_mbps)
        if getattr(ctx, "max_latency_ms", None) is not None:
            lat = float(ctx.max_latency_ms)
    except Exception:
        pass
    return dl, lat


def subscription_for(corr: str):
    """{"ams": ..., "qos": ...} for this CorrelationID, or None if unknown.

    Unknown means an orphan: a subscription the AF still holds but whose id we
    lost (this map is in memory, and restarts lose it while the AF keeps
    delivering). Callers must NOT guess what such an event means — measured
    2026-09-22, orphans delivered ~1.5 notifications/second and, interpreted
    blind, drove workloads into a continuous migration loop."""
    return _owner.get(corr)


def _payloads(ams_id: str = "") -> list:
    """The subscription bodies to register for one AMS, trigger first.

    Deliberately the exact field set of NCSRD's own sample receiver, with no
    ApplicationServerAddress: in a SUBSCRIPTION that field is a second callback
    URL for a degradation-only message (not traffic scoping, which is its
    meaning in the request/response API), their sample omits it, and we were
    sending a bare IP that is not a URI at all. Tested 2026-09-22 with an
    isolated listener: identical subscriptions with and without it both
    delivered nothing, so it was never the cause of the outage — it is dropped
    because it is wrong, not because it fixes anything."""
    want, out = _wanted(), []
    req_dl, req_lat = requirements(ams_id) if ams_id else (
        settings.pqos_threshold_dl_mbps, settings.pqos_threshold_lat_ms)
    if "latency" in want:
        out.append({
            "NotificationAddress": settings.pqos_sink_base,
            "Method": settings.pqos_method_latency,
            "QosType": "E2E_LATENCY",
            # seconds on the wire: the AF returns latency in seconds, so a
            # 200 ms ceiling is 0.2 here. Sending 200 would arm a 200-SECOND
            # threshold that can never be crossed, and look like silence.
            "QosThreshold": round(req_lat / 1000.0, 6),
            "Matching": "ABOVE",
            "Horizon": 10,
            "ApplicationFilterID": "DRIFT",
        })
    if "throughput" in want:
        out.append({
            "NotificationAddress": settings.pqos_sink_base,
            "Method": settings.pqos_method,
            "QosType": "THROUGHPUT_DL",
            "QosThreshold": req_dl,
            "Matching": "BELOW",
            "Horizon": 10,
            "ApplicationFilterID": "DRIFT",
        })
    return out


async def subscribe_for_ams(ams_id: str) -> None:
    if not (settings.pqos_sub_url and settings.pqos_sink_base):
        return
    from app.core.store import ams_store          # local: avoids an import cycle
    ams = ams_store.get(ams_id)
    if ams is not None and not getattr(ams.registered_app_context, "autonomous_trigger", True):
        return    # the app initiates its own procedures: nothing to be notified of
    corrs = []
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            for body in _payloads(ams_id):
                r = await c.post(f"{settings.pqos_sub_url}/pqos_inf/sub/add", json=body)
                corr = r.json().get("CorrelationID") if r.status_code in (200, 201) else None
                if corr:
                    corrs.append(corr)
                    _owner[corr] = {"ams": ams_id, "qos": body["QosType"]}
                print(f"[O-2] pQoS subscription for AMS {ams_id} "
                      f"({body['QosType']} {body['Matching']} {body['QosThreshold']} "
                      f"via {body['Method']}): {r.status_code} corr={corr}", flush=True)
    except Exception as exc:
        print(f"[O-2] pQoS subscribe failed for AMS {ams_id} "
              f"(request-driven still works): {exc}", flush=True)
    if corrs:
        _subs[ams_id] = corrs


async def resubscribe_all(dl_min_mbps=None, lat_max_ms=None) -> dict:
    """Push new thresholds to the AF for every registered AMS.

    A subscription's QosThreshold is fixed at creation, so changing the
    platform's idea of "degraded" is only half the job: the network is still
    watching the old numbers until the subscriptions are replaced. Delete then
    recreate, because the AF exposes no update verb."""
    if dl_min_mbps is not None:
        settings.pqos_threshold_dl_mbps = float(dl_min_mbps)
    if lat_max_ms is not None:
        settings.pqos_threshold_lat_ms = float(lat_max_ms)
    ams_ids = list(_subs.keys())
    for ams_id in ams_ids:
        await unsubscribe_for_ams(ams_id)
        await subscribe_for_ams(ams_id)
    return {"threshold_dl_mbps": settings.pqos_threshold_dl_mbps,
            "threshold_lat_ms": settings.pqos_threshold_lat_ms,
            "resubscribed": len(ams_ids),
            "correlation_ids": [c for cs in _subs.values() for c in cs]}


async def unsubscribe_for_ams(ams_id: str) -> None:
    corrs = _subs.pop(ams_id, None)
    if not (corrs and settings.pqos_sub_url):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            for corr in corrs:
                _owner.pop(corr, None)
                r = await c.delete(f"{settings.pqos_sub_url}/pqos_inf/sub/del/{corr}")
                print(f"[O-2] pQoS subscription {corr} removed (AMS {ams_id}): "
                      f"{r.status_code}", flush=True)
    except Exception as exc:
        print(f"[O-2] pQoS unsubscribe failed for AMS {ams_id}: {exc}", flush=True)
