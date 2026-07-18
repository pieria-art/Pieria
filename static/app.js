/**
 * Artwork Display Engine - Frontend Client (app.js)
 * Phase 4: Targeted WebSocket Routing for Multiple Displays.
 */

// 1. Digital Signage Rotation Logic
const urlParams = new URLSearchParams(window.location.search);
if (urlParams.get('rotate') === 'true') {
    document.body.classList.add('force-portrait');
}

// 2. True Fullscreen Trigger
document.addEventListener('click', (e) => {
    // C4: don't yank to fullscreen when the user is operating the controls/placard (❮ ❯, the playlist
    // dropdown, etc.) — only a click on the bare canvas should request it.
    if (e.target.closest('#controls') || e.target.closest('#placard')) return;
    if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(err => {
            console.warn(`[Client] Fullscreen failed: ${err.message}`);
        });
    }
}, { once: false });

// C3: keyboard / TV-remote (Fire TV, Android TV d-pad) control — the Canvas had no key handlers, so a
// mouse was required. ←/→ advance, Enter/OK reveals placard + controls, Esc hides them.
document.addEventListener('keydown', (e) => {
    switch (e.key) {
        case 'ArrowRight': startDisplayCycleManually(1); break;
        case 'ArrowLeft': startDisplayCycleManually(-1); break;
        case 'Enter': case ' ': showPlacard(8000); showControls(6000); break;
        case 'Escape':
            document.body.classList.remove('placard-visible', 'controls-visible');
            break;
        default: return;
    }
    e.preventDefault();
});

const API_BASE = (window.location.origin === 'null' || window.location.protocol === 'file:') 
    ? 'http://localhost:8000' 
    : window.location.origin;

// 3. Targeted WebSocket Endpoint
// Connects to /ws/[display_id] based on ?display= URL parameter
const DISPLAY_ID = urlParams.get('display') || 'default';
const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/${DISPLAY_ID}`;

// Hide the mouse cursor on the display Canvas — reveal it briefly on movement, then
// re-hide. A kiosk never moves the mouse, so the cursor stays gone (no stray pointer
// sitting on the TV); a real viewer still gets it back the moment they wiggle the mouse.
(() => {
    let t;
    const hide = () => { if (document.body) document.body.style.cursor = 'none'; };
    const show = () => {
        if (document.body) document.body.style.cursor = '';
        clearTimeout(t);
        t = setTimeout(hide, 3000);
    };
    window.addEventListener('mousemove', show, { passive: true });
    window.addEventListener('DOMContentLoaded', hide);
    hide();
})();

// Global Defaults & URL Overrides
const globalConfig = {
    cycle_time: parseInt(urlParams.get('cycle_time')) || null,
    mode: urlParams.get('mode') || null,
    placard_wait: parseInt(urlParams.get('placard_wait')) || null,
    placard_show: parseInt(urlParams.get('placard_show')) || null,
    placard_manual: parseInt(urlParams.get('placard_manual')) || null,
    shuffle: urlParams.get('shuffle') !== null ? urlParams.get('shuffle') === 'true' : null
};

const DEFAULT_SETTINGS = {
    cycle_time: 30,
    mode: 'ken-burns',
    shuffle: false,
    placard_wait: 5,
    placard_show: 15,
    placard_manual: 10
};

let currentPlaylist = '';
let currentImageIndex = null;
let activeLayerId = 1;
let firstLoad = true;
let displayMode = 'ken-burns'; 
let placardTimeout = null;
let controlsTimeout = null;
let currentImageUrl = '';
let currentDisplayTime = 30000; 
let currentCropData = null;
let currentFocal = null;
let cycleTimeout = null;
let currentPlaylists = [];
let socket = null;

// Telemetry State
let activeArtworkId = null;
let activeImageStartTime = 0;

async function init() {

    const requestedMode = urlParams.get('mode');
    const validModes = ['ken-burns', 'static-crop', 'contain-matte'];
    if (requestedMode && validModes.includes(requestedMode)) {
        displayMode = requestedMode;
    }

    setupUIInteraction();
    initModeToggles();
    initNavButtons();
    initCustomDropdown();
    connectWS();

    showManageHint();

    initNightSchedule();

    await refreshPlaylists(true);
    setInterval(() => refreshPlaylists(false), 15000);
}

// R1-F2: Night & Quiet Hours. The server resolves the current brightness/warmth/quiet from the wall
// clock (hierarchy lives there); the Canvas just applies a GPU-cheap CSS veil. `?schedule=off` opts a
// given display out (dev-rule #4); `?now=HH:MM` (also honored server-side) is handy for demos/time-lapse.
function applyScheduleState(state) {
    const warm = document.getElementById('night-warm');
    const dim = document.getElementById('night-dim');
    const black = document.getElementById('quiet-blackout');
    if (!warm || !dim || !black) return;
    if (!state || !state.enabled) {   // feature off -> fully neutral
        warm.style.opacity = '0'; dim.style.opacity = '0'; black.style.opacity = '0';
        return;
    }
    warm.style.opacity = String(state.warmth || 0);
    dim.style.opacity = String(1 - (state.brightness ?? 1));   // brightness 1 -> no dim
    // Always software-blackout during quiet hours; on the appliance HDMI-CEC also powers the panel
    // off (this is the fallback that works on any TV / off-Pi).
    black.style.opacity = state.quiet ? '1' : '0';
}

async function refreshScheduleState() {
    try {
        const nowOverride = urlParams.get('now');
        const url = `${API_BASE}/api/displays/${encodeURIComponent(DISPLAY_ID)}/schedule-state`
            + (nowOverride ? `?now=${encodeURIComponent(nowOverride)}` : '');
        applyScheduleState(await fetch(url).then(r => r.json()));
    } catch (e) { /* transient — the next poll retries; art keeps showing */ }
}

function initNightSchedule() {
    if (urlParams.get('schedule') === 'off') return;   // dev/per-display opt-out
    refreshScheduleState();
    setInterval(refreshScheduleState, 60000);          // re-resolve each minute for the slow ramps
}

// Briefly point a first-time viewer at the admin, then fade out (so it's invisible on a wall).
function showManageHint() {
    const hint = document.getElementById('manage-hint');
    const url = document.getElementById('manage-url');
    if (!hint || !url) return;
    url.textContent = `${window.location.host}/admin`;
    hint.style.display = 'block';
    setTimeout(() => { hint.style.opacity = '0'; }, 7000);
    setTimeout(() => { hint.style.display = 'none'; }, 8000);
}

// Show/hide the "no art yet" overlay (replaces a silent black screen).
function setEmptyState(show) {
    const el = document.getElementById('empty-state');
    if (!el) return;
    if (show) {
        const u = document.getElementById('empty-admin-url');
        if (u) u.textContent = `${window.location.host}/admin`;
        el.classList.remove('hidden');
    } else {
        el.classList.add('hidden');
    }
}

/**
 * Initializes Targeted WebSocket connection.
 */
function connectWS() {
    socket = new WebSocket(WS_URL);

    socket.onmessage = async (event) => {
        try {
            const msg = JSON.parse(event.data);

            switch (msg.action) {
                case 'set_playlist':
                    handleRemotePlaylistSwitch(msg.playlist);
                    break;
                case 'set_mode':
                    if (msg.mode) {
                        setMode(msg.mode);
                        updateModeButtonUI();
                    }
                    break;
                case 'next_image':
                    startDisplayCycleManually(1);
                    break;
                case 'prev_image':
                    startDisplayCycleManually(-1);
                    break;
                case 'show_placard':
                    if (placardTimeout) clearTimeout(placardTimeout);
                    const manualShowTime = globalConfig.placard_manual !== null ? globalConfig.placard_manual : (currentPlaylistData?.placard_manual !== undefined ? currentPlaylistData.placard_manual : DEFAULT_SETTINGS.placard_manual);
                    showPlacard(manualShowTime * 1000);
                    break;
                default:
                    console.warn('[Client] Unknown action:', msg.action);
            }
        } catch (err) {
            console.error('[Client] Message Parse Error:', err);
        }
    };

    socket.onclose = () => {
        console.warn('[Client] Hub connection lost. Retrying in 5s...');
        setTimeout(connectWS, 5000);
    };
}

async function handleRemotePlaylistSwitch(name) {
    const p = currentPlaylists.find(pl => pl.name === name);
    if (!p) return;

    currentPlaylist = name;
    currentDisplayTime = p.display_time * 1000;
    currentImageIndex = null; 
    
    updateDropdownLabel(p.name, p.artworks?.length || 0);
    showPlaylistTitle(currentPlaylist);
    startDisplayCycle();
}

/**
 * UI & Interaction Logic
 */
function setupUIInteraction() {
    const getManualTime = () => {
        return (globalConfig.placard_manual !== null ? globalConfig.placard_manual : (currentPlaylistData?.placard_manual !== undefined ? currentPlaylistData.placard_manual : DEFAULT_SETTINGS.placard_manual)) * 1000;
    };

    document.addEventListener('mousemove', (e) => {
        showPlacard(getManualTime());
        const isRotated = document.body.classList.contains('force-portrait');
        if (isRotated) {
            const threshold = window.innerWidth * 0.7; 
            if (e.clientX > threshold) showControls(10000);
        } else {
            const threshold = window.innerHeight * 0.7;
            if (e.clientY > threshold) showControls(10000);
        }
    });

    document.addEventListener('mousedown', (e) => {
        showPlacard(getManualTime());
        const isRotated = document.body.classList.contains('force-portrait');
        if (isRotated) {
            const threshold = window.innerWidth * 0.7;
            if (e.clientX > threshold) showControls(10000);
        } else {
            const threshold = window.innerHeight * 0.7;
            if (e.clientY > threshold) showControls(10000);
        }
    });
}

function showPlacard(duration) {
    document.body.classList.add('placard-visible');
    if (placardTimeout) clearTimeout(placardTimeout);
    placardTimeout = setTimeout(() => { document.body.classList.remove('placard-visible'); }, duration);
}

function showControls(duration) {
    document.body.classList.add('controls-visible');
    if (controlsTimeout) clearTimeout(controlsTimeout);
    controlsTimeout = setTimeout(() => {
        const options = document.getElementById('playlist-options');
        const isOptionsOpen = !options.classList.contains('hidden');
        const controls = document.getElementById('controls');
        const isHovering = controls.matches(':hover');
        if (!isOptionsOpen && !isHovering) {
            document.body.classList.remove('controls-visible');
        } else {
            showControls(2000); 
        }
    }, duration);
}

function initCustomDropdown() {
    const trigger = document.getElementById('playlist-current');
    const options = document.getElementById('playlist-options');
    trigger.addEventListener('click', (e) => { e.stopPropagation(); options.classList.toggle('hidden'); });
    document.addEventListener('click', () => { options.classList.add('hidden'); });
}

async function refreshPlaylists(isInitial = false) {
    try {
        const response = await fetch(`${API_BASE}/playlists`);
        const playlists = await response.json();
        if (playlists.length === 0) {
            setEmptyState(true);   // C7: no collections at all → show guidance, never a silent black screen
            return;
        }
        if (playlists.length > 0) {
            currentPlaylists = playlists;
            populatePlaylistSelect(playlists);
            if (isInitial) {
                const requestedPlaylistName = urlParams.get('playlist');
                let activePlaylist = playlists.find(p => p.name === requestedPlaylistName);
                if (!activePlaylist) {
                    // No explicit ?playlist= — ask the server what THIS display should resume
                    // (last-played → configured default), then fall back to the first non-empty.
                    let preferredName = null;
                    try {
                        const pref = await fetch(`${API_BASE}/api/displays/${encodeURIComponent(DISPLAY_ID)}/preferred-playlist`).then(r => r.json());
                        preferredName = pref.playlist;
                    } catch (e) { /* offline/early boot — fall through to default below */ }
                    activePlaylist = playlists.find(p => p.name === preferredName)
                                  || playlists.find(p => (p.artworks?.length || 0) > 0) || playlists[0];
                }
                currentPlaylist = activePlaylist.name;
                currentDisplayTime = activePlaylist.display_time * 1000;
                updateDropdownLabel(activePlaylist.name, activePlaylist.artworks?.length || 0);
                updateModeButtonUI();
                showPlaylistTitle(currentPlaylist);
                startDisplayCycle();
            }
        }
    } catch (error) { console.error('[Client] Sync Failed:', error); }
}

function updateModeButtonUI() {
    const modeMap = { 'ken-burns': 'mode-a', 'static-crop': 'mode-b', 'contain-matte': 'mode-c' };
    const activeBtnId = modeMap[displayMode];
    document.querySelectorAll('.mode-toggles button').forEach(btn => btn.classList.remove('active'));
    const btn = document.getElementById(activeBtnId);
    if (btn) btn.classList.add('active');
    document.getElementById('display-container').className = displayMode;
}

function updateDropdownLabel(name, count) {
    document.getElementById('playlist-current').textContent = `${name} (${count})`;
}

function populatePlaylistSelect(playlists) {
    const optionsContainer = document.getElementById('playlist-options');
    optionsContainer.innerHTML = '';
    playlists.forEach(p => {
        const div = document.createElement('div');
        div.className = `dropdown-option ${p.name === currentPlaylist ? 'active' : ''}`;
        div.textContent = `${p.name} (${p.artworks?.length || 0})`;
        div.onclick = (e) => {
            e.stopPropagation();
            currentPlaylist = p.name;
            currentDisplayTime = p.display_time * 1000;
            currentImageIndex = null;
            updateDropdownLabel(p.name, p.artworks?.length || 0);
            optionsContainer.classList.add('hidden');
            showPlaylistTitle(currentPlaylist);
            startDisplayCycle();
            document.querySelectorAll('.dropdown-option').forEach(el => el.classList.remove('active'));
            div.classList.add('active');
        };
        optionsContainer.appendChild(div);
    });
}

async function sendTelemetry(artworkId, startTime, skipped) {
    if (!artworkId || !startTime) return;
    const displayTimeSec = Math.round((Date.now() - startTime) / 1000);
    if (displayTimeSec < 1) return; // Ignore extreme rapid skipping or double-fires
    
    try {
        await fetch(`${API_BASE}/api/telemetry/heartbeat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                artwork_id: artworkId,
                display_time_sec: displayTimeSec,
                skipped: skipped
            })
        });
    } catch (e) {
        console.warn('[Telemetry] Failed to send heartbeat:', e.message);
    }
}

// E1: a monotonic generation guards against overlapping transitions. The WS onmessage handler is not
// awaited, so a burst (next_image/prev_image) plus the auto-cycle timer can launch concurrent
// fetchAndTransition calls; without this the slower response clobbers newer state and each caller
// schedules its own setTimeout, leaving an orphan timer that fires an extra advance.
let cycleGen = 0;

async function startDisplayCycle() {
    if (cycleTimeout) clearTimeout(cycleTimeout);
    const gen = await fetchAndTransition(1, false);
    if (gen === cycleGen) cycleTimeout = setTimeout(startDisplayCycle, currentDisplayTime);  // only the latest reschedules
}

let currentPlaylistData = null;

async function fetchAndTransition(direction = 1, isSkipped = false) {
    if (!currentPlaylist) return cycleGen;
    const gen = ++cycleGen;   // this call is now the latest; any older in-flight call will bail below

    // Telemetry: Record completion of previous image before fetching next
    if (activeArtworkId) {
        sendTelemetry(activeArtworkId, activeImageStartTime, isSkipped);
    }

    try {
        const params = new URLSearchParams({ 
            playlist_name: currentPlaylist, 
            display_id: DISPLAY_ID,
            direction: direction
        });
        
        // Only append shuffle if it was explicitly overridden in the URL
        if (globalConfig.shuffle !== null) {
            params.append('shuffle', globalConfig.shuffle.toString());
        }

        const response = await fetch(`${API_BASE}/next-image?${params.toString()}`);
        if (gen !== cycleGen) return gen;                   // a newer transition started — don't clobber it
        if (!response.ok) { setEmptyState(true); return gen; }  // no approved images → guidance, not black
        const data = await response.json();
        if (gen !== cycleGen) return gen;
        setEmptyState(false);

        currentPlaylistData = data;
        currentImageIndex = data.index;
        currentImageUrl = `${API_BASE}${data.image_url}`;
        currentCropData = data.crop;
        currentFocal = data.focal_point || { x: 0.5, y: 0.5 };
        
        // Update Telemetry state for the newly fetched image
        activeArtworkId = data.metadata.id;
        activeImageStartTime = Date.now();
        
        // Resolve Settings Hierarchy (URL > Playlist > Global Default)
        const cycleTime = globalConfig.cycle_time || data.display_time || DEFAULT_SETTINGS.cycle_time;
        const resolvedMode = globalConfig.mode || data.default_mode || DEFAULT_SETTINGS.mode;
        const resolvedShuffle = data.shuffle; // Use the truth from the backend
        
        currentDisplayTime = cycleTime * 1000;
        if (displayMode !== resolvedMode) {
            setMode(resolvedMode);
            updateModeButtonUI();
        }

        updatePlacard(data.metadata);
        performCrossfade(currentImageUrl, data.crop, currentFocal);

        // Automatic Placard Flow
        const waitTime = globalConfig.placard_wait !== null ? globalConfig.placard_wait : (data.placard_wait !== undefined ? data.placard_wait : DEFAULT_SETTINGS.placard_wait);
        const showTime = globalConfig.placard_show !== null ? globalConfig.placard_show : (data.placard_show !== undefined ? data.placard_show : DEFAULT_SETTINGS.placard_show);
        
        showPlacardFlow(waitTime, showTime);

    } catch (error) { console.error('[Client] Transition Error:', error.message); }
    return gen;
}

function showPlacardFlow(waitSec, showSec) {
    if (placardTimeout) clearTimeout(placardTimeout);
    document.body.classList.remove('placard-visible');
    
    placardTimeout = setTimeout(() => {
        document.body.classList.add('placard-visible');
        placardTimeout = setTimeout(() => {
            document.body.classList.remove('placard-visible');
        }, showSec * 1000);
    }, waitSec * 1000);
}

// C1: AI enrichment emits Markdown emphasis (e.g. "*The Irish Question*"); placard fields are plain
// textContent, so the markers render literally. Strip the common inline emphasis to plain prose.
function stripMd(s) {
    if (!s) return '';
    return String(s)
        .replace(/\*\*([^*]+)\*\*/g, '$1')
        .replace(/\*([^*]+)\*/g, '$1')
        .replace(/__([^_]+)__/g, '$1')
        .replace(/_([^_]+)_/g, '$1')
        .replace(/`([^`]+)`/g, '$1')
        .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
        .replace(/^#{1,6}\s+/gm, '');
}

function updatePlacard(metadata) {
    const placard = document.getElementById('placard');
    if (!metadata || !metadata.title) { placard.classList.add('hidden'); return; }
    placard.classList.remove('hidden');

    const isPersonal = !!metadata.is_personal;
    placard.classList.toggle('personal', isPersonal);

    document.getElementById('art-title').textContent = stripMd(metadata.title);
    const seriesEl = document.getElementById('art-series');
    const agentDate = document.getElementById('art-agent-date');
    const museumDetails = document.getElementById('art-museum-details');
    const description = document.getElementById('art-description');
    const tagsContainer = document.getElementById('art-tags');
    const qrContainer = document.getElementById('qrcode-container');
    const qrEl = document.getElementById('qrcode');
    tagsContainer.innerHTML = '';
    qrEl.innerHTML = '';

    if (isPersonal) {
        // Personal photo: just the caption + an optional date. No artist/medium/culture/museum
        // jargon, no series/tags, and no "Learn More" QR (there's nothing to look up).
        seriesEl.style.display = 'none';
        agentDate.textContent = metadata.date_display || metadata.creation_date || '';
        museumDetails.textContent = '';
        description.textContent = '';
        qrContainer.style.display = 'none';
        return;
    }

    qrContainer.style.display = '';
    // Series/set subtitle (e.g. a ukiyo-e "Famous Places…"), shown under the title only when present.
    seriesEl.style.display = metadata.series ? '' : 'none';
    seriesEl.textContent = stripMd(metadata.series || '');
    agentDate.textContent = `${metadata.agent_name || 'Unknown Artist'} ${metadata.agent_role && metadata.agent_role !== 'Artist' ? '(' + metadata.agent_role + ')' : ''} ${metadata.creation_date ? '• ' + metadata.creation_date : ''}`;

    // C2: drop date_display from the details row when it just repeats the byline's creation_date
    // (the common "… • 1503-1519" byline + "… | 1503-1519" details double-print).
    const dd = metadata.date_display;
    const dupDate = dd && dd.toLowerCase() === (metadata.creation_date || '').toLowerCase();
    const details = [metadata.cultural_context, metadata.medium, dupDate ? null : dd].filter(Boolean).join(' | ');
    museumDetails.textContent = details;
    description.textContent = stripMd(metadata.description || '');
    if (metadata.tags) {
        metadata.tags.split(',').forEach(tag => {
            const span = document.createElement('span');
            span.textContent = tag.trim();
            tagsContainer.appendChild(span);
        });
    }
    // Point at our own server-hosted detail page (works offline; no Google hand-off).
    const learnUrl = metadata.id
        ? `${window.location.origin}/art/${metadata.id}`
        : `https://www.google.com/search?q=${encodeURIComponent((metadata.agent_name || '') + ' ' + (metadata.title || ''))}`;
    new QRCode(qrEl, {
        text: learnUrl,
        width: 80, height: 80, colorDark : "#000000", colorLight : "#ffffff", correctLevel : QRCode.CorrectLevel.H
    });
}

function performCrossfade(imageUrl, cropData, focal) {
    const targetLayerId = activeLayerId === 1 ? 2 : 1;
    const activeLayer = document.getElementById(`artwork-${activeLayerId}`);
    const targetLayer = document.getElementById(`artwork-${targetLayerId}`);
    const img = new Image();
    img.src = imageUrl;
    img.onload = () => {
        const matteLayer = document.getElementById('matte-layer');
        if (displayMode === 'contain-matte') matteLayer.style.backgroundImage = `url('${imageUrl}')`;
        targetLayer.style.backgroundImage = `url('${imageUrl}')`;
        applyModeStyles(targetLayer, img, cropData, focal);
        targetLayer.classList.add('active');
        activeLayer.classList.remove('active');
        activeLayerId = targetLayerId;
        firstLoad = false;
        // C5: don't yank the controls out from under a viewer who's mid-interaction when the cycle
        // advances — keep them up while the playlist dropdown is open or the controls are hovered.
        const options = document.getElementById('playlist-options');
        const controls = document.getElementById('controls');
        const isOptionsOpen = options && !options.classList.contains('hidden');
        const isHovering = controls && controls.matches(':hover');
        if (!isOptionsOpen && !isHovering) {
            document.body.classList.remove('controls-visible');
        }
    };
}

function applyModeStyles(element, img, cropData, focal) {
    const fx = (focal && typeof focal.x === 'number') ? focal.x : 0.5;
    const fy = (focal && typeof focal.y === 'number') ? focal.y : 0.5;
    const hasValidCrop = cropData && cropData.width > 1;

    // Stop any Ken Burns animation left running on this layer from a previous render.
    if (element._kbAnim) { element._kbAnim.cancel(); element._kbAnim = null; }

    if (displayMode === 'ken-burns') {
        startKenBurns(element, fx, fy);
        return;
    }
    if (displayMode === 'static-crop' && hasValidCrop) {
        const zoomX = (img.naturalWidth / cropData.width) * 100;
        const zoomY = (img.naturalHeight / cropData.height) * 100;
        const posX = (cropData.x / (img.naturalWidth - cropData.width)) * 100 || 0;
        const posY = (cropData.y / (img.naturalHeight - cropData.height)) * 100 || 0;
        element.style.backgroundSize = `${zoomX}% ${zoomY}%`;
        element.style.backgroundPosition = `${posX}% ${posY}%`;
        element.style.transform = 'none';
        element.style.transformOrigin = '';
    } else {
        element.style.backgroundSize = displayMode === 'contain-matte' ? 'contain' : 'cover';
        // Anchor a plain cover-crop on the focal point too, so an off-center subject isn't sliced
        // on a mismatched aspect ratio. (contain shows the whole image, so it stays centered.)
        element.style.backgroundPosition = displayMode === 'contain-matte'
            ? 'center'
            : `${(fx * 100).toFixed(1)}% ${(fy * 100).toFixed(1)}%`;
        element.style.transform = 'none';
        element.style.transformOrigin = '';
    }
}

// Focal-adaptive Ken Burns, driven per-image via the Web Animations API (replaces a fixed CSS
// keyframe). The pan/zoom anchors on the artwork's focal point, and the DRIFT scales with how
// central that point is — full cinematic drift for centered subjects, near-pure zoom for edge
// subjects, so a portrait's head is never panned out of frame. Default focal (0.5, 0.5) ⇒ the
// prior centered zoom-and-drift.
function startKenBurns(element, fx, fy) {
    const SCALE = 1.12, BASE_DRIFT = 4;           // zoom depth + max drift (% of layer)
    const cx = 1 - 2 * Math.abs(fx - 0.5);        // centrality: 1 at center → 0 at the edge
    const cy = 1 - 2 * Math.abs(fy - 0.5);
    const ox = (fx * 100).toFixed(1), oy = (fy * 100).toFixed(1);
    const dx = (-BASE_DRIFT * cx).toFixed(2), dy = (-BASE_DRIFT * cy).toFixed(2);
    element.style.backgroundSize = 'cover';
    element.style.backgroundPosition = `${ox}% ${oy}%`;
    element.style.transformOrigin = `${ox}% ${oy}%`;
    element.style.transform = 'none';
    element._kbAnim = element.animate(
        [
            { transform: 'scale(1) translate(0%, 0%)' },
            { transform: `scale(${SCALE}) translate(${dx}%, ${dy}%)` },
        ],
        { duration: 45000, iterations: Infinity, direction: 'alternate', easing: 'linear' }
    );
}

function initNavButtons() {
    document.getElementById('prev-btn').addEventListener('click', () => startDisplayCycleManually(-1));
    document.getElementById('next-btn').addEventListener('click', () => startDisplayCycleManually(1));
}

async function startDisplayCycleManually(direction) {
    if (cycleTimeout) clearTimeout(cycleTimeout);
    const gen = await fetchAndTransition(direction, true); // true = user skipped
    if (gen === cycleGen) cycleTimeout = setTimeout(startDisplayCycle, currentDisplayTime);  // only the latest reschedules
}

function initModeToggles() {
    const modeButtons = { 'ken-burns': document.getElementById('mode-a'), 'static-crop': document.getElementById('mode-b'), 'contain-matte': document.getElementById('mode-c') };
    Object.entries(modeButtons).forEach(([mode, btn]) => {
        btn.addEventListener('click', () => { setMode(mode); Object.values(modeButtons).forEach(b => b.classList.remove('active')); btn.classList.add('active'); });
    });
}

function setMode(mode) {
    displayMode = mode;
    document.getElementById('display-container').className = mode;
    const activeLayer = document.getElementById(`artwork-${activeLayerId}`);
    const activeImg = new Image();
    const urlMatch = activeLayer.style.backgroundImage.match(/url\(['"]?(.*?)['"]?\)/);
    if (urlMatch && urlMatch[1]) {
        activeImg.src = urlMatch[1];
        activeImg.onload = () => applyModeStyles(activeLayer, activeImg, currentCropData, currentFocal);
    }
    const matteLayer = document.getElementById('matte-layer');
    if (mode === 'contain-matte') {
        matteLayer.classList.remove('hidden');
        if (currentImageUrl) matteLayer.style.backgroundImage = `url('${currentImageUrl}')`;
    } else {
        matteLayer.classList.add('hidden');
    }
}

function showPlaylistTitle(title) {
    const overlay = document.getElementById('overlay');
    const titleEl = document.getElementById('playlist-title');
    titleEl.textContent = title;
    overlay.classList.add('show');
    setTimeout(() => overlay.classList.remove('show'), 5000);
}

document.addEventListener('DOMContentLoaded', init);
