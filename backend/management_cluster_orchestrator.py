"""
Management Cluster Orchestrator

Handles the complete workflow for provisioning management clusters:
1. Generates Ansible inventory from user input
2. Prepares nodes (packages, K3s binary pre-stage)
3. Installs K3s server directly via Ansible
4. Joins worker nodes using the K3s node token
5. Installs addons (Longhorn, KubeVirt, Multus, Monitoring)
6. Extracts and stores kubeconfig
"""

import asyncio
import os
import tempfile
import yaml
import httpx
from typing import Dict, Any, Optional
from uuid import UUID
from datetime import datetime
from sqlalchemy.orm import Session

from models import ManagementCluster, ManagementClusterStatus, DeploymentLog


def _summarise_ansible_output(stdout_str: str, stderr_str: str):
    """
    Extract a concise summary from Ansible output.
    Returns (summary: str, raw: str).
    summary: PLAY RECAP block + any fatal/unreachable lines, capped at 800 chars.
    raw: full combined output for storage in details["raw_output"].
    """
    lines = stdout_str.splitlines()

    recap_lines = []
    in_recap = False
    for line in lines:
        if "PLAY RECAP" in line:
            in_recap = True
        if in_recap:
            recap_lines.append(line)
            if in_recap and recap_lines and line.strip() == "" and len(recap_lines) > 2:
                break

    # The fatal/error lines carry the ACTUAL cause (a 403 repo, a missing role,
    # a failed command). Capture them WITH a little following context (msg:, the
    # url:/status_code: fields ansible prints under a fatal), then the recap.
    important = []
    for i, l in enumerate(lines):
        if any(k in l for k in ("fatal:", "failed:", "UNREACHABLE!", "FAILED!", "ERROR!")):
            important.append(l)
            important.extend(lines[i + 1:i + 6])  # the msg/url/status detail block

    parts = important + (["", *recap_lines] if recap_lines else [])
    summary = "\n".join(parts).strip() or "(no output)"
    if len(summary) > 1800:
        summary = summary[:1800] + "…"

    raw = stdout_str
    if stderr_str.strip():
        raw += "\n--- STDERR ---\n" + stderr_str

    return summary, raw


class ManagementClusterOrchestrator:
    def __init__(self, db: Session):
        self.db = db
        self.ansible_path = os.getenv("ANSIBLE_PATH", "/app/ansible")
        if not os.path.exists(self.ansible_path):
            self.ansible_path = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "../ansible")
            )

    async def provision_cluster(self, cluster_id: UUID) -> bool:
        """
        Complete orchestration workflow for management cluster creation.
        """
        cluster = self.db.query(ManagementCluster).filter(
            ManagementCluster.id == cluster_id
        ).first()

        if not cluster:
            return False

        inventory_path = None

        try:
            # Phase 0: Pre-flight checks
            await self._update_status(
                cluster, ManagementClusterStatus.VALIDATING,
                "Validating configuration", 5
            )
            await self._validate_prerequisites(cluster)

            # Phase 1: Generate Ansible inventory
            await self._update_status(
                cluster, ManagementClusterStatus.PROVISIONING,
                "Generating Ansible inventory", 10
            )
            inventory_path = await self._generate_inventory(cluster)

            # Phase 2: Prepare ALL nodes in parallel (packages + K3s binary pre-stage)
            await self._update_status(
                cluster, ManagementClusterStatus.PROVISIONING,
                "Preparing all nodes (packages + K3s binary pre-stage)", 15
            )
            await self._prepare_all_nodes(cluster, inventory_path)

            # Phase 2b: L2 bridge/VLAN networking (if configured)
            has_l2_networks = any(
                "interface_mappings" in node and node["interface_mappings"]
                for node in cluster.nodes
            )
            if has_l2_networks or cluster.l2_networks:
                await self._update_status(
                    cluster, ManagementClusterStatus.CONFIGURING_NETWORK,
                    "Setting up L2 networks and bridges on nodes", 20
                )
                await self._configure_networking(cluster, inventory_path)
            else:
                self._log(cluster, "INFO", "No L2 networks configured, skipping network setup")

            # Phase 3: Install K3s server directly
            await self._update_status(
                cluster, ManagementClusterStatus.PROVISIONING,
                "Installing K3s server", 35
            )
            await self._execute_cluster_creation(cluster, inventory_path)

            # Extract kubeconfig immediately after K3s server is up
            kubeconfig_path = await self._extract_kubeconfig(cluster)
            cluster.kubeconfig_path = kubeconfig_path
            self.db.commit()

            # Phase 3b: Install KubeOVN CNI
            # Runs on ALL nodes (server + workers) before workers join K3s:
            # - masks host OVS/OVN on every node so DaemonSet pods won't conflict
            # - deploys KubeOVN to the cluster (run_once on server via kubectl)
            # Workers joining after this find OVS already masked and the DaemonSet
            # ready to schedule onto them immediately.
            await self._update_status(
                cluster, ManagementClusterStatus.CONFIGURING_NETWORK,
                "Installing KubeOVN CNI", 55
            )
            await self._install_kube_ovn(cluster, inventory_path)

            # Phase 3c: Register worker nodes if any
            has_workers = any("agent" in n.get("roles", []) for n in cluster.nodes)
            if has_workers:
                await self._update_status(
                    cluster, ManagementClusterStatus.PROVISIONING,
                    "Registering worker nodes", 65
                )
                await self._register_workers(cluster, inventory_path)

            # Phase 4: Install addons in parallel
            await self._update_status(
                cluster, ManagementClusterStatus.INSTALLING_ADDONS,
                "Installing addons (parallel execution)", 70
            )

            addon_pairs = []
            if cluster.enable_multus:
                addon_pairs.append(("Multus", self._install_multus(cluster, inventory_path)))
            if cluster.enable_longhorn:
                addon_pairs.append(("Longhorn", self._install_longhorn(cluster, inventory_path)))
            if cluster.enable_kubevirt:
                addon_pairs.append(("KubeVirt", self._install_kubevirt(cluster, inventory_path)))
            if cluster.enable_monitoring:
                addon_pairs.append(("Monitoring", self._install_monitoring(cluster, inventory_path)))

            failed_addons = []
            succeeded_addons = []

            if addon_pairs:
                statuses = {name: "installing" for name, _ in addon_pairs}
                cluster.addon_statuses = statuses
                self.db.commit()

                self._log(cluster, "INFO", f"Installing {len(addon_pairs)} addons in parallel...")

                completed = [0]

                async def _run_addon(name, coro):
                    try:
                        await coro
                        statuses[name] = "installed"
                        succeeded_addons.append(name)
                        self._log(cluster, "INFO", f"Addon {name} installed successfully")
                    except Exception as exc:
                        statuses[name] = "failed"
                        failed_addons.append(name)
                        self._log(cluster, "ERROR", f"Addon {name} installation failed: {exc}")
                    finally:
                        completed[0] += 1
                        cluster.addon_statuses = dict(statuses)
                        cluster.progress_percent = 70 + int((completed[0] / len(addon_pairs)) * 30)
                        self.db.commit()

                await asyncio.gather(*[_run_addon(n, c) for n, c in addon_pairs])

                if failed_addons:
                    self._log(cluster, "WARNING", f"Addons completed with failures — succeeded: {succeeded_addons}, failed: {failed_addons}")
                else:
                    self._log(cluster, "INFO", f"All {len(succeeded_addons)} addons installed successfully")

            # Phase 5: CRIU pod live-migration layer — only on CRI-O clusters
            # (containerd can checkpoint but not restore). Installs crun-drift +
            # node-agent (shared token) + macvlan-l2 NAD + the hub's NFS ckpt store.
            container_runtime = (cluster.meta_data or {}).get("container_runtime", "containerd")
            if container_runtime == "crio":
                await self._update_status(
                    cluster, ManagementClusterStatus.INSTALLING_ADDONS,
                    "Installing pod live-migration layer (crun-drift, node-agent, macvlan, ckpt store)", 95
                )
                try:
                    await self._install_pod_migration(cluster, inventory_path)
                    self._log(cluster, "INFO", "Pod live-migration layer installed")
                except Exception as exc:
                    self._log(cluster, "WARNING", f"Pod live-migration layer failed: {exc}")
                    failed_addons.append("PodMigration")

            cluster.status = ManagementClusterStatus.ACTIVE
            cluster.provisioned_at = datetime.utcnow()
            if failed_addons:
                cluster.current_phase = f"Active (failed addons: {', '.join(failed_addons)})"
                cluster.progress_percent = 70 + int((len(succeeded_addons) / len(addon_pairs)) * 30) if addon_pairs else 90
                self._log(cluster, "WARNING", f"Cluster active but {len(failed_addons)} addon(s) failed: {failed_addons}")
            else:
                cluster.current_phase = "Cluster ready"
                cluster.progress_percent = 100
                self._log(cluster, "INFO", "Management cluster provisioned successfully")
            self.db.commit()
            return True

        except Exception as e:
            cluster.status = ManagementClusterStatus.FAILED
            cluster.error_message = str(e)
            cluster.current_phase = "Provisioning failed"
            self.db.commit()
            self._log(cluster, "ERROR", f"Provisioning failed: {str(e)}")
            return False

        finally:
            if inventory_path and os.path.exists(inventory_path):
                try:
                    os.remove(inventory_path)
                    inventory_dir = os.path.dirname(inventory_path)
                    if os.path.exists(inventory_dir):
                        os.rmdir(inventory_dir)
                except Exception as e:
                    self._log(cluster, "WARNING", f"Failed to clean up inventory: {e}")

    async def _generate_inventory(self, cluster: ManagementCluster) -> str:
        """Generate Ansible inventory file from cluster configuration."""
        inventory_data = {
            "all": {
                "vars": {
                    "cluster_name": cluster.name,
                    "cluster_type": cluster.cluster_type,
                    "region": cluster.meta_data.get("region", "default-region")
                        if cluster.meta_data else "default-region"
                }
            }
        }

        if cluster.kubernetes_version:
            inventory_data["all"]["vars"]["kubernetes_version"] = cluster.kubernetes_version

        if cluster.cni_plugin:
            inventory_data["all"]["vars"]["cni"] = cluster.cni_plugin

        if cluster.enable_longhorn:
            inventory_data["all"]["vars"]["install_longhorn"] = True
        if cluster.enable_kubevirt:
            inventory_data["all"]["vars"]["install_kubevirt"] = True
        if cluster.enable_multus:
            inventory_data["all"]["vars"]["install_multus"] = True
        if cluster.enable_monitoring:
            inventory_data["all"]["vars"]["install_monitoring"] = True

        if cluster.nodes and "ssh_key_path" in cluster.nodes[0] and cluster.nodes[0]["ssh_key_path"]:
            inventory_data["all"]["vars"]["ansible_ssh_private_key_file"] = \
                cluster.nodes[0]["ssh_key_path"]

        # Resolve L2 networks from node interface_mappings
        l2_network_ids = set()
        for node in cluster.nodes:
            if "interface_mappings" in node and node["interface_mappings"]:
                for mapping in node["interface_mappings"]:
                    if isinstance(mapping, dict) and "l2_network_id" in mapping:
                        l2_network_ids.add(mapping["l2_network_id"])

        if l2_network_ids:
            from models import L2Network
            from uuid import UUID

            uuid_ids = []
            for net_id in l2_network_ids:
                if isinstance(net_id, str):
                    uuid_ids.append(UUID(net_id))
                else:
                    uuid_ids.append(net_id)

            networks = self.db.query(L2Network).filter(L2Network.id.in_(uuid_ids)).all()

            l2_networks_dict = {}
            network_id_to_name = {}

            for network in networks:
                l2_networks_dict[network.name] = {
                    "vlan_id": network.vlan_id,
                    "subnet": network.subnet,
                    "gateway": network.gateway,
                    "bridge_name": network.bridge_name
                }
                network_id_to_name[str(network.id)] = network.name

            inventory_data["all"]["vars"]["l2_networks"] = l2_networks_dict

            for node in cluster.nodes:
                if "interface_mappings" in node and node["interface_mappings"]:
                    converted_mappings = []
                    for mapping in node["interface_mappings"]:
                        if isinstance(mapping, dict) and "l2_network_id" in mapping:
                            net_id = str(mapping["l2_network_id"])
                            if net_id in network_id_to_name:
                                converted_mappings.append({
                                    "interface": mapping["interface"],
                                    "l2_network": network_id_to_name[net_id]
                                })
                    node["interface_mappings"] = converted_mappings

        elif cluster.l2_networks:
            inventory_data["all"]["vars"]["l2_networks"] = cluster.l2_networks

        # Build node inventory
        management_nodes = {}
        for node in cluster.nodes:
            node_config = {
                "ansible_host": node["host"],
                "ansible_user": node["user"],
                "node_roles": node["roles"]
            }

            if "interface_mappings" in node:
                node_config["interface_mappings"] = node["interface_mappings"]

            if "ssh_key_path" in node and node["ssh_key_path"]:
                node_config["ansible_ssh_private_key_file"] = node["ssh_key_path"]

            # The address K3s binds and advertises, when it must not be the one
            # on the default route (the vehicle Jetson: tunnel over 5G, not LAN).
            if node.get("node_ip"):
                node_config["node_ip"] = node["node_ip"]

            management_nodes[node["name"]] = node_config

        inventory_data["all"]["children"] = {
            "edge_regions": {
                "children": {
                    "management_nodes": {
                        "hosts": management_nodes
                    }
                }
            }
        }

        inventory_dir = tempfile.mkdtemp(prefix="drift_inventory_")
        inventory_path = os.path.join(inventory_dir, "hosts.yml")

        with open(inventory_path, 'w') as f:
            yaml.dump(inventory_data, f, default_flow_style=False)

        self._log(cluster, "INFO", f"Generated inventory at {inventory_path}")
        return inventory_path

    async def _execute_cluster_creation(
        self,
        cluster: ManagementCluster,
        inventory_path: str
    ):
        """Install K3s server directly via Ansible (no Rancher CAPI)."""
        playbook_path = os.path.join(self.ansible_path, "playbooks/provision_k3s_server.yml")

        extra_vars = {
            "pod_cidr": cluster.pod_cidr,
            "service_cidr": cluster.service_cidr,
            # "containerd" (default) or "crio" — when crio, provision_k3s_server
            # installs CRIU + CRI-O and points K3s's kubelet at the CRI-O socket
            # (enables container checkpoint/restore for pod live-migration).
            "container_runtime": (cluster.meta_data or {}).get("container_runtime", "containerd"),
        }
        if cluster.kubernetes_version:
            extra_vars["kubernetes_version"] = cluster.kubernetes_version
            # CRI-O must match the Kubernetes minor (k3s v1.34.5+k3s1 -> CRI-O v1.34),
            # otherwise the cri_o role installs a mismatched CRI-O from pkgs.k8s.io.
            import re as _re
            _m = _re.match(r"v?(\d+)\.(\d+)", cluster.kubernetes_version)
            if _m:
                extra_vars["crio_version"] = f"v{_m.group(1)}.{_m.group(2)}"

        self._log(cluster, "INFO", "Installing K3s server via Ansible")
        await self._run_ansible_playbook(cluster, playbook_path, inventory_path, extra_vars=extra_vars)

        # Read and store the K3s node token for subsequent worker registration
        token = await self._get_k3s_token(cluster)
        meta = dict(cluster.meta_data or {})
        meta["k3s_node_token"] = token
        cluster.meta_data = meta
        self.db.commit()
        self._log(cluster, "INFO", "K3s server provisioned and node token stored")

    async def _get_k3s_token(self, cluster: ManagementCluster) -> str:
        """Read the K3s node token from the server node via SSH."""
        server_node = next(
            (n for n in cluster.nodes if "server" in n.get("roles", [])), None
        )
        if not server_node:
            raise RuntimeError("No server node found to read K3s token")

        ssh_key_arg = []
        if server_node.get("ssh_key_path"):
            ssh_key_arg = ["-i", server_node["ssh_key_path"]]

        cmd = [
            "ssh", *ssh_key_arg,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{server_node['user']}@{server_node['host']}",
            "sudo cat /var/lib/rancher/k3s/server/node-token"
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"Failed to read K3s node token: {stderr.decode()}")

        token = stdout.decode().strip()
        if not token:
            raise RuntimeError("K3s node token is empty — server may not have started correctly")
        return token

    async def _configure_networking(
        self,
        cluster: ManagementCluster,
        inventory_path: str
    ):
        """Run L2 bridge/VLAN networking playbook."""
        playbook_path = os.path.join(self.ansible_path, "playbooks/setup_networking.yml")
        if not os.path.exists(playbook_path):
            self._log(cluster, "WARNING", f"Networking playbook not found: {playbook_path}")
            return
        await self._run_ansible_playbook(cluster, playbook_path, inventory_path)

    async def _prepare_all_nodes(self, cluster: ManagementCluster, inventory_path: str):
        """Run common role + K3s binary pre-stage on ALL nodes in parallel."""
        playbook_path = os.path.join(self.ansible_path, "playbooks/prepare_all_nodes.yml")
        if not os.path.exists(playbook_path):
            self._log(cluster, "WARNING", "prepare_all_nodes.yml not found, skipping node preparation")
            return
        extra_vars = {}
        if cluster.kubernetes_version:
            extra_vars["kubernetes_version"] = cluster.kubernetes_version
        self._log(cluster, "INFO", "Preparing all nodes (server + workers) in parallel")
        await self._run_ansible_playbook(cluster, playbook_path, inventory_path, extra_vars=extra_vars or None)
        self._log(cluster, "INFO", "All nodes prepared (packages installed, K3s binary pre-staged)")

    async def _register_workers(self, cluster: ManagementCluster, inventory_path: str):
        """Join worker/agent nodes to K3s using the stored node token."""
        has_workers = any("agent" in n.get("roles", []) for n in cluster.nodes)
        if not has_workers:
            self._log(cluster, "INFO", "No worker nodes configured, skipping")
            return

        k3s_node_token = (cluster.meta_data or {}).get("k3s_node_token")
        if not k3s_node_token:
            raise RuntimeError(
                "No k3s_node_token stored — K3s server provisioning may not have completed correctly"
            )

        server_node = next(
            (n for n in cluster.nodes if "server" in n.get("roles", [])), None
        )
        if not server_node:
            raise RuntimeError("No server node found for worker registration")

        k3s_server_url = f"https://{server_node['host']}:6443"

        playbook_path = os.path.join(self.ansible_path, "playbooks/join_k3s_workers.yml")
        if not os.path.exists(playbook_path):
            self._log(cluster, "WARNING", "join_k3s_workers.yml not found, skipping worker registration")
            return

        self._log(cluster, "INFO", f"Joining worker nodes to K3s server at {k3s_server_url}")
        await self._run_ansible_playbook(cluster, playbook_path, inventory_path, extra_vars={
            "k3s_server_url": k3s_server_url,
            "k3s_node_token": k3s_node_token,
            "kubernetes_version": cluster.kubernetes_version or "v1.31.5+k3s1",
        })
        self._log(cluster, "INFO", "Worker nodes joined K3s cluster")

    async def _install_kube_ovn(self, cluster: ManagementCluster, inventory_path: str):
        """Install KubeOVN as the primary CNI (must run before any other addon)."""
        playbook_path = os.path.join(self.ansible_path, "playbooks", "deploy_kube_ovn.yml")
        if not os.path.exists(playbook_path):
            self._log(cluster, "WARNING", f"KubeOVN playbook not found: {playbook_path}")
            return
        extra_vars = {
            "pod_cidr": cluster.pod_cidr,
            "svc_cidr": cluster.service_cidr,
            "ovn_networks": [],
        }
        # kube-ovn's join network defaults to 100.64.0.0/16, inside the CGNAT
        # range Tailscale owns: a node running Tailscale drops every packet from
        # 100.64.0.0/10 that does not arrive on tailscale0, so ovn0 never reaches
        # its gateway and the node stays NotReady. Such clusters set another
        # join CIDR in their metadata.
        join_cidr = (cluster.meta_data or {}).get("kubeovn_join_cidr")
        if join_cidr:
            extra_vars["kubeovn_join_cidr"] = join_cidr
        # kube-ovn otherwise takes the default-route NIC as its node interface.
        iface = (cluster.meta_data or {}).get("kubeovn_iface")
        if iface:
            extra_vars["kubeovn_iface"] = iface
        await self._run_ansible_playbook(cluster, playbook_path, inventory_path, extra_vars=extra_vars)
        self._log(cluster, "INFO", "KubeOVN CNI installed successfully")

    async def _install_multus(self, cluster: ManagementCluster, inventory_path: str):
        """Install Multus CNI for multi-network support."""
        playbook_path = os.path.join(self.ansible_path, "playbooks", "deploy_multus.yml")
        if os.path.exists(playbook_path):
            await self._run_ansible_playbook(cluster, playbook_path, inventory_path)
            self._log(cluster, "INFO", "Multus CNI installed successfully")
        else:
            self._log(cluster, "WARNING", f"Multus playbook not found: {playbook_path}")

    async def _install_longhorn(self, cluster: ManagementCluster, inventory_path: str):
        """Install Longhorn distributed storage."""
        playbook_path = os.path.join(self.ansible_path, "playbooks", "deploy_longhorn.yml")
        if os.path.exists(playbook_path):
            await self._run_ansible_playbook(cluster, playbook_path, inventory_path)
            self._log(cluster, "INFO", "Longhorn storage installed successfully")
        else:
            self._log(cluster, "WARNING", f"Longhorn playbook not found: {playbook_path}")

    async def _install_kubevirt(self, cluster: ManagementCluster, inventory_path: str):
        """Install KubeVirt for VM provisioning."""
        playbook_path = os.path.join(self.ansible_path, "playbooks", "deploy_kubevirt.yml")
        if os.path.exists(playbook_path):
            await self._run_ansible_playbook(cluster, playbook_path, inventory_path)
            self._log(cluster, "INFO", "KubeVirt installed successfully")
        else:
            self._log(cluster, "WARNING", f"KubeVirt playbook not found: {playbook_path}")

    async def _install_pod_migration(self, cluster: ManagementCluster, inventory_path: str):
        """CRIU pod live-migration layer (crun-drift + node-agent + macvlan-l2 NAD
        + shared NFS checkpoint store). Runs only on CRI-O clusters. The store is
        the hub's NFS export (DRIFT_CKPT_NFS_SERVER); the node-agent token is
        shared across clusters (DRIFT_NODE_AGENT_TOKEN) so the backend drives
        migrations on either edge with one credential."""
        playbook_path = os.path.join(self.ansible_path, "playbooks", "deploy_pod_migration.yml")
        if not os.path.exists(playbook_path):
            self._log(cluster, "WARNING", f"Pod-migration playbook not found: {playbook_path}")
            return
        meta = cluster.meta_data or {}
        # A cluster that reaches the hub on another path (the vehicle Jetson, via
        # its 5G tunnel) names the hub address on that path.
        nfs_server = meta.get("ckpt_nfs_server") or os.getenv("DRIFT_CKPT_NFS_SERVER", "")
        extra_vars = {
            "ckpt_nfs_server": nfs_server,
            "ckpt_store_manage_nfs": bool(nfs_server),
            "node_agent_token": os.getenv("DRIFT_NODE_AGENT_TOKEN", ""),
        }
        # "" skips the macvlan-l2 NAD: a node with no NIC on the shared L2.
        if "macvlan_nad_master" in meta:
            extra_vars["macvlan_nad_master"] = meta["macvlan_nad_master"]
        await self._run_ansible_playbook(cluster, playbook_path, inventory_path, extra_vars=extra_vars)

    async def wire_mesh(self, clusters, reg: dict = None):
        """Wire N edge clusters into a cross-cluster migration MESH: every cluster
        peers with every other (EVPN full-mesh stretched-L2 for VM IP continuity +
        routes to all peer pod-CIDRs + a CA-trust union + DecentralizedLiveMigration
        gate). Idempotent — re-run whenever the cluster set changes (adding a 3rd
        cluster just re-runs with all three). Kubeconfigs and the shared node-agent
        token are already registered during each cluster's own provisioning, so
        this is the mesh-level network + trust wiring only.

        Runs setup_evpn_l2.yml then prepare_xcluster_migration.yml against an
        `xcluster_pair` group where each host carries peer_hosts = all the others."""
        def _server(c):
            nodes = c.nodes or []
            return next((n for n in nodes if "server" in n.get("roles", [])),
                        nodes[0] if nodes else None)
        if len(clusters) < 2:
            raise RuntimeError("need at least two clusters to wire a mesh")
        servers = {}
        for c in clusters:
            s = _server(c)
            if not s:
                raise RuntimeError(f"cluster {c.name} has no server node")
            servers[c.name] = s
        first = next(iter(servers.values()))
        ssh_key = first.get("ssh_key_path") or "/root/.ssh/id_rsa"
        hosts = {}
        for c in clusters:
            s = servers[c.name]
            hosts[c.name] = {
                "ansible_host": s["host"],
                "cluster_pod_cidr": c.pod_cidr,
                # full mesh: every other cluster is a peer
                "peer_hosts": [o.name for o in clusters if o.name != c.name],
            }
        inventory = {"all": {
            "vars": {
                "ansible_user": first.get("user", "root"),
                "ansible_ssh_private_key_file": ssh_key,
            },
            "children": {"xcluster_pair": {"hosts": hosts}},
        }}
        inv_dir = tempfile.mkdtemp(prefix="drift_mesh_")
        inv_path = os.path.join(inv_dir, "hosts.yml")
        with open(inv_path, "w") as f:
            yaml.dump(inventory, f, default_flow_style=False)
        anchor = clusters[0]  # for logging
        try:
            for label, pb in (("EVPN stretched-L2 mesh", "setup_evpn_l2.yml"),
                              ("cross-cluster migration prep", "prepare_xcluster_migration.yml")):
                path = os.path.join(self.ansible_path, "playbooks", pb)
                if not os.path.exists(path):
                    raise RuntimeError(f"mesh playbook not found: {pb}")
                if reg is not None:
                    reg["phase"] = f"wiring {label}"
                self._log(anchor, "INFO",
                          f"Wiring cross-cluster mesh [{', '.join(c.name for c in clusters)}]: {label}")
                await self._run_ansible_playbook(anchor, path, inv_path)
        finally:
            try:
                os.remove(inv_path)
                os.rmdir(inv_dir)
            except Exception:
                pass

    async def _install_monitoring(self, cluster: ManagementCluster, inventory_path: str):
        """Install the Prometheus metrics stack."""
        playbook_path = os.path.join(self.ansible_path, "playbooks", "deploy_monitoring.yml")
        if os.path.exists(playbook_path):
            try:
                await asyncio.wait_for(
                    self._run_ansible_playbook(cluster, playbook_path, inventory_path),
                    timeout=600,
                )
            except asyncio.TimeoutError:
                self._log(cluster, "WARNING", "Monitoring playbook exceeded 10-minute timeout — marking as failed")
                raise RuntimeError("Monitoring playbook timed out after 600s")
            self._log(cluster, "INFO", "Prometheus stack installed successfully")
        else:
            self._log(cluster, "WARNING", f"Monitoring playbook not found: {playbook_path}")

    async def _run_ansible_playbook(
        self,
        cluster: ManagementCluster,
        playbook_path: str,
        inventory_path: str,
        extra_vars: dict = None,
    ):
        """Execute an Ansible playbook asynchronously."""
        import json as _json
        cmd = [
            "ansible-playbook",
            "-i", inventory_path,
            playbook_path
        ]
        if extra_vars:
            cmd += ["--extra-vars", _json.dumps(extra_vars)]

        self._log(cluster, "INFO", f"Running playbook: {playbook_path}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.ansible_path
        )

        stdout, stderr = await process.communicate()

        stdout_str = stdout.decode() if stdout else ""
        stderr_str = stderr.decode() if stderr else ""

        playbook_name = os.path.basename(playbook_path)
        summary, raw = _summarise_ansible_output(stdout_str, stderr_str)

        if process.returncode != 0:
            error_log = DeploymentLog(
                entity_id=cluster.id,
                entity_type="management_cluster",
                level="ERROR",
                message=f"Failed: {playbook_name} — {summary}",
                details={"raw_output": raw[:50000]},
            )
            self.db.add(error_log)
            self.db.commit()
            raise RuntimeError(f"Playbook {playbook_name} failed: {summary}")

        ok_log = DeploymentLog(
            entity_id=cluster.id,
            entity_type="management_cluster",
            level="INFO",
            message=f"Completed: {playbook_name} — {summary}",
            details={"raw_output": raw[:50000]},
        )
        self.db.add(ok_log)
        self.db.commit()

    async def _extract_kubeconfig(self, cluster: ManagementCluster) -> str:
        """Extract kubeconfig from the management cluster server node via SSH."""
        server_node = next(
            (n for n in cluster.nodes if "server" in n.get("roles", [])),
            None
        )

        if not server_node:
            raise RuntimeError("No server node found in cluster configuration")

        if cluster.cluster_type == "k3s":
            kubeconfig_remote_path = "/etc/rancher/k3s/k3s.yaml"
        else:
            kubeconfig_remote_path = "/etc/rancher/rke2/rke2.yaml"

        kubeconfigs_dir = os.path.join(os.path.dirname(__file__), "kubeconfigs")
        os.makedirs(kubeconfigs_dir, exist_ok=True)

        kubeconfig_local_path = os.path.join(
            kubeconfigs_dir,
            f"{cluster.name}-kubeconfig.yaml"
        )

        ssh_key_arg = []
        if "ssh_key_path" in server_node and server_node["ssh_key_path"]:
            ssh_key_arg = ["-i", server_node["ssh_key_path"]]

        cmd = [
            "ssh",
            *ssh_key_arg,
            "-o", "StrictHostKeyChecking=no",
            f"{server_node['user']}@{server_node['host']}",
            f"sudo cat {kubeconfig_remote_path}"
        ]

        self._log(cluster, "INFO", f"Extracting kubeconfig from {server_node['host']}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            raise RuntimeError(f"Failed to extract kubeconfig: {error_msg}")

        kubeconfig_content = stdout.decode()
        kubeconfig_content = kubeconfig_content.replace(
            "https://127.0.0.1:6443",
            f"https://{server_node['host']}:6443"
        )

        # Patch TLS — K3s cert SAN may not include the external IP
        try:
            kc = yaml.safe_load(kubeconfig_content)
            for cluster_entry in kc.get("clusters", []):
                cluster_entry["cluster"].pop("certificate-authority-data", None)
                cluster_entry["cluster"]["insecure-skip-tls-verify"] = True
            kubeconfig_content = yaml.dump(kc)
        except Exception as patch_err:
            self._log(cluster, "WARNING", f"Could not patch kubeconfig TLS settings: {patch_err}")

        with open(kubeconfig_local_path, 'w') as f:
            f.write(kubeconfig_content)

        self._log(cluster, "INFO", f"Kubeconfig saved to {kubeconfig_local_path}")
        return kubeconfig_local_path

    async def _validate_prerequisites(self, cluster: ManagementCluster):
        """Validate prerequisites before starting provisioning."""
        process = await asyncio.create_subprocess_exec(
            "which", "ansible-playbook",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        if process.returncode != 0:
            raise RuntimeError("Ansible not installed on the system")

        process = await asyncio.create_subprocess_exec(
            "which", "python3",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        if process.returncode != 0:
            raise RuntimeError("Python3 not installed on the system")

        for node in cluster.nodes:
            ssh_key_arg = []
            if "ssh_key_path" in node and node["ssh_key_path"]:
                ssh_key_arg = ["-i", node["ssh_key_path"]]

            test_cmd = [
                "ssh",
                *ssh_key_arg,
                "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=no",
                f"{node['user']}@{node['host']}",
                "echo 'connection test'"
            ]

            process = await asyncio.create_subprocess_exec(
                *test_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else "Connection failed"
                raise RuntimeError(
                    f"Cannot connect to node {node['name']} at {node['host']}: {error_msg}"
                )

        self._log(cluster, "INFO", "Prerequisites validated successfully")

    async def _update_status(
        self,
        cluster: ManagementCluster,
        status: ManagementClusterStatus,
        phase: str,
        progress: int
    ):
        """Update cluster status in database."""
        cluster.status = status
        cluster.current_phase = phase
        cluster.progress_percent = progress
        cluster.updated_at = datetime.utcnow()
        self.db.commit()

        self._log(cluster, "INFO", f"{phase} ({progress}%)")

    def _log(
        self,
        cluster: ManagementCluster,
        level: str,
        message: str
    ):
        """Log deployment event to database."""
        log = DeploymentLog(
            entity_id=cluster.id,
            entity_type="management_cluster",
            level=level,
            message=message
        )
        self.db.add(log)
        self.db.commit()

    async def add_node(self, cluster_id: UUID, node: dict) -> bool:
        """
        Add a new node to an existing active management cluster.

        Steps:
          1. Append the node definition to cluster.nodes in DB
          2. Regenerate Ansible inventory (covering only the new node)
          3. prepare_all_nodes — apt update, K3s binary pre-stage
          4. join_k3s_workers — node joins via K3s token
        """
        cluster = self.db.query(ManagementCluster).filter(
            ManagementCluster.id == cluster_id
        ).first()
        if not cluster:
            raise RuntimeError("Cluster not found")

        node_name = node.get("name", "")
        existing_names = [n.get("name") for n in (cluster.nodes or [])]
        if node_name in existing_names:
            raise RuntimeError(f"A node named '{node_name}' already exists in this cluster")

        nodes = list(cluster.nodes or [])
        nodes.append(node)
        cluster.nodes = nodes
        self.db.commit()

        self._log(cluster, "INFO", f"Adding node '{node_name}' (role: {node.get('roles')})")

        inventory_path = None
        try:
            inventory_path = await self._generate_single_node_inventory(cluster, node)

            # Step 1: Prepare (apt update, K3s binary, sysctl)
            self._log(cluster, "INFO", f"Preparing new node '{node_name}'")
            await self._run_ansible_playbook(
                cluster,
                os.path.join(self.ansible_path, "playbooks/prepare_all_nodes.yml"),
                inventory_path,
                extra_vars={"kubernetes_version": cluster.kubernetes_version} if cluster.kubernetes_version else None,
            )

            # Step 2: Join the node to K3s
            k3s_node_token = (cluster.meta_data or {}).get("k3s_node_token")
            if not k3s_node_token:
                raise RuntimeError("No k3s_node_token stored — was cluster provisioned correctly?")

            server_node = next(
                (n for n in cluster.nodes if "server" in n.get("roles", []) and n.get("name") != node_name),
                None
            )
            if not server_node:
                raise RuntimeError("No existing server node found to derive K3s server URL")

            k3s_server_url = f"https://{server_node['host']}:6443"

            self._log(cluster, "INFO", f"Joining node '{node_name}' to K3s cluster at {k3s_server_url}")
            await self._run_ansible_playbook(
                cluster,
                os.path.join(self.ansible_path, "playbooks/join_k3s_workers.yml"),
                inventory_path,
                extra_vars={
                    "k3s_server_url": k3s_server_url,
                    "k3s_node_token": k3s_node_token,
                    "kubernetes_version": cluster.kubernetes_version or "v1.31.5+k3s1",
                },
            )

            self._log(cluster, "INFO", f"Node '{node_name}' added successfully")
            return True

        except Exception as e:
            nodes = [n for n in (cluster.nodes or []) if n.get("name") != node_name]
            cluster.nodes = nodes
            self.db.commit()
            self._log(cluster, "ERROR", f"Failed to add node '{node_name}': {e}")
            raise

        finally:
            if inventory_path and os.path.exists(inventory_path):
                try:
                    inv_dir = os.path.dirname(inventory_path)
                    os.remove(inventory_path)
                    if os.path.exists(inv_dir):
                        os.rmdir(inv_dir)
                except Exception:
                    pass

    async def _generate_single_node_inventory(self, cluster: ManagementCluster, node: dict) -> str:
        """Generate an Ansible inventory containing only the given node."""
        full_inventory_path = await self._generate_inventory(cluster)
        with open(full_inventory_path) as f:
            inventory_data = yaml.safe_load(f)
        os.remove(full_inventory_path)
        try:
            os.rmdir(os.path.dirname(full_inventory_path))
        except Exception:
            pass

        ssh_key = node.get("ssh_key_path") or (
            cluster.nodes[0].get("ssh_key_path") if cluster.nodes else None
        )
        node_config = {
            "ansible_host": node["host"],
            "ansible_user": node["user"],
            "node_roles": node["roles"],
        }
        if ssh_key:
            node_config["ansible_ssh_private_key_file"] = ssh_key

        def _replace_hosts(obj):
            if isinstance(obj, dict):
                if "hosts" in obj:
                    obj["hosts"] = {node["name"]: node_config}
                for v in obj.values():
                    _replace_hosts(v)

        _replace_hosts(inventory_data)

        inventory_dir = tempfile.mkdtemp(prefix="drift_addnode_")
        inventory_path = os.path.join(inventory_dir, "hosts.yml")
        with open(inventory_path, "w") as f:
            yaml.dump(inventory_data, f, default_flow_style=False)

        self._log(cluster, "INFO", f"Generated single-node inventory for '{node['name']}' at {inventory_path}")
        return inventory_path

    async def deprovision_cluster(self, cluster_id: UUID) -> bool:
        """Tear down a management cluster via Ansible and remove from DB."""
        cluster = self.db.query(ManagementCluster).filter(
            ManagementCluster.id == cluster_id
        ).first()
        if not cluster:
            return False

        cluster.status = ManagementClusterStatus.DELETING
        cluster.current_phase = "Preparing teardown"
        cluster.progress_percent = 0
        self.db.commit()
        self._log(cluster, "INFO", f"Starting teardown of cluster {cluster.name}")

        # Delete the record only once the node is really clean. A failed or
        # unparsable uninstall used to be logged as a warning and the record
        # deleted anyway (in a finally:), which took the logs with it and left
        # k3s running on the node with no trace in the dashboard.
        teardown_ok = False
        try:
            cluster.current_phase = "Generating inventory"
            cluster.progress_percent = 10
            self.db.commit()
            inventory_path = await self._generate_inventory(cluster)

            playbook_path = os.path.join(self.ansible_path, "playbooks/uninstall_management_cluster.yml")

            if os.path.exists(playbook_path):
                cluster.current_phase = "Running uninstall playbook"
                cluster.progress_percent = 30
                self.db.commit()
                await self._run_ansible_playbook(
                    cluster, playbook_path, inventory_path,
                    extra_vars={"cluster_name": cluster.name}
                )
                cluster.current_phase = "Playbook complete"
                cluster.progress_percent = 90
                self.db.commit()
                self._log(cluster, "INFO", "Cluster teardown completed successfully")
                teardown_ok = True
            else:
                self._log(cluster, "WARNING", "Uninstall playbook not found — proceeding with DB delete only")
                teardown_ok = True

            if inventory_path and os.path.exists(inventory_path):
                try:
                    os.remove(inventory_path)
                    os.rmdir(os.path.dirname(inventory_path))
                except Exception:
                    pass

        except Exception as e:
            self._log(cluster, "ERROR", f"Teardown failed, record kept so it can be retried: {e}")
            cluster.status = ManagementClusterStatus.FAILED
            cluster.current_phase = f"Teardown failed: {str(e)[:80]}"
            self.db.commit()

        if teardown_ok:
            self.db.delete(cluster)
            self.db.commit()
        return teardown_ok
        return True


async def provision_management_cluster(cluster_id: UUID):
    """Background task to provision management cluster."""
    from database import SessionLocal

    db = SessionLocal()
    try:
        orchestrator = ManagementClusterOrchestrator(db)
        await orchestrator.provision_cluster(cluster_id)
    finally:
        db.close()


async def deprovision_management_cluster(cluster_id: UUID):
    """Background task to tear down and delete a management cluster."""
    from database import SessionLocal

    db = SessionLocal()
    try:
        orchestrator = ManagementClusterOrchestrator(db)
        await orchestrator.deprovision_cluster(cluster_id)
    finally:
        db.close()
