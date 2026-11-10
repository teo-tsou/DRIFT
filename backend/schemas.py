"""
Pydantic Schemas for API Request/Response Validation
"""

from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict

from models import (
    SiteStatus, LogLevel,
    ManagementClusterStatus,
)


# Base schemas with common configuration
class BaseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


# Site Schemas
class SiteBase(BaseSchema):
    name: str = Field(..., min_length=1, max_length=255)
    location: Optional[str] = Field(None, max_length=255)
    latitude: Optional[float] = Field(None)
    longitude: Optional[float] = Field(None)
    meta_data: Dict[str, Any] = Field(default_factory=dict)


class SiteCreate(SiteBase):
    pass


class SiteUpdate(BaseSchema):
    location: Optional[str] = None
    status: Optional[SiteStatus] = None
    metadata: Optional[Dict[str, Any]] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class SiteResponse(SiteBase):
    id: UUID
    status: SiteStatus
    created_at: datetime
    updated_at: datetime

class SiteListResponse(BaseSchema):
    sites: List[SiteResponse]
    total: int


# Deployment Log Schemas
class DeploymentLogResponse(BaseSchema):
    id: UUID
    entity_type: str
    entity_id: UUID
    level: LogLevel
    message: str
    details: Dict[str, Any]
    timestamp: datetime


class DeploymentLogListResponse(BaseSchema):
    logs: List[DeploymentLogResponse]
    total: int


# Health Check Schema
class HealthResponse(BaseSchema):
    status: str
    sites: Dict[str, int]
    clusters: Dict[str, int]
    timestamp: datetime


# Error Response Schema
class ErrorResponse(BaseSchema):
    error: str = Field(..., description="Error type/code")
    message: str = Field(..., description="Human-readable error message")
    details: Optional[Dict[str, Any]] = None


# Task/Job Response for Async Operations
class TaskResponse(BaseSchema):
    task_id: str = Field(..., description="Unique task identifier")
    status: str = Field(..., description="Task status: pending, running, completed, failed")
    message: str
    result: Optional[Dict[str, Any]] = None


# Management Cluster Schemas
class InterfaceMapping(BaseSchema):
    """Schema for mapping physical interfaces to L2 networks"""
    interface: str = Field(..., description="Physical interface name (e.g., 'eno1')")
    l2_network_id: UUID = Field(..., description="L2 Network ID to attach to this interface")


class NodeDefinition(BaseSchema):
    """Schema for node definition in management cluster"""
    name: str = Field(..., description="Node name/hostname")
    host: str = Field(..., description="IP address or hostname for SSH access")
    user: str = Field(..., description="SSH username")
    ssh_key_path: Optional[str] = Field(None, description="Path to SSH private key")
    roles: List[str] = Field(..., description="Node roles: ['server'] for master, ['agent'] for worker")
    interface_mappings: Optional[List[InterfaceMapping]] = Field(None, description="Interface to L2 network mappings")
    node_ip: Optional[str] = Field(None, description="Address K3s binds and advertises, if not the default-route one")


class L2NetworkDefinition(BaseSchema):
    """Schema for L2 network configuration (embedded in old API, deprecated)"""
    vlan_id: int = Field(..., ge=1, le=4094, description="VLAN ID")
    subnet: str = Field(..., description="Subnet in CIDR notation (e.g., 10.64.44.0/23)")
    gateway: str = Field(..., description="Gateway IP address")
    bridge_name: str = Field(..., description="Bridge name (e.g., drift-br0)")


# L2 Network Schemas (new separate entity)
class L2NetworkBase(BaseSchema):
    """Base schema for L2 Network"""
    name: str = Field(..., min_length=1, max_length=255, description="Network name (e.g., 'mgmt-net')")
    vlan_id: int = Field(..., ge=1, le=4094, description="VLAN ID (1-4094)")
    subnet: str = Field(..., description="Subnet in CIDR notation (e.g., 10.64.44.0/23)")
    gateway: str = Field(..., description="Gateway IP address")
    bridge_name: str = Field(..., description="Bridge name (e.g., drift-br0)")
    description: Optional[str] = Field(None, description="Network description")
    meta_data: Dict[str, Any] = Field(default_factory=dict)


class L2NetworkCreate(L2NetworkBase):
    """Schema for creating a new L2 network"""
    site_id: UUID = Field(..., description="Site ID where this network exists")


class L2NetworkUpdate(BaseSchema):
    """Schema for updating an L2 network"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    vlan_id: Optional[int] = Field(None, ge=1, le=4094)
    subnet: Optional[str] = None
    gateway: Optional[str] = None
    bridge_name: Optional[str] = None
    description: Optional[str] = None
    meta_data: Optional[Dict[str, Any]] = None


class L2NetworkResponse(L2NetworkBase):
    """Schema for L2 network response"""
    id: UUID
    site_id: UUID
    created_at: datetime
    updated_at: datetime


class L2NetworkListResponse(BaseSchema):
    """Schema for list of L2 networks"""
    networks: List[L2NetworkResponse]
    total: int


class ManagementClusterBase(BaseSchema):
    name: str = Field(..., min_length=1, max_length=255, description="Cluster name")
    cluster_type: str = Field(default="k3s", description="Cluster type: k3s or rke2")
    kubernetes_version: Optional[str] = Field(None, description="Kubernetes version (e.g., v1.31.5+k3s1)")
    cni_plugin: str = Field(default="flannel", description="CNI plugin: flannel or calico")
    pod_cidr: str = Field(default="10.42.0.0/16", description="Pod CIDR range")
    service_cidr: str = Field(default="10.43.0.0/16", description="Service CIDR range")
    enable_longhorn: bool = Field(default=True, description="Enable Longhorn storage")
    enable_kubevirt: bool = Field(default=True, description="Enable KubeVirt for VM provisioning")
    enable_multus: bool = Field(default=True, description="Enable Multus CNI for multi-network")
    enable_monitoring: bool = Field(default=False, description="Install Prometheus metrics stack")
    meta_data: Dict[str, Any] = Field(default_factory=dict)


class ManagementClusterCreate(ManagementClusterBase):
    site_id: UUID = Field(..., description="Site ID where this cluster will be deployed")
    nodes: List[NodeDefinition] = Field(..., min_length=1, description="Node definitions")
    l2_networks: Optional[Dict[str, L2NetworkDefinition]] = Field(
        None, description="Optional bridge/VLAN network config"
    )


class ManagementClusterUpdate(BaseSchema):
    enable_longhorn: Optional[bool] = None
    enable_kubevirt: Optional[bool] = None
    enable_multus: Optional[bool] = None
    enable_monitoring: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None


class ManagementClusterResponse(ManagementClusterBase):
    id: UUID
    site_id: UUID
    status: ManagementClusterStatus
    current_phase: Optional[str]
    progress_percent: int
    kubeconfig_path: Optional[str]
    nodes: List[Dict[str, Any]]
    l2_networks: Optional[Dict[str, Any]]
    error_message: Optional[str]
    # Live status fields
    nodes_status: List[Dict[str, Any]] = Field(default_factory=list)
    addon_statuses: Dict[str, str] = Field(default_factory=dict)
    # Live health of an ACTIVE cluster, probed on list: healthy | degraded |
    # unreachable (None for non-active clusters). "active" only means provisioning
    # finished; this reflects whether the API is actually reachable + nodes Ready.
    health: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    provisioned_at: Optional[datetime]
    deleted_at: Optional[datetime]


class ManagementClusterListResponse(BaseSchema):
    clusters: List[ManagementClusterResponse]
    total: int


