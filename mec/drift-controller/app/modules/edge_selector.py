"""
Optimal Edge Selector.

Enumerates every ACTIVE site/cluster known to the DRIFT backend (including
the VM's current one), hard-filters out anything that cannot actually fit
the VM's CPU/memory footprint using the backend's own resource-aware node
data, then picks a target.

Default model ("constrained", see _decide_constrained): greenness is the
objective and everything else is a hard constraint (resources, UE QoS after
QoD, service area, energy reserve); proximity and energy only break ties. The
earlier weighted sum of proximity, greenness and energy is kept behind
settings.selection_model = "weighted" so results measured with it stay
reproducible.

Design notes vs. a naive "pick highest score" selector:
  - Resource feasibility is a hard gate, not a scoring input. A cluster
    with no room for the VM is never a candidate, however green it is.
  - The current placement is itself a candidate ("stay"). A hysteresis
    margin means DRIFT only migrates when it's clearly worth it (see proposal
    risk: "increased overhead from frequent migrations").
  - A target on a different cluster than the source is reported as
    "cross_cluster" and executed via KubeVirt decentralized cross-cluster
    live migration (the DRIFT backend copies VM state over the network; no
    shared storage required).
"""

import asyncio
import math
from dataclasses import dataclass, field
from typing import Optional

from app.core.config import settings
from app.core.drift_backend_client import (
    DriftBackendError,
    get_cluster_nodes,
    get_vm,
    get_pod,
    list_active_clusters,
    list_sites,
)

# A pod that declares no resource requests still needs *some* footprint for the
# feasibility gate; keep it light so lightweight edge pods stay placeable anywhere.
POD_DEFAULT_CPU_M = 50
POD_DEFAULT_MEM_MI = 64
from app.modules.context import UeContext, fetch_ue_context
from app.modules.qod import release_qod, request_qod
from app.modules.energy import fetch_energy


async def _get_cluster_nodes_safe(cluster_id: str) -> list[dict]:
    try:
        return await get_cluster_nodes(cluster_id)
    except DriftBackendError:
        return []


@dataclass
class EdgeCandidate:
    cluster_id: str
    cluster_name: str
    node_name: str
    is_source_cluster: bool
    is_current_placement: bool
    feasible: bool
    available_cpu_m: int
    available_mem_mi: int
    distance_km: Optional[float]
    greenness: float
    energy_pct: float
    score: float = 0.0
    infeasible_reason: Optional[str] = None
    violations: list[str] = field(default_factory=list)


@dataclass
class EdgeSelection:
    decision: str  # "stay" | "intra_cluster" | "cross_cluster" | "no_candidates"
    target_cluster_id: Optional[str]
    target_cluster_name: Optional[str]
    target_node: Optional[str]
    reason: str
    candidates: list[EdgeCandidate] = field(default_factory=list)


def _parse_mem_to_mib(mem_str) -> int:
    s = str(mem_str).strip()
    try:
        if s.endswith("Gi"):
            return int(float(s[:-2]) * 1024)
        if s.endswith("Mi"):
            return int(float(s[:-2]))
        if s.endswith("Ki"):
            return max(1, int(float(s[:-2])) // 1024)
        return max(1, int(float(s)) // (1024 * 1024))
    except ValueError:
        return 512  # conservative fallback for unparsable/"—" values


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _score(distance_km: Optional[float], greenness: float, energy_pct: float) -> float:
    if distance_km is None:
        norm_dist = 0.5  # unknown proximity — neutral, don't penalise or reward
    else:
        span = settings.distance_max_km - settings.distance_min_km
        norm_dist = min(max((distance_km - settings.distance_min_km) / span, 0.0), 1.0)
    proximity_score = 1.0 - norm_dist
    return (
        settings.weight_distance * proximity_score
        + settings.weight_greenness * greenness
        + settings.weight_energy * energy_pct
    )


async def select_optimal_edge(
    vm_name: str,
    source_cluster: str,
    source_namespace: str,
    preferred_cluster: Optional[str] = None,
    kind: str = "vm",
) -> EdgeSelection:
    """Never raises — any DRIFT backend failure (unknown cluster, workload not
    found, backend unreachable) surfaces as a "no_candidates" decision with
    a human-readable reason, so callers (both the /migrate flow and the
    read-only /candidates endpoint) always get a clean, typed result.

    `kind` is "vm" or "pod": the selection algorithm is identical; only the
    workload's resource footprint is fetched differently (a VM's cpu/memory vs a
    pod's summed container requests)."""
    try:
        return await _select_optimal_edge(vm_name, source_cluster, source_namespace, preferred_cluster, kind)
    except DriftBackendError as exc:
        return EdgeSelection(
            decision="no_candidates",
            target_cluster_id=None,
            target_cluster_name=None,
            target_node=None,
            reason=str(exc),
        )


async def _ue_context_for(vm_name: str, preferred_cluster: Optional[str]) -> UeContext:
    if not preferred_cluster:
        return await fetch_ue_context(vm_name)
    try:
        return await asyncio.wait_for(fetch_ue_context(vm_name, max_age_s=120), timeout=1.0)
    except asyncio.TimeoutError:
        return UeContext()


async def _select_optimal_edge(
    vm_name: str,
    source_cluster: str,
    source_namespace: str,
    preferred_cluster: Optional[str],
    kind: str = "vm",
) -> EdgeSelection:
    # The workload lookup is independent of the UE context, cluster list and
    # site registry — run all four concurrently (KPI-7 hot path).
    _wl_fetch = get_pod if kind == "pod" else get_vm
    # A requested target is honoured whatever the UE's QoS or position (see
    # _decide_constrained), so the UE context only annotates it: take a recent
    # cached one rather than wait for the pQoS AF, which has taken ~10 s.
    workload, _ue_ctx, _clusters, _sites = await asyncio.gather(
        _wl_fetch(source_cluster, source_namespace, vm_name),
        _ue_context_for(vm_name, preferred_cluster), list_active_clusters(), list_sites()
    )
    if not workload:
        return EdgeSelection(
            decision="no_candidates",
            target_cluster_id=None,
            target_cluster_name=None,
            target_node=None,
            reason=f"{kind} '{vm_name}' not found on source cluster '{source_cluster}'",
        )

    current_node = workload.get("node", "")
    if kind == "pod":
        required_cpu_m = int(workload.get("cpu_m") or 0) or POD_DEFAULT_CPU_M
        required_mem_mi = int(workload.get("mem_mi") or 0) or POD_DEFAULT_MEM_MI
    else:
        try:
            required_cpu_m = int(workload.get("cpu", 1)) * 1000
        except (TypeError, ValueError):
            required_cpu_m = 1000
        required_mem_mi = _parse_mem_to_mib(workload.get("memory", "512Mi"))

    ue_ctx, clusters, sites = _ue_ctx, _clusters, _sites
    # A running process or VM cannot move between CPU architectures (CRIU and
    # KubeVirt restore native state), so only same-arch clusters are candidates.
    _arch = lambda c: ((c.get("meta_data") or {}).get("arch") or "amd64")
    _src = next((c for c in clusters if source_cluster in (c.get("id"), c.get("name"))), None)
    if _src is not None:
        clusters = [c for c in clusters if _arch(c) == _arch(_src)]
    sites_by_id = {s["id"]: s for s in sites}
    energy_task = asyncio.ensure_future(fetch_energy(clusters))
    nodes_list = await asyncio.gather(
        *[_get_cluster_nodes_safe(c["id"]) for c in clusters]
    )
    nodes_by_cluster = {c["id"]: n for c, n in zip(clusters, nodes_list)}
    energy = await energy_task

    candidates: list[EdgeCandidate] = []
    for cluster in clusters:
        cluster_id = cluster["id"]
        # The AMS addresses clusters by name (e.g. 'drift-edge-1'); internal
        # callers use the UUID. Match either so the source cluster is recognised
        # (only the source branch flags is_current_placement, so a miss here
        # leaves no stay-candidate and next() below raises StopIteration).
        is_source = source_cluster in (cluster_id, cluster.get("name"))
        site = sites_by_id.get(cluster.get("site_id"))
        distance_km = None
        if site and site.get("latitude") is not None and ue_ctx.ue_lat is not None:
            distance_km = _haversine_km(
                ue_ctx.ue_lat, ue_ctx.ue_lon, site["latitude"], site["longitude"]
            )
        greenness = energy.get(cluster_id, {}).get("greenness", 0.0)
        energy_pct = energy.get(cluster_id, {}).get("energy_pct", 1.0)

        nodes = nodes_by_cluster.get(cluster_id, [])

        if is_source:
            # The current placement is always a valid candidate — it's already running.
            cur = next((n for n in nodes if n["name"] == current_node), None)
            candidates.append(EdgeCandidate(
                cluster_id=cluster_id, cluster_name=cluster.get("name", cluster_id),
                node_name=current_node, is_source_cluster=True, is_current_placement=True,
                feasible=True,
                available_cpu_m=(cur or {}).get("available_cpu_m", 0),
                available_mem_mi=(cur or {}).get("available_mem_mi", 0),
                distance_km=distance_km, greenness=greenness, energy_pct=energy_pct,
            ))
            # A better node on the SAME cluster is also worth considering
            # (still an intra-cluster move DRIFT already knows how to do).
            alt_nodes = [n for n in nodes if n["name"] != current_node]
            best_alt = _best_feasible_node(alt_nodes, required_cpu_m, required_mem_mi)
            if best_alt:
                candidates.append(EdgeCandidate(
                    cluster_id=cluster_id, cluster_name=cluster.get("name", cluster_id),
                    node_name=best_alt["name"], is_source_cluster=True, is_current_placement=False,
                    feasible=True,
                    available_cpu_m=best_alt["available_cpu_m"], available_mem_mi=best_alt["available_mem_mi"],
                    distance_km=distance_km, greenness=greenness, energy_pct=energy_pct,
                ))
        else:
            best = _best_feasible_node(nodes, required_cpu_m, required_mem_mi)
            if best:
                candidates.append(EdgeCandidate(
                    cluster_id=cluster_id, cluster_name=cluster.get("name", cluster_id),
                    node_name=best["name"], is_source_cluster=False, is_current_placement=False,
                    feasible=True,
                    available_cpu_m=best["available_cpu_m"], available_mem_mi=best["available_mem_mi"],
                    distance_km=distance_km, greenness=greenness, energy_pct=energy_pct,
                ))
            else:
                candidates.append(EdgeCandidate(
                    cluster_id=cluster_id, cluster_name=cluster.get("name", cluster_id),
                    node_name="", is_source_cluster=False, is_current_placement=False,
                    feasible=False, available_cpu_m=0, available_mem_mi=0,
                    distance_km=distance_km, greenness=greenness, energy_pct=energy_pct,
                    infeasible_reason=f"no node with >= {required_cpu_m}m CPU / {required_mem_mi}Mi free",
                ))

    # Relocation is not free: penalise non-current candidates in proportion to
    # the workload's footprint (measured: UE-perceived stall grows with
    # checkpoint size). Full weight at >= 1 GiB RSS.
    move_cost = settings.migration_cost_weight * min(1.0, required_mem_mi / 1024.0)

    # ── QoS handling: the access link is a NETWORK problem, not an edge problem ──
    # Both edges sit behind the same RAN, so edge<->edge migration cannot fix a
    # degraded access link. So on predicted degradation we FIRST try QoD to
    # prioritise the UE's flow and sustain the session on the current edge (the
    # use-case "QoD extends edge operation"). QoS may trigger a *move* only when a
    # CLOSER edge shortens the UE<->edge path (mobility) — otherwise migrating
    # would disrupt the workload without improving QoS. ENERGY (greenness) remains
    # the primary reason to migrate between edges.
    qos_deg = ue_ctx.qos_degradation_expected
    qod_sustained = False
    # With a requested target the caller has already decided (and, for the
    # autopilot, already tried and verified QoD itself), so a second session
    # here would only collide with its own.
    if qos_deg and not preferred_cluster:
        granted = await request_qod(settings.ue_imsi)
        qod_sustained = granted
        if qod_sustained and ue_ctx.pqos_degraded and settings.qod_verify_delay_s > 0:
            # VERIFY the grant instead of trusting it: the numeric pQoS signal
            # raised the degradation, so it can also confirm the recovery. (The
            # CI capability verdict is not re-checked — it does not track the
            # flow fast enough to verify against.)
            await asyncio.sleep(settings.qod_verify_delay_s)
            recheck = await fetch_ue_context(vm_name)
            qod_sustained = not recheck.pqos_degraded
        if granted and not qod_sustained:
            # A session that did not restore the flow still caps it at the
            # profile's maximum, so release it rather than leave it in place.
            await release_qod(settings.ue_imsi)
        if qod_sustained:
            qos_deg = False  # flow prioritised -> QoS pressure relieved, no QoS move

    if settings.selection_model == "weighted":
        return await _decide_weighted(candidates, qos_deg, qod_sustained, move_cost, preferred_cluster)
    return _decide_constrained(candidates, qos_deg, qod_sustained, move_cost, preferred_cluster)


async def _decide_weighted(candidates, qos_deg, qod_sustained, move_cost, preferred_cluster) -> EdgeSelection:
    """Earlier model: weighted sum of proximity, greenness and energy."""
    for c in candidates:
        c.score = _score(c.distance_km, c.greenness, c.energy_pct)
        if not c.is_current_placement:
            c.score -= move_cost

    feasible = [c for c in candidates if c.feasible]
    if not feasible:
        return EdgeSelection(
            decision="no_candidates", target_cluster_id=None, target_cluster_name=None,
            target_node=None, reason="No feasible candidate (including source) had room for the VM.",
            candidates=candidates,
        )

    stay_candidate = next((c for c in feasible if c.is_current_placement), None)
    others = [c for c in feasible if not c.is_current_placement]
    best_other = max(others, key=lambda c: c.score) if others else None

    qos_proximity = bool(
        qos_deg and best_other and stay_candidate
        and best_other.distance_km is not None and stay_candidate.distance_km is not None
        and best_other.distance_km < stay_candidate.distance_km
    )
    # Energy-driven follow-me uses the calm hysteresis; QoS tightens it ONLY when a
    # closer edge would actually reduce UE latency.
    margin = settings.stay_margin_degrading if qos_proximity else settings.stay_margin_calm

    if stay_candidate is None:
        # Current placement isn't a feasible option (source cluster not active,
        # or its node has no room) — relocation is the only choice.
        chosen = best_other
    else:
        chosen = stay_candidate
        if best_other and best_other.score > stay_candidate.score * (1 + margin):
            chosen = best_other

    # An explicitly REQUESTED target (MEC-021 §6.2.4 lets the requester name one)
    # is AUTHORITATIVE when feasible: honor it even if DRIFT would otherwise keep
    # the current placement as optimal — this is what lets an operator drive the
    # reverse (or any) relocation on demand. Autonomous Optimal Edge Selection
    # (the block above) still decides when NO target is requested. A requested
    # target that isn't feasible (no room / unknown) falls through to the optimal.
    requested_honored = False
    if preferred_cluster:
        preferred = next(
            (c for c in feasible
             if preferred_cluster in (c.cluster_id, c.cluster_name) and not c.is_current_placement),
            None,
        )
        if preferred:
            chosen = preferred
            requested_honored = True

    if chosen.is_current_placement:
        decision = "stay"
        if qod_sustained:
            reason = ("UE QoS degraded → QoD prioritised the flow; session sustained on the "
                      "current edge (no migration — the access link isn't fixed by moving edges).")
        else:
            reason = "Current placement is optimal or close enough; no migration triggered."
    elif chosen.is_source_cluster:
        decision = "intra_cluster"
        reason = f"Better node '{chosen.node_name}' found on the same cluster."
    else:
        decision = "cross_cluster"
        if requested_honored:
            why = f"Requested target is cluster '{chosen.cluster_name}'"
        elif qos_proximity:
            why = (f"UE QoS degraded and cluster '{chosen.cluster_name}' is closer "
                   "(shorter UE↔edge path)")
        else:
            why = f"Greener/optimal edge is cluster '{chosen.cluster_name}'"
        reason = f"{why} (node '{chosen.node_name}'); relocating via cross-cluster live migration."

    return EdgeSelection(
        decision=decision,
        target_cluster_id=chosen.cluster_id,
        target_cluster_name=chosen.cluster_name,
        target_node=chosen.node_name or None,
        reason=reason,
        candidates=candidates,
    )


def _decide_constrained(candidates, qos_deg, qod_sustained, move_cost, preferred_cluster) -> EdgeSelection:
    """Greenness is the objective; everything else is a hard constraint.

    Constraints, per candidate:
      - resources: a node with the workload's CPU/memory free (checked above);
      - UE QoS: when the requirement is not met even after QoD, the current edge
        fails it, and since the access link is shared only a strictly closer
        edge can shorten the path and is kept;
      - service area: the UE is within `service_radius_km` of the site;
      - energy reserve: the site keeps at least `min_energy_pct` available
        (for the vehicle, the battery reserved for driving).
    Among the edges that satisfy all of them, the greenest wins; proximity and
    energy only break ties. The current placement is kept unless a feasible
    edge is greener by `green_margin` after the migration-cost penalty, or the
    current placement itself fails a constraint.
    """
    current = next((c for c in candidates if c.is_current_placement), None)
    radius = settings.service_radius_km
    for c in candidates:
        v = [c.infeasible_reason] if c.infeasible_reason else []
        if radius > 0 and c.distance_km is not None and c.distance_km > radius:
            v.append(f"UE outside service area ({c.distance_km:.3f} km > {radius:g} km)")
        if c.energy_pct < settings.min_energy_pct:
            v.append(f"energy {c.energy_pct:.0%} below reserve {settings.min_energy_pct:.0%}")
        if qos_deg:
            if c.cluster_id == (current.cluster_id if current else None):
                v.append("UE QoS requirement not met on this edge, even with QoD")
            elif not (current and c.distance_km is not None and current.distance_km is not None
                      and c.distance_km < current.distance_km):
                v.append("UE QoS not met and edge is not closer (shared access link)")
        c.violations = v
        c.feasible = not v
        c.score = c.greenness - (0.0 if c.is_current_placement else move_cost)

    feasible = [c for c in candidates if c.feasible]
    others = [c for c in feasible if not c.is_current_placement]
    # Objective first, then tie-breaks: nearer, then more energy.
    best_other = max(
        others,
        key=lambda c: (round(c.score, 6), -(c.distance_km if c.distance_km is not None else math.inf), c.energy_pct),
        default=None,
    )

    # An explicitly requested target is the caller's decision (an operator, or an
    # application that runs its own placement logic, e.g. the vehicle<->edge
    # autopilot), so the soft constraints above only annotate it. Resources stay
    # a hard gate: a target that cannot host the workload would fail the move.
    requested = None
    if preferred_cluster:
        requested = next((c for c in candidates
                          if not c.is_current_placement and not c.infeasible_reason
                          and preferred_cluster in (c.cluster_id, c.cluster_name)), None)

    if requested:
        chosen, why = requested, f"Requested target is cluster '{requested.cluster_name}'"
        if requested.violations:
            why += f" (requested despite: {'; '.join(requested.violations)})"
    elif current and current.feasible:
        if best_other and best_other.score > current.score + settings.green_margin:
            chosen = best_other
            why = (f"Cluster '{best_other.cluster_name}' is greener "
                   f"({best_other.greenness:.0%} vs {current.greenness:.0%}) and meets every constraint")
        else:
            chosen, why = current, None
    elif best_other:
        chosen = best_other
        why = (f"Current edge fails: {'; '.join(current.violations) if current else 'not available'}. "
               f"Cluster '{best_other.cluster_name}' is the greenest edge meeting every constraint")
    elif current:
        # Nothing satisfies all constraints: moving would not fix it, so stay.
        return EdgeSelection(
            decision="stay", target_cluster_id=current.cluster_id, target_cluster_name=current.cluster_name,
            target_node=current.node_name or None,
            reason=("No edge satisfies every constraint; staying on the current placement "
                    f"(current: {'; '.join(current.violations)})."),
            candidates=candidates,
        )
    else:
        return EdgeSelection(
            decision="no_candidates", target_cluster_id=None, target_cluster_name=None,
            target_node=None, reason="No candidate satisfies every constraint and the current placement is unknown.",
            candidates=candidates,
        )

    if chosen.is_current_placement:
        decision = "stay"
        reason = ("UE QoS degraded → QoD prioritised the flow; session sustained on the current edge."
                  if qod_sustained else
                  "Current placement meets every constraint and no feasible edge is greener by the margin.")
    elif chosen.is_source_cluster:
        decision = "intra_cluster"
        reason = f"{why} (node '{chosen.node_name}' on the same cluster)."
    else:
        decision = "cross_cluster"
        reason = f"{why} (node '{chosen.node_name}'); relocating via cross-cluster live migration."

    return EdgeSelection(
        decision=decision,
        target_cluster_id=chosen.cluster_id,
        target_cluster_name=chosen.cluster_name,
        target_node=chosen.node_name or None,
        reason=reason,
        candidates=candidates,
    )


def _best_feasible_node(nodes: list[dict], required_cpu_m: int, required_mem_mi: int) -> Optional[dict]:
    feasible = [
        n for n in nodes
        if n.get("available_cpu_m", 0) >= required_cpu_m and n.get("available_mem_mi", 0) >= required_mem_mi
    ]
    if not feasible:
        return None
    return max(feasible, key=lambda n: n["available_cpu_m"])
