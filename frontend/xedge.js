/* ══════════════════════════════════════════════════════════
   EXTREME EDGE — the vehicle Jetson as UE and as edge.
   YOLO (the dashcam's detector) runs on the vehicle or on the ARM network edge
   and moves live between them over 5G. State comes from the backend's
   /extreme-edge/* endpoints, which read the vehicle through its node agent.
   ══════════════════════════════════════════════════════════ */
let _xeTimer = null, _xeSlowTimer = null, _xePeriod = 2000;
let _xeQodSess = [];           // active QoD sessions (shaded on the uplink chart)
let _xePqUl = [];              // [{t, v}] pQoS predicted uplink capacity, when it answers
let _xeLast = null;            // last /extreme-edge/state
let _xeHist = [];              // [{t, processed, serving}] for the frame chart
const XE_HIST_S = 360;

function loadExtremeEdge() {
    xePoll();
    xeSlowPoll();
    xeLoadThresholds();
    if (_xeTimer) clearInterval(_xeTimer);
    if (_xeSlowTimer) clearInterval(_xeSlowTimer);
    _xeTimer = setInterval(xePoll, 2000);
    _xeSlowTimer = setInterval(xeSlowPoll, 15000);
}

function _xeActive() {
    const sec = document.getElementById('section-xedge');
    if (sec && sec.classList.contains('active')) return true;
    if (_xeTimer) { clearInterval(_xeTimer); _xeTimer = null; }
    if (_xeSlowTimer) { clearInterval(_xeSlowTimer); _xeSlowTimer = null; }
    xeCamClose();
    return false;
}

async function _xeGet(path) {
    const r = await fetch(`${apiUrl}/api/v1/extreme-edge/${path}`);
    if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
    return r.json();
}

// One poll at a time: when the vehicle answers slowly (5G, a migration), a timer
// that fires every 0.5-2 s would stack requests, and with the MJPEG holding a
// connection the browser's per-host limit then queues the user's own clicks.
let _xePolling = false;
async function xePoll() {
    if (_xePolling) return;
    _xePolling = true;
    try { await _xePollOnce(); } finally { _xePolling = false; }
}

async function _xePollOnce() {
    if (!_xeActive()) return;
    const dot = document.getElementById('xe-refresh-dot');
    let s;
    try { s = await _xeGet('state'); }
    catch (e) { if (dot) dot.style.background = 'var(--danger)'; return; }
    if (dot) dot.style.background = s.vehicle && s.vehicle.reachable ? '#2e9e4f' : '#d69a00';
    // poll fast while a migration runs, so the stage and the video follow it closely
    const want = s.in_flight ? 500 : 2000;
    if (_xeTimer && _xePeriod !== want) { clearInterval(_xeTimer); _xeTimer = setInterval(xePoll, want); _xePeriod = want; }
    _xeLast = s;
    const h = s.health || {};
    if (h.processed != null) {
        const prev = _xeHist[_xeHist.length - 1], w = s.wwan;
        let ul = null, dl = null;
        if (w && prev && prev.tx != null && s.now > prev.t) {
            ul = Math.max(0, (w.tx_bytes - prev.tx) * 8 / 1e6 / (s.now - prev.t));
            dl = Math.max(0, (w.rx_bytes - prev.rx) * 8 / 1e6 / (s.now - prev.t));
        }
        _xeHist.push({ t: s.now, processed: h.processed, serving: s.in_flight ? 'migrating' : s.serving, rtt: s.rtt_ms,
                       tx: w ? w.tx_bytes : null, rx: w ? w.rx_bytes : null, ul, dl });
        const cut = s.now - XE_HIST_S;
        while (_xeHist.length && _xeHist[0].t < cut) _xeHist.shift();
    }
    xeRenderKpis(s);
    xeRenderStage(s);
    xeRenderLink(s);
    xeRenderMigrations(s.migrations || []);
    xeDrawUplink(s);
    xeDrawLat(s);
    xeLoadEngine();
}

/* ── scenario autopilot (the backend's decision engine) ── */
let _xeEngine = {};
/* ── ENVELOPE API calls (pQoS AF, CAMARA QoD) ── */
let _xeApiSeq = 0, _xeApiRows = [], _xeApiEpoch = null, _xeApiBusy = false;
async function xeLoadApiLog() {
    if (_xeApiBusy) return;
    _xeApiBusy = true;
    let d;
    try { d = await _xeGet(`api-log?after=${_xeApiSeq}`); } catch (e) { return; } finally { _xeApiBusy = false; }
    if (!d || !Array.isArray(d.rows)) return;
    let rows = d.rows;
    if (d.epoch !== _xeApiEpoch) {                   // cleared (or first load): start over
        const first = _xeApiEpoch === null;
        _xeApiEpoch = d.epoch; _xeApiRows = []; _xeApiSeq = 0;
        if (!first) { try { rows = (await _xeGet('api-log?after=0')).rows || []; } catch (e) { rows = []; } }
        if (!rows.length) { const el = document.getElementById('xe-api-log'); if (el) el.innerHTML = '<span style="color:var(--text-muted);">no calls yet</span>'; return; }
    } else if (!rows.length) return;
    for (const r of rows) {
        _xeApiSeq = Math.max(_xeApiSeq, r.seq);
        if (!(r.status >= 200 && r.status < 300)) continue;   // successful calls only
        // the page re-polls pQoS and QoD every 15 s: a call repeating an earlier
        // one with the same answer folds into that row (count, latest time)
        const key = `${r.api}|${r.method}|${r.path}|${r.status}|${JSON.stringify(r.request)}|${JSON.stringify(r.response)}`;
        const i = _xeApiRows.findIndex(x => x.key === key);
        if (i >= 0) { const x = _xeApiRows.splice(i, 1)[0]; x.n++; x.t = r.t; x.ms = r.ms; _xeApiRows.unshift(x); continue; }
        _xeApiRows.unshift({ ...r, key, n: 1 });
    }
    _xeApiRows = _xeApiRows.slice(0, 60);
    const el = document.getElementById('xe-api-log');
    if (!el) return;
    const t = x => new Date(x * 1000).toLocaleTimeString([], { hour12: false });
    const js = v => escHtml(JSON.stringify(v, null, 1) || 'null');
    const sum = r => {
        const q = r.request || {}, a = r.response;
        if (r.api === 'CAMARA QoD') {
            if (r.method === 'POST') return `${q.qosProfile || ''} for ${q.device?.ipv4Address?.publicAddress || '?'} -> ${q.applicationServer?.ipv4Address || '?'}` +
                (a && a.sessionId ? `: session ${String(a.sessionId).slice(0, 8)} ${a.qosStatus || ''}` : '');
            if (r.method === 'DELETE') return `release session ${r.path.split('/').pop().slice(0, 8)}`;
            return Array.isArray(a) ? `${a.length} session(s)` : '';
        }
        const v = a && Array.isArray(a.Report) ? a.Report.map(x => `${String(x.UeID).slice(-3)}: ${(x.QosValue || []).slice(0, 1).map(n => (+n).toFixed(2)).join('')}`).join(', ') : (a && a.detail) || '';
        return `${q.QosType || ''} ${q.Method || ''}: ${v}`;
    };
    el.innerHTML = _xeApiRows.map(r => {
        const ok = r.status >= 200 && r.status < 300, col = ok ? '#2e9e4f' : 'var(--danger)';
        const badge = r.api === 'CAMARA QoD' ? '#d29922' : '#3b7ddd';
        return `<details style="border-bottom:1px solid var(--border);padding:3px 0;"><summary style="cursor:pointer;list-style:none;display:flex;gap:8px;align-items:baseline;">
            <span style="color:var(--text-muted);">${t(r.t)}</span>
            <span style="color:${badge};font-weight:700;min-width:96px;">${escHtml(r.api)}</span>
            <span>${escHtml(r.method)} ${escHtml(r.path)}</span>
            <span style="color:${col};font-weight:700;">${r.status ?? 'ERR'}</span>
            <span style="color:var(--text-muted);">${r.ms} ms${r.n > 1 ? ` &times;${r.n}` : ''}</span>
            <span style="color:var(--text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escHtml(sum(r))}</span></summary>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:4px 0 6px;">
            <pre style="margin:0;white-space:pre-wrap;font-size:.92em;background:var(--surface-2,rgba(127,127,127,.08));padding:6px;border-radius:6px;">request ${escHtml(r.url)}\n${js(r.request)}</pre>
            <pre style="margin:0;white-space:pre-wrap;font-size:.92em;background:var(--surface-2,rgba(127,127,127,.08));padding:6px;border-radius:6px;">response ${r.status ?? ''}\n${js(r.response)}</pre></div></details>`;
    }).join('') || '<span style="color:var(--text-muted);">no calls yet</span>';
}

async function xeLoadEngine() {
    try { _xeEngine = await _xeGet('engine'); } catch (e) { return; }
    const e = _xeEngine;
    xeLoadApiLog();
    const a = document.getElementById('xe-auto'), d = document.getElementById('xe-degrade');
    if (a) { a.textContent = `Autopilot: ${e.enabled ? 'on' : 'off'}`; a.className = 'btn btn-sm' + (e.enabled ? '' : ' btn-ghost'); }
    if (d) { d.className = 'btn btn-sm' + (e.degrade ? '' : ' btn-ghost'); d.style.background = e.degrade ? '#c0392b' : ''; d.style.color = e.degrade ? '#fff' : ''; }
    const st = document.getElementById('xe-engine-status');
    if (st) st.textContent = e.enabled ? `${e.status}${e.qod ? ` · QoD ${e.qod.profile} ${e.qod.verdict || 'being checked'}` : ''}`
                                       : 'Off: YOLO moves only with the buttons above. Turn the autopilot on to let greenness, pQoS and QoD decide.';
    const lg = document.getElementById('xe-engine-log');
    const icon = { 'move-edge': '⇧', 'move-vehicle': '⇩', 'qod-request': '◆', 'qod-release': '◇', stay: '✓' };
    if (lg) lg.innerHTML = (e.log || []).map(x => `<div style="padding:2px 0;border-bottom:1px solid var(--border);">
        <span style="color:var(--text-muted);">${new Date(x.t * 1000).toLocaleTimeString()}</span>
        <span style="display:inline-block;width:14px;text-align:center;">${icon[x.action] || '·'}</span>${escHtml(x.msg)}</div>`).join('')
        || '<span style="color:var(--text-muted);">No decisions yet.</span>';
}
async function xeEngineSet(body) {
    await fetch(`${apiUrl}/api/v1/extreme-edge/engine`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    xeLoadEngine();
}

async function xeSlowPoll() {
    if (!_xeActive()) return;
    xeLoadPqos();
    xeLoadQod();
}

/* ── KPIs ── */
function xeRenderKpis(s) {
    const el = document.getElementById('xe-kpis');
    if (!el) return;
    const h = s.health || {};
    const done = (s.migrations || []).filter(m => m.completed);
    const last = done[0];
    const where = s.in_flight ? 'migrating' : (s.serving === 'vehicle' ? 'Vehicle' : s.serving === 'edge' ? 'Edge' : s.serving);
    const color = s.in_flight ? '#9085e9' : (s.serving === 'vehicle' ? '#2e9e4f' : '#3b7ddd');
    const avg = (arr, k) => arr.length ? (arr.reduce((a, m) => a + (m[k] || 0), 0) / arr.length) : null;
    const deltas = done.filter(m => m.transfer === 'delta' && m.frames_lost != null);
    el.innerHTML =
        _fmKpiCard('YOLO runs on', escHtml(String(where || '—')),
            s.target ? `stream ${s.target.paused ? 'paused' : '→ ' + escHtml(s.target.ip || '')}` : '', color) +
        _fmKpiCard('YOLO FPS', h.fps != null ? h.fps : '—',
            h.processed != null ? `${h.processed.toLocaleString()} frames processed` : 'no answer') +
        _fmKpiCard('Last migration', last && last.freeze_s != null ? `${last.freeze_s}<span style="font-size:.5em;"> s</span>` : '—',
            last ? `${last.from} → ${last.to} · ${last.frames_lost != null ? last.frames_lost + ' frames lost' : 'stall pending'}` : 'none yet', '#2e9e4f') +
        _fmKpiCard('Frames lost / hop', deltas.length ? Math.round(avg(deltas, 'frames_lost')) : '—',
            deltas.length ? `avg of ${deltas.length} delta hop(s) at 12 FPS` : 'no delta hops yet', '#d69a00') +
        _fmKpiCard('On the wire / hop', last && last.wire_mib != null ? `${last.wire_mib}<span style="font-size:.5em;"> MiB</span>` : (last && last.image_mib != null ? `${last.image_mib}<span style="font-size:.5em;"> MiB</span>` : '—'),
            last ? (last.transfer === 'delta' ? `delta of a ${last.image_mib} MiB image` : 'full image (no common base)') : '', '#3b7ddd');
}

/* ── Stage (as on Follow-Me) ── */
const XE_GREEN = '#2e9e4f', XE_BLUE = '#3b7ddd', XE_PURPLE = '#9085e9';

function xeRenderStage(s) {
    const h = s.health || {};
    const mig = (s.migrations || []).find(m => !(m.completed || m.failed));
    const onV = s.serving === 'vehicle' && !mig, onE = s.serving === 'edge' && !mig;

    // the edge, on top
    const box = document.getElementById('xe-edges');
    if (box) {
        const col = mig ? XE_PURPLE : XE_BLUE;
        box.innerHTML = `<div id="xe-edge-0" style="flex:0 1 260px;min-width:220px;border:2px solid ${onE || mig ? col : 'var(--border)'};border-radius:12px;padding:12px 14px;background:var(--surface);${onE ? 'box-shadow:0 0 0 3px ' + col + '22;' : ''}">
            <div style="display:flex;align-items:center;gap:6px;">
                <span style="font-size:1.05em;">🖥️</span>
                <div style="font-weight:600;font-family:monospace;">${escHtml(s.edge?.name || 'drift-edge-agx')}</div>
                ${onE ? `<span style="font-size:.55em;font-weight:700;color:#fff;background:${col};padding:1px 6px;border-radius:8px;margin-left:auto;">● SERVING</span>` : ''}
            </div>
            <div style="color:var(--text-muted);font-size:.74em;margin:4px 0 8px;">Jetson AGX Orin · network edge · 10.220.2.244</div>
            <div style="display:flex;flex-direction:column;gap:4px;min-height:22px;">
                ${onE ? `<div style="display:flex;align-items:center;gap:6px;font-size:.8em;background:rgba(59,125,221,.12);border:1px solid rgba(59,125,221,.35);border-radius:6px;padding:3px 7px;">
                    <span style="font-size:.66em;text-transform:uppercase;font-weight:700;color:${XE_BLUE};">pod</span>
                    <span style="font-family:monospace;">${escHtml(s.pod || 'emt-yolo')}</span>
                    <span style="margin-left:auto;font-family:monospace;color:var(--text-muted);">${h.fps ?? '—'} FPS</span></div>`
                      : '<div style="font-size:.72em;color:var(--text-muted);">no workloads</div>'}
            </div>
            ${xeEdgeBars(s.energy)}
        </div>`;
    }

    xeVehicleBars(s.energy);
    xeClockButtons(s.energy);

    // the vehicle UE, as its interior: the centre tablet carries the state
    const tablet = document.getElementById('xe-tablet');
    const badge = document.getElementById('xe-phone-badge');
    const body = document.getElementById('xe-phone-body');
    const led = document.getElementById('xe-dashcam-led');
    if (tablet) {
        const col = onV ? XE_GREEN : mig ? XE_PURPLE : onE ? XE_BLUE : '#23292f';
        tablet.style.borderColor = col;
        tablet.style.boxShadow = onV ? `0 0 0 3px ${XE_GREEN}55, 0 0 30px ${XE_GREEN}77` : `0 8px 24px rgba(0,0,0,.6)${onE || mig ? `, 0 0 18px ${col}55` : ''}`;
    }
    if (led) led.setAttribute('fill', onE ? XE_BLUE : onV ? '#4ade80' : mig ? XE_PURPLE : '#ff3b30');
    if (badge) {
        badge.textContent = mig ? '⇄ MIGRATING' : onV ? '● SERVING ITSELF' : onE ? '▲ SERVED BY THE EDGE' : (s.target?.paused ? '⏸ PAUSED' : '—');
        badge.style.background = mig ? XE_PURPLE : onV ? XE_GREEN : onE ? XE_BLUE : 'rgba(0,0,0,.6)';
    }
    if (body) {
        const modemUp = s.link?.modem === 'active';
        const bars = Array.from({ length: 5 }, (_, i) =>
            `<div style="width:5px;height:${6 + i * 4}px;border-radius:1px;background:${modemUp && i < 4 ? '#4ade80' : 'rgba(255,255,255,.15)'};"></div>`).join('');
        const where = mig ? `<span style="color:${XE_PURPLE};">moving ${escHtml(mig.from)} → ${escHtml(mig.to)}</span>`
                    : onV ? `<span style="color:#4ade80;">YOLO on board</span>`
                    : onE ? `<span style="color:#7fb2ff;">YOLO on the edge</span>` : '—';
        body.innerHTML = `
            <div style="position:absolute;top:10px;right:12px;display:flex;align-items:flex-end;gap:3px;height:22px;" title="5G (wwan0)">${bars}</div>
            <div style="position:absolute;left:0;right:0;bottom:0;padding:22px 12px 8px;background:linear-gradient(0deg,rgba(0,0,0,.82),rgba(0,0,0,0));display:flex;align-items:flex-end;gap:16px;">
                <div style="font-size:.8em;font-weight:700;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${where}</div>
                <div style="text-align:right;"><span style="font-size:1.1em;font-weight:700;color:#fff;font-family:var(--mono);">${h.fps ?? '—'}</span><span style="font-size:.66em;color:#9fb4cc;"> FPS</span></div>
                <div style="text-align:right;"><span style="font-size:1.1em;font-weight:700;color:#fff;font-family:var(--mono);">${s.rtt_ms != null ? s.rtt_ms.toFixed(1) : '—'}</span><span style="font-size:.66em;color:#9fb4cc;"> ms</span></div>
            </div>`;
        xeCamUpdate(s, mig);
    }
    const note = document.getElementById('xe-stage-note');
    if (note) note.innerHTML = onV
        ? `The vehicle <span style="color:${XE_GREEN};font-weight:600;">serves itself</span>: the dashcam frames reach YOLO on board, with no network in the loop.`
        : onE ? `The vehicle is <span style="color:${XE_BLUE};font-weight:600;">served by the edge</span>: the dashcam streams over the 5G uplink to YOLO on the Orin.`
        : mig ? `YOLO is moving live: frozen, its memory delta sent over 5G, restored. Its counters carry over.` : '';

    // the windshield follows the dashcam drive: day sky or night sky
    // a poll that left before a scene switch still reports the old drive: hold
    // the requested one until the vehicle confirms it (or a few seconds pass)
    if (_xeScenePend && (s.scene === _xeScenePend.scene || Date.now() > _xeScenePend.until)) _xeScenePend = null;
    const scene = _xeScenePend ? _xeScenePend.scene : s.scene;
    xeSky(scene);
    if (scene) _xeScene = scene;
    const be = document.getElementById('xe-move-edge'), bv = document.getElementById('xe-move-vehicle');
    if (be) be.disabled = !!mig || s.serving === 'edge';
    if (bv) bv.disabled = !!mig || s.serving === 'vehicle';
    requestAnimationFrame(() => _xeDrawArrow(onE, mig));
}

// Arrow from the vehicle (phone top) up to the edge when the edge serves it;
// purple, pointing the way YOLO moves, during a migration; a faint connector
// when the vehicle serves itself.
function _xeDrawArrow(onE, mig) {
    const stage = document.getElementById('xe-stage'), svg = document.getElementById('xe-stage-svg');
    const phone = document.getElementById('xe-phone'), edge = document.getElementById('xe-edge-0');
    if (!stage || !svg || !phone || !edge) return;
    const cam = document.getElementById('xe-dashcam');
    const c = stage.getBoundingClientRect(), p = (cam || phone).getBoundingClientRect(), b = edge.getBoundingClientRect();
    const px = p.left - c.left + p.width / 2, py = p.top - c.top;
    const bx = b.left - c.left + b.width / 2, by = b.top - c.top + b.height;
    const my = (py + by) / 2;
    const up = `M ${px} ${py} C ${px} ${my}, ${bx} ${my}, ${bx} ${by + 4}`;
    const down = `M ${bx} ${by} C ${bx} ${my}, ${px} ${my}, ${px} ${py - 4}`;
    const color = mig ? XE_PURPLE : XE_BLUE;
    const defs = `<defs>
        <marker id="xe-ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${color}"/></marker>
        <marker id="xe-ah-dim" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--border)"/></marker></defs>`;
    let path;
    if (mig || onE) {
        const d = mig && mig.to === 'vehicle' ? down : up;
        path = `<path d="${d}" fill="none" stroke="${color}" stroke-width="3.5" stroke-linecap="round" stroke-dasharray="10 7" marker-end="url(#xe-ah)"><animate attributeName="stroke-dashoffset" from="34" to="0" dur="0.7s" repeatCount="indefinite"/></path>`;
    } else {
        path = `<path d="${up}" fill="none" stroke="var(--border)" stroke-width="1.5" stroke-dasharray="3 5" opacity="0.6" marker-end="url(#xe-ah-dim)"/>`;
    }
    svg.innerHTML = defs + path;
}

/* ── in-car view on the tablet ── */
// The MJPEG comes from the vehicle's relay, which connects to wherever YOLO runs
// at connect time, so the stream is reopened whenever the DNAT target changes.
let _xeCamTarget = null, _xeCamRetry = null, _xeCamCtl = null;

// The MJPEG is read with fetch() and each JPEG is handed to the <img>. An
// endless multipart stream in an <img src> keeps the browser tab "loading"
// for as long as the page is open; a fetch never counts as page loading.
async function xeCamOpen() {
    const img = document.getElementById('xe-cam');
    if (!img) return;
    if (_xeCamCtl) _xeCamCtl.abort();
    const ctl = _xeCamCtl = new AbortController();
    try {
        const r = await fetch(`${apiUrl}/api/v1/extreme-edge/stream?t=${Date.now()}`, { signal: ctl.signal, cache: 'no-store' });
        if (!r.ok || !r.body) throw new Error(`HTTP ${r.status}`);
        const rd = r.body.getReader(), dec = new TextDecoder();
        let buf = new Uint8Array(0);
        for (;;) {
            const { value, done } = await rd.read();
            if (done) break;
            const nb = new Uint8Array(buf.length + value.length);
            nb.set(buf); nb.set(value, buf.length); buf = nb;
            // each part: headers (with Content-Length), a blank line, the JPEG
            for (;;) {
                let he = -1;
                for (let i = 0; i + 3 < buf.length; i++)
                    if (buf[i] === 13 && buf[i + 1] === 10 && buf[i + 2] === 13 && buf[i + 3] === 10) { he = i; break; }
                if (he < 0) break;
                const m = /content-length:\s*(\d+)/i.exec(dec.decode(buf.subarray(0, he)));
                if (!m) { buf = buf.subarray(he + 4); continue; }
                const st = he + 4, n = +m[1];
                if (buf.length < st + n) break;
                const url = URL.createObjectURL(new Blob([buf.slice(st, st + n)], { type: 'image/jpeg' }));
                const old = img.dataset.u;
                img.src = url; img.dataset.u = url; img.style.display = 'block';
                if (old) setTimeout(() => URL.revokeObjectURL(old), 1000);
                buf = buf.subarray(st + n);
            }
        }
    } catch (e) {
        if (ctl.signal.aborted) return;
    }
    if (_xeCamCtl !== ctl) return;
    img.style.display = 'none';                      // the stream ended or failed: reconnect
    clearTimeout(_xeCamRetry); _xeCamRetry = setTimeout(xeCamOpen, 500);
}

function xeCamUpdate(s, mig) {
    const img = document.getElementById('xe-cam');
    if (!img) return;
    // open once: the vehicle's relay keeps the stream and follows YOLO itself;
    // reopening on a move would add a browser reconnect to the visible freeze.
    // No overlay: the passenger sees the stream itself stall, as it really does.
    if (!_xeCamTarget) { _xeCamTarget = 'open'; xeCamOpen(); }
}

function xeCamClose() {
    const img = document.getElementById('xe-cam');
    if (_xeCamCtl) { _xeCamCtl.abort(); _xeCamCtl = null; }
    if (img) { img.removeAttribute('src'); img.style.display = 'none'; }
    clearTimeout(_xeCamRetry); _xeCamTarget = null;
}

/* ── energy bars and scenario clock ── */
function _xeBarRow(icon, label, pct, color, tag) {
    const v = pct == null ? null : Math.round(pct);
    return `<div style="margin-top:6px;"><div style="display:flex;justify-content:space-between;font-size:.72em;color:var(--text-muted);">
        <span>${icon} ${label}${tag ? ` <span style="font-size:.85em;border:1px solid var(--border);border-radius:5px;padding:0 4px;">${escHtml(tag)}</span>` : ''}</span>
        <span style="font-family:var(--mono);">${v == null ? '—' : v + '%'}</span></div>
        <div style="background:rgba(127,127,127,.2);border-radius:6px;height:8px;overflow:hidden;margin-top:3px;">
        <div style="width:${v || 0}%;height:100%;background:${color};"></div></div></div>`;
}

function xeEdgeBars(en) {
    const e = (en || {}).edge || {};
    return _xeBarRow('🌿', 'greenness', e.greenness != null ? e.greenness * 100 : null, '#2e9e4f', e.source) +
           _xeBarRow('⚡', 'energy', e.energy_pct != null ? e.energy_pct * 100 : null, '#3b7ddd');
}

function xeVehicleBars(en) {
    const el = document.getElementById('xe-veh-energy');
    if (!el) return;
    const v = (en || {}).vehicle || {};
    const sub = v.residual_kwh != null ? `EV battery, ${v.residual_kwh} kWh` : 'EV battery';
    el.innerHTML = `<div style="font-size:.78em;font-weight:600;white-space:nowrap;">Vehicle<div style="font-size:.8em;font-weight:400;color:var(--text-muted);">${escHtml(sub)}</div></div>
        <div>${_xeBarRow('🌿', 'greenness', v.greenness != null ? v.greenness * 100 : null, '#2e9e4f', v.source)}</div>
        <div>${_xeBarRow('🔋', 'battery', v.battery_pct, '#3b7ddd')}</div>`;
}

const XE_TIMES = [[7, 'Morning 07:00'], [12, 'Noon 12:00'], [17, 'Afternoon 17:00'], [20, 'Evening 20:00']];
// The dashcam drive that matches the scenario's time of day (the EMT dataset
// has two daylight highway drives and night drives, no real dusk footage).
const XE_SKY = { day: ['#6f9cc8', '#c9d9e6', '#5f6670', '#4b5058'],
                 afternoon: ['#c98a4b', '#ecc48f', '#6a625a', '#524a44'],
                 night: ['#0e1a2b', '#1d3550', '#151b23', '#11161d'] };
let _xeScene = null;
let _xeScenePend = null;     // {scene, until}: a switch the state has not confirmed yet
function xeSceneFor(h) { return h >= 6 && h < 15 ? 'day' : h >= 15 && h < 18.5 ? 'afternoon' : 'night'; }
function xeSky(scene) {
    (XE_SKY[scene] || XE_SKY.night).forEach((c, i) => document.getElementById('xe-sky-' + i)?.setAttribute('stop-color', c));
    for (const sc of Object.keys(XE_SKY)) {
        const b = document.getElementById('xe-scene-' + sc);
        if (b) b.className = 'btn btn-sm' + (scene === sc ? '' : ' btn-ghost');
    }
}
let _xeClockPend = null;     // {hour, until}: a click the state has not confirmed yet
function xeClockButtons(en) {
    const el = document.getElementById('xe-clock');
    if (!el) return;
    let mode = (en || {}).mode, h = (en || {}).hour;
    if (_xeClockPend && ((mode === 'fixed' && Math.abs(h - _xeClockPend.hour) < 0.01) || Date.now() > _xeClockPend.until)) _xeClockPend = null;
    if (_xeClockPend) { mode = 'fixed'; h = _xeClockPend.hour; }
    const html = XE_TIMES.map(([v, label]) => {
        const on = mode === 'fixed' && Math.abs(h - v) < 0.01;
        return `<button class="btn btn-sm${on ? '' : ' btn-ghost'}" onclick='xeScenario({"hour":${v}})'>${label}</button>`;
    }).join('');
    // rewriting the buttons on every poll would swallow a click that lands between
    // mousedown and mouseup, so they are replaced only when they change
    if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
    const src = ((en || {}).edge || {}).source || '';
    const p = document.getElementById('xe-src-profile'), e = document.getElementById('xe-src-ecb');
    if (p) p.className = 'btn btn-sm' + (src.startsWith('profile') ? '' : ' btn-ghost');
    if (e) e.className = 'btn btn-sm' + (src === 'ecb' ? '' : ' btn-ghost');
    const vsrc = ((en || {}).vehicle || {}).source || '';
    const ve = document.getElementById('xe-vsrc-ecb'), vc = document.getElementById('xe-vsrc-cfg');
    if (ve) ve.className = 'btn btn-sm' + (vsrc.startsWith('ECB') ? '' : ' btn-ghost');
    if (vc) vc.className = 'btn btn-sm' + (vsrc.startsWith('configured') ? '' : ' btn-ghost');
}

async function xeScenario(body) {
    // a time of day also picks the matching dashcam drive: switch it at once,
    // alongside the clock, not after a round trip
    let sw = null;
    if (body.hour != null) {
        _xeClockPend = { hour: body.hour, until: Date.now() + 8000 };
        xeClockButtons(_xeLast && _xeLast.energy);   // show the click at once
        const sc = xeSceneFor(body.hour);
        if (sc !== _xeScene) sw = xeScene(sc);
    }
    const r = await fetch(`${apiUrl}/api/v1/extreme-edge/scenario`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) _xeClockPend = null;
    if (r.ok && body.mode === 'live') {
        const sc = xeSceneFor((await r.json()).effective_hour);
        if (sc !== _xeScene) sw = xeScene(sc);
    }
    await sw;
    xePoll();
}

/* ── 5G uplink & QoD (left of the car) ── */
function xeDrawUplink(s) {
    const c = document.getElementById('xe-ul-canvas');
    const last = _xeHist[_xeHist.length - 1] || {};
    const now = document.getElementById('xe-ul-now'), st = document.getElementById('xe-ul-stat');
    if (now) now.textContent = last.ul != null ? last.ul.toFixed(1) : '—';
    const qod = _xeQodSess.length ? _xeQodSess.map(x => x.qosProfile).join('+') : 'none';
    if (st) st.textContent = `down ${last.dl != null ? last.dl.toFixed(1) : '—'} · QoD ${qod}`;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1, W = c.clientWidth, H = c.clientHeight;
    c.width = W * dpr; c.height = H * dpr;
    const g = c.getContext('2d'); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
    const pts = _xeHist.filter(p => p.ul != null);
    if (pts.length < 2) { g.fillStyle = '#7d8590'; g.font = '12px sans-serif'; g.fillText('collecting…', 10, 20); return; }
    const t1 = s.now, t0 = t1 - XE_HIST_S, L = 34, R = 8, T = 8, B = 18;
    const x = t => L + (W - L - R) * (t - t0) / (t1 - t0);
    const pq = _xePqUl.filter(p => p.t >= t0);
    const hi = Math.max(5, ...pts.map(p => Math.max(p.ul, p.dl)), ...pq.map(p => p.v)) * 1.1;
    const y = v => T + (H - T - B) * (1 - v / hi);
    for (const q of _xeQodSess) {                                   // QoD sessions
        const a = Math.max(t0, Date.parse(q.startedAt) / 1000 || t0), b = Math.min(t1, Date.parse(q.expiresAt) / 1000 || t1);
        if (b <= a) continue;
        g.fillStyle = 'rgba(210,153,34,.18)'; g.fillRect(x(a), T, x(b) - x(a), H - T - B);
        g.fillStyle = '#d29922'; g.font = '9px monospace'; g.fillText(q.qosProfile || 'QoD', x(a) + 3, T + 9);
    }
    for (const m of (s.migrations || [])) {                         // migrations
        if (!m.started_at) continue;
        const a = Math.max(t0, m.started_at), b = Math.min(t1, m.finished_at || (m.failed || m.completed ? m.started_at : t1));
        if (b <= t0 || a >= t1) continue;
        g.fillStyle = 'rgba(144,133,233,.22)'; g.fillRect(x(a), T, Math.max(2, x(b) - x(a)), H - T - B);
    }
    for (let i = 1; i < _xeHist.length; i++) {                      // where YOLO ran
        const a = _xeHist[i - 1], b = _xeHist[i];
        g.fillStyle = a.serving === 'vehicle' ? 'rgba(46,158,79,.6)' : a.serving === 'edge' ? 'rgba(59,125,221,.6)' : 'rgba(144,133,233,.6)';
        g.fillRect(x(a.t), H - B + 4, Math.max(1, x(b.t) - x(a.t)), 5);
    }
    g.strokeStyle = '#30363d'; g.lineWidth = 1; g.beginPath(); g.moveTo(L, T); g.lineTo(L, H - B); g.lineTo(W - R, H - B); g.stroke();
    g.fillStyle = '#7d8590'; g.font = '10px monospace';
    g.fillText(hi.toFixed(0), 2, T + 8); g.fillText('0', 2, H - B);
    g.fillText(`-${XE_HIST_S / 60} min`, L, H - 2); g.fillText('now', W - R - 22, H - 2);
    const line = (key, color, width, dash) => {
        g.strokeStyle = color; g.lineWidth = width; g.setLineDash(dash || []); g.beginPath();
        pts.forEach((p, i) => (i ? g.lineTo(x(p.t), y(p[key])) : g.moveTo(x(p.t), y(p[key])))); g.stroke(); g.setLineDash([]);
    };
    line('dl', '#6e7681', 1.2);
    line('ul', '#e6edf3', 1.8);
    if (pq.length > 1) {                                            // pQoS predicted uplink
        g.strokeStyle = '#d29922'; g.lineWidth = 1.4; g.setLineDash([4, 3]); g.beginPath();
        pq.forEach((p, i) => (i ? g.lineTo(x(p.t), y(p.v)) : g.moveTo(x(p.t), y(p.v)))); g.stroke(); g.setLineDash([]);
    }
}

/* ── side charts ── */
function _xeChart(canvasId, s, valueOf, opts) {
    const c = document.getElementById(canvasId);
    if (!c) return null;
    const dpr = window.devicePixelRatio || 1, W = c.clientWidth, H = c.clientHeight;
    c.width = W * dpr; c.height = H * dpr;
    const g = c.getContext('2d'); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
    if (_xeHist.length < 2) { g.fillStyle = '#7d8590'; g.font = '12px sans-serif'; g.fillText('collecting…', 10, 20); return null; }
    const t1 = s.now, t0 = t1 - XE_HIST_S, L = 46, R = 8, T = 8, B = 18;
    const x = t => L + (W - L - R) * (t - t0) / (t1 - t0);
    const vals = _xeHist.map(valueOf).filter(v => v != null);
    let lo = vals.length ? (opts.zero ? 0 : Math.min(...vals)) : 0;
    let hi = vals.length ? Math.max(...vals, lo + (opts.minSpan || 1)) : (opts.minSpan || 1);
    const y = v => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    for (let i = 1; i < _xeHist.length; i++) {              // where YOLO was served
        const a = _xeHist[i - 1], b = _xeHist[i];
        g.fillStyle = a.serving === 'vehicle' ? 'rgba(46,158,79,.6)' : a.serving === 'edge' ? 'rgba(59,125,221,.6)' : 'rgba(144,133,233,.6)';
        g.fillRect(x(a.t), H - B + 4, Math.max(1, x(b.t) - x(a.t)), 5);
    }
    for (const m of (s.migrations || [])) {                  // migration windows
        if (!m.started_at) continue;
        const a = Math.max(t0, m.started_at), b = Math.min(t1, m.finished_at || (m.failed || m.completed ? m.started_at : t1));
        if (b <= t0 || a >= t1) continue;
        g.fillStyle = 'rgba(144,133,233,.22)';
        g.fillRect(x(a), T, Math.max(2, x(b) - x(a)), H - T - B);
    }
    if (opts.gaps) for (let i = 1; i < _xeHist.length; i++) {   // no response
        if (valueOf(_xeHist[i]) == null) { g.fillStyle = 'rgba(248,81,73,.35)'; g.fillRect(x(_xeHist[i - 1].t), T, Math.max(2, x(_xeHist[i].t) - x(_xeHist[i - 1].t)), H - T - B); }
    }
    g.strokeStyle = '#30363d'; g.lineWidth = 1; g.beginPath(); g.moveTo(L, T); g.lineTo(L, H - B); g.lineTo(W - R, H - B); g.stroke();
    g.fillStyle = '#7d8590'; g.font = '10px monospace';
    g.fillText(opts.fmt(hi), 2, T + 8); g.fillText(opts.fmt(lo), 2, H - B);
    g.fillText(`-${XE_HIST_S / 60} min`, L, H - 2); g.fillText('now', W - R - 22, H - 2);
    g.strokeStyle = '#e6edf3'; g.lineWidth = 1.6; g.beginPath();
    let pen = false;
    for (const p of _xeHist) {
        const v = valueOf(p);
        if (v == null) { pen = false; continue; }
        if (pen) g.lineTo(x(p.t), y(v)); else g.moveTo(x(p.t), y(v));
        pen = true;
    }
    g.stroke();
    return vals;
}

function xeDrawFrames(s) {
    const now = document.getElementById('xe-frm-now'), st = document.getElementById('xe-frm-stat');
    if (now) now.textContent = s.health?.processed != null ? s.health.processed.toLocaleString() : '—';
    const last = (s.migrations || []).find(m => m.completed && m.stall_s != null);
    if (st) st.textContent = last ? `last stall ${last.stall_s.toFixed(1)} s · ${last.frames_lost} frames` : '—';
    _xeChart('xe-frm-canvas', s, p => p.processed, { fmt: v => Math.round(v).toLocaleString() });
}

function xeDrawLat(s) {
    const now = document.getElementById('xe-lat-now'), st = document.getElementById('xe-lat-stat');
    if (now) now.textContent = s.rtt_ms != null ? s.rtt_ms.toFixed(1) : '—';
    const vals = _xeChart('xe-lat-canvas', s, p => p.rtt, { zero: true, minSpan: 5, gaps: true, fmt: v => v.toFixed(0) });
    if (st && vals && vals.length) {
        const sorted = [...vals].sort((a, b) => a - b);
        st.textContent = `p50 ${sorted[Math.floor(sorted.length / 2)].toFixed(1)} · max ${sorted[sorted.length - 1].toFixed(1)} ms`;
    }
}

/* ── 5G link card ── */
let _xeCongestOn = false;
async function xeCongest() {
    const on = !_xeCongestOn, b = document.getElementById('xe-congest');
    if (b) b.disabled = true;
    try {
        const r = await fetch(`${apiUrl}/api/v1/extreme-edge/congestion`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on }) });
        if (r.ok) _xeCongestOn = on;
        else { const m = document.getElementById('xe-move-msg'); if (m) m.textContent = `congestion: ${(await r.text()).slice(0, 140)}`; }
    } finally { if (b) b.disabled = false; xeCongestButton(); xePoll(); }
}

function xeCongestButton() {
    const b = document.getElementById('xe-congest');
    if (!b) return;
    b.className = 'btn btn-sm' + (_xeCongestOn ? '' : ' btn-ghost');
    b.style.background = _xeCongestOn ? '#c0392b' : ''; b.style.color = _xeCongestOn ? '#fff' : '';
    b.textContent = _xeCongestOn ? '5G uplink congested: on' : 'Congest the 5G uplink';
}

function xeRenderLink(s) {
    if (s.congestion !== undefined) { _xeCongestOn = !!s.congestion; xeCongestButton(); }
    const el = document.getElementById('xe-link');
    if (!el) return;
    const l = s.link || {}, mec = s.mec || {};
    const row = (k, v) => `<div style="display:flex;justify-content:space-between;gap:10px;font-size:.82em;padding:4px 0;border-bottom:1px solid var(--border);"><span style="color:var(--text-muted);">${k}</span><span style="font-family:var(--mono);">${v}</span></div>`;
    el.innerHTML =
        row('UE address (wwan0)', escHtml(l.ue_ip || '—')) +
        row('modem data call', escHtml(l.modem || '—')) +
        row('control tunnel', l.tunnel_handshake_age_s != null ? `handshake ${l.tunnel_handshake_age_s} s ago` : '—') +
        row('checkpoint data path', escHtml(l.bulk_path === '5g' ? 'raw 5G (wwan0)' : (l.bulk_path || '—'))) +
        row('delta base', escHtml(l.base || 'none (next hop sends the full image)')) +
        row('node agent', s.vehicle?.reachable ? 'reachable' : '<span style="color:var(--danger);">unreachable</span>') +
        row('uplink congestion', s.congestion ? `<span style="color:var(--danger);">emulated, ${escHtml(s.congestion)}</span>` : 'none') +
        row('MEC 021 app (AMS)', mec.ams_id ? `registered ${escHtml(mec.ams_id.slice(0, 8))}` : 'registered on the first move') +
        row('last mobility procedure', mec.procedure_id
            ? `<span title="${escHtml(mec.reason || mec.error || '')}">${escHtml(mec.procedure_id.slice(0, 8))} ${escHtml(mec.state || '')}</span>`
            : '—');
}

/* ── pQoS ── */
async function xeLoadPqos() {
    const el = document.getElementById('xe-pqos');
    if (!el) return;
    let p;
    try { p = await _xeGet('pqos'); } catch (e) { el.innerHTML = `<span style="color:var(--danger);font-size:.82em;">${escHtml(e.message)}</span>`; return; }
    const ulp = p.metrics && p.metrics.ul_mbps;
    if (ulp && ulp.value != null) { _xePqUl.push({ t: Date.now() / 1000, v: ulp.value }); _xePqUl = _xePqUl.slice(-200); }
    const key = document.getElementById('xe-ul-pq-key');
    if (key) key.textContent = (ulp && ulp.value != null) ? '\u2504 pQoS' : '\u2504 pQoS n/a';
    const lbl = { dl_mbps: ['Downlink capacity', 'Mbps', `≥ ${p.thresholds.dl_min_mbps}`], ul_mbps: ['Uplink capacity', 'Mbps', `≥ ${p.thresholds.ul_min_mbps}`],
                  latency_ms: ['Predicted latency', 'ms', `≤ ${p.thresholds.lat_max_ms}`] };
    const rows = Object.entries(p.metrics).map(([k, m]) => {
        const [name, unit, thr] = lbl[k];
        const val = m.value != null ? `<b>${m.value}</b> ${unit}` : '<span style="color:var(--text-muted);">no data</span>';
        const v = m.met == null ? '' : (m.met ? '<span style="color:#2e9e4f;">✓</span>' : '<span style="color:var(--danger);">✗</span>');
        return `<div style="display:flex;justify-content:space-between;gap:8px;font-size:.84em;padding:5px 0;border-bottom:1px solid var(--border);">
                  <span>${name} <span style="color:var(--text-muted);font-size:.85em;">(${m.method})</span></span>
                  <span style="font-family:var(--mono);">${val} <span style="color:var(--text-muted);">${thr}</span> ${v}</span></div>`;
    }).join('');
    const notes = [...new Set(Object.values(p.metrics).map(m => m.note).filter(Boolean))];
    const verdict = p.degradation_expected == null ? '<span style="color:var(--text-muted);">no prediction for this UE</span>'
        : (p.degradation_expected ? '<span style="color:var(--danger);font-weight:700;">⚠ degradation expected</span>' : '<span style="color:#2e9e4f;font-weight:700;">✓ QoS sufficient</span>');
    el.innerHTML = `<div style="font-size:.78em;color:var(--text-muted);margin-bottom:6px;font-family:var(--mono);">IMSI ${escHtml(p.imsi)}</div>${rows}
        <div style="margin-top:8px;font-size:.84em;">${verdict}</div>
        ${notes.length ? `<div style="margin-top:6px;font-size:.72em;color:var(--text-muted);">${notes.map(escHtml).join('<br>')}</div>` : ''}`;
}

async function xeLoadThresholds() {
    try {
        const t = await _xeGet('pqos-thresholds');
        document.getElementById('xe-pq-dl').value = t.dl_min_mbps;
        document.getElementById('xe-pq-ul').value = t.ul_min_mbps;
        document.getElementById('xe-pq-lat').value = t.lat_max_ms;
    } catch (e) { /* the card shows the error */ }
}

async function xeApplyThresholds() {
    const body = { dl_min_mbps: +document.getElementById('xe-pq-dl').value,
                   ul_min_mbps: +document.getElementById('xe-pq-ul').value,
                   lat_max_ms: +document.getElementById('xe-pq-lat').value };
    await fetch(`${apiUrl}/api/v1/extreme-edge/pqos-thresholds`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    xeLoadPqos();
}

/* ── QoD ── */
async function xeLoadQod() {
    const el = document.getElementById('xe-qod');
    if (!el) return;
    let q;
    try { q = await _xeGet('qod'); } catch (e) { el.innerHTML = `<span style="color:var(--danger);font-size:.82em;">${escHtml(e.message)}</span>`; return; }
    const head = `<div style="font-size:.76em;color:var(--text-muted);margin-bottom:6px;">UE <span style="font-family:var(--mono);">${escHtml(q.ue_ip)}</span> ↔ app server <span style="font-family:var(--mono);">${escHtml(q.app_server)}</span> (YOLO on the edge)</div>`;
    _xeQodSess = q.sessions || [];
    if (!q.sessions.length) { el.innerHTML = head + '<div style="font-size:.84em;color:var(--text-muted);">No active session.</div>'; return; }
    el.innerHTML = head + q.sessions.map(x => `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:.82em;padding:5px 0;border-bottom:1px solid var(--border);">
            <span><b>${escHtml(x.qosProfile || '')}</b> <span style="color:var(--text-muted);font-family:var(--mono);">${escHtml((x.sessionId || '').slice(0, 8))}</span></span>
            <span style="color:var(--text-muted);">until ${x.expiresAt ? new Date(x.expiresAt).toLocaleTimeString() : '—'}</span>
            <button class="btn btn-sm btn-ghost" onclick="xeQodRelease('${escHtml(x.sessionId)}')">Release</button>
        </div>`).join('') +
        '<div style="font-size:.7em;color:var(--text-muted);margin-top:6px;">Note: QoD sessions for this SIM are accepted but not yet enforced by the network (pending with NCSRD).</div>';
}

async function xeQodRequest() {
    const msg = document.getElementById('xe-qod-msg');
    const body = { profile: document.getElementById('xe-qod-profile').value, duration: +document.getElementById('xe-qod-dur').value || 300 };
    const r = await fetch(`${apiUrl}/api/v1/extreme-edge/qod`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (msg) msg.textContent = r.ok ? `${body.profile} requested` : `failed: ${(await r.text()).slice(0, 120)}`;
    xeLoadQod();
}

async function xeQodRelease(id) {
    await fetch(`${apiUrl}/api/v1/extreme-edge/qod/${encodeURIComponent(id)}`, { method: 'DELETE' });
    xeLoadQod();
}

/* ── move ── */
async function xeMove(to) {
    const msg = document.getElementById('xe-move-msg');
    if (msg) msg.textContent = `MEC 021 mobility procedure to the ${to}…`;
    const r = await fetch(`${apiUrl}/api/v1/extreme-edge/move`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ to }) });
    if (msg) msg.textContent = r.ok ? `migrating to the ${to}` : `failed: ${(await r.text()).slice(0, 160)}`;
    xePoll();
}

/* ── dashcam scene ── */
async function xeScene(scene) {
    const msg = document.getElementById('xe-move-msg');
    if (msg) msg.textContent = `switching the dashcam to the ${scene} drive…`;
    _xeScene = scene;
    _xeScenePend = { scene, until: Date.now() + 8000 };
    xeSky(scene);                                    // highlight now, not on the next poll
    const r = await fetch(`${apiUrl}/api/v1/extreme-edge/scene`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scene }) });
    if (!r.ok) _xeScenePend = null;                  // show what the vehicle really replays
    if (msg) msg.textContent = r.ok ? `${scene} drive` : `failed: ${(await r.text()).slice(0, 140)}`;
    xePoll();
}

/* ── migrations table ── */
function xeRenderMigrations(migs) {
    const el = document.getElementById('xe-migrations');
    if (!el) return;
    if (!migs.length) {
        el.innerHTML = '<div class="card" style="font-size:.84em;color:var(--text-muted);">No vehicle ↔ edge migrations since the backend started.</div>';
        return;
    }
    const n = v => (v == null ? '—' : v);
    el.innerHTML = `<div class="card" style="padding:0;overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:.82em;">
        <thead><tr style="text-align:left;color:var(--text-muted);font-size:.86em;">
            ${['when', 'direction', 'transfer', 'on the wire', 'freeze', 'stall', 'frames lost', 'status'].map(h => `<th style="padding:8px 10px;border-bottom:1px solid var(--border);">${h}</th>`).join('')}
        </tr></thead><tbody>
        ${migs.map(m => {
            const st = m.failed ? '<span style="color:var(--danger);">failed</span>' : m.completed ? '<span style="color:#2e9e4f;">completed</span>' : `<span style="color:#9085e9;">${escHtml(m.phase || 'running')}</span>`;
            const tr = m.transfer ? `${m.transfer}${m.image_mib ? ` <span style="color:var(--text-muted);">(${m.image_mib} MiB image)</span>` : ''}` : '—';
            return `<tr style="border-bottom:1px solid var(--border);" title="${escHtml(m.error || '')}">
                <td style="padding:7px 10px;font-family:var(--mono);">${m.started_at ? new Date(m.started_at * 1000).toLocaleTimeString() : '—'}</td>
                <td style="padding:7px 10px;">${m.from} → ${m.to}</td>
                <td style="padding:7px 10px;">${tr}</td>
                <td style="padding:7px 10px;font-family:var(--mono);">${m.wire_mib != null ? m.wire_mib + ' MiB' : '—'}</td>
                <td style="padding:7px 10px;font-family:var(--mono);">${m.freeze_s != null ? m.freeze_s + ' s' : '—'}</td>
                <td style="padding:7px 10px;font-family:var(--mono);">${m.stall_s != null ? m.stall_s.toFixed(1) + ' s' : '—'}</td>
                <td style="padding:7px 10px;font-family:var(--mono);">${n(m.frames_lost)}</td>
                <td style="padding:7px 10px;">${st}</td></tr>`;
        }).join('')}
        </tbody></table></div>`;
}
