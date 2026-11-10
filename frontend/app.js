// DRIFT Edge Cluster Control Panel - JavaScript

console.log('DRIFT Control Panel - JavaScript loaded');

// Global state
// Auto-detect the API URL based on current host
const defaultApiUrl = window.location.hostname === 'localhost'
    ? 'http://localhost:8000'
    : `http://${window.location.hostname}:8000`;
let apiUrl = localStorage.getItem('apiUrl') || defaultApiUrl;
console.log('API URL configured:', apiUrl);
console.log('Current hostname:', window.location.hostname);
console.log('Default API URL:', defaultApiUrl);
let nodeCounter = 0;
let networkCounter = 0;
let pollingInterval = null;
let _cachedClusters = [];

// Relative time helper — e.g. "5m", "2h", "3d"
function relativeTime(date) {
    const secs = Math.floor((Date.now() - date.getTime()) / 1000);
    if (secs < 60)  return `${secs}s`;
    if (secs < 3600) return `${Math.floor(secs / 60)}m`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
    return `${Math.floor(secs / 86400)}d`;
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    loadSettings();
    loadFollowMe();   // landing section — the live demo dashboard
    loadSites();
    loadClusters();
    addNode(); // Add first node by default

    // Set up form handlers
    document.getElementById('cluster-form').addEventListener('submit', handleClusterSubmit);
    document.getElementById('site-form').addEventListener('submit', handleSiteSubmit);
    // Cluster list auto-refresh — timer is managed by _rescheduleDashboardPoll()
    // called at the end of loadClusters(); kick it off after first load.
});

// Tab switching
function switchTab(tabName, event) {
    // Hide all tabs
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.remove('active');
    });
    document.querySelectorAll('.tab').forEach(tab => {
        tab.classList.remove('active');
    });

    // Show selected tab
    document.getElementById(`${tabName}-tab`).classList.add('active');

    // Activate the corresponding tab button
    if (event && event.target) {
        event.target.classList.add('active');
    } else {
        // If no event, find the tab button by its onclick attribute
        const tabs = document.querySelectorAll('.tab');
        tabs.forEach(tab => {
            const onclick = tab.getAttribute('onclick');
            if (onclick && onclick.includes(`'${tabName}'`)) {
                tab.classList.add('active');
            }
        });
    }

    // Load data when switching to certain tabs
    if (tabName === 'list') {
        loadClusters();
    } else if (tabName === 'sites') {
        loadSites();
    } else if (tabName === 'networks') {
        loadL2Networks();
    } else if (tabName === 'create') {
        document.getElementById('nodes-container').innerHTML = '';
        nodeCounter = 0;
        networkCounter = 0;
        addNode();
    }
}

// Settings management
function loadSettings() {
    const saved = localStorage.getItem('apiUrl');
    if (saved) {
        apiUrl = saved;
        document.getElementById('api-url').value = apiUrl;
    }
}

function saveSettings() {
    apiUrl = document.getElementById('api-url').value;
    localStorage.setItem('apiUrl', apiUrl);
    showAlert('settings', 'Settings saved successfully!', 'success');
}

// Health check
async function checkHealth() {
    const resultDiv = document.getElementById('health-result');
    resultDiv.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const response = await fetch(`${apiUrl}/health`);
        const data = await response.json();

        resultDiv.innerHTML = `
            <div class="alert alert-success">
                <strong>✅ API Connected</strong><br>
                Status: ${data.status}<br>
                Timestamp: ${new Date(data.timestamp).toLocaleString()}
            </div>
        `;
    } catch (error) {
        resultDiv.innerHTML = `
            <div class="alert alert-error">
                <strong>❌ Connection Failed</strong><br>
                ${error.message}
            </div>
        `;
    }
}


// Sites management
async function loadSites() {
    try {
        const [response, clustersResp] = await Promise.all([
            fetch(`${apiUrl}/api/v1/sites`),
            fetch(`${apiUrl}/api/v1/edge-clusters`).catch(() => null),
        ]);
        const data = await response.json();
        const clustersData = clustersResp ? await clustersResp.json().catch(() => ({ clusters: [] })) : { clusters: [] };
        console.log('Sites data:', data);

        // Update site dropdown in cluster form
        const siteSelect = document.getElementById('site-id');
        console.log('Site select element:', siteSelect);
        siteSelect.innerHTML = '<option value="">-- Select Site --</option>';
        console.log('Number of sites:', data.sites.length);
        data.sites.forEach(site => {
            const option = document.createElement('option');
            option.value = site.id;
            option.textContent = `${site.name} (${site.location || 'No location'})`;
            siteSelect.appendChild(option);
            console.log('Added site option:', site.name);
        });

        // Update sites list
        const sitesList = document.getElementById('sites-list');
        if (data.sites.length === 0) {
            sitesList.innerHTML = '<p style="color: #718096;">No sites found. Create one above.</p>';
        } else {
            sitesList.innerHTML = data.sites.map(site => {
                const hasCoords = site.latitude != null && site.longitude != null;
                const coordsHtml = hasCoords
                    ? `<span style="color:var(--text-muted);font-size:.8em;">${site.latitude.toFixed(4)}, ${site.longitude.toFixed(4)}</span>`
                    : `<span style="color:var(--text-muted);font-size:.8em;">No coordinates set</span>`;
                const editArgs = `'${site.id}','${(site.location||'').replace(/'/g,"\\'")}',${site.latitude ?? 'null'},${site.longitude ?? 'null'}`;
                return `
                <div class="cluster-item">
                    <div class="cluster-header">
                        <div class="cluster-name">${site.name}</div>
                    </div>
                    <div class="cluster-details">
                        ${site.location ? `<strong>Location:</strong> ${site.location}<br>` : ''}
                        ${coordsHtml}<br>
                        <strong>Created:</strong> ${new Date(site.created_at).toLocaleString()}
                    </div>
                    <div class="cluster-footer">
                        <div class="cluster-actions">
                            <button class="btn btn-sm" onclick="openEditSiteModal(${editArgs})">Edit</button>
                            <button class="btn btn-danger btn-sm" onclick="deleteSite('${site.id}', '${site.name}')">Delete</button>
                        </div>
                    </div>
                </div>`;
            }).join('');
        }
    } catch (error) {
        console.error('Error loading sites:', error);
        showAlert('site', `Error loading sites: ${error.message}`, 'error');
    }
}

async function handleSiteSubmit(e) {
    e.preventDefault();

    const latVal = document.getElementById('site-lat').value;
    const lonVal = document.getElementById('site-lon').value;
    const lat = latVal !== '' ? parseFloat(latVal) : null;
    const lon = lonVal !== '' ? parseFloat(lonVal) : null;
    const siteData = {
        name: document.getElementById('site-name').value,
        location: document.getElementById('site-location').value,
        ...(lat !== null && { latitude: lat }),
        ...(lon !== null && { longitude: lon }),
    };

    try {
        const response = await fetch(`${apiUrl}/api/v1/sites`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(siteData)
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create site');
        }

        const result = await response.json();
        showAlert('site', `Site "${result.name}" created successfully!`, 'success');
        document.getElementById('site-form').reset();
        loadSites();
    } catch (error) {
        showAlert('site', `Error: ${error.message}`, 'error');
    }
}

async function deleteSite(siteId, siteName) {
    if (!confirm(`Delete site "${siteName}"?\n\nThis cannot be undone.`)) return;
    try {
        const response = await fetch(`${apiUrl}/api/v1/sites/${siteId}`, { method: 'DELETE' });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to delete site');
        }
        showAlert('site', `Site "${siteName}" deleted.`, 'success');
        loadSites();
    } catch (error) {
        showAlert('site', `Error: ${error.message}`, 'error');
    }
}


function openEditSiteModal(siteId, location, lat, lon) {
    document.getElementById('site-edit-id').value = siteId;
    document.getElementById('site-edit-location').value = location || '';
    document.getElementById('site-edit-lat').value = lat != null ? lat : '';
    document.getElementById('site-edit-lon').value = lon != null ? lon : '';
    document.getElementById('site-edit-error').style.display = 'none';
    document.getElementById('site-edit-modal').classList.add('open');
}

function closeEditSiteModal() {
    document.getElementById('site-edit-modal').classList.remove('open');
}

async function submitEditSite() {
    const siteId = document.getElementById('site-edit-id').value;
    const errEl  = document.getElementById('site-edit-error');
    errEl.style.display = 'none';

    const latVal = document.getElementById('site-edit-lat').value;
    const lonVal = document.getElementById('site-edit-lon').value;
    const lat = latVal !== '' ? parseFloat(latVal) : undefined;
    const lon = lonVal !== '' ? parseFloat(lonVal) : undefined;

    const body = {
        location: document.getElementById('site-edit-location').value || null,
        ...(lat !== undefined && { latitude: lat }),
        ...(lon !== undefined && { longitude: lon }),
    };

    try {
        const resp = await fetch(`${apiUrl}/api/v1/sites/${siteId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        closeEditSiteModal();
        loadSites();
    } catch (e) {
        errEl.textContent = e.message;
        errEl.style.display = '';
    }
}

// Close modal on overlay click
document.addEventListener('DOMContentLoaded', () => {
    const overlay = document.getElementById('site-edit-modal');
    if (overlay) overlay.addEventListener('click', e => { if (e.target === overlay) closeEditSiteModal(); });
});

// ── Node management ───────────────────────────────────────────────────────

function addNode() {
    nodeCounter++;
    const container = document.getElementById('nodes-container');

    const nodeCard = document.createElement('div');
    nodeCard.className = 'node-card';
    nodeCard.id = `node-${nodeCounter}`;
    nodeCard.innerHTML = `
        <h3>Node ${nodeCounter}</h3>
        ${nodeCounter > 1 ? '<button type="button" class="remove-node-btn" onclick="removeNode(' + nodeCounter + ')">Remove</button>' : ''}

        <div class="grid-2">
            <div class="form-group">
                <label>Node Name *</label>
                <input type="text" class="node-name" required placeholder="edge-node-${nodeCounter}">
            </div>
            <div class="form-group">
                <label>IP Address / Hostname *</label>
                <input type="text" class="node-host" required placeholder="10.64.45.137">
            </div>
        </div>

        <div class="grid-2">
            <div class="form-group">
                <label>SSH Username *</label>
                <input type="text" class="node-user" required placeholder="ubuntu">
            </div>
            <div class="form-group">
                <label>SSH Key Path</label>
                <input type="text" class="node-key" placeholder="/home/user/.ssh/id_rsa">
            </div>
        </div>

        <div class="form-group">
            <label>Node Role *</label>
            <select class="node-role" required>
                <option value="server">Server (Control Plane)</option>
                <option value="agent">Agent (Worker)</option>
            </select>
        </div>

        <div class="interface-mappings-container" id="mappings-container-${nodeCounter}" style="display:none"></div>
    `;

    container.appendChild(nodeCard);
}


function removeNode(id) {
    const node = document.getElementById(`node-${id}`);
    if (node) {
        node.remove();
    }
}


// Cluster form submission
async function handleClusterSubmit(e) {
    e.preventDefault();

    const submitBtn = document.getElementById('submit-btn');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Creating...';

    try {
        // Collect nodes with interface mappings
        const nodes = [];
        document.querySelectorAll('.node-card').forEach(nodeCard => {
            const name = nodeCard.querySelector('.node-name').value;
            const host = nodeCard.querySelector('.node-host').value;
            const user = nodeCard.querySelector('.node-user').value;
            const keyPath = nodeCard.querySelector('.node-key').value;
            const role = nodeCard.querySelector('.node-role').value;

            const node = {
                name,
                host,
                user,
                roles: [role]
            };

            if (keyPath) {
                node.ssh_key_path = keyPath;
            }

            nodes.push(node);
        });

        // Build request payload
        const clusterData = {
            name: document.getElementById('cluster-name').value,
            site_id: document.getElementById('site-id').value,
            cluster_type: document.getElementById('cluster-type').value,
            cni_plugin: document.getElementById('cni-plugin').value,
            pod_cidr: document.getElementById('pod-cidr').value,
            service_cidr: document.getElementById('service-cidr').value,
            enable_longhorn: document.getElementById('enable-longhorn').checked,
            enable_kubevirt: document.getElementById('enable-kubevirt').checked,
            enable_multus: document.getElementById('enable-multus').checked,
            enable_monitoring: document.getElementById('enable-monitoring').checked,
            nodes
        };

        const k8sVersion = document.getElementById('k8s-version').value;
        if (k8sVersion) clusterData.kubernetes_version = k8sVersion;

        // Pod live-migration ⇒ CRI-O runtime (containerd can't restore). The
        // orchestrator reads meta_data.container_runtime; when "crio" it installs
        // CRIU + CRI-O (kubelet on CRI-O) and the pod-migration layer (crun-drift,
        // node-agent, macvlan-l2 NAD, NFS checkpoint store).
        const podMig = document.getElementById('enable-pod-migration');
        if (podMig && podMig.checked) {
            clusterData.meta_data = Object.assign({}, clusterData.meta_data, { container_runtime: 'crio' });
        }

        // Send request
        console.log('Sending cluster creation request:', clusterData);
        console.log('API URL:', `${apiUrl}/api/v1/edge-clusters`);

        const response = await fetch(`${apiUrl}/api/v1/edge-clusters`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(clusterData)
        });

        console.log('Response status:', response.status);
        console.log('Response ok:', response.ok);

        if (!response.ok) {
            const error = await response.json();
            console.error('Error response:', error);
            throw new Error(error.detail || 'Failed to create cluster');
        }

        const result = await response.json();

        showAlert('create', `
            <strong>Cluster provisioning started</strong><br>
            <span style="opacity:.8">${result.name} is being set up. Redirecting to Edge Clusters…</span>
        `, 'success');

        // Reset form
        document.getElementById('cluster-form').reset();
        document.getElementById('nodes-container').innerHTML = '';
        nodeCounter = 0;
        networkCounter = 0;
        addNode();

        // Switch to cluster list after 2 seconds
        setTimeout(() => switchTab('list'), 2000);

    } catch (error) {
        console.error('Cluster creation error:', error);
        console.error('Error stack:', error.stack);
        showAlert('create', `Error: ${error.message}`, 'error');
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Create Edge Cluster';
    }
}

// ── Dashboard auto-refresh ─────────────────────────────────────────────────
// Polls fast (5 s) while any cluster is transient, slow (20 s) when idle.
// Only fires when the "clusters" section is visible.
const _TRANSIENT_STATES = new Set(['provisioning', 'pending', 'deleting', 'creating']);
let _dashboardPollTimer = null;

function _rescheduleDashboardPoll(clusters) {
    clearInterval(_dashboardPollTimer);
    const anyTransient = (clusters || []).some(c => _TRANSIENT_STATES.has((c.status || '').toLowerCase()));
    if (anyTransient) {
        // Poll every 5 s unconditionally while any cluster is in a transient state,
        // regardless of which section is visible — keeps the list live as soon as
        // the user navigates back to Edge Clusters.
        _dashboardPollTimer = setInterval(loadClusters, 5000);
    } else {
        // All stable — only refresh when the clusters section is actually open.
        _dashboardPollTimer = setInterval(() => {
            const sec = document.getElementById('section-clusters');
            if (sec && sec.classList.contains('active')) loadClusters();
        }, 20000);
    }
}

// Load and display clusters — grouped by site, with nested user clusters
// ── Cross-Cluster Migration section (full-mesh, N clusters) ─────────────────
let _pairPollTimer = null;
async function loadXcluster() {
    const picker = document.getElementById('mesh-cluster-picker');
    const err  = document.getElementById('pair-modal-error');
    err.textContent = '';
    document.getElementById('pair-progress-body').style.display = 'none';
    const btn = document.getElementById('pair-submit-btn');
    btn.disabled = false; btn.textContent = 'Wire Cross-Cluster Mesh'; btn.onclick = submitMesh;
    picker.innerHTML = '<div class="loading"><div class="spinner"></div> Loading clusters…</div>';
    try {
        const resp = await fetch(`${apiUrl}/api/v1/edge-clusters`);
        const data = await resp.json();
        const clusters = (data.clusters || []).filter(c => c.status === 'active');
        if (clusters.length < 2) {
            picker.innerHTML = '';
            err.textContent = 'Need at least two active clusters to wire a mesh.';
        } else {
            // all checked by default — the common case is "mesh everything"
            picker.innerHTML = clusters.map(c => `
                <label style="display:flex;align-items:center;gap:9px;font-size:.9em;cursor:pointer;">
                    <input type="checkbox" class="mesh-cluster-cb" value="${c.id}" checked>
                    <span style="font-family:monospace;font-weight:600;">${c.name}</span>
                    <span style="color:var(--text-muted);font-size:.85em;">${c.pod_cidr}</span>
                </label>`).join('');
        }
    } catch (e) {
        picker.innerHTML = '';
        err.textContent = 'Could not load clusters: ' + e.message;
    }
    listMeshRuns();
}
async function listMeshRuns() {
    const el = document.getElementById('pairings-list');
    if (!el) return;
    try {
        const runs = await (await fetch(`${apiUrl}/api/v1/cluster-mesh`)).json();
        if (!Array.isArray(runs) || runs.length === 0) {
            el.innerHTML = `<div class="empty-hint" style="color:var(--text-muted);font-size:.85em;padding:10px 2px;">No mesh wired yet. Select clusters above and wire the mesh to enable cross-cluster migration between them.</div>`;
            return;
        }
        const dot = s => s.completed ? '#2e9e4f' : (s.failed ? '#c0392b' : '#d69a00');
        const label = s => s.completed ? 'meshed' : (s.failed ? 'failed' : (s.phase || 'working…'));
        el.innerHTML = runs.map(s => `
            <div class="site-group" style="display:flex;align-items:center;gap:10px;padding:10px 14px;">
                <span style="width:8px;height:8px;border-radius:50%;background:${dot(s)};flex-shrink:0;"></span>
                <span style="font-family:monospace;font-weight:600;">${(s.clusters || []).join(' ⇄ ')}</span>
                <span style="margin-left:auto;color:var(--text-muted);font-size:.82em;">${label(s)}</span>
            </div>`).join('');
    } catch (e) {
        el.innerHTML = `<div style="color:var(--text-muted);font-size:.85em;">Could not load mesh runs.</div>`;
    }
}
async function submitMesh() {
    const ids = Array.from(document.querySelectorAll('.mesh-cluster-cb:checked')).map(cb => cb.value);
    const err = document.getElementById('pair-modal-error');
    err.textContent = '';
    if (ids.length < 2) { err.textContent = 'Select at least two clusters.'; return; }
    const btn = document.getElementById('pair-submit-btn');
    btn.disabled = true; btn.textContent = 'Wiring…';
    try {
        const resp = await fetch(`${apiUrl}/api/v1/cluster-mesh`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cluster_ids: ids }),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
        document.getElementById('pair-progress-body').style.display = '';
        const txt = document.getElementById('pair-progress-text');
        listMeshRuns();
        _pairPollTimer = setInterval(async () => {
            try {
                const s = await (await fetch(`${apiUrl}/api/v1/cluster-mesh/${body.mesh_id}`)).json();
                txt.textContent = s.phase || 'working…';
                listMeshRuns();
                if (s.completed || s.failed) {
                    clearInterval(_pairPollTimer); _pairPollTimer = null;
                    document.getElementById('pair-progress-body').style.display = 'none';
                    btn.disabled = false; btn.textContent = 'Wire Cross-Cluster Mesh';
                    if (s.failed) err.textContent = 'Mesh wiring failed: ' + (s.error || 'see backend logs');
                }
            } catch (_) { /* transient poll error */ }
        }, 3000);
    } catch (e) {
        err.textContent = e.message;
        btn.disabled = false; btn.textContent = 'Wire Cross-Cluster Mesh';
    }
}

async function loadClusters() {
    const container = document.getElementById('cluster-list');
    container.className = 'cluster-tree';

    try {
        const [clResp, siteResp] = await Promise.all([
            fetch(`${apiUrl}/api/v1/edge-clusters`),
            fetch(`${apiUrl}/api/v1/sites`).catch(() => null),
        ]);
        if (!clResp.ok) throw new Error('Failed to fetch clusters');

        const data     = await clResp.json();
        const sitesData = siteResp ? await siteResp.json().catch(() => ({ sites: [] })) : { sites: [] };
        const siteMap  = {};
        (sitesData.sites || []).forEach(s => { siteMap[String(s.id)] = s; });

        if (data.total === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-title">No clusters yet</div>
                    <div class="empty-state-desc">Use <strong>Deploy Cluster</strong> to provision one.</div>
                </div>`;
            _cachedClusters = [];
            _rescheduleDashboardPoll([]);
            return;
        }

        // Render a single cluster row
        const renderCluster = c => {
            const isActive   = c.status === 'active';
            const isDeleting = c.status === 'deleting';
            const nodes  = c.nodes_status?.length > 0
                ? c.nodes_status
                : (c.nodes || []).map(n => ({ name: n.name || n.host, ready: null }));
            const ready  = nodes.filter(n => n.ready === true).length;
            const total  = nodes.length;
            const cni    = c.cni_plugin || '';
            const ver    = (c.kubernetes_version || '').replace(/\+.*/, ''); // strip +k3s1
            const age    = (() => { const ts = c.provisioned_at || c.created_at; return ts ? relativeTime(new Date(ts + (ts.endsWith('Z') ? '' : 'Z'))) : ''; })();
            const addons = c.addon_statuses || {};
            // "active" only means provisioning finished; c.health is the live probe
            // (healthy | degraded | unreachable). Show the real state, not just active.
            const problem  = isActive && (c.health === 'unreachable' || c.health === 'degraded');
            const dotColor = problem
                ? (c.health === 'unreachable' ? 'var(--danger)' : 'var(--warning)')
                : ({ active:'var(--success)', failed:'var(--danger)', deleting:'var(--danger)' }[c.status] || 'var(--warning)');

            return `
            <div id="cluster-${c.id}">
                <div class="cr"${isActive ? ` onclick="window.location.href='cluster.html?id=${c.id}'" style="cursor:pointer;"` : ''}>
                    <div class="cr-dot" style="background:${dotColor};box-shadow:0 0 6px ${dotColor}60;"></div>
                    <div class="cr-info">
                        <div class="cr-name">${c.name}</div>
                        <div class="cr-meta">${c.cluster_type.toUpperCase()}${cni ? ' · ' + cni : ''}${age ? ' · ' + age : ''}</div>
                    </div>
                    ${isActive && total > 0 ? `<div class="cr-nodes"><span class="nr">${ready}</span>/${total} nodes</div>` : ''}
                    ${ver ? `<div class="cr-version">${ver}</div>` : ''}
                    <span class="status-badge status-${problem ? (c.health === 'unreachable' ? 'failed' : 'provisioning') : c.status}" style="font-size:.6em;">${isDeleting ? 'Deleting' : (problem ? c.health : c.status)}</span>
                    <div class="cr-actions">
                        ${isActive   ? `<button class="btn btn-sm" onclick="event.stopPropagation();openCluster('${c.id}')">Open</button>` : ''}
                        ${!isDeleting && !isActive ? `<button class="btn btn-ghost btn-sm" onclick="viewClusterDetails('${c.id}')">Details</button>` : ''}
                        ${isActive   ? `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();downloadKubeconfig('${c.id}','${c.name}')">Kubeconfig</button>` : ''}
                        ${!isDeleting ? `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();viewLogs('${c.id}')">Logs</button>` : ''}
                        ${!isDeleting && c.status !== 'deleted' ? `<button class="btn btn-danger btn-sm" onclick="event.stopPropagation();deleteCluster('${c.id}','${c.name}')">Delete</button>` : ''}
                        ${isDeleting ? `<button class="btn btn-danger btn-sm" onclick="event.stopPropagation();forceDeleteCluster('${c.id}','${c.name}')">Force Delete</button>` : ''}
                    </div>
                </div>
                ${!isActive ? `
                <div class="cr-sub">
                    ${isDeleting
                        ? `<div class="progress-bar"><div class="progress-fill deleting progress-indeterminate"></div></div>
                           <div class="cr-sub-label">${c.current_phase || 'Teardown…'}</div>`
                        : `<div class="progress-bar"><div class="progress-fill" style="width:${c.progress_percent || 0}%"></div></div>
                           <div class="cr-sub-label">${c.current_phase || 'Initializing'} — ${c.progress_percent || 0}%</div>`}
                </div>` : ''}
                ${c.error_message ? `<div class="cr-sub"><div class="alert alert-error" style="padding:6px 10px;font-size:.78em;">${c.error_message}</div></div>` : ''}
            </div>`;
        };

        // Group by site
        const groups = {}, noSite = [];
        data.clusters.forEach(c => {
            const sid = String(c.site_id);
            if (c.site_id && siteMap[sid]) {
                if (!groups[sid]) groups[sid] = { site: siteMap[sid], clusters: [] };
                groups[sid].clusters.push(c);
            } else { noSite.push(c); }
        });

        let html = '';
        Object.values(groups).forEach(g => {
            html += `<div class="site-group">
                <div class="site-group-header">
                    <span class="site-group-name">${g.site.name}</span>
                    ${g.site.location ? `<span class="site-group-loc">${g.site.location}</span>` : ''}
                    <span class="site-group-count">${g.clusters.length} cluster${g.clusters.length !== 1 ? 's' : ''}</span>
                </div>
                ${g.clusters.map(renderCluster).join('')}
            </div>`;
        });
        if (noSite.length) html += `<div class="site-group">${noSite.map(renderCluster).join('')}</div>`;

        container.innerHTML = html;

        // Cache for poll scheduler and map
        _cachedClusters = data.clusters || [];
        _rescheduleDashboardPoll(_cachedClusters);

    } catch (error) {
        container.innerHTML = `<div class="alert alert-error">Error loading clusters: ${error.message}</div>`;
    }
}

// View cluster details
async function viewClusterDetails(clusterId) {
    try {
        const response = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}`);
        const cluster = await response.json();

        alert(`
Cluster Details:
================
Name: ${cluster.name}
Status: ${cluster.status}
Progress: ${cluster.progress_percent}%
Current Phase: ${cluster.current_phase || 'N/A'}

Configuration:
- Type: ${cluster.cluster_type}
- CNI: ${cluster.cni_plugin}
- K8s Version: ${cluster.kubernetes_version || 'default'}
- Pod CIDR: ${cluster.pod_cidr}
- Service CIDR: ${cluster.service_cidr}

Addons:
- Virtualization (KubeVirt): ${cluster.enable_kubevirt ? 'Yes' : 'No'}
- Storage (Longhorn): always on
- Multi-network (Multus): always on

Nodes: ${cluster.nodes.length}
${cluster.nodes.map((n, i) => `  ${i+1}. ${n.name} (${n.host}) - ${n.roles.join(', ')}`).join('\n')}

${cluster.error_message ? `\nError: ${cluster.error_message}` : ''}
        `.trim());
    } catch (error) {
        alert(`Error fetching cluster details: ${error.message}`);
    }
}

// Download kubeconfig
async function downloadKubeconfig(clusterId, clusterName) {
    try {
        const response = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}/kubeconfig`);

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to download kubeconfig');
        }

        const kubeconfig = await response.text();

        // Create download
        const blob = new Blob([kubeconfig], { type: 'text/yaml' });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${clusterName}-kubeconfig.yaml`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);

        alert('Kubeconfig downloaded successfully!');
    } catch (error) {
        alert(`Error downloading kubeconfig: ${error.message}`);
    }
}

// View logs
async function viewLogs(clusterId) {
    try {
        const response = await fetch(`${apiUrl}/api/v1/logs?entity_id=${clusterId}&entity_type=management_cluster&limit=50`);
        const data = await response.json();

        if (data.total === 0) {
            alert('No logs found for this cluster.');
            return;
        }

        const logsHtml = data.logs.map(log => `
            <div class="log-entry">
                <span class="log-timestamp">${new Date(log.timestamp).toLocaleString()}</span>
                <span class="log-level-${log.level}">[${log.level}]</span>
                ${log.message}
            </div>
        `).join('');

        const modal = document.createElement('div');
        modal.style.cssText = 'position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.8); z-index: 1000; padding: 40px; overflow: auto;';
        modal.innerHTML = `
            <div style="max-width: 1200px; margin: 0 auto;">
                <div class="card">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                        <h2>Cluster Deployment Logs</h2>
                        <button class="btn btn-danger" onclick="this.closest('div[style*=fixed]').remove()">Close</button>
                    </div>
                    <div class="logs-container">
                        ${logsHtml}
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    } catch (error) {
        alert(`Error loading logs: ${error.message}`);
    }
}

// Force delete a stuck cluster (DB record only)
function openCluster(clusterId) {
    // Cluster detail page — coming soon
    // For now navigate to a detail view (stub)
    window.location.href = `cluster.html?id=${clusterId}`;
}

async function forceDeleteCluster(clusterId, clusterName) {
    if (!confirm(`Force-delete "${clusterName}" from the database?\n\nThis only removes the DB record. Make sure nodes are already clean.`)) return;
    try {
        const response = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}/force`, { method: 'DELETE' });
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Force delete failed');
        }
        loadClusters();
    } catch (error) {
        alert(`Error: ${error.message}`);
    }
}

// Delete cluster
async function deleteCluster(clusterId, clusterName) {
    if (!confirm(`Are you sure you want to delete cluster "${clusterName}"?\n\nThis will uninstall all components and clean up nodes.`)) {
        return;
    }

    try {
        const response = await fetch(`${apiUrl}/api/v1/edge-clusters/${clusterId}`, {
            method: 'DELETE'
        });

        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || 'Failed to delete cluster');
        }
    } catch (error) {
        // "Failed to fetch" = connection dropped (e.g. server reload) but delete likely went through
        if (error.message === 'Failed to fetch' || error.name === 'TypeError') {
            setTimeout(loadClusters, 1500);
            return;
        }
        alert(`Error deleting cluster: ${error.message}`);
    }
    loadClusters();
}

function escHtml(str) {
    if (str == null) return '';
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Show alert
function showAlert(section, message, type) {
    const alertDiv = document.getElementById(`${section}-alert`);
    if (!alertDiv) return;

    alertDiv.innerHTML = `
        <div class="alert alert-${type}">
            ${message}
        </div>
    `;

    setTimeout(() => {
        alertDiv.innerHTML = '';
    }, 5000);
}

/* ══════════════════════════════════════════════════════════
   FOLLOW-ME dashboard — the live demo story in one view
   ══════════════════════════════════════════════════════════ */
let _fmTimer = null;

function loadFollowMe() {
    fmPoll();
    fmLoadReqs();
    fmInitVideo();
    fmStartTs();
    if (_fmTimer) clearInterval(_fmTimer);
    _fmTimer = setInterval(fmPoll, 3000);
}

function fmVideoState(s) {
    const el = document.getElementById('fm-video-state');
    if (el) el.textContent = s;
}

function _fmShowFallback(show) {
    const fb = document.getElementById('fm-video-fallback');
    if (fb) fb.style.display = show ? 'flex' : 'none';
}

function fmRenderVideoFallback(ue) {
    const t = document.getElementById('fm-fb-tput');
    if (!t) return;
    const l = document.getElementById('fm-fb-lat');
    const q = document.getElementById('fm-fb-quality');
    const sig = document.getElementById('fm-fb-signal');
    const tput = ue ? ue.predicted_throughput_mbps : null;
    const lat = ue ? ue.predicted_latency_ms : null;
    const deg = !!(ue && ue.qos_degradation_expected);
    t.textContent = (tput != null) ? tput : '—';
    if (l) l.textContent = (lat != null) ? lat : '—';
    if (q) { q.textContent = ue ? (deg ? '⚠ degrading' : '✓ good') : '—'; q.style.color = deg ? '#ff6b6b' : '#4ade80'; }
    if (sig) {
        const bars = 5;
        const level = tput != null ? Math.min(bars, Math.max(1, Math.ceil(tput / 5))) : (deg ? 1 : 3);
        sig.innerHTML = Array.from({ length: bars }, (_, i) => {
            const on = i < level, h = 6 + i * 4;
            return `<div style="width:5px;height:${h}px;border-radius:1px;background:${on ? (deg ? '#ff6b6b' : '#4ade80') : 'rgba(255,255,255,.15)'};"></div>`;
        }).join('');
    }
}

function _fmMode() { return localStorage.getItem('drift_video_mode') || 'file'; }

// Camera-style date/time drawn over the video; advances with real playback and
// FREEZES during a migration (paused + red MIGRATING tag), then resumes where it
// left off — the visible "session froze, then relocated" cue on a buffered file.
let _fmMigrating = false;
let _fmTs = { base: Date.now(), secs: 0, lastCt: 0, wasMig: false, timer: null };
let _fmVideoPos = 0;   // last playback position while the tab was visible (survive nav)

function fmStartTs() {
    if (_fmTs.timer) return;
    _fmTs.timer = setInterval(fmTsTick, 200);
}

function fmTsTick() {
    const bar = document.getElementById('fm-ts');
    if (!bar) return;
    if (_fmMode() !== 'file') { bar.style.display = 'none'; return; }
    bar.style.display = 'flex';
    const v = document.getElementById('fm-video');
    const txt = document.getElementById('fm-ts-txt'), rec = document.getElementById('fm-ts-rec'), tag = document.getElementById('fm-ts-tag');
    // Show a MIGRATING tag while a procedure is in flight, but DO NOT pause the
    // video — let it reflect reality. The clock advances with actual playback, so
    // it freezes only if the stream really stalls (the pod's CRIU checkpoint),
    // and keeps ticking through a VM live-migrate where playback isn't disrupted.
    if (tag) tag.style.display = _fmMigrating ? 'inline-block' : 'none';
    // With the server-rendered timestamp overlaid on the video, the UI-drawn
    // clock is redundant (and less honest) — show only the MIGRATING tag.
    if (_fmStampOk) {
        if (txt) txt.textContent = '';
        if (rec) rec.style.display = 'none';
        return;
    }
    if (rec) rec.style.display = '';
    if (rec) {
        rec.style.background = _fmMigrating ? '#c0392b' : '#ff3b30';
        rec.style.opacity = _fmMigrating ? '1' : ((Math.floor(Date.now() / 600) % 2) ? '1' : '0.35');
    }
    if (v && !v.paused) {
        const ct = v.currentTime, d = ct - _fmTs.lastCt;
        if (d > 0 && d < 2) _fmTs.secs += d;            // advance by real playback progress
        _fmTs.lastCt = ct;
        if (v.offsetParent !== null && ct > 0) _fmVideoPos = ct;   // remember pos only while visible
    }
    const dt = new Date(_fmTs.base + _fmTs.secs * 1000), p = n => String(n).padStart(2, '0');
    if (txt) txt.textContent = `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())} ${p(dt.getHours())}:${p(dt.getMinutes())}:${p(dt.getSeconds())}`;
}

function fmToggleMute() {
    const v = document.getElementById('fm-video');
    if (!v) return;
    v.muted = !v.muted;
    if (!v.muted) { v.volume = 1; v.play().catch(() => {}); }   // unmute needs the click gesture
    const b = document.getElementById('fm-mute');
    if (b) b.textContent = v.muted ? '🔇 Unmute' : '🔊 Mute';
}

function fmSetMode(mode) {
    localStorage.setItem('drift_video_mode', mode);
    const camBtn = document.getElementById('fm-mode-cam');
    const fileBtn = document.getElementById('fm-mode-file');
    if (camBtn) camBtn.className = 'btn btn-sm' + (mode === 'cam' ? '' : ' btn-ghost');
    if (fileBtn) fileBtn.className = 'btn btn-sm' + (mode === 'file' ? '' : ' btn-ghost');
    const input = document.getElementById('fm-video-url');
    const ub = _fmUeBase();
    if (input) input.placeholder = ub ? (mode === 'cam' ? `${ub}/8080/stream` : `${ub}/8082/loop.webm`) : 'pick an app or paste a URL';
    fmApplyStream();
}

let _fmLoadedKey = '';   // mode|url currently loaded — avoids restarting a playing clip
let _fmCamTimer = null;  // MJPEG-poll timer (generic cam endpoints)
let _fmStampTimer = null;   // server-timestamp overlay poll (file mode)
let _fmStampOk = false;     // a stamp frame has loaded — hide the fake UI clock

function _fmCamStop() { if (_fmCamTimer) { clearInterval(_fmCamTimer); _fmCamTimer = null; } }

function _fmCamPoll(base) {
    const cam = document.getElementById('fm-cam');
    if (!cam) return;
    const im = new Image();
    im.onload = () => { cam.src = im.src; fmVideoState(''); };
    im.src = base + (base.includes('?') ? '&' : '?') + 't=' + Date.now();
}

function _fmStampStop() {
    if (_fmStampTimer) { clearInterval(_fmStampTimer); _fmStampTimer = null; }
    _fmStampOk = false;
    const st = document.getElementById('fm-stamp');
    if (st) { st.style.display = 'none'; st.removeAttribute('src'); }
}

// Poll the pod's /stamp strip and overlay it on the playing video: the timestamp
// is rendered by the SERVER, so it freezes exactly while the pod is checkpointed
// (the buffered video may keep playing around it) — an honest downtime clock.
function _fmStampPoll(base) {
    const st = document.getElementById('fm-stamp');
    if (!st) return;
    const im = new Image();
    im.onload = () => { st.src = im.src; st.style.display = 'block'; _fmStampOk = true; };
    im.onerror = () => { _fmStampOk = false; };
    im.src = base + '?t=' + Date.now();
}

// Apply the saved URL in the current mode: MJPEG cam -> <img>, file -> <video>.
// Only (re)loads when the mode/URL changed or force=true (Reconnect); otherwise it
// leaves a playing clip alone (pressing Load again won't restart from the beginning).

// Is the currently loaded stream actually traversing the UE's 5G path, or is
// this browser talking straight to the edge over the lab LAN? The phone frame
// looks identical either way, so it has to be stated rather than implied.
// The active UE profile (vehicle Jetson or a Pi): the browser reaches its relays
// through the backend's per-UE proxy, so "via the UE" = through that proxy base.
let _fmActiveUe = null;
function _fmUeBase(ue) {
    ue = ue || _fmActiveUe;
    return ue ? `${apiUrl}${ue.proxy_base}` : null;
}
async function fmActiveUe(force) {
    if (_fmActiveUe && !force) return _fmActiveUe;
    try {
        const d = await (await fetch(`${apiUrl}/api/v1/ues`)).json();
        _fmActiveUe = (d.ues || []).find(u => u.active) || null;
        fmUeDatalists();
    } catch (e) {}
    return _fmActiveUe;
}
// "over the UE (5G)" entries of the video / latency pickers, for the active UE
function fmUeDatalists() {
    const ub = _fmUeBase();
    const nm = _fmActiveUe ? _ueEsc(_fmActiveUe.name) : '';
    const fill = (id, opts) => {
        const dl = document.getElementById(id);
        if (!dl) return;
        dl.querySelectorAll('option[data-ue]').forEach(o => o.remove());
        if (!ub) return;
        dl.insertAdjacentHTML('afterbegin', opts.map(([u, l]) =>
            `<option data-ue="1" value="${u}">${l} over ${nm} (5G)</option>`).join(''));
    };
    fill('fm-video-urls', [[`${ub}/8080/stream`, 'YOLO perception'],
                           [`${ub}/8082/loop.webm`, 'Video streaming (pod)'],
                           [`${ub}/8081/loop.webm`, 'Video streaming (VM)']]);
    fill('fm-lat-targets', [[`${ub}/8080/health`, 'yolo']]);
}
async function fmPathBadge() {
    const el = document.getElementById('fm-path-badge');
    if (!el) return;
    const url = (localStorage.getItem('drift_video_url') || '').trim();
    const ue = await fmActiveUe();
    if (!url) { el.textContent = ''; return; }
    let host = '';
    try { host = new URL(url).hostname; } catch (e) { host = ''; }
    const viaUe = !!ue && url.includes(ue.proxy_base);
    if (viaUe) host = `${ue.name} (${ue.host})`;
    el.textContent = viaUe ? '● over 5G · via the UE' : '▲ direct from the edge · NOT the UE path';
    el.style.background = viaUe ? 'rgba(46,158,79,.92)' : 'rgba(210,153,34,.95)';
    el.title = viaUe
        ? `streamed through the UE proxy at ${host}, so it crosses the radio`
        : `this browser is fetching ${host} over the lab network; the UE is not involved`;
    // the charts probe whatever the app link points at, so they inherit the same truth
    const note = document.getElementById('fm-lat-note');
    if (note) note.textContent = viaUe
        ? 'Sampled from this browser through the UE proxy, so it crosses the 5G path.'
        : 'Sampled from this browser straight to the edge over the lab network — this is NOT the UE path.';
}

function fmApplyStream(force) {
    fmPathBadge();
    const mode = _fmMode();
    const url = (localStorage.getItem('drift_video_url') || '').trim();
    const v = document.getElementById('fm-video');
    const cam = document.getElementById('fm-cam');
    const buf = document.getElementById('fm-video-buffer');
    if (buf) buf.style.display = 'none';
    const key = mode + '|' + url;
    const changed = !!force || _fmLoadedKey !== key;
    if (mode === 'cam') {
        _fmStampStop();
        if (v) { try { v.pause(); } catch (e) {} v.style.display = 'none'; }
        if (cam) {
            cam.style.display = 'block';
            if (!url) { _fmCamStop(); cam.removeAttribute('src'); _fmShowFallback(true); fmVideoState('set a stream URL'); }
            else if (changed) {
                _fmShowFallback(false); fmVideoState('connecting…');
                _fmCamStop();
                // /frame endpoints (yolo-app) serve one JPEG per request — poll
                // them; anything else is treated as a raw MJPEG stream.
                const m = url.match(/^(.*?)\/frame\/?(\?.*)?$/i);
                if (m) {
                    const base = m[1] + '/frame';
                    _fmCamPoll(base);
                    _fmCamTimer = setInterval(() => _fmCamPoll(base), 250);
                } else cam.src = url + (url.includes('?') ? '&' : '?') + 't=' + Date.now();
            }
        }
    } else {
        _fmCamStop();
        if (cam) { cam.style.display = 'none'; if (changed) cam.removeAttribute('src'); }
        if (v) {
            v.style.display = 'block';
            if (!url) { _fmStampStop(); _fmShowFallback(true); fmVideoState('set a stream URL'); }
            else if (changed) {
                _fmShowFallback(false); v.src = url; v.load(); v.play().catch(() => {});
                _fmStampStop();
                const base = url.replace(/\/[^/]*$/, '') + '/stamp';
                if (/^https?:\/\//i.test(base)) {
                    _fmStampPoll(base);
                    _fmStampTimer = setInterval(() => _fmStampPoll(base), 250);
                }
            }
            else {   // returning to the tab: restore position if the browser dropped it, then resume
                _fmShowFallback(false);
                if (_fmVideoPos > 1 && v.currentTime < _fmVideoPos - 3) { try { v.currentTime = _fmVideoPos; } catch (e) {} }
                if (v.paused) v.play().catch(() => {});
            }
        }
    }
    if (url) _fmLoadedKey = key;
}

function fmInitVideo() {
    const v = document.getElementById('fm-video');
    const cam = document.getElementById('fm-cam');
    if (!v && !cam) return;
    const url = localStorage.getItem('drift_video_url') || '';
    const input = document.getElementById('fm-video-url');
    if (input && url && !input.value) input.value = url;
    if (v && !v._fmWired) {   // <video> (file mode) reports its own stalls; fall back to PQoS
        v._fmWired = true;
        const buf = document.getElementById('fm-video-buffer');
        const show = () => { if (_fmMode() !== 'file') return; if (buf) buf.style.display = 'flex'; fmVideoState('buffering'); };
        v.addEventListener('waiting', show);
        v.addEventListener('stalled', show);
        v.addEventListener('playing', () => { if (buf) buf.style.display = 'none'; _fmShowFallback(false); fmVideoState('playing'); });
        v.addEventListener('canplay', () => { if (buf) buf.style.display = 'none'; });
        v.addEventListener('error', () => { if (_fmMode() === 'file') { _fmShowFallback(true); fmVideoState('no stream'); } });
        v.addEventListener('emptied', () => { if (_fmMode() === 'file') _fmShowFallback(true); });
    }
    if (cam && !cam._fmWired) {   // MJPEG <img> (cam mode): 'load' fires per frame; 'error' -> reconnect
        cam._fmWired = true;
        cam.addEventListener('load', () => { if (_fmMode() === 'cam') { _fmShowFallback(false); fmVideoState('● LIVE'); } });
        cam.addEventListener('error', () => {
            if (_fmMode() !== 'cam') return;
            _fmShowFallback(true); fmVideoState('reconnecting…');
            clearTimeout(cam._fmRetry);
            cam._fmRetry = setTimeout(() => {
                const u = (localStorage.getItem('drift_video_url') || '').trim();
                if (_fmMode() === 'cam' && u) cam.src = u + (u.includes('?') ? '&' : '?') + 't=' + Date.now();
            }, 1500);
        });
    }
    const phone = document.getElementById('fm-phone');
    if (phone && !phone._fmWired) {   // tap the phone to unmute (first user gesture)
        phone._fmWired = true;
        phone.style.cursor = 'pointer';
        phone.addEventListener('click', () => {
            const v = document.getElementById('fm-video');
            if (v && v.muted && _fmMode() === 'file') fmToggleMute();
        });
    }
    fmSetMode(_fmMode());   // sets button styles + applies the stream in the current mode
}

function fmSetVideo() {
    let url = (document.getElementById('fm-video-url').value || '').trim();
    if (!url) return;
    // Frame-serving apps (yolo-app): /ue is the phone wrapper, /frame the JPEG it
    // polls — both map to cam-mode frame polling. Auto-pick the mode so Load just
    // works whichever URL is pasted; anything ending in a clip name is file mode.
    url = url.replace(/\/(ue|stamp|clock)\/?(\?.*)?$/, '/frame');
    const isMjpeg = /\/(frame|cam|mjpe?g|stream)(\?|$)/i.test(url);
    localStorage.setItem('drift_video_url', url);
    fmLatFollow(url);          // the plots follow whichever app is now on screen
    if (_fmMode() !== (isMjpeg ? 'cam' : 'file')) fmSetMode(isMjpeg ? 'cam' : 'file');
    else fmApplyStream();
}

// Point the two charts at the app the operator just loaded, so there is ONE
// control rather than two that can disagree. The probe path differs per app
// because a cheap endpoint differs per app: yolo publishes a few bytes of JSON,
// the video pod answers every path with a 30 MB clip (so use its 1 KB stamp),
// and the VM is a static server whose root listing is 230 bytes.
// The VM serves no CORS header, so the browser refuses a direct fetch. Probe it
// through the backend, which is same-origin for this page. yolo and the video pod
// both send `Access-Control-Allow-Origin: *`, so those stay direct and keep the
// honest browser-to-app measurement.
// The VM has two addresses and BOTH need proxying: the lab relay and the UE
// proxy on the Pi. Matching only the lab one left the UE route probing directly,
// where CORS silently blocked it — the clip played and the charts stayed empty.
function fmLatIsVmLab(origin) { return /10\.220\.3\.21:9000(\/|$)/.test(origin); }
function fmLatIsVmUe(origin)  { return /:8081(\/|$)/.test(origin); }
function fmLatIsVm(origin) { return fmLatIsVmLab(origin) || fmLatIsVmUe(origin); }
function fmLatProbePathFor(origin) {
    if (/\/proxy\/8081$/.test(origin)) return '/';                   // VM over a UE profile
    if (/\/proxy\/8082$/.test(origin)) return '/stamp';              // video pod over a UE profile
    if (fmLatIsVmUe(origin)) return '/api/v1/demo/probe/vm-ue';    // keeps the 5G leg
    if (fmLatIsVmLab(origin)) return '/api/v1/demo/probe/vm';
    if (/:9101(\/|$)/.test(origin)) return '/stamp';               // video pod
    return '/health';                                              // yolo, either route
}
// Throughput needs bytes to measure; latency needs as few as possible. So each
// app gets both: a cheap path polled every tick, and a bulk path pulled (capped)
// only on a throughput sample.
function fmLatBulkPathFor(origin) {
    if (/\/proxy\/8081$/.test(origin)) return '/loop.webm';
    if (/\/proxy\/8082$/.test(origin)) return '/';
    if (fmLatIsVmUe(origin)) return '/api/v1/demo/probe/vm-ue-bulk';
    if (fmLatIsVmLab(origin)) return '/api/v1/demo/probe/vm-bulk';
    if (/:9101(\/|$)/.test(origin)) return '/';                      // video pod: the clip
    return '/frame';                                                 // yolo: ~35 KB JPEG
}
function fmLatFollow(url) {
    const box = document.getElementById('fm-lat-target');
    if (!box) return;
    const m = String(url).match(/^(https?:\/\/[^/]+)/);
    if (!m) return;
    const next = m[1] + fmLatProbePathFor(m[1]);
    if (box.value.trim() === next) return;
    box.value = next;          // visible, and still editable as an override
    fmLatDraw(); fmFrmDraw();  // fmLatBase() sees the change and resets the history
}

async function fmLoadReqs() {
    try {
        const set = (id, v) => { const el = document.getElementById(id); if (el && v != null) el.value = v; };
        const p = await (await fetch(`${apiUrl}/api/v1/demo/pqos-thresholds`)).json();
        set('fm-pq-dl', p.dl_min_mbps);
        set('fm-pq-ul', p.ul_min_mbps);
        set('fm-pq-lat', p.lat_max_ms);
    } catch (e) { /* controller not up yet */ }
}

// Arm/disarm the autonomous trigger from the page, instead of a curl. Read from
// the server every refresh rather than remembered locally, because a mec021
// restart silently re-arms it — the badge must show the truth, not the last click.
let _fmAtEnabled = null;
async function fmLoadAutoTrigger() {
    const st = document.getElementById('fm-at-state'), btn = document.getElementById('fm-at-btn');
    if (!st || !btn) return;
    let r = null;
    try {
        r = await (await fetch(`${apiUrl}/api/v1/demo/auto-trigger`)).json();
        _fmAtEnabled = !!r.enabled;
    } catch (e) { _fmAtEnabled = null; }
    if (_fmAtEnabled === null) {
        st.textContent = 'unknown'; st.style.background = 'var(--surface-2)'; st.style.color = 'var(--text-muted)';
        btn.textContent = 'Retry'; return;
    }
    const sel = document.getElementById('fm-at-mode');
    if (sel && r && r.mode && document.activeElement !== sel) sel.value = r.mode;
    st.textContent = _fmAtEnabled ? '● ARMED' : 'DISARMED';
    st.style.background = _fmAtEnabled ? 'rgba(248,81,73,.15)' : 'var(--surface-2)';
    st.style.color = _fmAtEnabled ? 'var(--danger)' : 'var(--text-muted)';
    btn.textContent = _fmAtEnabled ? 'Disarm' : 'Arm';
}
// Which pipeline drives relocation: the AF's live request path (polled), its
// subscription notifications, or both. They fail independently on the testbed.
async function fmSetTriggerMode(mode) {
    try {
        await fetch(`${apiUrl}/api/v1/demo/auto-trigger`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode }) });
    } catch (e) {}
    fmLoadAutoTrigger();
}
async function fmToggleAutoTrigger() {
    const want = !_fmAtEnabled;
    try {
        await fetch(`${apiUrl}/api/v1/demo/auto-trigger`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: want }) });
    } catch (e) {}
    fmLoadAutoTrigger();
}

async function fmApplyPqos() {
    const msg = document.getElementById('fm-pq-msg');
    const num = id => { const v = parseFloat(document.getElementById(id).value); return isNaN(v) ? null : v; };
    const body = { dl_min_mbps: num('fm-pq-dl'), ul_min_mbps: num('fm-pq-ul'), lat_max_ms: num('fm-pq-lat') };
    if (msg) msg.textContent = 'Applying…';
    try {
        const r = await (await fetch(`${apiUrl}/api/v1/demo/pqos-thresholds`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        })).json();
        // healthy predicted latency through the model is ~50-120 ms — warn below that floor
        const low = r.lat_max_ms != null && r.lat_max_ms < 120;
        const sub = r.subscription;
        const subTxt = !sub ? ''
            : sub.resubscribed ? ` AF subscription re-armed at ${sub.threshold_dl_mbps} Mbps (${sub.resubscribed} AMS).`
            : ' No AMS registered, so there is no AF subscription to re-arm.';
        if (msg) msg.textContent = 'Applied — the numeric signal uses the new thresholds on the next poll.'
            + subTxt
            + (low ? ' Note: below the model’s healthy latency floor (~50–120 ms) this reads as permanently degraded.' : '');
        setTimeout(fmPoll, 600);
    } catch (e) { if (msg) msg.textContent = 'Failed: ' + e.message; }
}

// ── UE -> app latency, live ───────────────────────────────────────────────────
// The user-perspective KPI. Everything else on this page is what the platform
// believes; this is what the phone actually waits for. One series (latency), so
// no legend box: the migration band and outages are annotated in place. No pQoS
// rule here on purpose: this series is what the browser MEASURED end to end,
// while the pQoS threshold governs a FORECAST of the 5G segment. Putting them on
// one axis reads as "the line should react when the series crosses", and it
// never will. Predictions vs thresholds live in the UE QoS card.
const FM_LAT = {
    line:  '#4493f8',   // the series
    thr:   '#c98500',   // amber: freeze annotations (validated in-band for this dark surface)
    mig:   '#9085e9',   // migration in flight
    gap:   '#f85149',   // probe got no answer
    grid:  'rgba(139,148,158,.16)',
    ink:   '#8b949e',
};
let _fmLatData = [];            // {t, ms, ok, mig}
let _fmLatBase = null;          // UE proxy base URL, learned once from the backend
let _fmLatTimer = null, _fmLatBusy = false, _fmLatHover = null;
// Which claim the left panel can honestly make about THIS app:
//   'state'   - it publishes a monotonic counter, so a restart is distinguishable
//   'service' - it does not, so we can only show that it kept answering
let _fmProbeMode = null;
let _fmProbePath = '/health';
let _fmBulkPath = '/frame';
const FM_TP_BYTES = 131072;   // 128 KB per throughput sample
const FM_TP_EVERY = 5;        // every 5th tick (~2 s) -> ~0.5 Mbps of probe traffic
let _fmTpTick = 0;
const FM_LAT_MAX = 300;         // ~2 min of history at 400 ms

// The charts must follow whichever app is being demoed. Left to itself the probe
// resolves the UE path, which always lands on yolo via the hub relay — so a
// target box lets the operator point it at the video pod or the VM instead, and
// switching wipes the history rather than splicing two apps into one line.
let _fmLatUrl = null;
async function fmLatBase() {
    const el = document.getElementById('fm-lat-target');
    const typed = el && el.value.trim();
    if (typed) {
        if (typed !== _fmLatUrl) {
            _fmLatUrl = typed; _fmLatData = []; _fmProbeMode = null; _fmLatHover = null;
            const m = typed.match(/^(https?:\/\/[^/]+(?:\/api\/v1\/ues\/[^/]+\/proxy\/\d+)?)(\/.*)?$/);
            const origin = m ? m[1] : typed;
            _fmProbePath = fmLatProbePathFor(origin);
            _fmBulkPath = fmLatBulkPathFor(origin);
            // proxied probes are served by the API, not by the app's own host
            _fmLatBase = _fmProbePath.startsWith('/api/') ? apiUrl : origin;
        }
        return _fmLatBase;
    }
    if (_fmLatUrl) {   // cleared the box -> fall back to the UE path
        _fmLatUrl = null; _fmLatBase = null; _fmLatData = []; _fmProbeMode = null;
        _fmProbePath = '/health'; _fmBulkPath = '/frame';
    }
    if (_fmLatBase) return _fmLatBase;
    try {
        const p = await (await fetch(`${apiUrl}/api/v1/demo/ue-path`)).json();
        // ue_url is .../ue on the active UE's relay (via the backend proxy when
        // it is a path); the probe wants the base of it
        if (p.ue_url) _fmLatBase = (p.ue_url.startsWith('/') ? apiUrl : '') + p.ue_url.replace(/\/ue\/?$/, '');
    } catch (e) { /* retry next tick */ }
    return _fmLatBase;
}

async function fmLatTick() {
    const run = document.getElementById('fm-lat-run');
    if (_fmLatBusy || (run && !run.checked)) return;
    const base = await fmLatBase();
    if (!base) return;
    _fmLatBusy = true;
    // /health is a few bytes, so this times the round trip rather than the
    // throughput of a 44 KB frame — the thing that actually spikes on a cutover.
    const ctl = new AbortController();
    const kill = setTimeout(() => ctl.abort(), 2500);
    const t0 = performance.now();
    let ok = true;
    let frames = null, mbps = null;
    _fmTpTick = (_fmTpTick + 1) % FM_TP_EVERY;
    // During a migration the whole point is the dip, and a 2 s cadence can step
    // clean over a 1-2 s freeze. Sample every tick while it is in flight.
    const _fmTpDue = (_fmTpTick === 0) || !!_fmMigrating;
    try {
        const path = (_fmTpDue && _fmProbeMode === 'service') ? _fmBulkPath : _fmProbePath;
        const r = await fetch(`${base}${path}?t=${Date.now()}`, { cache: 'no-store', signal: ctl.signal });
        ok = r.ok;
        const ctype = (r.headers.get('content-type') || '').toLowerCase();
        if (ctype.includes('json')) {
            const v = JSON.parse(await r.text()).frames;
            frames = (typeof v === 'number') ? v : null;
            if (frames != null) _fmProbeMode = 'state';
        } else {
            // No JSON state to read. Measure what IS universal: delivered throughput.
            // Bounded to FM_TP_BYTES and cancelled, because some apps (the video pod)
            // serve a 30 MB clip on every path and would otherwise saturate the very
            // link we are measuring. Sampled rarely so the probe stays ~0.5 Mbps.
            if (_fmProbeMode !== 'state') _fmProbeMode = 'service';
            const rd = r.body && r.body.getReader();
            if (rd) {
                if (_fmTpDue) {
                    const tp0 = performance.now();
                    let got = 0;
                    while (got < FM_TP_BYTES) {
                        const { done, value } = await rd.read();
                        if (done) break;
                        got += value.length;
                    }
                    const secs = (performance.now() - tp0) / 1000;
                    if (got > 0 && secs > 0) mbps = (got * 8 / 1e6) / secs;
                } else {
                    // not a throughput sample: do not pull a chunk at all, or a
                    // 30 MB clip costs ~64 KB every tick for nothing
                }
                try { await rd.cancel(); } catch (e) {}
            }
        }
    } catch (e) { ok = false; }
    clearTimeout(kill);
    _fmLatBusy = false;
    const waited = performance.now() - t0;
    if (!ok && _fmProbeMode === 'service') mbps = 0;
    _fmLatData.push({ t: Date.now(), ms: ok ? waited : null, waited, ok, mig: !!_fmMigrating, frames, mbps });
    if (_fmLatData.length > FM_LAT_MAX) _fmLatData.splice(0, _fmLatData.length - FM_LAT_MAX);
    fmLatDraw();
    fmFrmDraw();
}

// A failed probe has no round-trip to report, but it does have a duration: how
// long the user waited with nothing coming back. That span, not a missing dot,
// is the number the audience is asking for when the line breaks.
function fmLatOutage(i) {
    const d = _fmLatData;
    if (!d[i] || d[i].ok) return null;
    let a = i, b = i;
    while (a > 0 && !d[a - 1].ok) a--;
    while (b < d.length - 1 && !d[b + 1].ok) b++;
    // Measure to the first answer after the run so the gap is not truncated. While
    // the outage is STILL open there is no such answer yet, so measure to now and
    // let the number count up live rather than adding a whole timeout.
    const endT = (b + 1 < d.length) ? d[b + 1].t : Date.now();
    const startT = (a > 0) ? d[a - 1].t : d[a].t;
    return { a, b, ms: Math.max(endT - startT, d[i].waited || 0) };
}

function fmLatDraw() {
    const cv = document.getElementById('fm-lat-canvas');
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    if (cv.width !== w * dpr || cv.height !== h * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);

    const padL = 38, padR = 8, padT = 10, padB = 16;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    const d = _fmLatData;
    const okv = d.filter(s => s.ok).map(s => s.ms);
    // Scale to the data (p95-driven), so the spike the demo is about stays visible.
    const sortedAll = okv.slice().sort((a, b) => a - b);
    const p95 = sortedAll.length ? sortedAll[Math.floor(sortedAll.length * 0.95)] : 0;
    let hi = Math.max(120, p95 * 1.6, (okv.length ? Math.max(...okv) : 0) * 1.1);
    hi = Math.ceil(hi / 50) * 50;
    const X = i => padL + (d.length <= 1 ? plotW : (i / (FM_LAT_MAX - 1)) * plotW);
    const Y = v => padT + plotH - Math.min(1, v / hi) * plotH;

    // recessive grid + y labels
    g.strokeStyle = FM_LAT.grid; g.lineWidth = 1;
    g.fillStyle = FM_LAT.ink; g.font = '10px ui-monospace, monospace'; g.textAlign = 'right';
    for (let k = 0; k <= 3; k++) {
        const v = (hi / 3) * k, y = Math.round(Y(v)) + 0.5;
        g.beginPath(); g.moveTo(padL, y); g.lineTo(w - padR, y); g.stroke();
        g.fillText(String(Math.round(v)), padL - 6, y + 3);
    }

    // migration bands and outages, drawn under the line
    for (let i = 0; i < d.length; i++) {
        const x0 = X(i), x1 = X(i + 1);
        if (d[i].mig) { g.fillStyle = 'rgba(144,133,233,.22)'; g.fillRect(x0, padT, Math.max(1, x1 - x0), plotH); }
        if (!d[i].ok) { g.fillStyle = 'rgba(248,81,73,.30)'; g.fillRect(x0, padT, Math.max(1.5, x1 - x0), plotH); }
    }

    // the series: 2px, broken across gaps rather than bridged
    g.strokeStyle = FM_LAT.line; g.lineWidth = 2; g.lineJoin = 'round'; g.lineCap = 'round';
    g.beginPath(); let pen = false;
    d.forEach((s, i) => {
        if (!s.ok) { pen = false; return; }
        const x = X(i), y = Y(s.ms);
        if (pen) g.lineTo(x, y); else g.moveTo(x, y);
        pen = true;
    });
    g.stroke();

    // hover crosshair + readout
    if (_fmLatHover != null && d[_fmLatHover]) {
        const s = d[_fmLatHover], x = X(_fmLatHover);
        g.strokeStyle = 'rgba(205,217,229,.35)'; g.lineWidth = 1;
        g.beginPath(); g.moveTo(x + 0.5, padT); g.lineTo(x + 0.5, padT + plotH); g.stroke();
        if (s.ok) {
            g.fillStyle = FM_LAT.line; g.beginPath(); g.arc(x, Y(s.ms), 4, 0, 7); g.fill();
            g.strokeStyle = '#161b22'; g.lineWidth = 2; g.stroke();   // 2px surface ring
        }
        const out = s.ok ? null : fmLatOutage(_fmLatHover);
        const label = (s.ok ? `${Math.round(s.ms)} ms`
                            : `no response ${(out.ms / 1000).toFixed(1)} s`) +
                      (s.mig ? '  · migrating' : '');
        g.font = '11px ui-monospace, monospace';
        const tw = g.measureText(label).width + 12;
        const bx = Math.min(Math.max(padL, x - tw / 2), w - padR - tw);
        g.fillStyle = 'rgba(13,17,23,.94)'; g.strokeStyle = '#30363d'; g.lineWidth = 1;
        g.beginPath(); g.rect(bx, padT + 2, tw, 20); g.fill(); g.stroke();
        g.fillStyle = s.ok ? '#cdd9e5' : FM_LAT.gap; g.textAlign = 'left';
        g.fillText(label, bx + 6, padT + 16);
    }

    // hero number + summary
    const last = d[d.length - 1];
    const now = document.getElementById('fm-lat-now');
    if (now) {
        const unitEl = document.getElementById('fm-lat-unit');
        if (!last) { now.textContent = '—'; if (unitEl) unitEl.textContent = 'ms'; }
        else if (last.ok) { now.textContent = Math.round(last.ms); if (unitEl) unitEl.textContent = 'ms'; }
        else {
            // mid-outage the honest number is the wait so far, not a missing value
            const o = fmLatOutage(_fmLatData.length - 1);
            now.textContent = o ? (o.ms / 1000).toFixed(1) : '—';
            if (unitEl) unitEl.textContent = 's with no response';
        }
        now.style.color = !last ? 'var(--text)'
            : !last.ok ? FM_LAT.gap
            : 'var(--text)';
    }
    const st = document.getElementById('fm-lat-stat');
    if (st) {
        const sorted = okv.slice().sort((a, b) => a - b);
        const p50 = sorted.length ? sorted[Math.floor(sorted.length / 2)] : null;
        const gaps = d.filter(s => !s.ok).length;
        let worst = 0;
        for (let i = 0; i < d.length; i++) {
            if (d[i].ok) continue;
            const o = fmLatOutage(i);
            if (o && o.ms > worst) { worst = o.ms; i = o.b; }
        }
        st.textContent = sorted.length
            ? `p50 ${Math.round(p50)} · max ${Math.round(sorted[sorted.length - 1])}`
              + (gaps ? ` · outage ${(worst / 1000).toFixed(1)} s` : ' · no outage')
            : '—';
    }
}


// ── Application state continuity ──────────────────────────────────────────────
// The proof that this is a LIVE migration and not a restart. The app's own
// cumulative frame counter is monotonic: a restart would drop it to zero (a
// cliff), while a live migration keeps it climbing from exactly where it was,
// with a plateau whose WIDTH is the freeze. So the shape carries both claims —
// state survived, and here is how long the user waited.
function fmFrmStalls() {
    // contiguous spans where the counter did not advance (a failed probe counts:
    // the app was not answering, so it was not progressing either)
    const d = _fmLatData, spans = [];
    let start = -1, prev = null;
    for (let i = 0; i < d.length; i++) {
        const f = (_fmProbeMode === 'state') ? (d[i].ok ? d[i].frames : null) : (d[i].ok ? i : null);   // service: any answer counts as progress
        const stalled = (f == null) || (prev != null && f <= prev);
        if (stalled && start < 0) start = i;
        if (!stalled && start >= 0) { spans.push([start, i]); start = -1; }
        if (f != null) prev = f;
    }
    if (start >= 0) spans.push([start, d.length - 1]);
    return spans;
}

function fmFrmDraw() {
    const cv = document.getElementById('fm-frm-canvas');
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    if (cv.width !== w * dpr || cv.height !== h * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);

    const padL = 44, padR = 8, padT = 10, padB = 16;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    const d = _fmLatData;
    const stateMode = _fmProbeMode === 'state';
    // No app counter to trust in service mode, so plot the universal thing: the
    // throughput actually delivered to the UE. Samples are sparse, so hold the
    // last value between them rather than drawing a comb.
    let held = null;
    const series = d.map(s => {
        if (stateMode) return s.ok ? s.frames : null;
        if (!s.ok) { held = 0; return 0; }   // nothing arrived = zero delivered
        if (s.mbps != null) held = s.mbps;
        return held;
    });
    const vals = series.filter(v => v != null);
    const X = i => padL + (i / (FM_LAT_MAX - 1)) * plotW;
    if (!vals.length) {
        g.fillStyle = FM_LAT.ink; g.font = '11px ui-monospace, monospace'; g.textAlign = 'center';
        g.fillText('waiting for the app…', w / 2, h / 2);
        return;
    }
    // window the y-scale to the visible range, otherwise a counter in the tens of
    // thousands flattens the plateau we are trying to show
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const span = Math.max(1, hi - lo);
    const Y = v => padT + plotH - ((v - lo) / span) * plotH;

    g.strokeStyle = FM_LAT.grid; g.lineWidth = 1;
    g.fillStyle = FM_LAT.ink; g.font = '10px ui-monospace, monospace'; g.textAlign = 'right';
    for (let k = 0; k <= 3; k++) {
        const v = lo + (span / 3) * k, y = Math.round(Y(v)) + 0.5;
        g.beginPath(); g.moveTo(padL, y); g.lineTo(w - padR, y); g.stroke();
        g.fillText(String(Math.round(v)), padL - 6, y + 3);
    }

    for (let i = 0; i < d.length; i++) {
        if (!d[i].mig) continue;
        g.fillStyle = 'rgba(144,133,233,.22)';
        g.fillRect(X(i), padT, Math.max(1, X(i + 1) - X(i)), plotH);
    }

    // the freezes, and a reset if one ever happens
    const spans = fmFrmStalls();
    let longest = 0, longestSpan = null;
    spans.forEach(([a, b]) => {
        const ms = (d[b].t - d[a].t);
        if (ms > longest) { longest = ms; longestSpan = [a, b]; }
        g.fillStyle = 'rgba(201,133,0,.20)';
        g.fillRect(X(a), padT, Math.max(1.5, X(b) - X(a)), plotH);
    });
    let reset = false;
    for (let i = 1; stateMode && i < d.length; i++) {
        const a = d[i - 1].ok ? d[i - 1].frames : null, b = d[i].ok ? d[i].frames : null;
        if (a != null && b != null && b < a * 0.5) {
            reset = true;
            g.fillStyle = 'rgba(248,81,73,.45)'; g.fillRect(X(i) - 1, padT, 3, plotH);
        }
    }

    g.strokeStyle = FM_LAT.line; g.lineWidth = 2; g.lineJoin = 'round'; g.lineCap = 'round';
    g.beginPath(); let pen = false;
    d.forEach((s, i) => {
        const v = series[i];
        if (v == null) return;
        const x = X(i), y = Y(v);
        if (pen) g.lineTo(x, y); else g.moveTo(x, y);
        pen = true;
    });
    g.stroke();

    if (longestSpan && longest > 600) {
        const [a, b] = longestSpan, mid = (X(a) + X(b)) / 2;
        g.fillStyle = FM_LAT.thr; g.font = '10px ui-monospace, monospace'; g.textAlign = 'center';
        g.fillText(`${(longest / 1000).toFixed(1)} s frozen`, Math.min(Math.max(mid, padL + 32), w - padR - 32), padT + 11);
    }

    const now = document.getElementById('fm-frm-now');
    const lastV = vals[vals.length - 1];
    if (now) { now.textContent = stateMode ? lastV.toLocaleString() : lastV.toFixed(1); now.style.color = reset ? FM_LAT.gap : 'var(--text)'; }
    const ttl = document.getElementById('fm-frm-title');
    if (ttl) ttl.textContent = stateMode ? 'Application state continuity' : 'UE \u2192 app throughput';
    const unit = document.getElementById('fm-frm-unit');
    if (unit) unit.textContent = stateMode ? 'frames processed' : 'Mbps to the UE';
    const note = document.getElementById('fm-frm-note');
    if (note) note.innerHTML = stateMode
        ? 'Frames the app has inferred since it started (it runs continuously, not only when watched). Read over the UE path: it never restarts from zero, so the process moved rather than relaunched.'
        : 'This app publishes no state counter, so this is throughput delivered over the UE path: universal, but it shows delivery rather than proving the process survived.';
    const st = document.getElementById('fm-frm-stat');
    if (st) {
        st.textContent = reset ? 'counter RESET — app restarted'
            : longest > 600 ? `longest freeze ${(longest / 1000).toFixed(1)} s${stateMode ? ' · never reset' : ''}`
            : (stateMode ? 'never reset' : 'serving');
        st.style.color = reset ? FM_LAT.gap : 'var(--text-muted)';
    }
}

function fmLatStart() {
    if (_fmLatTimer) return;
    const cv = document.getElementById('fm-lat-canvas');
    if (cv && !cv._fmBound) {
        cv._fmBound = true;
        cv.addEventListener('mousemove', e => {
            const r = cv.getBoundingClientRect(), padL = 38, padR = 8;
            const plotW = r.width - padL - padR;
            const idx = Math.round(((e.clientX - r.left - padL) / plotW) * (FM_LAT_MAX - 1));
            _fmLatHover = (idx >= 0 && idx < _fmLatData.length) ? idx : null;
            fmLatDraw();
        });
        cv.addEventListener('mouseleave', () => { _fmLatHover = null; fmLatDraw(); });
        window.addEventListener('resize', () => { fmLatDraw(); fmFrmDraw(); });
    }
    _fmLatTimer = setInterval(fmLatTick, 400);
    fmLatTick();
}
function fmLatStop() { if (_fmLatTimer) { clearInterval(_fmLatTimer); _fmLatTimer = null; } }

async function fmPoll() {
    const sec = document.getElementById('section-followme');
    if (!sec || !sec.classList.contains('active')) {   // navigated away -> stop
        if (_fmTimer) { clearInterval(_fmTimer); _fmTimer = null; }
        fmLatStop();
        return;
    }
    let s;
    try { s = await (await fetch(`${apiUrl}/api/v1/demo/state`)).json(); }
    catch (e) { return; }
    // Each renderer isolated: they used to run in one straight line, so a throw in
    // an early one (KPIs, topology) silently skipped every later one — the page
    // kept polling every 3 s while the UE QoS card, IMSI and refresh dot stayed
    // frozen. Now a failure costs only its own panel, and says which one.
    const safe = (name, fn) => { try { fn(); } catch (e) { console.error(`[followme] ${name} failed:`, e); } };
    safe('latency charts', () => fmLatStart());
    safe('KPI cards', () => fmRenderKpis(s.kpis || {}));
    safe('topology', () => fmRenderTopology(s));
    safe('UE QoS card', () => fmRenderUe(s.ue_qos));
    safe('auto-trigger badge', () => fmLoadAutoTrigger());
    safe('decision card', () => fmRenderDecision(s.last_decision));
    safe('edge applications', () => fmRenderApp(s.app, s.workloads));
    safe('video fallback', () => fmRenderVideoFallback(s.ue_qos));
    safe('migrations list', () => fmRenderMigrations(s.migrations || []));
    safe('UE path', () => fmUePathPoll());   // relay health, throttled internally
    // migration in progress? -> freeze the on-video camera clock
    _fmMigrating = (s.migrations || []).some(m => !m.completed && !m.failed);
    const imsiEl = document.getElementById('fm-ue-imsi');
    if (imsiEl) imsiEl.textContent = 'IMSI ' + ((s.ue_qos && s.ue_qos.ue_imsi) || '—');
    const dot = document.getElementById('fm-refresh-dot');
    if (dot) { dot.style.opacity = '0.3'; setTimeout(() => { dot.style.opacity = '1'; }, 200); }
}

function _fmKpiCard(label, value, sub, color) {
    return `<div class="card" style="padding:16px;">
        <div style="font-size:.75em;text-transform:uppercase;letter-spacing:.04em;color:var(--text-muted);margin-bottom:6px;">${label}</div>
        <div style="font-size:1.7em;font-weight:700;font-family:monospace;color:${color || 'var(--text)'};line-height:1;">${value}</div>
        <div style="font-size:.76em;color:var(--text-muted);margin-top:6px;min-height:1em;">${sub || ''}</div>
    </div>`;
}

function fmRenderKpis(k) {
    const el = document.getElementById('fm-kpis');
    if (!el) return;
    const n = v => (v == null ? '—' : v);
    const rate = (k.completed != null && k.total) ? Math.round(100 * k.completed / k.total) : null;
    el.innerHTML =
        _fmKpiCard('Migrations', n(k.total),
            `${n(k.completed)} completed · ${n(k.failed)} failed`) +
        _fmKpiCard('Freeze / downtime',
            k.last_freeze_s != null ? `${k.last_freeze_s}<span style="font-size:.5em;"> s</span>` : '—',
            k.best_freeze_s != null ? `best ${k.best_freeze_s}s · avg ${n(k.avg_freeze_s)}s` : 'no pod migrations yet',
            '#2e9e4f') +
        _fmKpiCard('Bandwidth / migration',
            k.last_net_mib != null ? `${k.last_net_mib}<span style="font-size:.5em;"> MiB</span>` : '—',
            k.avg_net_mib != null ? `avg ${k.avg_net_mib} MiB` : '',
            '#3b7ddd') +
        _fmKpiCard('Success rate',
            rate != null ? `${rate}<span style="font-size:.5em;"> %</span>` : '—',
            k.total ? `${n(k.completed)}/${n(k.total)}` : '',
            rate != null && rate >= 80 ? '#2e9e4f' : (rate != null ? '#d69a00' : null));
}

function _fmBar(pct, color) {
    return `<div style="background:rgba(127,127,127,.2);border-radius:6px;height:8px;overflow:hidden;">
        <div style="width:${pct}%;height:100%;background:${color};"></div></div>
        <div style="font-size:.72em;color:var(--text-muted);text-align:right;">${pct}%</div>`;
}

// Which workload is the UE actually WATCHING? Derived from the stream URL:
// :8081 / a clip file -> the VM video; :8080 / frame-polling -> the pod app.
// Falls back to the backend's app probe when the URL doesn't identify one.
function _fmWatchedLocation(s) {
    const url = (localStorage.getItem('drift_video_url') || '');
    let kind = null;
    if (/:8081|\/proxy\/8081\/|loop\.(webm|mp4)|192\.170\./.test(url)) kind = 'vm';
    else if (/:8080|\/proxy\/8080\/|\/(frame|ue)(\?|$)/i.test(url)) kind = 'pod';
    const w = kind ? (s.workloads || []).find(x => x.kind === kind) : null;
    return w ? { workload: w.name, edge: w.edge, kind: w.kind } : null;
}

function fmRenderTopology(s) {
    const box = document.getElementById('fm-edges');
    if (!box) return;
    const edges = s.edges || [];
    const deg = !!(s.ue_qos && s.ue_qos.qos_degradation_expected);
    const linkColor = deg ? '#e67e22' : '#2e9e4f';   // link colour = 5G QoS state
    // which edge currently hosts the app the UE is talking to (moves on migration);
    // the WATCHED workload (from the stream URL) wins over the backend's default probe
    const watched = _fmWatchedLocation(s);
    const activeName = (watched && watched.edge) ||
        (s.app && s.app.edge) || (s.workload_location && s.workload_location.edge) || null;
    let activeIdx = edges.findIndex(e => e.name === activeName);
    if (activeIdx < 0) activeIdx = edges.findIndex(e => (e.workloads || []).length > 0);

    box.innerHTML = edges.map((e, i) => {
        const wls = e.workloads || [];
        const isActive = i === activeIdx;
        const en = e.energy || {};
        const green = en.greenness != null ? Math.round(en.greenness * 100) : null;
        const epct = en.energy_pct != null ? Math.round(en.energy_pct * 100) : null;
        const wlBadge = w => `<div style="display:flex;align-items:center;gap:6px;font-size:.8em;background:rgba(46,158,79,.12);border:1px solid rgba(46,158,79,.35);border-radius:6px;padding:3px 7px;">
            <span style="font-size:.66em;text-transform:uppercase;font-weight:700;color:#2e9e4f;">${_mecEsc(w.kind)}</span>
            <span style="font-family:monospace;">${_mecEsc(w.name || '')}</span></div>`;
        return `<div id="fm-edge-${i}" style="flex:0 1 240px;min-width:200px;border:2px solid ${isActive ? linkColor : 'var(--border)'};border-radius:12px;padding:12px 14px;background:var(--surface);${isActive ? 'box-shadow:0 0 0 3px ' + linkColor + '22;' : ''}">
            <div style="display:flex;align-items:center;gap:6px;">
                <span style="font-size:1.05em;">🖥️</span>
                <div style="font-weight:600;font-family:monospace;">${_mecEsc(e.name)}</div>
                ${e.arch ? `<span style="font-size:.55em;font-family:monospace;color:var(--text-muted);border:1px solid var(--border);border-radius:6px;padding:0 5px;">${_mecEsc(e.arch)}</span>` : ''}
                ${isActive ? `<span style="font-size:.55em;font-weight:700;color:#fff;background:${linkColor};padding:1px 6px;border-radius:8px;margin-left:auto;">● SERVING</span>` : ''}
            </div>
            <div style="color:var(--text-muted);font-size:.74em;margin:4px 0 8px;">${_mecEsc(e.status || '')}</div>
            <div style="display:flex;flex-direction:column;gap:4px;margin-bottom:8px;min-height:22px;">
                ${wls.length ? wls.map(wlBadge).join('') : '<div style="font-size:.72em;color:var(--text-muted);">no workloads</div>'}
            </div>
            ${green != null ? `<div style="font-size:.72em;color:var(--text-muted);">🌿 greenness</div>${_fmBar(green, '#2e9e4f')}` : '<div style="font-size:.72em;color:var(--text-muted);">energy: n/a</div>'}
            ${epct != null ? `<div style="font-size:.72em;color:var(--text-muted);margin-top:6px;">⚡ energy</div>${_fmBar(epct, '#3b7ddd')}` : ''}
        </div>`;
    }).join('');
    requestAnimationFrame(() => _fmDrawStageLink(activeIdx, edges.length, linkColor));
}

// Draw arrows from the UE (phone) up to the edges: bold animated arrow to the
// serving edge, faint dashed connectors to the others. Re-points after migration.
function _fmDrawStageLink(activeIdx, nEdges, color) {
    const stage = document.getElementById('fm-stage');
    const svg = document.getElementById('fm-stage-svg');
    const phone = document.getElementById('fm-phone');
    if (!stage || !svg || !phone) return;
    const c = stage.getBoundingClientRect(), p = phone.getBoundingClientRect();
    const px = p.left - c.left + p.width / 2, py = p.top - c.top;   // phone top-center
    let defs = `<defs>
        <marker id="fm-ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${color}"/></marker>
        <marker id="fm-ah-dim" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--border)"/></marker>
    </defs>`;
    let paths = '';
    for (let i = 0; i < nEdges; i++) {
        const b0 = document.getElementById('fm-edge-' + i);
        if (!b0) continue;
        const b = b0.getBoundingClientRect();
        const bx = b.left - c.left + b.width / 2, by = b.top - c.top + b.height;   // edge bottom-center
        const my = (py + by) / 2;
        const d = `M ${px} ${py} C ${px} ${my}, ${bx} ${my}, ${bx} ${by + 4}`;
        if (i === activeIdx) {
            paths += `<path d="${d}" fill="none" stroke="${color}" stroke-width="3.5" stroke-linecap="round" stroke-dasharray="10 7" marker-end="url(#fm-ah)"><animate attributeName="stroke-dashoffset" from="34" to="0" dur="0.7s" repeatCount="indefinite"/></path>`;
        } else {
            paths += `<path d="${d}" fill="none" stroke="var(--border)" stroke-width="1.5" stroke-dasharray="3 5" opacity="0.6" marker-end="url(#fm-ah-dim)"/>`;
        }
    }
    svg.innerHTML = defs + paths;
}

const _fmPqLabels = {
    downlink_capacity: 'Downlink capacity',
    uplink_capacity: 'Uplink capacity',
    latency: 'Predicted latency',
};

function fmRenderUe(ue) {
    const el = document.getElementById('fm-ue');
    if (!el) return;
    const pm = (ue && ue.pqos_metrics) || {};
    const has = ue && (ue.predicted_throughput_mbps != null || ue.predicted_latency_ms != null || ue.qos_degradation_expected || Object.keys(pm).length);
    if (!has) {
        el.innerHTML = `<div style="color:var(--text-muted);font-size:.85em;line-height:1.5;">No live UE QoS yet — the UE isn't passing 5G traffic, or the controller can't reach PQoS. Selection falls back to energy/distance.</div>`;
        return;
    }
    const deg = ue.qos_degradation_expected;

    // headline verdict
    let html = `<div style="display:flex;justify-content:space-between;padding:2px 0 8px;font-size:.95em;align-items:center;border-bottom:1px solid var(--border);margin-bottom:8px;">
        <span style="color:var(--text-muted);">Network quality</span>
        <span style="font-weight:700;color:${deg ? '#c0392b' : '#2e9e4f'};">${deg ? '⚠ degrading' : '✓ meets requirements'}</span></div>`;

    // WHY — per-metric numeric verdicts against the pQoS thresholds
    const keys = Object.keys(pm);
    if (keys.length) {
        html += `<div style="font-size:.72em;text-transform:uppercase;letter-spacing:.04em;color:var(--text-muted);margin-bottom:4px;">Predictions vs thresholds (PQoS)</div>`;
        html += keys.map(k => {
            const m = pm[k];
            const cmp = m.kind === 'min' ? '≥' : '≤';
            return `<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:.86em;">
                <span style="color:var(--text-muted);">${_mecEsc(_fmPqLabels[k] || k)}</span>
                <span style="font-family:monospace;">${m.value} ${_mecEsc(m.unit)} <span style="opacity:.6;">(${cmp} ${m.threshold})</span></span>
                <span style="font-weight:600;color:${m.met ? '#2e9e4f' : '#c0392b'};">${m.met ? '✓ met' : '⚠ unmet'}</span></div>`;
        }).join('');
    }
    // A stable network moves these only in the second decimal, which reads as a
    // frozen panel. The stamp shows the reading is live; carried-forward values
    // (the AF skipped that metric this poll) are named rather than passed off.
    const cf = (ue.carried_forward || []).map(k => ({ predicted_throughput_mbps: 'DL', predicted_ul_mbps: 'UL', predicted_latency_ms: 'latency' }[k] || k));
    html += `<div style="font-size:.7em;color:var(--text-muted);margin-top:6px;font-family:var(--mono);">updated ${new Date().toLocaleTimeString()}${cf.length ? ` &middot; ${cf.join(', ')} reused from the previous poll` : ''}</div>`;

    el.innerHTML = html;
}

function fmRenderDecision(d) {
    const el = document.getElementById('fm-decision');
    if (!el) return;
    if (!d) { el.innerHTML = `<div style="color:var(--text-muted);font-size:.85em;">No decision yet — trigger a migration from the MEC API page.</div>`; return; }
    const colors = { cross_cluster: '#3b7ddd', intra_cluster: '#3b7ddd', stay: '#8a8f98', no_candidates: '#c0392b' };
    const c = colors[d.decision] || '#8a8f98';
    el.innerHTML = `
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
            <span style="width:9px;height:9px;border-radius:50%;background:${c};"></span>
            <strong style="font-family:monospace;">${_mecEsc(d.decision || '—')}</strong>
            <span style="color:var(--text-muted);font-size:.82em;">${_mecEsc(d.workload || '')} ${d.state ? '· ' + _mecEsc(d.state) : ''}</span>
        </div>
        ${d.reason ? `<div style="font-size:.85em;color:var(--text-muted);line-height:1.5;">${_mecEsc(d.reason)}</div>` : ''}
        ${d.migration_latency_ms != null ? `<div style="font-size:.8em;color:var(--text-muted);margin-top:6px;">migration time: ${d.migration_latency_ms} ms</div>` : ''}`;
}

// ── UE data path: status of the two socat relays + one-click repair ──────────
let _fmUePathLast = 0;

function _fmUeDot(up) {
    return `<span style="display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:${up ? '#2ecc71' : '#e74c3c'};"></span>`;
}

function fmUePathRender(p, ues) {
    const el = document.getElementById('fm-uepath');
    if (!el || !p) return;
    const row = (label, x) => `<div style="margin:3px 0;">${_fmUeDot(x.up)}${label} <span style="color:var(--text-2);font-family:var(--mono);font-size:.85em;">${x.host}:${x.port}</span></div>`;
    let html =
        row('Application (floating IP)', p.app) +
        row('Hub relay (on-link)', p.hub_relay);
    for (const u of (ues || [])) {
        const s = u._status;
        const dots = s
            ? `${_fmUeDot(s.wwan.up)}5G ${s.wwan.up ? '<span style="font-family:var(--mono);font-size:.85em;">' + _ueEsc(s.wwan.ip) + '</span>' : 'down'}
               &nbsp;${_fmUeDot(s.pod_proxy.up)}pod :${s.pod_proxy.port}
               &nbsp;${_fmUeDot(s.vm_proxy.up)}VM :${s.vm_proxy.port}`
            : '<span style="color:var(--text-muted);">checking…</span>';
        const tag = u.active
            ? '<span style="font-size:.7em;font-weight:700;color:#fff;background:#2e9e4f;border-radius:4px;padding:1px 6px;">ACTIVE</span>'
            : (u.retired ? '<span style="font-size:.7em;color:var(--text-muted);border:1px solid var(--border);border-radius:4px;padding:0 5px;">retired</span>' : '');
        const watch = `${_fmUeBase(u)}/${s ? s.pod_proxy.port : 8080}/ue`;
        html += `
        <div style="margin:8px 0 3px;padding-top:6px;border-top:1px solid var(--border,rgba(128,128,128,.25));${u.active ? '' : 'opacity:.7;'}">
            <div style="display:flex;align-items:center;gap:8px;">
                <strong>${_ueEsc(u.name)}</strong> ${tag}
                <span style="color:var(--text-muted);font-size:.78em;font-family:var(--mono);">${_ueEsc(u.host)}${u.imsi ? ' · IMSI ' + _ueEsc(u.imsi) : ''}</span>
                <span style="margin-left:auto;display:flex;gap:4px;">
                    ${u.active ? '' : `<button class="btn btn-sm btn-ghost" onclick="fmUeActivate(this,'${u.id}')" title="pQoS, QoD and the UE links follow this UE">Use this UE</button>`}
                    <button class="btn btn-sm btn-ghost" onclick="fmUePathRepair(this,'${u.id}')">⟳ Repair</button>
                </span>
            </div>
            <div style="margin-top:3px;">${dots}</div>
            ${s && s.pod_proxy.up ? `<div style="font-size:.78em;color:var(--text-2);margin-top:2px;">watch: <a href="${watch}" target="_blank" style="font-family:var(--mono);">${_ueEsc(u.name)} :${s.pod_proxy.port}/ue</a></div>` : ''}
        </div>`;
    }
    el.innerHTML = html;
}

async function fmUePathPoll(force) {
    if (!force && Date.now() - _fmUePathLast < 15000) return;   // every ~15 s
    _fmUePathLast = Date.now();
    try {
        const [p, ud] = await Promise.all([
            (await fetch(`${apiUrl}/api/v1/demo/ue-path`)).json(),
            (await fetch(`${apiUrl}/api/v1/ues`)).json(),
        ]);
        const ues = (ud.ues || []).sort((a, b) => (b.active - a.active) || (a.retired - b.retired));
        _fmActiveUe = ues.find(u => u.active) || null;
        fmUeDatalists();
        fmUePathRender(p, ues);                      // fast paint, statuses pending
        await Promise.all(ues.map(async u => {       // per-UE live status in parallel
            try { u._status = await (await fetch(`${apiUrl}/api/v1/ues/${u.id}/status`)).json(); }
            catch (e) { u._status = null; }
        }));
        fmUePathRender(p, ues);
    } catch (e) {}
}

// Make a UE profile the active one: the backend repoints the controller's
// pQoS/QoD at it, and every "over the UE" link here follows.
async function fmUeActivate(btn, ueId) {
    if (btn) { btn.disabled = true; btn.textContent = 'switching…'; }
    try {
        await fetch(`${apiUrl}/api/v1/ues/${ueId}/activate`, { method: 'POST' });
        _fmLatBase = null; _fmLatUrl = null;
        await fmActiveUe(true);
        fmPathBadge();
        await fmUePathPoll(true);
    } finally { if (btn) btn.disabled = false; }
}

async function fmUePathRepair(btn, ueId) {
    if (btn) { btn.disabled = true; btn.textContent = '⟳ repairing…'; }
    try {
        const url = ueId ? `${apiUrl}/api/v1/ues/${ueId}/apply-path`
                         : `${apiUrl}/api/v1/demo/ue-path/apply`;
        await fetch(url, { method: 'POST' });
        await fmUePathPoll(true);
    } catch (e) {
        const el = document.getElementById('fm-uepath');
        if (el) el.innerHTML = '<span style="color:#e74c3c;">repair failed: ' + e.message + '</span>';
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = ueId ? '⟳ Repair' : '⟳ Repair relays'; }
        _fmUePathLast = 0;
    }
}

function fmRenderApp(app, workloads) {
    const el = document.getElementById('fm-app');
    if (!el) return;
    const ws = workloads || [];
    if (!ws.length) { el.innerHTML = `<div style="color:var(--text-muted);font-size:.85em;">no edge applications found</div>`; return; }
    // One row per application. The endpoint shown is the follow-me address (the
    // thing that survives a migration); the link is the on-link relay, because a
    // browser off the edge segment cannot open a floating IP directly.
    const kindTag = k => k === 'vm'
        ? `<span style="font-size:.62em;font-weight:700;color:#9085e9;border:1px solid #9085e9;border-radius:4px;padding:0 4px;">VM</span>`
        : `<span style="font-size:.62em;font-weight:700;color:#4493f8;border:1px solid #4493f8;border-radius:4px;padding:0 4px;">POD</span>`;
    el.innerHTML = ws.map(w => {
        const up = w.reachable !== false;
        return `<div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border);">
            <span style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:${up ? '#2e9e4f' : '#c0392b'};"></span>
            ${kindTag(w.kind)}
            <span style="font-weight:600;font-size:.86em;white-space:nowrap;">${_mecEsc(w.label || w.name)}</span>
            <span style="font-size:.74em;color:var(--text-muted);font-family:var(--mono);white-space:nowrap;">${_mecEsc(w.name)}</span>
            <span style="margin-left:auto;display:flex;align-items:center;gap:8px;white-space:nowrap;">
                <span style="font-size:.74em;color:var(--text-muted);">on</span>
                <strong style="font-size:.78em;color:#2e9e4f;">${_mecEsc(w.edge || '—')}</strong>
            </span>
        </div>
        <div style="padding:2px 0 8px 18px;font-size:.72em;color:var(--text-muted);line-height:1.7;">
            ${w.url_ue
                ? `<div><a href="${_mecEsc(w.url_ue)}" target="_blank" rel="noopener" style="color:${w.reachable_ue === false ? '#c0392b' : '#3fb950'};text-decoration:none;font-family:var(--mono);font-weight:600;">${_mecEsc(w.url_ue)}</a> <span style="opacity:.8;">&larr; <strong>use this</strong>: over 5G, through the UE</span></div>`
                : `<div style="opacity:.7;">no UE entry point &mdash; cannot be shown over 5G</div>`}
            ${w.url ? `<div><a href="${_mecEsc(w.url)}" target="_blank" rel="noopener" style="color:#8b949e;text-decoration:none;font-family:var(--mono);">${_mecEsc(w.url)}</a> <span style="opacity:.7;">&larr; lab only, skips the radio</span></div>` : ''}
            <div><span style="font-family:var(--mono);opacity:.8;">${_mecEsc(w.endpoint || '—')}</span>
                 <span style="opacity:.75;">follow-me address, edge segment only</span></div>
        </div>`;
    }).join('') + `<div style="font-size:.7em;color:var(--text-muted);margin-top:8px;line-height:1.5;">
        The green address enters at the UE, so the traffic crosses the radio &mdash; that is the one to open
        when showing the user&rsquo;s experience. The grey one reaches the same workload from the lab network
        and skips 5G entirely, which is useful for checking an app is alive and misleading for anything else.
        The endpoint beneath each is the workload&rsquo;s follow-me address: it survives migration but is only
        reachable from the edge segment.</div>`;
}


function fmRenderMigrations(migs) {
    const el = document.getElementById('fm-migrations');
    if (!el) return;
    if (!migs.length) { el.innerHTML = `<div class="empty-hint" style="color:var(--text-muted);font-size:.85em;padding:10px 2px;">No migrations yet.</div>`; return; }
    const dot = m => m.completed ? '#2e9e4f' : (m.failed ? '#c0392b' : '#d69a00');
    const lbl = m => m.completed ? 'completed' : (m.failed ? 'failed' : (m.phase || 'working…'));
    el.innerHTML = migs.map(m => `
        <div class="site-group" style="display:flex;align-items:center;gap:10px;padding:10px 14px;">
            <span style="width:8px;height:8px;border-radius:50%;background:${dot(m)};flex-shrink:0;"></span>
            <span style="font-size:.68em;text-transform:uppercase;color:var(--text-muted);width:26px;">${_mecEsc(m.kind)}</span>
            <span style="font-family:monospace;font-weight:600;">${_mecEsc(m.workload || '')}</span>
            <span style="color:var(--text-muted);font-size:.85em;">${_mecEsc(m.src || '?')} → ${_mecEsc(m.dst || '?')}</span>
            <span style="margin-left:auto;display:flex;gap:12px;align-items:center;font-size:.8em;color:var(--text-muted);">
                ${m.freeze_s != null ? `<span title="freeze / downtime">❄ ${m.freeze_s}s</span>` : ''}
                ${m.net_total_mib != null ? `<span title="bytes moved">📦 ${m.net_total_mib} MiB</span>` : ''}
                <span style="color:${dot(m)};font-weight:600;">${lbl(m)}</span>
            </span>
        </div>`).join('');
}

/* ══════════════════════════════════════════════════════════
   MEC API — ETSI MEC-021 follow-me trigger (proxied via backend)
   ══════════════════════════════════════════════════════════ */
let _mecPollTimer = null;

function _mecEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

async function loadMec() {
    if (_mecPollTimer) { clearInterval(_mecPollTimer); _mecPollTimer = null; }
    document.getElementById('mec-error').textContent = '';
    const svc = document.getElementById('mec-svc-url');
    if (svc) svc.textContent = `http://${window.location.hostname}:8010`;
    const srcSel = document.getElementById('mec-source');
    const tgtSel = document.getElementById('mec-target');
    try {
        const data = await (await fetch(`${apiUrl}/api/v1/edge-clusters`)).json();
        const clusters = (data.clusters || []).filter(c => c.status === 'active');
        if (!clusters.length) {
            srcSel.innerHTML = '<option value="">No active clusters</option>';
            tgtSel.innerHTML = '<option value="">Auto (Optimal Edge Selection)</option>';
        } else {
            const arch = c => ((c.meta_data || {}).arch || 'amd64');
            srcSel.innerHTML = clusters.map(c =>
                `<option value="${_mecEsc(c.id)}" data-arch="${_mecEsc(arch(c))}">${_mecEsc(c.name)} — ${_mecEsc(c.pod_cidr)} · ${_mecEsc(arch(c))}</option>`).join('');
            tgtSel.innerHTML = '<option value="">Auto (Optimal Edge Selection)</option>' +
                clusters.map(c => `<option value="${_mecEsc(c.id)}" data-arch="${_mecEsc(arch(c))}">${_mecEsc(c.name)} · ${_mecEsc(arch(c))}</option>`).join('');
            // A running workload cannot move between CPU architectures: disable
            // the targets that differ from the source, and say why.
            const syncArch = () => {
                const a = (srcSel.selectedOptions[0] || {}).dataset?.arch;
                let note = document.getElementById('mec-arch-note');
                if (!note) { note = document.createElement('div'); note.id = 'mec-arch-note'; note.style.cssText = 'font-size:.74em;color:#d29922;margin-top:4px;'; tgtSel.after(note); }
                let off = 0;
                for (const o of tgtSel.options) {
                    const bad = !!o.value && !!a && o.dataset.arch !== a;
                    o.disabled = bad; if (bad) off++;
                    o.textContent = o.textContent.replace(/ — incompatible.*$/, '') + (bad ? ` — incompatible (${o.dataset.arch})` : '');
                }
                if (tgtSel.selectedOptions[0] && tgtSel.selectedOptions[0].disabled) tgtSel.value = '';
                note.textContent = off ? `⚠ ${off} target(s) disabled: a running ${a} workload cannot migrate to a different CPU architecture.` : '';
            };
            srcSel.onchange = syncArch; syncArch();
        }
    } catch (e) {
        srcSel.innerHTML = '<option value="">Could not load clusters</option>';
    }
    mecRenderCurl();
}

function mecPreset(kind) {
    document.getElementById('mec-kind').value = kind;
    if (kind === 'pod') {
        document.getElementById('mec-workload').value = 'my-app';
        document.getElementById('mec-appid').value = 'my-app';
    } else {
        document.getElementById('mec-workload').value = 'demo-vm';
        document.getElementById('mec-appid').value = 'demo-vm';
    }
    document.getElementById('mec-namespace').value = 'default';
    mecRenderCurl();
}

function _mecFormBody() {
    return {
        source_cluster: document.getElementById('mec-source').value.trim(),
        workload_name: document.getElementById('mec-workload').value.trim(),
        resource_kind: document.getElementById('mec-kind').value,
        namespace: (document.getElementById('mec-namespace').value.trim() || 'default'),
        app_id: document.getElementById('mec-appid').value.trim(),
        target_cluster: document.getElementById('mec-target').value.trim(),
        ue_imsi: (document.getElementById('mec-ue-imsi') || {value:''}).value.trim(),
    };
}

function mecRenderCurl() {
    const pre = document.getElementById('mec-curl');
    if (!pre) return;
    const b = _mecFormBody();
    const hub = `http://${window.location.hostname}:8010`;
    const appId = b.app_id || b.workload_name || '<workload>';
    const wl = b.workload_name || '<workload>';
    const src = b.source_cluster || '<source-cluster>';
    const amsBody = {
        registered_app_context: {
            associated_app_id: appId, source_cluster: src,
            source_namespace: b.namespace, vm_name: wl, resource_kind: b.resource_kind,
            ...(b.ue_imsi ? { ue_imsi: b.ue_imsi } : {}),
        }, service_continuity_mode: 1,
    };
    const procBody = { target_cluster: b.target_cluster || null };
    pre.textContent =
`# 1) Register the Application Mobility Service (AMS)
curl -sX POST ${hub}/amsi/v1/app_mobility_services \\
  -H 'Content-Type: application/json' \\
  -d '${JSON.stringify(amsBody)}'
#   -> { "app_mobility_service_id": "<AMS_ID>", ... }

# 2) Initiate the mobility procedure (blank target = auto edge-select)
curl -sX POST ${hub}/amsi/v1/app_mobility_services/<AMS_ID>/mobility_procedures \\
  -H 'Content-Type: application/json' \\
  -d '${JSON.stringify(procBody)}'
#   -> { "mobility_procedure_id": "<PROC_ID>", "state": "INITIALIZING", ... }

# 3) Poll status until COMPLETED / FAILED
curl -s ${hub}/amsi/v1/app_mobility_services/<AMS_ID>/mobility_procedures/<PROC_ID>`;
}


// ── AMS registrations ────────────────────────────────────────────────────────
// Separated from triggering because MEC-021 separates them: a registration is a
// long-lived declaration that also arms the pQoS subscription, while a procedure
// is an event fired against it. Folding the two together minted a new AMS on
// every trigger, and each surplus registration multiplies one pQoS event into
// another mobility procedure.
let _mecSelectedAms = null;
let _mecAmsCtx = [];   // contexts of what is registered, to mark apps as done

// Discover what is actually deployed rather than making the operator retype it
// into the step-2 form. The same call the Follow-Me page uses already knows every
// workload, its kind and which edge it sits on; the edges list maps that name to
// the cluster id a registration needs.
let _mecApps = [];
// What each app needs, typed on its own row in step 1. Kept outside the DOM
// because the list re-renders on refresh and would otherwise wipe a half-typed
// value. Blank = platform default.
const _mecReq = {};
function mecSetReq(name, key, val) { (_mecReq[name] = _mecReq[name] || {})[key] = val; }
async function mecLoadApps() {
    const el = document.getElementById('mec-apps-list');
    if (!el) return;
    let st;
    try { st = await (await fetch(`${apiUrl}/api/v1/demo/state`)).json(); }
    catch (e) { el.innerHTML = `<span style="color:var(--danger);">could not list edge applications</span>`; return; }
    const byName = {};
    (st.edges || []).forEach(e => { byName[e.name] = e.id; });
    _mecApps = (st.workloads || []).map(w => ({ ...w, cluster_id: byName[w.edge] || '' }));
    if (!_mecApps.length) { el.innerHTML = `<div style="color:var(--text-muted);">none found</div>`; return; }
    const registered = new Set(_mecAmsCtx.map(c => `${c.vm_name}@${c.source_cluster}`));
    el.innerHTML = _mecApps.map((w, i) => {
        const done = registered.has(`${w.name}@${w.cluster_id}`);
        return `<div style="display:flex;align-items:center;flex-wrap:wrap;gap:7px 9px;padding:6px 0;border-bottom:1px solid var(--border);">
            <span style="font-size:.7em;font-weight:700;color:${w.kind === 'vm' ? '#9085e9' : '#4493f8'};border:1px solid ${w.kind === 'vm' ? '#9085e9' : '#4493f8'};border-radius:4px;padding:0 4px;">${w.kind === 'vm' ? 'VM' : 'POD'}</span>
            <span style="font-weight:600;">${_mecEsc(w.label || w.name)}</span>
            <span style="font-size:.74em;color:var(--text-muted);font-family:var(--mono);">${_mecEsc(w.name)}</span>
            <span style="margin-left:auto;font-size:.74em;color:var(--text-muted);">${_mecEsc(w.edge)}</span>
            ${done
                ? `<span style="font-size:.74em;color:#2e9e4f;font-weight:600;">registered</span>`
                : `<input type="number" min="1" step="1" placeholder="max ms" title="latency this app needs, ms (blank = platform default)"
                          value="${_mecEsc((_mecReq[w.name] || {}).lat || '')}" oninput="mecSetReq('${_mecEsc(w.name)}','lat',this.value)"
                          style="width:72px;padding:3px 6px;font-size:.78em;">
                   <input type="number" min="0" step="0.1" placeholder="min Mbps" title="downlink this app needs, Mbps (blank = platform default)"
                          value="${_mecEsc((_mecReq[w.name] || {}).dl || '')}" oninput="mecSetReq('${_mecEsc(w.name)}','dl',this.value)"
                          style="width:72px;padding:3px 6px;font-size:.78em;">
                   <button class="btn btn-sm" onclick="mecRegisterApp(${i})">Register</button>`}
        </div>`;
    }).join('');
}

async function mecRegisterApp(i) {
    const w = _mecApps[i];
    const msg = document.getElementById('mec-ams-msg');
    if (!w || !w.cluster_id) { if (msg) msg.textContent = 'could not resolve that app’s cluster id'; return; }
    if (msg) msg.textContent = `registering ${w.name}…`;
    const imsi = (document.getElementById('mec-ue-imsi') || {}).value || '';
    try {
        const r = await fetch(`${apiUrl}/api/v1/mec/ams`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source_cluster: w.cluster_id, workload_name: w.name,
                                   resource_kind: w.kind, namespace: 'default',
                                   app_id: w.name, ue_imsi: imsi.trim(),
                                   min_dl_mbps: (_mecReq[w.name] || {}).dl || '',
                                   max_latency_ms: (_mecReq[w.name] || {}).lat || '' }),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
        _mecSelectedAms = body.app_mobility_service_id;
        const rq = _mecReq[w.name] || {};
        const need = [rq.lat ? `< ${rq.lat} ms` : '', rq.dl ? `> ${rq.dl} Mbps` : ''].filter(Boolean).join(', ') || 'platform default';
        if (msg) msg.textContent = `registered ${w.name} (${String(_mecSelectedAms).slice(0, 8)}), needs ${need} — its pQoS subscriptions are armed at that`;
        mecLoadAms();
    } catch (e) { if (msg) msg.textContent = 'failed: ' + e.message; }
}



async function mecLoadAms() {
    const el = document.getElementById('mec-ams-list');
    if (!el) return;
    let list = [];
    try { list = await (await fetch(`${apiUrl}/api/v1/mec/ams`)).json(); }
    catch (e) { el.innerHTML = `<span style="color:var(--danger);">could not reach the MEC API</span>`; return; }
    _mecAmsCtx = (Array.isArray(list) ? list : []).map(a => a.registered_app_context || {});
    if (!Array.isArray(list) || !list.length) {
        _mecSelectedAms = null;
        mecLoadApps();
        el.innerHTML = `<div style="color:var(--text-muted);">No registrations. Nothing is subscribed to pQoS, so nothing can fire on its own.</div>`;
        return;
    }
    if (_mecSelectedAms && !list.some(a => a.app_mobility_service_id === _mecSelectedAms)) _mecSelectedAms = null;
    el.innerHTML = list.map(a => {
        const c = a.registered_app_context || {};
        const id = a.app_mobility_service_id;
        const sel = id === _mecSelectedAms;
        return `<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border);">
            <input type="radio" name="mec-ams-pick" style="width:auto;margin:0;" ${sel ? 'checked' : ''}
                   onchange="mecPickAms('${_mecEsc(id)}')">
            <span style="font-weight:600;">${_mecEsc(c.associated_app_id || c.vm_name || '—')}</span>
            <span style="font-size:.74em;color:var(--text-muted);border:1px solid var(--border);border-radius:4px;padding:0 5px;">${_mecEsc(c.resource_kind || '?')}</span>
            <span style="font-size:.76em;color:var(--text-muted);font-family:var(--mono);" title="${c.ue_imsi ? 'pinned to this IMSI' : 'follows the service UE_IMSI, which is the safe default'}">UE ${_mecEsc(c.ue_imsi || 'platform default')}</span>
            <span style="font-size:.74em;color:var(--text-muted);font-family:var(--mono);" title="what this app declared it needs; blank fields use the platform default">${c.min_dl_mbps != null ? '&ge;' + _mecEsc(c.min_dl_mbps) + ' Mbps' : ''}${c.min_dl_mbps != null && c.max_latency_ms != null ? ' &middot; ' : ''}${c.max_latency_ms != null ? '&le;' + _mecEsc(c.max_latency_ms) + ' ms' : ''}</span>
            ${c.autonomous_trigger === false ? '<span style="font-size:.72em;color:#9085e9;border:1px solid #9085e9;border-radius:4px;padding:0 5px;" title="this app initiates its own procedures (the vehicle<->edge autopilot); pQoS does not trigger it">app-initiated</span>' : ''}
            <span style="font-size:.72em;color:var(--text-muted);font-family:var(--mono);margin-left:auto;">${_mecEsc(id.slice(0, 8))}</span>
            <button class="btn btn-sm btn-ghost" onclick="mecDeregisterAms('${_mecEsc(id)}')">Deregister</button>
        </div>`;
    }).join('');
    mecLoadApps();
}

function mecPickAms(id) {
    _mecSelectedAms = id;
    const msg = document.getElementById('mec-ams-msg');
    if (msg) msg.textContent = `step 2 will reuse ${id.slice(0, 8)} instead of registering a new one`;
}

async function mecDeregisterAms(id) {
    const msg = document.getElementById('mec-ams-msg');
    try {
        const r = await fetch(`${apiUrl}/api/v1/mec/ams/${encodeURIComponent(id)}`, { method: 'DELETE' });
        if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
        if (_mecSelectedAms === id) _mecSelectedAms = null;
        if (msg) msg.textContent = `deregistered ${id.slice(0, 8)} — its pQoS subscription is gone too`;
        mecLoadAms();
    } catch (e) { if (msg) msg.textContent = 'deregister failed: ' + e.message; }
}

async function mecTrigger() {
    const err = document.getElementById('mec-error');
    err.textContent = '';
    const b = _mecFormBody();
    if (!b.source_cluster) { err.textContent = 'Pick a source cluster.'; return; }
    if (!b.workload_name) { err.textContent = 'Enter the workload name.'; return; }
    const btn = document.getElementById('mec-submit-btn');
    btn.disabled = true; btn.textContent = 'Triggering…';
    const res = document.getElementById('mec-result');
    res.innerHTML = '<div class="loading"><div class="spinner"></div> Registering AMS + initiating procedure…</div>';
    try {
        const resp = await fetch(`${apiUrl}/api/v1/mec/trigger`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_mecSelectedAms ? { ...b, ams_id: _mecSelectedAms } : b),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
        mecRenderResult({ state: 'INITIALIZING' }, b, body);
        // Now that the server has minted them, fill the real ids into the
        // equivalent-curl doc so the status call (step 3) is copy-paste ready.
        // A later form edit re-renders fresh placeholders via mecRenderCurl().
        const _pre = document.getElementById('mec-curl');
        if (_pre && body.ams_id) _pre.textContent = _pre.textContent
            .replace(/<AMS_ID>/g, body.ams_id)
            .replace(/<PROC_ID>/g, body.procedure_id);
        if (body.ams_id) _mecSelectedAms = body.ams_id;
        mecLoadAms();
        if (_mecPollTimer) clearInterval(_mecPollTimer);
        _mecPollTimer = setInterval(() => mecPollStatus(body.ams_id, body.procedure_id, b, body), 2000);
    } catch (e) {
        err.textContent = e.message;
        res.innerHTML = `<p style="color:var(--danger);font-size:.85em;margin:0;">Trigger failed: ${_mecEsc(e.message)}</p>`;
        btn.disabled = false; btn.textContent = 'Trigger Migration';
    }
}

async function mecPollStatus(amsId, procId, form, ids) {
    try {
        const s = await (await fetch(`${apiUrl}/api/v1/mec/status/${amsId}/${procId}`)).json();
        mecRenderResult(s, form, ids);
        if (['COMPLETED', 'FAILED', 'CANCELLED'].includes(s.state)) {
            clearInterval(_mecPollTimer); _mecPollTimer = null;
            const btn = document.getElementById('mec-submit-btn');
            btn.disabled = false; btn.textContent = 'Trigger Migration';
        }
    } catch (_) { /* transient poll error */ }
}

async function mecCancel(amsId, procId) {
    if (!amsId || !procId) return;
    const err = document.getElementById('mec-error');
    if (err) err.textContent = '';
    try {
        const resp = await fetch(`${apiUrl}/api/v1/mec/cancel/${amsId}/${procId}`, { method: 'DELETE' });
        if (!resp.ok) {
            const b = await resp.json().catch(() => ({}));
            throw new Error(b.detail || `HTTP ${resp.status}`);
        }
        if (_mecPollTimer) { clearInterval(_mecPollTimer); _mecPollTimer = null; }
        const btn = document.getElementById('mec-submit-btn');
        if (btn) { btn.disabled = false; btn.textContent = 'Trigger Migration'; }
        const res = document.getElementById('mec-result');
        if (res) res.innerHTML = '<p style="color:var(--danger);font-size:.85em;margin:0;">Procedure cancelled.</p>';
    } catch (e) {
        // The controller 409s once KubeVirt has taken over — surface that clearly.
        if (err) err.textContent = 'Cancel: ' + e.message;
    }
}

function mecRenderResult(s, form, ids) {
    const res = document.getElementById('mec-result');
    const stateColors = {
        COMPLETED: '#2e9e4f', FAILED: '#c0392b', CANCELLED: '#c0392b',
        INITIALIZING: '#d69a00', SELECTING: '#d69a00', PREPARING: '#d69a00',
        TRANSFERRING: '#3b7ddd', ACTIVATING: '#3b7ddd',
    };
    const col = stateColors[s.state] || '#8a8f98';
    const row = (k, v) => v == null || v === '' ? '' :
        `<div style="display:flex;gap:10px;padding:4px 0;font-size:.85em;">
            <span style="color:var(--text-muted);min-width:150px;">${k}</span>
            <span style="font-family:monospace;">${_mecEsc(v)}</span></div>`;
    const lat = s.migration_latency_ms != null ? `${s.migration_latency_ms} ms` : null;
    const inProgress = ['INITIALIZING', 'SELECTING', 'PREPARING', 'TRANSFERRING', 'ACTIVATING'].includes(s.state);
    res.innerHTML = `
        <div style="display:flex;align-items:center;gap:9px;margin-bottom:10px;">
            <span style="width:9px;height:9px;border-radius:50%;background:${col};"></span>
            <strong style="font-family:monospace;">${_mecEsc(s.state || '—')}</strong>
            ${inProgress ? '<div class="spinner" style="width:14px;height:14px;"></div>' : ''}
            ${inProgress ? `<button class="btn btn-sm btn-danger" style="margin-left:auto;" onclick="mecCancel('${_mecEsc(ids.ams_id || '')}','${_mecEsc(ids.procedure_id || '')}')">Cancel</button>` : ''}
        </div>
        ${row('Workload', `${form.workload_name} (${form.resource_kind})`)}
        ${row('Source cluster', form.source_cluster)}
        ${row('Decision', s.decision)}
        ${row('Reason', s.decision_reason)}
        ${row('Requested target', s.requested_target_cluster)}
        ${row('Selected target', s.target_cluster)}
        ${row('Target node', s.target_node)}
        ${row('Migration time', lat)}
        ${row('Error', s.error_details)}
        <div style="margin-top:10px;color:var(--text-muted);font-size:.76em;font-family:monospace;">
            ams=${_mecEsc(ids.ams_id || '')} · proc=${_mecEsc(ids.procedure_id || '')}
        </div>`;
}

// ═══════════════════════════════════════════════
// UEs — 5G devices (follow-me endpoints + pQoS identity)
// ═══════════════════════════════════════════════

function _ueEsc(s) { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }

async function loadUes() {
    const list = document.getElementById('ues-list');
    if (!list) return;
    try {
        const r = await fetch(`${apiUrl}/api/v1/ues`);
        const data = await r.json();
        const badge = document.getElementById('badge-ues');
        if (badge) badge.textContent = data.ues.length;
        // feed the MEC form's IMSI datalist from the inventory
        const dl = document.getElementById('mec-ue-imsi-list');
        if (dl) dl.innerHTML = data.ues.filter(u => u.imsi)
            .map(u => `<option value="${_ueEsc(u.imsi)}">${_ueEsc(u.name)} (${_ueEsc(u.host)})</option>`).join('');
        if (!data.ues.length) {
            list.innerHTML = '<div class="card" style="color:var(--text-muted);">No UEs registered yet.</div>';
            return;
        }
        list.innerHTML = data.ues.map(u => `
            <div class="card" style="margin-bottom:12px;" id="ue-card-${u.id}">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;">
                    <div>
                        <div style="font-weight:700;font-size:1.05em;">${_ueEsc(u.name)}
                            ${u.active ? '<span style="font-size:.7em;font-weight:700;color:#fff;background:#2e9e4f;border-radius:4px;padding:1px 6px;margin-left:6px;">ACTIVE</span>' : ''}
                            ${u.retired ? '<span style="font-size:.7em;font-weight:400;color:var(--text-muted);border:1px solid var(--border);border-radius:4px;padding:0 5px;margin-left:6px;">retired</span>' : ''}</div>
                        <div style="color:var(--text-muted);font-size:.85em;font-family:monospace;">
                            ${_ueEsc(u.ssh_user)}@${_ueEsc(u.host)} · 5G ${_ueEsc(u.wwan_iface)}${u.wwan_gw ? ' via ' + _ueEsc(u.wwan_gw) : ''}
                        </div>
                        <div style="font-size:.85em;margin-top:4px;">
                            IMSI: <span style="font-family:monospace;">${u.imsi ? _ueEsc(u.imsi) : '<i style="color:var(--text-muted);">unknown, read it from the SIM</i>'}</span>
                        </div>
                        ${u.notes ? `<div style="color:var(--text-muted);font-size:.78em;margin-top:2px;">${_ueEsc(u.notes)}</div>` : ''}
                        <div id="ue-status-${u.id}" style="font-size:.82em;margin-top:6px;color:var(--text-muted);">checking status…</div>
                    </div>
                    <div style="display:flex;gap:8px;flex-wrap:wrap;">
                        ${u.active ? '' : `<button class="btn btn-secondary" onclick="fmUeActivate(this,'${u.id}').then(loadUes)">Make active</button>`}
                        <button class="btn btn-secondary" onclick="ueReadImsi('${u.id}')">Read IMSI</button>
                        <button class="btn btn-secondary" onclick="ueApplyPath('${u.id}')">Apply relays</button>
                        <button class="btn btn-secondary" ${u.imsi ? '' : 'disabled'} onclick="ueUseInMec('${_ueEsc(u.imsi)}')">Use in Follow-me</button>
                        <button class="btn btn-secondary" style="color:var(--danger);" onclick="ueDelete('${u.id}','${_ueEsc(u.name)}')">Delete</button>
                    </div>
                </div>
            </div>`).join('');
        data.ues.forEach(u => _ueStatus(u.id));
    } catch (e) {
        list.innerHTML = `<div class="card" style="color:var(--danger);">Failed to load UEs: ${_ueEsc(e.message)}</div>`;
    }
}

async function _ueStatus(id) {
    const el = document.getElementById(`ue-status-${id}`);
    if (!el) return;
    try {
        const s = await (await fetch(`${apiUrl}/api/v1/ues/${id}/status`)).json();
        const dot = ok => `<span style="color:${ok ? 'var(--success,#16a34a)' : 'var(--danger)'};">●</span>`;
        el.innerHTML =
            `${dot(s.ssh)} ssh &nbsp; ${dot(s.wwan.up)} 5G ${s.wwan.up ? '(' + _ueEsc(s.wwan.ip) + ')' : 'down'} &nbsp; ` +
            `${dot(s.pod_proxy.up)} pod proxy :${s.pod_proxy.port} &nbsp; ${dot(s.vm_proxy.up)} vm proxy :${s.vm_proxy.port} &nbsp; ` +
            `${dot(s.hub_relay.up)} hub relay` +
            (s.pod_proxy.up ? ` &nbsp;·&nbsp; <a href="${s.ue_url}" target="_blank">open UE view</a>` : '');
    } catch (e) {
        el.innerHTML = `<span style="color:var(--danger);">status check failed</span>`;
    }
}

async function ueAdd() {
    const err = document.getElementById('ue-add-error');
    err.textContent = '';
    const body = {
        name: document.getElementById('ue-name').value.trim(),
        host: document.getElementById('ue-host').value.trim(),
        ssh_user: document.getElementById('ue-user').value.trim() || 'pi',
        wwan_iface: document.getElementById('ue-iface').value.trim() || 'wwan0',
        wwan_gw: document.getElementById('ue-gw').value.trim(),
        notes: document.getElementById('ue-notes').value.trim(),
    };
    if (!body.name || !body.host) { err.textContent = 'Name and management IP are required.'; return; }
    const btn = document.getElementById('ue-add-btn');
    btn.disabled = true;
    try {
        const r = await fetch(`${apiUrl}/api/v1/ues`, { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
        ['ue-name','ue-host','ue-iface','ue-gw','ue-notes'].forEach(i => document.getElementById(i).value = '');
        loadUes();
    } catch (e) { err.textContent = e.message; }
    btn.disabled = false;
}

async function ueDelete(id, name) {
    if (!confirm(`Remove UE "${name}" from the inventory? (does not touch the device)`)) return;
    await fetch(`${apiUrl}/api/v1/ues/${id}`, { method: 'DELETE' });
    loadUes();
}

async function ueReadImsi(id) {
    const el = document.getElementById(`ue-status-${id}`);
    if (el) el.innerHTML = 'reading EF_IMSI from the SIM…';
    try {
        const r = await fetch(`${apiUrl}/api/v1/ues/${id}/read-imsi`, { method: 'POST' });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || r.statusText);
        loadUes();
    } catch (e) {
        if (el) el.innerHTML = `<span style="color:var(--danger);">IMSI read failed: ${_ueEsc(e.message)}</span>`;
    }
}

async function ueApplyPath(id) {
    const el = document.getElementById(`ue-status-${id}`);
    if (el) el.innerHTML = 'installing relays (hub + device, routes over 5G)…';
    try {
        const r = await fetch(`${apiUrl}/api/v1/ues/${id}/apply-path`, { method: 'POST' });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || r.statusText);
        _ueStatus(id);
    } catch (e) {
        if (el) el.innerHTML = `<span style="color:var(--danger);">apply failed: ${_ueEsc(e.message)}</span>`;
    }
}

function ueUseInMec(imsi) {
    showSection('mec');
    const f = document.getElementById('mec-ue-imsi');
    if (f) { f.value = imsi; if (typeof mecRenderCurl === 'function') mecRenderCurl(); }
}
