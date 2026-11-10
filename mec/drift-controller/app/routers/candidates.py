"""
Candidate targets router — read-only edge selection, backs the MEC 021 API's
`adjacent_app_info` endpoint (query candidate targets without migrating).
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from app.modules.edge_selector import select_optimal_edge

router = APIRouter()


class CandidateOut(BaseModel):
    cluster_id: str
    cluster_name: str
    node_name: str
    is_source_cluster: bool
    is_current_placement: bool
    feasible: bool
    available_cpu_m: int
    available_mem_mi: int
    distance_km: Optional[float] = None
    greenness: float
    energy_pct: float
    score: float
    infeasible_reason: Optional[str] = None
    violations: list[str] = []


class CandidatesResponse(BaseModel):
    decision: str
    target_cluster_id: Optional[str]
    target_node: Optional[str]
    reason: str
    candidates: list[CandidateOut]


@router.get("/candidates", response_model=CandidatesResponse)
async def get_candidates(
    vm_name: str,
    source_cluster: str,
    source_namespace: str = "default",
    preferred_cluster: Optional[str] = None,
    kind: str = "vm",
) -> CandidatesResponse:
    selection = await select_optimal_edge(
        vm_name=vm_name,
        source_cluster=source_cluster,
        source_namespace=source_namespace,
        preferred_cluster=preferred_cluster,
        kind=kind,
    )
    # Always 200 — an empty/no_candidates result is a valid answer about the
    # current state of the fleet, not an error in the request itself.
    return CandidatesResponse(
        decision=selection.decision,
        target_cluster_id=selection.target_cluster_id,
        target_node=selection.target_node,
        reason=selection.reason,
        candidates=[CandidateOut(**c.__dict__) for c in selection.candidates],
    )
