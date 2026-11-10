"""
ETSI MEC 021 data models (Pydantic v2).
Based on ETSI GS MEC 021 v2.2.1 information model.
"""

from __future__ import annotations
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field
import uuid


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MobilityStatus(str, Enum):
    INTERHOST_MOVEOUT_TRIGGERED = "INTERHOST_MOVEOUT_TRIGGERED"
    INTERHOST_MOVEOUT_COMPLETED = "INTERHOST_MOVEOUT_COMPLETED"
    INTERHOST_MOVEOUT_FAILED = "INTERHOST_MOVEOUT_FAILED"


class MobilityProcedureState(str, Enum):
    INITIALIZING = "INITIALIZING"
    PREPARING = "PREPARING"
    SELECTING = "SELECTING"
    TRANSFERRING = "TRANSFERRING"
    ACTIVATING = "ACTIVATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ContextTransferState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# AMS Registration
# ---------------------------------------------------------------------------

class RegisteredAppContext(BaseModel):
    context_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    associated_app_id: str
    callback_reference: Optional[str] = None
    source_cluster: str = Field(description="Source DRIFT edge-cluster id")
    source_namespace: str = "default"
    vm_name: str = Field(description="Workload name (VMI or pod) in the source cluster")
    resource_kind: str = Field(default="vm", description="'vm' (KubeVirt) or 'pod' (CRIU)")
    # O-2: the UE this application serves. Degradation notifications initiate a
    # procedure for this AMS only when the event's UeID matches (falls back to
    # the platform-wide UE_IMSI when unset).
    ue_imsi: Optional[str] = Field(default=None, description="IMSI of the served UE")
    # What THIS application needs, as opposed to what the platform currently
    # calls degraded. Unset means "use the platform default", so an existing
    # registration keeps working unchanged. These arm the app's own pQoS
    # subscriptions, which is what lets one degradation event move the video
    # pod without also moving a batch job that does not care.
    min_dl_mbps: Optional[float] = Field(
        default=None, description="Downlink this app needs, Mbps; None = platform default")
    max_latency_ms: Optional[float] = Field(
        default=None, description="Latency ceiling this app tolerates, ms; None = platform default")
    # Who decides WHEN to relocate. True: DRIFT's pQoS triggers (notifications
    # and polling) initiate procedures on their own. False: the application's
    # own decision logic initiates every procedure through this API (the
    # vehicle<->edge autopilot does: greenness, pQoS and a verified QoD), so
    # the platform must not start competing moves, nor hold AF subscriptions.
    autonomous_trigger: bool = Field(
        default=True, description="Let pQoS degradation initiate procedures for this app")


class AppMobilityServiceInfo(BaseModel):
    """Info element for an Application Mobility Service registration."""
    app_mobility_service_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    registered_app_context: RegisteredAppContext
    service_continuity_mode: int = Field(default=1, description="1=stateful, 0=stateless")
    expiry_time: Optional[int] = None


class AppMobilityServiceRegistration(BaseModel):
    """Request body for POST /app_mobility_services."""
    registered_app_context: RegisteredAppContext
    service_continuity_mode: int = 1
    expiry_time: Optional[int] = None


# ---------------------------------------------------------------------------
# Mobility Procedures (trigger / activate / cancel)
# ---------------------------------------------------------------------------

class MobilityProcedureInitiateRequest(BaseModel):
    """Request body for initiating a relocation.

    target_cluster is an optional hint (ETSI MEC 021 §6.2.4 allows the
    requester to suggest a target). When omitted, DRIFT's Optimal Edge
    Selection chooses the target autonomously; a supplied hint is only
    honoured if its score is close enough to the optimum.
    """
    target_cluster: Optional[str] = None
    context_transfer_state: ContextTransferState = ContextTransferState.NOT_STARTED


class MobilityProcedureInfo(BaseModel):
    """Info element for a mobility procedure."""
    mobility_procedure_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    app_mobility_service_id: str
    state: MobilityProcedureState = MobilityProcedureState.INITIALIZING
    requested_target_cluster: Optional[str] = None
    target_cluster: Optional[str] = None
    target_node: Optional[str] = None
    decision: Optional[str] = Field(
        default=None, description="stay | intra_cluster | cross_cluster | no_candidates"
    )
    decision_reason: Optional[str] = None
    context_transfer_state: ContextTransferState = ContextTransferState.NOT_STARTED
    migration_latency_ms: Optional[float] = None
    error_details: Optional[str] = None


# ---------------------------------------------------------------------------
# Adjacent App Info (candidate targets)
# ---------------------------------------------------------------------------

class AdjacentAppInfo(BaseModel):
    """Info about an adjacent / candidate target app instance."""
    adjacent_app_info_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    cluster_id: str
    node_name: str
    available_resources: Optional[ResourceInfo] = None


class ResourceInfo(BaseModel):
    cpu_available: str  # e.g. "2000m"
    memory_available: str  # e.g. "4Gi"
    greenness_score: float = Field(default=0.0, ge=0.0, le=1.0,
                                   description="0=none, 1=100% green energy")


# ---------------------------------------------------------------------------
# Subscriptions (event notifications)
# ---------------------------------------------------------------------------

class SubscriptionType(str, Enum):
    MOBILITY_PROCEDURE = "MobilityProcedureSubscription"
    ADJACENT_APP = "AdjacentAppInfoSubscription"


class MobilityProcedureSubscription(BaseModel):
    """Subscribe to mobility procedure events."""
    subscription_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    callback_reference: str = Field(description="HTTP URL for event notifications")
    app_mobility_service_id: Optional[str] = None
    filter_criteria: Optional[List[MobilityStatus]] = None


class MobilityProcedureNotification(BaseModel):
    """Event notification payload sent to subscriber callback_reference."""
    subscription_id: str
    app_mobility_service_id: str
    mobility_procedure_id: str
    mobility_status: MobilityStatus
    timestamp: str
