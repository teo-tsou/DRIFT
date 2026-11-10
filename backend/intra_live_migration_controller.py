"""
Intra-cluster Live Migration Controller

Handles KubeVirt VM live migration within a management cluster:
  1. Node resource preflight check (3-tier: metrics-server > kubelet stats > pod requests)
  2. Target node selection (via VMI nodeSelector patch)
  3. VirtualMachineInstanceMigration CR creation (KubeVirt-native trigger)

All operations target the management cluster via its kubeconfig.
"""

import time
from typing import Optional
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from k8s_client import _k8s_get, _k8s_post


# ---------------------------------------------------------------------------
# Kubernetes quantity parsers
# ---------------------------------------------------------------------------

def _parse_cpu_to_millicores(cpu_str: str) -> int:
    """
    Parse a K8s CPU quantity string to millicores (int).
      "2"            → 2000
      "500m"         → 500
      "1500m"        → 1500
      "1005397470n"  → 1005  (nanocores from metrics-server)
    """
    s = str(cpu_str).strip()
    if s.endswith("n"):
        # nanocores → millicores (divide by 1_000_000)
        return max(1, int(float(s[:-1])) // 1_000_000)
    if s.endswith("m"):
        return int(float(s[:-1]))
    # Whole cores (may be float string like "1.5")
    return int(float(s) * 1000)


def _parse_mem_to_mib(mem_str: str) -> int:
    """
    Parse a K8s memory quantity string to MiB (int).
      "16Gi"       → 16384
      "512Mi"      → 512
      "1073741824" → 1024   (bytes)
      "1024Ki"     → 1
      "16222592Ki" → 15842  (typical node allocatable)
    """
    s = str(mem_str).strip()
    if s.endswith("Ki"):
        return max(1, int(float(s[:-2])) // 1024)
    if s.endswith("Mi"):
        return int(float(s[:-2]))
    if s.endswith("Gi"):
        return int(float(s[:-2]) * 1024)
    if s.endswith("Ti"):
        return int(float(s[:-2]) * 1024 * 1024)
    if s.endswith("K") or s.endswith("k"):
        return max(1, int(float(s[:-1])) // 1024)
    if s.endswith("M"):
        return int(float(s[:-1]))
    if s.endswith("G"):
        return int(float(s[:-1]) * 1024)
    # Raw bytes
    try:
        return max(1, int(float(s)) // (1024 * 1024))
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Node resource inspection — 3-tier
# ---------------------------------------------------------------------------

def _get_usage_from_metrics_server(kubeconfig_path: str, node_name: str) -> Optional[dict]:
    """
    Tier 1: Metrics-server API — real OS-level usage (same as kubectl top nodes).
    Returns {cpu_m, mem_mi} or None if metrics-server unavailable.
    """
    data = _k8s_get(kubeconfig_path, f"/apis/metrics.k8s.io/v1beta1/nodes/{node_name}")
    if not data:
        return None
    usage = data.get("usage", {})
    cpu_str = usage.get("cpu", "")
    mem_str = usage.get("memory", "")
    if not cpu_str or not mem_str:
        return None
    return {
        "cpu_m": _parse_cpu_to_millicores(cpu_str),
        "mem_mi": _parse_mem_to_mib(mem_str),
    }


def _get_usage_from_kubelet_stats(kubeconfig_path: str, node_name: str) -> Optional[dict]:
    """
    Tier 2: Kubelet stats/summary — direct from cAdvisor, always available.
    Returns {cpu_m, mem_mi} or None on failure.
    """
    data = _k8s_get(kubeconfig_path, f"/api/v1/nodes/{node_name}/proxy/stats/summary")
    if not data:
        return None
    node_stats = data.get("node", {})
    cpu_nano = node_stats.get("cpu", {}).get("usageNanoCores", 0)
    mem_bytes = node_stats.get("memory", {}).get("workingSetBytes", 0)
    if not cpu_nano and not mem_bytes:
        return None
    return {
        "cpu_m": int(cpu_nano / 1_000_000),  # nanocores → millicores
        "mem_mi": int(mem_bytes / (1024 * 1024)),
    }


def _get_usage_from_pod_requests(kubeconfig_path: str, node_name: str) -> dict:
    """
    Tier 3 (always works): Sum resource requests of all pods on the node.
    Underestimates (misses VMs overhead, system daemons), but is a safe lower bound.
    """
    encoded = node_name.replace("/", "%2F")
    pods = _k8s_get(
        kubeconfig_path,
        f"/api/v1/pods?fieldSelector=spec.nodeName%3D{encoded}%2Cstatus.phase%3DRunning",
    )
    cpu_total_m = 0
    mem_total_mi = 0
    for pod in (pods or {}).get("items", []):
        for container in pod.get("spec", {}).get("containers", []):
            req = container.get("resources", {}).get("requests", {})
            if req.get("cpu"):
                cpu_total_m += _parse_cpu_to_millicores(req["cpu"])
            if req.get("memory"):
                mem_total_mi += _parse_mem_to_mib(req["memory"])
    return {"cpu_m": cpu_total_m, "mem_mi": mem_total_mi}


def get_node_available_resources(kubeconfig_path: str, node_name: str) -> dict:
    """
    Get available CPU (millicores) and memory (MiB) on a node.

    Tries in order:
      1. Metrics-server  — real OS-level usage (captures VMs, daemons, buffers)
      2. Kubelet stats   — direct from cAdvisor, always available
      3. Pod requests    — static estimate, always works

    Returns:
      {
        allocatable_cpu_m:  int,   # node's total allocatable CPU in millicores
        allocatable_mem_mi: int,   # node's total allocatable memory in MiB
        used_cpu_m:         int,
        used_mem_mi:        int,
        available_cpu_m:    int,   # allocatable - used
        available_mem_mi:   int,
        source:             str,   # "metrics-server" | "kubelet-stats" | "pod-requests"
      }
    """
    node_data = _k8s_get(kubeconfig_path, f"/api/v1/nodes/{node_name}")
    if not node_data:
        return {}

    allocatable = node_data.get("status", {}).get("allocatable", {})
    alloc_cpu_m = _parse_cpu_to_millicores(allocatable.get("cpu", "0"))
    alloc_mem_mi = _parse_mem_to_mib(allocatable.get("memory", "0"))
    alloc_disk_gi = round(_parse_mem_to_mib(allocatable.get("ephemeral-storage", "0")) / 1024, 1)

    # Always compute pod requests — this is what the scheduler uses for admission.
    # Actual OS-level usage can be lower than requests (overcommit), so we must
    # take the maximum of (actual, requested) to avoid false-positive feasibility.
    pod_req = _get_usage_from_pod_requests(kubeconfig_path, node_name)
    req_cpu_m  = pod_req["cpu_m"]
    req_mem_mi = pod_req["mem_mi"]

    # Also get real usage from metrics-server / kubelet for context
    actual = _get_usage_from_metrics_server(kubeconfig_path, node_name)
    source = "metrics-server+pod-requests"
    if not actual:
        actual = _get_usage_from_kubelet_stats(kubeconfig_path, node_name)
        source = "kubelet-stats+pod-requests"
    if not actual:
        actual = pod_req
        source = "pod-requests"

    # Used = max(actual, requested) — matches what k8s scheduler sees
    used_cpu_m  = max(actual["cpu_m"],  req_cpu_m)
    used_mem_mi = max(actual["mem_mi"], req_mem_mi)

    return {
        "allocatable_cpu_m": alloc_cpu_m,
        "allocatable_mem_mi": alloc_mem_mi,
        "allocatable_disk_gi": alloc_disk_gi,
        "used_cpu_m": used_cpu_m,
        "used_mem_mi": used_mem_mi,
        "available_cpu_m": max(0, alloc_cpu_m - used_cpu_m),
        "available_mem_mi": max(0, alloc_mem_mi - used_mem_mi),
        "source": source,
    }


# ---------------------------------------------------------------------------
# Node listing for migration target selection
# ---------------------------------------------------------------------------

def list_nodes_for_migration(
    kubeconfig_path: str, exclude_node: Optional[str] = None
) -> list:
    """
    List all Ready nodes with available resource data, excluding the source node.

    Returns list of:
      {name, ip, roles, ready, allocatable_cpu_m, allocatable_mem_mi,
       used_cpu_m, used_mem_mi, available_cpu_m, available_mem_mi, source}
    """
    nodes_data = _k8s_get(kubeconfig_path, "/api/v1/nodes")
    if not nodes_data:
        return []

    result = []
    for item in nodes_data.get("items", []):
        meta = item.get("metadata", {})
        name = meta.get("name", "")

        # Skip source node
        if exclude_node and name == exclude_node:
            continue

        # Only Ready nodes
        conditions = item.get("status", {}).get("conditions", [])
        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), {})
        if ready_cond.get("status") != "True":
            continue

        # IP
        ip = ""
        for addr in item.get("status", {}).get("addresses", []):
            if addr.get("type") == "InternalIP":
                ip = addr.get("address", "")
                break

        # Roles
        labels = meta.get("labels", {})
        roles = []
        for k in labels:
            if k.startswith("node-role.kubernetes.io/"):
                role = k.split("/", 1)[1]
                roles.append(role)
        if not roles:
            roles = ["worker"]

        resources = get_node_available_resources(kubeconfig_path, name)

        result.append({
            "name": name,
            "ip": ip,
            "roles": roles,
            **resources,
        })

    return result


# ---------------------------------------------------------------------------
# Feasibility check
# ---------------------------------------------------------------------------

def check_migration_feasibility(
    kubeconfig_path: str,
    target_node: str,
    vm_cpu_cores: int,
    vm_ram_str: str,
) -> tuple:
    """
    Check whether target_node has sufficient free resources for the VM.

    Checks:
      1. KVM device slots (hard limit — node can't accept more VMs if full)
      2. Available memory (scheduler-view: max of actual usage vs pod requests)
      3. Available CPU

    Args:
      vm_cpu_cores: int — e.g. 2
      vm_ram_str:   str — K8s quantity, e.g. "2Gi", "512Mi"

    Returns:
      (True, "")  if feasible
      (False, "reason string")  if not feasible
    """
    node_data = _k8s_get(kubeconfig_path, f"/api/v1/nodes/{target_node}")
    if not node_data:
        return False, f"Could not retrieve resource data for node '{target_node}'"

    # ── KVM device check ────────────────────────────────────────────────────
    allocatable = node_data.get("status", {}).get("allocatable", {})
    def _parse_int_quantity(v) -> int:
        """Parse a K8s integer resource quantity: '1', '1k', '1K' → int."""
        s = str(v).strip()
        if s.endswith(('k', 'K')):
            return int(float(s[:-1]) * 1000)
        try:
            return int(s)
        except ValueError:
            return 0
    kvm_allocatable = _parse_int_quantity(allocatable.get("devices.kubevirt.io/kvm", 0))
    if kvm_allocatable > 0:
        # Count pods already consuming a KVM device on this node
        encoded = target_node.replace("/", "%2F")
        pods = _k8s_get(
            kubeconfig_path,
            f"/api/v1/pods?fieldSelector=spec.nodeName%3D{encoded}%2Cstatus.phase%3DRunning",
        )
        kvm_used = 0
        for pod in (pods or {}).get("items", []):
            for container in pod.get("spec", {}).get("containers", []):
                req = container.get("resources", {}).get("requests", {})
                if "devices.kubevirt.io/kvm" in req:
                    kvm_used += _parse_int_quantity(req["devices.kubevirt.io/kvm"])
        kvm_free = kvm_allocatable - kvm_used
        if kvm_free < 1:
            return False, (
                f"Node '{target_node}' has no free KVM device slots "
                f"({kvm_used}/{kvm_allocatable} used). Cannot host another VM."
            )

    # ── CPU / memory check ──────────────────────────────────────────────────
    resources = get_node_available_resources(kubeconfig_path, target_node)
    if not resources:
        return False, f"Could not retrieve resource data for node '{target_node}'"

    required_cpu_m  = vm_cpu_cores * 1000
    required_mem_mi = _parse_mem_to_mib(vm_ram_str)
    available_cpu_m  = resources["available_cpu_m"]
    available_mem_mi = resources["available_mem_mi"]
    source = resources["source"]

    cpu_ok = available_cpu_m >= required_cpu_m
    mem_ok = available_mem_mi >= required_mem_mi

    if cpu_ok and mem_ok:
        return True, ""

    parts = []
    if not cpu_ok:
        parts.append(f"CPU: {available_cpu_m}m available, {required_cpu_m}m required")
    if not mem_ok:
        parts.append(f"Memory: {available_mem_mi}Mi available, {required_mem_mi}Mi required")

    return False, (
        f"Node '{target_node}' has insufficient resources "
        f"({'; '.join(parts)}) [measured via {source}]"
    )


# ---------------------------------------------------------------------------
# PATCH helper (merge-patch, reuses cluster_provisioner cert logic)
# ---------------------------------------------------------------------------

def _k8s_patch_merge(kubeconfig_path: str, path: str, body: dict) -> Optional[dict]:
    """
    MERGE-PATCH a K8s resource via the management cluster API.
    Reads kubeconfig from file (same pattern as cluster_reconciler helpers).
    """
    import base64
    import tempfile
    import yaml
    import requests as _req
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        with open(kubeconfig_path) as f:
            kc = yaml.safe_load(f)
        cluster_info = kc["clusters"][0]["cluster"]
        server = cluster_info["server"]
        user_info = kc["users"][0]["user"]
        cert = user_info.get("client-certificate-data")
        key = user_info.get("client-key-data")

        cert_file = key_file = ca_file = None
        if cert and key:
            ct = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
            ct.write(base64.b64decode(cert)); ct.flush(); cert_file = ct.name
            kt = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
            kt.write(base64.b64decode(key)); kt.flush(); key_file = kt.name
        ca_data = cluster_info.get("certificate-authority-data")
        if ca_data:
            cat = tempfile.NamedTemporaryFile(delete=False, suffix=".ca")
            cat.write(base64.b64decode(ca_data)); cat.flush(); ca_file = cat.name

        resp = _req.patch(
            f"{server}{path}",
            json=body,
            headers={"Content-Type": "application/merge-patch+json"},
            cert=(cert_file, key_file) if cert_file else None,
            verify=ca_file if ca_file else False,
            timeout=15,
        )
        for fname in [cert_file, key_file, ca_file]:
            if fname:
                try:
                    os.unlink(fname)
                except Exception:
                    pass

        if resp.status_code in (200, 201, 202):
            return resp.json()
        print(f"[WARN] PATCH {path} → HTTP {resp.status_code}: {resp.text[:300]}")
        return None
    except Exception as e:
        print(f"[ERROR] _k8s_patch_merge {path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Live migration trigger
# ---------------------------------------------------------------------------

def trigger_live_migration(
    kubeconfig_path: str,
    vm_name: str,
    namespace: str,
    target_node: str,
) -> tuple:
    """
    Trigger a KubeVirt live migration to target_node.

    Steps:
      1. PATCH the VMI spec.nodeSelector to pin the destination node.
         This directs KubeVirt's migration scheduler to the right host.
      2. POST a VirtualMachineInstanceMigration CR to start the migration.
      3. Schedule cleanup of the nodeSelector after migration starts
         (done async — the migration controller handles the rest).

    Returns:
      (True, migration_name)   on success
      (False, error_reason)    on failure
    """
    # Step 1: Verify the VMI exists and is running
    vmi_path = f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstances/{vm_name}"
    vmi = _k8s_get(kubeconfig_path, vmi_path)
    if not vmi:
        return False, f"VMI '{vm_name}' not found in namespace '{namespace}' — is the VM running?"

    # Step 2: Patch the VM object (not the VMI) to set nodeSelector.
    # KubeVirt's admission webhook blocks direct VMI spec patches on running instances,
    # but VM object patches are permitted. KubeVirt uses the VM template nodeSelector
    # when scheduling the migration target pod.
    vm_path = f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines/{vm_name}"
    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "nodeSelector": {
                        "kubernetes.io/hostname": target_node
                    }
                }
            }
        }
    }
    patched = _k8s_patch_merge(kubeconfig_path, vm_path, patch_body)
    if not patched:
        return False, f"Failed to set nodeSelector on VM '{vm_name}'"

    # Step 3: Create VirtualMachineInstanceMigration CR
    migration_name = f"{vm_name}-migration-{int(time.time())}"
    vmim_body = {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachineInstanceMigration",
        "metadata": {
            "name": migration_name,
            "namespace": namespace,
        },
        "spec": {
            "vmiName": vm_name,
        },
    }
    vmim_path = f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachineinstancemigrations"
    result = _k8s_post(kubeconfig_path, vmim_path, vmim_body)
    if not result:
        # Roll back nodeSelector patch on the VM
        _k8s_patch_merge(kubeconfig_path, vm_path, {"spec": {"template": {"spec": {"nodeSelector": None}}}})
        return False, f"Failed to create VirtualMachineInstanceMigration for VM '{vm_name}'"

    print(f"[SUCCESS] Live migration '{migration_name}' created: {vm_name} → {target_node}")
    return True, migration_name


def clear_vmi_node_selector(
    kubeconfig_path: str, vm_name: str, namespace: str
) -> bool:
    """
    Remove the nodeSelector from the VM object after migration completes.
    Called post-migration so the VM can be freely migrated again in future.
    """
    vm_path = f"/apis/kubevirt.io/v1/namespaces/{namespace}/virtualmachines/{vm_name}"
    result = _k8s_patch_merge(kubeconfig_path, vm_path, {"spec": {"template": {"spec": {"nodeSelector": None}}}})
    return result is not None
