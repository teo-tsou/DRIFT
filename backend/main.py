"""
DRIFT Platform - FastAPI Main Application
Edge cluster orchestration for industrial/private 5G environments
"""

import asyncio
import os
import re
import subprocess
import time
from fastapi import FastAPI, Depends, HTTPException, status, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Dict, List, Optional
from uuid import UUID, uuid4
import logging

from config import settings
from database import get_db, init_db
from models import (
    Site, SiteStatus, DeploymentLog,
    ManagementCluster, ManagementClusterStatus, L2Network, UserEquipment,
)
from schemas import (
    SiteCreate, SiteUpdate, SiteResponse, SiteListResponse,
    DeploymentLogListResponse, DeploymentLogResponse,
    HealthResponse, ErrorResponse, TaskResponse,
    ManagementClusterCreate, ManagementClusterUpdate,
    ManagementClusterResponse, ManagementClusterListResponse,
    L2NetworkCreate, L2NetworkUpdate, L2NetworkResponse, L2NetworkListResponse,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO if not settings.debug else logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="DRIFT Platform",
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Startup event
@app.on_event("startup")
async def startup_event():
    """Initialize database on startup."""
    logger.info("Initializing DRIFT Platform...")
    init_db()
    # Safe migrations for new columns
    from database import engine
    with engine.connect() as conn:
        for stmt in [
            "ALTER TABLE sites ADD COLUMN IF NOT EXISTS latitude FLOAT",
            "ALTER TABLE sites ADD COLUMN IF NOT EXISTS longitude FLOAT",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()
    logger.info("Database initialized")
    asyncio.create_task(_xe_engine_loop())



@app.on_event("shutdown")
async def shutdown_event():
    pass


# Health check endpoint
@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check(db: Session = Depends(get_db)):
    """Platform health check"""
    try:
        # Count sites by status
        sites_total = db.query(Site).count()
        sites_online = db.query(Site).filter(Site.status == SiteStatus.ONLINE).count()
        sites_offline = db.query(Site).filter(Site.status == SiteStatus.OFFLINE).count()

        # Count edge clusters by status
        clusters_total = db.query(ManagementCluster).count()
        clusters_active = db.query(ManagementCluster).filter(ManagementCluster.status == ManagementClusterStatus.ACTIVE).count()
        clusters_creating = db.query(ManagementCluster).filter(ManagementCluster.status == ManagementClusterStatus.PROVISIONING).count()

        return HealthResponse(
            status="healthy",
            sites={
                "total": sites_total,
                "online": sites_online,
                "offline": sites_offline
            },
            clusters={
                "total": clusters_total,
                "active": clusters_active,
                "creating": clusters_creating
            },
            timestamp=datetime.utcnow()
        )
    except Exception as e:
        logger.exception("Health check failed")
        raise HTTPException(status_code=500, detail="Health check failed")


# ==================== Sites Endpoints ====================

@app.get(f"{settings.api_prefix}/sites", response_model=SiteListResponse, tags=["Sites"])
async def list_sites(
    status: Optional[SiteStatus] = None,
    db: Session = Depends(get_db)
):
    """List all sites, optionally filtered by status"""
    query = db.query(Site)
    if status:
        query = query.filter(Site.status == status)

    sites = query.all()
    return SiteListResponse(
        sites=[SiteResponse(**s.__dict__) for s in sites],
        total=len(sites)
    )


@app.get(f"{settings.api_prefix}/sites/{{site_id}}", response_model=SiteResponse, tags=["Sites"])
async def get_site(site_id: UUID, db: Session = Depends(get_db)):
    """Get site details by ID"""
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    return SiteResponse(**site.__dict__)


@app.post(f"{settings.api_prefix}/sites", response_model=SiteResponse, status_code=201, tags=["Sites"])
async def register_site(site_data: SiteCreate, db: Session = Depends(get_db)):
    """Register a new edge site"""
    # Check if site name already exists
    existing = db.query(Site).filter(Site.name == site_data.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Site with this name already exists")

    site = Site(
        name=site_data.name,
        location=site_data.location,
        latitude=site_data.latitude,
        longitude=site_data.longitude,
        meta_data=site_data.meta_data,
        status=SiteStatus.REGISTERING
    )

    db.add(site)
    db.commit()
    db.refresh(site)

    logger.info(f"Registered new site: {site.name}")
    return SiteResponse(**site.__dict__)


@app.put(f"{settings.api_prefix}/sites/{{site_id}}", response_model=SiteResponse, tags=["Sites"])
async def update_site(site_id: UUID, site_update: SiteUpdate, db: Session = Depends(get_db)):
    """Update site information"""
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    # Update fields
    if site_update.location is not None:
        site.location = site_update.location
    if site_update.status is not None:
        site.status = site_update.status
    if site_update.metadata is not None:
        site.meta_data = site_update.metadata
    if site_update.latitude is not None:
        site.latitude = site_update.latitude
    if site_update.longitude is not None:
        site.longitude = site_update.longitude

    db.commit()
    db.refresh(site)

    return SiteResponse(**site.__dict__)


@app.delete(f"{settings.api_prefix}/sites/{{site_id}}", status_code=204, tags=["Sites"])
async def delete_site(site_id: UUID, db: Session = Depends(get_db)):
    """Delete a site (cascade deletes clusters and VMs)"""
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    # Check if there are active edge clusters
    active_clusters = db.query(ManagementCluster).filter(
        ManagementCluster.site_id == site_id,
        ManagementCluster.status == ManagementClusterStatus.ACTIVE
    ).count()

    if active_clusters > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete site with {active_clusters} active cluster(s). Delete clusters first."
        )

    db.delete(site)
    db.commit()
    logger.info(f"Deleted site: {site.name}")


# ==================== L2 Network Endpoints ====================

@app.get(f"{settings.api_prefix}/l2-networks", response_model=L2NetworkListResponse, tags=["L2 Networks"])
async def list_l2_networks(
    site_id: Optional[UUID] = None,
    db: Session = Depends(get_db)
):
    """List all L2 networks, optionally filtered by site"""
    query = db.query(L2Network)
    if site_id:
        query = query.filter(L2Network.site_id == site_id)

    networks = query.all()
    return L2NetworkListResponse(
        networks=[L2NetworkResponse(**n.__dict__) for n in networks],
        total=len(networks)
    )


@app.get(f"{settings.api_prefix}/l2-networks/{{network_id}}", response_model=L2NetworkResponse, tags=["L2 Networks"])
async def get_l2_network(network_id: UUID, db: Session = Depends(get_db)):
    """Get a specific L2 network by ID"""
    network = db.query(L2Network).filter(L2Network.id == network_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="L2 network not found")
    return L2NetworkResponse(**network.__dict__)


@app.post(f"{settings.api_prefix}/l2-networks", response_model=L2NetworkResponse, status_code=201, tags=["L2 Networks"])
async def create_l2_network(network_data: L2NetworkCreate, db: Session = Depends(get_db)):
    """Create a new L2 network"""
    # Verify site exists
    site = db.query(Site).filter(Site.id == network_data.site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    # Check for duplicate network name in site
    existing_name = db.query(L2Network).filter(
        L2Network.site_id == network_data.site_id,
        L2Network.name == network_data.name
    ).first()
    if existing_name:
        raise HTTPException(
            status_code=409,
            detail=f"L2 network with name '{network_data.name}' already exists in this site"
        )

    # Check for duplicate VLAN ID in site
    existing_vlan = db.query(L2Network).filter(
        L2Network.site_id == network_data.site_id,
        L2Network.vlan_id == network_data.vlan_id
    ).first()
    if existing_vlan:
        raise HTTPException(
            status_code=409,
            detail=f"VLAN ID {network_data.vlan_id} already exists in this site"
        )

    network = L2Network(
        name=network_data.name,
        site_id=network_data.site_id,
        vlan_id=network_data.vlan_id,
        subnet=network_data.subnet,
        gateway=network_data.gateway,
        bridge_name=network_data.bridge_name,
        description=network_data.description,
        meta_data=network_data.meta_data
    )

    db.add(network)
    db.commit()
    db.refresh(network)

    logger.info(f"Created L2 network: {network.name} (VLAN {network.vlan_id}) in site {site.name}")
    return L2NetworkResponse(**network.__dict__)


@app.put(f"{settings.api_prefix}/l2-networks/{{network_id}}", response_model=L2NetworkResponse, tags=["L2 Networks"])
async def update_l2_network(
    network_id: UUID,
    network_data: L2NetworkUpdate,
    db: Session = Depends(get_db)
):
    """Update an L2 network"""
    network = db.query(L2Network).filter(L2Network.id == network_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="L2 network not found")

    # Update fields if provided
    update_data = network_data.model_dump(exclude_unset=True)

    # Check for conflicts if updating name or VLAN ID
    if 'name' in update_data and update_data['name'] != network.name:
        existing = db.query(L2Network).filter(
            L2Network.site_id == network.site_id,
            L2Network.name == update_data['name'],
            L2Network.id != network_id
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="Network name already exists in this site")

    if 'vlan_id' in update_data and update_data['vlan_id'] != network.vlan_id:
        existing = db.query(L2Network).filter(
            L2Network.site_id == network.site_id,
            L2Network.vlan_id == update_data['vlan_id'],
            L2Network.id != network_id
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="VLAN ID already exists in this site")

    for key, value in update_data.items():
        setattr(network, key, value)

    db.commit()
    db.refresh(network)

    logger.info(f"Updated L2 network: {network.name}")
    return L2NetworkResponse(**network.__dict__)


@app.delete(f"{settings.api_prefix}/l2-networks/{{network_id}}", status_code=204, tags=["L2 Networks"])
async def delete_l2_network(network_id: UUID, db: Session = Depends(get_db)):
    """Delete an L2 network"""
    network = db.query(L2Network).filter(L2Network.id == network_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="L2 network not found")

    # TODO: Check if network is in use by any edge clusters
    # For now, allow deletion (cascade handled by DB)

    network_name = network.name
    db.delete(network)
    db.commit()
    logger.info(f"Deleted L2 network: {network_name}")


# ==================== IPAM Endpoints ====================

# ==================== Edge Clusters Endpoints ====================

@app.post(f"{settings.api_prefix}/edge-clusters",
          response_model=ManagementClusterResponse,
          status_code=202,
          tags=["Edge Clusters"])
async def create_management_cluster(
    cluster_data: ManagementClusterCreate,
    db: Session = Depends(get_db)
):
    """
    Create a new edge cluster

    This endpoint accepts a edge cluster configuration and initiates
    the provisioning workflow asynchronously. The cluster will be created
    using Ansible playbooks in the background.
    """
    # Validate site exists
    site = db.query(Site).filter(Site.id == cluster_data.site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    # Check if site already has an active edge cluster
    existing = db.query(ManagementCluster).filter(
        ManagementCluster.site_id == cluster_data.site_id,
        ManagementCluster.status.notin_([
            ManagementClusterStatus.DELETED,
            ManagementClusterStatus.DELETING,
            ManagementClusterStatus.FAILED,
        ])
    ).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Site already has a edge cluster: {existing.name}"
        )

    # Reject duplicate pod/service CIDRs across all non-deleted clusters
    cidr_conflict = db.query(ManagementCluster).filter(
        ManagementCluster.status.notin_([
            ManagementClusterStatus.DELETED,
            ManagementClusterStatus.DELETING,
            ManagementClusterStatus.FAILED,
        ]),
        (ManagementCluster.pod_cidr == cluster_data.pod_cidr) |
        (ManagementCluster.service_cidr == cluster_data.service_cidr)
    ).first()
    if cidr_conflict:
        if cidr_conflict.pod_cidr == cluster_data.pod_cidr:
            raise HTTPException(
                status_code=400,
                detail=f"Pod CIDR {cluster_data.pod_cidr} is already used by cluster '{cidr_conflict.name}'"
            )
        raise HTTPException(
            status_code=400,
            detail=f"Service CIDR {cluster_data.service_cidr} is already used by cluster '{cidr_conflict.name}'"
        )

    # Validate nodes
    if not cluster_data.nodes:
        raise HTTPException(status_code=400, detail="At least one node is required")

    server_nodes = [n for n in cluster_data.nodes if "server" in n.roles]
    if not server_nodes:
        raise HTTPException(status_code=400, detail="At least one server node is required")

    # Create database record
    mgmt_cluster = ManagementCluster(
        name=cluster_data.name,
        site_id=cluster_data.site_id,
        cluster_type=cluster_data.cluster_type,
        kubernetes_version=cluster_data.kubernetes_version,
        cni_plugin=cluster_data.cni_plugin,
        pod_cidr=cluster_data.pod_cidr,
        service_cidr=cluster_data.service_cidr,
        nodes=[node.model_dump(mode='json') for node in cluster_data.nodes],  # Use mode='json' to serialize UUIDs as strings
        l2_networks={k: v.model_dump(mode='json') for k, v in cluster_data.l2_networks.items()} if cluster_data.l2_networks else {},
        enable_longhorn=cluster_data.enable_longhorn,
        enable_kubevirt=cluster_data.enable_kubevirt,
        enable_multus=cluster_data.enable_multus,
        enable_monitoring=cluster_data.enable_monitoring,
        status=ManagementClusterStatus.PENDING,
        current_phase="Initializing",
        progress_percent=0,
        meta_data=cluster_data.meta_data
    )
    db.add(mgmt_cluster)
    db.commit()
    db.refresh(mgmt_cluster)

    # Log creation
    log = DeploymentLog(
        entity_id=mgmt_cluster.id,
        entity_type="management_cluster",
        level="INFO",
        message="Edge cluster creation requested"
    )
    db.add(log)
    db.commit()

    # Queue async provisioning
    import asyncio
    from management_cluster_orchestrator import provision_management_cluster
    asyncio.create_task(provision_management_cluster(mgmt_cluster.id))

    logger.info(f"Edge cluster {mgmt_cluster.name} queued for provisioning")

    return ManagementClusterResponse(**mgmt_cluster.__dict__)


@app.get(f"{settings.api_prefix}/edge-clusters",
         response_model=ManagementClusterListResponse,
         tags=["Edge Clusters"])
async def list_management_clusters(
    site_id: Optional[UUID] = None,
    status: Optional[ManagementClusterStatus] = None,
    db: Session = Depends(get_db)
):
    """List edge clusters with optional filtering"""
    query = db.query(ManagementCluster).filter(
        ManagementCluster.status != ManagementClusterStatus.DELETED
    )

    if site_id:
        query = query.filter(ManagementCluster.site_id == site_id)
    if status:
        query = query.filter(ManagementCluster.status == status)

    clusters = query.order_by(ManagementCluster.created_at.desc()).all()

    # Live health probe: an ACTIVE cluster only means provisioning finished — it
    # may since have gone unreachable (API down, k3s broken) or degraded (a node
    # NotReady). Probe each active cluster's API so the list reflects reality.
    from k8s_client import _k8s_get

    def _probe(kc_path):
        try:
            nodes_raw = _k8s_get(kc_path, "/api/v1/nodes")
        except Exception:
            return None
        if not nodes_raw or "items" not in nodes_raw:
            return None  # API unreachable / no response
        return [
            {"name": (it.get("metadata") or {}).get("name"),
             "ready": {c["type"]: c["status"] for c in (it.get("status") or {}).get("conditions", [])}.get("Ready") == "True"}
            for it in nodes_raw.get("items", [])
        ]

    async def _build(c):
        resp = ManagementClusterResponse(**c.__dict__)
        if c.status == ManagementClusterStatus.ACTIVE and c.kubeconfig_path and os.path.exists(c.kubeconfig_path):
            try:
                nodes = await asyncio.wait_for(asyncio.to_thread(_probe, c.kubeconfig_path), timeout=6)
            except (asyncio.TimeoutError, Exception):
                nodes = None
            if nodes is None:
                resp.health = "unreachable"
            elif nodes and all(n["ready"] for n in nodes):
                resp.health = "healthy"
                resp.nodes_status = nodes
            else:
                resp.health = "degraded"
                resp.nodes_status = nodes or []
        return resp

    responses = await asyncio.gather(*[_build(c) for c in clusters])
    return ManagementClusterListResponse(clusters=list(responses), total=len(responses))


@app.get(f"{settings.api_prefix}/edge-clusters/{{cluster_id}}",
         response_model=ManagementClusterResponse,
         tags=["Edge Clusters"])
async def get_management_cluster(
    cluster_id: UUID,
    db: Session = Depends(get_db)
):
    """Get edge cluster details"""
    cluster = db.query(ManagementCluster).filter(
        ManagementCluster.id == cluster_id
    ).first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Edge cluster not found")

    return ManagementClusterResponse(**cluster.__dict__)


@app.get(f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/kubeconfig",
         response_class=PlainTextResponse,
         tags=["Edge Clusters"])
async def get_management_cluster_kubeconfig(
    cluster_id: UUID,
    db: Session = Depends(get_db)
):
    """Download kubeconfig for edge cluster"""
    cluster = db.query(ManagementCluster).filter(
        ManagementCluster.id == cluster_id
    ).first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Edge cluster not found")

    if cluster.status != ManagementClusterStatus.ACTIVE:
        raise HTTPException(
            status_code=400,
            detail=f"Cluster is not active (current status: {cluster.status})"
        )

    if not cluster.kubeconfig_path:
        raise HTTPException(status_code=404, detail="Kubeconfig not available")

    try:
        with open(cluster.kubeconfig_path, 'r') as f:
            kubeconfig_content = f.read()
        return PlainTextResponse(content=kubeconfig_content)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Kubeconfig file not found")


@app.patch(f"{settings.api_prefix}/edge-clusters/{{cluster_id}}",
           response_model=ManagementClusterResponse,
           tags=["Edge Clusters"])
async def update_management_cluster(
    cluster_id: UUID,
    update_data: ManagementClusterUpdate,
    db: Session = Depends(get_db)
):
    """Update edge cluster configuration"""
    cluster = db.query(ManagementCluster).filter(
        ManagementCluster.id == cluster_id
    ).first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Edge cluster not found")

    # Update fields
    update_dict = update_data.model_dump(exclude_unset=True)
    for key, value in update_dict.items():
        setattr(cluster, key, value)

    db.commit()
    db.refresh(cluster)

    return ManagementClusterResponse(**cluster.__dict__)


@app.delete(f"{settings.api_prefix}/edge-clusters/{{cluster_id}}",
            status_code=202,
            tags=["Edge Clusters"])
async def delete_management_cluster(
    cluster_id: UUID,
    db: Session = Depends(get_db)
):
    """
    Delete a edge cluster

    This will trigger the uninstall process using Ansible playbooks.
    """
    cluster = db.query(ManagementCluster).filter(
        ManagementCluster.id == cluster_id
    ).first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Edge cluster not found")

    cluster_name = cluster.name
    cluster_id_str = str(cluster.id)

    if cluster.status == ManagementClusterStatus.DELETING:
        return {
            "message": f"Edge cluster {cluster_name} teardown already in progress",
            "cluster_id": cluster_id_str
        }

    # Mark as deleting immediately so the UI reflects it
    cluster.status = ManagementClusterStatus.DELETING
    cluster.current_phase = "Deleting cluster"
    cluster.progress_percent = 0
    db.commit()

    # Queue async teardown (mirrors the provisioning pattern)
    from management_cluster_orchestrator import deprovision_management_cluster
    asyncio.create_task(deprovision_management_cluster(cluster_id))

    logger.info(f"Teardown queued for edge cluster {cluster_name}")

    return {
        "message": f"Edge cluster {cluster_name} teardown started",
        "cluster_id": cluster_id_str
    }


@app.delete(f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/force",
            status_code=200,
            tags=["Edge Clusters"])
async def force_delete_management_cluster(cluster_id: UUID, db: Session = Depends(get_db)):
    """Force-remove a cluster DB record regardless of status (e.g. stuck in 'deleting')."""
    cluster = db.query(ManagementCluster).filter(ManagementCluster.id == cluster_id).first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Edge cluster not found")
    name = cluster.name

    db.delete(cluster)
    db.commit()
    return {"message": f"Edge cluster {name} force-deleted", "cluster_id": str(cluster_id)}


@app.post(f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/teardown-progress",
          status_code=200,
          tags=["Edge Clusters"])
async def update_teardown_progress(
    cluster_id: UUID,
    payload: dict,
    db: Session = Depends(get_db)
):
    """Called by the teardown subprocess to report progress back to the UI."""
    cluster = db.query(ManagementCluster).filter(ManagementCluster.id == cluster_id).first()
    if not cluster:
        return {"ok": False}
    if "phase" in payload:
        cluster.current_phase = payload["phase"]
    if "progress" in payload:
        cluster.progress_percent = int(payload["progress"])
    db.commit()
    return {"ok": True}


# ==================== Logs Endpoints ====================

@app.get(f"{settings.api_prefix}/logs", response_model=DeploymentLogListResponse, tags=["Logs"])
async def get_logs(
    entity_type: Optional[str] = None,
    entity_id: Optional[UUID] = None,
    level: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """Get deployment logs"""
    query = db.query(DeploymentLog)

    if entity_type:
        query = query.filter(DeploymentLog.entity_type == entity_type)
    if entity_id:
        query = query.filter(DeploymentLog.entity_id == entity_id)
    if level:
        query = query.filter(DeploymentLog.level == level)

    logs = query.order_by(DeploymentLog.timestamp.desc()).limit(limit).all()

    return DeploymentLogListResponse(
        logs=[DeploymentLogResponse(**log.__dict__) for log in logs],
        total=len(logs)
    )


# ── Edge-cluster level VM endpoints ─────────────────────────────────

def _resolve_cluster(db: Session, ident: str):
    """Look up an edge cluster by UUID id OR by its unique name. The GUI/MEC
    flow addresses clusters by name (e.g. 'drift-edge-2') while internal callers
    use the UUID; comparing the UUID column to a non-UUID string otherwise
    raises a DB cast error (500). Names are unique, so this is unambiguous."""
    try:
        UUID(str(ident))
        c = db.query(ManagementCluster).filter(ManagementCluster.id == ident).first()
        if c:
            return c
    except (ValueError, AttributeError, TypeError):
        pass
    return db.query(ManagementCluster).filter(ManagementCluster.name == ident).first()


@app.get(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/all-vms",
    tags=["VMs"],
)
async def list_all_vms(cluster_id: str, db: Session = Depends(get_db)):
    """List all VirtualMachines + VMIs across every namespace on a edge cluster."""
    cluster = _resolve_cluster(db, cluster_id)
    if not cluster:
        raise HTTPException(404, "Edge cluster not found")
    if not cluster.kubeconfig_path or not os.path.exists(cluster.kubeconfig_path):
        raise HTTPException(503, "Kubeconfig not available")

    from k8s_client import _k8s_get
    kc = cluster.kubeconfig_path

    # Fetch all VMs and VMIs cluster-wide
    vm_all  = _k8s_get(kc, "/apis/kubevirt.io/v1/virtualmachines")
    vmi_all = _k8s_get(kc, "/apis/kubevirt.io/v1/virtualmachineinstances")

    vmi_map = {}
    for vmi in (vmi_all or {}).get("items", []):
        ns   = vmi["metadata"]["namespace"]
        name = vmi["metadata"]["name"]
        vmi_map[f"{ns}/{name}"] = vmi

    by_ns: dict = {}
    for vm in (vm_all or {}).get("items", []):
        meta = vm.get("metadata", {})
        ns   = meta.get("namespace", "default")
        name = meta.get("name", "")
        vmi  = vmi_map.get(f"{ns}/{name}", {})
        vmi_status = vmi.get("status", {})
        phase = vmi_status.get("phase", "Stopped")
        iface_ips = {
            iface.get("name", ""): iface.get("ipAddress", "")
            for iface in vmi_status.get("interfaces", [])
            if iface.get("ipAddress")
        }
        ip = next(iter(iface_ips.values()), "")
        node = vmi_status.get("nodeName", "")
        spec = vm.get("spec", {})
        running = spec.get("running", False)
        domain  = spec.get("template", {}).get("spec", {}).get("domain", {})
        cpu = domain.get("cpu", {}).get("cores", "—")
        mem = domain.get("resources", {}).get("requests", {}).get("memory", "—")

        # Migration progress, when a VirtualMachineInstanceMigration is active/recent.
        # Consumed by the DRIFT MEC controller to poll relocation outcomes without
        # needing its own kubeconfig/K8s access.
        mig = vmi_status.get("migrationState", {})
        migration_state = None
        if mig:
            migration_state = {
                "completed": mig.get("completed", False),
                "failed": mig.get("failed", False),
                "target_node": mig.get("targetNode", ""),
                "start_timestamp": mig.get("startTimestamp"),
                "end_timestamp": mig.get("endTimestamp"),
            }

        by_ns.setdefault(ns, []).append({
            "name": name,
            "phase": phase,
            "running": running,
            "ip": ip,
            "interfaces": iface_ips,
            "node": node,
            "cpu": cpu,
            "memory": mem,
            "migration_state": migration_state,
        })

    return {"namespaces": by_ns}


# ── Pods (containers) ─────────────────────────────────────────────────────────
# The container twin of the VM endpoints. Listing + live relocation of pods driven
# by drift-pod-migrate (CRIU post-copy + OVN multi-chassis intra-cluster / EVPN
# cross-cluster). A pod is "migration-ready" when it carries the 2-container
# holder+work pattern (a `holder` sleeper keeps the sandbox/netns+IP alive while
# `work` is checkpointed) that drift-pod-migrate requires.
def _parse_cpu_to_millicores(v) -> int:
    if not v:
        return 0
    s = str(v).strip()
    try:
        return int(float(s[:-1])) if s.endswith("m") else int(float(s) * 1000)
    except ValueError:
        return 0


def _parse_mem_to_mib(v) -> int:
    if not v:
        return 0
    s = str(v).strip()
    try:
        if s.endswith("Gi"):
            return int(float(s[:-2]) * 1024)
        if s.endswith("Mi"):
            return int(float(s[:-2]))
        if s.endswith("Ki"):
            return int(float(s[:-2]) / 1024)
        if s.endswith("G"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("M"):
            return int(float(s[:-1]))
        return int(int(s) / (1024 * 1024))   # plain bytes
    except ValueError:
        return 0


@app.get(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/all-pods",
    tags=["Pods"],
)
async def list_all_pods(cluster_id: str, include_system: bool = False,
                        name: Optional[str] = None, namespace: Optional[str] = None,
                        db: Session = Depends(get_db)):
    """List pods across every namespace on an edge cluster (workload namespaces by
    default; pass include_system=true for kube-system/kube-ovn infra too).
    With name+namespace, fetch just that pod (single-object GET) — the fast path
    the controller's workload lookup uses on the decision hot path (KPI-7)."""
    cluster = _resolve_cluster(db, cluster_id)
    if not cluster:
        raise HTTPException(404, "Edge cluster not found")
    if not cluster.kubeconfig_path or not os.path.exists(cluster.kubeconfig_path):
        raise HTTPException(503, "Kubeconfig not available")

    from k8s_client import _k8s_get
    kc = cluster.kubeconfig_path
    _SYSTEM_NS = {"kube-system", "kube-public", "kube-node-lease", "kube-ovn"}

    if name and namespace:
        one = _k8s_get(kc, f"/api/v1/namespaces/{namespace}/pods/{name}")
        pods_all = {"items": [one] if one and one.get("kind") == "Pod" else []}
    else:
        pods_all = _k8s_get(kc, "/api/v1/pods")
    agent_tok = _agent_token(kc)   # for verifying migrated targets via the node agent
    by_ns: dict = {}
    for pod in (pods_all or {}).get("items", []):
        meta = pod.get("metadata", {})
        ns = meta.get("namespace", "default")
        if not include_system and ns in _SYSTEM_NS:
            continue
        spec = pod.get("spec", {})
        status = pod.get("status", {})
        ann = meta.get("annotations", {}) or {}
        containers = [c.get("name", "") for c in spec.get("containers", [])]
        node = spec.get("nodeName", "")
        phase = status.get("phase", "Unknown")
        # migration-ready = the holder+work pattern drift-pod-migrate drives.
        migratable = "holder" in containers and "work" in containers
        migration = None

        # legacy pods may carry BOTH markers (pre-hygiene hops): the migrated-from
        # branch wins because it VERIFIES liveness instead of trusting history
        if ann.get("drift.io/migrated-to") and not ann.get("drift.io/migrated-from"):
            # Source of a completed migration — the workload was moved away.
            migration = {"role": "source", "peer_node": ann["drift.io/migrated-to"],
                         "peer_pod": ann.get("drift.io/migration-dst-pod", "")}
            migratable = False
            phase = "Migrated"
        elif ann.get("drift.io/migrated-from"):
            # Target of a migration — the workload runs under runc, which CRI-O
            # reports Terminated. Ask the runtime for the truth and report that.
            work_cid = ""
            for cs in status.get("containerStatuses", []):
                if cs.get("name") == "work":
                    term = cs.get("state", {}).get("terminated", {})
                    work_cid = (term.get("containerID", "") or "").replace("cri-o://", "")
            live = await asyncio.to_thread(_work_running, _node_internal_ip(kc, node), agent_tok, work_cid)
            migration = {"role": "target", "peer_node": ann["drift.io/migrated-from"],
                         "freeze_s": ann.get("drift.io/migration-freeze"),
                         "network": ann.get("drift.io/migration-network", ""),
                         "verified": live}
            if live is not False:   # running, or couldn't verify → treat as live
                phase = "Running"
            # a live migrated target can be migrated again (follow-me hops): the CLI
            # resolves its runc-restored container via `crictl ps -a` + runtime check.
            migratable = ("holder" in containers and "work" in containers) and (live is not False)

        # resource footprint (summed container requests) for the edge-selector's
        # feasibility gate — the pod twin of a VM's cpu/memory.
        cpu_m = mem_mi = 0
        for c in spec.get("containers", []):
            reqs = (c.get("resources", {}) or {}).get("requests", {}) or {}
            cpu_m += _parse_cpu_to_millicores(reqs.get("cpu"))
            mem_mi += _parse_mem_to_mib(reqs.get("memory"))

        by_ns.setdefault(ns, []).append({
            "name": meta.get("name", ""),
            "phase": phase,
            "ip": status.get("podIP", ""),
            "node": node,
            "containers": containers,
            "runtime_class": spec.get("runtimeClassName", ""),
            "migratable": migratable,
            "migration": migration,
            "cpu_m": cpu_m,
            "mem_mi": mem_mi,
        })
    return {"namespaces": by_ns}


# ── Pod live migration (drives drift-pod-migrate) ─────────────────────────────
# In-memory job registry, mirroring the VM cross-migration one. The STRATEGY is
# never chosen by the caller: same-cluster (node → node) → OVN multi-chassis;
# other cluster → EVPN-stretched-L2. Containers always use CRIU post-copy.
_pod_migrations: Dict[str, dict] = {}
DRIFT_POD_MIGRATE = "/app/ansible/roles/pod_migration/files/drift-pod-migrate.sh"
NODE_AGENT_PORT = "8099"


def _agent_token(kc: str) -> str:
    """Shared secret for the drift-node-agent, from its kube-system Secret."""
    from k8s_client import _k8s_get
    import base64
    s = _k8s_get(kc, "/api/v1/namespaces/kube-system/secrets/drift-node-agent")
    tok = ((s or {}).get("data", {}) or {}).get("token", "")
    try:
        return base64.b64decode(tok).decode() if tok else ""
    except Exception:
        return ""


def _agent_exec(host_ip: str, token: str, cmd: str, timeout: int = 15):
    """Run a shell command on a node's drift-node-agent. Returns (stdout, ok) where
    ok is True iff the command exited 0 (HTTP 200), False on non-zero, None on error."""
    import urllib.request, urllib.error
    try:
        req = urllib.request.Request(f"http://{host_ip}:{NODE_AGENT_PORT}/exec",
                                     data=cmd.encode(),
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace"), True
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace"), False
    except Exception:
        return "", None


def _work_running(host_ip: str, token: str, cid: str) -> Optional[bool]:
    """After a controller-driven restore the migrated `work` runs under runc/crun,
    which CRI-O no longer tracks. Ask the runtime (via the node agent) whether <cid>
    is running on <host_ip>. Returns True/False, or None if it couldn't be checked."""
    if not (host_ip and cid):
        return None
    import json as _json
    for rt, root in (("/usr/libexec/crio/runc", "/run/runc"),
                     ("/usr/local/bin/crun-drift", "/run/crun")):
        out, ok = _agent_exec(host_ip, token, f"{rt} --root {root} state {cid} 2>/dev/null", timeout=6)
        if ok and out.strip():
            try:
                return _json.loads(out).get("status") == "running"
            except Exception:
                return '"status": "running"' in out
    return None


def _node_internal_ip(kc: str, node_name: str) -> Optional[str]:
    """The host (InternalIP) of a k8s node — the address drift-pod-migrate sshes to."""
    from k8s_client import _k8s_get
    nodes = _k8s_get(kc, "/api/v1/nodes")
    for n in (nodes or {}).get("items", []):
        if n.get("metadata", {}).get("name") == node_name:
            for a in n.get("status", {}).get("addresses", []):
                if a.get("type") == "InternalIP":
                    return a.get("address")
    return None


def _kube_apiserver_host(kc_path: str) -> Optional[str]:
    """Host of the cluster API server — the control-plane node that holds kubeconfig
    (drift-pod-migrate runs kubectl/ovn-nbctl there via DRIFT_KUBE_HOST)."""
    try:
        import yaml
        from urllib.parse import urlparse
        kc = yaml.safe_load(open(kc_path))
        return urlparse(kc["clusters"][0]["cluster"]["server"]).hostname
    except Exception:
        return None


def _pod_l2_attachment(pod: dict) -> Optional[dict]:
    """The pod's macvlan (shared-L2) attachment, parsed from the multus request
    annotation. Cross-cluster follow rides this interface: its static IP is the
    floating IP that GARP moves between clusters. Returns
    {nad, ip, prefix, iface} or None if the pod has no static L2 attachment."""
    import json as _json
    ann = pod.get("metadata", {}).get("annotations", {}) or {}
    raw = (ann.get("k8s.v1.cni.cncf.io/networks") or "").strip()
    if not raw.startswith("["):
        return None  # absent, or comma-form without a static IP — not followable
    try:
        nets = _json.loads(raw)
    except ValueError:
        return None
    for n in nets:
        ips = n.get("ips") or []
        if n.get("name") and ips:
            ip, _, pfx = ips[0].partition("/")
            return {"nad": n["name"], "ip": ip, "prefix": int(pfx or 24),
                    "iface": n.get("interface") or "net1"}
    return None


BULK_STORE = "/var/lib/drift-ckpt"   # node-local checkpoint dir for --bulk-stream
FOLLOW_ANNOTATIONS = ("drift.io/follow", "drift.io/follow-host", "drift.io/follow-edge-ip")
YOLO_TARGET_CMD = "/usr/local/sbin/drift-yolo-target"


def _pod_dnat_follow(pod: dict) -> Optional[dict]:
    """Stream follow by DNAT, for a workload whose client sends to a fixed VIP
    that a node re-points (the vehicle Jetson's drift-yolo-target): the pod
    needs no shared L2 on every cluster. Annotations:
      drift.io/follow: "dnat"
      drift.io/follow-host: node-agent address of the node that holds the DNAT
      drift.io/follow-edge-ip: "<ip>/<prefix>", the macvlan floating IP the pod
        takes on clusters that do have the shared L2
    Returns {host, ip, prefix} or None."""
    ann = pod.get("metadata", {}).get("annotations", {}) or {}
    if ann.get("drift.io/follow") != "dnat" or not ann.get("drift.io/follow-host"):
        return None
    ip, _, pfx = (ann.get("drift.io/follow-edge-ip") or "").partition("/")
    if not ip:
        return None
    return {"host": ann["drift.io/follow-host"], "ip": ip, "prefix": int(pfx or 24)}


def _cluster_has_l2(cluster) -> bool:
    """A cluster provisioned with macvlan_nad_master "" has no macvlan-l2 NAD
    (the vehicle Jetson: no LAN on its 5G path)."""
    return (cluster.meta_data or {}).get("macvlan_nad_master", "eth0") != ""


def _cluster_arch(cluster) -> str:
    """CPU architecture of a cluster's nodes (meta_data.arch; amd64 when unset)."""
    return (cluster.meta_data or {}).get("arch") or "amd64"


def _require_same_arch(src, tgt):
    """A running process (CRIU) or VM (KubeVirt) carries native CPU state: it
    cannot be restored on another architecture. Refuse with a clear reason."""
    a, b = _cluster_arch(src), _cluster_arch(tgt)
    if a != b:
        raise HTTPException(409, f"Architecture mismatch: '{src.name}' is {a}, '{tgt.name}' is {b}. "
                                 f"A running workload cannot migrate between CPU architectures; "
                                 f"choose a target that is also {a}.")


def _pick_l2_dummy(serving_ip: str) -> Optional[str]:
    """A free placeholder IP for the target pod's L2 interface (replaced by the
    floating IP at restore). Drawn from a small pool on the shared L2 subnet."""
    pool = os.environ.get("DRIFT_L2_DUMMY_POOL",
                          "10.0.1.223,10.0.1.224,10.0.1.225,10.0.1.226").split(",")
    in_use = {r.get("l2_dummy") for r in _pod_migrations.values()
              if not (r.get("completed") or r.get("failed"))}
    for ip in (p.strip() for p in pool):
        if ip and ip != serving_ip and ip not in in_use:
            return ip
    return None


async def _run_pod_migration(mig_id: str, kc: str, kube_host: str, namespace: str,
                             pod_name: str, pod_ip: str, work_image: str,
                             src_host: str, dst_host: str, target_node: str,
                             target_pod: str, network: str,
                             kc_dst: Optional[str] = None,
                             l2_dummy: Optional[str] = None,
                             l2_iface: str = "net1", l2_nad: str = "macvlan-l2",
                             l2_prefix: int = 24,
                             follow: Optional[dict] = None,
                             follow_ann: Optional[dict] = None,
                             bulk: bool = False, bulk_dial: str = "src"):
    """Create the target pod (holder + work placeholder), then run drift-pod-migrate.
    network: 'multichassis' (intra) | 'evpn' (inter — target lives in ANOTHER
    cluster, reached via kc_dst; the workload's floating IP follows over the
    shared/stretched L2 on a macvlan interface) | 'dnat' (inter, target cluster
    has no shared L2: the pod keeps only its OVN IP and the client's DNAT
    follows it). With follow (see _pod_dnat_follow), the follow host's DNAT is
    re-pointed the moment the restore is live: to the target's OVN pod IP for
    'dnat', to the floating IP for 'evpn'."""
    from k8s_client import _k8s_post, _k8s_get
    reg = _pod_migrations[mig_id]
    kc_tgt = kc_dst or kc  # cluster that hosts the target pod
    try:
        reg["phase"] = "creating target"
        # A prior hop may have left a same-named pod here (the name is stable across
        # cross-cluster hops) — clear it and wait for it to be gone so the create
        # below doesn't collide with a leftover/terminating pod.
        if await asyncio.to_thread(_k8s_get, kc_tgt, f"/api/v1/namespaces/{namespace}/pods/{target_pod}"):
            await _reap_target_pod(kc_tgt, namespace, target_pod)
            for _ in range(50):
                if not await asyncio.to_thread(_k8s_get, kc_tgt, f"/api/v1/namespaces/{namespace}/pods/{target_pod}"):
                    break
                await asyncio.sleep(0.2)
        target_manifest = {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": target_pod, "namespace": namespace,
                         "labels": {"drift.io/migration-target": pod_name}},
            "spec": {"restartPolicy": "Never", "nodeName": target_node, "containers": [
                {"name": "holder", "image": "docker.io/library/python:3-slim",
                 "imagePullPolicy": "IfNotPresent", "command": ["sleep", "infinity"]},
                {"name": "work", "image": work_image,
                 "imagePullPolicy": "IfNotPresent", "command": ["sleep", "infinity"]},
            ]},
        }
        target_manifest["metadata"]["annotations"] = dict(follow_ann or {})  # next hop
        if network == "evpn":
            # The target needs its own macvlan leg on the shared L2 (with a
            # throwaway IP) — restore swaps it for the workload's floating IP.
            import json as _json
            target_manifest["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"] = _json.dumps(
                [{"name": l2_nad, "interface": l2_iface,
                  "ips": [f"{l2_dummy}/{l2_prefix}"]}])
        await asyncio.to_thread(_k8s_post, kc_tgt, f"/api/v1/namespaces/{namespace}/pods", target_manifest)
        for _ in range(45):
            t = await asyncio.to_thread(_k8s_get, kc_tgt, f"/api/v1/namespaces/{namespace}/pods/{target_pod}")
            if t and t.get("status", {}).get("phase") == "Running":
                break
            await asyncio.sleep(1)
        else:
            reg.update(phase="failed", failed=True, error="target pod did not become Running", finished_at=time.time())
            return
        follow_cmd = None
        if follow:
            dest = pod_ip if network == "evpn" else t.get("status", {}).get("podIP", "")
            mode = "edge" if network == "evpn" else "local"
            if dest:
                follow_cmd = f"{YOLO_TARGET_CMD} {mode} {dest}"
                reg["follow_cmd"] = follow_cmd

        reg["phase"] = "migrating"
        # bulk: a hop that crosses the vehicle's 5G. Measured 2026-10-20 (Nano ->
        # Orin, 45 ms RTT): post-copy froze 17.6 s writing small files into the
        # NFS store and then faulted pages in at ~34 KB/s, one RTT each, so YOLO
        # stalled for minutes. Instead: blocking, dumped to a node-local store and
        # shipped as one stream at the link's full rate.
        cmd = ["bash", DRIFT_POD_MIGRATE,
               "--src-host", src_host, "--src-pod", pod_name,
               "--dst-host", dst_host, "--dst-pod", target_pod,
               "--namespace", namespace, "--container", "work",
               "--store", BULK_STORE if bulk else "/srv/drift-ckpt"]
        if bulk:
            cmd += ["--bulk-stream", "--bulk-dial", bulk_dial]
        # Compute mode is chosen by MIGRATION SCOPE, not the caller:
        #  • intra-cluster (multichassis): POST-COPY — the lazy-page channel back to
        #    the source is node-local and reliable, so the freeze is memory-independent.
        #  • cross-cluster (evpn): BLOCKING by default — self-contained dump, no
        #    page-server dependency, rolls back TCP-preserving on failure. With
        #    DRIFT_POD_CROSS_POSTCOPY=1, POST-COPY runs cross-cluster too: validated
        #    2026-08-27 on the flat testbed (650 MB pod: freeze 3.4 s, 36 MiB in the
        #    freeze, 554 MiB drained post-resume) — requires the source node's
        #    page-server to be routable from the target node. The old "task dies on
        #    first fault" failure dates from before the inter-cluster routes existed.
        #    DRIFT_POD_ROLLBACK_SAFE=1 forces blocking everywhere.
        cross_cluster = network in ("evpn", "dnat")
        # DRIFT_POD_CROSS_POSTCOPY=1 enables post-copy ACROSS clusters too — valid
        # where the source node's page-server is routable from the target node
        # (our flat testbed; the July report measured cross-cluster post-copy at
        # ~11 MiB-in-freeze). Default stays blocking: self-contained, rollback-safe.
        cross_postcopy_ok = os.environ.get("DRIFT_POD_CROSS_POSTCOPY") == "1"
        use_post_copy = ((not cross_cluster) or cross_postcopy_ok) \
            and os.environ.get("DRIFT_POD_ROLLBACK_SAFE") != "1" and not bulk
        if use_post_copy:
            cmd.append("--post-copy")
        reg["compute_mode"] = "post-copy" if use_post_copy else ("blocking+bulk" if bulk else "blocking")
        reg["rollback_safe"] = not use_post_copy
        if network == "multichassis":
            cmd += ["--ovn-ip", pod_ip, "--ovn-multichassis"]
        elif network == "evpn":  # cross-cluster floating IP over the stretched L2
            cmd += ["--l2-ip", f"{pod_ip}/{l2_prefix}",
                    "--l2-dummy", f"{l2_dummy}/{l2_prefix}",
                    "--l2-iface", l2_iface]
        if follow_cmd:
            cmd += ["--follow-host", follow["host"], "--follow-cmd", follow_cmd,
                    # ...and aim the stream at the target at the freeze
                    "--follow-aim-cmd", follow_cmd.replace(f"{YOLO_TARGET_CMD} ", f"{YOLO_TARGET_CMD} aim ", 1),
                    # stop the input just before the freeze: the idle receiver frees
                    # its per-frame buffers, so the checkpoint is ~40% smaller
                    "--quiesce-cmd", f"{YOLO_TARGET_CMD} pause",
                    "--resume-cmd", f"{YOLO_TARGET_CMD} resume"]
        # Drive drift-pod-migrate through the per-node agent (no SSH).
        env = dict(os.environ, DRIFT_KUBE_HOST=kube_host,
                   DRIFT_NODE_AGENT_PORT=NODE_AGENT_PORT,
                   DRIFT_NODE_AGENT_TOKEN=_agent_token(kc))
        if reg.get("inject_fault") == "restore":
            env["DRIFT_TEST_FAIL_RESTORE"] = "2"
        if not use_post_copy:
            # The blocking checkpoint/restore defaults to crun-drift, but these pods
            # are runc-managed (no runtimeClassName -> CRI-O default runtime = runc,
            # state under /run/runc). Point the blocking path at runc so it finds the
            # container — crun-drift would look in /run/crun and fail "does not exist".
            env["DRIFT_CRUN"] = env.get("DRIFT_RUNC", "/usr/libexec/crio/runc")
            env["DRIFT_CRUN_ROOT"] = "/run/runc"
        proc = await asyncio.to_thread(
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        reg["output"] = out[-4000:]
        if proc.returncode == 0:
            import re
            m = re.search(r"freeze ~?([0-9.]+)s", proc.stdout or "")
            freeze = float(m.group(1)) if m else None
            reg.update(phase="completed", completed=True, freeze_s=freeze, finished_at=time.time())
            # bytes-on-the-wire, as metered by drift-pod-migrate (KPI harness input)
            m = re.search(r"freeze\+restore moved ~([0-9.]+) MiB", out)
            if m:
                reg["net_freeze_mib"] = float(m.group(1))
            m = re.search(r"page drain ~([0-9.]+) MiB in ([0-9.]+)s \(~([0-9.]+) MiB/s\); migration total ~([0-9.]+) MiB", out)
            if m:
                reg.update(net_drain_mib=float(m.group(1)), net_drain_s=float(m.group(2)),
                           net_drain_mib_s=float(m.group(3)), net_total_mib=float(m.group(4)))
            # Durable migration markers. After a controller-driven restore the work
            # process runs under runc, invisible to CRI-O (which shows the container
            # Terminated) — so k8s reports the pod as not-ready even though it is
            # live. These annotations let list_all_pods report the TRUE state: the
            # target now runs the workload; the source has been drained.
            from k8s_client import _k8s_patch
            src_node = reg.get("src_node", "")
            tgt_node = reg.get("target_node", "")
            # Annotation HYGIENE: the target inherits the source's annotations
            # (the receiver is built from the source spec), so a pod that hops
            # A->B->A would carry BOTH migrated-from and a stale migrated-to and
            # be misclassified as "Migrated" (source) on its return. Each role
            # therefore explicitly CLEARS the other role's markers (None deletes
            # the key in a JSON merge patch).
            tgt_ann = {"drift.io/migrated-from": src_node,
                       "drift.io/migrated-to": None,
                       "drift.io/migration-dst-pod": None,
                       "drift.io/migration-dst-cluster": None,
                       "drift.io/migration-src-pod": pod_name,
                       "drift.io/migration-freeze": str(freeze),
                       "drift.io/migration-network": network,
                       # the IP the workload actually serves (carried across every hop),
                       # so the NEXT migration of this pod uses it, not the pod's own IP.
                       "drift.io/serving-ip": pod_ip}
            src_ann = {"drift.io/migrated-to": tgt_node,
                       "drift.io/migrated-from": None,
                       "drift.io/migration-dst-pod": target_pod}
            if kc_dst:  # cross-cluster: record the peer site on each side
                tgt_ann["drift.io/migration-src-cluster"] = reg.get("src_cluster", "")
                src_ann["drift.io/migration-dst-cluster"] = reg.get("target_cluster", "")
                # stable identity for the NEXT hop (keeps the name from growing)
                tgt_ann["drift.io/serving-name"] = target_pod
            await asyncio.to_thread(_k8s_patch, kc_tgt,
                f"/api/v1/namespaces/{namespace}/pods/{target_pod}",
                {"metadata": {"annotations": tgt_ann}})
            await asyncio.to_thread(_k8s_patch, kc,
                f"/api/v1/namespaces/{namespace}/pods/{pod_name}",
                {"metadata": {"annotations": src_ann}})
            # The source is annotated before it is collected, so a failed GC
            # leaves a pod that is still correctly labelled as the old end.
            reg["source_gc"] = await _gc_migrated_source(
                kc, kc_tgt, namespace, pod_name, target_pod, reg)
        else:
            reg.update(phase="failed", failed=True, error=out[-2500:], finished_at=time.time())
            # drift-pod-migrate rolled the workload back onto the source (O-6);
            # surface that + reap the orphaned target pod so it doesn't linger as
            # a 1/2 husk. The source pod is intact and still serving.
            reg["rolled_back"] = "ROLLBACK OK" in out
            await _reap_target_pod(kc_tgt, namespace, target_pod)
    except Exception as e:
        reg.update(phase="failed", failed=True, error=str(e), finished_at=time.time())
        try:
            await _reap_target_pod(kc_tgt, namespace, target_pod)
        except Exception:
            pass


# Post-copy deliberately keeps the source alive: the target restores immediately
# and pulls its remaining pages lazily FROM the source, so drift-pod-migrate sets
# KEEP_SOURCE=1 and never fences it. That leaves a frozen, non-serving pod of the
# same name on the old cluster, which the NEXT migration then fails on as a name
# conflict. So collect it — but only on evidence, never on assumption.
GC_MIGRATED_SOURCES = os.getenv("DRIFT_GC_SOURCES", "1") not in ("0", "false", "no")


async def _gc_migrated_source(kc_src: str, kc_tgt: str, namespace: str,
                              src_pod: str, target_pod: str, reg: dict) -> str:
    """Delete a drained post-copy source, after proving the target owns the work.

    Two independent conditions, both required:

      * the page drain was MEASURED (`net_drain_mib` parsed from the migrator's
        own output). Deleting a source mid-drain breaks the lazy restore, and a
        reported drain is the only proof it finished. If net stats were disabled
        the figure is absent and this declines rather than guesses.
      * the target exists, is Running, and carries `drift.io/migrated-from` —
        i.e. it is the restored end, not a husk left by a rollback.
    """
    from k8s_client import _k8s_get, _k8s_delete
    if not GC_MIGRATED_SOURCES:
        return "disabled"
    if reg.get("net_drain_mib") is None:
        return "skipped: page drain not measured, cannot prove it finished"
    tgt = await asyncio.to_thread(_k8s_get, kc_tgt,
        f"/api/v1/namespaces/{namespace}/pods/{target_pod}")
    if not tgt:
        return "skipped: target pod not found"
    if (tgt.get("status") or {}).get("phase") != "Running":
        return f"skipped: target is {(tgt.get('status') or {}).get('phase')}"
    ann = (tgt.get("metadata") or {}).get("annotations") or {}
    if not ann.get("drift.io/migrated-from"):
        return "skipped: target carries no migrated-from marker"
    ok = await asyncio.to_thread(_k8s_delete, kc_src,
        f"/api/v1/namespaces/{namespace}/pods/{src_pod}?gracePeriodSeconds=0")
    return "collected" if ok else "delete failed"


async def _reap_target_pod(kc: str, namespace: str, target_pod: str):
    """Delete a failed migration's target pod (best-effort)."""
    from k8s_client import _k8s_delete
    try:
        await asyncio.to_thread(_k8s_delete, kc,
            f"/api/v1/namespaces/{namespace}/pods/{target_pod}?gracePeriodSeconds=0")
    except Exception:
        pass


async def _start_pod_migration(cluster_id, namespace, name, target_node, db, network):
    cluster = db.query(ManagementCluster).filter(ManagementCluster.id == cluster_id).first()
    if not cluster:
        raise HTTPException(404, "Edge cluster not found")
    if not cluster.kubeconfig_path or not os.path.exists(cluster.kubeconfig_path):
        raise HTTPException(503, "Kubeconfig not available")
    kc = cluster.kubeconfig_path

    from k8s_client import _k8s_get
    pod = _k8s_get(kc, f"/api/v1/namespaces/{namespace}/pods/{name}")
    if not pod:
        raise HTTPException(404, f"Pod '{name}' not found in namespace '{namespace}'")
    conts = [c.get("name") for c in pod.get("spec", {}).get("containers", [])]
    if not ("holder" in conts and "work" in conts):
        raise HTTPException(400, "Pod is not migration-ready — it needs the holder+work container pattern.")
    src_node = pod.get("spec", {}).get("nodeName", "")
    # The IP the workload SERVES follows it across hops (via LSP reuse), so it may
    # differ from this pod's own podIP if the pod is itself a prior migration target.
    ann = pod.get("metadata", {}).get("annotations", {}) or {}
    pod_ip = ann.get("drift.io/serving-ip") or pod.get("status", {}).get("podIP", "")
    if not pod_ip:
        raise HTTPException(400, "Pod has no IP yet")
    if network == "multichassis" and target_node == src_node:
        raise HTTPException(400, "Target node is the source node.")
    work_image = next((c.get("image") for c in pod["spec"]["containers"] if c.get("name") == "work"),
                      "docker.io/library/python:3-slim")

    src_host = _node_internal_ip(kc, src_node)
    dst_host = _node_internal_ip(kc, target_node)
    kube_host = _kube_apiserver_host(kc)
    if not (src_host and dst_host and kube_host):
        raise HTTPException(500, "Could not resolve node host addresses")

    mig_id = str(uuid4())
    target_pod = f"{name}-mig"
    _pod_migrations[mig_id] = {
        "id": mig_id, "kind": "pod", "pod": name, "namespace": namespace,
        "src_node": src_node, "target_node": target_node, "network": network,
        "phase": "starting", "completed": False, "failed": False,
    }
    asyncio.create_task(_run_pod_migration(
        mig_id, kc, kube_host, namespace, name, pod_ip, work_image,
        src_host, dst_host, target_node, target_pod, network))
    return {"migration_id": mig_id, "pod": name, "target_node": target_node,
            "network": network, "status_url": f"{settings.api_prefix}/pod-migrations/{mig_id}"}


@app.post(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/all-pods/{{namespace}}/{{name}}/migrate",
    tags=["Pods"], status_code=202,
)
async def migrate_pod(cluster_id: str, namespace: str, name: str, payload: dict, db: Session = Depends(get_db)):
    """In-cluster live migration (node → node): CRIU post-copy + OVN multi-chassis."""
    target_node = (payload.get("target_node") or "").strip()
    if not target_node:
        raise HTTPException(400, "target_node is required")
    return await _start_pod_migration(cluster_id, namespace, name, target_node, db, "multichassis")


async def _start_cross_pod_migration(cluster_id, target_cluster_id, namespace, name,
                                     target_node, db, inject_fault=None):
    """Cross-cluster pod migration: the target pod is created in the PEER cluster
    (via its own kubeconfig) with a macvlan leg on the shared L2, then
    drift-pod-migrate carries the workload + its floating IP across."""
    src_cluster = _resolve_cluster(db, cluster_id)
    tgt_cluster = _resolve_cluster(db, target_cluster_id)
    if not src_cluster or not tgt_cluster:
        raise HTTPException(404, "Edge cluster not found")
    for c in (src_cluster, tgt_cluster):
        if not c.kubeconfig_path or not os.path.exists(c.kubeconfig_path):
            raise HTTPException(503, f"Kubeconfig not available for cluster '{c.name}'")
    _require_same_arch(src_cluster, tgt_cluster)
    kc, kc_dst = src_cluster.kubeconfig_path, tgt_cluster.kubeconfig_path

    from k8s_client import _k8s_get
    # API calls off the event loop: the vehicle's apiserver is over 5G, and a
    # blocked loop stalls every stream the backend proxies (the in-car view).
    pod = await asyncio.to_thread(_k8s_get, kc, f"/api/v1/namespaces/{namespace}/pods/{name}")
    if not pod:
        raise HTTPException(404, f"Pod '{name}' not found in namespace '{namespace}'")
    conts = [c.get("name") for c in pod.get("spec", {}).get("containers", [])]
    if not ("holder" in conts and "work" in conts):
        raise HTTPException(400, "Pod is not migration-ready — it needs the holder+work container pattern.")
    src_node = pod.get("spec", {}).get("nodeName", "")

    # Cross-cluster follow rides the pod's macvlan leg on the shared L2 — the
    # OVN podIP is cluster-local and cannot move. No L2 attachment, no follow —
    # unless the pod follows by DNAT, where only clusters WITH the shared L2
    # give it the floating IP.
    ann = pod.get("metadata", {}).get("annotations", {}) or {}
    follow = _pod_dnat_follow(pod)
    l2 = _pod_l2_attachment(pod)
    if follow:
        l2 = {"nad": "macvlan-l2", "ip": follow["ip"], "prefix": follow["prefix"],
              "iface": "net1"} if _cluster_has_l2(tgt_cluster) else None
        pod_ip = follow["ip"]
    elif not l2:
        raise HTTPException(400,
            "Pod has no static macvlan (shared-L2) attachment — cross-cluster follow "
            "carries that floating IP. Attach the macvlan-l2 network with a static IP.")
    else:
        pod_ip = ann.get("drift.io/serving-ip") or l2["ip"]
    work_image = next((c.get("image") for c in pod["spec"]["containers"] if c.get("name") == "work"),
                      "docker.io/library/python:3-slim")

    # Destination node: caller's choice, else the peer cluster's first Ready node.
    if not target_node:
        nodes = await asyncio.to_thread(_k8s_get, kc_dst, "/api/v1/nodes") or {}
        for n in nodes.get("items", []):
            ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in n.get("status", {}).get("conditions", []))
            if ready:
                target_node = n.get("metadata", {}).get("name", "")
                break
    if not target_node:
        raise HTTPException(503, f"No Ready node in target cluster '{tgt_cluster.name}'")

    src_host, dst_host, kube_host = await asyncio.gather(
        asyncio.to_thread(_node_internal_ip, kc, src_node),
        asyncio.to_thread(_node_internal_ip, kc_dst, target_node),
        asyncio.to_thread(_kube_apiserver_host, kc))
    if not (src_host and dst_host and kube_host):
        raise HTTPException(500, "Could not resolve node host addresses")

    dummy = None
    if l2:
        dummy = _pick_l2_dummy(pod_ip)
        if not dummy:
            raise HTTPException(503, "No free L2 placeholder IP (DRIFT_L2_DUMMY_POOL exhausted)")
    network = "evpn" if l2 else "dnat"
    # A cluster without the shared L2 sits behind a WAN (the vehicle's 5G).
    bulk = not (_cluster_has_l2(src_cluster) and _cluster_has_l2(tgt_cluster))
    # The vehicle's end opens the stream, so it enters the WireGuard hub through
    # the vehicle's tunnel only (see drift-pod-migrate.sh --bulk-dial).
    bulk_dial = "dst" if bulk and not _cluster_has_l2(tgt_cluster) else "src"

    mig_id = str(uuid4())
    # Stable identity across cross-cluster hops: the target lives in a DIFFERENT
    # cluster, so it reuses the workload's name instead of accreting a '-mig'
    # suffix each hop (my-app -> my-app-mig -> my-app-mig-mig ...). The name is
    # carried forward via drift.io/serving-name; legacy pods without it are
    # normalised by stripping any trailing '-mig'.
    stable_name = ann.get("drift.io/serving-name")
    if not stable_name:
        stable_name = name
        while stable_name.endswith("-mig"):
            stable_name = stable_name[:-4]
        stable_name = stable_name or name
    target_pod = stable_name
    _pod_migrations[mig_id] = {
        "id": mig_id, "kind": "pod", "pod": name, "namespace": namespace,
        "src_node": src_node, "target_node": target_node, "network": network,
        "src_cluster": src_cluster.name, "target_cluster": tgt_cluster.name,
        "l2_dummy": dummy, "follow": follow, "started_at": time.time(),
        "phase": "starting", "completed": False, "failed": False,
        "inject_fault": inject_fault if inject_fault == "restore" else None,
    }
    asyncio.create_task(_run_pod_migration(
        mig_id, kc, kube_host, namespace, name, pod_ip, work_image,
        src_host, dst_host, target_node, target_pod, network,
        kc_dst=kc_dst, l2_dummy=dummy,
        l2_iface=l2["iface"] if l2 else "net1", l2_nad=l2["nad"] if l2 else "macvlan-l2",
        l2_prefix=l2["prefix"] if l2 else 24,
        follow=follow,
        follow_ann={k: ann[k] for k in FOLLOW_ANNOTATIONS if k in ann} if follow else None,
        bulk=bulk, bulk_dial=bulk_dial))
    return {"migration_id": mig_id, "pod": name, "target_node": target_node,
            "target_cluster": tgt_cluster.name, "network": network,
            "status_url": f"{settings.api_prefix}/pod-migrations/{mig_id}"}


@app.post(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/all-pods/{{namespace}}/{{name}}/cross-migrate",
    tags=["Pods"], status_code=202,
)
async def cross_migrate_pod(cluster_id: str, namespace: str, name: str, payload: dict, db: Session = Depends(get_db)):
    """Cross-cluster live migration: CRIU post-copy + shared-L2 floating-IP follow."""
    target_cluster_id = (payload.get("target_cluster_id") or "").strip()
    if not target_cluster_id:
        raise HTTPException(400, "target_cluster_id is required")
    if target_cluster_id == cluster_id:
        raise HTTPException(400, "Target cluster is the source cluster")
    target_node = (payload.get("target_node") or "").strip()
    # inject_fault "restore": the target restore fails on purpose (KPI-4 rollback harness)
    return await _start_cross_pod_migration(cluster_id, target_cluster_id, namespace,
                                            name, target_node, db,
                                            inject_fault=payload.get("inject_fault"))


@app.get(f"{settings.api_prefix}/pod-migrations", tags=["Pods"])
async def list_pod_migrations():
    """All pod-migration attempts (most recent first) with full output/error —
    for debugging a failed migration without needing its id."""
    return list(reversed(list(_pod_migrations.values())))


# ── Extreme edge: the vehicle Jetson <-> ARM edge view ─────────────────────────
# The YOLO receiver runs either on the vehicle (its camera's own board) or on the
# edge; the vehicle's drift-yolo-target DNAT says which, and YOLO's /health is
# only reachable from the vehicle's host (its pod IP, or the edge floating IP over
# 5G). So one node-agent call on the vehicle reads all of it.
XE_VEHICLE = os.environ.get("DRIFT_XE_VEHICLE_CLUSTER", "drift-vehicle")
XE_EDGE = os.environ.get("DRIFT_XE_EDGE_CLUSTER", "drift-edge-agx")
XE_POD = os.environ.get("DRIFT_XE_POD", "emt-yolo")
_XE_PROBE = r"""
# during a migration the stream is aimed at the new home (.aim) before the final
# switch confirms it: report where the frames really go, and that YOLO's health
S=/run/drift-yolo-target; [ -f $S.aim ] && S=$S.aim
echo "target=$(cat $S 2>/dev/null || echo off)"
[ -f /run/drift-yolo-target.paused ] && echo "paused=1"
T=$(cut -d' ' -f2 $S 2>/dev/null)
[ -n "$T" ] && echo "health=$(curl -s --max-time 3 http://$T:9000/health)"
# round trip from the vehicle to wherever YOLO runs: on board, or over 5G
[ -n "$T" ] && echo "rtt=$(ping -c1 -W1 -q $T 2>/dev/null | awk -F/ '/^rtt|^round-trip/{print $5}')"
echo "ue=$(ip -4 -o addr show wwan0 2>/dev/null | awk '{print $4}')"
echo "modem=$(systemctl is-active drift-quectel-cm 2>/dev/null)"
echo "bulk=$(cat /etc/drift-bulk-path 2>/dev/null || echo tunnel)"
echo "wg=$(wg show drift5g latest-handshakes 2>/dev/null | awk '{print $2}' | sort -n | tail -1)"
echo "base=$(cat /var/lib/drift-ckpt/.base-""" + XE_POD + r""" 2>/dev/null)"
echo "scene=$(readlink /srv/emt/scene 2>/dev/null | sed 's|scenes/||')"
echo "wwan=$(cat /sys/class/net/wwan0/statistics/tx_bytes /sys/class/net/wwan0/statistics/rx_bytes 2>/dev/null | tr '\n' ' ')"
# drift-rtt-probe's latest window to the edge over 5G: <time> <p90 ms> <median ms> <loss %>
echo "rttw=$(cat /run/drift-rtt 2>/dev/null)"
# emulated uplink congestion (POST /extreme-edge/congestion): the shaped rate, if on
echo "congest=$(tc class show dev wwan0 2>/dev/null | awk '/class htb 1:20 /{for(i=1;i<=NF;i++) if($i=="rate") print $(i+1)}')"
"""


@app.get(f"{settings.api_prefix}/extreme-edge/state", tags=["Extreme Edge"])
async def extreme_edge_state(db: Session = Depends(get_db)):
    """Where the vehicle's YOLO runs, its live counters, the vehicle's 5G link and
    the recent vehicle <-> edge migrations (freeze, bytes on the wire, frames lost)."""
    import json as _json, re as _re
    from k8s_client import _k8s_get
    veh, edge = _resolve_cluster(db, XE_VEHICLE), _resolve_cluster(db, XE_EDGE)
    if not veh or not edge:
        raise HTTPException(404, f"Clusters '{XE_VEHICLE}' / '{XE_EDGE}' not found")
    out = {"vehicle": {"cluster_id": str(veh.id), "name": veh.name},
           "edge": {"cluster_id": str(edge.id), "name": edge.name},
           "pod": XE_POD, "serving": "unknown", "now": time.time()}
    kc = veh.kubeconfig_path
    if kc and os.path.exists(kc):
        nodes = await asyncio.to_thread(_k8s_get, kc, "/api/v1/nodes") or {}
        node = next(iter(nodes.get("items", [])), {})
        host = next((a["address"] for a in node.get("status", {}).get("addresses", [])
                     if a.get("type") == "InternalIP"), None)
        out["vehicle"]["node"] = node.get("metadata", {}).get("name")
        out["vehicle"]["host"] = host
        if host:
            token = await asyncio.to_thread(_agent_token, kc)
            raw, ok = await asyncio.to_thread(_agent_exec, host, token, _XE_PROBE, 8)
            out["vehicle"]["reachable"] = ok is not None
            kv = dict(l.split("=", 1) for l in (raw or "").splitlines() if "=" in l)
            mode, _, ip = kv.get("target", "off").partition(" ")
            out["target"] = {"mode": mode, "ip": ip, "paused": kv.get("paused") == "1"}
            out["serving"] = {"local": "vehicle", "edge": "edge"}.get(mode, mode)
            try:
                out["health"] = _json.loads(kv.get("health") or "null")
            except ValueError:
                out["health"] = None
            try:
                out["rtt_ms"] = float(kv.get("rtt") or "")
            except ValueError:
                out["rtt_ms"] = None
            w5 = (kv.get("rttw") or "").split()
            try:
                out["rtt_window"] = {"t": float(w5[0]), "p90_ms": None if w5[1] == "-" else float(w5[1]),
                                     "median_ms": None if w5[2] == "-" else float(w5[2]),
                                     "loss_pct": float(w5[3])} if len(w5) == 4 else None
            except ValueError:
                out["rtt_window"] = None
            hs = kv.get("wg") or ""
            out["scene"] = kv.get("scene") or None
            out["congestion"] = kv.get("congest") or None
            w = (kv.get("wwan") or "").split()
            out["wwan"] = {"tx_bytes": int(w[0]), "rx_bytes": int(w[1])} if len(w) == 2 else None
            out["link"] = {"ue_ip": kv.get("ue", ""), "modem": kv.get("modem", ""),
                           "bulk_path": kv.get("bulk", ""), "base": kv.get("base", ""),
                           "tunnel_handshake_age_s": round(time.time() - int(hs)) if hs.isdigit() and int(hs) else None}
    out["energy"] = await asyncio.to_thread(_xe_energy, str(edge.id))
    gaps = (out.get("health") or {}).get("gaps") or []
    names = {veh.name: "vehicle", edge.name: "edge"}
    migs = []
    for r in reversed(list(_pod_migrations.values())):
        if r.get("pod") != XE_POD or r.get("src_cluster") not in names or r.get("target_cluster") not in names:
            continue
        log = r.get("output") or r.get("error") or ""
        m = _re.search(r"bulk \((\w+) dials, ([^)]*)\): ([0-9.]+) MiB of image in ([0-9.]+)s", log)
        w = _re.search(r"([0-9.]+) MiB on the wire", log)
        t0, t1 = r.get("started_at"), r.get("finished_at")
        # YOLO records a stall when the next frame is processed: match it by start
        stall = max((g[1] for g in gaps if t0 and g[0] >= t0 - 5 and (not t1 or g[0] <= t1)), default=None)
        migs.append({
            "id": r.get("id"), "from": names[r["src_cluster"]], "to": names[r["target_cluster"]],
            "phase": r.get("phase"), "completed": r.get("completed"), "failed": r.get("failed"),
            "mode": r.get("compute_mode"), "freeze_s": r.get("freeze_s"),
            "started_at": t0, "finished_at": t1,
            "transfer": ("delta" if m and m.group(2).startswith("delta") else "full") if m else None,
            "image_mib": float(m.group(3)) if m else None, "transfer_s": float(m.group(4)) if m else None,
            "wire_mib": float(w.group(1)) if w else None,
            "stall_s": stall, "frames_lost": max(0, round(stall * 12) - 1) if stall else None,
            "error": (r.get("error") or "")[-300:] if r.get("failed") else None,
        })
        if len(migs) >= 20:
            break
    out["migrations"] = migs
    out["in_flight"] = any(not (m["completed"] or m["failed"]) for m in migs) or _xe_mec["pending"]
    out["mec"] = await _xe_mec_view()
    return out


# ── Scenario clock and energy (the day scenario: Morning / Noon / Afternoon /
# Evening). Edge greenness comes from a REPRODUCIBLE day profile by default, or
# from ECB's live PV forecast for the same hour (getGreenness takes any hour).
# The vehicle's numbers are the EV battery: its level and the green share of
# the energy stored at its last charge, from ECB's getEVGreenness (real Tesla
# data) by default, or the configured values (vehicle_source "configured").
_ATHENS_TZ = "Europe/Athens"
_xe_scn = {"mode": "fixed", "hour": 7.0, "edge_source": "profile",   # the page picks fixed times only
           "vehicle_source": "ecb", "battery_pct": 68.0, "battery_green_pct": 75.0}


def _xe_load_snapshot(path: str) -> None:
    """Start on the demo day's frozen ECB forecast (06/10, the representative PV
    day) instead of the profile, so a reload or restart no longer needs it
    re-POSTed. Missing or bad file: keep the profile."""
    import json as _json
    try:
        with open(path) as f:
            g = _json.load(f)["greenness"]
        if len(g) == 24:
            _xe_scn.update(edge_snapshot=[max(0.0, min(1.0, float(v))) for v in g], edge_source="snapshot")
    except Exception as ex:
        logger.info("no extreme-edge snapshot loaded from %s: %s", path, ex)


_xe_load_snapshot(os.environ.get("DRIFT_XE_EDGE_SNAPSHOT",
                                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "xe_edge_snapshot.json")))
_xe_ecb_cache: Dict[tuple, tuple] = {}
XE_EV_CAPACITY_KWH = float(os.environ.get("DRIFT_XE_EV_CAPACITY_KWH", "57.5"))   # Tesla Model 3, usable


def _xe_hour() -> float:
    if _xe_scn["mode"] == "fixed":
        return float(_xe_scn["hour"])
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(_ATHENS_TZ))
    return now.hour + now.minute / 60.0


def _xe_edge_profile(h: float) -> float:
    """Reproducible PV greenness of the edge site over the day (0-1): brown
    until mid-morning, a ramp to full solar by late morning, green through the
    early afternoon, a ramp down to brown by sunset."""
    pts = [(0, 0), (9.5, 0), (11.5, 1), (15.5, 1), (17.5, 0), (24, 0)]
    for (h0, g0), (h1, g1) in zip(pts, pts[1:]):
        if h0 <= h <= h1:
            return g0 + (g1 - g0) * (h - h0) / (h1 - h0) if h1 > h0 else g1
    return 0.0


def _xe_edge_ecb(h: float):
    """ECB greenness for the edge (agx-orin, yolo26s, 13.7 W) at hour h, or None."""
    url, user, pw = os.getenv("ECB_API_URL"), os.getenv("ECB_API_USER"), os.getenv("ECB_API_PASSWORD")
    if not url:
        return None
    hh = int(h) % 24
    key = (hh, datetime.utcnow().date())
    hit = _xe_ecb_cache.get(key)
    if hit and time.time() - hit[1] < 300:
        return hit[0]
    import base64, json as _json, urllib.parse, urllib.request
    q = urllib.parse.urlencode({"start_HH": hh, "end_HH": min(24, hh + 1), "workload_energy_kwh": 0.0137,
                                "edge_location": "agx-orin", "model": "yolo26s"})
    req = urllib.request.Request(f"{url}/getGreenness?{q}", headers={
        "Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            g = float(_json.load(r)["greenness_percentage"]) / 100.0
    except Exception:
        return None
    _xe_ecb_cache[key] = (g, time.time())
    return g


def _xe_vehicle_ecb():
    """(greenness 0-1, residual kWh, date) of the EV battery from ECB, or None.
    A date reports the previous day, so fall back a day while a date has no reading."""
    url, user, pw = os.getenv("ECB_API_URL"), os.getenv("ECB_API_USER"), os.getenv("ECB_API_PASSWORD")
    if not url:
        return None
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo(_ATHENS_TZ)).date()
    hit = _xe_ecb_cache.get(("ev", today))
    if hit and time.time() - hit[1] < 300:
        return hit[0]
    import base64, json as _json, urllib.request
    auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    for back in range(3):
        day = today.fromordinal(today.toordinal() - back).isoformat()
        req = urllib.request.Request(f"{url}/v1/getEVGreenness?date={day}", headers={"Authorization": auth})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                d = _json.load(r)
        except urllib.error.HTTPError:
            continue        # 422 right after midnight: that Greek day has no data yet
        except Exception:
            return None
        g, kwh = d.get("EV_greenness_percentage"), d.get("EV_residual_energy_kwh")
        if g is not None and kwh is not None:
            val = (float(g) / 100.0, float(kwh), day)
            _xe_ecb_cache[("ev", today)] = (val, time.time())
            return val
    return None


def _xe_vehicle_energy() -> dict:
    if _xe_scn["vehicle_source"] == "ecb":
        ev = _xe_vehicle_ecb()
        if ev is not None:
            g, kwh, day = ev
            return {"greenness": round(g, 3), "battery_pct": round(min(100.0, 100.0 * kwh / XE_EV_CAPACITY_KWH), 1),
                    "residual_kwh": round(kwh, 1), "date": day, "source": "ECB (Tesla)"}
    src = "configured" if _xe_scn["vehicle_source"] == "configured" else "configured (ECB unavailable)"
    return {"greenness": _xe_scn["battery_green_pct"] / 100.0, "battery_pct": _xe_scn["battery_pct"], "source": src}


def _xe_energy(edge_id: str) -> dict:
    h = _xe_hour()
    src = _xe_scn["edge_source"]
    snap = _xe_scn.get("edge_snapshot")
    if src == "snapshot" and snap:          # one forecast frozen for a whole run (day_scenario.py)
        g = float(snap[int(h) % 24])
    else:
        g = _xe_edge_ecb(h) if src == "ecb" else None
    if g is None:
        g, src = _xe_edge_profile(h), ("profile" if src == "profile" else "profile (ECB unavailable)")
    tel = _svc_get(f"{ENERGY_TELEMETRY_URL}/energy?ids={edge_id}") or {}
    node = (tel.get("nodes") or {}).get(edge_id) or {}
    return {"hour": round(h, 2), "mode": _xe_scn["mode"],
            "edge": {"greenness": round(g, 3), "source": src, "energy_pct": node.get("energy_pct")},
            "vehicle": _xe_vehicle_energy()}


@app.get(f"{settings.api_prefix}/extreme-edge/scenario", tags=["Extreme Edge"])
async def extreme_edge_get_scenario():
    return dict(_xe_scn, effective_hour=round(_xe_hour(), 2))


@app.post(f"{settings.api_prefix}/extreme-edge/scenario", tags=["Extreme Edge"])
async def extreme_edge_set_scenario(payload: dict):
    """mode live|fixed, hour (0-24, with fixed), edge_source profile|ecb|snapshot,
    edge_snapshot (24 hourly greenness values, 0-1, for snapshot), vehicle_source
    ecb|configured, battery_pct, battery_green_pct (configured)."""
    if payload.get("mode") in ("live", "fixed"):
        _xe_scn["mode"] = payload["mode"]
    if payload.get("hour") is not None:
        _xe_scn["hour"] = max(0.0, min(23.99, float(payload["hour"])))
        _xe_scn["mode"] = payload.get("mode", "fixed")
    if isinstance(payload.get("edge_snapshot"), list) and len(payload["edge_snapshot"]) == 24:
        _xe_scn["edge_snapshot"] = [max(0.0, min(1.0, float(v))) for v in payload["edge_snapshot"]]
    if payload.get("edge_source") in ("profile", "ecb", "snapshot"):
        _xe_scn["edge_source"] = payload["edge_source"]
    if payload.get("vehicle_source") in ("ecb", "configured"):
        _xe_scn["vehicle_source"] = payload["vehicle_source"]
    for k in ("battery_pct", "battery_green_pct"):
        if payload.get(k) is not None:
            _xe_scn[k] = max(0.0, min(100.0, float(payload[k])))
    return dict(_xe_scn, effective_hour=round(_xe_hour(), 2))


# pQoS and QoD for the VEHICLE UE. The drift-controller's pQoS/QoD are bound to
# one global UE (the x86 Follow-Me demo's), so this page asks the ENVELOPE pQoS
# AF and the CAMARA QoD API directly, for its own UE, with its own thresholds.
XE_IMSI = os.environ.get("DRIFT_XE_IMSI", "999700000000020")
XE_PQOS_URL = os.environ.get("DRIFT_XE_PQOS_URL", "http://10.220.2.49:8001")
XE_QOD_URL = os.environ.get("DRIFT_XE_QOD_URL", "http://10.220.2.50:8584/quality-on-demand/v1")
XE_EDGE_IP = os.environ.get("DRIFT_XE_EDGE_IP", "10.220.2.244")      # YOLO's floating IP on the edge
XE_UE_IP = os.environ.get("DRIFT_XE_UE_IP", "10.45.0.45")
# throughput on H-PQOS-I (capacity), latency on SIMPLE (the methods DRIFT adopted)
_XE_PQOS_METRICS = {"dl_mbps": ("H-PQOS-I", "THROUGHPUT_DL"), "ul_mbps": ("H-PQOS-I", "THROUGHPUT_UL"),
                    "latency_ms": ("SIMPLE-QS-PQOS-I", "E2E_LATENCY")}
_xe_thresholds = {"dl_min_mbps": 4.0, "ul_min_mbps": 2.0, "lat_max_ms": 400.0}


# Every call DRIFT makes to the ENVELOPE pQoS AF and the CAMARA QoD API for the
# vehicle, request and answer as sent and received, for the page and the demo's
# terminal (GET /extreme-edge/api-log). Cleared with the autopilot's log.
from collections import deque
import itertools
_xe_api_log: deque = deque(maxlen=300)
_xe_api_seq = itertools.count(1)
_xe_api_epoch = {"n": 0}          # bumped on every clear, so clients reset their view


def _xe_http(method: str, url: str, body=None, timeout: float = 10):
    t0 = time.time()
    st, d = _xe_http_raw(method, url, body, timeout)
    api = "CAMARA QoD" if url.startswith(XE_QOD_URL) else "ENVELOPE pQoS" if url.startswith(XE_PQOS_URL) else "other"
    base = XE_QOD_URL if api == "CAMARA QoD" else XE_PQOS_URL if api == "ENVELOPE pQoS" else ""
    _xe_api_log.append({"seq": next(_xe_api_seq), "t": t0, "api": api, "method": method,
                        "url": url, "path": url[len(base):] or "/", "request": body, "status": st,
                        "ms": round((time.time() - t0) * 1000), "response": d})
    return st, d


def _xe_http_raw(method: str, url: str, body=None, timeout: float = 10):
    import json as _json, urllib.request, urllib.error
    req = urllib.request.Request(url, method=method, data=_json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "null"
            return r.status, _json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:300]
        try:
            return e.code, _json.loads(raw)
        except ValueError:
            return e.code, raw
    except Exception as e:
        return None, str(e)


@app.get(f"{settings.api_prefix}/extreme-edge/api-log", tags=["Extreme Edge"])
async def extreme_edge_api_log(after: int = 0, limit: int = 100):
    """pQoS and QoD API calls after sequence number `after`, oldest first."""
    rows = [r for r in _xe_api_log if r["seq"] > after]
    return {"epoch": _xe_api_epoch["n"], "rows": rows[-max(1, min(limit, 300)):]}


def _xe_pqos_one(method: str, qos_type: str):
    st, d = _xe_http("POST", f"{XE_PQOS_URL}/pqos_inf/req",
                     {"Method": method, "QosType": qos_type, "ApplicationServerAddress": XE_EDGE_IP,
                      "Horizon": 10, "ApplicationFilterID": "DRIFT"}, timeout=12)
    if st not in (200, 201) or not isinstance(d, dict):
        import json as _json
        try:
            detail = d.get("detail") if isinstance(d, dict) else \
                _json.loads(d).get("detail") if isinstance(d, str) else None
        except ValueError:
            detail = None
        return None, f"pQoS AF: {detail or d}" if st == 404 else f"pQoS AF answered {st}"
    ues = [r.get("UeID") for r in d.get("Report", [])]
    for r in d.get("Report", []):
        if str(r.get("UeID")) == XE_IMSI and r.get("QosValue"):
            v = r["QosValue"]
            return sum(v) / len(v), None
    return None, f"no prediction for {XE_IMSI} ({len(ues)} UE(s) in the report)"


@app.get(f"{settings.api_prefix}/extreme-edge/pqos", tags=["Extreme Edge"])
async def extreme_edge_pqos():
    """pQoS predictions for the vehicle UE, judged against this page's thresholds."""
    keys = list(_XE_PQOS_METRICS)
    res = await asyncio.gather(*(asyncio.to_thread(_xe_pqos_one, *_XE_PQOS_METRICS[k]) for k in keys))
    t = _xe_thresholds
    out = {"imsi": XE_IMSI, "thresholds": dict(t), "metrics": {}}
    for k, (v, why) in zip(keys, res):
        if v is not None and k == "latency_ms":
            v *= 1000.0                                    # the AF reports seconds
        met = None
        if v is not None:
            met = {"dl_mbps": v >= t["dl_min_mbps"], "ul_mbps": v >= t["ul_min_mbps"],
                   "latency_ms": v <= t["lat_max_ms"]}[k]
        out["metrics"][k] = {"value": round(v, 2) if v is not None else None, "met": met,
                             "method": _XE_PQOS_METRICS[k][0], "note": why}
    vals = [m["met"] for m in out["metrics"].values() if m["met"] is not None]
    out["degradation_expected"] = (not all(vals)) if vals else None
    return out


@app.get(f"{settings.api_prefix}/extreme-edge/pqos-thresholds", tags=["Extreme Edge"])
async def extreme_edge_get_thresholds():
    return dict(_xe_thresholds)


@app.post(f"{settings.api_prefix}/extreme-edge/pqos-thresholds", tags=["Extreme Edge"])
async def extreme_edge_set_thresholds(payload: dict):
    for k in _xe_thresholds:
        if payload.get(k) is not None:
            _xe_thresholds[k] = float(payload[k])
    return dict(_xe_thresholds)


def _xe_device():
    return {"ipv4Address": {"publicAddress": XE_UE_IP, "privateAddress": XE_UE_IP}}


@app.get(f"{settings.api_prefix}/extreme-edge/qod", tags=["Extreme Edge"])
async def extreme_edge_qod_sessions():
    """Active CAMARA QoD sessions of the vehicle UE."""
    st, d = await asyncio.to_thread(_xe_http, "POST", f"{XE_QOD_URL}/retrieve_sessions", {"device": _xe_device()})
    if st != 200:
        raise HTTPException(502, f"QoD API answered {st}: {d}")
    return {"ue_ip": XE_UE_IP, "app_server": XE_EDGE_IP, "sessions": d if isinstance(d, list) else []}


@app.post(f"{settings.api_prefix}/extreme-edge/qod", tags=["Extreme Edge"], status_code=201)
async def extreme_edge_qod_request(payload: dict):
    """Request a QoD session for the vehicle UE <-> edge flow (the camera stream
    to YOLO's floating IP on the edge)."""
    body = {"device": _xe_device(), "applicationServer": {"ipv4Address": XE_EDGE_IP},
            "qosProfile": payload.get("profile", "QOS_L"), "duration": int(payload.get("duration", 300))}
    st, d = await asyncio.to_thread(_xe_http, "POST", f"{XE_QOD_URL}/sessions", body)
    if st not in (200, 201):
        raise HTTPException(502, f"QoD API answered {st}: {d}")
    return d


@app.delete(f"{settings.api_prefix}/extreme-edge/qod/{{session_id}}", tags=["Extreme Edge"])
async def extreme_edge_qod_release(session_id: str):
    st, d = await asyncio.to_thread(_xe_http, "DELETE", f"{XE_QOD_URL}/sessions/{session_id}")
    if st not in (200, 204):
        raise HTTPException(502, f"QoD API answered {st}: {d}")
    return {"released": session_id}


# ── Scenario decision engine (the day scenario, autonomous) ──────────────────
# Every XE_ENGINE_PERIOD s, while enabled, it decides where the vehicle's YOLO
# runs from what the page shows: edge greenness at the scenario hour, the pQoS
# verdict for the vehicle UE, where YOLO runs now and its live counters.
#   vehicle -> edge   the edge is greener than the vehicle by `margin` (and at
#                     least green_min) and pQoS is OK                   (noon)
#   on the edge, pQoS predicts degradation                          (afternoon)
#                     request QoD, watch the real stream for verify_s; if FPS
#                     holds (>= 80% of before) and incomplete frames stay low,
#                     stay; otherwise release QoD and migrate back
#   edge not green    (below green_min, or the vehicle greener by `margin`)
#                     release QoD, back to the vehicle            (evening)
#   pQoS recovers     release QoD
# A migration in flight or the cooldown after one pauses decisions, and YOLO
# does not go back to the edge while pQoS stays degraded. `degrade` is the
# demo lever: treat pQoS as degraded whatever the AF predicts.
#
# trigger "pqos" judges the AF's predictions; trigger "rtt" judges the vehicle's
# own round trip to the edge over 5G (see _xe_rtt_sample): degraded after
# rtt_confirm bad samples in a row, healthy again after as many good ones.
# qod_first False skips the QoD attempt and relocates at once. After leaving a
# degraded edge, YOLO stays on the vehicle for hold_s: the camera's own uplink
# was part of the load, so the 5G path looks healthy as soon as it is gone.
_xe_engine = {"enabled": False, "degrade": False, "green_min": 0.2, "margin": 0.05, "period_s": 5.0,
              "verify_s": 10.0, "cooldown_s": 30.0, "qod_profile": "QOS_L",
              "trigger": os.environ.get("DRIFT_XE_TRIGGER", "rtt"),   # rtt: the vehicle's own 5G round trip (pQoS has no data for its SIM)
              "qod_first": True,
              "rtt_p90_max_ms": 100.0, "rtt_loss_max_pct": 5.0, "rtt_confirm": 2, "hold_s": 90.0,
              "hold_until": 0.0, "ticks": [],
              "rtt": None, "rtt_bad": 0, "rtt_good": 0, "rtt_degraded": False, "rtt_bad_since": None,
              "events": [],
              "qod": None, "cooldown_until": 0.0, "status": "off", "log": []}

# The vehicle UE is not in the operator's ping telemetry and the pQoS latency
# follows traffic volume, not delay, so trigger "rtt" measures the camera's own
# path: 10 pings from the vehicle to the edge node over wwan0 per tick (the
# vehicle also has a LAN port on the same subnet, hence -I wwan0).
XE_EDGE_NODE_IP = os.environ.get("DRIFT_XE_EDGE_NODE_IP", "10.220.2.170")
_XE_RTT_CMD = f"ping -I wwan0 -c 10 -i 0.2 -W 1 {XE_EDGE_NODE_IP}"


def _xe_rtt_judge(s: dict) -> dict:
    e = _xe_engine
    s["bad"] = s["p90_ms"] is None or s["p90_ms"] > e["rtt_p90_max_ms"] or s["loss_pct"] > e["rtt_loss_max_pct"]
    return s


async def _xe_rtt_sample(st: dict, db) -> Optional[dict]:
    """The newest window of drift-rtt-probe (read with the state, no extra call).
    None when it is the window already judged; pings directly if the probe is
    not running (no window, or one older than 10 s)."""
    w = st.get("rtt_window")
    if w and time.time() - w["t"] < 10:
        if (_xe_engine["rtt"] or {}).get("t") == w["t"]:
            return None
        return _xe_rtt_judge(dict(w, source="probe"))
    import re as _re
    host = (st.get("vehicle") or {}).get("host")
    veh = _resolve_cluster(db, XE_VEHICLE)
    if not host or not veh or not veh.kubeconfig_path:
        return None
    token = await asyncio.to_thread(_agent_token, veh.kubeconfig_path)
    raw, ok = await asyncio.to_thread(_agent_exec, host, token, _XE_RTT_CMD, 10)
    if ok is None:
        return None
    rtts = sorted(float(x) for x in _re.findall(r"time=([\d.]+)", raw or ""))
    loss = 100.0 * (10 - len(rtts)) / 10
    p90 = rtts[max(0, int(0.9 * len(rtts)) - 1)] if rtts else None
    return _xe_rtt_judge({"t": time.time(), "p90_ms": p90, "median_ms": rtts[len(rtts) // 2] if rtts else None,
                          "loss_pct": loss, "source": "direct"})


def _xe_event(kind: str, **kw):
    _xe_engine["events"] = (_xe_engine["events"] + [{"t": time.time(), "kind": kind, **kw}])[-100:]


def _xe_engine_log(msg: str, action: Optional[str] = None):
    _xe_engine["log"] = ([{"t": time.time(), "msg": msg, "action": action}] + _xe_engine["log"])[:40]
    logger.info("extreme-edge engine: %s", msg)


def _xe_counters(st: dict) -> dict:
    h = st.get("health") or {}
    return {"t": time.time(), "processed": h.get("processed"), "incomplete": h.get("incomplete"), "fps": h.get("fps")}


async def _xe_qod_release(reason: str):
    q = _xe_engine["qod"]
    if not q:
        return
    st, _ = await asyncio.to_thread(_xe_http, "DELETE", f"{XE_QOD_URL}/sessions/{q['session']}")
    _xe_engine["qod"] = None
    _xe_engine_log(f"QoD {q['profile']} released ({reason})", "qod-release")


async def _xe_engine_move(to: str, why: str, db, t_decided: Optional[float] = None):
    # logged at decision time: the MEC 021 procedure (selection, then the
    # migration's pre-flight over the vehicle's 5G) takes several seconds more
    _xe_engine_log(f"{why} -> MEC 021 mobility procedure: YOLO to the {to}", f"move-{to}")
    try:
        r = await extreme_edge_move({"to": to}, db)
        _xe_engine["cooldown_until"] = time.time() + _xe_engine["cooldown_s"]
        mid = (r or {}).get("migration_id")
        _xe_event("move", to=to, why=why, migration_id=mid, t_decided=t_decided,
                  t_initiated=(_pod_migrations.get(mid) or {}).get("started_at"))
    except HTTPException as e:
        _xe_engine_log(f"{why}, but the migration to the {to} was refused: {e.detail}")


async def _xe_engine_tick(db):
    e, now = _xe_engine, time.time()
    st = await extreme_edge_state(db)
    if st.get("in_flight"):
        e["status"] = "migration in flight"; return
    if now < e["cooldown_until"]:
        e["status"] = f"cooldown {e['cooldown_until'] - now:.0f} s"; return
    green = ((st.get("energy") or {}).get("edge") or {}).get("greenness") or 0.0
    veh = ((st.get("energy") or {}).get("vehicle") or {}).get("greenness") or 0.0
    hour = (st.get("energy") or {}).get("hour")
    if e["trigger"] == "rtt":
        s = await _xe_rtt_sample(st, db)
        if s is not None:                      # no new window: keep the current verdict
            e["rtt"] = s
            if s["bad"]:
                e["rtt_bad"], e["rtt_good"] = e["rtt_bad"] + 1, 0
                if e["rtt_bad"] == 1:
                    e["rtt_bad_since"] = s["t"]
            else:
                e["rtt_bad"], e["rtt_good"] = 0, e["rtt_good"] + 1
            if not e["rtt_degraded"] and e["rtt_bad"] >= e["rtt_confirm"]:
                e["rtt_degraded"] = True
                _xe_event("degraded", first_bad=e["rtt_bad_since"], sample=s, serving=st.get("serving"))
                _xe_engine_log(f"5G round trip degraded: p90 {s['p90_ms']} ms, loss {s['loss_pct']:.0f}%")
            elif e["rtt_degraded"] and e["rtt_good"] >= e["rtt_confirm"]:
                e["rtt_degraded"] = False
                _xe_event("recovered", sample=s, serving=st.get("serving"))
                _xe_engine_log(f"5G round trip healthy again: p90 {s['p90_ms']} ms")
        degraded = e["degrade"] or e["rtt_degraded"]
        why_pq = "pQoS degradation (lever)" if e["degrade"] else \
                 "5G round trip degraded" if e["rtt_degraded"] else "5G round trip OK"
    else:
        pq = await extreme_edge_pqos()
        degraded = e["degrade"] or pq.get("degradation_expected") is True
        why_pq = "pQoS degradation (lever)" if e["degrade"] else \
                 "pQoS predicts degradation" if pq.get("degradation_expected") else "pQoS OK"
    t_dec = time.time()
    e["tick_ms"] = round((t_dec - now) * 1000)   # context acquisition + decision
    # data_age_ms: from the end of the QoS measurement window to this decision (KPI-5)
    w = e["rtt"] if e["trigger"] == "rtt" else None
    e["ticks"] = (e["ticks"] + [{"t": now, "ms": e["tick_ms"], "trigger": e["trigger"],
                                 "data_age_ms": round((t_dec - w["t"]) * 1000) if w else None,
                                 "source": (w or {}).get("source")}])[-500:]
    serving = st.get("serving")
    # hysteresis as in the placement: move only if the other side is greener by `margin`
    if serving == "edge":
        want_edge = green >= e["green_min"] and veh <= green + e["margin"]
    else:
        want_edge = green >= e["green_min"] and green > veh + e["margin"]
    e["status"] = f"{hour:05.2f} h, edge {green:.0%} / vehicle {veh:.0%} green, {why_pq}, YOLO on the {serving}"
    if serving == "vehicle":
        if e["qod"]:
            await _xe_qod_release("YOLO is on the vehicle")
        if want_edge and not degraded and now < e["hold_until"]:
            e["status"] += f", holding on the vehicle {e['hold_until'] - now:.0f} s"
        elif want_edge and not degraded:
            await _xe_engine_move("edge", f"edge {green:.0%} green (vehicle {veh:.0%}) and {why_pq}", db, t_dec)
    elif serving == "edge":
        if not want_edge:
            await _xe_qod_release("leaving the edge")
            await _xe_engine_move("vehicle", f"edge only {green:.0%} green (vehicle {veh:.0%})", db, t_dec)
        elif degraded and not e["qod_first"]:
            e["hold_until"] = time.time() + e["hold_s"]
            await _xe_engine_move("vehicle", f"{why_pq} on the edge", db, t_dec)
        elif degraded:
            q = e["qod"]
            if not q:
                body = {"device": _xe_device(), "applicationServer": {"ipv4Address": XE_EDGE_IP},
                        "qosProfile": e["qod_profile"], "duration": 3600}
                code, d = await asyncio.to_thread(_xe_http, "POST", f"{XE_QOD_URL}/sessions", body)
                if code in (200, 201) and isinstance(d, dict):
                    e["qod"] = {"session": d.get("sessionId"), "profile": e["qod_profile"], "since": now,
                                "before": _xe_counters(st), "verdict": None}
                    _xe_engine_log(f"{why_pq} on the edge -> QoD {e['qod_profile']} requested, "
                                   f"checking the stream for {e['verify_s']:.0f} s", "qod-request")
                else:
                    e["hold_until"] = time.time() + e["hold_s"]
                    await _xe_engine_move("vehicle", f"{why_pq} and QoD was refused ({code})", db, t_dec)
            elif q["verdict"] == "holds" and e["trigger"] == "rtt" and e["rtt_degraded"]:
                # QoD held the stream, but the vehicle's own path has degraded
                # since: check again whether the session still keeps it usable
                q.update(verdict=None, since=now)
                _xe_engine_log("5G round trip degraded although QoD is active -> "
                               f"checking the stream again for {e['verify_s']:.0f} s", "qod-recheck")
            elif q["verdict"] is None and now - q["since"] >= e["verify_s"]:
                b, a = q["before"], _xe_counters(st)
                dt = max(a["t"] - b["t"], 1e-3)
                dp = (a["processed"] or 0) - (b["processed"] or 0)
                di = (a["incomplete"] or 0) - (b["incomplete"] or 0)
                fps, fps0 = dp / dt, b["fps"] or 0.0
                bad = di / max(dp + di, 1)
                ok = fps >= 0.8 * fps0 and bad <= 0.10
                if e["trigger"] == "rtt" and (e["rtt"] or {}).get("bad"):
                    ok = False                 # the stream may flow, but late or lossy
                q["verdict"] = "holds" if ok else "fails"
                detail = f"FPS {fps0:.1f} -> {fps:.1f}, incomplete frames {bad:.0%}"
                if ok:
                    _xe_engine_log(f"QoD {q['profile']} holds the stream ({detail}) -> YOLO stays on the edge", "stay")
                else:
                    await _xe_qod_release(f"it did not hold the stream: {detail}")
                    e["hold_until"] = time.time() + e["hold_s"]
                    await _xe_engine_move("vehicle", f"QoD could not keep the edge usable ({detail})", db, t_dec)
        elif e["qod"]:
            await _xe_qod_release("pQoS recovered")


async def _xe_engine_loop():
    from database import SessionLocal
    while True:
        await asyncio.sleep(_xe_engine["period_s"])
        if not _xe_engine["enabled"]:
            _xe_engine["status"] = "off"
            continue
        db = SessionLocal()
        try:
            await _xe_engine_tick(db)
        except Exception as ex:                       # never let one bad tick stop the engine
            _xe_engine["status"] = f"error: {ex}"
            logger.exception("extreme-edge engine tick failed")
        finally:
            db.close()


@app.get(f"{settings.api_prefix}/extreme-edge/engine", tags=["Extreme Edge"])
async def extreme_edge_engine():
    e = _xe_engine
    return {k: e[k] for k in ("enabled", "degrade", "green_min", "margin", "verify_s", "cooldown_s", "qod_profile",
                              "trigger", "qod_first", "rtt_p90_max_ms", "rtt_loss_max_pct", "rtt_confirm", "hold_s",
                              "period_s",
                              "rtt", "rtt_degraded", "events", "status", "log")} | \
           {"qod": e["qod"], "tick_ms": e.get("tick_ms"), "ticks": e["ticks"]}


@app.post(f"{settings.api_prefix}/extreme-edge/engine", tags=["Extreme Edge"])
async def extreme_edge_engine_set(payload: dict):
    """enabled (autopilot), degrade (the pQoS demo lever), green_min, margin,
    verify_s, cooldown_s, qod_profile, trigger (pqos|rtt), qod_first,
    rtt_p90_max_ms, rtt_loss_max_pct, rtt_confirm, hold_s, period_s, clear_events."""
    e = _xe_engine
    for k in ("enabled", "degrade"):
        if k in payload:
            if bool(payload[k]) != e[k]:
                _xe_engine_log(f"{'autopilot' if k == 'enabled' else 'pQoS degradation lever'} "
                               f"{'on' if payload[k] else 'off'}")
            e[k] = bool(payload[k])
    for k in ("green_min", "margin", "verify_s", "cooldown_s", "rtt_p90_max_ms", "rtt_loss_max_pct", "hold_s",
              "period_s"):
        if payload.get(k) is not None:
            e[k] = float(payload[k])
    if payload.get("rtt_confirm") is not None:
        e["rtt_confirm"] = max(1, int(payload["rtt_confirm"]))
    if payload.get("qod_profile") in ("QOS_E", "QOS_S", "QOS_M", "QOS_L"):
        e["qod_profile"] = payload["qod_profile"]
    if payload.get("trigger") in ("pqos", "rtt") and payload["trigger"] != e["trigger"]:
        e["trigger"] = payload["trigger"]
        e.update(rtt=None, rtt_bad=0, rtt_good=0, rtt_degraded=False, rtt_bad_since=None)
        _xe_engine_log(f"trigger: {e['trigger']}")
    if "qod_first" in payload:
        e["qod_first"] = bool(payload["qod_first"])
    if payload.get("clear_events"):
        e["events"], e["ticks"], e["log"] = [], [], []
        _xe_api_log.clear()
        _xe_api_epoch["n"] += 1
    if not e["enabled"] and e["qod"]:
        await _xe_qod_release("autopilot off")
    return await extreme_edge_engine()


# In-car view: what a passenger sees, fetched FROM the vehicle. The vehicle's
# drift-yolo-view relay (10.250.0.2:8090, tunnel only) connects each request to
# YOLO wherever it runs, so when the edge serves it these frames cross the 5G
# downlink, and a migration shows as a freeze and a reconnect.
XE_VIEW_URL = os.environ.get("DRIFT_XE_VIEW_URL", "http://10.250.0.2:8090")


@app.get(f"{settings.api_prefix}/extreme-edge/stream", tags=["Extreme Edge"])
async def extreme_edge_stream():
    """MJPEG of YOLO's annotated frames, as received in the vehicle."""
    import httpx
    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=15.0))
    try:
        req = client.build_request("GET", f"{XE_VIEW_URL}/stream")
        resp = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(502, f"in-car view unreachable: {e}")

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        except Exception:
            pass
        finally:
            await resp.aclose()
            await client.aclose()
    return StreamingResponse(body(), media_type=resp.headers.get("content-type", "multipart/x-mixed-replace; boundary=frame"),
                             headers={"Cache-Control": "no-store"})


@app.get(f"{settings.api_prefix}/extreme-edge/frame", tags=["Extreme Edge"])
async def extreme_edge_frame():
    """The latest annotated frame (204 until a viewer has woken the renderer)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.get(f"{XE_VIEW_URL}/frame")
    except Exception as e:
        raise HTTPException(502, f"in-car view unreachable: {e}")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "image/jpeg"), headers={"Cache-Control": "no-store"})


@app.post(f"{settings.api_prefix}/extreme-edge/scene", tags=["Extreme Edge"])
async def extreme_edge_scene(payload: dict, db: Session = Depends(get_db)):
    """Switch the dashcam replay between the day and the night drive, so the
    scenario's time of day matches the video (drift-dashcam-scene on the vehicle)."""
    scene = payload.get("scene")
    if scene not in ("day", "afternoon", "night"):
        raise HTTPException(400, 'scene must be "day", "afternoon" or "night"')
    from k8s_client import _k8s_get
    veh = _resolve_cluster(db, XE_VEHICLE)
    if not veh or not veh.kubeconfig_path:
        raise HTTPException(404, f"Cluster '{XE_VEHICLE}' not found")
    host = _node_internal_ip(veh.kubeconfig_path,
                             next(iter((_k8s_get(veh.kubeconfig_path, "/api/v1/nodes") or {}).get("items", [])), {}).get("metadata", {}).get("name", ""))
    token = await asyncio.to_thread(_agent_token, veh.kubeconfig_path)
    out, ok = await asyncio.to_thread(_agent_exec, host, token, f"/usr/local/sbin/drift-dashcam-scene {scene}", 20)
    if not ok:
        raise HTTPException(502, f"scene switch failed on the vehicle: {out.strip()[:200]}")
    return {"scene": scene}


# Emulated 5G uplink congestion, for the afternoon of the demo. The vehicle's
# wwan0 egress gets an HTB class at `mbit` with a short FIFO (~0.4 s at 5 Mbit/s)
# that carries the camera flow (UDP 5005) and ICMP. The camera (~5.6 Mbit/s)
# then loses frames and queues, and the autopilot's RTT trigger, whose pings
# share that queue, measures the congestion the stream really suffers. Only
# matters while YOLO runs on the edge: on board, the camera never leaves the car.
XE_CONGEST_MBIT = float(os.environ.get("DRIFT_XE_CONGEST_MBIT", "3"))
_XE_TC_OFF = "tc qdisc del dev wwan0 root 2>/dev/null; true"


def _xe_tc_on(mbit: float) -> str:
    return (f"{_XE_TC_OFF}; tc qdisc add dev wwan0 root handle 1: htb default 10 && "
            "tc class add dev wwan0 parent 1: classid 1:10 htb rate 1000mbit && "
            f"tc class add dev wwan0 parent 1: classid 1:20 htb rate {mbit:g}mbit ceil {mbit:g}mbit burst 15k && "
            "tc qdisc add dev wwan0 parent 1:20 handle 20: pfifo limit 200 && "
            "tc filter add dev wwan0 parent 1: protocol ip prio 1 u32 match ip protocol 17 0xff "
            "match ip dport 5005 0xffff flowid 1:20 && "
            "tc filter add dev wwan0 parent 1: protocol ip prio 2 u32 match ip protocol 1 0xff flowid 1:20")


@app.post(f"{settings.api_prefix}/extreme-edge/congestion", tags=["Extreme Edge"])
async def extreme_edge_congestion(payload: dict, db: Session = Depends(get_db)):
    """on (bool), mbit (default 3): emulate a congested 5G uplink on the vehicle."""
    from k8s_client import _k8s_get
    on = bool(payload.get("on"))
    mbit = float(payload.get("mbit") or XE_CONGEST_MBIT)
    if not 0.5 <= mbit <= 100:
        raise HTTPException(400, "mbit must be 0.5-100")
    veh = _resolve_cluster(db, XE_VEHICLE)
    if not veh or not veh.kubeconfig_path:
        raise HTTPException(404, f"Cluster '{XE_VEHICLE}' not found")
    nodes = await asyncio.to_thread(_k8s_get, veh.kubeconfig_path, "/api/v1/nodes") or {}
    host = await asyncio.to_thread(_node_internal_ip, veh.kubeconfig_path,
                                   next(iter(nodes.get("items", [])), {}).get("metadata", {}).get("name", ""))
    token = await asyncio.to_thread(_agent_token, veh.kubeconfig_path)
    out, ok = await asyncio.to_thread(_agent_exec, host, token, _xe_tc_on(mbit) if on else _XE_TC_OFF, 20)
    if not ok:
        raise HTTPException(502, f"tc failed on the vehicle: {(out or '').strip()[:200]}")
    _xe_event("congestion", on=on, mbit=mbit if on else None)
    _xe_engine_log(f"5G uplink congestion {'on (emulated, ' + format(mbit, 'g') + ' Mbit/s)' if on else 'off'}")
    return {"on": on, "mbit": mbit if on else None}


@app.post(f"{settings.api_prefix}/extreme-edge/move", tags=["Extreme Edge"], status_code=202)
async def extreme_edge_move(payload: dict, db: Session = Depends(get_db)):
    """Live-migrate the vehicle's YOLO to "edge" or "vehicle" (blocking + quiesce +
    delta bulk stream, chosen automatically for a vehicle hop)."""
    to = payload.get("to")
    if to not in ("edge", "vehicle"):
        raise HTTPException(400, 'to must be "edge" or "vehicle"')
    veh, edge = _resolve_cluster(db, XE_VEHICLE), _resolve_cluster(db, XE_EDGE)
    if not veh or not edge:
        raise HTTPException(404, f"Clusters '{XE_VEHICLE}' / '{XE_EDGE}' not found")
    if any(r.get("pod") == XE_POD and not (r.get("completed") or r.get("failed"))
           for r in _pod_migrations.values()):
        raise HTTPException(409, "A migration of this workload is already running")
    src, tgt = (veh, edge) if to == "edge" else (edge, veh)
    if payload.get("direct"):            # bypass MEC 021 (debugging only)
        return await _start_cross_pod_migration(str(src.id), str(tgt.id), "default", XE_POD, "", db)
    ue = _active_ue(db)
    return await _xe_mec_relocate(str(src.id), str(tgt.id), (ue.imsi if ue else None) or XE_UE_IMSI)


# ── MEC 021 for the extreme edge. emt-yolo is a registered application (an AMS)
# for the vehicle UE, and every vehicle<->edge move, from the autopilot or the
# GUI, is a MEC 021 mobility procedure with an explicit target: mec021-api ->
# drift-controller (selection honours the requested target) -> this backend's
# cross-migrate -> drift-pod-migrate. The autopilot stays the decision logic
# (greenness, pQoS, verified QoD), so the AMS opts out of the platform's own
# pQoS triggers (autonomous_trigger false) and they cannot start competing moves.
XE_UE_IMSI = os.environ.get("DRIFT_XE_UE_IMSI", "999700000000020")
_xe_mec = {"ams_id": None, "procedure_id": None, "procedure": None, "pending": False}


def _xe_mec_ams(src_cluster_id: str, imsi: str) -> str:
    """The AMS for emt-yolo, registered at its CURRENT cluster. mec021-api keeps
    registrations in memory and moves source_cluster after each completed
    procedure, so reuse it while it matches reality, else register afresh."""
    ams_path = "/amsi/v1/app_mobility_services"
    keep = None
    for a in _mec_call(ams_path, "GET") or []:
        ctx = a.get("registered_app_context") or {}
        if ctx.get("vm_name") != XE_POD:
            continue
        if keep is None and ctx.get("source_cluster") == src_cluster_id and ctx.get("ue_imsi") == imsi \
                and ctx.get("autonomous_trigger") is False:
            keep = a["app_mobility_service_id"]
        else:                            # stale: a restart, or the pod moved outside MEC
            _mec_call(f"{ams_path}/{a['app_mobility_service_id']}", "DELETE")
    if keep is None:
        keep = _mec_call(ams_path, "POST", {
            "registered_app_context": {
                "associated_app_id": XE_POD, "source_cluster": src_cluster_id,
                "source_namespace": "default", "vm_name": XE_POD, "resource_kind": "pod",
                "ue_imsi": imsi, "autonomous_trigger": False,
            },
            "service_continuity_mode": 1,
        })["app_mobility_service_id"]
    _xe_mec["ams_id"] = keep
    return keep


async def _xe_mec_relocate(src_id: str, tgt_id: str, imsi: str) -> dict:
    """Start the move as a MEC 021 mobility procedure and return the backend
    migration it hands down (callers track that, as before)."""
    t0 = time.time()
    _xe_mec["pending"] = True
    try:
        ams_id = await asyncio.to_thread(_xe_mec_ams, src_id, imsi)
        base = f"/amsi/v1/app_mobility_services/{ams_id}/mobility_procedures"
        proc = await asyncio.to_thread(_mec_call, base, "POST", {"target_cluster": tgt_id})
        pid = proc["mobility_procedure_id"]
        _xe_mec.update(procedure_id=pid, procedure=proc)
        # Selection takes ~1 s before the controller calls cross-migrate here.
        while time.time() - t0 < 30:
            mid = next((k for k, r in _pod_migrations.items()
                        if r.get("pod") == XE_POD and (r.get("started_at") or 0) >= t0), None)
            if mid:
                return {"migration_id": mid, "ams_id": ams_id, "mobility_procedure_id": pid, "via": "mec021"}
            st = await asyncio.to_thread(_mec_call, f"{base}/{pid}")
            _xe_mec["procedure"] = st
            if st.get("state") in ("COMPLETED", "FAILED", "CANCELLED"):
                # COMPLETED without a migration is a "stay" decision
                raise HTTPException(409, f"MEC 021 procedure {st.get('state')} ({st.get('decision')}): "
                                         f"{st.get('error_details') or st.get('decision_reason')}")
            await asyncio.sleep(0.3)
        raise HTTPException(504, "MEC 021 procedure did not start the migration within 30 s")
    finally:
        _xe_mec["pending"] = False


async def _xe_mec_view() -> dict:
    """The AMS and the latest procedure, for the page; refreshed until terminal."""
    p = _xe_mec.get("procedure") or {}
    if p and p.get("state") not in ("COMPLETED", "FAILED", "CANCELLED") and _xe_mec.get("ams_id"):
        try:
            p = await asyncio.to_thread(
                _mec_call, f"/amsi/v1/app_mobility_services/{_xe_mec['ams_id']}"
                           f"/mobility_procedures/{p['mobility_procedure_id']}")
            _xe_mec["procedure"] = p
        except HTTPException:
            pass
    return {"ams_id": _xe_mec.get("ams_id"), "procedure_id": p.get("mobility_procedure_id"),
            "state": p.get("state"), "decision": p.get("decision"),
            "reason": p.get("decision_reason"), "error": p.get("error_details")}


@app.get(f"{settings.api_prefix}/pod-migrations/{{migration_id}}", tags=["Pods"])
async def pod_migration_status(migration_id: str):
    reg = _pod_migrations.get(migration_id)
    if not reg:
        raise HTTPException(404, "Pod migration not found")
    return reg


@app.post(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/all-vms",
    tags=["VMs"],
    status_code=201,
)
async def create_vm(cluster_id: str, payload: dict, db: Session = Depends(get_db)):
    """Create a KubeVirt VirtualMachine on a edge cluster."""
    cluster = db.query(ManagementCluster).filter(
        ManagementCluster.id == cluster_id
    ).first()
    if not cluster:
        raise HTTPException(404, "Edge cluster not found")
    if not cluster.kubeconfig_path or not os.path.exists(cluster.kubeconfig_path):
        raise HTTPException(503, "Kubeconfig not available")

    from k8s_client import _k8s_post, _k8s_get
    import re as _re
    import base64 as _b64

    name       = payload.get("name", "").strip()
    namespace  = payload.get("namespace", "default").strip() or "default"
    cpu        = int(payload.get("cpu", 2))
    memory     = payload.get("memory", "2Gi")
    disk       = payload.get("disk", "20Gi") or "20Gi"
    image      = payload.get("image", "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img")
    persistent = bool(payload.get("persistent", False))

    if not name:
        raise HTTPException(400, "name is required")
    if not _re.match(r'^[a-z0-9][a-z0-9\-]*[a-z0-9]$', name):
        raise HTTPException(400, "name must be lowercase alphanumeric with hyphens")

    # ── Namespace ──────────────────────────────────────────────────────────────
    ns_obj = _k8s_get(cluster.kubeconfig_path, f"/api/v1/namespaces/{namespace}")
    if not ns_obj:
        _k8s_post(cluster.kubeconfig_path, "/api/v1/namespaces", {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace},
        })

    # ── cloud-init userData ────────────────────────────────────────────────────
    _pw_hash = "$6$pPFL6.hvqp9LsTgT$icDa1JoDFTn3YmTwHZ1TQhSji5AWcPbf9nI1ZwCAlzsNrgBmY0F03kPMfakRZsrQ6ZU.svFJqK3ltVe.z60js0"
    cloud_init = (
        "#cloud-config\n"
        "ssh_pwauth: true\n"
        "users:\n"
        "  - name: ubuntu\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    shell: /bin/bash\n"
        "    lock_passwd: false\n"
        f"    passwd: '{_pw_hash}'\n"
        "chpasswd:\n"
        "  expire: false\n"
        "packages:\n"
        "  - qemu-guest-agent\n"
        "runcmd:\n"
        "  - systemctl enable --now qemu-guest-agent\n"
    )
    cloud_init_b64 = _b64.b64encode(cloud_init.encode()).decode()
    cloud_init_vol: dict = {"name": "cloudinitdisk", "cloudInitNoCloud": {"userDataBase64": cloud_init_b64}}

    # ── Disk volumes ───────────────────────────────────────────────────────────
    if persistent:
        dv_name = f"{name}-dv"
        _k8s_post(
            cluster.kubeconfig_path,
            f"/apis/cdi.kubevirt.io/v1beta1/namespaces/{namespace}/datavolumes",
            {
                "apiVersion": "cdi.kubevirt.io/v1beta1",
                "kind": "DataVolume",
                "metadata": {"name": dv_name, "namespace": namespace},
                "spec": {
                    "source": {"http": {"url": image}},
                    "storage": {
                        # RWX class for live-migratable VMs. longhorn-rwx is the
                        # provisioned default (enable_longhorn); labs without
                        # Longhorn set DRIFT_VM_STORAGE_CLASS to the class the
                        # nfs_storage role publishes — a DV on a class that
                        # doesn't exist sits Pending forever, silently.
                        "storageClassName": os.environ.get("DRIFT_VM_STORAGE_CLASS", "longhorn-rwx"),
                        "volumeMode": "Filesystem",
                        "accessModes": ["ReadWriteMany"],
                        "resources": {"requests": {"storage": disk}},
                    },
                },
            },
            [],
        )
        disk_devices = [{"name": "datavolumedisk", "disk": {"bus": "virtio"}}]
        disk_volumes = [{"name": "datavolumedisk", "dataVolume": {"name": dv_name}}]
    else:
        disk_devices = [{"name": "containerdisk", "disk": {"bus": "virtio"}}]
        disk_volumes = [{"name": "containerdisk", "containerDisk": {"image": image}}]

    # ── VM manifest ────────────────────────────────────────────────────────────
    # Bridge binding (not masquerade): guest IP == OVN pod IP, no NAT.
    # KubeOVN ties the logical port to the VM — the IP persists through live migration.
    # The annotation opts into bridge-binding live migration (KubeVirt blocks it by default).
    _net_annotations = {"kubevirt.io/allow-pod-bridge-network-live-migration": ""}
    manifest = {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {"name": name, "namespace": namespace, "annotations": dict(_net_annotations)},
        "spec": {
            "running": True,
            "template": {
                "metadata": {
                    "labels": {"kubevirt.io/domain": name},
                    "annotations": dict(_net_annotations),
                },
                "spec": {
                    "domain": {
                        "cpu": {"cores": cpu},
                        "resources": {"requests": {"memory": memory}},
                        "devices": {
                            "disks": disk_devices + [{"name": "cloudinitdisk", "disk": {"bus": "virtio"}}],
                            "interfaces": [{"name": "default", "bridge": {}}],
                        },
                    },
                    "networks": [{"name": "default", "pod": {}}],
                    "volumes": disk_volumes + [cloud_init_vol],
                },
            },
        },
    }

    _err = []
    result = _k8s_post(
        cluster.kubeconfig_path,
        f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines",
        manifest,
        _err,
    )
    if not result:
        detail = _err[0] if _err else "Failed to create VM on cluster"
        raise HTTPException(500, detail)

    return {"name": name, "namespace": namespace, "status": "creating"}


@app.delete(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/all-vms/{{namespace}}/{{name}}",
    tags=["VMs"],
    status_code=204,
)
async def delete_vm(cluster_id: str, namespace: str, name: str, db: Session = Depends(get_db)):
    """Delete a KubeVirt VirtualMachine."""
    cluster = db.query(ManagementCluster).filter(
        ManagementCluster.id == cluster_id
    ).first()
    if not cluster:
        raise HTTPException(404, "Edge cluster not found")
    if not cluster.kubeconfig_path or not os.path.exists(cluster.kubeconfig_path):
        raise HTTPException(503, "Kubeconfig not available")

    from k8s_client import _k8s_delete
    ok = _k8s_delete(
        cluster.kubeconfig_path,
        f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines/{name}",
    )
    if not ok:
        raise HTTPException(500, "Failed to delete VM")


# ---------------------------------------------------------------------------
# Add Node to existing Edge Cluster
# ---------------------------------------------------------------------------

@app.post(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/nodes",
    tags=["Edge Clusters"],
    status_code=202,
)
async def add_node_to_cluster(
    cluster_id: UUID,
    payload: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Add a new node to an existing active edge cluster.
    Runs prepare + register asynchronously; returns immediately with 202.
    """
    cluster = db.query(ManagementCluster).filter(ManagementCluster.id == cluster_id).first()
    if not cluster:
        raise HTTPException(404, "Edge cluster not found")
    if cluster.status not in (
        ManagementClusterStatus.ACTIVE,
        ManagementClusterStatus.DEGRADED,
    ):
        raise HTTPException(400, f"Cluster must be Active or Degraded to add a node (current: {cluster.status})")

    # Validate required fields
    name = (payload.get("name") or "").strip()
    host = (payload.get("host") or "").strip()
    user = (payload.get("user") or "root").strip()
    roles = payload.get("roles") or ["agent"]
    ssh_key_path = (payload.get("ssh_key_path") or "").strip() or None

    if not name:
        raise HTTPException(400, "node name is required")
    if not host:
        raise HTTPException(400, "node host (IP/hostname) is required")

    node = {"name": name, "host": host, "user": user, "roles": roles, "ssh_key_path": ssh_key_path}

    async def _run():
        from management_cluster_orchestrator import ManagementClusterOrchestrator
        from database import SessionLocal
        db = SessionLocal()
        try:
            orch = ManagementClusterOrchestrator(db)
            await orch.add_node(cluster_id, node)
        except Exception as e:
            logging.getLogger(__name__).error(f"add_node failed: {e}")
        finally:
            db.close()

    background_tasks.add_task(_run)
    return {"status": "accepted", "node": name, "message": f"Node '{name}' is being added in the background. Check deployment logs for progress."}


# ---------------------------------------------------------------------------
# Cross-cluster mesh (zero-touch: EVPN full-mesh + xcluster prep across N clusters)
# ---------------------------------------------------------------------------
_cluster_meshes: Dict[str, dict] = {}


@app.post(f"{settings.api_prefix}/cluster-mesh", tags=["Clusters"], status_code=202)
async def wire_cluster_mesh(payload: dict, db: Session = Depends(get_db)):
    """Wire N edge clusters into a cross-cluster migration mesh (EVPN full-mesh
    stretched-L2 + routes to all peer pod-CIDRs + CA-trust union +
    DecentralizedLiveMigration). Every cluster peers with every other, so this
    composes for 3+ clusters — re-run with the full set whenever it changes. Each
    cluster's kubeconfig + the shared node-agent token are already in place from
    provisioning. Async; poll the returned status URL."""
    ids = payload.get("cluster_ids")
    if not isinstance(ids, list):
        # back-compat: accept {source_cluster_id, target_cluster_id}
        ids = [i for i in (payload.get("source_cluster_id"), payload.get("target_cluster_id")) if i]
    ids = [str(i).strip() for i in ids if str(i).strip()]
    ids = list(dict.fromkeys(ids))  # dedupe, preserve order
    if len(ids) < 2:
        raise HTTPException(400, "cluster_ids must list at least two distinct clusters")
    found = []
    for cid in ids:
        c = db.query(ManagementCluster).filter(ManagementCluster.id == cid).first()
        if not c:
            raise HTTPException(404, f"cluster not found: {cid}")
        found.append(c)

    mid = str(uuid4())
    names = [c.name for c in found]
    _cluster_meshes[mid] = {"id": mid, "clusters": names, "phase": "starting",
                            "completed": False, "failed": False}

    async def _run():
        from management_cluster_orchestrator import ManagementClusterOrchestrator
        from database import SessionLocal
        reg = _cluster_meshes[mid]
        _db = SessionLocal()
        try:
            cs = [_db.query(ManagementCluster).filter(ManagementCluster.id == cid).first() for cid in ids]
            orch = ManagementClusterOrchestrator(_db)
            await orch.wire_mesh(cs, reg=reg)
            reg.update(phase="completed", completed=True)
        except Exception as e:
            reg.update(phase="failed", failed=True, error=str(e))
        finally:
            _db.close()

    asyncio.create_task(_run())
    return {"mesh_id": mid, "clusters": names,
            "status_url": f"{settings.api_prefix}/cluster-mesh/{mid}"}


@app.get(f"{settings.api_prefix}/cluster-mesh", tags=["Clusters"])
async def list_cluster_meshes():
    """All cross-cluster mesh wiring runs (most-recent first) for the
    Cross-Cluster Migration view."""
    return list(reversed(list(_cluster_meshes.values())))


@app.get(f"{settings.api_prefix}/cluster-mesh/{{mesh_id}}", tags=["Clusters"])
async def cluster_mesh_status(mesh_id: str):
    reg = _cluster_meshes.get(mesh_id)
    if not reg:
        raise HTTPException(404, "mesh run not found")
    return reg


# ---------------------------------------------------------------------------
# MEC-021 migration trigger — proxy to the mec021-api service (for the GUI).
# Browsers can't easily hit the :8010 MEC service (CORS/port); the backend does
# it internally over the docker network and exposes a same-origin flow.
# ---------------------------------------------------------------------------
MEC021_URL = os.getenv("MEC021_URL", "http://mec021-api:8010")


def _mec_call(path: str, method: str = "GET", payload: dict = None):
    import json as _json
    import time as _time
    import urllib.request as _u, urllib.error as _ue
    data = _json.dumps(payload).encode() if payload is not None else None
    last = None
    # Retry transient DNS/connection blips (docker embedded-DNS can momentarily
    # fail to resolve a service name); an HTTP error is a real answer — don't retry.
    for attempt in range(4):
        req = _u.Request(f"{MEC021_URL}{path}", data=data, method=method,
                         headers={"Content-Type": "application/json"})
        try:
            with _u.urlopen(req, timeout=20) as r:
                return _json.loads(r.read().decode() or "{}")
        except _ue.HTTPError as e:
            raise HTTPException(502, f"MEC API {method} {path} -> {e.code}: {e.read().decode()[:400]}")
        except Exception as e:
            last = e
            _time.sleep(0.5 * (attempt + 1))
    raise HTTPException(502, f"MEC API unreachable at {MEC021_URL} after retries: {last}")


@app.get(f"{settings.api_prefix}/mec/ams", tags=["MEC"])
async def mec_list_ams():
    """Registered Application Mobility Services. Each one also holds a pQoS
    subscription, so a stale registration keeps costing procedures."""
    out = await asyncio.to_thread(_mec_call, "/amsi/v1/app_mobility_services", "GET")
    return out if isinstance(out, list) else [out]


def _opt_float(v):
    """'' / None -> None, so a blank GUI field means 'platform default' rather
    than a hard 0 that would disable the metric."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@app.post(f"{settings.api_prefix}/mec/ams", tags=["MEC"], status_code=201)
async def mec_register_ams(payload: dict):
    src = (payload.get("source_cluster") or "").strip()
    workload = (payload.get("workload_name") or "").strip()
    if not src or not workload:
        raise HTTPException(400, "source_cluster and workload_name are required")
    body = {
        "registered_app_context": {
            "associated_app_id": (payload.get("app_id") or workload).strip(),
            "source_cluster": src,
            "source_namespace": (payload.get("namespace") or "default").strip(),
            "vm_name": workload,
            "resource_kind": (payload.get("resource_kind") or "pod").strip(),
            "ue_imsi": (payload.get("ue_imsi") or "").strip() or None,
            # What THIS app needs. Blank means "use the platform default", which
            # is what every registration did before these existed.
            "min_dl_mbps": _opt_float(payload.get("min_dl_mbps")),
            "max_latency_ms": _opt_float(payload.get("max_latency_ms")),
        },
        "service_continuity_mode": 1,
    }
    return await asyncio.to_thread(_mec_call, "/amsi/v1/app_mobility_services", "POST", body)


@app.delete(f"{settings.api_prefix}/mec/ams/{{ams_id}}", tags=["MEC"], status_code=204)
async def mec_deregister_ams(ams_id: str):
    await asyncio.to_thread(_mec_call, f"/amsi/v1/app_mobility_services/{ams_id}", "DELETE")


@app.post(f"{settings.api_prefix}/mec/trigger", tags=["MEC"], status_code=202)
async def mec_trigger(payload: dict):
    """Follow-me MEC flow: register an AMS then fire a mobility procedure via the
    MEC-021 API. The drift-controller's Optimal Edge Selection then decides the
    target and drives the migration. Returns ams_id + procedure_id; poll
    /mec/status/{ams_id}/{procedure_id}."""
    src = (payload.get("source_cluster") or "").strip()
    workload = (payload.get("workload_name") or "").strip()
    if not src or not workload:
        raise HTTPException(400, "source_cluster and workload_name are required")
    kind = (payload.get("resource_kind") or "pod").strip()
    ns = (payload.get("namespace") or "default").strip()
    target = (payload.get("target_cluster") or "").strip() or None
    ams_body = {
        "registered_app_context": {
            "associated_app_id": (payload.get("app_id") or workload).strip(),
            "source_cluster": src, "source_namespace": ns,
            "vm_name": workload, "resource_kind": kind,
            "ue_imsi": (payload.get("ue_imsi") or "").strip() or None,
        },
        "service_continuity_mode": 1,
    }
    # Reuse the caller's registration when given one; only mint a new AMS if this
    # really is a new application. Otherwise every trigger leaves another
    # registration (and another pQoS subscription) behind.
    existing = (payload.get("ams_id") or "").strip()
    if existing:
        ams_id = existing
    else:
        ams = await asyncio.to_thread(_mec_call, "/amsi/v1/app_mobility_services", "POST", ams_body)
        ams_id = ams["app_mobility_service_id"]
    proc = await asyncio.to_thread(
        _mec_call, f"/amsi/v1/app_mobility_services/{ams_id}/mobility_procedures",
        "POST", {"target_cluster": target})
    return {"ams_id": ams_id, "procedure_id": proc["mobility_procedure_id"],
            "resource_kind": kind, "workload": workload}


@app.get(f"{settings.api_prefix}/mec/status/{{ams_id}}/{{procedure_id}}", tags=["MEC"])
async def mec_status(ams_id: str, procedure_id: str):
    return await asyncio.to_thread(
        _mec_call, f"/amsi/v1/app_mobility_services/{ams_id}/mobility_procedures/{procedure_id}")


@app.delete(f"{settings.api_prefix}/mec/cancel/{{ams_id}}/{{procedure_id}}", tags=["MEC"])
async def mec_cancel(ams_id: str, procedure_id: str):
    """Cancel an in-flight mobility procedure (ETSI MEC-021 DELETE on the procedure).
    Only cancellable before KubeVirt hand-off (PREPARING/SELECTING); once the live
    migration is handed off the controller returns 409 and you must clean up manually."""
    return await asyncio.to_thread(
        _mec_call, f"/amsi/v1/app_mobility_services/{ams_id}/mobility_procedures/{procedure_id}", "DELETE")


# ---------------------------------------------------------------------------
# Follow-Me dashboard — one aggregated view for the demo. Every sub-source is
# best-effort: a missing controller / energy service / migration just comes
# back null so the page degrades gracefully.
# ---------------------------------------------------------------------------
DRIFT_CONTROLLER_URL = os.getenv("DRIFT_CONTROLLER_URL", "http://drift-controller:8011")
ENERGY_TELEMETRY_URL = os.getenv("ENERGY_TELEMETRY_URL", "http://energy-telemetry:9004")
# The demo workload's stable serving endpoint (the floating IP that follows it
# across migrations), probed for the dashboard's Application panel.
DEMO_APP_HOST = os.getenv("DEMO_APP_HOST", "10.220.3.101")
DEMO_APP_PORT = int(os.getenv("DEMO_APP_PORT", "9000"))


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _svc_get(url: str, timeout: int = 6):
    import json as _json
    import urllib.request as _u
    try:
        with _u.urlopen(url, timeout=timeout) as r:
            return _json.loads(r.read().decode() or "null")
    except Exception:
        return None


def _svc_post(url: str, payload: dict, timeout: int = 6):
    import json as _json
    import urllib.request as _u
    req = _u.Request(url, data=_json.dumps(payload).encode(),
                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _u.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode() or "null")
    except Exception as e:
        return {"error": str(e)}


@app.get(f"{settings.api_prefix}/demo/state", tags=["Dashboard"])
async def demo_state(db: Session = Depends(get_db)):
    # The vehicle (drift-vehicle) is a UE that also hosts workloads; it has its
    # own page (Extreme Edge) and is not one of Follow-Me's network edges.
    clusters = [c for c in db.query(ManagementCluster).filter(ManagementCluster.deleted_at.is_(None)).all()
                if c.name != XE_VEHICLE]
    name_by_id = {str(c.id): c.name for c in clusters}
    edges = [{"id": str(c.id), "name": c.name, "status": c.status, "pod_cidr": c.pod_cidr,
              "arch": _cluster_arch(c)} for c in clusters]

    # per-edge energy (best-effort proxy to the energy-telemetry service)
    ids = ",".join(str(c.id) for c in clusters)
    energy = await asyncio.to_thread(_svc_get, f"{ENERGY_TELEMETRY_URL}/energy?ids={ids}")
    energy_nodes = (energy or {}).get("nodes", {})
    for e in edges:
        e["energy"] = energy_nodes.get(e["id"])

    # actual placement of the migratable workloads (holder+work pods + running
    # VMIs) on each edge — so the topology shows every workload where it runs.
    def _edge_workloads(c):
        from k8s_client import _k8s_get as _kget
        out = []
        if not (c.kubeconfig_path and os.path.exists(c.kubeconfig_path)):
            return out
        kc = c.kubeconfig_path
        for p in ((_kget(kc, "/api/v1/pods") or {}).get("items") or []):
            meta = p.get("metadata", {}) or {}
            ann = meta.get("annotations", {}) or {}
            conts = [x.get("name") for x in (p.get("spec", {}) or {}).get("containers", [])]
            # skip a drained source husk (moved away); include fresh + restored targets
            if "holder" in conts and "work" in conts and not ann.get("drift.io/migrated-to"):
                out.append({"name": ann.get("drift.io/serving-name") or meta.get("name"),
                            "kind": "pod", "edge": c.name})
        for v in ((_kget(kc, "/apis/kubevirt.io/v1/virtualmachineinstances") or {}).get("items") or []):
            if (v.get("status", {}) or {}).get("phase") == "Running":
                out.append({"name": (v.get("metadata", {}) or {}).get("name"),
                            "kind": "vm", "edge": c.name})
        return out

    workloads = []
    for c in clusters:
        try:
            workloads += await asyncio.to_thread(_edge_workloads, c)
        except Exception:
            pass
    for e in edges:
        e["workloads"] = [w for w in workloads if w["edge"] == e["name"]]

    # Give each edge application its own endpoint + liveness, so the dashboard can
    # list them all instead of showing one conflated "the app". The address is the
    # workload's follow-me endpoint, not a node: that is what survives a migration
    # and therefore what anyone watching the demo should be pointed at.
    # Two ways in per app: the lab path (relay on the edge segment) and, where one
    # exists, the path that actually crosses 5G via the UE's own proxy. The video
    # pod deliberately has no UE entry — it has no proxy on the phone, and saying
    # so is more useful than quietly offering a link that would not go over radio.
    DEMO_ENDPOINTS = {
        "yolo-app": {"label": "YOLO perception", "host": "10.220.3.103", "port": 9000,
                     "path": "/ue", "via": UE_RELAY_BIND, "via_port": UE_RELAY_PORT,
                     "ue_via": UE_PI_HOST, "ue_port": UE_PI_PORT, "ue_path": "/ue"},
        "my-app":   {"label": "Video streaming", "host": "10.220.3.101", "port": 9000,
                     "path": "/ue", "via": UE_VIDEO_RELAY_BIND, "via_port": UE_VIDEO_RELAY_PORT,
                     "ue_via": UE_PI_HOST, "ue_port": UE_VIDEO_PI_PORT, "ue_path": "/ue"},
        "demo-vm":  {"label": "Video streaming (VM)", "host": "192.170.10.10", "port": 9000,
                     "path": "/loop.webm", "via": UE_VM_RELAY_HOST, "via_port": UE_VM_RELAY_PORT,
                     "ue_via": UE_PI_HOST, "ue_port": UE_PI_VM_PORT, "ue_path": "/loop.webm"},
    }
    active_ue = _active_ue(db)
    for w in workloads:
        meta = DEMO_ENDPOINTS.get(w["name"])
        if not meta:
            continue
        w["label"] = meta["label"]
        w["endpoint"] = f'{meta["host"]}:{meta["port"]}'
        # the floating IP is only on-link from the edge segment; the relay is what
        # an off-subnet browser can actually open
        w["url"] = f'http://{meta["via"]}:{meta["via_port"]}{meta["path"]}'
        w["reachable"] = await asyncio.to_thread(_tcp_reachable, meta["via"], meta["via_port"])
        if meta.get("ue_via") and active_ue is not None:
            # through the ACTIVE UE's relay, opened via the backend's per-UE proxy
            w["url_ue"] = f'{settings.api_prefix}/ues/{active_ue.id}/proxy/{meta["ue_port"]}{meta["ue_path"]}'
            w["reachable_ue"] = await asyncio.to_thread(_tcp_reachable, active_ue.host, meta["ue_port"])

    # UE QoS + recent decisions (best-effort proxy to the controller)
    ue_qos = await asyncio.to_thread(_svc_get, f"{DRIFT_CONTROLLER_URL}/context")
    decisions = await asyncio.to_thread(_svc_get, f"{DRIFT_CONTROLLER_URL}/decisions") or []
    last_decision = decisions[0] if decisions else None

    def _cname(x):
        return name_by_id.get(str(x), x)

    migs = []
    for m in reversed(list(_pod_migrations.values())):
        migs.append({"kind": "pod", "workload": m.get("pod"),
                     "src": _cname(m.get("src_cluster")), "dst": _cname(m.get("target_cluster")),
                     "phase": m.get("phase"), "completed": bool(m.get("completed")),
                     "failed": bool(m.get("failed")), "freeze_s": m.get("freeze_s"),
                     "net_total_mib": m.get("net_total_mib"), "compute_mode": m.get("compute_mode")})
    for m in reversed(list(_xcluster_migrations.values())):
        st = (m.get("status") or "").lower()
        migs.append({"kind": "vm", "workload": m.get("vm"),
                     "src": _cname(m.get("source_cluster")), "dst": _cname(m.get("target_cluster")),
                     "phase": st or None,
                     # VM cross-migration terminal statuses: succeeded / unhealthy /
                     # rolled_back / rollback_failed / failed (running while in flight).
                     "completed": st in ("succeeded", "completed"),
                     "failed": st in ("failed", "unhealthy", "rolled_back", "rollback_failed"),
                     "freeze_s": None, "net_total_mib": None, "compute_mode": "vm"})

    # workload current edge: live placement wins (a workload can be (re)deployed
    # outside the migration flow); the most recent completed migration's
    # destination is only the fallback while no live placement is visible
    # (e.g. between checkpoint and restore).
    location = None
    for m in migs:
        if m.get("completed"):
            location = {"workload": m["workload"], "edge": m["dst"], "kind": m["kind"]}
            break
    live = next((w for w in workloads
                 if location is None or w["name"] == location["workload"]), None)
    if live:
        location = {"workload": live["name"], "edge": live["edge"], "kind": live["kind"]}

    # KPI aggregates over the FULL history (not the capped list above).
    _VM_DONE = ("succeeded", "completed")
    _VM_FAIL = ("failed", "unhealthy", "rolled_back", "rollback_failed")
    pod_done = [m for m in _pod_migrations.values() if m.get("completed")]
    vm_done = [m for m in _xcluster_migrations.values()
               if (m.get("status") or "").lower() in _VM_DONE]
    failed = (sum(1 for m in _pod_migrations.values() if m.get("failed"))
              + sum(1 for m in _xcluster_migrations.values() if (m.get("status") or "").lower() in _VM_FAIL))
    total = len(_pod_migrations) + len(_xcluster_migrations)
    freezes = [m["freeze_s"] for m in pod_done if m.get("freeze_s") is not None]
    nets = [m["net_total_mib"] for m in pod_done if m.get("net_total_mib") is not None]
    _avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    kpis = {
        "total": total,
        "completed": len(pod_done) + len(vm_done),
        "failed": failed,
        "last_freeze_s": freezes[-1] if freezes else None,
        "best_freeze_s": min(freezes) if freezes else None,
        "avg_freeze_s": _avg(freezes),
        "last_net_mib": nets[-1] if nets else None,
        "avg_net_mib": _avg(nets),
    }

    # live probe of the workload's serving endpoint (the floating IP)
    reachable = await asyncio.to_thread(_tcp_reachable, DEMO_APP_HOST, DEMO_APP_PORT)
    app = {"host": DEMO_APP_HOST, "port": DEMO_APP_PORT, "reachable": reachable,
           "workload": location["workload"] if location else None,
           "edge": location["edge"] if location else None}

    return {"edges": edges, "ue_qos": ue_qos, "last_decision": last_decision,
            "workload_location": location, "workloads": workloads,
            "migrations": migs[:15], "kpis": kpis, "app": app}


@app.post(f"{settings.api_prefix}/demo/gc-sources", tags=["Dashboard"])
async def gc_migrated_sources(payload: dict = None, db: Session = Depends(get_db)):
    """Sweep every cluster for drained post-copy sources and collect them.

    The automatic collector runs at the end of each migration, but sources
    predating it — or left by a migration whose drain was not measured — need a
    sweep. Same rule as the automatic path: a source is removed only when the
    workload is demonstrably alive on the other side, so this cannot orphan a
    running application. `{"dry_run": true}` reports without deleting."""
    from k8s_client import _k8s_get, _k8s_delete
    dry = bool((payload or {}).get("dry_run"))
    clusters = [c for c in db.query(ManagementCluster).all()
                if c.kubeconfig_path and os.path.exists(c.kubeconfig_path)]
    by_name = {c.name: c.kubeconfig_path for c in clusters}
    out = []
    for c in clusters:
        pods = await asyncio.to_thread(_k8s_get, c.kubeconfig_path,
                                       "/api/v1/namespaces/default/pods")
        for pod in (pods or {}).get("items", []):
            meta = pod.get("metadata") or {}
            ann = meta.get("annotations") or {}
            if not ann.get("drift.io/migrated-to"):
                continue                      # not a source
            name = meta.get("name")
            dst_pod = ann.get("drift.io/migration-dst-pod") or name
            dst_kc = by_name.get(ann.get("drift.io/migration-dst-cluster")) or c.kubeconfig_path
            tgt = await asyncio.to_thread(_k8s_get, dst_kc,
                f"/api/v1/namespaces/default/pods/{dst_pod}")
            tann = ((tgt or {}).get("metadata") or {}).get("annotations") or {}
            live = bool(tgt) and (tgt.get("status") or {}).get("phase") == "Running" \
                   and bool(tann.get("drift.io/migrated-from"))
            rec = {"cluster": c.name, "pod": name,
                   "migrated_to": ann.get("drift.io/migrated-to"),
                   "target_cluster": ann.get("drift.io/migration-dst-cluster"),
                   "target_alive": live}
            if not live:
                rec["action"] = "kept: target not confirmed running"
            elif dry:
                rec["action"] = "would collect"
            else:
                ok = await asyncio.to_thread(_k8s_delete, c.kubeconfig_path,
                    f"/api/v1/namespaces/default/pods/{name}?gracePeriodSeconds=0")
                rec["action"] = "collected" if ok else "delete failed"
            out.append(rec)
    return {"dry_run": dry, "sources": out,
            "collected": sum(1 for r in out if r.get("action") == "collected")}


@app.get(f"{settings.api_prefix}/demo/auto-trigger", tags=["Dashboard"])
async def get_auto_trigger():
    return await asyncio.to_thread(_svc_get, f"{MEC021_URL}/auto-trigger") or {}


@app.post(f"{settings.api_prefix}/demo/auto-trigger", tags=["Dashboard"])
async def set_auto_trigger(payload: dict):
    """Arm/disarm autonomous migrations without restarting anything."""
    return await asyncio.to_thread(_svc_post, f"{MEC021_URL}/auto-trigger", payload) or {}


@app.get(f"{settings.api_prefix}/demo/pqos-thresholds", tags=["Dashboard"])
async def get_pqos_thresholds():
    return await asyncio.to_thread(_svc_get, f"{DRIFT_CONTROLLER_URL}/pqos-thresholds") or {}


@app.post(f"{settings.api_prefix}/demo/pqos-thresholds", tags=["Dashboard"])
async def set_pqos_thresholds(payload: dict):
    """Set the degradation thresholds in BOTH places they exist.

    The controller compares its own polled predictions locally, while the AF
    subscriptions carry QosThresholds the NETWORK watches. Updating only the
    first leaves the network still armed on the old numbers, so both the
    downlink and the latency limits are pushed to the subscriptions too
    (E2E_LATENCY/ABOVE and THROUGHPUT_DL/BELOW). The uplink limit stays a local
    judgement: no AF subscription carries it."""
    out = await asyncio.to_thread(_svc_post, f"{DRIFT_CONTROLLER_URL}/pqos-thresholds", payload)
    result = dict(out or {})
    if payload.get("dl_min_mbps") is not None or payload.get("lat_max_ms") is not None:
        sub = await asyncio.to_thread(_svc_post, f"{MEC021_URL}/pqos-thresholds", payload)
        result["subscription"] = sub or {"changed": False, "reason": "mec021 unreachable"}
    return result


# ---------------------------------------------------------------------------
# UE data path (demo): the two socat relays that carry the UE's stream.
#   phone -> Pi:8080 -> (5G/wwan0) -> hub relay <bind>:9000 -> app floating IP
# The hub relay must sit on a host that is ON-LINK with the app's macvlan IP
# (a macvlan pod cannot answer off-subnet sources), and the Pi leg pins the
# relay IP out over wwan0 so the path genuinely crosses the 5G access.
# ---------------------------------------------------------------------------

UE_RELAY_HOST = os.getenv("UE_RELAY_HOST", "10.220.3.20")   # hub host (ssh, on-link)
UE_RELAY_BIND = os.getenv("UE_RELAY_BIND", "10.220.3.30")   # secondary IP the relay binds
UE_RELAY_PORT = int(os.getenv("UE_RELAY_PORT", "9000"))
UE_PI_HOST = os.getenv("UE_PI_HOST", "10.220.2.187")
UE_PI_USER = os.getenv("UE_PI_USER", "pi")
UE_PI_PORT = int(os.getenv("UE_PI_PORT", "8080"))
# second UE proxy: the VM video path (drift-1 relay -> EVPN overlay -> guest)
UE_PI_VM_PORT = int(os.getenv("UE_PI_VM_PORT", "8081"))
UE_VM_RELAY_HOST = os.getenv("UE_VM_RELAY_HOST", "10.220.3.21")
UE_VM_RELAY_PORT = int(os.getenv("UE_VM_RELAY_PORT", "9000"))
# The video pod has no UE proxy of its own and its floating IP is on-link only,
# so an off-segment browser needs this relay on the hub. The VM needs one on the
# edge node itself, because its address lives on the stretched EVPN L2 that only
# the edge nodes are attached to.
UE_VIDEO_RELAY_BIND = os.getenv("UE_VIDEO_RELAY_BIND", "10.220.3.20")
UE_VIDEO_RELAY_PORT = int(os.getenv("UE_VIDEO_RELAY_PORT", "9101"))
# The video pod's UE entry point. It targets the hub relay on .30 rather than
# .20 because only .30 is routed over wwan0 on the Pi — same relay either way,
# but this is the address whose traffic actually crosses the radio.
UE_VIDEO_PI_PORT = int(os.getenv("UE_VIDEO_PI_PORT", "8082"))
UE_VIDEO_APP_HOST = os.getenv("UE_VIDEO_APP_HOST", "10.220.3.101")
UE_VIDEO_APP_PORT = int(os.getenv("UE_VIDEO_APP_PORT", "9000"))
UE_VM_EVPN_IP = os.getenv("UE_VM_EVPN_IP", "192.170.10.10")
UE_VM_EVPN_PORT = int(os.getenv("UE_VM_EVPN_PORT", "9000"))
UE_VM_BR_ADDR = os.getenv("UE_VM_BR_ADDR", "192.170.10.1/24")
UE_PI_WWAN_GW = os.getenv("UE_PI_WWAN_GW", "10.45.0.16")
UE_PI_WWAN_DEV = os.getenv("UE_PI_WWAN_DEV", "wwan0")

_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5"]


def _relay_ssh(user_host: str, cmd: str) -> tuple:
    r = subprocess.run(["ssh", *_SSH_OPTS, user_host, cmd],
                       capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout + r.stderr).strip()


def _ue_path_status(ue=None) -> dict:
    """The demo data path for the ACTIVE UE (the Pi or the vehicle): the app, the
    hub/edge relays, and that UE's own relays, which the browser reaches through
    the backend's per-UE proxy."""
    host = ue.host if ue is not None else UE_PI_HOST
    base = f"{settings.api_prefix}/ues/{ue.id}/proxy" if ue is not None else None
    app_up = _tcp_reachable(DEMO_APP_HOST, DEMO_APP_PORT)
    relay_up = _tcp_reachable(UE_RELAY_BIND, UE_RELAY_PORT)
    pi_up = _tcp_reachable(host, UE_PI_PORT)
    vm_pi_up = _tcp_reachable(host, UE_PI_VM_PORT)
    video_up = _tcp_reachable(UE_VIDEO_RELAY_BIND, UE_VIDEO_RELAY_PORT)
    video_ue_up = _tcp_reachable(host, UE_VIDEO_PI_PORT)
    vm_edge_up = _tcp_reachable(UE_VM_RELAY_HOST, UE_VM_RELAY_PORT)
    return {
        "app": {"host": DEMO_APP_HOST, "port": DEMO_APP_PORT, "up": app_up},
        "hub_relay": {"host": UE_RELAY_BIND, "port": UE_RELAY_PORT, "up": relay_up},
        "pi_proxy": {"host": host, "port": UE_PI_PORT, "up": pi_up},
        "vm_proxy": {"host": host, "port": UE_PI_VM_PORT, "up": vm_pi_up},
        "video_relay": {"host": UE_VIDEO_RELAY_BIND, "port": UE_VIDEO_RELAY_PORT, "up": video_up},
        "video_ue_proxy": {"host": host, "port": UE_VIDEO_PI_PORT, "up": video_ue_up},
        "vm_edge_relay": {"host": UE_VM_RELAY_HOST, "port": UE_VM_RELAY_PORT, "up": vm_edge_up},
        "ok": app_up and relay_up and pi_up and vm_pi_up and video_up and vm_edge_up and video_ue_up,
        "ue": _ue_row(ue) if ue is not None else None,
        "ue_base": base,     # + "/<port>/<path>": the UE's relay, via the backend
        "ue_url": f"{base}/{UE_PI_PORT}/ue" if base else f"http://{host}:{UE_PI_PORT}/ue",
        "vm_url": f"{base}/{UE_PI_VM_PORT}/loop.webm" if base else f"http://{host}:{UE_PI_VM_PORT}/loop.webm",
    }


# The VM runs a stock `python3 -m http.server`, which sends no CORS header, so a
# browser blocks the dashboard's fetch() against it even though <video> playback
# and curl work fine. That is why the VM's charts sat at zero while the UE was
# happily streaming. Proxy those probes through here instead: same origin as the
# rest of the API, so the browser is satisfied. Whitelisted, never an open proxy.
# Two pairs, because WHICH path is probed is the whole claim. The lab pair goes
# straight to the edge relay; the UE pair goes through the Pi's proxy, so the
# measurement genuinely crosses the radio and matches what the phone experiences.
# Routing a UE-path URL to the lab target would quietly report the wrong network.
DEMO_PROBE_TARGETS = {
    "vm":         (f"http://{UE_VM_RELAY_HOST}:{UE_VM_RELAY_PORT}/", 8192),
    "vm-bulk":    (f"http://{UE_VM_RELAY_HOST}:{UE_VM_RELAY_PORT}/loop.webm", 262144),
    "vm-ue":      ("http://{ue}:%d/" % UE_PI_VM_PORT, 8192),            # {ue}: the active UE
    "vm-ue-bulk": ("http://{ue}:%d/loop.webm" % UE_PI_VM_PORT, 262144),
}


@app.get(f"{settings.api_prefix}/demo/probe/{{name}}", tags=["Dashboard"])
async def demo_probe(name: str, db: Session = Depends(get_db)):
    entry = DEMO_PROBE_TARGETS.get(name)
    if not entry:
        raise HTTPException(404, f"unknown probe target '{name}'")
    url, cap = entry
    if "{ue}" in url:
        ue = _active_ue(db)
        url = url.replace("{ue}", ue.host if ue is not None else UE_PI_HOST)

    def _get():
        import urllib.request as _u
        req = _u.Request(url, headers={"User-Agent": "drift-probe"})
        with _u.urlopen(req, timeout=6) as r:
            return r.read(cap), r.headers.get("Content-Type", "application/octet-stream")
    try:
        data, ctype = await asyncio.to_thread(_get)
    except Exception as exc:
        raise HTTPException(502, f"probe target unreachable: {exc}")
    return Response(content=data, media_type=ctype, headers={"Cache-Control": "no-store"})


@app.get(f"{settings.api_prefix}/demo/ue-path", tags=["Dashboard"])
async def get_ue_path(db: Session = Depends(get_db)):
    ue = _active_ue(db)
    return await asyncio.to_thread(_ue_path_status, ue)


@app.post(f"{settings.api_prefix}/demo/ue-path/apply", tags=["Dashboard"], status_code=202)
async def apply_ue_path(db: Session = Depends(get_db)):
    """(Re)start both relays over SSH. Idempotent: existing listeners are
    replaced. The pkill patterns use a [b]racket so they can never match the
    ssh command line that carries them."""
    def _apply():
        steps = {}
        # NOTE: the pkill MUST travel in its own ssh call — in a combined command
        # the pattern matches the socat launch text on the same remote command
        # line and pkill kills the shell before the new relay starts.
        # hub relay: ensure the on-link bind IP exists, kill old, start new
        _relay_ssh(f"root@{UE_RELAY_HOST}",
                   f"ip addr add {UE_RELAY_BIND}/24 dev eth0 2>/dev/null; "
                   f"pkill -f 'TCP-LISTEN:{UE_RELAY_PORT},bind=[{UE_RELAY_BIND[0]}]{UE_RELAY_BIND[1:]}'; true")
        rc, out = _relay_ssh(f"root@{UE_RELAY_HOST}", (
            f"nohup socat TCP-LISTEN:{UE_RELAY_PORT},bind={UE_RELAY_BIND},fork,reuseaddr "
            f"TCP:{DEMO_APP_HOST}:{DEMO_APP_PORT} >/tmp/drift-hub-relay.log 2>&1 & disown; "
            f"sleep 0.5; ss -tln sport = :{UE_RELAY_PORT} | tail -1"))
        steps["hub_relay"] = {"rc": rc, "out": out[-200:]}
        # Video-pod relay on the hub: same trick as the hub relay, different app.
        _relay_ssh(f"root@{UE_RELAY_HOST}",
                   f"pkill -f 'TCP-LISTEN:[{str(UE_VIDEO_RELAY_PORT)[0]}]{str(UE_VIDEO_RELAY_PORT)[1:]}'; true")
        rc, out = _relay_ssh(f"root@{UE_RELAY_HOST}", (
            f"nohup socat TCP-LISTEN:{UE_VIDEO_RELAY_PORT},fork,reuseaddr "
            f"TCP:{UE_VIDEO_APP_HOST}:{UE_VIDEO_APP_PORT} >/tmp/drift-video-relay.log 2>&1 & disown; "
            f"sleep 0.5; ss -tln sport = :{UE_VIDEO_RELAY_PORT} | tail -1"))
        steps["video_relay"] = {"rc": rc, "out": out[-200:]}
        # The active UE's side (Pi or vehicle): pin the relay routes over its 5G
        # interface and (re)start its pod / VM / video proxies.
        if ue is not None:
            _st, gw = _probe_ue(ue)
            if _st["ssh"] and gw:
                steps.update(_repair_ue_steps(ue, gw))
            else:
                steps["ue"] = {"rc": 1, "out": f"{ue.name}: unreachable or 5G down"}
        # VM relay on the edge node: the guest lives on the stretched EVPN L2, so
        # the node needs an address on br-drift before it can reach it at all.
        _relay_ssh(f"root@{UE_VM_RELAY_HOST}",
                   f"ip addr add {UE_VM_BR_ADDR} dev br-drift 2>/dev/null; "
                   f"ip link set br-drift mtu 1400 2>/dev/null; "
                   f"pkill -f 'TCP-LISTEN:[{str(UE_VM_RELAY_PORT)[0]}]{str(UE_VM_RELAY_PORT)[1:]},bind={UE_VM_RELAY_HOST}'; true")
        rc, out = _relay_ssh(f"root@{UE_VM_RELAY_HOST}", (
            f"nohup socat TCP-LISTEN:{UE_VM_RELAY_PORT},bind={UE_VM_RELAY_HOST},fork,reuseaddr "
            f"TCP:{UE_VM_EVPN_IP}:{UE_VM_EVPN_PORT} >/tmp/drift-vm-relay.log 2>&1 & disown; "
            f"sleep 0.5; ss -tln sport = :{UE_VM_RELAY_PORT} | tail -1"))
        steps["vm_edge_relay"] = {"rc": rc, "out": out[-200:]}
        st = _ue_path_status(ue)
        st["steps"] = steps
        return st
    ue = _active_ue(db)
    return await asyncio.to_thread(_apply)


# ---------------------------------------------------------------------------
# UE inventory: devices on the 5G access. Today: follow-me traffic endpoints
# (socat proxies) + pQoS identity (IMSI); later: extreme-edge pod hosts.
# ---------------------------------------------------------------------------

def _ue_row(ue: UserEquipment) -> dict:
    return {"id": str(ue.id), "name": ue.name, "host": ue.host,
            "ssh_user": ue.ssh_user, "wwan_iface": ue.wwan_iface,
            "wwan_gw": ue.wwan_gw or "", "imsi": ue.imsi or "",
            "notes": ue.notes or "", "active": bool(ue.active), "retired": bool(ue.retired),
            # the browser reaches every UE's relays through the backend (the
            # vehicle is only reachable over its 5G tunnel, from the hub)
            "proxy_base": f"{settings.api_prefix}/ues/{ue.id}/proxy",
            "created_at": ue.created_at.isoformat() if ue.created_at else None}


# The vehicle Jetson as a UE: managed over its 5G tunnel (10.250.0.2), which
# DRIFT's backend reaches through the WireGuard hub; the Pi UEs are retired.
VEHICLE_UE = {"name": "vehicle-ue", "host": "10.250.0.2", "ssh_user": "ecb", "wwan_iface": "wwan0",
              "imsi": XE_IMSI, "notes": "vehicle Jetson (jetson-nano-01): Quectel RM530N-GL, managed over its 5G WireGuard tunnel"}
_ue_schema_ok = False


def _seed_default_ue(db: Session):
    global _ue_schema_ok
    if not _ue_schema_ok:   # columns added after the table existed (no migrations here)
        # Only ALTER when a column is really missing, and never wait for the lock:
        # an ALTER queued behind other requests' open transactions blocks this
        # (sync) call on the event loop, so those requests never finish.
        have = {r[0] for r in db.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'user_equipments'"))}
        missing = [c for c in ("active", "retired") if c not in have]
        if missing:
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            for c in missing:
                db.execute(text(f"ALTER TABLE user_equipments ADD COLUMN IF NOT EXISTS {c} boolean DEFAULT false"))
        db.commit()
        _ue_schema_ok = True
    if db.query(UserEquipment).count() == 0 and UE_PI_HOST:
        db.add(UserEquipment(name="ncsrd-ue1", host=UE_PI_HOST, ssh_user=UE_PI_USER,
                             wwan_iface=UE_PI_WWAN_DEV, wwan_gw=UE_PI_WWAN_GW,
                             imsi="999700000000011",
                             notes="seeded from env; IMSI verified from SIM 2026-08-28"))
        db.commit()
    if not db.query(UserEquipment).filter(UserEquipment.name == VEHICLE_UE["name"]).first():
        for old in db.query(UserEquipment).all():      # the Pi UEs: kept, retired, inactive
            old.retired, old.active = True, False
        db.add(UserEquipment(active=True, retired=False, **VEHICLE_UE))
        db.commit()
    if not db.query(UserEquipment).filter(UserEquipment.active.is_(True)).first():
        first = db.query(UserEquipment).filter(UserEquipment.retired.isnot(True)).first() \
            or db.query(UserEquipment).first()
        if first:
            first.active = True
            db.commit()


def _active_ue(db: Session):
    _seed_default_ue(db)
    return db.query(UserEquipment).filter(UserEquipment.active.is_(True)).first()


def _push_ue_to_controller(ue) -> dict:
    """Point the drift-controller's pQoS/QoD at this UE (its IMSI and address)."""
    st = None
    try:
        st, _ = _probe_ue(ue)
    except Exception:
        pass
    body = {"imsi": ue.imsi or "", "ue_ip": ((st or {}).get("wwan") or {}).get("ip", "")}
    return _svc_post(f"{DRIFT_CONTROLLER_URL}/ue", body) if body["imsi"] else {"skipped": "no IMSI"}


@app.post(f"{settings.api_prefix}/ues/{{ue_id}}/activate", tags=["UEs"])
async def activate_ue(ue_id: str, db: Session = Depends(get_db)):
    """Make this UE the one Follow-Me shows and the controller's pQoS/QoD use."""
    ue = _get_ue(db, ue_id)
    for u in db.query(UserEquipment).all():
        u.active = (u.id == ue.id)
    db.commit()
    controller = await asyncio.to_thread(_push_ue_to_controller, ue)
    return {"active": _ue_row(ue), "controller": controller}


# UE relays seen from the browser: the backend fetches http://<ue host>:<port>/...
# (streamed, Range passed through for video). Only the relay ports, never an
# open proxy.
@app.get(f"{settings.api_prefix}/ues/{{ue_id}}/proxy/{{port}}/{{path:path}}", tags=["UEs"])
async def ue_proxy(ue_id: str, port: int, path: str, request: Request, db: Session = Depends(get_db)):
    if port not in (UE_PI_PORT, UE_PI_VM_PORT, UE_VIDEO_PI_PORT):
        raise HTTPException(404, "not a UE relay port")
    ue = _get_ue(db, ue_id)
    import httpx
    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=30.0))
    hdrs = {k: v for k, v in request.headers.items() if k.lower() in ("range", "accept")}
    try:
        resp = await client.send(client.build_request("GET", f"http://{ue.host}:{port}/{path}",
                                                      params=dict(request.query_params), headers=hdrs), stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(502, f"UE relay unreachable: {e}")

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        except Exception:
            pass
        finally:
            await resp.aclose()
            await client.aclose()
    keep = {k: v for k, v in resp.headers.items()
            if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges")}
    keep["Cache-Control"] = "no-store"
    return StreamingResponse(body(), status_code=resp.status_code, headers=keep)


@app.get(f"{settings.api_prefix}/ues", tags=["UEs"])
async def list_ues(db: Session = Depends(get_db)):
    _seed_default_ue(db)
    return {"ues": [_ue_row(u) for u in db.query(UserEquipment).order_by(UserEquipment.created_at)]}


@app.post(f"{settings.api_prefix}/ues", tags=["UEs"], status_code=201)
async def create_ue(payload: dict, db: Session = Depends(get_db)):
    name = (payload.get("name") or "").strip()
    host = (payload.get("host") or "").strip()
    if not name or not host:
        raise HTTPException(400, "name and host are required")
    if db.query(UserEquipment).filter(UserEquipment.name == name).first():
        raise HTTPException(409, f"UE '{name}' already exists")
    ue = UserEquipment(
        name=name, host=host,
        ssh_user=(payload.get("ssh_user") or "pi").strip(),
        wwan_iface=(payload.get("wwan_iface") or "wwan0").strip(),
        wwan_gw=(payload.get("wwan_gw") or "").strip() or None,
        imsi=(payload.get("imsi") or "").strip() or None,
        notes=(payload.get("notes") or "").strip() or None)
    db.add(ue)
    db.commit()
    db.refresh(ue)
    return _ue_row(ue)


@app.delete(f"{settings.api_prefix}/ues/{{ue_id}}", tags=["UEs"], status_code=204)
async def delete_ue(ue_id: str, db: Session = Depends(get_db)):
    ue = db.query(UserEquipment).filter(UserEquipment.id == ue_id).first()
    if not ue:
        raise HTTPException(404, "UE not found")
    db.delete(ue)
    db.commit()


def _get_ue(db, ue_id) -> UserEquipment:
    ue = db.query(UserEquipment).filter(UserEquipment.id == ue_id).first()
    if not ue:
        raise HTTPException(404, "UE not found")
    return ue


def _probe_ue(ue) -> tuple:
    """One SSH round-trip: 5G address, live wwan gateway, and whether BOTH relay
    routes actually leave via the 5G interface (proxies can be up while the
    traffic silently leaks over eth0 — ports alone don't prove the 5G path)."""
    rc, out = _relay_ssh(f"{ue.ssh_user}@{ue.host}", (
        f"ip -4 -o addr show {ue.wwan_iface} 2>/dev/null | awk '{{print $4}}'; "
        f"ip -4 route show default dev {ue.wwan_iface} 2>/dev/null; "
        f"ip route get {UE_RELAY_BIND} 2>/dev/null | head -1; "
        f"ip route get {UE_VM_RELAY_HOST} 2>/dev/null | head -1"))
    # ssh noise (known-hosts warnings) shares the captured stream — parse, don't slice
    m = re.search(r"^(\d+\.\d+\.\d+\.\d+)/\d+", out, re.M) if rc == 0 else None
    wwan_ip = m.group(1) if m else ""
    # the wwan gateway is dynamic (changes on 5G re-registration) — prefer the
    # live default route on the interface over the stored value
    gw_m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out) if rc == 0 else None
    gw = gw_m.group(1) if gw_m else (ue.wwan_gw or "")

    def _via_5g(target: str) -> bool:
        mm = re.search(rf"^{re.escape(target)} .*?dev (\S+)", out, re.M)
        return bool(mm and mm.group(1) == ue.wwan_iface)

    st = {
        "ssh": rc == 0,
        "wwan": {"iface": ue.wwan_iface, "ip": wwan_ip, "up": bool(wwan_ip), "gw": gw},
        "path_5g": _via_5g(UE_RELAY_BIND) and _via_5g(UE_VM_RELAY_HOST),
        "pod_proxy": {"port": UE_PI_PORT, "up": _tcp_reachable(ue.host, UE_PI_PORT)},
        "vm_proxy": {"port": UE_PI_VM_PORT, "up": _tcp_reachable(ue.host, UE_PI_VM_PORT)},
        "video_proxy": {"port": UE_VIDEO_PI_PORT, "up": _tcp_reachable(ue.host, UE_VIDEO_PI_PORT)},
        "hub_relay": {"host": UE_RELAY_BIND, "port": UE_RELAY_PORT,
                      "up": _tcp_reachable(UE_RELAY_BIND, UE_RELAY_PORT)},
        "ue_url": f"{settings.api_prefix}/ues/{ue.id}/proxy/{UE_PI_PORT}/ue",
    }
    return st, gw


def _repair_ue_steps(ue, gw: str) -> dict:
    """Per-UE self-heal: re-pin both relay routes over the 5G interface and
    (re)start both socat proxies. Same idiom as apply_ue_path: the pkill travels
    in its own ssh call, and its pattern uses a [b]racket so it can never match
    the ssh command line that carries it."""
    uh = f"{ue.ssh_user}@{ue.host}"
    steps = {}
    rc, out = _relay_ssh(uh, (
        f"sudo ip route replace {UE_RELAY_BIND}/32 via {gw} dev {ue.wwan_iface} && "
        f"sudo ip route replace {UE_VM_RELAY_HOST}/32 via {gw} dev {ue.wwan_iface} && "
        f"ip route get {UE_RELAY_BIND} | head -1"))
    steps["routes"] = {"rc": rc, "out": out[-200:]}
    for name, port, target, tport in (
            ("pod_proxy", UE_PI_PORT, UE_RELAY_BIND, UE_RELAY_PORT),
            ("vm_proxy", UE_PI_VM_PORT, UE_VM_RELAY_HOST, UE_VM_RELAY_PORT),
            ("video_proxy", UE_VIDEO_PI_PORT, UE_RELAY_BIND, UE_VIDEO_RELAY_PORT)):
        _relay_ssh(uh, f"pkill -f 'TCP-LISTEN:[{str(port)[0]}]{str(port)[1:]}'; true")
        rc, out = _relay_ssh(uh, (
            f"nohup socat TCP-LISTEN:{port},fork,reuseaddr TCP:{target}:{tport} "
            f">/tmp/drift-{name}.log 2>&1 & disown; "
            f"sleep 0.5; ss -tln sport = :{port} | tail -1"))
        steps[name] = {"rc": rc, "out": out[-200:]}
    return steps


# ue_id -> monotonic ts of the last auto-repair (don't hammer a UE that stays broken)
_UE_REPAIR_TS: dict = {}


def _ue_defective(st: dict) -> bool:
    """Repairable defect: the device is reachable and registered on 5G, but a
    proxy is down or the relay routes don't leave via the 5G interface. An
    unreachable device or a dead radio is NOT repairable from here."""
    return (st["ssh"] and st["wwan"]["up"] and bool(st["wwan"]["gw"])
            and not (st["path_5g"] and st["pod_proxy"]["up"] and st["vm_proxy"]["up"]))


@app.get(f"{settings.api_prefix}/ues/{{ue_id}}/status", tags=["UEs"])
async def ue_status(ue_id: str, db: Session = Depends(get_db)):
    """Live status: ssh reachability, 5G interface + address, both proxies, and
    whether the data path actually rides the 5G interface. Self-healing: a
    repairable defect triggers an automatic repair (per-UE cooldown), and the
    response carries the repair steps in "repaired"."""
    ue = _get_ue(db, ue_id)
    st, gw = await asyncio.to_thread(_probe_ue, ue)
    now = time.monotonic()
    if _ue_defective(st) and now - _UE_REPAIR_TS.get(str(ue.id), 0) > 60:
        _UE_REPAIR_TS[str(ue.id)] = now
        steps = await asyncio.to_thread(_repair_ue_steps, ue, gw)
        st, gw = await asyncio.to_thread(_probe_ue, ue)
        st["repaired"] = steps
    if gw and gw != (ue.wwan_gw or ""):
        ue.wwan_gw = gw
        db.commit()
    return st


@app.post(f"{settings.api_prefix}/ues/{{ue_id}}/repair", tags=["UEs"])
async def ue_repair(ue_id: str, db: Session = Depends(get_db)):
    """Explicit repair, no cooldown: re-pin the 5G routes and restart both
    proxies, then report the resulting status."""
    ue = _get_ue(db, ue_id)
    st, gw = await asyncio.to_thread(_probe_ue, ue)
    if not (st["ssh"] and st["wwan"]["up"] and gw):
        raise HTTPException(502, "UE unreachable or 5G interface down — nothing to repair from here")
    steps = await asyncio.to_thread(_repair_ue_steps, ue, gw)
    _UE_REPAIR_TS[str(ue.id)] = time.monotonic()
    st, gw = await asyncio.to_thread(_probe_ue, ue)
    st["repaired"] = steps
    if gw and gw != (ue.wwan_gw or ""):
        ue.wwan_gw = gw
        db.commit()
    return st


@app.post(f"{settings.api_prefix}/ues/{{ue_id}}/read-imsi", tags=["UEs"])
async def ue_read_imsi(ue_id: str, db: Session = Depends(get_db)):
    """Read EF_IMSI from the device's SIM over QMI and store it. Never trust a
    SIM inventory sheet: the record is read from the hardware."""
    ue = _get_ue(db, ue_id)

    def _read():
        rc, out = _relay_ssh(f"{ue.ssh_user}@{ue.host}",
                             "ls /dev/cdc-wdm* 2>/dev/null | head -1")
        m = re.search(r"^(/dev/cdc-wdm\d+)$", out, re.M) if rc == 0 else None
        dev = m.group(1) if m else ""
        if not dev:
            raise HTTPException(502, "no QMI device (/dev/cdc-wdm*) on the UE")
        rc, out = _relay_ssh(f"{ue.ssh_user}@{ue.host}",
                             f"sudo qmicli -d {dev} --uim-read-transparent=0x3F00,0x7FFF,0x6F07 2>&1")
        m = re.search(r"Read result:\s*([0-9a-fA-F:\s]+)", out)
        if rc != 0 or not m:
            raise HTTPException(502, f"EF_IMSI read failed: {out[-200:]}")
        raw = [b for b in re.split(r"[:\s]+", m.group(1).strip()) if b]
        digits = "".join(b[1] + b[0] for b in raw[1:])   # nibble-swap each byte
        return digits[1:1 + 15]                          # drop parity nibble
    imsi = await asyncio.to_thread(_read)
    ue.imsi = imsi
    db.commit()
    return {"imsi": imsi, "source": "SIM EF_IMSI"}


@app.post(f"{settings.api_prefix}/ues/{{ue_id}}/apply-path", tags=["UEs"], status_code=202)
async def ue_apply_path(ue_id: str, db: Session = Depends(get_db)):
    """(Re)install the follow-me relays for THIS UE: hub relay plus the
    device's pod/VM proxies, with the relay routes pinned out over its 5G
    interface. Same bracket-trick/pkill-isolation rules as the legacy apply."""
    ue = _get_ue(db, ue_id)

    def _apply():
        steps = {}
        via = f"via {ue.wwan_gw} " if ue.wwan_gw else ""
        _relay_ssh(f"root@{UE_RELAY_HOST}",
                   f"ip addr add {UE_RELAY_BIND}/24 dev eth0 2>/dev/null; "
                   f"pkill -f 'TCP-LISTEN:{UE_RELAY_PORT},bind=[{UE_RELAY_BIND[0]}]{UE_RELAY_BIND[1:]}'; true")
        rc, out = _relay_ssh(f"root@{UE_RELAY_HOST}", (
            f"nohup socat TCP-LISTEN:{UE_RELAY_PORT},bind={UE_RELAY_BIND},fork,reuseaddr "
            f"TCP:{DEMO_APP_HOST}:{DEMO_APP_PORT} >/tmp/drift-hub-relay.log 2>&1 & disown; "
            f"sleep 0.5; ss -tln sport = :{UE_RELAY_PORT} | tail -1"))
        steps["hub_relay"] = {"rc": rc, "out": out[-200:]}
        _relay_ssh(f"{ue.ssh_user}@{ue.host}",
                   f"sudo ip route replace {UE_RELAY_BIND}/32 {via}dev {ue.wwan_iface} 2>/dev/null; "
                   f"pkill -f 'TCP-LISTEN:[{str(UE_PI_PORT)[0]}]{str(UE_PI_PORT)[1:]}'; true")
        rc, out = _relay_ssh(f"{ue.ssh_user}@{ue.host}", (
            f"nohup socat TCP-LISTEN:{UE_PI_PORT},fork,reuseaddr "
            f"TCP:{UE_RELAY_BIND}:{UE_RELAY_PORT} >/tmp/drift-ue-proxy.log 2>&1 & disown; "
            f"sleep 0.5; ss -tln sport = :{UE_PI_PORT} | tail -1"))
        steps["pod_proxy"] = {"rc": rc, "out": out[-200:]}
        _relay_ssh(f"{ue.ssh_user}@{ue.host}",
                   f"sudo ip route replace {UE_VM_RELAY_HOST}/32 {via}dev {ue.wwan_iface} 2>/dev/null; "
                   f"pkill -f 'TCP-LISTEN:[{str(UE_PI_VM_PORT)[0]}]{str(UE_PI_VM_PORT)[1:]}'; true")
        rc, out = _relay_ssh(f"{ue.ssh_user}@{ue.host}", (
            f"nohup socat TCP-LISTEN:{UE_PI_VM_PORT},fork,reuseaddr "
            f"TCP:{UE_VM_RELAY_HOST}:{UE_VM_RELAY_PORT} >/tmp/drift-vm-proxy.log 2>&1 & disown; "
            f"sleep 0.5; ss -tln sport = :{UE_PI_VM_PORT} | tail -1"))
        steps["vm_proxy"] = {"rc": rc, "out": out[-200:]}
        return {"steps": steps,
                "pod_proxy_up": _tcp_reachable(ue.host, UE_PI_PORT),
                "vm_proxy_up": _tcp_reachable(ue.host, UE_PI_VM_PORT),
                "ue_url": f"{settings.api_prefix}/ues/{ue.id}/proxy/{UE_PI_PORT}/ue"}
    return await asyncio.to_thread(_apply)


# ---------------------------------------------------------------------------
# Live Migration endpoints

# (cluster_id, exclude) -> (timestamp, nodes): short-TTL snapshot of the node
# resource inventory, served by list_mgmt_nodes on the controller's hot path.
_NODES_CACHE: dict = {}
# ---------------------------------------------------------------------------

@app.get(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/edge-nodes",
    tags=["VMs"],
)
async def list_mgmt_nodes(
    cluster_id: str,
    exclude: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    List edge cluster nodes with available resource data.
    Optionally exclude the source node (where the VM is currently running).
    Used to populate the target-node dropdown in the live migration modal.
    """
    cluster = db.query(ManagementCluster).filter(ManagementCluster.id == cluster_id).first()
    if not cluster:
        raise HTTPException(404, "Edge cluster not found")
    if not cluster.kubeconfig_path or not os.path.exists(cluster.kubeconfig_path):
        raise HTTPException(503, "Kubeconfig not available")

    # Short-TTL cache: this endpoint sits on the controller's decision hot path
    # (KPI-7), and the underlying metrics-server/kubelet queries can take a
    # second or more. A few seconds of staleness is immaterial next to the
    # metrics pipeline's own 30-60 s lag, so serve a recent snapshot when we
    # have one. DRIFT_NODES_CACHE_TTL=0 disables.
    ttl = float(os.environ.get("DRIFT_NODES_CACHE_TTL", "5"))
    ck = (str(cluster_id), exclude or "")
    hit = _NODES_CACHE.get(ck)
    if ttl > 0 and hit and (time.time() - hit[0]) < ttl:
        return {"nodes": hit[1]}

    from intra_live_migration_controller import list_nodes_for_migration
    nodes = await asyncio.to_thread(
        list_nodes_for_migration, cluster.kubeconfig_path, exclude
    )
    _NODES_CACHE[ck] = (time.time(), nodes)
    return {"nodes": nodes}


@app.post(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/all-vms/{{namespace}}/{{vm_name}}/migrate",
    tags=["VMs"],
)
async def migrate_vm(
    cluster_id: str,
    namespace: str,
    vm_name: str,
    payload: dict,
    db: Session = Depends(get_db),
):
    """
    Live-migrate a KubeVirt VM to the specified target node.

    Preflight: checks target node has sufficient CPU + memory (real OS-level usage
    via metrics-server or kubelet stats — not just pod requests).
    Trigger: creates a VirtualMachineInstanceMigration CR (KubeVirt-native).
    """
    cluster = db.query(ManagementCluster).filter(ManagementCluster.id == cluster_id).first()
    if not cluster:
        raise HTTPException(404, "Edge cluster not found")
    if not cluster.kubeconfig_path or not os.path.exists(cluster.kubeconfig_path):
        raise HTTPException(503, "Kubeconfig not available")

    target_node = (payload.get("target_node") or "").strip()
    if not target_node:
        raise HTTPException(400, "target_node is required")

    kc = cluster.kubeconfig_path

    # Fetch VM spec to get cpu/ram requirements
    from k8s_client import _k8s_get as _rec_get
    vm_obj = _rec_get(kc, f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines/{vm_name}")
    if not vm_obj:
        raise HTTPException(404, f"VM '{vm_name}' not found in namespace '{namespace}'")

    domain = vm_obj.get("spec", {}).get("template", {}).get("spec", {}).get("domain", {})
    cpu_cores = domain.get("cpu", {}).get("cores", 1)
    ram_str = domain.get("resources", {}).get("requests", {}).get("memory", "512Mi")

    # Preflight resource check
    from intra_live_migration_controller import check_migration_feasibility, trigger_live_migration
    feasible, reason = await asyncio.to_thread(
        check_migration_feasibility, kc, target_node, cpu_cores, ram_str
    )
    if not feasible:
        raise HTTPException(400, reason)

    # Trigger live migration
    ok, result = await asyncio.to_thread(
        trigger_live_migration, kc, vm_name, namespace, target_node
    )
    if not ok:
        raise HTTPException(500, result)

    return {"migration": result, "vm": vm_name, "target_node": target_node}


# ── Cross-cluster (decentralized) live migration ──────────────────────────────
# In-memory registry of running/finished cross-cluster migrations. The migration
# act is executed here (SBI) because the backend is the only holder of both
# kubeconfigs; drift-controller drives it over HTTP and stays kubeconfig-free.
_xcluster_migrations: Dict[str, dict] = {}
# VMs with an in-flight migration/failover, keyed "<namespace>/<vm>". Guards
# against concurrent operations on the same VM: without it, a stale/aborted
# migration task's delayed cleanup (which deletes the receiver VM on its target)
# can delete a healthy VM that a later migration has since placed there.
_active_vm_ops: set = set()


@app.post(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/all-vms/{{namespace}}/{{vm_name}}/cross-migrate",
    tags=["VMs"],
    status_code=202,
)
async def cross_migrate_vm(
    cluster_id: str,
    namespace: str,
    vm_name: str,
    payload: dict,
    db: Session = Depends(get_db),
):
    """
    Live-migrate a KubeVirt VM to a DIFFERENT edge cluster (decentralized
    cross-cluster live migration). Requires the cluster pair to have been
    prepared (ansible role `xcluster_migration`: feature gate + peer CA merged
    + cross-cluster route). Runs asynchronously; poll the returned status URL.
    """
    from cross_cluster_migration_controller import CrossClusterMigration

    target_cluster_id = (payload.get("target_cluster_id") or "").strip()
    if not target_cluster_id:
        raise HTTPException(400, "target_cluster_id is required")
    if target_cluster_id == cluster_id:
        raise HTTPException(400, "target cluster must differ from source cluster")

    source = _resolve_cluster(db, cluster_id)
    target = _resolve_cluster(db, target_cluster_id)
    for label, c in (("source", source), ("target", target)):
        if not c:
            raise HTTPException(404, f"{label} edge cluster not found")
        if not c.kubeconfig_path or not os.path.exists(c.kubeconfig_path):
            raise HTTPException(503, f"{label} kubeconfig not available")
    _require_same_arch(source, target)

    # Single-flight per VM: reject a second migration/failover of the same VM
    # while one is in flight (its delayed cleanup could otherwise delete the VM).
    vm_key = f"{namespace}/{vm_name}"
    if vm_key in _active_vm_ops:
        raise HTTPException(409, f"an operation for {vm_key} is already in progress")

    # Optional per-request overrides for the O-6 fault-tolerance path:
    #   force_unhealthy: make the post-migration health gate fail (rollback demo)
    #   enable_rollback: allow disabling the fallback for a single run
    _force_unhealthy = bool(payload.get("force_unhealthy", False))
    _enable_rollback = payload.get("enable_rollback")
    mig = CrossClusterMigration(
        source_kubeconfig=source.kubeconfig_path,
        target_kubeconfig=target.kubeconfig_path,
        namespace=namespace,
        vm_name=vm_name,
        force_unhealthy=_force_unhealthy,
        enable_rollback=(None if _enable_rollback is None else bool(_enable_rollback)),
    )
    state = {
        "migration_id": mig.migration_id,
        "status": "running",
        "source_cluster": cluster_id,
        "target_cluster": target_cluster_id,
        "vm": vm_name,
        "namespace": namespace,
        "logs": [],
        "result": None,
    }
    _xcluster_migrations[mig.migration_id] = state
    mig._log = lambda level, msg: state["logs"].append(f"{level}: {msg}")
    _active_vm_ops.add(vm_key)

    async def _run():
        try:
            result = await mig.run()
            state["status"] = result["status"]
            state["result"] = result
        finally:
            _active_vm_ops.discard(vm_key)

    asyncio.create_task(_run())

    return {
        "migration_id": mig.migration_id,
        "status": "running",
        "status_url": f"{settings.api_prefix}/cross-migrations/{mig.migration_id}",
    }


@app.get(f"{settings.api_prefix}/cross-migrations/{{migration_id}}", tags=["VMs"])
async def get_cross_migration(migration_id: str):
    state = _xcluster_migrations.get(migration_id)
    if not state:
        raise HTTPException(404, "cross-cluster migration not found")
    return state


# ── Crash-failover (O-6 "failover decisions") ────────────────────────────────
_failover_guards: Dict[str, dict] = {}


def _resolve_kc(db, cluster_id: str, label: str):
    c = db.query(ManagementCluster).filter(ManagementCluster.id == cluster_id).first()
    if not c:
        raise HTTPException(404, f"{label} edge cluster not found")
    if not c.kubeconfig_path or not os.path.exists(c.kubeconfig_path):
        raise HTTPException(503, f"{label} kubeconfig not available")
    return c


@app.post(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/all-vms/{{namespace}}/{{vm_name}}/failover",
    tags=["VMs"],
)
async def failover_vm(
    cluster_id: str, namespace: str, vm_name: str,
    payload: dict, db: Session = Depends(get_db),
):
    """Crash-failover a VM from its (assumed dead) home cluster onto a warm
    standby, fencing the home so it cannot dual-run. Unplanned-recovery
    counterpart to cross-migrate; cold restart, EVPN IP reclaimed on boot."""
    from crash_failover_controller import CrashFailoverController

    failover_cluster_id = (payload.get("failover_cluster_id") or "").strip()
    if not failover_cluster_id:
        raise HTTPException(400, "failover_cluster_id is required")
    if failover_cluster_id == cluster_id:
        raise HTTPException(400, "failover cluster must differ from home cluster")
    home = _resolve_kc(db, cluster_id, "home")
    standby = _resolve_kc(db, failover_cluster_id, "failover")

    vm_key = f"{namespace}/{vm_name}"
    if vm_key in _active_vm_ops:
        raise HTTPException(409, f"an operation for {vm_key} is already in progress")

    logs: list = []
    ctl = CrashFailoverController(
        home_kubeconfig=home.kubeconfig_path,
        failover_kubeconfig=standby.kubeconfig_path,
        namespace=namespace, vm_name=vm_name,
        log=lambda level, msg: logs.append(f"{level}: {msg}"),
        force_home_down=bool(payload.get("force_home_down", False)),
    )
    _active_vm_ops.add(vm_key)
    try:
        result = await ctl.fail_over()
        result["logs"] = logs
    finally:
        _active_vm_ops.discard(vm_key)
    return result


@app.get(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/all-vms/{{namespace}}/{{vm_name}}/failover-status",
    tags=["VMs"],
)
async def failover_status(
    cluster_id: str, namespace: str, vm_name: str,
    failover_cluster_id: str, db: Session = Depends(get_db),
):
    from crash_failover_controller import CrashFailoverController
    home = _resolve_kc(db, cluster_id, "home")
    standby = _resolve_kc(db, failover_cluster_id, "failover")
    ctl = CrashFailoverController(home.kubeconfig_path, standby.kubeconfig_path,
                                  namespace, vm_name)
    return ctl.status()


@app.post(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/all-vms/{{namespace}}/{{vm_name}}/failover-reconcile",
    tags=["VMs"],
)
async def failover_reconcile(
    cluster_id: str, namespace: str, vm_name: str,
    payload: dict, db: Session = Depends(get_db),
):
    """Recovery guard: after the (previously dead) home cluster returns, halt its
    stale VM so it cannot dual-claim the EVPN IP while the VM runs on the
    standby. `cluster_id` is the recovered home; failover_cluster_id the standby."""
    from crash_failover_controller import CrashFailoverController
    failover_cluster_id = (payload.get("failover_cluster_id") or "").strip()
    if not failover_cluster_id:
        raise HTTPException(400, "failover_cluster_id is required")
    home = _resolve_kc(db, cluster_id, "home")
    standby = _resolve_kc(db, failover_cluster_id, "failover")
    logs: list = []
    ctl = CrashFailoverController(
        home.kubeconfig_path, standby.kubeconfig_path, namespace, vm_name,
        log=lambda level, msg: logs.append(f"{level}: {msg}"),
    )
    result = await ctl.reconcile_recovered_home()
    result["logs"] = logs
    return result


async def _failover_monitor(guard_id: str):
    """Background loop: probe the home; on `threshold` consecutive down probes,
    fail over to the standby; then watch for the home's return and fence it."""
    g = _failover_guards[guard_id]
    ctl: "CrashFailoverController" = g["ctl"]
    down = 0
    while g["active"]:
        try:
            if g["phase"] == "watch_home":
                if ctl.home_down():
                    down += 1
                    g["last"] = f"home down ({down}/{g['threshold']})"
                    if down >= g["threshold"]:
                        g["events"].append(await ctl.fail_over())
                        g["phase"] = "failed_over"
                        down = 0
                else:
                    down = 0
                    g["last"] = "home healthy"
            elif g["phase"] == "failed_over":
                r = await ctl.reconcile_recovered_home()
                g["last"] = f"post-failover: {r.get('status')}"
                if r.get("status") == "home_fenced":
                    g["events"].append(r)
        except Exception as e:  # never let the monitor die on a transient error
            g["last"] = f"monitor error: {e}"
        await asyncio.sleep(g["interval"])


@app.post(f"{settings.api_prefix}/failover-guards", tags=["VMs"])
async def register_failover_guard(payload: dict, db: Session = Depends(get_db)):
    """Start automated crash-failover management for one VM: probe its home
    cluster and fail over to the standby on sustained loss."""
    from crash_failover_controller import CrashFailoverController
    home_id = (payload.get("home_cluster_id") or "").strip()
    standby_id = (payload.get("failover_cluster_id") or "").strip()
    ns = payload.get("namespace") or "default"
    vm = (payload.get("vm_name") or "").strip()
    if not (home_id and standby_id and vm):
        raise HTTPException(400, "home_cluster_id, failover_cluster_id, vm_name required")
    home = _resolve_kc(db, home_id, "home")
    standby = _resolve_kc(db, standby_id, "failover")

    guard_id = f"guard-{uuid4().hex[:8]}"
    g = {
        "guard_id": guard_id, "vm": vm, "namespace": ns,
        "home_cluster": home_id, "failover_cluster": standby_id,
        "interval": int(payload.get("interval", 15)),
        "threshold": int(payload.get("threshold", 3)),
        "phase": "watch_home", "active": True, "events": [], "last": "starting",
        "ctl": CrashFailoverController(
            home.kubeconfig_path, standby.kubeconfig_path, ns, vm,
            log=lambda level, msg: logger.log(getattr(logging, level, logging.INFO), msg),
        ),
    }
    _failover_guards[guard_id] = g
    asyncio.create_task(_failover_monitor(guard_id))
    return {"guard_id": guard_id, "watching": f"{ns}/{vm}", "home": home_id,
            "failover": standby_id, "interval": g["interval"], "threshold": g["threshold"]}


@app.get(f"{settings.api_prefix}/failover-guards", tags=["VMs"])
async def list_failover_guards():
    return [{k: v for k, v in g.items() if k != "ctl"} for g in _failover_guards.values()]


@app.delete(f"{settings.api_prefix}/failover-guards/{{guard_id}}", tags=["VMs"])
async def stop_failover_guard(guard_id: str):
    g = _failover_guards.get(guard_id)
    if not g:
        raise HTTPException(404, "guard not found")
    g["active"] = False
    return {"guard_id": guard_id, "stopped": True}


# ── Live cluster / node status ────────────────────────────────────────────────

@app.get(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/live-status",
    tags=["Edge Clusters"],
)
async def get_live_status(cluster_id: UUID, db: Session = Depends(get_db)):
    """
    Query the K8s API directly for real-time node status, resource capacity,
    and addon pod health.  Returns immediately from the live cluster state
    rather than cached DB values.
    """
    cluster = db.query(ManagementCluster).filter(ManagementCluster.id == cluster_id).first()
    if not cluster:
        raise HTTPException(404, "Edge cluster not found")
    if not cluster.kubeconfig_path or not os.path.exists(cluster.kubeconfig_path):
        raise HTTPException(503, "Kubeconfig not available — cluster not yet provisioned")

    from k8s_client import _k8s_get

    def _fetch():
        nodes_raw = _k8s_get(cluster.kubeconfig_path, "/api/v1/nodes") or {}
        pods_raw  = _k8s_get(cluster.kubeconfig_path, "/api/v1/pods?fieldSelector=metadata.namespace!=kube-system") or {}
        sys_pods  = _k8s_get(cluster.kubeconfig_path, "/api/v1/namespaces/kube-system/pods") or {}

        nodes = []
        for item in nodes_raw.get("items", []):
            meta   = item.get("metadata", {})
            spec   = item.get("spec", {})
            status = item.get("status", {})
            conds  = {c["type"]: c["status"] for c in status.get("conditions", [])}
            cap    = status.get("allocatable", status.get("capacity", {}))
            addrs  = {a["type"]: a["address"] for a in status.get("addresses", [])}
            labels = meta.get("labels", {})
            roles  = []
            for lbl in labels:
                if lbl.startswith("node-role.kubernetes.io/"):
                    roles.append(lbl.split("/", 1)[1])
            nodes.append({
                "name":            meta.get("name"),
                "ready":           conds.get("Ready") == "True",
                "roles":           roles or ["worker"],
                "ip":              addrs.get("InternalIP") or addrs.get("ExternalIP"),
                "cpu_allocatable": cap.get("cpu"),
                "ram_allocatable": cap.get("memory"),
                "k8s_version":     status.get("nodeInfo", {}).get("kubeletVersion"),
                "conditions":      [
                    {"type": c["type"], "status": c["status"], "reason": c.get("reason", "")}
                    for c in status.get("conditions", [])
                ],
                "unschedulable":   spec.get("unschedulable", False),
            })

        # Summarise addon pod health from kube-system
        addon_pods: dict[str, list[str]] = {}
        for item in sys_pods.get("items", []):
            labels = item.get("metadata", {}).get("labels", {})
            app_name = (
                labels.get("app.kubernetes.io/name")
                or labels.get("app")
                or labels.get("k8s-app")
            )
            if not app_name:
                continue
            phase = item.get("status", {}).get("phase", "Unknown")
            addon_pods.setdefault(app_name, []).append(phase)

        addon_statuses = {}
        for name, phases in addon_pods.items():
            if all(p == "Running" for p in phases):
                addon_statuses[name] = "ok"
            elif any(p == "Running" for p in phases):
                addon_statuses[name] = "degraded"
            else:
                addon_statuses[name] = "missing"

        return {"nodes": nodes, "addon_statuses": addon_statuses}

    result = await asyncio.to_thread(_fetch)

    # Persist the live snapshot back into the DB so the overview tab stays fresh
    cluster.nodes_status  = result["nodes"]
    cluster.addon_statuses = result["addon_statuses"]
    db.commit()

    return result


# ── Delete node from cluster ──────────────────────────────────────────────────

@app.delete(
    f"{settings.api_prefix}/edge-clusters/{{cluster_id}}/nodes/{{node_name}}",
    status_code=200,
    tags=["Edge Clusters"],
)
async def delete_node_from_cluster(
    cluster_id: UUID,
    node_name: str,
    db: Session = Depends(get_db),
):
    """
    Drain and delete a node from the edge cluster.

    Steps:
    1. kubectl cordon + drain (evicts pods gracefully, ignores DaemonSets)
    2. kubectl delete node
    3. Remove the node from cluster.nodes in the DB
    """
    cluster = db.query(ManagementCluster).filter(ManagementCluster.id == cluster_id).first()
    if not cluster:
        raise HTTPException(404, "Edge cluster not found")
    if not cluster.kubeconfig_path or not os.path.exists(cluster.kubeconfig_path):
        raise HTTPException(503, "Kubeconfig not available")

    kc = cluster.kubeconfig_path
    env = {**os.environ, "KUBECONFIG": kc}

    def _drain_and_delete():
        errors = []
        # Cordon first (marks unschedulable)
        r = subprocess.run(
            ["kubectl", "cordon", node_name],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            errors.append(f"cordon: {r.stderr.strip()}")

        # Drain: evict pods, ignore DaemonSets, force-delete stuck pods
        r = subprocess.run(
            [
                "kubectl", "drain", node_name,
                "--ignore-daemonsets", "--delete-emptydir-data",
                "--force", "--timeout=120s",
            ],
            env=env, capture_output=True, text=True, timeout=150,
        )
        if r.returncode != 0:
            errors.append(f"drain: {r.stderr.strip()}")

        # Delete the node object
        r = subprocess.run(
            ["kubectl", "delete", "node", node_name],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0 and "not found" not in r.stderr:
            errors.append(f"delete: {r.stderr.strip()}")

        return errors

    errors = await asyncio.to_thread(_drain_and_delete)

    # Remove from cluster.nodes DB record regardless of kubectl errors
    # (the node may already be gone from the cluster's perspective)
    cluster.nodes = [n for n in (cluster.nodes or []) if n.get("name") != node_name and n.get("host") != node_name]
    cluster.nodes_status = [n for n in (cluster.nodes_status or []) if n.get("name") != node_name]
    db.commit()

    if errors:
        logger.warning(f"Node deletion for {node_name} completed with warnings: {errors}")

    return {
        "node": node_name,
        "status": "deleted",
        "warnings": errors,
    }


# Import datetime for health check
from datetime import datetime

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug
    )
