"""O-2: event-driven predictive trigger.

DRIFT registers as the notification sink of the ENVELOPE pQoS subscription API
(POST /pqos_inf/sub/add, NotificationAddress = this service). NWDAF then posts
QOS_SUSTAINABILITY events to {sink}/{CorrelationID}/{UeID} whenever the
predicted QoS crosses the calibrated threshold. On a matching event, this
router initiates a standard MEC-021 mobility procedure for every registered
Application Mobility Service, so the autonomous trigger stays fully
standards-visible: same procedure records, same state machine, same
notifications as an operator-initiated relocation.

Two subscriptions feed this route: E2E_LATENCY/ABOVE (the trigger, since
relocation can actually reduce UE->edge latency) and THROUGHPUT_DL/BELOW (a
coverage-collapse backstop). Either one arriving initiates the procedure; the
router does not re-evaluate the numbers, it trusts the AF's threshold.

Scope. The AF quotes back the CorrelationID of the subscription that fired, and
each AMS holds its own subscriptions, so an event normally names ONE
application. Only when that mapping is unknown — after a restart, since it is
held in memory — does this fall back to every AMS on the UE.

Guards, in the order they apply:
  * only events for the configured UE are honoured;
  * a no-traffic prediction horizon means an IDLE UE, not degradation — ignored;
  * per-AMS cooldown + in-flight suppression prevent procedure spam
    (the controller's own anti-flap hysteresis/cooldown still applies below);
  * the predicted values are re-checked against THIS application's declared
    requirements, because a UE-level degradation does not affect every
    application on that UE equally;
  * admission control caps concurrent auto-initiated migrations, stateful
    services first.
"""

import asyncio
import re
import time

import httpx

from fastapi import APIRouter, BackgroundTasks, Request

from app.core import pqos_subs
from app.core.config import settings
from app.core.store import ams_store, procedure_store
from app.models.mec021 import MobilityProcedureInfo, MobilityProcedureState
from app.routers.mobility_procedures import _run_migration

router = APIRouter()

_last_auto: dict = {}   # ams_id -> monotonic ts of the last auto-initiated procedure

_ACTIVE = (MobilityProcedureState.INITIALIZING, MobilityProcedureState.PREPARING,
           MobilityProcedureState.SELECTING, MobilityProcedureState.TRANSFERRING)


# The AF sends a raw NWDAF QOS_SUSTAINABILITY event whose value field is ALWAYS
# "ranUeThrouThd" and ALWAYS suffixed "Mbps" — even for an E2E_LATENCY
# subscription. Nothing in the body says which metric it is; only the
# CorrelationID does. So parse the numbers here and let the caller, who knows
# the QosType, decide what they mean.
_VALS = re.compile(r'"ranUeThrouThd"\s*:\s*"?([-\d.eE+]+)')


def _values(body_text: str) -> list:
    return [float(v) for v in _VALS.findall(body_text)]


def _assess(values: list, qos_type: str, min_dl_mbps: float, max_latency_ms: float):
    """(violated, description) for one horizon, read as the metric subscribed to.

    Uses the MEAN of the horizon, not an extreme. A single zero second in ten is
    normal jitter; migrating a workload because of it is how a controller ends up
    flapping, which is exactly what min() produced on 2026-09-22."""
    if not values:
        return None, "no values in payload"
    mean = sum(values) / len(values)
    if qos_type == "E2E_LATENCY":
        # the AF speaks seconds for latency, whatever the field is labelled
        ms = mean * 1000.0
        if max_latency_ms > 0 and ms > max_latency_ms:
            return True, f"mean predicted latency {ms:.0f} > {max_latency_ms:.0f} ms"
        return False, f"mean predicted latency {ms:.0f} ms, within {max_latency_ms:.0f}"
    if qos_type == "THROUGHPUT_DL":
        if min_dl_mbps > 0 and mean < min_dl_mbps:
            return True, f"mean predicted DL {mean:.2f} < {min_dl_mbps:.2f} Mbps"
        return False, f"mean predicted DL {mean:.2f} Mbps, above {min_dl_mbps:.2f}"
    return None, f"unhandled QosType {qos_type}"


def _all_zero(values: list) -> bool:
    """An all-zero horizon is an IDLE UE reported by a traffic-following model,
    not a degraded network."""
    return bool(values) and all(v == 0.0 for v in values)


def _in_flight() -> int:
    return sum(1 for p in procedure_store.values() if p.state in _ACTIVE)


def _admit(ams_id: str, why: str, spawn) -> tuple:
    """Admission control shared by BOTH trigger sources, so a notification and a
    poll can never double-start a migration and every guard behaves the same:
    per-AMS cooldown, one procedure per AMS in flight, global concurrency cap."""
    ams = ams_store.get(ams_id)
    now = time.monotonic()
    initiated, skipped = [], []
    if ams is None:
        skipped.append({"ams": ams_id, "reason": "no longer registered"})
    elif now - _last_auto.get(ams_id, 0) < settings.auto_trigger_cooldown_s:
        skipped.append({"ams": ams_id, "reason": "cooldown"})
    elif any(p.app_mobility_service_id == ams_id and p.state in _ACTIVE
             for p in procedure_store.values()):
        skipped.append({"ams": ams_id, "reason": "procedure already in flight"})
    elif _in_flight() >= settings.auto_trigger_max_concurrent:
        skipped.append({"ams": ams_id, "reason": "at the concurrent-migration limit"})
    else:
        proc = MobilityProcedureInfo(
            app_mobility_service_id=ams_id,
            state=MobilityProcedureState.INITIALIZING,
            requested_target_cluster=None,   # autonomous: Optimal Edge Selection decides
        )
        procedure_store[proc.mobility_procedure_id] = proc
        _last_auto[ams_id] = now
        spawn(proc.mobility_procedure_id, ams_id)
        print(f"[O-2] auto-initiated {proc.mobility_procedure_id[:8]} for "
              f"{ams.registered_app_context.vm_name}: {why}", flush=True)
        initiated.append({"ams": ams_id, "procedure": proc.mobility_procedure_id,
                          "workload": ams.registered_app_context.vm_name, "why": why})
    return initiated, skipped


async def _handle(correlation_id: str, ue_id: str, body: str,
                  background_tasks: BackgroundTasks) -> dict:
    if not settings.auto_trigger_enabled:
        return {"status": "ignored", "reason": "auto trigger disabled"}
    if settings.pqos_trigger_mode not in ("notify", "both"):
        return {"status": "ignored", "reason": f"trigger mode is {settings.pqos_trigger_mode}"}

    sub = pqos_subs.subscription_for(correlation_id)
    if sub is None:
        # An orphan: the AF still holds this subscription but we lost its id, so
        # we cannot know which metric the numbers are, nor which application
        # asked for it. Guessing is what produced a migration loop on
        # 2026-09-22 — a latency horizon read as throughput looks like total
        # collapse. Registering an AMS creates fresh subscriptions within
        # seconds, so refusing here self-heals rather than disabling anything.
        return {"status": "ignored", "correlation": correlation_id,
                "reason": "unknown subscription (orphaned on the AF); cannot tell which metric or application this is"}

    values = _values(body)
    if _all_zero(values):
        return {"status": "ignored", "reason": "idle UE (all-zero horizon), not degradation"}

    ams_id = sub["ams"]
    qos_type = sub["qos"]
    ams = ams_store.get(ams_id)
    if ams is None:
        return {"status": "ignored", "reason": f"AMS {ams_id} no longer registered"}

    if not getattr(ams.registered_app_context, "autonomous_trigger", True):
        return {"status": "ignored", "reason": f"AMS {ams_id} initiates its own procedures"}

    imsi = getattr(ams.registered_app_context, "ue_imsi", None) or settings.ue_imsi
    if ue_id != imsi:
        return {"status": "ignored", "reason": f"event is for UE {ue_id}, this AMS serves {imsi}"}

    min_dl, max_lat = pqos_subs.requirements(ams_id)
    violated, why = _assess(values, qos_type, min_dl, max_lat)
    if not violated:
        return {"status": "ignored", "correlation": correlation_id, "qos_type": qos_type,
                "reason": why}

    initiated, skipped = _admit(
        ams_id, why, lambda pid, a: background_tasks.add_task(_run_migration, pid, a))

    return {"status": "accepted", "correlation": correlation_id, "ue": ue_id,
            "qos_type": qos_type, "assessment": why, "values": values[:10],
            "initiated": initiated, "skipped": skipped}


def _note(correlation_id: str, ue_id: str, body: str, resp: dict) -> None:
    """One human-readable line per notification, whatever the outcome.

    Without this the sink was silent unless it acted, so the only visible trace
    of the network talking to us was uvicorn's bare access line — no metric, no
    value, no verdict. This is the line to put on screen: it shows the
    notification arriving from NCSRD's AF, what it predicted, what the
    application asked for, and what the platform decided."""
    if settings.pqos_trigger_mode == "poll":
        return          # not a trigger source right now; keep the pane readable
    sub = pqos_subs.subscription_for(correlation_id)
    qos = sub["qos"] if sub else "?"
    app, req = "unknown subscription", ""
    if sub and sub["ams"] in ams_store:
        app = ams_store[sub["ams"]].registered_app_context.vm_name
        min_dl, max_lat = pqos_subs.requirements(sub["ams"])
        req = (f"needs < {max_lat:.0f} ms" if qos == "E2E_LATENCY"
               else f"needs > {min_dl:.1f} Mbps")
    vals = _values(body)
    if vals:
        m = sum(vals) / len(vals)
        pred = f"{m*1000:.0f} ms" if qos == "E2E_LATENCY" else f"{m:.1f} Mbps"
    else:
        pred = "no values"
    if resp.get("initiated"):
        verdict = f"MIGRATION INITIATED ({resp['initiated'][0]['procedure'][:8]})"
    elif resp.get("skipped"):
        verdict = f"not acting: {resp['skipped'][0]['reason']}"
    else:
        verdict = f"not acting: {resp.get('reason', resp.get('status'))}"
    print(f"[pqos] {time.strftime('%H:%M:%S')}  notification from AF  UE ...{ue_id[-3:]}  "
          f"{qos} predicted {pred}  | {app} {req}  -> {verdict}", flush=True)


@router.post("/pqos-events/{correlation_id}/{ue_id}", status_code=200)
async def pqos_event(correlation_id: str, ue_id: str,
                     request: Request, background_tasks: BackgroundTasks):
    body = (await request.body()).decode("utf-8", "replace")
    resp = await _handle(correlation_id, ue_id, body, background_tasks)
    _note(correlation_id, ue_id, body, resp)
    return resp


# ── Poll trigger: evaluate the AF's live request path on a timer ─────────────
_tasks: set = set()     # keep references so spawned migrations are not GC'd


async def _req_values(client, method: str, qos_type: str, imsi: str):
    try:
        r = await client.post(f"{settings.pqos_sub_url}/pqos_inf/req", json={
            "Method": method, "QosType": qos_type, "Horizon": 10,
            "ApplicationFilterID": "DRIFT"})
        if r.status_code not in (200, 201):
            return None
        for rep in r.json().get("Report", []):
            if rep.get("UeID") == imsi:
                return rep.get("QosValue") or None
    except Exception:
        return None
    return None


async def _poll_once(client) -> None:
    if not (settings.auto_trigger_enabled and settings.pqos_sub_url and ams_store):
        return
    if settings.pqos_trigger_mode not in ("poll", "both"):
        return
    by_imsi: dict = {}
    for ams_id, ams in list(ams_store.items()):
        if not getattr(ams.registered_app_context, "autonomous_trigger", True):
            continue
        imsi = getattr(ams.registered_app_context, "ue_imsi", None) or settings.ue_imsi
        by_imsi.setdefault(imsi, []).append(ams_id)
    for imsi, ids in by_imsi.items():
        lat = await _req_values(client, settings.pqos_method_latency, "E2E_LATENCY", imsi)
        dl = await _req_values(client, settings.pqos_method, "THROUGHPUT_DL", imsi)
        for ams_id in ids:
            ams = ams_store.get(ams_id)
            if ams is None:
                continue
            min_dl, max_lat = pqos_subs.requirements(ams_id)
            why, violated = None, False
            for vals, qt in ((lat, "E2E_LATENCY"), (dl, "THROUGHPUT_DL")):
                if vals and not _all_zero(vals):
                    v, w = _assess(vals, qt, min_dl, max_lat)
                    if v:
                        violated, why = True, w
                        break
            if violated:
                initiated, skipped = _admit(ams_id, why, _spawn)
                verdict = (f"MIGRATION INITIATED ({initiated[0]['procedure'][:8]})" if initiated
                           else f"not acting: {skipped[0]['reason']}")
            else:
                verdict = f"ok, within {max_lat:.0f} ms"
            # Lead with the two values that move under cell congestion (predicted
            # latency up ~20x, predicted DL capacity roughly halves) and mark the
            # direction against the previous poll, so a spike reads at a glance.
            lat_ms = sum(lat) / len(lat) * 1000 if lat else None
            dl_mb = sum(dl) / len(dl) if dl else None
            prev = _last_seen.get(imsi, (None, None))
            ls = "n/a" if lat_ms is None else f"{lat_ms:5.0f} ms {_arrow(lat_ms, prev[0])}"
            ds = "n/a" if dl_mb is None else f"{dl_mb:5.1f} Mbps {_arrow(dl_mb, prev[1])}"
            _last_seen[imsi] = (lat_ms, dl_mb)
            mark = "  <<<" if initiated_now(verdict) else ""
            print(f"[pqos] {time.strftime('%H:%M:%S')}  pQoS prediction  latency {ls}   DL capacity {ds}"
                  f"   | {ams.registered_app_context.vm_name} needs < {max_lat:.0f} ms  ->  {verdict}{mark}",
                  flush=True)


_last_seen: dict = {}   # imsi -> (latency_ms, dl_mbps) from the previous poll


def _arrow(now, before) -> str:
    """Direction vs the previous poll; flat within 15% so noise stays quiet."""
    if now is None or before is None or before == 0:
        return " "
    r = now / before
    return "▲" if r > 1.15 else "▼" if r < 0.87 else "·"


def initiated_now(verdict: str) -> bool:
    return verdict.startswith("MIGRATION INITIATED")


def _spawn(pid: str, ams_id: str) -> None:
    t = asyncio.create_task(_run_migration(pid, ams_id))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def poll_loop() -> None:
    """Runs for the life of the service. Cheap when idle: it returns before
    touching the AF unless armed, in poll/both mode, with apps registered."""
    await asyncio.sleep(5)
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                await _poll_once(client)
            except Exception as exc:
                print(f"[pqos] poll error: {exc}", flush=True)
            await asyncio.sleep(max(1.0, settings.pqos_poll_interval_s))
