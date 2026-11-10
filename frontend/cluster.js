// DRIFT — Edge Cluster Page

const apiUrl = (() => {
    const stored = localStorage.getItem('apiUrl');
    if (stored) return stored;
    return window.location.hostname === 'localhost'
        ? 'http://localhost:8000'
        : `http://${window.location.hostname}:8000`;
})();

// Read cluster ID from URL: cluster.html?id=<uuid>
const clusterId = new URLSearchParams(window.location.search).get('id');

let cluster = null;
let refreshTimer = null;
let migrationVm = null;  // tracks the VM currently being migrated

// Adaptive polling: 5 s while cluster is transient, 30 s when idle.
const _MC_TRANSIENT = new Set(['provisioning', 'pending', 'deleting', 'creating']);
function _mcIsTransient(s) { return _MC_TRANSIENT.has((s || '').toLowerCase()); }
function _rescheduleRefresh() {
    clearInterval(refreshTimer);
    const fast = _mcIsTransient(cluster?.status);
    refreshTimer = setInterval(loadCluster, fast ? 5000 : 30000);
}

function utcDate(s) {
    if (!s) return new Date();
    return new Date(s.endsWith('Z') ? s : s + 'Z');
}

function timeSince(date) {
    const s = Math.floor((Date.now() - date) / 1000);
    if (s < 60)   return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h`;
    return `${Math.floor(s / 86400)}d`;
}

function formatRam(val) {
    if (!val && val !== 0) return '—';
    const s = String(val);
    let mi;
    if (s.endsWith('Ki')) mi = parseFloat(s) / 1024;
    else if (s.endsWith('Mi')) mi = parseFloat(s);
    else if (s.endsWith('Gi')) mi = parseFloat(s) * 1024;
    else mi = parseFloat(s);
    if (isNaN(mi)) return s;
    return mi >= 1024 ? `${(mi / 1024).toFixed(1)} Gi` : `${Math.round(mi)} Mi`;
}

// ── Bootstrap ─────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    if (!clusterId) {
        document.getElementById('cluster-title').textContent = 'No cluster ID provided';
        return;
    }
    loadCluster(); // _rescheduleRefresh() is called at the end of loadCluster
});

// ── Tab switching ──────────────────────────────────────────────────────────

function switchTab(name, btn) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`tab-${name}`).classList.add('active');

    if (name === 'nodes') syncLiveStatus();
    if (name === 'vms') loadAllVMs();
    if (name === 'pods') loadAllPods();
    if (name === 'logs') {
        loadLogs();
        clearInterval(_logsInterval);
        _logsInterval = setInterval(loadLogs, 5000);
    } else {
        clearInterval(_logsInterval);
    }
}

// ── Fetch cluster data ─────────────────────────────────────────────────────

async function loadCluster() {
    try {
        const clusterResp = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}`);
        if (clusterResp.status === 404) {
            clearInterval(refreshTimer);
            window.location.href = 'index.html';
            return;
        }
        if (!clusterResp.ok) throw new Error(`HTTP ${clusterResp.status}`);
        cluster = await clusterResp.json();
        renderHeader();
        renderNodes();
        _rescheduleRefresh(); // adaptive: 5 s if transient, 30 s if stable
    } catch (e) {
        document.getElementById('cluster-title').textContent = `Error: ${e.message}`;
    }
}

// ── Header ─────────────────────────────────────────────────────────────────

function renderHeader() {
    document.title = `DRIFT — ${cluster.name}`;
    document.getElementById('cluster-title').textContent = cluster.name;
    document.getElementById('cluster-sub').textContent =
        cluster.provisioned_at
            ? `Provisioned ${timeSince(utcDate(cluster.provisioned_at))} ago`
            : cluster.created_at
                ? `Created ${new Date(cluster.created_at).toLocaleDateString()}`
                : '';

    const badge = document.getElementById('cluster-status-badge');
    const phase = (cluster.current_phase || '').toLowerCase();
    const degraded = phase.includes('failed') || phase.includes('degraded');
    badge.textContent = degraded ? 'DEGRADED' : cluster.status.toUpperCase();
    badge.className = `status-badge status-${degraded ? 'degraded' : cluster.status}`;
}

// ── Nodes ──────────────────────────────────────────────────────────────────

function renderNodes() {
    const confirmed = cluster.nodes_status || [];
    const defined   = cluster.nodes || [];
    const syncEl    = document.getElementById('nodes-sync');

    if (cluster.last_synced) {
        syncEl.textContent = `Last synced: ${new Date(cluster.last_synced).toLocaleTimeString()}`;
    }

    const confirmedNames = new Set(confirmed.map(n => n.name));
    const pending = defined.filter(n => n.name && !confirmedNames.has(n.name));

    if (confirmed.length === 0 && pending.length === 0) {
        document.getElementById('nodes-content').innerHTML =
            '<p style="color:var(--text-muted);text-align:center;padding:30px;">No node data yet — click Sync to fetch live status.</p>';
        return;
    }

    const isCP = r => /^(master|control-plane|controlplane|server)$/.test(r);

    const pendingRows = pending.map(n => `
        <tr style="opacity:.6;">
            <td><span style="color:#d29922;font-size:.82em;">Joining…</span></td>
            <td style="font-weight:600;">${n.name}</td>
            <td style="color:var(--text-muted);">${n.host || '—'}</td>
            <td>${(n.roles || []).map(r => `<span class="role-tag ${isCP(r) ? '' : 'role-worker'}">${r}</span>`).join('')}</td>
            <td>—</td><td>—</td>
            <td></td>
        </tr>`).join('');

    document.getElementById('nodes-content').innerHTML = `
        <table class="node-table">
            <thead><tr>
                <th>Status</th><th>Name</th><th>IP</th><th>Roles</th>
                <th>CPU</th><th>RAM</th><th></th>
            </tr></thead>
            <tbody>
                ${confirmed.map(n => {
                    const cp = (n.roles || []).some(isCP);
                    const deleteBtn = cp ? '' :
                        `<button class="btn btn-danger btn-sm" onclick="confirmDeleteNode('${n.name}')">Remove</button>`;
                    return `
                    <tr>
                        <td>
                            <span class="ready-dot ${n.ready ? 'ready-yes' : 'ready-no'}"></span>
                            ${n.unschedulable ? '<span style="color:#d29922;font-size:.78em;">Cordoned</span>' : (n.ready ? 'Ready' : 'NotReady')}
                        </td>
                        <td style="font-weight:600;">${n.name}</td>
                        <td style="color:var(--text-muted);">${n.ip || '—'}</td>
                        <td>${(n.roles || []).map(r => `<span class="role-tag ${isCP(r) ? '' : 'role-worker'}">${r}</span>`).join('')}</td>
                        <td>${n.cpu_allocatable || '—'}</td>
                        <td>${formatRam(n.ram_allocatable)}</td>
                        <td>${deleteBtn}</td>
                    </tr>`;
                }).join('')}
                ${pendingRows}
            </tbody>
        </table>
    `;
}

async function syncLiveStatus() {
    const btn = document.getElementById('nodes-sync-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Syncing…'; }
    try {
        const resp = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}/live-status`);
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        cluster.nodes_status   = data.nodes;
        cluster.addon_statuses = data.addon_statuses;
        cluster.last_synced = new Date().toISOString();
        renderNodes();
    } catch (e) {
        alert(`Sync failed: ${e.message}`);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Sync'; }
    }
}

function confirmDeleteNode(nodeName) {
    openDangerModal(
        'Remove Node',
        `Drain and permanently remove <strong>${nodeName}</strong> from this cluster?<br><br>
         Running VMs on this node will be evicted first. This cannot be undone.`,
        () => deleteNode(nodeName),
    );
}

async function deleteNode(nodeName) {
    try {
        const resp = await fetch(
            `${apiUrl}/api/v1/edge-clusters/${clusterId}/nodes/${encodeURIComponent(nodeName)}`,
            { method: 'DELETE' },
        );
        if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
        const data = await resp.json();
        if (data.warnings?.length) {
            console.warn('Node removal warnings:', data.warnings);
        }
        await loadCluster();
        await syncLiveStatus();
    } catch (e) {
        alert(`Failed to remove node: ${e.message}`);
    }
}

// ── Logs ───────────────────────────────────────────────────────────────────

let _logsInterval = null;

async function loadLogs() {
    const el = document.getElementById('logs-content');
    if (!el) return;
    try {
        const resp = await fetch(`${apiUrl}/api/v1/logs?entity_type=management_cluster&entity_id=${clusterId}&limit=200`);
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        renderLogs(data.logs || []);
    } catch (e) {
        el.innerHTML = `<p style="color:var(--danger);padding:20px;">Failed to load logs: ${e.message}</p>`;
    }
}

function renderLogs(logs) {
    const el = document.getElementById('logs-content');
    if (!el) return;
    if (!logs.length) {
        el.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:30px;">No logs yet.</p>';
        return;
    }
    const levelClass = l => {
        const m = { INFO:'info', WARNING:'warning', WARN:'warning', ERROR:'error', SUCCESS:'success' };
        return 'log-level log-level-' + (m[l?.toUpperCase()] || 'info');
    };
    el.innerHTML = `
        <table class="log-table">
            <thead><tr><th>Time</th><th>Level</th><th>Message</th></tr></thead>
            <tbody>
                ${logs.map(l => `
                <tr>
                    <td style="white-space:nowrap;color:var(--text-muted);">${new Date(l.timestamp).toLocaleString()}</td>
                    <td><span class="${levelClass(l.level)}">${l.level}</span></td>
                    <td style="color:var(--text);word-break:break-word;">${escHtml(l.message)}</td>
                </tr>`).join('')}
            </tbody>
        </table>`;
}

function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── VMs Tab ────────────────────────────────────────────────────────────────

async function loadAllVMs() {
    const el = document.getElementById('vms-content');
    el.innerHTML = '<div class="spinner"></div>';
    try {
        const resp = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}/all-vms`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderAllVMs(data.namespaces || {});
    } catch (e) {
        el.innerHTML = `<p style="color:var(--danger);padding:20px 0;">Error: ${e.message}</p>`;
    }
}

function renderAllVMs(byNs) {
    const el = document.getElementById('vms-content');
    const namespaces = Object.keys(byNs);

    if (namespaces.length === 0) {
        el.innerHTML = '<p style="color:var(--text-muted);padding:20px 0;text-align:center;">No VMs found. Create one to get started.</p>';
        return;
    }

    const phaseColor = p => {
        if (p === 'Running')   return 'var(--success)';
        if (p === 'Pending' || p === 'Scheduling') return 'var(--warning)';
        if (p === 'Failed')    return 'var(--danger)';
        return 'var(--text-muted)';
    };

    el.innerHTML = namespaces.map(ns => `
        <div style="margin-bottom:24px;">
            <div style="font-size:.72em;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);margin-bottom:10px;display:flex;align-items:center;gap:8px;">
                <span style="background:var(--accent-dim);color:#818cf8;padding:2px 10px;border-radius:20px;">${ns}</span>
                <span>${byNs[ns].length} VM${byNs[ns].length !== 1 ? 's' : ''}</span>
            </div>
            <table class="node-table">
                <thead><tr>
                    <th>Name</th><th>Phase</th><th>IP</th><th>Node</th><th>CPU</th><th>Memory</th><th></th>
                </tr></thead>
                <tbody>
                ${byNs[ns].map(vm => `
                    <tr>
                        <td style="font-family:monospace;font-weight:600;">${vm.name}</td>
                        <td>
                            <span style="display:inline-flex;align-items:center;gap:6px;">
                                <span style="width:7px;height:7px;border-radius:50%;background:${phaseColor(vm.phase)};flex-shrink:0;"></span>
                                ${vm.phase}
                            </span>
                        </td>
                        <td style="font-family:monospace;color:var(--text-muted);">${vm.ip || '—'}</td>
                        <td style="color:var(--text-muted);">${vm.node || '—'}</td>
                        <td>${vm.cpu} core${vm.cpu !== 1 ? 's' : ''}</td>
                        <td>${vm.memory}</td>
                        <td style="display:flex;gap:6px;align-items:center;">
                            <button class="btn btn-sm" style="background:rgba(99,102,241,.12);color:#818cf8;border:1px solid rgba(99,102,241,.2);"
                                onclick="openMigrateModal('${ns}', '${vm.name}', '${vm.node}', '${vm.cpu}', '${vm.memory}')"
                                ${vm.phase !== 'Running' ? 'disabled title="VM must be Running to migrate"' : ''}>
                                Migrate
                            </button>
<button class="btn btn-sm" style="background:rgba(248,81,73,.12);color:#f85149;border:1px solid rgba(248,81,73,.2);"
                                onclick="deleteVM('${ns}', '${vm.name}')">Delete</button>
                        </td>
                    </tr>
                `).join('')}
                </tbody>
            </table>
        </div>
    `).join('');
}

// ── Pods (containers) ───────────────────────────────────────────────────────

async function loadAllPods() {
    const el = document.getElementById('pods-content');
    el.innerHTML = '<div class="spinner"></div>';
    const sys = document.getElementById('pods-show-system')?.checked ? '?include_system=true' : '';
    try {
        const resp = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}/all-pods${sys}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderAllPods(data.namespaces || {});
    } catch (e) {
        el.innerHTML = `<p style="color:var(--danger);padding:20px 0;">Error: ${e.message}</p>`;
    }
}

function renderAllPods(byNs) {
    const el = document.getElementById('pods-content');
    const namespaces = Object.keys(byNs);
    if (namespaces.length === 0) {
        el.innerHTML = '<p style="color:var(--text-muted);padding:20px 0;text-align:center;">No workload pods found.</p>';
        return;
    }
    const phaseColor = p => {
        if (p === 'Running') return 'var(--success)';
        if (p === 'Pending') return 'var(--warning)';
        if (p === 'Failed')  return 'var(--danger)';
        return 'var(--text-muted)';
    };
    el.innerHTML = namespaces.map(ns => `
        <div style="margin-bottom:24px;">
            <div style="font-size:.72em;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);margin-bottom:10px;display:flex;align-items:center;gap:8px;">
                <span style="background:var(--accent-dim);color:#818cf8;padding:2px 10px;border-radius:20px;">${ns}</span>
                <span>${byNs[ns].length} pod${byNs[ns].length !== 1 ? 's' : ''}</span>
            </div>
            <table class="node-table">
                <thead><tr>
                    <th>Name</th><th>Phase</th><th>IP</th><th>Node</th><th>Containers</th><th></th>
                </tr></thead>
                <tbody>
                ${byNs[ns].map(pod => {
                    const mig = pod.migration;
                    const dot = c => `<span style="width:7px;height:7px;border-radius:50%;background:${c};flex-shrink:0;display:inline-block;"></span>`;
                    let badge, statusCell, action, rowStyle = '';
                    if (mig && mig.role === 'target') {
                        const ok = mig.verified !== false;
                        badge = `<span title="Migrated to this node" style="color:#818cf8;font-size:.8em;">⇐ migrated from ${mig.peer_node}${mig.freeze_s ? ` · freeze ${mig.freeze_s}s` : ''}</span>`;
                        statusCell = `<span style="display:inline-flex;align-items:center;gap:6px;">${dot(ok ? 'var(--success)' : 'var(--danger)')}${ok ? 'Running' : 'Stopped'}</span>`;
                        // a live migrated workload can hop again (follow-me)
                        action = pod.migratable
                            ? `<button class="btn btn-sm" style="background:rgba(99,102,241,.12);color:#818cf8;border:1px solid rgba(99,102,241,.2);"
                                onclick="openMigrateModal('${ns}', '${pod.name}', '${pod.node}', 0, '0', 'all-pods')" title="Migrate again (follow-me)">Migrate</button>`
                            : '<span style="color:var(--text-muted);font-size:.85em;">—</span>';
                    } else if (mig && mig.role === 'source') {
                        rowStyle = 'opacity:.5;';
                        badge = `<span title="Migrated away" style="color:var(--text-muted);font-size:.8em;">⇒ moved to ${mig.peer_node}</span>`;
                        statusCell = `<span style="display:inline-flex;align-items:center;gap:6px;color:var(--text-muted);">${dot('var(--text-muted)')}Migrated</span>`;
                        action = '<span style="color:var(--text-muted);font-size:.85em;">—</span>';
                    } else {
                        badge = pod.migratable
                            ? '<span title="Migration-ready (holder+work)" style="color:var(--success);font-size:.8em;">● live-migratable</span>'
                            : '<span title="Not migration-ready — needs the holder+work container pattern" style="color:var(--text-muted);font-size:.8em;">○ not ready</span>';
                        statusCell = `<span style="display:inline-flex;align-items:center;gap:6px;">${dot(phaseColor(pod.phase))}${pod.phase}</span>`;
                        const why = pod.phase !== 'Running'
                            ? 'disabled title="Pod must be Running to migrate"'
                            : (!pod.migratable ? 'disabled title="Not migration-ready — needs the holder+work container pattern"' : '');
                        action = `<button class="btn btn-sm" style="background:rgba(99,102,241,.12);color:#818cf8;border:1px solid rgba(99,102,241,.2);"
                                onclick="openMigrateModal('${ns}', '${pod.name}', '${pod.node}', 0, '0', 'all-pods')" ${why}>Migrate</button>`;
                    }
                    return `
                    <tr style="${rowStyle}">
                        <td style="font-family:monospace;font-weight:600;">
                            ${pod.name}
                            <div style="margin-top:2px;">${badge}</div>
                        </td>
                        <td>${statusCell}</td>
                        <td style="font-family:monospace;color:var(--text-muted);">${pod.ip || '—'}</td>
                        <td style="color:var(--text-muted);">${pod.node || '—'}</td>
                        <td style="color:var(--text-muted);font-size:.85em;">${(pod.containers || []).join(', ')}</td>
                        <td>${action}</td>
                    </tr>`;
                }).join('')}
                </tbody>
            </table>
        </div>
    `).join('');
}

// ── Create VM modal ─────────────────────────────────────────────────────────

let _vmNodes = [];   // cached node list for feasibility checks

function _parseMi(s) {
    s = String(s || '0');
    if (s.endsWith('Gi')) return parseFloat(s) * 1024;
    if (s.endsWith('Mi')) return parseFloat(s);
    if (s.endsWith('G'))  return parseFloat(s) * 1024;
    if (s.endsWith('M'))  return parseFloat(s);
    return 0;
}


function _updateVMFeasibility() {
    const statusEl = document.getElementById('vm-resource-status');
    const createBtn = document.getElementById('vm-create-btn');
    if (!statusEl || !_vmNodes.length) return;

    const cpuCores = parseInt(document.getElementById('vm-cpu').value) || 0;
    const memMi    = _parseMi(document.getElementById('vm-memory').value);
    const cpuM     = cpuCores * 1000;

    const fmtCpu  = m  => m  >= 1000 ? `${(m/1000).toFixed(1)} cores` : `${m}m`;
    const fmtMem  = mi => mi >= 1024 ? `${(mi/1024).toFixed(1)} Gi` : `${mi} Mi`;
    const fmtDisk = gi => gi ? `${gi} Gi disk` : null;

    const feasible = _vmNodes.filter(n =>
        (n.available_cpu_m  ?? 0) >= cpuM  &&
        (n.available_mem_mi ?? 0) >= memMi
    );
    const best = _vmNodes.reduce((a, b) =>
        (b.available_mem_mi ?? 0) > (a.available_mem_mi ?? 0) ? b : a, _vmNodes[0]
    );

    if (feasible.length > 0) {
        const n = feasible[0];
        const disk = fmtDisk(n.allocatable_disk_gi);
        statusEl.style.borderLeftColor = 'var(--success)';
        statusEl.innerHTML =
            `<div style="color:var(--success);font-weight:600;">✓ ${feasible.length} node${feasible.length > 1 ? 's' : ''} can fit this VM</div>` +
            `<div style="color:var(--text-muted);margin-top:3px;">Best: ${n.name} — ${fmtCpu(n.available_cpu_m ?? 0)} CPU / ${fmtMem(n.available_mem_mi ?? 0)} RAM free${disk ? ` / ${disk} allocatable` : ''}</div>`;
        createBtn.disabled = false;
        createBtn.style.opacity = '';
    } else {
        const disk = best ? fmtDisk(best.allocatable_disk_gi) : null;
        statusEl.style.borderLeftColor = 'var(--danger)';
        statusEl.innerHTML =
            `<div style="color:var(--danger);font-weight:600;">✗ No node has enough resources</div>` +
            (best ? `<div style="color:var(--text-muted);margin-top:3px;">Max available on ${best.name}: ${fmtCpu(best.available_cpu_m ?? 0)} CPU / ${fmtMem(best.available_mem_mi ?? 0)} RAM${disk ? ` / ${disk} allocatable` : ''}</div>` : '');
        createBtn.disabled = true;
        createBtn.style.opacity = '0.45';
    }
}

const _VM_IMAGES_EPHEMERAL = [
    { label: 'Ubuntu 22.04', value: 'quay.io/containerdisks/ubuntu:22.04' },
    { label: 'Ubuntu 20.04', value: 'quay.io/containerdisks/ubuntu:20.04' },
    { label: 'Fedora 40',    value: 'quay.io/containerdisks/fedora:40' },
    { label: 'Debian 12',    value: 'quay.io/containerdisks/debian:12' },
    { label: 'Custom…',      value: 'custom' },
];
const _VM_IMAGES_PERSISTENT = [
    { label: 'Ubuntu 22.04', value: 'https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img' },
    { label: 'Ubuntu 20.04', value: 'https://cloud-images.ubuntu.com/focal/current/focal-server-cloudimg-amd64.img' },
    { label: 'Fedora 40',    value: 'https://dl.fedoraproject.org/pub/fedora/linux/releases/40/Cloud/x86_64/images/Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2' },
    { label: 'Debian 12',    value: 'https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2' },
    { label: 'Custom URL…',  value: 'custom' },
];

function onVMDiskModeChange() {
    const persistent = document.getElementById('vm-disk-persistent').checked;
    document.getElementById('vm-disk-size-row').style.display = persistent ? '' : 'none';
    const sel = document.getElementById('vm-image');
    const images = persistent ? _VM_IMAGES_PERSISTENT : _VM_IMAGES_EPHEMERAL;
    sel.innerHTML = images.map(i => `<option value="${i.value}">${i.label}</option>`).join('');
    document.getElementById('vm-custom-image-row').style.display = 'none';
}

async function openCreateVMModal() {
    document.getElementById('vm-modal').classList.add('open');
    document.getElementById('vm-modal-error').textContent = '';
    document.getElementById('vm-name').value = '';
    document.getElementById('vm-namespace').value = 'default';
    document.getElementById('vm-disk-ephemeral').checked = true;
    document.getElementById('vm-disk-size-row').style.display = 'none';
    onVMDiskModeChange();

    const statusEl = document.getElementById('vm-resource-status');
    const createBtn = document.getElementById('vm-create-btn');
    statusEl.style.borderLeftColor = 'var(--accent)';
    statusEl.innerHTML = '<span style="color:var(--text-muted);">Checking available node resources…</span>';
    createBtn.disabled = true;
    createBtn.style.opacity = '0.45';

    try {
        const data = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}/edge-nodes`).then(r => r.json());
        _vmNodes = data.nodes || [];
        _updateVMFeasibility();
    } catch (e) {
        statusEl.innerHTML = `<span style="color:var(--text-muted);">Could not load node resources: ${e.message}</span>`;
        createBtn.disabled = false;
        createBtn.style.opacity = '';
    }
}

function closeVMModal() {
    document.getElementById('vm-modal').classList.remove('open');
    _vmNodes = [];
}

document.addEventListener('DOMContentLoaded', () => {
    const imgSel = document.getElementById('vm-image');
    if (imgSel) {
        imgSel.addEventListener('change', () => {
            const row = document.getElementById('vm-custom-image-row');
            row.style.display = imgSel.value === 'custom' ? '' : 'none';
        });
    }
    // Live feasibility update when CPU, memory, or disk changes
    ['vm-cpu', 'vm-memory'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', _updateVMFeasibility);
    });
    const vmOverlay = document.getElementById('vm-modal');
    if (vmOverlay) {
        vmOverlay.addEventListener('click', e => { if (e.target === vmOverlay) closeVMModal(); });
    }
});

async function submitCreateVM() {
    const errEl = document.getElementById('vm-modal-error');
    errEl.textContent = '';

    const imgSel = document.getElementById('vm-image');
    const image = imgSel.value === 'custom'
        ? document.getElementById('vm-custom-image').value.trim()
        : imgSel.value;

    const normalizeSize = v => v && /^\d+$/.test(v.trim()) ? v.trim() + 'Gi' : v.trim();
    const persistent = document.getElementById('vm-disk-persistent').checked;

    const payload = {
        name:       document.getElementById('vm-name').value.trim(),
        namespace:  document.getElementById('vm-namespace').value.trim() || 'default',
        cpu:        parseInt(document.getElementById('vm-cpu').value),
        memory:     document.getElementById('vm-memory').value,
        persistent,
        disk:       persistent ? normalizeSize(document.getElementById('vm-disk').value) : null,
        image,
    };

    if (!payload.name) { errEl.textContent = 'Name is required.'; return; }
    if (!image)        { errEl.textContent = 'Image is required.'; return; }

    try {
        const resp = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}/all-vms`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        const created = await resp.json();
        closeVMModal();
        loadAllVMs();
    } catch (e) {
        errEl.textContent = `Error: ${e.message}`;
    }
}

// ── Danger (delete) confirm modal ──────────────────────────────────────────
// Shared by VM delete + node remove. The modal markup lives in cluster.html
// (#danger-confirm-modal); these drive it. (Previously called but never
// defined — every Delete/Remove button silently threw ReferenceError.)
let _dangerConfirm = null;
function openDangerModal(name, bodyText, onConfirm) {
    _dangerConfirm = onConfirm;
    document.getElementById('danger-modal-title').textContent = name;
    document.getElementById('danger-modal-body').textContent = bodyText;
    document.getElementById('danger-modal-error').textContent = '';
    const btn = document.getElementById('danger-confirm-btn');
    btn.disabled = false;
    btn.textContent = 'Delete';
    btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = 'Deleting…';
        try {
            if (_dangerConfirm) await _dangerConfirm();
            closeDangerModal();
        } catch (e) {
            document.getElementById('danger-modal-error').textContent = e.message || String(e);
            btn.disabled = false;
            btn.textContent = 'Delete';
        }
    };
    document.getElementById('danger-confirm-modal').classList.add('open');
}
function closeDangerModal() {
    document.getElementById('danger-confirm-modal').classList.remove('open');
    _dangerConfirm = null;
}

function deleteVM(namespace, name) {
    openDangerModal(
        name,
        `This VM will be permanently deleted from namespace "${namespace}".`,
        async () => {
            const resp = await fetch(
                `${apiUrl}/api/v1/edge-clusters/${clusterId}/all-vms/${namespace}/${name}`,
                { method: 'DELETE' }
            );
            if (!resp.ok && resp.status !== 204) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${resp.status}`);
            }
            loadAllVMs();
        }
    );
}

// ── Live Migration Modal ───────────────────────────────────────────────────

async function openMigrateModal(namespace, vmName, currentNode, vmCpu, vmRam, resource) {
    // resource: 'all-vms' (KubeVirt live-migration) | 'all-pods' (CRIU post-copy).
    // The workload kind is a fact, not a user choice; the migration MECHANISM is
    // derived from it and from whether the target is in- or out-of-cluster.
    resource = resource || 'all-vms';
    migrationVm = { namespace, vmName, currentNode, vmCpu, vmRam, resource };

    // Reset to form view in case it was left on progress
    document.getElementById('migrate-form-body').style.display = '';
    document.getElementById('migrate-progress-body').style.display = 'none';
    document.getElementById('migrate-modal-title').textContent = `Migrate: ${vmName}`;
    const errEl2 = document.getElementById('migrate-modal-error');
    errEl2.textContent = '';
    errEl2.style.display = 'none';
    document.getElementById('mg-source-node-display').textContent = currentNode || '(unknown)';
    document.getElementById('mg-target-node-display').textContent = 'Select…';

    const sel = document.getElementById('migrate-target-node');
    sel.innerHTML = '<option disabled selected>Loading targets…</option>';
    document.getElementById('migrate-modal').classList.add('open');

    const fmtCpu  = m => m >= 1000 ? `${(m / 1000).toFixed(1)} cores` : `${m}m cores`;
    const fmtMem  = mi => mi >= 1024 ? `${(mi / 1024).toFixed(1)} GB` : `${mi} MB`;
    const liveTag = s => (s === 'metrics-server' || s === 'kubelet-stats') ? ' · live' : ' · est.';
    const vmCpuM = (vmCpu || 0) * 1000;
    const vmMemMi = (() => {
        const s = String(vmRam || '0');
        if (s.endsWith('Gi')) return parseFloat(s) * 1024;
        if (s.endsWith('Mi')) return parseFloat(s);
        if (s.endsWith('G'))  return parseFloat(s) * 1024;
        if (s.endsWith('M'))  return parseFloat(s);
        return 0;
    })();

    let optionsHtml = '';
    let anyTarget = false;

    // 1) intra-cluster nodes (live migration, node → node)
    try {
        const excludeParam = currentNode ? `?exclude=${encodeURIComponent(currentNode)}` : '';
        const resp = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}/edge-nodes${excludeParam}`);
        if (resp.ok) {
            const nodes = (await resp.json()).nodes || [];
            if (nodes.length) {
                const opts = nodes.map(n => {
                    const feasible = (n.available_cpu_m ?? 0) >= vmCpuM && (n.available_mem_mi ?? 0) >= vmMemMi;
                    const cpuFree = n.available_cpu_m  != null ? fmtCpu(n.available_cpu_m)  : '—';
                    const memFree = n.available_mem_mi != null ? fmtMem(n.available_mem_mi) : '—';
                    const tag = n.source ? liveTag(n.source) : '';
                    const status = feasible ? '✓' : '✗ insufficient';
                    if (feasible) anyTarget = true;
                    return `<option value="node:${n.name}" ${feasible ? '' : 'disabled'}>` +
                           `${status}  ${n.name}  —  ${cpuFree} free / ${memFree} free${tag}</option>`;
                }).join('');
                optionsHtml += `<optgroup label="This cluster — live migration (node → node)">${opts}</optgroup>`;
            }
        }
    } catch (_) { /* ignore, cross-cluster may still be available */ }

    // 2) other active clusters (cross-cluster / decentralized migration)
    try {
        const resp = await fetch(`${apiUrl}/api/v1/edge-clusters?status=active`);
        if (resp.ok) {
            const clusters = ((await resp.json()).clusters || []).filter(c => c.id !== clusterId);
            if (clusters.length) {
                const opts = clusters.map(c =>
                    `<option value="cluster:${c.id}" data-name="${c.name}">⇄  ${c.name}  —  cross-cluster</option>`
                ).join('');
                optionsHtml += `<optgroup label="Other clusters — cross-cluster (decentralized)">${opts}</optgroup>`;
                anyTarget = true;  // backend validates cross-cluster feasibility
            }
        }
    } catch (_) { /* ignore */ }

    if (!optionsHtml) {
        sel.innerHTML = '<option disabled selected>No eligible targets available</option>';
        return;
    }
    sel.innerHTML = '<option value="" disabled selected>Select…</option>' + optionsHtml;
    if (!anyTarget) {
        const el = document.getElementById('migrate-modal-error');
        el.textContent = 'No target has sufficient resources for this VM.';
        el.style.display = '';
    }
}

let _migrationPollInterval = null;
let _migrationTimerInterval = null;
let _migrationStartTime = null;

function closeMigrateModal() {
    clearInterval(_migrationPollInterval);
    clearInterval(_migrationTimerInterval);
    _migrationPollInterval = null;
    _migrationTimerInterval = null;
    _migrationStartTime = null;
    // Reset to form view for next time
    document.getElementById('migrate-form-body').style.display = '';
    document.getElementById('migrate-progress-body').style.display = 'none';
    document.getElementById('migrate-modal').classList.remove('open');
    migrationVm = null;
}

async function submitMigration() {
    if (!migrationVm) return;
    const errEl = document.getElementById('migrate-modal-error');
    errEl.style.display = 'none';
    errEl.textContent = '';

    const sel = document.getElementById('migrate-target-node');
    const val = sel.value;
    if (!val || sel.selectedIndex === -1 || sel.options[sel.selectedIndex].disabled) {
        errEl.textContent = 'Please select a target.';
        errEl.style.display = '';
        return;
    }
    const sep = val.indexOf(':');
    const kind = val.slice(0, sep);          // 'node' | 'cluster'
    const target = val.slice(sep + 1);       // node name | cluster id

    const btn = document.querySelector('#migrate-form-body .btn:last-child');
    btn.disabled = true;
    btn.textContent = 'Starting…';

    try {
        if (kind === 'cluster') {
            // ── cross-cluster (decentralized) migration ──
            const targetName = sel.options[sel.selectedIndex].dataset.name || target;
            const resp = await fetch(
                `${apiUrl}/api/v1/edge-clusters/${clusterId}/${migrationVm.resource}/${migrationVm.namespace}/${migrationVm.vmName}/cross-migrate`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ target_cluster_id: target }),
                }
            );
            const body = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                errEl.textContent = body.detail || `Cross-cluster migration rejected (HTTP ${resp.status})`;
                errEl.style.display = '';
                btn.disabled = false; btn.textContent = 'Start Migration';
                return;
            }
            const tgtDisp = document.getElementById('mg-target-node-display');
            if (tgtDisp) tgtDisp.textContent = `cluster ${targetName}`;
            if (migrationVm.resource === 'all-pods')
                _startPodMigrationProgress(migrationVm.vmName, migrationVm.currentNode, `cluster ${targetName}`, body.migration_id);
            else
                _startCrossMigrationProgress(migrationVm.vmName, migrationVm.currentNode, targetName, body.migration_id);
        } else {
            // ── intra-cluster live migration (node → node) ──
            const resp = await fetch(
                `${apiUrl}/api/v1/edge-clusters/${clusterId}/${migrationVm.resource}/${migrationVm.namespace}/${migrationVm.vmName}/migrate`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ target_node: target }),
                }
            );
            const body = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                errEl.textContent = body.detail || `Migration failed (HTTP ${resp.status})`;
                errEl.style.display = '';
                btn.disabled = false; btn.textContent = 'Start Migration';
                return;
            }
            const tgtDisp = document.getElementById('mg-target-node-display');
            if (tgtDisp) tgtDisp.textContent = target;
            if (migrationVm.resource === 'all-pods')
                _startPodMigrationProgress(migrationVm.vmName, migrationVm.currentNode, target, body.migration_id);
            else
                _startMigrationProgress(migrationVm.vmName, migrationVm.currentNode, target);
        }
    } catch (e) {
        errEl.textContent = `Error: ${e.message}`;
        errEl.style.display = '';
        btn.disabled = false;
        btn.textContent = 'Start Migration';
    }
}

function _fmtElapsed(ms) {
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    return m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}

function _startMigrationProgress(vmName, sourceNode, targetNode) {
    document.getElementById('migrate-form-body').style.display = 'none';
    document.getElementById('migrate-progress-body').style.display = '';
    document.getElementById('migrate-modal-title').textContent = `Migrating: ${vmName}`;
    document.getElementById('migrate-progress-phase').textContent = 'Transferring memory state…';
    document.getElementById('migrate-progress-phase').style.color = '';
    document.getElementById('migrate-progress-detail').textContent = `${sourceNode} → ${targetNode}`;
    document.getElementById('migrate-progress-timer').textContent = '0s elapsed';
    document.getElementById('migrate-progress-timer').style.color = '';
    document.getElementById('migrate-steps').innerHTML = '';

    // Indeterminate progress — ramps to ~85% then stalls until success
    const fillEl = document.getElementById('migrate-progress-fill');
    if (fillEl) { fillEl.style.width = '0%'; fillEl.style.background = ''; }

    _migrationStartTime = Date.now();
    clearInterval(_migrationTimerInterval);
    _migrationTimerInterval = setInterval(() => {
        const ms = Date.now() - _migrationStartTime;
        const el = document.getElementById('migrate-progress-timer');
        if (el) el.textContent = _fmtElapsed(ms) + ' elapsed';
        // Smooth indeterminate fill: asymptotic curve reaching ~85% at 120s
        if (fillEl) {
            const pct = Math.min(85, (ms / 1000) / (ms / 1000 + 30) * 100);
            fillEl.style.width = pct.toFixed(1) + '%';
        }
    }, 1000);

    clearInterval(_migrationPollInterval);
    _migrationPollInterval = setInterval(() => _pollMigrationStatus(vmName, sourceNode, targetNode), 3000);
}

async function _pollMigrationStatus(vmName, sourceNode, targetNode) {
    try {
        const resp = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}/all-vms`);
        if (!resp.ok) return;
        const data = await resp.json();

        // Flatten namespaces dict into one list
        const allVms = Object.values(data.namespaces || {}).flat();
        const vm = allVms.find(v => v.name === vmName);
        if (!vm) return;

        const currentNode = vm.node;

        // Update the route display
        document.getElementById('migrate-progress-detail').textContent =
            `${currentNode || sourceNode} → ${targetNode}`;

        if (vm.phase === 'Failed') {
            clearInterval(_migrationPollInterval);
            clearInterval(_migrationTimerInterval);
            _migrationPollInterval = null;
            _migrationTimerInterval = null;
            const elapsed = _migrationStartTime ? _fmtElapsed(Date.now() - _migrationStartTime) : '?';
            const fillEl = document.getElementById('migrate-progress-fill');
            if (fillEl) { fillEl.style.width = '100%'; fillEl.style.background = 'var(--danger)'; }
            document.getElementById('migrate-progress-phase').style.color = 'var(--danger)';
            document.getElementById('migrate-progress-phase').textContent = 'Migration failed';
            document.getElementById('migrate-progress-timer').textContent = `Failed after ${elapsed}`;
            document.getElementById('migrate-steps').innerHTML =
                `<div class="alert alert-error">VM phase is Failed — check VM events for details.</div>`;
            return;
        }

        // Success: VM is now running on the target node
        if (currentNode === targetNode) {
            clearInterval(_migrationPollInterval);
            clearInterval(_migrationTimerInterval);
            _migrationPollInterval = null;
            _migrationTimerInterval = null;
            const elapsed = _migrationStartTime ? _fmtElapsed(Date.now() - _migrationStartTime) : '?';
            const fillEl = document.getElementById('migrate-progress-fill');
            if (fillEl) { fillEl.style.width = '100%'; fillEl.style.background = 'var(--success)'; }
            document.getElementById('migrate-progress-phase').style.color = 'var(--success)';
            document.getElementById('migrate-progress-phase').textContent = 'Migration complete';
            document.getElementById('migrate-progress-detail').textContent = `Now running on ${targetNode}`;
            document.getElementById('migrate-progress-timer').textContent = `Completed in ${elapsed}`;
            document.getElementById('migrate-progress-timer').style.color = 'var(--success)';
            document.getElementById('migrate-steps').innerHTML =
                `<div class="alert alert-success">VM successfully migrated to ${targetNode}</div>`;
            loadAllVMs();
            setTimeout(() => closeMigrateModal(), 2500);
        }
    } catch (_) { /* network blip */ }
}

// ── Cross-cluster (decentralized) migration progress ───────────────────────

function _startCrossMigrationProgress(vmName, sourceNode, targetClusterName, migrationId) {
    document.getElementById('migrate-form-body').style.display = 'none';
    document.getElementById('migrate-progress-body').style.display = '';
    document.getElementById('migrate-modal-title').textContent = `Migrating: ${vmName}`;
    document.getElementById('migrate-progress-phase').textContent = 'Cross-cluster migration…';
    document.getElementById('migrate-progress-phase').style.color = '';
    document.getElementById('migrate-progress-detail').textContent =
        `${sourceNode || 'source'} → cluster ${targetClusterName}`;
    document.getElementById('migrate-progress-timer').textContent = '0s elapsed';
    document.getElementById('migrate-progress-timer').style.color = '';
    document.getElementById('migrate-steps').innerHTML = '';

    const fillEl = document.getElementById('migrate-progress-fill');
    if (fillEl) { fillEl.style.width = '0%'; fillEl.style.background = ''; }

    _migrationStartTime = Date.now();
    clearInterval(_migrationTimerInterval);
    _migrationTimerInterval = setInterval(() => {
        const ms = Date.now() - _migrationStartTime;
        const el = document.getElementById('migrate-progress-timer');
        if (el) el.textContent = _fmtElapsed(ms) + ' elapsed';
        if (fillEl) {  // cross-cluster is slower; ramp to ~85% over ~120s
            const s = ms / 1000;
            fillEl.style.width = Math.min(85, s / (s + 60) * 100).toFixed(1) + '%';
        }
    }, 1000);

    clearInterval(_migrationPollInterval);
    _migrationPollInterval = setInterval(
        () => _pollCrossMigrationStatus(migrationId, targetClusterName), 3000);
}

// ── Pod migration progress (polls /pod-migrations/{id}) ─────────────────────
const _POD_PHASES = {
    'starting': 'Starting…',
    'creating target': 'Preparing target on destination node…',
    'migrating': 'Live-migrating — CRIU post-copy…',
    'completed': 'Migration complete',
    'failed': 'Migration failed',
};

function _startPodMigrationProgress(podName, sourceNode, targetLabel, migrationId) {
    document.getElementById('migrate-form-body').style.display = 'none';
    document.getElementById('migrate-progress-body').style.display = '';
    document.getElementById('migrate-modal-title').textContent = `Migrating: ${podName}`;
    document.getElementById('migrate-progress-phase').textContent = 'Starting…';
    document.getElementById('migrate-progress-phase').style.color = '';
    document.getElementById('migrate-progress-detail').textContent = `${sourceNode || 'source'} → ${targetLabel}`;
    document.getElementById('migrate-progress-timer').textContent = '0s elapsed';
    document.getElementById('migrate-progress-timer').style.color = '';
    document.getElementById('migrate-steps').innerHTML = '';
    const fillEl = document.getElementById('migrate-progress-fill');
    if (fillEl) { fillEl.style.width = '0%'; fillEl.style.background = ''; }

    _migrationStartTime = Date.now();
    clearInterval(_migrationTimerInterval);
    _migrationTimerInterval = setInterval(() => {
        const ms = Date.now() - _migrationStartTime;
        const el = document.getElementById('migrate-progress-timer');
        if (el) el.textContent = _fmtElapsed(ms) + ' elapsed';
        if (fillEl) { const s = ms / 1000; fillEl.style.width = Math.min(90, s / (s + 15) * 100).toFixed(1) + '%'; }
    }, 1000);

    clearInterval(_migrationPollInterval);
    _migrationPollInterval = setInterval(() => _pollPodMigrationStatus(migrationId), 2000);
}

async function _pollPodMigrationStatus(migrationId) {
    try {
        const resp = await fetch(`${apiUrl}/api/v1/pod-migrations/${migrationId}`);
        if (!resp.ok) return;
        const m = await resp.json();
        const phaseEl = document.getElementById('migrate-progress-phase');
        if (phaseEl) phaseEl.textContent = _POD_PHASES[m.phase] || m.phase;
        if (!(m.completed || m.failed)) return;

        clearInterval(_migrationPollInterval);
        clearInterval(_migrationTimerInterval);
        const fillEl = document.getElementById('migrate-progress-fill');
        const detEl = document.getElementById('migrate-progress-detail');
        if (m.completed) {
            if (fillEl) fillEl.style.width = '100%';
            if (phaseEl) phaseEl.style.color = 'var(--success)';
            if (detEl && m.freeze_s != null) detEl.textContent += `  ·  freeze window ~${m.freeze_s}s`;
            setTimeout(() => { closeMigrateModal(); loadAllPods(); }, 2600);
        } else {
            if (fillEl) { fillEl.style.width = '100%'; fillEl.style.background = 'var(--danger)'; }
            if (phaseEl) phaseEl.style.color = 'var(--danger)';
            if (detEl) detEl.textContent = (m.error || 'migration failed').slice(0, 240);
        }
    } catch (_) { /* transient; keep polling */ }
}

async function _pollCrossMigrationStatus(migrationId, targetClusterName) {
    try {
        const resp = await fetch(`${apiUrl}/api/v1/cross-migrations/${migrationId}`);
        if (!resp.ok) return;
        const d = await resp.json();
        const logs = d.logs || [];
        // strip "LEVEL: [migration-id] " prefix for a readable phase line
        const last = logs.length ? logs[logs.length - 1].replace(/^\w+:\s*\[[^\]]*\]\s*/, '') : '';
        if (last) document.getElementById('migrate-progress-phase').textContent = last;

        if (d.status === 'succeeded') {
            clearInterval(_migrationPollInterval); clearInterval(_migrationTimerInterval);
            _migrationPollInterval = null; _migrationTimerInterval = null;
            const elapsed = _migrationStartTime ? _fmtElapsed(Date.now() - _migrationStartTime) : '?';
            const fillEl = document.getElementById('migrate-progress-fill');
            if (fillEl) { fillEl.style.width = '100%'; fillEl.style.background = 'var(--success)'; }
            document.getElementById('migrate-progress-phase').style.color = 'var(--success)';
            document.getElementById('migrate-progress-phase').textContent = 'Migration complete';
            document.getElementById('migrate-progress-detail').textContent =
                `Now running on cluster ${targetClusterName}`;
            document.getElementById('migrate-progress-timer').textContent = `Completed in ${elapsed}`;
            document.getElementById('migrate-progress-timer').style.color = 'var(--success)';
            document.getElementById('migrate-steps').innerHTML =
                `<div class="alert alert-success">VM migrated to cluster ${targetClusterName}. ` +
                `It no longer runs in this cluster.</div>`;
            loadAllVMs();
            setTimeout(() => closeMigrateModal(), 3000);
        } else if (d.status === 'failed') {
            clearInterval(_migrationPollInterval); clearInterval(_migrationTimerInterval);
            _migrationPollInterval = null; _migrationTimerInterval = null;
            const elapsed = _migrationStartTime ? _fmtElapsed(Date.now() - _migrationStartTime) : '?';
            const fillEl = document.getElementById('migrate-progress-fill');
            if (fillEl) { fillEl.style.width = '100%'; fillEl.style.background = 'var(--danger)'; }
            document.getElementById('migrate-progress-phase').style.color = 'var(--danger)';
            document.getElementById('migrate-progress-phase').textContent = 'Migration failed';
            document.getElementById('migrate-progress-timer').textContent = `Failed after ${elapsed}`;
            const err = (d.result && d.result.error) || last || 'see logs';
            document.getElementById('migrate-steps').innerHTML =
                `<div class="alert alert-error">Cross-cluster migration failed: ${err}</div>`;
        }
    } catch (_) { /* network blip — keep polling */ }
}

// Close migrate modal on overlay click
document.addEventListener('DOMContentLoaded', () => {
    const overlay = document.getElementById('migrate-modal');
    if (overlay) overlay.addEventListener('click', e => { if (e.target === overlay) closeMigrateModal(); });
    const anOverlay = document.getElementById('add-node-modal');
    if (anOverlay) anOverlay.addEventListener('click', e => { if (e.target === anOverlay) closeAddNodeModal(); });

});

// ── Add Node ──────────────────────────────────────────────────────────────────

function openAddNodeModal() {
    document.getElementById('add-node-name').value = '';
    document.getElementById('add-node-host').value = '';
    document.getElementById('add-node-user').value = 'root';
    document.getElementById('add-node-ssh-key').value = '';
    document.getElementById('add-node-role').value = 'agent';
    document.getElementById('add-node-modal-error').textContent = '';
    const btn = document.getElementById('add-node-submit-btn');
    btn.disabled = false;
    btn.textContent = 'Add Node';
    document.getElementById('add-node-modal').classList.add('open');
}

function closeAddNodeModal() {
    document.getElementById('add-node-modal').classList.remove('open');
    loadCluster();
}

async function submitAddNode() {
    const errEl = document.getElementById('add-node-modal-error');
    const btn = document.getElementById('add-node-submit-btn');
    errEl.textContent = '';

    const name = document.getElementById('add-node-name').value.trim();
    const host = document.getElementById('add-node-host').value.trim();
    const user = document.getElementById('add-node-user').value.trim() || 'root';
    const role = document.getElementById('add-node-role').value;
    const sshKey = document.getElementById('add-node-ssh-key').value.trim() || null;

    if (!name) { errEl.textContent = 'Node name is required.'; return; }
    if (!host) { errEl.textContent = 'Host / IP is required.'; return; }

    btn.disabled = true;
    btn.textContent = 'Submitting…';

    try {
        const resp = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}/nodes`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, host, user, roles: [role], ssh_key_path: sshKey }),
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            errEl.textContent = data.detail || `Error ${resp.status}`;
            btn.disabled = false;
            btn.textContent = 'Add Node';
            return;
        }
        // Close and switch to Nodes tab — the Joining… row provides feedback
        closeAddNodeModal();
        const nodesTabBtn = document.querySelector('.tab[onclick*="nodes"]');
        if (nodesTabBtn) nodesTabBtn.click();
    } catch (e) {
        errEl.textContent = `Error: ${e.message}`;
        btn.disabled = false;
        btn.textContent = 'Add Node';
    }
}




// ═══════════════════════════════════════════════════════════════════════════
// CHAT TAB — AI Cluster Assistant
// ═══════════════════════════════════════════════════════════════════════════
