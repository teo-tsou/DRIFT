"""
Energy telemetry client – per-cluster greenness and available energy, keyed by
the real DRIFT cluster id (UUID), not a fixed cluster list.

Three sources:
  - the ECB DRIFT Public API (GET /getGreenness) for the clusters named in
    settings.ecb_edge_map: the share of the workload's energy over the next
    ecb_horizon_h hours covered by the site's P50 PV forecast, matched in
    15-minute intervals;
  - the same API (GET /v1/getEVGreenness) for the vehicle clusters named in
    settings.ecb_vehicle_clusters: the green share of the energy stored at the
    EV's last charge and its residual energy, read from the vehicle (Tesla);
  - the energy telemetry endpoint for every other cluster, and for energy_pct.

Only GET is ever sent to ECB, always with workload_energy_kwh. A POST without
that value starts a calibration on their hardware, which DRIFT must not do.
"""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)

_ATHENS = ZoneInfo("Europe/Athens")
_cache: dict[tuple, tuple[float, float]] = {}


def _edge_map() -> dict[str, tuple[str, str, float]]:
    """Parse ecb_edge_map into {cluster name: (edge_location, model, watts)}."""
    out = {}
    for item in filter(None, (s.strip() for s in settings.ecb_edge_map.split(","))):
        name, _, spec = item.partition("=")
        parts = spec.split(":")
        if len(parts) < 2:
            log.warning("ignoring malformed ECB_EDGE_MAP entry %r", item)
            continue
        watts = float(parts[2]) if len(parts) > 2 and parts[2] else settings.ecb_workload_w
        out[name.strip()] = (parts[0], parts[1], watts)
    return out


async def _ecb_greenness(client: httpx.AsyncClient, location: str, model: str,
                         watts: float) -> float | None:
    """Greenness (0-1) of running the workload at `location` from now on, or
    None if ECB cannot answer. Hours are Europe/Athens, as the API expects."""
    now = datetime.now(_ATHENS)
    start = now.hour
    end = min(24, start + max(1, settings.ecb_horizon_h))
    kwh = watts * (end - start) / 1000.0
    key = (location, model, watts, now.date(), start, end)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[1] < settings.ecb_cache_s:
        return hit[0]
    try:
        r = await client.get(
            f"{settings.ecb_api_url}/getGreenness",
            params={"start_HH": start, "end_HH": end, "workload_energy_kwh": kwh,
                    "edge_location": location, "model": model},
            auth=(settings.ecb_api_user, settings.ecb_api_password),
        )
        if r.status_code != 200:
            log.warning("ECB greenness %s: HTTP %s %s", location, r.status_code, r.text[:200])
            return None
        g = float(r.json()["greenness_percentage"]) / 100.0
    except Exception as e:
        log.warning("ECB greenness %s failed: %s", location, e)
        return None
    _cache[key] = (g, time.monotonic())
    return g


async def _ecb_ev(client: httpx.AsyncClient) -> tuple[float, float] | None:
    """(greenness 0-1, available energy 0-1) of the EV battery, or None. The API
    reports the previous day for a date, so fall back a day when a date has no
    reading yet."""
    today = datetime.now(_ATHENS).date()
    hit = _cache.get(("ev", today))
    if hit and time.monotonic() - hit[1] < settings.ecb_cache_s:
        return hit[0]
    for back in range(3):
        day = today.fromordinal(today.toordinal() - back)
        try:
            r = await client.get(f"{settings.ecb_api_url}/v1/getEVGreenness", params={"date": day.isoformat()},
                                 auth=(settings.ecb_api_user, settings.ecb_api_password))
        except Exception as e:
            log.warning("ECB EV greenness failed: %s", e)
            return None
        if r.status_code != 200:     # 422 right after midnight: that Greek day has no data yet
            log.warning("ECB EV greenness %s: HTTP %s %s", day, r.status_code, r.text[:200])
            continue
        d = r.json()
        g, kwh = d.get("EV_greenness_percentage"), d.get("EV_residual_energy_kwh")
        if g is not None and kwh is not None:
            val = (float(g) / 100.0, max(0.0, min(1.0, float(kwh) / settings.ecb_ev_capacity_kwh)))
            _cache[("ev", today)] = (val, time.monotonic())
            return val
    return None


async def fetch_energy(clusters: list[dict]) -> dict[str, dict]:
    """Returns {cluster_id: {"greenness": float 0-1, "energy_pct": float 0-1,
    "source": "ecb" | "telemetry" | "default"}}.

    Falls back to a neutral 0.0/1.0 reading per cluster if no source answers,
    so scoring degrades gracefully instead of crashing the selection.
    """
    if not clusters:
        return {}
    ids = [c["id"] for c in clusters]
    out = {cid: {"greenness": 0.0, "energy_pct": 1.0, "source": "default"} for cid in ids}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(
                f"{settings.energy_telemetry_url}/energy",
                params={"ids": ",".join(ids)},
            )
            if r.status_code == 200:
                nodes = r.json().get("nodes", {})
                for cid in ids:
                    if cid in nodes:
                        out[cid] = {
                            "greenness": nodes[cid].get("greenness", 0.0),
                            "energy_pct": nodes[cid].get("energy_pct", 1.0),
                            "source": "telemetry",
                        }
        except Exception:
            pass

        if settings.ecb_api_url:
            emap = _edge_map()
            vehicles = {s.strip() for s in settings.ecb_vehicle_clusters.split(",") if s.strip()}
            for c in clusters:
                if c.get("name", "") in vehicles:
                    ev = await _ecb_ev(client)
                    if ev is not None:
                        out[c["id"]] = {"greenness": ev[0], "energy_pct": ev[1], "source": "ecb-ev"}
                    continue
                spec = emap.get(c.get("name", ""))
                if not spec:
                    continue
                g = await _ecb_greenness(client, *spec)
                if g is not None:
                    out[c["id"]]["greenness"] = g
                    out[c["id"]]["source"] = "ecb"
    return out
