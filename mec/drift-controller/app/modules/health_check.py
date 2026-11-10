"""Pre/post migration VM health verification, via the DRIFT backend's all-vms view."""

from app.core.drift_backend_client import get_vm


async def assert_vm_running(cluster_id: str, namespace: str, vm_name: str) -> None:
    vm = await get_vm(cluster_id, namespace, vm_name)
    if not vm:
        raise RuntimeError(f"VM '{vm_name}' not found on cluster '{cluster_id}'")
    if vm.get("phase") != "Running":
        raise RuntimeError(f"VM '{vm_name}' is not Running (phase={vm.get('phase')})")
