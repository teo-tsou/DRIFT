"""
SQLAlchemy Database Models for DRIFT Platform
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer,
    String, Text, JSON, Enum as SQLEnum, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship, declarative_base
import enum

Base = declarative_base()


# Enums for status fields
class SiteStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    REGISTERING = "registering"


class ManagementClusterStatus(str, enum.Enum):
    PENDING = "pending"
    VALIDATING = "validating"
    PROVISIONING = "provisioning"
    CONFIGURING_NETWORK = "configuring_network"
    INSTALLING_ADDONS = "installing_addons"
    ACTIVE = "active"
    DEGRADED = "degraded"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class LogLevel(str, enum.Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# Database Models
class Site(Base):
    __tablename__ = "sites"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(255), unique=True, nullable=False, index=True)
    location = Column(String(255))
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    status = Column(SQLEnum(SiteStatus), nullable=False, default=SiteStatus.OFFLINE, index=True)

    kubeconfig_path = Column(Text)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    meta_data = Column(JSONB, default={})

    # Relationships
    l2_networks = relationship("L2Network", back_populates="site", cascade="all, delete-orphan")
    management_cluster = relationship("ManagementCluster", back_populates="site", uselist=False, cascade="all, delete-orphan")


class L2Network(Base):
    """L2 Network definitions for VLANs and bridges across sites/regions"""
    __tablename__ = "l2_networks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(255), nullable=False, index=True)  # e.g., "mgmt-net", "storage-net"
    site_id = Column(UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True)

    # Network Configuration
    vlan_id = Column(Integer, nullable=False)  # 1-4094
    subnet = Column(String(50), nullable=False)  # CIDR notation, e.g., "10.64.44.0/23"
    gateway = Column(String(50), nullable=False)  # Gateway IP
    bridge_name = Column(String(50), nullable=False)  # e.g., "drift-br0"
    description = Column(Text)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    meta_data = Column(JSONB, default={})

    # Relationships
    site = relationship("Site", back_populates="l2_networks")

    # Constraints: VLAN ID and name must be unique per site
    __table_args__ = (
        UniqueConstraint('site_id', 'name', name='uq_l2network_site_name'),
        UniqueConstraint('site_id', 'vlan_id', name='uq_l2network_site_vlan'),
    )


class ManagementCluster(Base):
    __tablename__ = "management_clusters"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(255), nullable=False, index=True)
    site_id = Column(UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True)

    # Cluster Configuration
    cluster_type = Column(String(50), nullable=False, default="k3s")  # k3s, rke2
    kubernetes_version = Column(String(50))
    cni_plugin = Column(String(50), default="flannel")  # flannel, calico

    kubeconfig_path = Column(Text)

    # Network Configuration
    pod_cidr = Column(String(50), default="10.42.0.0/16")
    service_cidr = Column(String(50), default="10.43.0.0/16")
    l2_networks = Column(JSONB, default={})  # Bridge/VLAN config

    # Addon Configuration
    enable_longhorn = Column(Boolean, default=True)
    enable_kubevirt = Column(Boolean, default=True)
    enable_multus = Column(Boolean, default=True)
    enable_monitoring = Column(Boolean, default=False)  # Install Prometheus metrics stack

    # Node Inventory
    nodes = Column(JSONB, nullable=False)  # Array of node definitions

    # Status Tracking
    status = Column(SQLEnum(ManagementClusterStatus), nullable=False, default=ManagementClusterStatus.PENDING, index=True)
    current_phase = Column(String(100))
    progress_percent = Column(Integer, default=0)
    error_message = Column(Text)

    # Live health data
    nodes_status = Column(JSONB, default=[])       # [{name, ip, ready, roles, cpu_pct, ram_pct}]
    addon_statuses = Column(JSONB, default={})     # {longhorn: "ok"|"degraded"|"missing", ...}

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    provisioned_at = Column(DateTime)
    deleted_at = Column(DateTime)

    # Metadata
    meta_data = Column(JSONB, default={})

    # Relationships
    site = relationship("Site", back_populates="management_cluster")


class UserEquipment(Base):
    """A UE device on the 5G access: today a traffic endpoint for the
    follow-me path (socat proxies + pQoS identity), later an extreme-edge
    host for pods."""
    __tablename__ = "user_equipments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(100), nullable=False, unique=True)
    host = Column(String(100), nullable=False)          # management IP (ssh)
    ssh_user = Column(String(50), default="pi", nullable=False)
    wwan_iface = Column(String(50), default="wwan0", nullable=False)
    wwan_gw = Column(String(50))                        # blank: dev-route only (p2p)
    imsi = Column(String(30))                           # read from the SIM, not a sheet
    notes = Column(Text)
    # active: the UE Follow-Me, pQoS and QoD use now (one at a time).
    # retired: kept for its history and selectable, but no longer deployed.
    active = Column(Boolean, default=False)
    retired = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class DeploymentLog(Base):
    __tablename__ = "deployment_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    entity_type = Column(String(50), nullable=False, index=True)  # site, intent, cluster, vm, management_cluster
    entity_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    # Log entry
    level = Column(SQLEnum(LogLevel), nullable=False, default=LogLevel.INFO)
    message = Column(Text, nullable=False)
    details = Column(JSONB, default={})

    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
