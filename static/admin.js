/**
 * Artwork Admin Console - Client Logic (admin.js)
 * Phase 3 Refactor: Many-to-Many Playlists and Centralized Library.
 * Enhanced with automatic background polling for live updates.
 */

const API_BASE = (window.location.origin === 'null' || window.location.protocol === 'file:') 
    ? 'http://localhost:8000' 
    : window.location.origin;

let currentPlaylistId = null;
let currentPlaylists = [];
let fullLibrary = [];
let discoveryQueue = [];
let currentView = 'playlists';
let pollInterval = null;
let sortableInstance = null;
let currentSessionId = null;
// Set only during page load when a catalog collection was open at refresh time; renderCatalog()
// consumes it once (re-opening that collection instead of drawing the grid) then clears it.
let pendingCatalogRestore = null;

// Serialization Queue for API Actions
let actionQueue = [];
let isProcessingQueue = false;

function enqueueAction(actionFn) {
    actionQueue.push(actionFn);
    processQueue();
}

async function processQueue() {
    if (isProcessingQueue || actionQueue.length === 0) return;
    isProcessingQueue = true;
    while (actionQueue.length > 0) {
        const action = actionQueue.shift();
        try {
            await action();
        } catch (e) {
            console.error("[Admin] Queue action failed:", e);
        }
    }
    isProcessingQueue = false;
    await refreshData();
}

async function init() {
    setupUploadZone();
    setupPlaylistInput(); // Add key listener
    initServerAddress();  // show the address to point displays/Pi/e-ink/Frame at
    initDevicesCapability(); // un-hide the Devices tab only on an all-in-one appliance
    initPublisherCapability(); // un-hide the Publisher tab only once an identity exists
    loadSubscriptions();  // federated collections panel
    await loadPremiumSettings();
    await handleOAuthCallback();   // catch an OpenRouter OAuth redirect (?code=…)
    await loadAiSettings();
    loadFrameSettings();   // non-blocking: populate the Frame TV panel
    loadCatalogSource();   // non-blocking: populate the Catalog Source panel
    loadDefaultPlaylist(); // non-blocking: populate the Default Playlist panel
    loadNightSchedule();   // non-blocking: populate the Night & Quiet Hours panel
    loadCatalogCount();    // non-blocking: show the Museum Art count without opening the view
    loadPhotosCount();     // non-blocking: show the My Photos count

    // Restore the view the user was last on (survives a browser refresh).
    let savedView = (() => { try { return localStorage.getItem('sd_admin_view'); } catch (e) { return null; } })();
    // Migrate the old split Discover/Browse-Catalog views to the merged Museum Art view.
    if (savedView === 'catalog' || savedView === 'discover') savedView = 'museum';
    // Restore the catalog collection that was open, so refresh returns to it (not the collections
    // grid). Must be read before switchView() below — switching to 'museum' synchronously renders
    // the grid via enterMuseum(), and renderCatalog() consumes this to redirect into the collection.
    try { pendingCatalogRestore = localStorage.getItem('sd_admin_catalog_collection'); } catch (e) { pendingCatalogRestore = null; }
    const validViews = ['playlists', 'library', 'review', 'museum', 'devices', 'publisher', 'settings'];
    if (savedView && validViews.includes(savedView)) {
        switchView(savedView);
    }
    // Deep-link: /admin?view=publisher (from the /publisher redirect or the Help link) opens that view
    // even before its capability-gated nav button is visible. Strip the param after applying.
    try {
        const qp = new URLSearchParams(window.location.search).get('view');
        if (qp && validViews.includes(qp)) {
            switchView(qp);
            const u = new URL(window.location); u.searchParams.delete('view'); history.replaceState({}, '', u);
        }
    } catch (e) {}

    // Restore the collection that was open, so refresh returns to it (not the first one).
    // fetchPlaylists() (inside refreshData) reads currentPlaylistId to re-select it.
    const savedPlaylist = (() => { try { return localStorage.getItem('sd_admin_playlist'); } catch (e) { return null; } })();
    if (savedPlaylist && !isNaN(parseInt(savedPlaylist, 10))) {
        currentPlaylistId = parseInt(savedPlaylist, 10);
    }

    await refreshData();

    // Start background polling every 5 seconds for live updates
    startPolling();
}

/**
 * Handles Enter key on playlist input.
 */
function setupPlaylistInput() {
    const input = document.getElementById('new-playlist-name');
    input.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
            createPlaylist();
        }
    });
}

/**
 * Creates a new playlist via the API.
 */
async function createPlaylist() {
    const input = document.getElementById('new-playlist-name');
    const name = input.value.trim();
    if (!name) return;

    try {
        const fd = new FormData();
        fd.append('name', name);
        const res = await fetch(`${API_BASE}/playlists`, { method: 'POST', body: fd });
        if (res.ok) {
            input.value = '';
            await refreshData();
        } else {
            const err = await res.json();
            showToast(`Error: ${err.detail}`, 'error');
        }
    } catch (error) {
        console.error('[Admin] Playlist creation failed:', error);
    }
}

/**
 * Periodically refreshes data to reflect background AI processing or uploads.
 */
function startPolling() {
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(async () => {
        // Only refresh if no modal is open to avoid disrupting user interaction
        const isModalOpen = document.getElementById('library-modal').style.display === 'flex' ||
                           document.getElementById('edit-overlay').classList.contains('open');

        if (!isModalOpen) {
            await refreshData();
            if (currentView === 'devices') refreshHostHealth();
        }
    }, 5000);
}

async function refreshData() {
    // We fetch in parallel for efficiency
    await Promise.all([
        fetchPlaylists(),
        fetchLibrary(),
        fetchReviewQueue(),
        fetchDiscoveryQueue()
    ]);
}

function switchView(view) {
    if (gridSelectMode) exitSelectMode();   // leaving a grid cancels any multi-selection
    currentView = view;
    // Remember the active view so a browser refresh returns here instead of
    // snapping back to the default Collections screen.
    try { localStorage.setItem('sd_admin_view', view); } catch (e) {}
    document.getElementById('nav-playlists').classList.toggle('active', view === 'playlists');
    document.getElementById('nav-library').classList.toggle('active', view === 'library');
    document.getElementById('nav-review').classList.toggle('active', view === 'review');
    document.getElementById('nav-museum').classList.toggle('active', view === 'museum');
    document.getElementById('nav-devices').classList.toggle('active', view === 'devices');
    document.getElementById('nav-publisher').classList.toggle('active', view === 'publisher');
    document.getElementById('nav-settings').classList.toggle('active', view === 'settings');

    document.getElementById('view-playlists').classList.toggle('hidden', view !== 'playlists');
    document.getElementById('view-library').classList.toggle('hidden', view !== 'library');
    document.getElementById('view-review').classList.toggle('hidden', view !== 'review');
    document.getElementById('view-museum').classList.toggle('hidden', view !== 'museum');
    document.getElementById('view-devices').classList.toggle('hidden', view !== 'devices');
    document.getElementById('view-publisher').classList.toggle('hidden', view !== 'publisher');
    document.getElementById('view-settings').classList.toggle('hidden', view !== 'settings');

    document.getElementById('sidebar-playlists').classList.toggle('hidden', view !== 'playlists');

    // On mobile, picking a view closes the slide-in drawer.
    document.body.classList.remove('sidebar-open');

    if (view === 'museum') enterMuseum();
    if (view === 'devices') enterDevices();
    if (view === 'publisher') enterPublisher();
}

// Mobile-only: toggle the slide-in sidebar drawer (no-op visual on desktop).
function toggleSidebar() {
    document.body.classList.toggle('sidebar-open');
}
window.toggleSidebar = toggleSidebar;

// Transient, themed feedback. type: '' | 'success' | 'error'. Replaces native alert().
function showToast(message, type = '') {
    let c = document.getElementById('toast-container');
    if (!c) { c = document.createElement('div'); c.id = 'toast-container'; document.body.appendChild(c); }
    const t = document.createElement('div');
    t.className = 'toast' + (type ? ' ' + type : '');
    t.textContent = message;
    c.appendChild(t);
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 2600);
}
window.showToast = showToast;

// Themed modal replacing native confirm()/prompt(). confirmModal -> Promise<bool>;
// promptModal -> Promise<string|null> (null = cancelled). Both are async/awaitable.
function _buildModal({ message, input, placeholder = '', confirmText = 'OK', cancelText = 'Cancel', danger = false }) {
    return new Promise((resolve) => {
        const overlay = document.createElement('div');
        overlay.id = 'modal-overlay';
        const box = document.createElement('div');
        box.className = 'modal-box';
        const msg = document.createElement('p');
        msg.className = 'modal-msg';
        msg.textContent = message;
        box.appendChild(msg);
        let field = null;
        if (input) {
            field = document.createElement('input');
            field.placeholder = placeholder;
            box.appendChild(field);
        }
        const actions = document.createElement('div');
        actions.className = 'modal-actions';
        const cancel = document.createElement('button');
        cancel.textContent = cancelText;
        const ok = document.createElement('button');
        ok.className = 'btn-confirm' + (danger ? ' danger' : '');
        ok.textContent = confirmText;
        actions.append(cancel, ok);
        box.appendChild(actions);
        overlay.appendChild(box);
        document.body.appendChild(overlay);
        (field || ok).focus();
        const close = (val) => { overlay.remove(); resolve(val); };
        cancel.onclick = () => close(input ? null : false);
        ok.onclick = () => close(input ? (field ? field.value : '') : true);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(input ? null : false); });
        document.addEventListener('keydown', function esc(e) {
            if (e.key === 'Escape') { document.removeEventListener('keydown', esc); close(input ? null : false); }
        });
        if (field) field.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok.click(); });
    });
}
function confirmModal(message, opts = {}) { return _buildModal({ message, input: false, ...opts }); }
function promptModal(message, opts = {}) { return _buildModal({ message, input: true, ...opts }); }
window.confirmModal = confirmModal;
window.promptModal = promptModal;

// Trim a raw ISO timestamp (e.g. 2017-12-08T00:00:00Z) to a readable date for the editable field.
function _fmtDate(v) {
    if (!v) return '';
    return String(v).replace(/T\d{2}:\d{2}:\d{2}\S*$/, '');
}

// Surface the address other devices should use to reach this server. We echo the
// origin the admin was actually reached on (so LAN-IP / <host>.local just work), and
// warn when it's localhost (which other devices can't reach). This avoids reporting a
// bogus container IP — Docker bridging makes a server-side LAN-IP lookup unreliable.
function initServerAddress() {
    const el = document.getElementById('server-address');
    if (!el) return;
    el.textContent = window.location.origin;
    const host = window.location.hostname;
    if (host === 'localhost' || host === '127.0.0.1') {
        const note = document.getElementById('server-address-note');
        if (note) {
            note.style.display = 'block';
            note.innerHTML = "You're viewing this on the server itself, so this shows <code>localhost</code> — " +
                "other devices can't reach that. Point them at this machine's LAN address instead: find it with " +
                "<code>hostname -I</code>, or open this admin from another device via " +
                "<code>http://&lt;this-hostname&gt;.local:8000/admin</code> and this address will update to match.";
        }
    }
}

function copyServerAddress() {
    const txt = document.getElementById('server-address').textContent;
    if (navigator.clipboard) navigator.clipboard.writeText(txt);
    const c = document.getElementById('server-address-copied');
    if (c) { c.style.display = 'inline'; setTimeout(() => { c.style.display = 'none'; }, 1500); }
}
window.copyServerAddress = copyServerAddress;

// --- Devices (all-in-one appliance only) ------------------------------------
// GET /api/health/host is 404 unless SD_APPLIANCE_MODE=all-in-one, so a successful
// fetch is exactly the signal that this box is a manageable appliance. On a generic
// server or a thin client the tab stays hidden.
async function initDevicesCapability() {
    const btn = document.getElementById('nav-devices');
    if (!btn) return;
    try {
        const res = await fetch(`${API_BASE}/api/health/host`);
        if (res.ok) btn.style.display = '';
    } catch (e) { /* not an appliance — leave hidden */ }
}

function enterDevices() {
    refreshHostHealth();
}

function _fmtUptime(s) {
    if (s == null) return '—';
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
}

function _metricTile(label, value, warn) {
    const color = warn ? '#fbbf24' : 'var(--success-color)';
    return `<div style="background:#0f172a; border:1px solid var(--border-color); border-radius:8px; padding:12px 14px;">
        <div style="font-size:0.68rem; color:#94a3b8; text-transform:uppercase; letter-spacing:0.05rem;">${label}</div>
        <div style="font-size:1.05rem; color:${color}; margin-top:4px;">${value}</div>
    </div>`;
}

function _renderHostHealth(host) {
    const grid = document.getElementById('host-health-grid');
    if (!grid) return;
    const load = host.loadavg ? host.loadavg.map(n => n.toFixed(2)).join(' ') : '—';
    const temp = host.temp_c != null ? `${host.temp_c} °C` : '—';
    const mem = host.memory ? `${host.memory.used_pct}% of ${host.memory.total_mb} MB` : '—';
    const disk = host.disk ? `${host.disk.free_gb} GB free (${host.disk.used_pct}% used)` : '—';
    const uptime = _fmtUptime(host.uptime_s);

    let throttle, throttleWarn = false;
    if (host.throttled === 'unavailable' || host.throttled == null) {
        throttle = 'unavailable';
    } else {
        const t = host.throttled;
        if (t.active && t.active.length) { throttle = '⚠ ' + t.active.join(', '); throttleWarn = true; }
        else if (t.occurred && t.occurred.length) { throttle = 'OK (since boot: ' + t.occurred.join(', ') + ')'; throttleWarn = true; }
        else { throttle = 'OK'; }
    }

    // Self-heal (watchdog): show mode + health + any action. Warn if it's actually acting (enforce +
    // a real action) or currently seeing the box as unhealthy.
    let selfheal = null, selfhealWarn = false;
    const wd = host.watchdog;
    if (wd) {
        const acting = wd.action && wd.action !== 'none';
        const unhealthy = wd.server_ok === 0 || wd.kiosk_ok === 0;
        selfheal = `${wd.mode}${acting ? ' · ' + wd.action : (unhealthy ? ' · watching' : ' · healthy')}`;
        selfhealWarn = unhealthy || (acting && !String(wd.action).startsWith('observe'));
    }

    grid.innerHTML =
        _metricTile('CPU Load', load) +
        _metricTile('Temperature', temp, host.temp_c != null && host.temp_c >= 75) +
        _metricTile('Memory', mem, host.memory && host.memory.used_pct >= 90) +
        _metricTile('Disk', disk, host.disk && host.disk.used_pct >= 90) +
        _metricTile('Uptime', uptime) +
        _metricTile('Power / Throttle', throttle, throttleWarn) +
        (selfheal ? _metricTile('Self-heal', selfheal, selfhealWarn) : '');
}

function _renderActiveDisplays(displays) {
    const el = document.getElementById('active-displays-list');
    if (!el) return;
    if (!displays || !displays.length) {
        el.innerHTML = '<span style="color:#94a3b8;">No displays connected in the last 15s.</span>';
        return;
    }
    el.innerHTML = displays.map(d => {
        const art = d.artwork;
        const thumb = art
            ? `<img src="${_esc(art.thumb_url)}" alt="" style="width:44px;height:44px;border-radius:6px;object-fit:cover;background:#1e293b;flex:0 0 auto;">`
            : `<span style="width:44px;height:44px;border-radius:6px;background:#1e293b;flex:0 0 auto;"></span>`;
        // Museum/user data — escape title, artist, and collection name.
        const artist = (art && !art.is_personal && art.agent_name) ? ' — ' + _esc(art.agent_name) : '';
        const nowShowing = art
            ? `<div style="color:var(--text-color);font-size:0.9rem;">▶ ${_esc(art.title || 'Untitled')}${artist}</div>`
            : `<div style="color:#94a3b8;font-size:0.9rem;">Idle — nothing showing yet</div>`;
        const coll = d.playlist ? `<div style="color:#94a3b8;font-size:0.75rem;">in “${_esc(d.playlist)}” collection</div>` : '';
        return `<div style="display:flex; align-items:center; gap:10px; padding:8px 0; border-bottom:1px solid var(--border-color);">
            ${thumb}
            <div style="min-width:0;">
                <div style="display:flex; align-items:center; gap:6px;">
                    <span style="color:var(--success-color);">●</span>
                    <code style="color:var(--text-color);">${_esc(d.display_id)}</code>
                </div>
                ${nowShowing}${coll}
            </div>
        </div>`;
    }).join('');
}

async function refreshHostHealth() {
    try {
        const res = await fetch(`${API_BASE}/api/health/host`);
        if (!res.ok) return;
        const data = await res.json();
        _renderHostHealth(data.host || {});
        _renderActiveDisplays(data.displays || []);
        const stamp = document.getElementById('device-health-updated');
        if (stamp) stamp.textContent = '· updated ' + new Date().toLocaleTimeString();
    } catch (e) { /* transient — next poll retries */ }
}

// --- Appliance maintenance: GUI-triggered host updates ----------------------
let _maintPoll = null;
const _MAINT_BTNS = ['maint-update-app', 'maint-update-scripts', 'maint-reboot'];
const _MAINT_PROMPTS = {
    'update-app': 'Update the app now? This pulls the latest from origin/main and rebuilds — the display drops briefly while the container restarts.',
    'update-scripts': 'Re-run the appliance installer to refresh the kiosk scripts and services?',
    'reboot': 'Reboot this device now?',
};

function _maintButtons(disabled) {
    _MAINT_BTNS.forEach(id => { const b = document.getElementById(id); if (b) b.disabled = disabled; });
}

async function applianceAction(action) {
    const ok = await confirmModal(_MAINT_PROMPTS[action] || `Run ${action}?`, {
        confirmText: action === 'reboot' ? 'Reboot' : 'Proceed',
        danger: action === 'reboot' || action === 'update-app',
    });
    if (!ok) return;
    const statusEl = document.getElementById('maint-status');
    statusEl.style.display = 'block';
    statusEl.textContent = '⏳ Queued…';
    _maintButtons(true);
    try {
        const res = await fetch(`${API_BASE}/api/appliance/update`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action }),
        });
        if (!res.ok) {
            const e = await res.json().catch(() => ({}));
            statusEl.textContent = '✗ ' + (e.detail || 'request failed');
            _maintButtons(false);
            return;
        }
        _pollMaint();
    } catch (e) {
        statusEl.textContent = '✗ ' + e.message;
        _maintButtons(false);
    }
}
window.applianceAction = applianceAction;

function _pollMaint() {
    if (_maintPoll) clearInterval(_maintPoll);
    _maintPoll = setInterval(async () => {
        let data;
        try {
            const res = await fetch(`${API_BASE}/api/appliance/update/status`);
            if (!res.ok) return;   // mid-update the container may be restarting — keep polling
            data = await res.json();
        } catch (e) { return; }    // transient (rebuild/reboot drops the server) — keep polling
        const statusEl = document.getElementById('maint-status');
        const logEl = document.getElementById('maint-log');
        const icon = { queued: '⏳', running: '⏳', done: '✓', error: '✗' }[data.state] || '';
        statusEl.textContent = `${icon} ${data.state}${data.message ? ' — ' + data.message : ''}`;
        if (data.log_tail && data.log_tail.length) {
            logEl.style.display = 'block';
            logEl.textContent = data.log_tail.join('\n');
        }
        if (['done', 'error', 'idle'].includes(data.state)) {
            clearInterval(_maintPoll); _maintPoll = null;
            _maintButtons(false);
        }
    }, 2500);
}

// --- Federation: subscriptions + trust badges -------------------------------
const _TRUST = {
    bundled:   { label: 'Official',  color: '#3b82f6' },
    verified:  { label: 'Verified',  color: '#10b981' },
    community: { label: 'Community', color: '#94a3b8' },
};

// Origin/trust badge for a collection (or a subscription row).
function trustBadge(origin, trust) {
    const key = origin === 'bundled' ? 'bundled' : (trust === 'verified' ? 'verified' : 'community');
    const b = _TRUST[key];
    return `<span class="trust-badge" style="font-size:0.62rem; padding:2px 8px; border-radius:10px; border:1px solid ${b.color}; color:${b.color}; white-space:nowrap;">${b.label}</span>`;
}

async function loadSubscriptions() {
    const list = document.getElementById('subscriptions-list');
    if (!list) return;
    try {
        const subs = await (await fetch(`${API_BASE}/api/subscriptions`)).json();
        if (!subs.length) {
            list.innerHTML = '<p style="font-size:0.8rem; color:#64748b;">No subscriptions yet.</p>';
            return;
        }
        list.innerHTML = subs.map(s => {
            const pub = (s.publisher && s.publisher.name) || 'Unknown publisher';
            const err = s.last_status && s.last_status !== 'ok'
                ? ` · <span style="color:#ef4444;">${_esc(s.last_status)}</span>` : '';
            return `<div style="border:1px solid var(--border-color); border-radius:8px; padding:12px; background:#0f172a;">
                <div style="display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap;">
                    <div>
                        <strong style="font-size:0.85rem;">${_esc(s.title || s.url)}</strong> ${trustBadge('subscription', s.trust)}<br>
                        <small style="color:#94a3b8;">${_esc(pub)} · ${s.item_count} works${err}</small>
                    </div>
                    <div style="display:flex; gap:8px;">
                        <button class="secondary" onclick="syncSubscription(${s.id})" style="padding:6px 12px; font-size:0.75rem;">Sync</button>
                        <button class="secondary" onclick="removeSubscription(${s.id}, '${_esc((s.title || s.url).replace(/'/g, ''))}')" style="padding:6px 12px; font-size:0.75rem; border-color:#ef4444; color:#ef4444;">Remove</button>
                    </div>
                </div>
            </div>`;
        }).join('');
    } catch (e) { console.error('[Admin] loadSubscriptions failed:', e); }
}

async function addSubscription() {
    const input = document.getElementById('subscription-url');
    const url = (input.value || '').trim();
    if (!url) { showToast('Enter a manifest URL first.', 'error'); return; }
    try {
        const res = await fetch(`${API_BASE}/api/subscriptions`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
        const data = await res.json();
        if (!res.ok) { showToast(data.detail || 'Could not subscribe.', 'error'); return; }
        input.value = '';
        showToast(`Subscribed to ${data.title || 'collection'} ✓`, 'success');
        loadSubscriptions();
        if (currentView === 'museum') enterMuseum();
    } catch (e) { showToast('Network error subscribing.', 'error'); }
}

async function syncSubscription(id) {
    try {
        const res = await fetch(`${API_BASE}/api/subscriptions/${id}/sync`, { method: 'POST' });
        const data = await res.json();
        const ok = data.last_status === 'ok';
        showToast(ok ? 'Synced ✓' : (data.last_status || 'Sync failed'), ok ? 'success' : 'error');
        loadSubscriptions();
    } catch (e) { showToast('Network error syncing.', 'error'); }
}

async function removeSubscription(id, name) {
    if (!(await confirmModal(`Unsubscribe from "${name}"? Already-added artworks stay in your library.`,
        { confirmText: 'Unsubscribe', danger: true }))) return;
    try {
        await fetch(`${API_BASE}/api/subscriptions/${id}`, { method: 'DELETE' });
        showToast('Unsubscribed.', 'success');
        loadSubscriptions();
        if (currentView === 'museum') enterMuseum();
    } catch (e) { showToast('Network error.', 'error'); }
}
window.addSubscription = addSubscription;
window.syncSubscription = syncSubscription;
window.removeSubscription = removeSubscription;

async function fetchLibrary() {
    try {
        const response = await fetch(`${API_BASE}/artworks`);
        const data = await response.json();

        // Re-render when the data actually changed — not just when the COUNT changed. The old
        // count-only guard meant an in-place metadata edit (same count) never repainted the grid, so a
        // saved edit looked lost (UX-A1). Diff the payload so edits re-render but idle polls don't churn.
        const newStr = JSON.stringify(data);
        if (window._lastLibraryJSON === newStr) return;
        window._lastLibraryJSON = newStr;
        fullLibrary = data;
        document.getElementById('library-count').textContent = fullLibrary.length;
        renderLibraryGrid();
    } catch (error) { console.error('[Admin] Fetch library failed:', error); }
}

async function fetchPlaylists() {
    try {
        const response = await fetch(`${API_BASE}/playlists`);
        const data = await response.json();
        
        // Comprehensive optimization check to prevent 5-second blinking/wiping of the grid.
        // It checks the stringified deeply-nested data to ensure ANY external artwork addition triggers a fresh render.
        const newDataStr = JSON.stringify(data);
        if (window._lastPlaylistsJSON === newDataStr) return;
        window._lastPlaylistsJSON = newDataStr;
        
        // Check if any playlist input is currently focused to avoid overwriting user edits
        const focusedEl = document.activeElement;
        const isEditingSidebar = focusedEl && focusedEl.tagName === 'INPUT' && focusedEl.closest('.playlist-item');

        currentPlaylists = data;
        document.getElementById('playlist-count').textContent = currentPlaylists.length;
        
        if (!isEditingSidebar) {
            renderSidebar();
        }
        
        if (currentPlaylistId) {
            const active = currentPlaylists.find(p => p.id === currentPlaylistId);
            if (active) {
                selectPlaylist(active.id);
            } else if (currentPlaylists.length > 0) {
                // Saved collection no longer exists (e.g. deleted) — fall back to the first.
                selectPlaylist(currentPlaylists[0].id);
            }
        } else if (currentPlaylists.length > 0) {
            selectPlaylist(currentPlaylists[0].id);
        }
    } catch (error) { console.error('[Admin] Fetch playlists failed:', error); }
}

async function fetchReviewQueue() {
    try {
        const response = await fetch(`${API_BASE}/artworks/pending`);
        const data = await response.json();
        
        // Always update count
        document.getElementById('review-count').textContent = data.length;

        // Re-render whenever the server data changes in ANY way — not just when the
        // count changes. Freshly-uploaded cards arrive blank and get their AI metadata
        // filled in by background enrichment a few seconds later; that's a content
        // change with no count change, so a length-only guard would leave the cards
        // blank until a manual page reload. renderReviewQueue reconciles by id and
        // preserves any field the user is actively editing.
        const dataStr = JSON.stringify(data);
        if (dataStr !== window._lastReviewJSON) {
            window._lastReviewJSON = dataStr;
            renderReviewQueue(data);
        }
    } catch (error) { console.error('[Admin] Fetch queue failed:', error); }
}

async function fetchDiscoveryQueue() {
    try {
        const response = await fetch(`${API_BASE}/api/discover/queue`);
        const data = await response.json();
        // The standalone Discover-count badge folded into Museum Art; update it only if present.
        const dc = document.getElementById('discover-count');
        if (dc) dc.textContent = data.length;
        // Drop items currently expanded inline so a poll can't re-add a card for one we've consumed.
        const filtered = data.filter(it => !inlineDiscoveryIds.has(it.id));
        if (filtered.length !== discoveryQueue.length) {
            discoveryQueue = filtered;
            renderDiscoveryGrid();
        }
    } catch (error) { console.error('[Admin] Fetch discovery failed:', error); }
}

// proposed_title/proposed_artist/source_api come verbatim from external museum-API JSON — every field
// is escaped (H2: a crafted title like `"><img src=x onerror=...>` would otherwise execute in the
// unauth admin the moment a scout returns it).
function discoveryCardHTML(item) {
    const thumbUrl = item.thumbnail_url + (item.thumbnail_url.includes('?') ? '&' : '?') + '_cb=' + encodeURIComponent(item.source_url);
    return `
                <img src="${_esc(thumbUrl)}" alt="${_esc(item.proposed_title)}">
                <div class="info">
                    <strong>${_esc(item.proposed_title)}</strong><br>
                    <small>${_esc(item.proposed_artist)}</small><br>
                    <small style="opacity:0.6">${_esc(item.source_api)}</small>
                </div>
                <div class="actions" style="grid-template-columns: 1fr 1fr;">
                    <button onclick="reviewDiscoveryInline(${item.id}, this)" class="success" title="Finalize &amp; publish right here — no tab hop">Review</button>
                    <button onclick="rejectDiscovery(${item.id}, this)" style="color: #ef4444;">Reject</button>
                </div>`;
}

function renderDiscoveryGrid() {
    // A card mid inline-review has had its dataset.id removed and its item dropped from discoveryQueue,
    // so it is invisible to reconcile (never matched, never pruned) and its form is left intact.
    reconcileGrid(document.getElementById('discover-grid'), discoveryQueue, it => it.id,
        'artwork-card', discoveryCardHTML);
}

async function dispatchScouts() {
    const searchInput = document.getElementById('scout-search');
    const query = searchInput.value.trim();
    const btn = document.getElementById('deploy-scout-btn');
    const statusArea = document.getElementById('scout-status');
    const statusText = document.getElementById('scout-status-text');
    const limitSelect = document.getElementById('scout-limit');
    const limit = parseInt(limitSelect.value) || 10;
    
    // Get selected sources
    const selectedSources = Array.from(document.querySelectorAll('input[name="scout-source"]:checked'))
                                 .map(cb => cb.value);
    
    if (selectedSources.length === 0) return showToast("Please select at least one source.", 'error');

    // UI Feedback
    btn.disabled = true;
    statusArea.style.display = 'block';
    statusText.textContent = `Scout is hunting in ${selectedSources.length} museum archives...`;

    try {
        const response = await fetch(`${API_BASE}/api/discover/dispatch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                search: query,
                sources: selectedSources,
                limit: limit
            })
        });
        
        if (response.ok) {
            const data = await response.json();
            currentSessionId = data.session_id;
            
            // Show classification feedback
            const intentType = data.intent?.type || 'freetext';
            const intentName = data.intent?.canonical || query;
            const intentLabel = intentType === 'artist' ? `🎨 Artist: ${intentName}`
                              : intentType === 'genre' ? `🖼️ Genre: ${intentName}`
                              : intentType === 'subject' ? `🔍 Subject: ${intentName}`
                              : `📝 Search: ${intentName}`;
            
            statusText.textContent = `Scout deployed! Classified as ${intentLabel}. Results incoming...`;
            
            // Show Load More button
            document.getElementById('load-more-btn').style.display = 'block';
            
            // Don't clear input — user might want to tweak and re-search
            setTimeout(() => {
                statusArea.style.display = 'none';
                btn.disabled = false;
            }, 4000);
            await refreshData();
        } else {
            throw new Error("Dispatch failed");
        }
    } catch (error) { 
        console.error('[Admin] Scout dispatch failed:', error); 
        statusText.textContent = "Scout lost contact with the museums. Try again.";
        setTimeout(() => {
            statusArea.style.display = 'none';
            btn.disabled = false;
        }, 5000);
    }
}

async function loadMoreDiscoveries() {
    if (!currentSessionId) {
        showToast('No active search session. Please run a new search first.', 'error');
        return;
    }
    
    const btn = document.getElementById('load-more-btn');
    const statusArea = document.getElementById('scout-status');
    const statusText = document.getElementById('scout-status-text');
    
    btn.disabled = true;
    btn.textContent = 'Loading...';
    statusArea.style.display = 'block';
    statusText.textContent = 'Fetching next batch of results...';
    
    try {
        const response = await fetch(`${API_BASE}/api/discover/more`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: currentSessionId })
        });
        
        if (response.ok) {
            statusText.textContent = 'More masterpieces loaded!';
            setTimeout(() => {
                statusArea.style.display = 'none';
            }, 3000);
            // Wait a moment for background task to complete, then refresh
            setTimeout(async () => {
                await refreshData();
            }, 2000);
        } else {
            const err = await response.json();
            statusText.textContent = err.detail || 'Failed to load more results.';
            currentSessionId = null;
            document.getElementById('load-more-btn').style.display = 'none';
        }
    } catch (error) {
        console.error('[Admin] Load more failed:', error);
        statusText.textContent = 'Failed to load more. Try a new search.';
    } finally {
        btn.disabled = false;
        btn.textContent = 'Load More Results';
    }
}

function rejectDiscovery(id, btn) {
    btn.disabled = true;
    btn.textContent = "Queued...";
    btn.style.opacity = "0.7";
    enqueueAction(async () => {
        await fetch(`${API_BASE}/api/discover/reject/${id}`, { method: 'POST' });
    });
}

// ===== Inline review (Inc 4): finalize a live find without leaving Museum Art =====
// "Review" creates the artwork (which kicks off RAG enrichment) and expands the discovery card in
// place into the review form — Approve/Delete happen right here, no Review-Queue tab hop. The new
// artwork is shielded from both polls: its discovery id from fetchDiscoveryQueue (inlineDiscoveryIds)
// and its artwork id from the Review Queue (inlineReviewing — which instead streams enrichment into
// the inline card via renderReviewQueue).
const inlineReviewing = new Set();     // artwork ids currently expanded inline
const inlineDiscoveryIds = new Set();  // discovery-queue ids consumed by an inline expansion

function reviewDiscoveryInline(discoveryId, btn) {
    const card = btn.closest('.artwork-card');
    const item = discoveryQueue.find(it => it.id === discoveryId) || {};
    btn.disabled = true; btn.textContent = 'Opening…';
    enqueueAction(async () => {
        let res, data = null;
        try {
            res = await fetch(`${API_BASE}/api/discover/approve/${discoveryId}`, { method: 'POST' });
            data = await res.json();
        } catch (e) { res = null; }
        if (!res || !res.ok || !data || !data.artwork_id) {
            showToast("Couldn't fetch that artwork — the museum server may be busy.", 'error');
            btn.disabled = false; btn.textContent = 'Review';
            return;
        }
        const artId = data.artwork_id;
        inlineReviewing.add(artId);
        inlineDiscoveryIds.add(discoveryId);
        discoveryQueue = discoveryQueue.filter(it => it.id !== discoveryId);
        expandDiscoveryCard(card, discoveryId, artId, {
            id: artId, filename: '', title: item.proposed_title || '', agent_name: item.proposed_artist || '',
        });
    });
}

function expandDiscoveryCard(card, discoveryId, artId, seed) {
    delete card.dataset.id;                 // hide from renderDiscoveryGrid reconciliation
    card.dataset.inlineArt = artId;
    card.dataset.discId = discoveryId;
    card.className = 'review-card inline-review';
    card.style.gridColumn = '1 / -1';       // span the whole grid row
    card.innerHTML = `
        <div class="inline-review-spinner">✨ Writing the placard… you can edit as it lands.</div>
        <div class="inline-review-body">${reviewFormHTML(seed)}</div>`;
    // Treat the seeded proposed values as the server baseline, so enrichment cleanly replaces them
    // (unless the user edits first) — see syncReviewCardFields' data-server logic.
    REVIEW_FIELDS.forEach(([prefix]) => {
        const el = card.querySelector(`#${prefix}-${artId}`);
        if (el) el.dataset.server = el.value;
    });
    // Re-target the two actions so they also collapse the inline card.
    const actions = card.querySelector('.review-actions');
    const approveBtn = actions.querySelector('.success');
    const deleteBtn = actions.querySelector('.secondary');
    approveBtn.setAttribute('onclick', ''); approveBtn.onclick = () => approveInline(artId, card);
    deleteBtn.setAttribute('onclick', '');  deleteBtn.onclick = () => rejectInline(artId, card);
}

function _collapseInline(card) {
    inlineReviewing.delete(parseInt(card.dataset.inlineArt, 10));
    inlineDiscoveryIds.delete(parseInt(card.dataset.discId, 10));
    card.remove();
}

function approveInline(artId, card) {
    approveArtwork(artId);   // reads the inline fields synchronously, then PATCHes approve in the background
    _collapseInline(card);
    showToast('Published ✓', 'success');
}

async function rejectInline(artId, card) {
    if (!(await confirmModal('Discard this find? It removes the downloaded artwork.', { confirmText: 'Discard', danger: true }))) return;
    _collapseInline(card);
    try { await fetch(`${API_BASE}/artworks/${artId}`, { method: 'DELETE' }); }
    catch (e) { console.error('[Admin] inline discard failed:', e); }
}

async function clearRejectedHistory() {
    if (!(await confirmModal('Clear your rejected history? Scouts will be able to recommend previously denied artwork again.', { confirmText: 'Clear history' }))) return;

    enqueueAction(async () => {
        try {
            await fetch(`${API_BASE}/api/discover/history`, { method: 'DELETE' });
            showToast('Rejected history cleared — scouts will rediscover skipped artwork.', 'success');
        } catch (error) {
            console.error('[Admin] Clear history failed:', error);
            showToast('Failed to clear history. Check console.', 'error');
        }
    });
}

async function clearOrphanedHistory() {
    if (!(await confirmModal('Clear history for artworks you approved but later deleted? This allows scouts to recommend them again.', { confirmText: 'Clear' }))) return;

    enqueueAction(async () => {
        try {
            const res = await fetch(`${API_BASE}/api/discover/orphans`, { method: 'DELETE' });
            const data = await res.json();
            showToast(data.status + '. Scouts will now rediscover them.', 'success');
        } catch (error) {
            console.error('[Admin] Clear orphans failed:', error);
            showToast('Failed to clear orphaned history. Check console.', 'error');
        }
    });
}

async function clearPendingDiscoveries() {
    if (!(await confirmModal('Clear ALL pending discover items? This gives you a clean slate.', { confirmText: 'Clear pending', danger: true }))) return;

    enqueueAction(async () => {
        try {
            const res = await fetch(`${API_BASE}/api/discover/clear-pending`, { method: 'DELETE' });
            const data = await res.json();
            showToast(data.status, 'success');
            // Clear the discover grid UI
            document.getElementById('discover-grid').innerHTML = '';
            document.getElementById('load-more-btn').style.display = 'none';
            currentSessionId = null;
            loadDiscoverQueueThrottled();
        } catch (error) {
            console.error('[Admin] Clear pending failed:', error);
            showToast('Failed to clear pending items. Check console.', 'error');
        }
    });
}

async function factoryReset() {
    if (!(await confirmModal('⚠️ FACTORY RESET\n\nThis deletes ALL artwork except the original seed masterpieces, clears the entire discover queue, and removes playlist associations. This CANNOT be undone.', { confirmText: 'Continue', danger: true }))) return;

    const typed = await promptModal('Type RESET to confirm factory reset:', { placeholder: 'RESET', confirmText: 'Reset', danger: true });
    if (typed !== 'RESET') {
        if (typed !== null) showToast('Factory reset cancelled.');
        return;
    }

    enqueueAction(async () => {
        try {
            const res = await fetch(`${API_BASE}/api/admin/factory-reset`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ confirm: 'RESET' }),
            });
            const data = await res.json();
            showToast(`${data.status} — ${data.artworks_removed} removed, ${data.seed_artworks_preserved} seeds kept.`, 'success');
            // Full page reload to reflect the reset state
            setTimeout(() => window.location.reload(), 1200);
        } catch (error) {
            console.error('[Admin] Factory reset failed:', error);
            showToast('Factory reset failed. Check console.', 'error');
        }
    });
}

async function batchEnrich() {
    if (!aiConfigured) { nudgeConnectModel(); return; }
    if (!(await confirmModal('Run RAG enrichment on the entire approved library? This uses AI and takes time.', { confirmText: 'Run enrichment' }))) return;
    try {
        await fetch(`${API_BASE}/api/curate/batch-enrich`, { method: 'POST' });
        showToast('Batch enrichment started in the background.', 'success');
    } catch (error) { console.error('[Admin] Batch enrich failed:', error); }
}

async function reenrichArtwork(id) {
    if (!aiConfigured) { nudgeConnectModel(); return; }
    const hint = await promptModal('AI Guidance (optional):', { placeholder: 'e.g. focus on the historical context', confirmText: 'Re-enrich' });
    if (hint === null) return; // Cancelled

    try {
        await fetch(`${API_BASE}/api/curate/reenrich/${id}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hint: hint })
        });
        showToast('Artwork sent back to the Review Queue for re-enrichment.', 'success');
        await refreshData();
    } catch (error) { console.error('[Admin] Re-enrich failed:', error); }
}

// Card subtitle line. Personal photos have no artist — the caption IS the title, so the subtitle
// shows the date (or nothing), matching the jargon-free display placard (app.js is_personal branch).
// Museum works show the artist, falling back to "Unknown".
function cardSubtitle(art) {
    if (art.is_personal) return art.date_display || '';
    return art.agent_name || 'Unknown';
}

// Reconcile a list of id-keyed cards into `container` in place: prune removed cards, insert/reorder,
// and REPAINT matched cards so edited data actually shows (the old copies reused a matched card without
// ever rewriting it → "saved edit doesn't refresh the grid"). One helper replaces the four hand-rolled
// copies. `cardHTML(item)` returns the card's inner HTML — ALL untrusted text inside it MUST go through
// _esc() (these grids render external-museum metadata into the unauth admin → stored-XSS surface).
// opts.repaintOnReuse=false leaves a matched card's DOM alone (for cards with live <input>s the user may
// be editing — the caller patches those via opts.onCard instead). opts.onCard(card,item,isNew) runs for
// every card after placement.
function reconcileGrid(container, items, keyFn, cardClass, cardHTML, opts = {}) {
    const { repaintOnReuse = true, onCard = null } = opts;
    const ids = items.map(it => String(keyFn(it)));
    const newIds = new Set(ids);
    // Prune obsolete cards BEFORE indexing to prevent 'leapfrog' detaching on reorder.
    Array.from(container.children).forEach(card => {
        if (card.dataset.id && !newIds.has(card.dataset.id)) card.remove();
    });
    const existing = {};
    Array.from(container.children).forEach(card => {
        if (card.dataset.id) existing[card.dataset.id] = card;
    });
    items.forEach((item, i) => {
        const idStr = ids[i];
        let card = existing[idStr];
        const isNew = !card;
        if (card) {
            delete existing[idStr];
            if (repaintOnReuse) card.innerHTML = cardHTML(item);
            if (container.children[i] !== card) container.insertBefore(card, container.children[i]);
        } else {
            card = document.createElement('div');
            card.className = cardClass;
            card.dataset.id = idStr;
            card.innerHTML = cardHTML(item);
            if (i < container.children.length) container.insertBefore(card, container.children[i]);
            else container.appendChild(card);
        }
        if (onCard) onCard(card, item, isNew);
    });
}

function artworkCardHTML(art, view) {
    const removeBtn = view === 'collection'
        ? `<button onclick="removeArtworkFromPlaylist(${art.id})" title="Remove from this collection" aria-label="Remove from this collection" style="color: #f59e0b;">✕</button>`
        : `<button onclick="deleteArtworkPermanently(${art.id})" title="Delete from library" aria-label="Delete from library" style="color: #ef4444;">✕</button>`;
    return `
                <img src="${API_BASE}/artworks/${art.id}/thumbnail?f=${encodeURIComponent(art.filename)}" alt="${_esc(art.filename)}" onclick="openEdit(${art.id})" style="cursor: pointer;">
                <div class="info">
                    <strong>${_esc(art.title || art.filename)}</strong><br>
                    <small>${_esc(cardSubtitle(art))}</small>${art.is_seed ? '<br><span style="color: #10b981; font-weight: bold; font-size: 0.75rem;">🌱 Built-In</span>' : ''}
                </div>
                <div class="actions" style="grid-template-columns: 1fr auto;">
                    <button onclick="openEdit(${art.id}, '${view}')">Edit</button>
                    ${removeBtn}
                </div>`;
}

// A3: client-side filter over the already-loaded library (title/artist/tags/filename). Curation stops
// scaling past ~100 works without it; Museum Art already set the expectation that search exists.
let libraryFilter = '';

function _filteredLibrary() {
    const q = libraryFilter.trim().toLowerCase();
    if (!q) return fullLibrary;
    return fullLibrary.filter(a => (
        `${a.title || ''} ${a.agent_name || ''} ${a.tags || ''} ${a.filename || ''}`
    ).toLowerCase().includes(q));
}

function filterLibrary(q) { libraryFilter = q; renderLibraryGrid(); }

function renderLibraryGrid() {
    reconcileGrid(document.getElementById('library-grid'), _filteredLibrary(), a => a.id,
        'artwork-card', art => artworkCardHTML(art, 'library'));
}

function renderArtworkGrid(artworks) {
    reconcileGrid(document.getElementById('artwork-grid'), artworks, a => a.id,
        'artwork-card', art => artworkCardHTML(art, 'collection'));
    setupSortable();
}

async function removeArtworkFromPlaylist(artworkId) {
    if (!currentPlaylistId) return;
    try {
        await fetch(`${API_BASE}/playlists/${currentPlaylistId}/artworks/${artworkId}`, { method: 'DELETE' });
        await refreshData();
    } catch (error) { console.error('[Admin] Unlink failed:', error); }
}

async function deleteArtworkPermanently(id) {
    if (!(await confirmModal('Permanently delete this artwork from the library and all playlists? This wipes the file.', { confirmText: 'Delete', danger: true }))) return;
    enqueueAction(async () => {
        try {
            await fetch(`${API_BASE}/artworks/${id}`, { method: 'DELETE' });
        } catch (error) { console.error('[Admin] Delete failed:', error); }
    });
}

// Library picker is now multi-select: tap cards to toggle, then "Add N to collection" in one call.
let pickerSelected = new Set();

function openLibraryPicker() {
    const modal = document.getElementById('library-modal');
    const grid = document.getElementById('library-picker-grid');
    grid.innerHTML = '';
    pickerSelected = new Set();

    const playlist = currentPlaylists.find(p => p.id === currentPlaylistId);
    const existingIds = new Set(playlist.artworks.map(a => a.id));

    fullLibrary.filter(art => !existingIds.has(art.id)).forEach(art => {
        const card = document.createElement('div');
        card.className = 'picker-card';
        card.onclick = () => {
            if (pickerSelected.has(art.id)) { pickerSelected.delete(art.id); card.classList.remove('selected'); }
            else { pickerSelected.add(art.id); card.classList.add('selected'); }
            _renderPickerCount();
        };
        card.innerHTML = `
            <img src="${API_BASE}/artworks/${art.id}/thumbnail?f=${encodeURIComponent(art.filename)}">
            <p>${_esc(art.title || art.filename)}</p>
        `;
        grid.appendChild(card);
    });
    _renderPickerCount();
    modal.style.display = 'flex';
}

function _renderPickerCount() {
    const n = pickerSelected.size;
    document.getElementById('picker-count').textContent = `${n} selected`;
    const btn = document.getElementById('picker-add-btn');
    btn.disabled = n === 0;
    btn.textContent = n ? `Add ${n} to collection` : 'Add selected';
}

async function addSelectedToPlaylist() {
    if (!pickerSelected.size) return;
    const n = pickerSelected.size;
    try {
        await fetch(`${API_BASE}/playlists/${currentPlaylistId}/artworks`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artwork_ids: [...pickerSelected] }) });
        closeLibraryPicker();
        showToast(`Added ${n} to the collection ✓`, 'success');
        await refreshData();
    } catch (error) { console.error('[Admin] bulk add failed:', error); showToast('Add failed.', 'error'); }
}

function closeLibraryPicker() { document.getElementById('library-modal').style.display = 'none'; pickerSelected = new Set(); }

// ===== Grid multi-select: bulk add-to-collection / remove / delete =====
let gridSelectMode = false;
const gridSelected = new Set();

function toggleSelectMode() { gridSelectMode ? exitSelectMode() : enterSelectMode(); }

function enterSelectMode() {
    gridSelectMode = true;
    gridSelected.clear();
    document.body.classList.add('selecting');
    updateBulkBar();
    document.getElementById('bulk-bar').classList.add('open');
}

function exitSelectMode() {
    gridSelectMode = false;
    gridSelected.clear();
    document.body.classList.remove('selecting');
    document.getElementById('bulk-bar').classList.remove('open');
    document.querySelectorAll('.artwork-card.selected, .review-card.selected').forEach(c => c.classList.remove('selected'));
    document.querySelectorAll('.review-select input:checked').forEach(cb => { cb.checked = false; });
}

// Review Queue uses explicit checkboxes (whole-card click would fight the form inputs).
function _reviewSelectToggle(id, checked) {
    if (checked) gridSelected.add(id); else gridSelected.delete(id);
    const card = document.querySelector(`.review-card[data-id="${id}"]`);
    if (card) card.classList.toggle('selected', checked);
    updateBulkBar();
}

// Capture-phase so a click in select mode toggles selection instead of opening Edit / firing ✕.
function _gridSelectClick(e) {
    if (!gridSelectMode) return;
    const card = e.target.closest('.artwork-card');
    if (!card || !card.dataset.id) return;
    e.stopPropagation(); e.preventDefault();
    const id = parseInt(card.dataset.id, 10);
    if (gridSelected.has(id)) { gridSelected.delete(id); card.classList.remove('selected'); }
    else { gridSelected.add(id); card.classList.add('selected'); }
    updateBulkBar();
}

function updateBulkBar() {
    document.getElementById('bulk-count').textContent = `${gridSelected.size} selected`;
    const inReview = currentView === 'review';
    const inLibrary = currentView === 'library';
    // Review Queue gets a single "Approve & Publish" action; the collection/library actions hide.
    document.getElementById('bulk-approve-btn').style.display = inReview ? '' : 'none';
    document.getElementById('bulk-add-target').style.display = inReview ? 'none' : '';
    document.getElementById('bulk-remove-btn').style.display = (inReview || inLibrary) ? 'none' : '';
    document.getElementById('bulk-delete-btn').style.display = (inLibrary && !inReview) ? '' : 'none';
    if (inReview) return;
    const sel = document.getElementById('bulk-add-target');
    sel.innerHTML = '<option value="">Add to collection…</option>' +
        (currentPlaylists || [])
            .filter(p => !(currentView === 'playlists' && p.id === currentPlaylistId))
            .map(p => `<option value="${p.id}">Add to: ${p.name}</option>`).join('');
}

async function bulkAddToCollection(pid) {
    const sel = document.getElementById('bulk-add-target');
    if (!pid || !gridSelected.size) { sel.value = ''; return; }
    const n = gridSelected.size;
    try {
        await fetch(`${API_BASE}/playlists/${pid}/artworks`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artwork_ids: [...gridSelected] }) });
        showToast(`Added ${n} ✓`, 'success');
        exitSelectMode(); await refreshData();
    } catch (e) { console.error('[Admin] bulk add failed:', e); showToast('Add failed.', 'error'); }
    sel.value = '';
}

async function bulkRemoveFromCollection() {
    if (!gridSelected.size) return;
    if (!(await confirmModal(`Remove ${gridSelected.size} from this collection? They stay in your library.`, { confirmText: 'Remove' }))) return;
    try {
        await fetch(`${API_BASE}/playlists/${currentPlaylistId}/artworks`, {
            method: 'DELETE', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artwork_ids: [...gridSelected] }) });
        showToast('Removed ✓', 'success'); exitSelectMode(); await refreshData();
    } catch (e) { console.error('[Admin] bulk remove failed:', e); showToast('Remove failed.', 'error'); }
}

// Bulk-approve pending Review-Queue items using their already-enriched stored values (no per-item
// edit — that's the point of bulk). Anything off can be fixed afterward via the Edit overlay.
async function bulkApproveSelected() {
    if (!gridSelected.size) return;
    const n = gridSelected.size;
    if (!(await confirmModal(`Approve & publish ${n} item(s) with their current details? You can edit any of them later.`, { confirmText: 'Approve' }))) return;
    try {
        await fetch(`${API_BASE}/artworks/approve-bulk`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artwork_ids: [...gridSelected] }) });
        showToast(`Approved ${n} ✓`, 'success'); exitSelectMode(); await refreshData();
    } catch (e) { console.error('[Admin] bulk approve failed:', e); showToast('Approve failed.', 'error'); }
}

async function bulkDeleteSelected() {
    if (!gridSelected.size) return;
    if (!(await confirmModal(`Permanently delete ${gridSelected.size} artwork(s) from the library and all collections? This wipes the files.`, { confirmText: 'Delete', danger: true }))) return;
    try {
        await fetch(`${API_BASE}/artworks/delete`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artwork_ids: [...gridSelected] }) });
        showToast('Deleted ✓', 'success'); exitSelectMode(); await refreshData();
    } catch (e) { console.error('[Admin] bulk delete failed:', e); showToast('Delete failed.', 'error'); }
}

['library-grid', 'artwork-grid'].forEach(gid => {
    const g = document.getElementById(gid);
    if (g) g.addEventListener('click', _gridSelectClick, true);
});

// ===== Curated-catalog multi-select (Inc 3) =====
// Catalog items aren't artworks yet — they're keyed by collection_id + item_index — so they get
// their own selection separate from gridSelected (which holds artwork ids). Items can span
// collections (flat search), so the value carries both halves for the bulk payload.
let catalogSelectMode = false;
const catalogSelected = new Map();  // "cid:idx" -> { collection_id, item_index }

function toggleCatalogSelect() { catalogSelectMode ? exitCatalogSelect() : enterCatalogSelect(); }

function enterCatalogSelect() {
    catalogSelectMode = true;
    catalogSelected.clear();
    document.body.classList.add('selecting');
    populateCatalogBulkTarget();
    updateCatalogBulkBar();
    document.getElementById('catalog-bulk-bar').classList.add('open');
    syncCatalogSelectButtons();
}

function exitCatalogSelect() {
    catalogSelectMode = false;
    catalogSelected.clear();
    document.body.classList.remove('selecting');
    const bar = document.getElementById('catalog-bulk-bar');
    if (bar) bar.classList.remove('open');
    document.querySelectorAll('#catalog-container .artwork-card.selected').forEach(c => c.classList.remove('selected'));
    syncCatalogSelectButtons();
}

function syncCatalogSelectButtons() {
    document.querySelectorAll('.catalog-select-btn').forEach(b => {
        b.textContent = catalogSelectMode ? '✕ Cancel select' : '☑ Select';
    });
}

function populateCatalogBulkTarget() {
    const sel = document.getElementById('catalog-bulk-target');
    if (sel) sel.innerHTML = '<option value="">Library only</option>' +
        (currentPlaylists || []).map(p => `<option value="${p.id}">Add to: ${_esc(p.name)}</option>`).join('');
}

function updateCatalogBulkBar() {
    const el = document.getElementById('catalog-bulk-count');
    if (el) el.textContent = `${catalogSelected.size} selected`;
}

// Capture-phase so a click in select mode toggles selection instead of firing the card's Add button.
// Only item cards carry data-cidx; collection-cover cards (level 1) are ignored, so their open-click
// still works even if select mode somehow lingers.
function _catalogSelectClick(e) {
    if (!catalogSelectMode) return;
    const card = e.target.closest('.artwork-card[data-cidx]');
    if (!card) return;
    if (card.dataset.added === '1') return;  // already in the library — not selectable
    e.stopPropagation(); e.preventDefault();
    const key = card.dataset.cidx;
    if (catalogSelected.has(key)) { catalogSelected.delete(key); card.classList.remove('selected'); }
    else { catalogSelected.set(key, { collection_id: card.dataset.cid, item_index: parseInt(card.dataset.idx, 10) }); card.classList.add('selected'); }
    updateCatalogBulkBar();
}
const _catContainer = document.getElementById('catalog-container');
if (_catContainer) _catContainer.addEventListener('click', _catalogSelectClick, true);

async function catalogBulkAdd() {
    if (!catalogSelected.size) return;
    const sel = document.getElementById('catalog-bulk-target');
    const playlistId = sel && sel.value ? parseInt(sel.value, 10) : null;
    const destName = sel && sel.value ? sel.options[sel.selectedIndex].text.replace(/^Add to: /, '') : 'the Library';
    const items = [...catalogSelected.values()];
    const keys = [...catalogSelected.keys()];
    const payload = { items };
    if (playlistId) payload.playlist_id = playlistId;
    const btn = document.getElementById('catalog-bulk-add-btn');
    btn.disabled = true; btn.textContent = '⏳ Adding…';
    try {
        const resp = await fetch(`${API_BASE}/api/catalog/add-bulk`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload) });
        const data = await resp.json();
        // Mark the added cards in place (preserves scroll/place) rather than re-rendering the grid.
        keys.forEach(key => {
            const card = document.querySelector(`#catalog-container .artwork-card[data-cidx="${CSS.escape(key)}"]`);
            if (!card) return;
            card.dataset.added = '1';
            const b = card.querySelector('.actions button');
            if (b) { b.disabled = true; b.textContent = 'Added ✓'; }
        });
        showToast(`Added ${data.added} to ${destName}${data.failed ? ` · ${data.failed} failed` : ''} ✓`, data.failed ? 'error' : 'success');
        exitCatalogSelect();
        fetchLibrary();
    } catch (e) {
        console.error('[Catalog] bulk add failed:', e); showToast('Bulk add failed.', 'error');
    } finally { btn.disabled = false; btn.textContent = 'Add selected'; }
}

function renderSidebar() {
    const list = document.getElementById('playlist-list');
    // Delegated delete handler (L3): the collection name is never interpolated into an inline onclick
    // string (a name like `x'); …//` used to break out). Bound once; survives innerHTML rebuilds.
    if (!list.dataset.delDelegated) {
        list.dataset.delDelegated = '1';
        list.addEventListener('click', async (e) => {
            const del = e.target.closest('.pl-delete');
            if (del) { e.stopPropagation(); deletePlaylist(parseInt(del.dataset.id, 10)); return; }
            const ren = e.target.closest('.pl-rename');
            if (ren) {   // A4: rename via promptModal (name never interpolated into inline onclick)
                e.stopPropagation();
                const id = parseInt(ren.dataset.id, 10);
                const cur = (currentPlaylists.find(p => p.id === id) || {}).name || '';
                const name = await promptModal('Rename collection', { placeholder: cur, confirmText: 'Rename' });
                if (name === null || !name.trim()) return;
                const r = await fetch(`${API_BASE}/playlists/${id}`, {
                    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: name.trim() }),
                });
                if (!r.ok) { const d = await r.json().catch(() => ({})); showToast(d.detail || 'Rename failed', 'error'); return; }
                showToast('Renamed ✓', 'success');
                await refreshData();
            }
        });
    }
    list.innerHTML = '';
    currentPlaylists.forEach(p => {
        const li = document.createElement('li');
        li.className = `playlist-item ${p.id === currentPlaylistId ? 'active' : ''}`;
        li.dataset.id = p.id;
        li.innerHTML = `
            <div style="display:flex; justify-content:space-between; align-items:center; gap:6px;">
                <strong>${_esc(p.name)}</strong>
                <span style="display:flex; gap:8px; flex-shrink:0;">
                    <button class="pl-rename" data-id="${p.id}" title="Rename collection" aria-label="Rename collection" style="background:none; border:none; color:#94a3b8; cursor:pointer;">✎</button>
                    <button class="pl-delete" data-id="${p.id}" title="Delete collection" aria-label="Delete collection" style="background:none; border:none; color:#ef4444; cursor:pointer;">×</button>
                </span>
            </div>
            <div style="font-size:0.75rem; color:#94a3b8; margin-top:5px;">${p.artworks?.length || 0} images</div>
            <div class="playlist-meta" onclick="event.stopPropagation()" style="display: grid; grid-template-columns: 1fr 1fr; gap: 5px; margin-top: 10px;">
                <div style="grid-column: span 2; margin-bottom: 5px;">
                    <label style="display:block;">Default Mode:</label>
                    <select onchange="updatePlaylistSetting(${p.id}, {default_mode: this.value})" style="width:100%; background:#0f172a; color:white; border:1px solid var(--border-color); border-radius:4px; font-size:0.7rem;">
                        <option value="ken-burns" ${p.default_mode === 'ken-burns' ? 'selected' : ''}>Ken Burns</option>
                        <option value="static-crop" ${p.default_mode === 'static-crop' ? 'selected' : ''}>Static Crop</option>
                        <option value="contain-matte" ${p.default_mode === 'contain-matte' ? 'selected' : ''}>Contain Matte</option>
                    </select>
                </div>
                <div style="grid-column: span 2; margin-bottom: 5px; display: flex; align-items: center; gap: 10px;">
                    <label style="display:flex; align-items:center; gap:5px; cursor:pointer;">
                        <input type="checkbox" ${p.shuffle ? 'checked' : ''} onchange="updatePlaylistSetting(${p.id}, {shuffle: this.checked})" style="width:auto; margin:0;">
                        Randomize Order
                    </label>
                </div>
                <details style="grid-column: span 2; margin-top: 4px;">
                    <summary style="cursor:pointer; color:#94a3b8; font-size:0.72rem; user-select:none;">⚙ Advanced timing</summary>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:5px; margin-top:8px;">
                        <div>
                            <label title="Seconds each image is shown before advancing">Cycle (s):</label>
                            <input type="number" value="${p.display_time}" min="1" onchange="updatePlaylistSetting(${p.id}, {display_time: parseInt(this.value)})" style="width:100%;">
                        </div>
                        <div>
                            <label title="Seconds before the placard fades in after an image appears">Wait (s):</label>
                            <input type="number" value="${p.placard_initial_wait_sec}" min="0" onchange="updatePlaylistSetting(${p.id}, {placard_initial_wait_sec: parseInt(this.value)})" style="width:100%;">
                        </div>
                        <div>
                            <label title="Seconds the placard stays visible automatically">Show (s):</label>
                            <input type="number" value="${p.placard_initial_show_sec}" min="0" onchange="updatePlaylistSetting(${p.id}, {placard_initial_show_sec: parseInt(this.value)})" style="width:100%;">
                        </div>
                        <div>
                            <label title="Seconds the placard shows when you tap or click the screen">Manual (s):</label>
                            <input type="number" value="${p.placard_interaction_show_sec}" min="0" onchange="updatePlaylistSetting(${p.id}, {placard_interaction_show_sec: parseInt(this.value)})" style="width:100%;">
                        </div>
                    </div>
                </details>
            </div>
        `;
        li.onclick = () => selectPlaylist(p.id);
        list.appendChild(li);
    });
}

async function deletePlaylist(id) {
    const name = (currentPlaylists.find(p => p.id === id) || {}).name || 'this collection';
    if (!(await confirmModal(`Delete collection "${name}"? Library images will remain.`, { confirmText: 'Delete', danger: true }))) return;
    try {
        await fetch(`${API_BASE}/playlists/${id}`, { method: 'DELETE' });
        if (currentPlaylistId === id) currentPlaylistId = null;
        await refreshData();
    } catch (error) { console.error('[Admin] Delete failed:', error); }
}

function selectPlaylist(id) {
    currentPlaylistId = id;
    // Remember the open collection so a browser refresh returns to it instead of
    // resetting to the first collection in the list.
    try { localStorage.setItem('sd_admin_playlist', String(id)); } catch (e) {}
    const playlist = currentPlaylists.find(p => p.id === id);
    if (!playlist) return;
    document.querySelectorAll('.playlist-item').forEach(el => el.classList.toggle('active', parseInt(el.dataset.id) === id));
    document.getElementById('target-playlist-name').textContent = 'to ' + playlist.name;
    renderArtworkGrid(playlist.artworks || []);
    document.body.classList.remove('sidebar-open');  // close the mobile drawer on selection
}

function setupUploadZone() {
    const zones = [document.getElementById('upload-zone'), document.getElementById('library-upload-zone')];
    const inputs = [document.getElementById('file-input'), document.getElementById('library-file-input')];

    zones.forEach((zone, idx) => {
        if (!zone) return;
        zone.ondragover = (e) => { e.preventDefault(); zone.style.borderColor = '#3b82f6'; };
        zone.ondragleave = () => { zone.style.borderColor = '#334155'; };
        zone.ondrop = (e) => {
            e.preventDefault();
            zone.style.borderColor = '#334155';
            const pid = (zone.id === 'upload-zone') ? currentPlaylistId : null;
            if (e.dataTransfer.files) uploadFiles(e.dataTransfer.files, pid);
        };
    });

    document.getElementById('upload-zone').onclick = () => document.getElementById('file-input').click();
    document.getElementById('file-input').onchange = (e) => { if (e.target.files) uploadFiles(e.target.files, currentPlaylistId); };
}

async function uploadFiles(files, playlistId) {
    for (let file of files) {
        const fd = new FormData();
        fd.append('file', file);
        if (playlistId) fd.append('playlist_id', playlistId);
        try { await fetch(`${API_BASE}/upload`, { method: 'POST', body: fd }); }
        catch (error) { console.error('[Admin] Upload failed:', error); }
    }
    // Immediate refresh after upload completes
    await refreshData();

    // No model connected → auto-analysis didn't run. Tell the user (no silent empty metadata),
    // and un-dismiss the Review Queue banner so the manual path is obvious.
    if (!aiConfigured) {
        const banner = document.getElementById('ai-offline-banner');
        if (banner) delete banner.dataset.dismissed;
        applyAiGating();
        showTransientNotice('Uploaded to the Review Queue. Auto-analysis is off — add details there, or connect a model.', nudgeConnectModel);
    }
}

// Minimal non-blocking toast (no framework). Optional action label jumps to the AI Engine panel.
function showTransientNotice(message, onAction) {
    const t = document.createElement('div');
    t.style.cssText = 'position:fixed; bottom:24px; left:50%; transform:translateX(-50%); z-index:9999; ' +
        'background:#1e293b; border:1px solid #f59e0b; color:#fcd34d; padding:12px 18px; border-radius:10px; ' +
        'font-size:0.82rem; max-width:520px; box-shadow:0 8px 24px rgba(0,0,0,0.4); display:flex; gap:14px; align-items:center;';
    const span = document.createElement('span');
    span.textContent = message;
    span.style.flexGrow = '1';
    t.appendChild(span);
    if (onAction) {
        const a = document.createElement('button');
        a.className = 'secondary';
        a.textContent = 'Connect a model';
        a.style.cssText = 'padding:5px 12px; font-size:0.75rem; white-space:nowrap;';
        a.onclick = () => { t.remove(); onAction(); };
        t.appendChild(a);
    }
    document.body.appendChild(t);
    setTimeout(() => { t.style.transition = 'opacity 0.5s'; t.style.opacity = '0'; setTimeout(() => t.remove(), 500); }, 6000);
}

async function updatePlaylistSetting(id, settings) {
    try {
        if (pollInterval) clearInterval(pollInterval);
        await fetch(`${API_BASE}/playlists/${id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(settings)
        });
        await refreshData();
        startPolling();
    } catch (error) { 
        console.error('[Admin] Update failed:', error); 
        startPolling();
    }
}

function setupSortable() {
    const grid = document.getElementById('artwork-grid');
    if (sortableInstance) sortableInstance.destroy();
    sortableInstance = new Sortable(grid, {
        animation: 150, ghostClass: 'sortable-ghost',
        onEnd: async () => {
            const ids = Array.from(grid.children).map(el => parseInt(el.dataset.id));
            await saveOrder(ids);
        }
    });
}

async function saveOrder(ids) {
    if (!currentPlaylistId) return;
    try {
        await fetch(`${API_BASE}/playlists/${currentPlaylistId}/reorder`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ artwork_ids: ids })
        });
        await refreshData();
    } catch (error) { console.error('[Admin] Reorder failed:', error); }
}

// Review-card input id prefix -> artwork property. Used to live-sync enrichment
// data onto already-rendered cards without clobbering manual edits.
const REVIEW_FIELDS = [
    ['title', 'title'],
    ['agent', 'agent_name'],
    ['role', 'agent_role'],
    ['date', 'creation_date'],
    ['context', 'cultural_context'],
    ['medium', 'medium'],
    ['date-display', 'date_display'],
    ['tags', 'tags'],
    ['desc', 'description_narrative'],
];

/**
 * Pushes the latest server values onto a card's inputs, but only where the user
 * hasn't diverged. Each input remembers the last server value in data-server; if
 * the current value still matches it (and the field isn't focused), we overwrite
 * with the new server value. This fills in blank cards as enrichment lands while
 * leaving any field the user has typed into untouched.
 */
function syncReviewCardFields(art) {
    REVIEW_FIELDS.forEach(([prefix, key]) => {
        const el = document.getElementById(`${prefix}-${art.id}`);
        if (!el) return;
        const serverVal = art[key] || '';
        if (document.activeElement === el) return; // never yank text from under the cursor
        if (el.value === (el.dataset.server || '')) {
            el.value = serverVal;
        }
        el.dataset.server = serverVal;
    });
}

// The review card's image + editable form (everything but the bulk-select checkbox). Shared by the
// Review Queue and the inline discovery review (Inc 4), so both use identical fields, ids, and the
// same approve/regenerate/sync wiring.
function reviewFormHTML(art) {
    return `
                <div class="review-image"><img src="${API_BASE}/artworks/${art.id}/thumbnail?f=${encodeURIComponent(art.filename || '')}"></div>
                <div class="review-form">
                    <div class="form-group"><label>Title</label><input type="text" id="title-${art.id}" value="${_esc(art.title || '')}"></div>
                    <div class="form-group"><label>Agent/Artist</label><input type="text" id="agent-${art.id}" value="${_esc(art.agent_name || '')}"></div>
                    <div class="form-group"><label>Role</label><input type="text" id="role-${art.id}" value="${_esc(art.agent_role || '')}"></div>
                    <div class="form-group"><label>Date/Year</label><input type="text" id="date-${art.id}" value="${_esc(_fmtDate(art.creation_date))}"></div>
                    <div class="form-group"><label>Context</label><input type="text" id="context-${art.id}" value="${_esc(art.cultural_context || '')}"></div>
                    <div class="form-group"><label>Medium</label><input type="text" id="medium-${art.id}" value="${_esc(art.medium || '')}"></div>
                    <div class="form-group"><label>Display Date</label><input type="text" id="date-display-${art.id}" value="${_esc(art.date_display || '')}"></div>
                    <div class="form-group"><label>Tags</label><input type="text" id="tags-${art.id}" value="${_esc(art.tags || '')}"></div>
                    <div class="form-group full"><label>Narrative Description</label><textarea id="desc-${art.id}" rows="3">${_esc(art.description_narrative || '')}</textarea></div>
                    <div class="form-group full" style="border-top: 1px solid var(--border-color); padding-top: 15px; margin-top: 5px;">
                        <label>AI Guidance (Optional)</label>
                        <div style="display: flex; gap: 10px;">
                            <input type="text" id="hint-${art.id}" placeholder="e.g., 'This is my dog Buster in 2021'" style="flex-grow: 1;">
                            <button class="primary" data-ai-action="1" id="regen-btn-${art.id}" onclick="regenerateArtworkMetadata(${art.id})" style="padding: 10px 20px;">
                                <span id="regen-text-${art.id}">Regenerate</span>
                            </button>
                        </div>
                    </div>
                    <div class="review-actions">
                        <button class="secondary" onclick="deleteArtworkPermanently(${art.id})">Delete</button>
                        <button class="success" onclick="approveArtwork(${art.id})">Approve & Publish</button>
                    </div>
                </div>`;
}

function renderReviewQueue(artworks) {
    const list = document.getElementById('review-list');

    // Items being reviewed inline (in the Museum discovery grid, Inc 4) are finalized there — stream
    // their enrichment into the inline card and keep them out of the Review-Queue list entirely.
    artworks.filter(a => inlineReviewing.has(a.id)).forEach(a => {
        syncReviewCardFields(a);
        const ic = document.querySelector(`.inline-review[data-inline-art="${a.id}"]`);
        if (ic) { const sp = ic.querySelector('.inline-review-spinner'); if (sp) sp.remove(); }
    });
    artworks = artworks.filter(a => !inlineReviewing.has(a.id));

    // A5: rich empty state that teaches what the queue is + where items come from; hide the ☑ Select
    // toolbar when there's nothing to select.
    const toolbar = document.getElementById('review-toolbar');
    if (artworks.length > 0 && list.innerHTML.includes('review-empty')) {
        list.innerHTML = '';
    } else if (artworks.length === 0) {
        if (toolbar) toolbar.style.display = 'none';
        list.innerHTML = `<div class="review-empty" style="text-align:center; color:#94a3b8; margin-top:40px;">
            <p style="font-size:1rem; color:var(--text-color);">Your review queue is empty.</p>
            <p style="font-size:0.85rem; max-width:440px; margin:8px auto 18px;">Live museum finds and your uploads land here for approval — so nothing reaches your walls unchecked.</p>
            <p><a href="#" onclick="switchView('museum');return false;">🏛️ Search live museums</a> &nbsp;·&nbsp; <a href="#" onclick="switchView('library');return false;">⬆ Upload to Library</a></p>
        </div>`;
        return;
    }
    if (toolbar) toolbar.style.display = '';
    
    // repaintOnReuse:false — these cards hold live <input>s the user may be editing, so we never
    // rewrite a matched card's DOM; syncReviewCardFields patches values in place (and is XSS-safe
    // because it assigns .value, not innerHTML — the escaping that matters is in reviewFormHTML above).
    reconcileGrid(list, artworks, a => a.id, 'review-card',
        art => `
                <label class="review-select" title="Select for bulk approve"><input type="checkbox" onchange="_reviewSelectToggle(${art.id}, this.checked)"></label>
                ${reviewFormHTML(art)}
            `,
        { repaintOnReuse: false, onCard: (card, art) => {
            // New cards: records the server baseline. Existing cards: fills in enrichment that
            // arrived since first render. Then keep the bulk-select checkbox in sync with gridSelected.
            syncReviewCardFields(art);
            const cb = card.querySelector('.review-select input');
            if (cb) { const sel = gridSelected.has(art.id); cb.checked = sel; card.classList.toggle('selected', sel); }
        }});

    applyAiGating(); // re-gate freshly rendered Regenerate buttons + toggle the no-AI banner
}

function regenerateArtworkMetadata(id) {
    if (!aiConfigured) { nudgeConnectModel(); return; }
    const hint = document.getElementById(`hint-${id}`).value;
    const btn = document.getElementById(`regen-btn-${id}`);
    const textSpan = document.getElementById(`regen-text-${id}`);
    
    // UI Feedback
    btn.disabled = true;
    btn.style.opacity = "0.7";
    const originalText = textSpan.textContent;
    textSpan.textContent = "Queued...";
    
    enqueueAction(async () => {
        textSpan.textContent = "Processing...";
        try {
            const response = await fetch(`${API_BASE}/api/curate/regenerate/${id}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ hint: hint })
            });
            
            if (!response.ok) throw new Error("Regeneration failed");
            
            const updatedArt = await response.json();
            
            // Dynamically update fields
            document.getElementById(`title-${id}`).value = updatedArt.title || '';
            document.getElementById(`agent-${id}`).value = updatedArt.agent_name || '';
            document.getElementById(`role-${id}`).value = updatedArt.agent_role || '';
            document.getElementById(`date-${id}`).value = _fmtDate(updatedArt.creation_date);
            document.getElementById(`context-${id}`).value = updatedArt.cultural_context || '';
            document.getElementById(`medium-${id}`).value = updatedArt.medium || '';
            document.getElementById(`date-display-${id}`).value = updatedArt.date_display || '';
            document.getElementById(`tags-${id}`).value = updatedArt.tags || '';
            document.getElementById(`desc-${id}`).value = updatedArt.description_narrative || '';
            
            // Clear hint
            document.getElementById(`hint-${id}`).value = '';
            
        } catch (error) {
            console.error('[Admin] Regen failed:', error);
            showToast("AI Regeneration failed. Check logs.", 'error');
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.style.opacity = "1";
                if(textSpan) textSpan.textContent = originalText;
            }
        }
    });
}

function approveArtwork(id) {
    const btn = document.querySelector(`#title-${id}`).closest('.review-form').querySelector('.success');
    btn.disabled = true;
    btn.textContent = "Queued...";
    btn.style.opacity = "0.7";
    
    const metadata = {
        title: document.getElementById(`title-${id}`).value,
        agent_name: document.getElementById(`agent-${id}`).value,
        agent_role: document.getElementById(`role-${id}`).value,
        creation_date: document.getElementById(`date-${id}`).value,
        cultural_context: document.getElementById(`context-${id}`).value,
        medium: document.getElementById(`medium-${id}`).value,
        date_display: document.getElementById(`date-display-${id}`).value,
        tags: document.getElementById(`tags-${id}`).value,
        description_narrative: document.getElementById(`desc-${id}`).value
    };
    
    enqueueAction(async () => {
        btn.textContent = "Approving...";
        try {
            await fetch(`${API_BASE}/artworks/${id}/approve`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(metadata)
            });
        } catch (error) { console.error('[Admin] Approval failed:', error); }
    });
}

// =============================================================================
// Unified full-screen Edit landing — one screen to crop, set the focal point,
// edit the placard, and re-enrich. Opened from Library + Collections (click the
// thumbnail or Edit). Branches on is_personal: museum = full metadata; personal
// = jargon-free caption + date. Both backends are reused, so no behaviour drift.
// =============================================================================
let editCropper = null, editId = null, editCtx = 'library', editIsPersonal = false;
let editFocalX = 0.5, editFocalY = 0.5;
const EDIT_MUSEUM_FIELDS = ['title','agent_name','agent_role','creation_date','date_display',
                            'cultural_context','medium','tags','description_narrative'];

function _findArt(id) {
    let a = fullLibrary.find(x => x.id === id);
    if (!a) { for (const p of (currentPlaylists || [])) { a = (p.artworks || []).find(x => x.id === id); if (a) break; } }
    return a;
}

function openEdit(id, ctx) {
    const art = _findArt(id);
    if (!art) { showToast('Could not load that artwork.', 'error'); return; }
    // Context decides the danger action: Delete (whole library) vs Remove (this collection only).
    editId = id; editCtx = ctx || (currentView === 'library' ? 'library' : 'collection'); editIsPersonal = !!art.is_personal;

    document.getElementById('edit-head-title').textContent = editIsPersonal ? 'Edit photo' : 'Edit artwork';
    document.getElementById('edit-museum').style.display = editIsPersonal ? 'none' : '';
    document.getElementById('edit-personal').style.display = editIsPersonal ? '' : 'none';

    if (editIsPersonal) {
        document.getElementById('edit-p-caption').value = art.title || '';
        document.getElementById('edit-p-date').value = art.date_display || '';
        document.getElementById('edit-personal-note').textContent = aiConfigured
            ? '✨ Suggest writes a short, album-style caption.'
            : '✨ auto-caption needs an AI model (Settings → AI Engine).';
    } else {
        EDIT_MUSEUM_FIELDS.forEach(k => { document.getElementById('edit-f-' + k).value = art[k] || ''; });
    }

    // Danger button: Delete (library) vs Remove from collection.
    const danger = document.getElementById('edit-danger');
    const dangerCtx = editCtx;   // capture for the async handler
    danger.textContent = dangerCtx === 'collection' ? 'Remove from collection' : 'Delete';
    danger.onclick = async () => { closeEdit(); if (dangerCtx === 'collection') await removeArtworkFromPlaylist(id); else await deleteArtworkPermanently(id); };

    // Focal mini-picker (thumbnail + draggable dot).
    document.getElementById('edit-focal-img').src = `${API_BASE}/artworks/${id}/thumbnail?f=${encodeURIComponent(art.filename)}`;
    _editSetDot(art.focal_x ?? 0.5, art.focal_y ?? 0.5);

    // Cropper on the full preview, restoring any saved crop rect.
    const cimg = document.getElementById('edit-cropper-image');
    cimg.src = `${API_BASE}/artworks/${id}/preview?f=${encodeURIComponent(art.filename)}`;
    document.getElementById('edit-overlay').classList.add('open');
    if (editCropper) editCropper.destroy();
    editCropper = new Cropper(cimg, {
        viewMode: 1, dragMode: 'move', autoCropArea: 0.9, restore: false,
        guides: true, center: true, highlight: false,
        ready() {
            if (art.crop_width > 1) {
                const cd = editCropper.getCanvasData();
                const r = cd.naturalWidth / art.original_width;
                editCropper.setData({ x: art.crop_x * r, y: art.crop_y * r, width: art.crop_width * r, height: art.crop_height * r });
            }
        }
    });
    document.querySelectorAll('#edit-ratios button').forEach((b, i) => b.classList.toggle('active', i === 2));
}

function editSetRatio(ratio, btn) {
    if (!editCropper) return;
    editCropper.setAspectRatio(ratio);
    document.querySelectorAll('#edit-ratios button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
}

function _editSetDot(fx, fy) {
    editFocalX = Math.min(1, Math.max(0, fx)); editFocalY = Math.min(1, Math.max(0, fy));
    const dot = document.getElementById('edit-focal-dot');
    dot.style.left = (editFocalX * 100) + '%'; dot.style.top = (editFocalY * 100) + '%';
}

// Wire the focal picker once (the element lives in static HTML).
(function wireEditFocal() {
    const wrap = document.getElementById('edit-focal-wrap');
    if (!wrap) return;
    let dragging = false;
    const set = e => {
        const r = wrap.getBoundingClientRect();
        const p = e.touches ? e.touches[0] : e;
        _editSetDot((p.clientX - r.left) / r.width, (p.clientY - r.top) / r.height);
    };
    wrap.addEventListener('mousedown', e => { dragging = true; set(e); });
    window.addEventListener('mousemove', e => { if (dragging) set(e); });
    window.addEventListener('mouseup', () => { dragging = false; });
    wrap.addEventListener('touchstart', e => { set(e); e.preventDefault(); }, { passive: false });
    wrap.addEventListener('touchmove', e => { set(e); e.preventDefault(); }, { passive: false });
})();

async function saveEdit() {
    if (editId == null) return;
    const id = editId;
    try {
        // Crop + focal in one /crop call (it already accepts both).
        const body = { crop_x: 0, crop_y: 0, crop_width: 0, crop_height: 0, focal_x: editFocalX, focal_y: editFocalY };
        if (editCropper) {
            const data = editCropper.getData();
            const cd = editCropper.getCanvasData();
            const art = _findArt(id);
            const r = art.original_width / cd.naturalWidth;
            body.crop_x = data.x * r; body.crop_y = data.y * r; body.crop_width = data.width * r; body.crop_height = data.height * r;
        }
        await fetch(`${API_BASE}/artworks/${id}/crop`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

        if (editIsPersonal) {
            await fetch(`${API_BASE}/api/studio/photo/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ caption: document.getElementById('edit-p-caption').value, date: document.getElementById('edit-p-date').value }) });
        } else {
            const meta = {};
            EDIT_MUSEUM_FIELDS.forEach(k => { meta[k] = document.getElementById('edit-f-' + k).value || ''; });
            await fetch(`${API_BASE}/artworks/${id}/metadata`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(meta) });
        }
        showToast('Saved ✓', 'success');
        closeEdit();
        await refreshData();
    } catch (e) { console.error('[Edit] save failed:', e); showToast('Save failed.', 'error'); }
}

function closeEdit() {
    document.getElementById('edit-overlay').classList.remove('open');
    if (editCropper) { editCropper.destroy(); editCropper = null; }
    editId = null;
}

async function editReenrich() {
    if (!aiConfigured) { nudgeConnectModel(); closeEdit(); return; }
    const id = editId;
    closeEdit();
    await reenrichArtwork(id);   // re-enriches via AI and bounces the item to the Review Queue
}

async function editSuggest() {
    if (!aiConfigured) { nudgeConnectModel(); return; }
    const id = editId;
    const btn = document.getElementById('edit-suggest');
    btn.disabled = true; const old = btn.textContent; btn.textContent = '✨…';
    try {
        const cap = document.getElementById('edit-p-caption').value.trim();
        const r = await fetch(`${API_BASE}/api/studio/caption/${id}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ hint: cap || null }) });
        const d = await r.json();
        if (r.ok && d.caption) document.getElementById('edit-p-caption').value = d.caption;
        else showToast(d.detail || 'Could not suggest a caption.', 'error');
    } catch (e) { showToast('Caption service unavailable.', 'error'); }
    finally { btn.disabled = false; btn.textContent = old; }
}

document.addEventListener('DOMContentLoaded', init);

// -----------------------------------------------------------------------------
// Freemium API Configuration (Tier-2 Scouts)
// -----------------------------------------------------------------------------
async function loadPremiumSettings() {
    try {
        const response = await fetch(`${API_BASE}/api/settings/keys`);
        const keys = await response.json();
        
        if (keys.harvard) unlockPremiumScout('harvard', 'Harvard Art Museums');
        if (keys.smithsonian) unlockPremiumScout('smithsonian', 'Smithsonian');
        if (keys.europeana) unlockPremiumScout('europeana', 'Europeana');

    } catch (e) {
        console.error("Failed to load settings:", e);
    }
}

async function promptApiKey(source, name, registerUrl) {
    const key = await promptModal(`Unlock ${name}\n\nEnter your free developer API key (generate one instantly at ${registerUrl}):`, { placeholder: 'Paste API key', confirmText: 'Unlock' });
    if (!key) return; // User cancelled

    const label = document.getElementById(`premium-${source}`);
    const originalContent = label.innerHTML;
    label.innerHTML = "⏳ Validating...";
    label.style.pointerEvents = "none";

    try {
        const response = await fetch(`${API_BASE}/api/settings/keys/${source}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: key })
        });
        
        const data = await response.json();
        if (!response.ok) {
            showToast(data.detail || "Invalid key", 'error');
            label.innerHTML = originalContent;
            label.style.pointerEvents = "auto";
            return;
        }

        // Success! Convert to standard checkbox
        showToast(`${name} unlocked and available for scouting.`, 'success');
        unlockPremiumScout(source, name);
    } catch (e) {
        showToast("Network error validating API key.", 'error');
        label.innerHTML = originalContent;
        label.style.pointerEvents = "auto";
    }
}

function unlockPremiumScout(source, name) {
    const label = document.getElementById(`premium-${source}`);
    if (label) label.remove();
    
    // Add to standard checkboxes if it doesn't already exist
    const scoutsContainer = document.getElementById('scout-sources');
    if (!scoutsContainer) return;

    // Check if exists
    if (scoutsContainer.querySelector(`input[value="${source}"]`)) return;

    const newCb = document.createElement('label');
    newCb.style.cssText = "display: flex; align-items: center; gap: 6px; font-size: 0.8rem; cursor: pointer;";
    newCb.innerHTML = `<input type="checkbox" name="scout-source" value="${source}" checked style="width: auto; margin: 0;"> ${name}`;
    scoutsContainer.appendChild(newCb);
}

// -----------------------------------------------------------------------------
// AI Engine (model provider configuration)
// -----------------------------------------------------------------------------
let aiPresets = {};
let aiConfigured = false;   // mirrors /api/settings/ai has_key; gates AI-only controls

function _setVal(id, v) { const el = document.getElementById(id); if (el) el.value = v || ''; }

// Soft-disable AI-only controls (Batch Enrich / Enrich / Regenerate) when no model is connected.
// Visual cue only; the handlers themselves enforce the block via nudgeConnectModel().
function applyAiGating() {
    document.querySelectorAll('[data-ai-action]').forEach(el => {
        el.classList.toggle('ai-gated', !aiConfigured);
        el.title = aiConfigured ? '' : 'Connect a model in Settings → AI Engine to use this';
    });
    // Review Queue banner: show only when there are pending items and no model is connected.
    const banner = document.getElementById('ai-offline-banner');
    if (banner && !banner.dataset.dismissed) {
        const hasPending = !!document.querySelector('#review-list .review-card');
        banner.style.display = (!aiConfigured && hasPending) ? 'flex' : 'none';
    }
}

// Send the user to the AI Engine panel and flash it (used when they trigger a gated action).
function nudgeConnectModel() {
    switchView('settings');
    const card = document.getElementById('ai-engine-card');
    if (card) {
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        card.classList.remove('ai-flash');
        void card.offsetWidth; // restart the animation
        card.classList.add('ai-flash');
    }
}

async function loadAiSettings() {
    try {
        const resp = await fetch(`${API_BASE}/api/settings/ai`);
        const cfg = await resp.json();
        aiPresets = cfg.presets || {};
        aiConfigured = !!cfg.has_key;
        applyAiGating();

        const provSel = document.getElementById('ai-provider');
        if (!provSel) return; // panel not present
        provSel.innerHTML = '';
        Object.entries(aiPresets).forEach(([key, p]) => {
            const opt = document.createElement('option');
            opt.value = key; opt.textContent = p.label;
            provSel.appendChild(opt);
        });
        provSel.value = (cfg.provider in aiPresets) ? cfg.provider : 'gemini';

        renderAiModels(provSel.value, cfg.model);
        _setVal('ai-base-url', cfg.base_url);
        _setVal('ai-model-fast', cfg.model_fast);
        _setVal('ai-temp', cfg.temperature);
        applyProviderUI(provSel.value);

        const status = document.getElementById('ai-status');
        if (status) {
            if (cfg.has_key) {
                const src = cfg.key_source === 'env' ? ' (key from environment)' : '';
                const label = aiPresets[cfg.provider] ? aiPresets[cfg.provider].label : cfg.provider;
                status.innerHTML = `✓ Connected — <strong>${label}</strong> · ${cfg.model || '(default model)'}${src}`;
                status.style.color = '#34d399';
            } else {
                status.textContent = '⚠ No model configured yet — enrichment & smart search are disabled until you set one.';
                status.style.color = '#f59e0b';
            }
        }
    } catch (e) {
        console.error('Failed to load AI settings:', e);
    }
}

function renderAiModels(provider, selected) {
    const sel = document.getElementById('ai-model');
    if (!sel) return;
    const models = (aiPresets[provider] && aiPresets[provider].models) || [];
    sel.innerHTML = '';
    models.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m; opt.textContent = m;
        sel.appendChild(opt);
    });
    const customOpt = document.createElement('option');
    customOpt.value = '__custom__'; customOpt.textContent = 'Custom…';
    sel.appendChild(customOpt);

    if (selected && models.includes(selected)) {
        sel.value = selected;
        toggleCustomModel(false);
    } else if (selected) {
        sel.value = '__custom__';
        toggleCustomModel(true, selected);
    } else {
        sel.selectedIndex = 0;
        toggleCustomModel(sel.value === '__custom__');
    }
}

function toggleCustomModel(show, val) {
    const inp = document.getElementById('ai-model-custom');
    if (!inp) return;
    inp.style.display = show ? 'block' : 'none';
    if (val !== undefined) inp.value = val || '';
}

function onAiModelChange() {
    const sel = document.getElementById('ai-model');
    toggleCustomModel(sel.value === '__custom__');
}

function applyProviderUI(provider) {
    const p = aiPresets[provider] || {};
    const keyRow = document.getElementById('ai-key-row');
    const oauthRow = document.getElementById('ai-oauth-row');
    const note = document.getElementById('ai-oauth-note');
    const link = document.getElementById('ai-key-link');
    // crypto.subtle (used by the OAuth PKCE flow) only works in a secure context
    // (HTTPS or localhost). Over http://<LAN-IP> it's unavailable, so fall back to paste-a-key.
    const oauthUsable = p.oauth && window.isSecureContext;
    if (keyRow) keyRow.style.display = oauthUsable ? 'none' : 'block';
    if (oauthRow) oauthRow.style.display = oauthUsable ? 'block' : 'none';
    if (note) note.style.display = (p.oauth && !window.isSecureContext) ? 'block' : 'none';
    if (link) {
        if (p.key_url) { link.style.display = 'inline'; link.href = p.key_url; }
        else { link.style.display = 'none'; }
    }
}

function onAiProviderChange() {
    const provider = document.getElementById('ai-provider').value;
    renderAiModels(provider, null);
    const base = document.getElementById('ai-base-url');
    if (base) base.value = (aiPresets[provider] && aiPresets[provider].base_url) || '';
    applyProviderUI(provider);
}

function currentAiModel() {
    const sel = document.getElementById('ai-model');
    if (sel.value === '__custom__') return document.getElementById('ai-model-custom').value.trim();
    return sel.value;
}

async function saveAiSettings() {
    const provider = document.getElementById('ai-provider').value;
    const model = currentAiModel();
    const result = document.getElementById('ai-save-result');
    const btn = document.getElementById('ai-save-btn');
    if (!model) { result.textContent = 'Please choose or enter a model.'; result.style.color = '#f59e0b'; return; }

    const payload = {
        provider,
        api_key: document.getElementById('ai-key').value.trim(),
        model,
        model_fast: document.getElementById('ai-model-fast').value.trim(),
        base_url: document.getElementById('ai-base-url').value.trim(),
        temperature: document.getElementById('ai-temp').value.trim()
    };

    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = '⏳ Testing…'; result.textContent = '';
    try {
        const resp = await fetch(`${API_BASE}/api/settings/ai`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok) {
            result.textContent = '✗ ' + (data.detail || 'Save failed');
            result.style.color = '#ef4444';
        } else {
            result.textContent = '✓ Saved & verified';
            result.style.color = '#34d399';
            document.getElementById('ai-key').value = '';
            await loadAiSettings();
        }
    } catch (e) {
        result.textContent = '✗ Network error';
        result.style.color = '#ef4444';
    } finally {
        btn.disabled = false; btn.textContent = orig;
    }
}

// --- Samsung Frame TV ---
async function loadFrameSettings() {
    try {
        const [cfgResp, plResp] = await Promise.all([
            fetch(`${API_BASE}/api/settings/frame`),
            fetch(`${API_BASE}/playlists`)
        ]);
        const cfg = await cfgResp.json();
        const playlists = plResp.ok ? await plResp.json() : [];
        const sel = document.getElementById('frame-playlist');
        sel.innerHTML = '<option value="">First collection (default)</option>' +
            playlists.map(p => `<option value="${_esc(p.name)}">${_esc(p.name)}</option>`).join('');
        document.getElementById('frame-enabled').checked = !!cfg.enabled;
        document.getElementById('frame-host').value = cfg.host || '';
        sel.value = cfg.playlist || '';
        const mins = Math.round((cfg.interval_sec || 900) / 60);
        const intSel = document.getElementById('frame-interval-min');
        if ([...intSel.options].some(o => o.value == mins)) intSel.value = String(mins);
        document.getElementById('frame-resolution').value = `${cfg.width || 3840}x${cfg.height || 2160}`;
        document.getElementById('frame-matte').value = cfg.matte || 'none';
        const status = document.getElementById('frame-status');
        status.textContent = cfg.last_push_at
            ? `Last pushed artwork #${cfg.last_artwork_id} at ${new Date(cfg.last_push_at * 1000).toLocaleString()}.`
            : '';
    } catch (e) { /* non-fatal: panel just shows defaults */ }
}

async function saveFrameSettings() {
    const result = document.getElementById('frame-save-result');
    const btn = document.getElementById('frame-save-btn');
    const [w, h] = document.getElementById('frame-resolution').value.split('x').map(Number);
    const payload = {
        enabled: document.getElementById('frame-enabled').checked,
        host: document.getElementById('frame-host').value.trim(),
        playlist: document.getElementById('frame-playlist').value,
        interval_sec: parseInt(document.getElementById('frame-interval-min').value, 10) * 60,
        width: w, height: h,
        matte: document.getElementById('frame-matte').value
    };
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = '⏳ Saving…'; result.textContent = '';
    try {
        const resp = await fetch(`${API_BASE}/api/settings/frame`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok) { result.textContent = '✗ ' + (data.detail || 'Save failed'); result.style.color = '#ef4444'; }
        else { result.textContent = '✓ Saved'; result.style.color = '#34d399'; await loadFrameSettings(); }
    } catch (e) { result.textContent = '✗ Network error'; result.style.color = '#ef4444'; }
    finally { btn.disabled = false; btn.textContent = orig; }
}

async function testFramePush() {
    const result = document.getElementById('frame-save-result');
    const btn = document.getElementById('frame-test-btn');
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = '⏳ Pushing…'; result.textContent = ' (uses saved settings)';
    try {
        const resp = await fetch(`${API_BASE}/api/settings/frame/test`, { method: 'POST' });
        const data = await resp.json();
        if (data.status === 'pushed') { result.textContent = `✓ Pushed to Frame (artwork #${data.artwork_id})`; result.style.color = '#34d399'; }
        else if (data.status === 'unchanged') { result.textContent = '✓ Frame already shows the current artwork'; result.style.color = '#34d399'; }
        else if (data.status === 'skipped') { result.textContent = '⚠ ' + (data.reason || 'nothing to push'); result.style.color = '#f59e0b'; }
        else { result.textContent = '✗ ' + (data.reason || 'push failed'); result.style.color = '#ef4444'; }
    } catch (e) { result.textContent = '✗ Network error'; result.style.color = '#ef4444'; }
    finally { btn.disabled = false; btn.textContent = orig; }
}

// --- Catalog Source (remote-hosted manifest) ---
async function loadCatalogSource() {
    try {
        const resp = await fetch(`${API_BASE}/api/settings/catalog`);
        if (!resp.ok) return;
        const cfg = await resp.json();
        document.getElementById('catalog-url').value = cfg.catalog_url || '';
        const status = document.getElementById('catalog-source-status');
        status.textContent = cfg.using_remote
            ? `Currently serving a remote catalog from ${cfg.catalog_url}.`
            : 'Currently serving the bundled catalog.';
    } catch (e) { /* non-fatal: panel just shows defaults */ }
}

async function saveCatalogSource() {
    const result = document.getElementById('catalog-source-result');
    const btn = document.getElementById('catalog-save-btn');
    const url = document.getElementById('catalog-url').value.trim();
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = '⏳ Testing…'; result.textContent = '';
    try {
        const resp = await fetch(`${API_BASE}/api/settings/catalog`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ catalog_url: url })
        });
        const data = await resp.json();
        if (!resp.ok) { result.textContent = '✗ ' + (data.detail || 'Save failed'); result.style.color = '#ef4444'; }
        else if (data.warning) { result.textContent = '⚠ ' + data.warning; result.style.color = '#f59e0b'; await loadCatalogSource(); }
        else { result.textContent = '✓ ' + (data.message || 'Saved'); result.style.color = '#34d399'; await loadCatalogSource(); }
    } catch (e) { result.textContent = '✗ Network error'; result.style.color = '#ef4444'; }
    finally { btn.disabled = false; btn.textContent = orig; }
}

async function resetCatalogSource() {
    document.getElementById('catalog-url').value = '';
    await saveCatalogSource();   // empty URL ⇒ backend reverts to the bundled catalog
}

async function loadDefaultPlaylist() {
    const sel = document.getElementById('default-playlist-select');
    if (!sel) return;
    try {
        const [plsResp, defResp] = await Promise.all([
            fetch(`${API_BASE}/playlists`),
            fetch(`${API_BASE}/api/settings/default-playlist`)
        ]);
        const pls = await plsResp.json();
        const cur = (await defResp.json()).default_playlist || '';
        // Rebuild options (keep the leading "automatic" choice), then restore the saved selection.
        sel.length = 1;
        pls.forEach(p => {
            const o = document.createElement('option');
            o.value = p.name; o.textContent = `${p.name} (${p.artworks?.length || 0})`;
            sel.appendChild(o);
        });
        sel.value = cur;
    } catch (e) { /* non-fatal: panel just shows the automatic default */ }
}

async function saveDefaultPlaylist() {
    const sel = document.getElementById('default-playlist-select');
    const result = document.getElementById('default-playlist-result');
    const btn = document.getElementById('default-playlist-save-btn');
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = '⏳ Saving…'; result.textContent = '';
    try {
        const resp = await fetch(`${API_BASE}/api/settings/default-playlist`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ default_playlist: sel.value })
        });
        const data = await resp.json();
        if (!resp.ok) { result.textContent = '✗ ' + (data.detail || 'Save failed'); result.style.color = '#ef4444'; }
        else { result.textContent = sel.value ? `✓ Boots to “${sel.value}”` : '✓ Automatic'; result.style.color = '#34d399'; }
    } catch (e) { result.textContent = '✗ Network error'; result.style.color = '#ef4444'; }
    finally { btn.disabled = false; btn.textContent = orig; }
}

// --- Night & Quiet Hours (R1-F2) ---
const _SCHED_RANGES = {   // range input id -> live value-label id
    'sched-day-brightness': 'sched-day-b-val',
    'sched-night-brightness': 'sched-night-b-val',
    'sched-night-warmth': 'sched-warmth-val',
};
function _schedShowRangeVals() {
    for (const [inp, out] of Object.entries(_SCHED_RANGES)) {
        const i = document.getElementById(inp), o = document.getElementById(out);
        if (i && o) o.textContent = Math.round(parseFloat(i.value) * 100) + '%';
    }
}

async function loadNightSchedule() {
    const card = document.getElementById('night-schedule-card');
    if (!card) return;
    try {
        const s = await fetch(`${API_BASE}/api/settings/display-schedule`).then(r => r.json());
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
        document.getElementById('sched-enabled').checked = !!s.enabled;
        document.getElementById('sched-quiet-enabled').checked = !!s.quiet_enabled;
        set('sched-day-brightness', s.day_brightness);
        set('sched-night-brightness', s.night_brightness);
        set('sched-night-warmth', s.night_warmth);
        set('sched-evening-start', s.evening_start);
        set('sched-night-start', s.night_start);
        set('sched-morning-start', s.morning_start);
        set('sched-day-start', s.day_start);
        set('sched-quiet-start', s.quiet_start);
        set('sched-quiet-end', s.quiet_end);
        set('sched-quiet-mode', s.quiet_mode);
        _schedShowRangeVals();
        // Keep the % labels live as the sliders move.
        for (const inp of Object.keys(_SCHED_RANGES)) {
            const el = document.getElementById(inp);
            if (el) el.oninput = _schedShowRangeVals;
        }
    } catch (e) { /* non-fatal: the card just stays blank */ }
}

async function saveNightSchedule() {
    const btn = document.getElementById('sched-save-btn');
    const result = document.getElementById('sched-result');
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = '⏳ Saving…'; result.textContent = '';
    const v = id => document.getElementById(id).value;
    const payload = {
        enabled: document.getElementById('sched-enabled').checked,
        quiet_enabled: document.getElementById('sched-quiet-enabled').checked,
        day_brightness: parseFloat(v('sched-day-brightness')),
        night_brightness: parseFloat(v('sched-night-brightness')),
        night_warmth: parseFloat(v('sched-night-warmth')),
        evening_start: v('sched-evening-start'), night_start: v('sched-night-start'),
        morning_start: v('sched-morning-start'), day_start: v('sched-day-start'),
        quiet_start: v('sched-quiet-start'), quiet_end: v('sched-quiet-end'),
        quiet_mode: v('sched-quiet-mode'),
    };
    try {
        const resp = await fetch(`${API_BASE}/api/settings/display-schedule`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok) { result.textContent = '✗ ' + (data.detail || 'Save failed'); result.style.color = '#ef4444'; }
        else { result.textContent = '✓ Saved — displays update within a minute'; result.style.color = '#34d399'; }
    } catch (e) { result.textContent = '✗ Network error'; result.style.color = '#ef4444'; }
    finally { btn.disabled = false; btn.textContent = orig; }
}

// --- OpenRouter OAuth (PKCE) ---
function _b64url(bytes) {
    return btoa(String.fromCharCode(...new Uint8Array(bytes)))
        .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function _randomVerifier() {
    const arr = new Uint8Array(48);
    crypto.getRandomValues(arr);
    return _b64url(arr);
}
async function _pkceChallenge(verifier) {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
    return _b64url(digest);
}
async function startOpenRouterOAuth() {
    if (!window.isSecureContext) {
        showToast('One-click sign-in needs HTTPS or localhost — on a plain http://IP the browser blocks the required crypto. Paste an OpenRouter key instead, or open admin at localhost / over HTTPS.', 'error');
        return;
    }
    try {
        const verifier = _randomVerifier();
        const challenge = await _pkceChallenge(verifier);
        sessionStorage.setItem('sd_or_verifier', verifier);
        try { localStorage.setItem('sd_admin_view', 'settings'); } catch (e) {}
        const callback = window.location.origin + window.location.pathname;
        const resp = await fetch(`${API_BASE}/api/settings/ai/oauth/start?callback_url=${encodeURIComponent(callback)}&challenge=${encodeURIComponent(challenge)}`);
        const data = await resp.json();
        window.location.href = data.auth_url;
    } catch (e) {
        showToast('Could not start OpenRouter sign-in: ' + e, 'error');
    }
}
async function handleOAuthCallback() {
    const params = new URLSearchParams(window.location.search);
    const code = params.get('code');
    if (!code) return;
    const verifier = sessionStorage.getItem('sd_or_verifier');
    history.replaceState({}, document.title, window.location.pathname); // strip ?code= from URL
    if (!verifier) return;
    try {
        const resp = await fetch(`${API_BASE}/api/settings/ai/oauth/exchange`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code, verifier })
        });
        const data = await resp.json();
        if (resp.ok) showToast('Connected to OpenRouter ✓', 'success');
        else showToast('OpenRouter sign-in failed: ' + (data.detail || 'unknown error'), 'error');
    } catch (e) {
        showToast('OpenRouter exchange error: ' + e, 'error');
    } finally {
        sessionStorage.removeItem('sd_or_verifier');
    }
}

// -----------------------------------------------------------------------------
// Browse Catalog (curated public-domain art; lazy high-res on add)
// -----------------------------------------------------------------------------
function _esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Level 1: the collections grid (covers + counts). Items load only when a collection is opened.
// ===== Museum Art — unified Browse-Catalog (curated) + live Discover, behind one search box =====
// `museumScope` decides which source the grid shows: 'curated' (the bundled catalog) or 'live' (a
// live scout search whose hits go to the Review Queue). The two backends are untouched — this is
// purely the shared front door.
let museumScope = 'curated';

function enterMuseum() {
    // Entering or returning to the view: render whichever scope is active.
    setMuseumScope(museumScope, true);
}

function setMuseumScope(scope, forceRerender) {
    const changed = scope !== museumScope || forceRerender;
    museumScope = scope;
    document.getElementById('scope-curated').classList.toggle('active', scope === 'curated');
    document.getElementById('scope-live').classList.toggle('active', scope === 'live');
    // Live-only chrome: the repositories disclosure + the "goes to Review Queue" note.
    document.getElementById('museum-advanced').classList.toggle('hidden', scope !== 'live');
    document.getElementById('museum-live-note').classList.toggle('hidden', scope !== 'live');
    // Grid panes: curated browse/search lives in #catalog-container, live results in #discover-grid.
    document.getElementById('catalog-container').classList.toggle('hidden', scope !== 'curated');
    document.getElementById('discover-grid').classList.toggle('hidden', scope !== 'live');
    if (scope !== 'live') document.getElementById('load-more-btn').style.display = 'none';
    const btn = document.getElementById('museum-search-btn');
    if (btn) btn.textContent = scope === 'live' ? 'Search museums' : 'Search';
    if (!changed) return;
    if (scope === 'curated') {
        const q = (document.getElementById('scout-search').value || '').trim();
        if (q) renderCuratedSearch(q); else renderCatalog();
    } else {
        renderDiscoveryGrid();  // show whatever's already queued; new hits arrive via refreshData()
    }
}

// Autocomplete (BTW #1): suggest distinct catalog titles + artist names as the user types, via the
// native <datalist>. Debounced so a fast typist doesn't fire a request per keystroke.
let _suggestTimer = null;
function museumSuggest(value) {
    clearTimeout(_suggestTimer);
    const q = (value || '').trim();
    if (q.length < 2) { document.getElementById('catalog-suggest').innerHTML = ''; return; }
    _suggestTimer = setTimeout(async () => {
        try {
            const data = await (await fetch(`${API_BASE}/api/catalog/suggest?q=${encodeURIComponent(q)}`)).json();
            document.getElementById('catalog-suggest').innerHTML =
                (data.suggestions || []).map(s => `<option value="${_esc(s)}"></option>`).join('');
        } catch (e) { /* suggestions are best-effort — never block typing */ }
    }, 180);
}

function museumSearch() {
    const q = (document.getElementById('scout-search').value || '').trim();
    if (museumScope === 'live') {
        if (!q) { showToast('Type something to search the live museums.', 'error'); return; }
        dispatchScouts();
    } else {
        if (q) renderCuratedSearch(q); else renderCatalog();
    }
}

// Prominent "go live" call-to-action, shown when the curated catalog has few/no matches for a
// query. This is escalation, not a blend: curated stays instant-first, and the same query is
// handed to the live scouts only when the user asks. #scout-search still holds q, so the
// setMuseumScope('live') → museumSearch() handoff carries the query verbatim — one motion.
const MUSEUM_THIN_THRESHOLD = 3;  // ≤ this many curated hits → surface the prominent live CTA
function museumEscalateCTA(q) {
    return `
        <div style="margin-top:18px; padding:16px 18px; background:rgba(59,130,246,0.08);
                    border:1px solid var(--accent-color); border-radius:10px; display:flex;
                    flex-wrap:wrap; align-items:center; gap:12px; justify-content:space-between;">
            <div style="font-size:0.85rem; color:var(--text-color);">
                Not in the collection? Search the world's live museum APIs for “${_esc(q)}”.
            </div>
            <button class="success" onclick="setMuseumScope('live'); museumSearch(); return false;"
                    style="white-space:nowrap;">🌐 Search live museums →</button>
        </div>`;
}

// Flat curated search across all collections (server-side), rendered with the same card + add-path
// as a single collection — each result carries its own collection_id + item_index.
async function renderCuratedSearch(q) {
    if (catalogSelectMode) exitCatalogSelect();  // clean slate when navigating between catalog views
    const container = document.getElementById('catalog-container');
    if (!container) return;
    container.innerHTML = '<p style="color:#94a3b8;">Searching the catalog…</p>';
    try {
        const data = await (await fetch(`${API_BASE}/api/catalog/search?q=${encodeURIComponent(q)}`)).json();
        const results = data.results || [];
        const head = document.createElement('div');
        head.style.cssText = 'margin: 4px 0 16px;';
        head.innerHTML = `
            <button class="secondary" onclick="clearMuseumSearch()" style="font-size:0.75rem; padding:6px 12px; margin-bottom:10px;">← All collections</button>
            <div style="font-size:0.85rem; color:#94a3b8; display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <span>${results.length} curated ${results.length === 1 ? 'match' : 'matches'} for “${_esc(q)}”.</span>
                <a href="#" onclick="setMuseumScope('live'); museumSearch(); return false;" style="color:var(--accent-color);">Search live museums →</a>
                ${results.length ? `<button class="secondary catalog-select-btn" onclick="toggleCatalogSelect()" style="font-size:0.72rem; padding:5px 10px; margin-left:auto;">☑ Select</button>` : ''}
            </div>`;
        container.innerHTML = '';
        container.appendChild(head);
        if (results.length) {
            const grid = document.createElement('div');
            grid.className = 'artwork-grid';
            results.forEach(it => {
                const card = document.createElement('div');
                card.className = 'artwork-card';
                const added = !!it.added;
                card.dataset.cid = it.collection_id;
                card.dataset.idx = it.item_index;
                card.dataset.cidx = `${it.collection_id}:${it.item_index}`;
                if (added) card.dataset.added = '1';
                card.innerHTML = `
                    <img loading="lazy" src="${_esc(it.thumbnail_url)}" alt="${_esc(it.title)}" style="background:#0f172a;">
                    <div class="info">
                        <strong>${_esc(it.title || 'Untitled')}</strong><br>
                        <small>${_esc(it.agent_name || 'Unknown')}</small><br>
                        <small style="opacity:0.6">${_esc(it.collection_title || '')}</small>
                    </div>
                    <div class="actions">
                        <button class="success" ${added ? 'disabled' : ''} onclick="addCatalogItem('${_esc(it.collection_id)}', ${it.item_index}, this)">${added ? 'Added ✓' : 'Add to Library'}</button>
                    </div>`;
                grid.appendChild(card);
            });
            container.appendChild(grid);
            // Thin curated coverage → make the live escalation a real CTA, not just the header link.
            if (results.length <= MUSEUM_THIN_THRESHOLD) {
                const cta = document.createElement('div');
                cta.innerHTML = museumEscalateCTA(q);
                container.appendChild(cta.firstElementChild);
            }
        } else {
            const none = document.createElement('div');
            none.innerHTML = `<p style="color:#94a3b8; margin:0 0 4px;">No curated works match “${_esc(q)}”.</p>` + museumEscalateCTA(q);
            container.appendChild(none);
        }
    } catch (e) {
        console.error('[Museum] curated search failed:', e);
        container.innerHTML = '<p style="color:#ef4444;">Search failed.</p>';
    }
}

function clearMuseumSearch() {
    document.getElementById('scout-search').value = '';
    renderCatalog();
}

// Populate the Museum Art (catalog) badge. Shared by renderCatalog and the init-time loader so the
// count is correct on first paint, not only after the user opens the Museum view.
function _setCatalogCount(collections) {
    const total = (collections || []).reduce((n, c) => n + (c.count || 0), 0);
    const el = document.getElementById('catalog-count');
    if (el) el.textContent = total;
}

// Fetch just enough to show the Museum Art count on a fresh admin load (the badge otherwise sat at 0
// until the user clicked into the Museum view, which is what populated it).
async function loadCatalogCount() {
    try {
        const index = await (await fetch(`${API_BASE}/api/catalog`)).json();
        _setCatalogCount(index.collections || []);
    } catch (e) { /* leave the placeholder 0 */ }
}

// Populate the My Photos badge so it's consistent with the other nav counts. /api/studio/photos
// returns a ready `count` of personal photos.
async function loadPhotosCount() {
    const el = document.getElementById('photos-count');
    if (!el) return;
    try {
        const data = await (await fetch(`${API_BASE}/api/studio/photos`)).json();
        el.textContent = data.count || 0;
    } catch (e) { /* leave the placeholder 0 */ }
}

async function renderCatalog() {
    if (catalogSelectMode) exitCatalogSelect();  // clean slate when navigating between catalog views
    // Page-load restore: re-open the collection that was open at refresh time instead of drawing
    // the grid. Consumed once so later calls to renderCatalog() (e.g. the "All collections" back
    // button) behave normally.
    if (pendingCatalogRestore) {
        const restoreId = pendingCatalogRestore;
        pendingCatalogRestore = null;
        return openCatalogCollection(restoreId);
    }
    // Showing the collections grid means we're no longer inside a specific collection.
    try { localStorage.removeItem('sd_admin_catalog_collection'); } catch (e) {}
    const container = document.getElementById('catalog-container');
    if (!container) return;
    container.innerHTML = '<p style="color:#94a3b8;">Loading catalog…</p>';
    try {
        const index = await (await fetch(`${API_BASE}/api/catalog`)).json();
        const collections = index.collections || [];
        _setCatalogCount(collections);

        if (!collections.length) {
            container.innerHTML = '<p style="color:#94a3b8;">No catalog collections are loaded. Screen Docent normally ships with a curated catalog — meanwhile, the <strong>Discover</strong> tab can search the world\'s museums directly.</p>';
            return;
        }
        const grid = document.createElement('div');
        grid.className = 'artwork-grid';
        collections.forEach(col => {
            const card = document.createElement('div');
            card.className = 'artwork-card';
            card.style.cursor = 'pointer';
            // "Start Here" — the curated Masterpieces collection is the paintings-only fame-sorted
            // on-ramp into the catalog; index.json already lists it first, this just makes that visible.
            const isStartHere = col.id === 'masterpieces';
            // A tiny collection (e.g. 2-item Ukiyo-e) reads as broken next to 30-work neighbors —
            // de-emphasize, never hide, so it doesn't look like a UI bug.
            const isSmall = (col.count || 0) > 0 && col.count < 5;
            if (isStartHere) card.classList.add('catalog-start-here');
            card.onclick = () => openCatalogCollection(col.id);
            card.innerHTML = `
                <img loading="lazy" src="${_esc(col.cover_thumbnail)}" alt="${_esc(col.title)}" style="background:#0f172a;">
                <div class="info">
                    ${isStartHere ? '<span class="badge start-here-badge">★ Start Here</span><br>' : ''}
                    <strong>${_esc(col.title)}</strong> ${trustBadge(col.origin, col.trust)}<br>
                    <small style="opacity:0.7">${_esc(col.description || '')}</small><br>
                    <small style="color:var(--accent-color)">${col.count} works →</small>
                    ${isSmall ? ' <span class="badge few-works-badge">small set</span>' : ''}
                </div>`;
            grid.appendChild(card);
        });
        container.innerHTML = '';
        container.appendChild(grid);
    } catch (e) {
        console.error('[Catalog] index load failed:', e);
        container.innerHTML = '<p style="color:#ef4444;">Failed to load catalog.</p>';
    }
}

// Level 2: one collection's items (lazy — only this collection's thumbnails load).
async function openCatalogCollection(collectionId) {
    if (catalogSelectMode) exitCatalogSelect();  // clean slate when navigating between catalog views
    // Remember the open collection so a browser refresh returns to it instead of resetting to the
    // collections grid (mirrors sd_admin_playlist for the LIBRARY playlist sidebar).
    try { localStorage.setItem('sd_admin_catalog_collection', String(collectionId)); } catch (e) {}
    const container = document.getElementById('catalog-container');
    if (!container) return;
    container.innerHTML = '<p style="color:#94a3b8;">Loading…</p>';
    try {
        const resp = await fetch(`${API_BASE}/api/catalog/${encodeURIComponent(collectionId)}`);
        if (!resp.ok) {
            // Stale/deleted collection id — self-heal so a refresh doesn't keep retrying it.
            try { localStorage.removeItem('sd_admin_catalog_collection'); } catch (e) {}
            container.innerHTML = '<p style="color:#ef4444;">Collection not found.</p>';
            return;
        }
        const col = await resp.json();
        const items = col.items || [];

        const head = document.createElement('div');
        head.style.cssText = 'margin-bottom:16px;';
        head.innerHTML = `
            <button class="secondary" onclick="renderCatalog()" style="font-size:0.75rem; padding:6px 12px; margin-bottom:10px;">← All collections</button>
            <h3 style="margin:6px 0 2px; color:var(--text-color); font-size:1.05rem;">${_esc(col.title)} ${trustBadge(col.origin, col.trust)}</h3>
            <span style="font-size:0.78rem; color:#94a3b8;">${_esc(col.description || '')}</span>
            <span style="display:block; font-size:0.68rem; color:#64748b; margin-top:4px;">${_esc(col.source || '')}${col.license ? ' · ' + _esc(col.license) : ''}</span>
            <div style="margin-top:12px; display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <label style="font-size:0.78rem; color:#94a3b8;">Add to:</label>
                <select id="catalog-target" style="background:#0f172a; border:1px solid var(--border-color); color:var(--text-color); padding:6px 10px; border-radius:6px; font-size:0.8rem;">
                    <option value="">Library only</option>
                    ${(currentPlaylists || []).map(p => `<option value="${p.id}">${_esc(p.name)}</option>`).join('')}
                </select>
                ${items.length ? `<button class="secondary catalog-select-btn" onclick="toggleCatalogSelect()" style="font-size:0.75rem; padding:6px 12px; margin-left:auto;">☑ Select</button>` : ''}
            </div>`;

        const grid = document.createElement('div');
        grid.className = 'artwork-grid';
        items.forEach((it, idx) => {
            const card = document.createElement('div');
            card.className = 'artwork-card';
            const added = !!it.added;
            // Items arrive fame-sorted (featured_rank desc), so the array position `idx` is NOT the
            // index /api/catalog/add expects — the server stamps the original position as
            // `item_index` on every item for exactly this reason. Fall back to `idx` only if it's
            // ever missing (e.g. an older cached response).
            const itemIdx = it.item_index ?? idx;
            card.dataset.cid = col.id;
            card.dataset.idx = itemIdx;
            card.dataset.cidx = `${col.id}:${itemIdx}`;
            if (added) card.dataset.added = '1';
            card.innerHTML = `
                <img loading="lazy" src="${_esc(it.thumbnail_url)}" alt="${_esc(it.title)}" style="background:#0f172a;">
                <div class="info">
                    <strong>${_esc(it.title || 'Untitled')}</strong><br>
                    <small>${_esc(it.agent_name || 'Unknown')}</small><br>
                    <small style="opacity:0.6">${_esc(it.date_display || '')}</small>
                </div>
                <div class="actions">
                    <button class="success" ${added ? 'disabled' : ''} onclick="addCatalogItem('${_esc(col.id)}', ${itemIdx}, this)">${added ? 'Added ✓' : 'Add to Library'}</button>
                </div>`;
            grid.appendChild(card);
        });
        container.innerHTML = '';
        container.appendChild(head);
        container.appendChild(grid);
    } catch (e) {
        console.error('[Catalog] collection load failed:', e);
        container.innerHTML = '<p style="color:#ef4444;">Failed to load collection.</p>';
    }
}

async function addCatalogItem(collectionId, itemIndex, btn) {
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = '⏳ Adding…';
    const dest = document.getElementById('catalog-target');
    const playlistId = dest && dest.value ? parseInt(dest.value, 10) : null;
    const payload = { collection_id: collectionId, item_index: itemIndex };
    if (playlistId) payload.playlist_id = playlistId;
    try {
        const resp = await fetch(`${API_BASE}/api/catalog/add`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok) {
            showToast(data.detail || 'Add failed', 'error');
            btn.disabled = false; btn.textContent = orig;
            return;
        }
        btn.textContent = 'Added ✓';
        const dest = document.getElementById('catalog-target');
        const destName = dest && dest.value ? dest.options[dest.selectedIndex].text : 'the Library';
        showToast(`Added to ${destName} ✓`, 'success');
        fetchLibrary(); // refresh the Full Library count in the background
    } catch (e) {
        showToast('Network error adding artwork.', 'error');
        btn.disabled = false; btn.textContent = orig;
    }
}

// ===========================================================================
// Publisher Studio — folded-in view (was static/publish.html). All state +
// helpers are pub*-prefixed and only initialized via enterPublisher(), so they
// never bleed into the Personal (My Photos) or Subscriptions flows. Uses the
// shared showToast / confirmModal / promptModal / _esc instead of its old
// bespoke toast + native confirm/prompt/alert.
// ===========================================================================
const _pq = s => document.querySelector(s);
const PUB_LICENSES = ["", "CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "PD", "proprietary"];
let pubCollections = [];
let pubCurrent = null;
let pubIdentity = { has_private_key: false };
let pubValidateTimer = null;

function enterPublisher() { pubLoadIdentity(); pubLoadCollections(); }

// Un-hide the Publisher nav only once an identity is configured (mirrors the Devices capability gate).
async function initPublisherCapability() {
    const btn = document.getElementById('nav-publisher');
    if (!btn) return;
    try {
        const id = await (await fetch(`${API_BASE}/api/publisher/identity`)).json();
        if (id && id.id) btn.style.display = '';
    } catch (e) { /* leave hidden */ }
}

function pubLicenseOptions(sel, selected) {
    sel.innerHTML = PUB_LICENSES.map(l => `<option value="${l}">${l || '— none —'}</option>`).join('');
    sel.value = selected || "";
}

async function pubLoadIdentity() {
    try { pubIdentity = await (await fetch(`${API_BASE}/api/publisher/identity`)).json(); } catch (e) { return; }
    _pq('#pub-id').value = pubIdentity.id || '';
    _pq('#pub-name').value = pubIdentity.name || '';
    _pq('#pub-url').value = pubIdentity.url || '';
    const st = _pq('#pub-identity-status');
    if (pubIdentity.has_private_key) {
        st.className = 'status ok'; st.textContent = '🔑 signing key ready';
        _pq('#pub-pubkey').textContent = pubIdentity.public_key ? ('public key: ' + pubIdentity.public_key) : '';
        _pq('#pub-regen-key').classList.remove('hidden');
    } else {
        st.className = 'status'; st.textContent = 'No signing key yet — saving identity generates one.';
        _pq('#pub-pubkey').textContent = ''; _pq('#pub-regen-key').classList.add('hidden');
    }
}

async function pubRegenKey() {
    if (await confirmModal('Regenerate your signing key? This invalidates the signature on anything you already published.', { confirmText: 'Regenerate', danger: true }))
        pubSaveIdentity(true);
}

async function pubSaveIdentity(regenerate) {
    const body = { id: _pq('#pub-id').value.trim(), name: _pq('#pub-name').value.trim(), url: _pq('#pub-url').value.trim(), regenerate };
    if (!body.id || !body.name) { showToast('Publisher ID and name are required', 'error'); return; }
    try {
        const r = await fetch(`${API_BASE}/api/publisher/identity`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const d = await r.json();
        if (!r.ok) { showToast(d.detail || 'Could not save identity', 'error'); return; }
        pubIdentity = d; await pubLoadIdentity();
        if (d.warning) { showToast('⚠ key rotated', 'error'); await confirmModal(d.warning, { confirmText: 'OK' }); }
        else showToast('✓ identity saved', 'success');
        const btn = document.getElementById('nav-publisher'); if (btn) btn.style.display = '';   // reveal the tab now
    } catch (e) { showToast('Could not save identity', 'error'); }
}

async function pubLoadCollections(selectId) {
    try { pubCollections = await (await fetch(`${API_BASE}/api/publisher/collections`)).json(); } catch (e) { pubCollections = []; }
    const sel = _pq('#pub-collection-sel');
    sel.innerHTML = pubCollections.length ? '' : '<option value="">— no collections yet —</option>';
    pubCollections.forEach(c => { const o = document.createElement('option'); o.value = c.id; o.textContent = `${c.title} (${c.item_count})`; sel.appendChild(o); });
    if (selectId) { sel.value = String(selectId); await pubSelectCollection(selectId); }
    else if (pubCollections.length) { sel.value = String(pubCollections[0].id); await pubSelectCollection(pubCollections[0].id); }
    else { pubCurrent = null; pubRenderEditor(); }
}

async function pubSelectCollection(id) {
    if (!id) { pubCurrent = null; pubRenderEditor(); return; }
    try { pubCurrent = await (await fetch(`${API_BASE}/api/publisher/collections/${id}`)).json(); } catch (e) { return; }
    pubRenderEditor();
}

async function pubNewCollection() {
    const title = (await promptModal('Collection title') || '').trim();
    if (!title) return;
    try {
        const r = await fetch(`${API_BASE}/api/publisher/collections`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title, items: [] }) });
        const d = await r.json();
        if (!r.ok) { showToast(d.detail || 'Could not create', 'error'); return; }
        await pubLoadCollections(d.id);
    } catch (e) { showToast('Could not create collection', 'error'); }
}

async function pubDeleteCollection() {
    if (!pubCurrent) return;
    if (!(await confirmModal(`Delete collection "${pubCurrent.title}"? This cannot be undone.`, { confirmText: 'Delete', danger: true }))) return;
    await fetch(`${API_BASE}/api/publisher/collections/${pubCurrent.id}`, { method: 'DELETE' });
    await pubLoadCollections();
}

function pubRenderEditor() {
    const has = !!pubCurrent;
    _pq('#pub-collection-editor').classList.toggle('hidden', !has);
    _pq('#pub-items-panel').hidden = !has;
    _pq('#pub-del-collection').style.display = has ? '' : 'none';
    if (!has) return;
    _pq('#c-title').value = pubCurrent.title || '';
    _pq('#c-slug').value = pubCurrent.slug || '';
    _pq('#c-desc').value = pubCurrent.description || '';
    _pq('#c-cover').value = pubCurrent.cover_image || '';
    pubLicenseOptions(_pq('#c-license'), pubCurrent.default_license);
    pubRenderItems();
    pubScheduleValidate();
}

function pubRenderItems() {
    const wrap = _pq('#pub-items'); wrap.innerHTML = '';
    (pubCurrent.items || []).forEach((it, i) => wrap.appendChild(pubItemCard(it, i)));
}

function pubAddItem() {
    pubCurrent.items = pubCurrent.items || [];
    pubCurrent.items.push({ title: '', image: { full_url: '' } });
    pubRenderItems(); pubScheduleValidate();
}

function pubItemCard(it, idx) {
    const img = it.image || (it.image = {});
    const el = document.createElement('div'); el.className = 'item-card';
    const fp = img.focal_point;
    el.innerHTML = `
    <div class="thumb-wrap ${img.full_url ? '' : 'empty'}">
      ${img.full_url ? `<img alt="preview" src="${_esc(img.full_url)}">` : 'paste an image URL below to preview'}
      <span class="focal-dot" ${fp ? '' : 'hidden'}></span>
      <span class="focal-hint">${img.full_url ? 'tap the image to set the focus point' : ''}</span>
    </div>
    <div class="item-body">
      <div class="item-head"><span class="t">Artwork ${idx + 1}</span>
        <button class="secondary remove" style="color:#ef4444">Remove</button></div>
      <label class="field">Public image URL <span class="muted">(hosted by you)</span>
        <input type="url" class="f-url" placeholder="https://yoursite/art/piece.jpg" value="${_esc(img.full_url || '')}"></label>
      <span class="dims"></span>
      <div class="row">
        <label class="field">Title <input type="text" class="f-title" value="${_esc(it.title || '')}"></label>
        <label class="field">Artist <input type="text" class="f-artist" value="${_esc(it.artist || '')}"></label>
      </div>
      <div class="row">
        <label class="field">Date <input type="text" class="f-date" placeholder="1889" value="${_esc(it.date || '')}"></label>
        <label class="field">Medium <input type="text" class="f-medium" value="${_esc(it.medium || '')}"></label>
        <label class="field">Culture / movement <input type="text" class="f-culture" value="${_esc(it.culture || '')}"></label>
      </div>
      <label class="field">Placard <span class="muted">(short blurb on the display)</span>
        <textarea class="f-placard">${_esc(it.placard || '')}</textarea></label>
      <label class="field">Tags <span class="muted">(comma-separated)</span>
        <input type="text" class="f-tags" value="${_esc((it.tags || []).join(', '))}"></label>
      <div class="row">
        <label class="field">License <select class="f-license"></select></label>
        <label class="field f-attr-wrap">Attribution <span class="muted">(required for CC-BY*)</span>
          <input type="text" class="f-attribution" value="${_esc(img.attribution || '')}"></label>
        <label class="field">Rights holder <input type="text" class="f-rights" value="${_esc(img.rights_holder || '')}"></label>
      </div>
    </div>`;
    const lic = el.querySelector('.f-license');
    pubLicenseOptions(lic, img.license || pubCurrent.default_license);
    const wrapEl = el.querySelector('.thumb-wrap');
    const dot = el.querySelector('.focal-dot');
    const dims = el.querySelector('.dims');
    if (fp) { dot.style.left = (fp[0] * 100) + '%'; dot.style.top = (fp[1] * 100) + '%'; }
    if (img.width && img.height) dims.textContent = `${img.width}×${img.height}px`;
    // URL change → client-side preview + auto width/height (bytes never round-trip through us)
    el.querySelector('.f-url').addEventListener('change', e => {
        const url = e.target.value.trim(); img.full_url = url;
        if (!url) { pubRenderItems(); return; }
        const probe = new Image();
        probe.onload = () => { img.width = probe.naturalWidth; img.height = probe.naturalHeight; pubRenderItems(); pubScheduleValidate(); };
        probe.onerror = () => { dims.textContent = '⚠ could not load a preview (the URL is still saved)'; pubScheduleValidate(); };
        probe.src = url;
    });
    // tap-to-set focal point
    wrapEl.addEventListener('click', ev => {
        if (!img.full_url) return;
        const r = wrapEl.getBoundingClientRect();
        const fx = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
        const fy = Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height));
        img.focal_point = [pubRound3(fx), pubRound3(fy)];
        dot.hidden = false; dot.style.left = (fx * 100) + '%'; dot.style.top = (fy * 100) + '%';
    });
    const bind = (cls, set) => el.querySelector(cls).addEventListener('change', e => { set(e.target.value); pubScheduleValidate(); });
    bind('.f-title', v => it.title = v);
    bind('.f-artist', v => it.artist = v);
    bind('.f-date', v => it.date = v);
    bind('.f-medium', v => it.medium = v);
    bind('.f-culture', v => it.culture = v);
    bind('.f-placard', v => it.placard = v);
    bind('.f-tags', v => it.tags = v.split(',').map(t => t.trim()).filter(Boolean));
    bind('.f-license', v => img.license = v);
    bind('.f-attribution', v => img.attribution = v);
    bind('.f-rights', v => img.rights_holder = v);
    el.querySelector('.remove').onclick = () => { pubCurrent.items.splice(idx, 1); pubRenderItems(); pubScheduleValidate(); };
    return el;
}

function pubCollectPayload() {
    return {
        slug: _pq('#c-slug').value.trim() || null,
        title: _pq('#c-title').value.trim(),
        description: _pq('#c-desc').value.trim() || null,
        cover_image: _pq('#c-cover').value.trim() || null,
        default_license: _pq('#c-license').value || null,
        items: (pubCurrent.items || []).map(it => {
            const img = it.image || {};
            return { id: it.id || null, title: it.title || '', artist: it.artist || null,
                artist_role: it.artist_role || null, date: it.date || null, creation_date: it.creation_date || null,
                medium: it.medium || null, culture: it.culture || null,
                tags: (it.tags && it.tags.length) ? it.tags : null, placard: it.placard || null,
                full_url: img.full_url || '', thumbnail_url: img.thumbnail_url || null,
                license: img.license || null, attribution: img.attribution || null,
                rights_holder: img.rights_holder || null,
                width: img.width || null, height: img.height || null,
                focal_point: img.focal_point || null };
        })
    };
}

async function pubSaveCollection() {
    if (!pubCurrent) return false;
    const r = await fetch(`${API_BASE}/api/publisher/collections/${pubCurrent.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(pubCollectPayload()) });
    const d = await r.json();
    if (!r.ok) { showToast(d.detail || 'Save failed', 'error'); return false; }
    // Keep the LOCAL model authoritative — reassigning pubCurrent to the response would replace every
    // item object and detach the rendered cards' closures (silent edit loss). Only sync server-derived
    // fields back onto the existing object.
    pubCurrent.slug = d.slug; _pq('#c-slug').value = d.slug;
    const opt = [..._pq('#pub-collection-sel').options].find(o => o.value === String(pubCurrent.id));
    if (opt) opt.textContent = `${pubCurrent.title} (${(pubCurrent.items || []).length})`;
    return true;
}

async function pubSaveCollectionClick() { if (await pubSaveCollection()) showToast('✓ saved', 'success'); }

function pubScheduleValidate() { clearTimeout(pubValidateTimer); pubValidateTimer = setTimeout(pubValidateNow, 600); }

async function pubValidateNow() {
    if (!pubCurrent) return;
    if (!(await pubSaveCollection())) return;   // validation runs against the persisted draft
    try {
        const d = await (await fetch(`${API_BASE}/api/publisher/collections/${pubCurrent.id}/validate`, { method: 'POST' })).json();
        const v = _pq('#pub-validation');
        if (d.valid) { v.innerHTML = '✓ <span style="color:var(--success-color)">valid — ready to export</span>'; }
        else { v.innerHTML = `⚠ ${d.errors.length} issue(s):<ul>${d.errors.map(e => `<li>${_esc(e)}</li>`).join('')}</ul>`; }
    } catch (e) {}
}

async function pubExport() {
    if (!pubCurrent) return;
    if (!pubIdentity.has_private_key) { showToast('Set up your publisher identity first', 'error'); return; }
    if (!(await pubSaveCollection())) return;
    const r = await fetch(`${API_BASE}/api/publisher/collections/${pubCurrent.id}/export`, { method: 'POST' });
    if (!r.ok) {
        let detail; try { detail = (await r.json()).detail; } catch (e) {}
        if (Array.isArray(detail)) { _pq('#pub-validation').innerHTML = `⚠ fix before export:<ul>${detail.map(e => `<li>${_esc(e)}</li>`).join('')}</ul>`; showToast('Manifest invalid — see issues', 'error'); }
        else showToast(detail || 'Export failed', 'error');
        return;
    }
    const blob = await r.blob();
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = `${pubCurrent.slug}.json`; document.body.appendChild(a); a.click();
    a.remove(); URL.revokeObjectURL(a.href);
    showToast('✓ exported & signed', 'success');
}

function pubRound3(n) { return Math.round(n * 1000) / 1000; }
