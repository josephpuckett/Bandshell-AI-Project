// ============================= State =============================
let currentView = "events";
let currentEventId = null;
let currentWebsiteId = null;
let eventsCache = [];
let websitesCache = [];
let selectedEventId = "";
let eventsSig = "";
let lastBuiltSig = "";
let actionsSig = "";
let scriptsSig = "";
let editingEventId = null;
let editingWebsiteId = null;
let sitesSig = "";
let eventsListSig = "";
let logWid = null;              // which website's messages are currently in #wsLog
let lastLogLen = {};            // website id -> number of messages already rendered
const pollers = {};

// ============================= Helpers =============================
async function fetchJSON(url, opts) {
    const res = await fetch(url, opts);
    if (!res.ok) {
        const body = await res.text().catch(() => "");
        throw new Error(`${res.status} ${res.statusText} ${body}`);
    }
    return res.json();
}

function escapeHtml(s) {
    return String(s ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function formatDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
}

function badge(className, text) {
    return `<span class="badge ${className}">${escapeHtml(text)}</span>`;
}

const FIELD_LABELS = {
    title: "Title", short_summary: "Short summary", long_summary: "Long summary",
    categories: "Categories", image: "Image", start_date: "Start", end_date: "End",
    venue: "Venue", location: "Location", open_to_public: "Open to public",
    event_cost: "Cost", website_url: "Website", organizer: "Organizer",
    organizer_first_name: "First name", organizer_last_name: "Last name",
    organizer_phone_number: "Phone", organizer_email_address: "Email",
};

// ============================= View switching =============================
const viewEventsBtn = document.getElementById("viewEventsBtn");
const viewWebsitesBtn = document.getElementById("viewWebsitesBtn");

viewEventsBtn.onclick = () => switchView("events");
viewWebsitesBtn.onclick = () => switchView("websites");

function switchView(view) {
    currentView = view;
    stopPolling();
    closeBrowserStream();
    viewEventsBtn.classList.toggle("active", view === "events");
    viewWebsitesBtn.classList.toggle("active", view === "websites");
    document.getElementById("eventsView").hidden = view !== "events";
    document.getElementById("websitesView").hidden = view !== "websites";
    if (view === "websites") {
        loadWebsites();
        if (currentWebsiteId) openWebsite(currentWebsiteId);
    } else if (currentEventId) {
        openEvent(currentEventId);
    }
}

// ============================= Events =============================
async function loadEvents() {
    const events = await fetchJSON("/api/events");
    eventsCache = events;
    eventsSig = events.map(e => e.id).join(",");
    const list = document.getElementById("eventsList");
    // Same guard as the websites list: don't wipe/recreate rows unless the
    // displayed fields actually changed (preserves hover/focus/selection).
    const sig = events.map(e => [e.id, e.title, e.start_date,
                                 e.id === currentEventId ? "1" : "0"].join("|")).join(";");
    if (sig === eventsListSig && list.childElementCount > 0) return;
    eventsListSig = sig;
    list.innerHTML = "";
    document.getElementById("eventCount").textContent = events.length;
    document.getElementById("emptyEvents").hidden = events.length > 0;

    for (const ev of events) {
        const wrap = document.createElement("div");
        wrap.className = "list-item-wrap";
        const btn = document.createElement("button");
        btn.className = "list-item" + (ev.id === currentEventId ? " active" : "");
        btn.innerHTML = `
            <span class="item-title">${escapeHtml(ev.title)}</span>
            <span class="item-sub">${escapeHtml(formatDate(ev.start_date))}</span>
        `;
        btn.onclick = () => openEvent(ev.id);
        const del = document.createElement("button");
        del.className = "list-del";
        del.title = "Delete event";
        del.textContent = "×";
        del.onclick = async () => await deleteEvent(ev.id, ev.title);
        wrap.appendChild(btn);
        wrap.appendChild(del);
        list.appendChild(wrap);
    }
    fillEventSelects(); // reflect any newly added events in website dropdowns
}

async function deleteEvent(id, title) {
    if (!confirm(`Delete event "${title}"? This cannot be undone.`)) return;
    try {
        await fetchJSON(`/api/events/${id}`, { method: "DELETE" });
        if (currentEventId === id) {
            currentEventId = null;
            document.getElementById("eventPanel").hidden = true;
        }
        await loadEvents();
    } catch (err) { alert("Failed to delete event: " + err.message); }
}

async function openEvent(id) {
    currentEventId = id;
    stopPolling();
    const ev = await fetchJSON(`/api/events/${id}`);
    document.getElementById("eventTitle").textContent = ev.title;
    document.getElementById("eventDates").textContent = `${formatDate(ev.start_date)} – ${formatDate(ev.end_date)}`;

    const order = ["short_summary", "long_summary", "categories", "image",
        "start_date", "end_date", "venue", "location", "open_to_public",
        "event_cost", "website_url", "organizer", "organizer_first_name",
        "organizer_last_name", "organizer_phone_number", "organizer_email_address"];

    const detail = document.getElementById("eventDetail");
    detail.innerHTML = "";
    for (const key of order) {
        if (!(key in ev)) continue;
        let value = ev[key];
        if (key === "categories" && Array.isArray(value)) value = value.join(", ");
        if (key === "open_to_public") value = value ? "Yes" : "No";
        if (value === "" || value === null || value === undefined) value = "—";
        const isImage = key === "image" && value !== "—";
        const div = document.createElement("div");
        div.className = "field" + (key === "long_summary" ? " full" : "");
        div.innerHTML = `
            <span class="k">${escapeHtml(FIELD_LABELS[key] || key)}</span>
            <span class="v">${isImage ? `<a href="${escapeHtml(value)}" target="_blank" rel="noopener">${escapeHtml(value)}</a>` : escapeHtml(value)}</span>
        `;
        detail.appendChild(div);
    }
    document.getElementById("eventPanel").hidden = false;
    await loadEvents();
}

function stopPolling() {
    if (pollers.website) { clearInterval(pollers.website); pollers.website = null; }
}

function renderMessage(msg) {
    const div = document.createElement("div");
    div.className = "msg " + (msg.role || "unknown");
    let body = "";
    if (Array.isArray(msg.content)) {
        body = msg.content.map(c => c.type === "text" ? c.text : "[image]").join("\n");
    } else {
        body = msg.content ?? "";
    }
    let whoText = msg.role;
    if (msg.role === "tool") whoText = (msg.name || msg.tool_name || "tool");
    div.innerHTML = `<span class="who">${escapeHtml(whoText)}</span><div class="body">${escapeHtml(body)}</div>`;
    return div;
}

// ============================= Websites =============================
async function loadWebsites() {
    const data = await fetchJSON("/api/websites");
    websitesCache = data;
    document.getElementById("websiteCount").textContent = data.length;
    const list = document.getElementById("websitesList");
    // Skip the innerHTML wipe unless the visible rows actually changed — this is
    // what stops the hover/„active” effects on the sidebar from replaying on the
    // 1s poll. (Log/action changes aren't reflected in the list, so they don't
    // force a rebuild.)
    const sig = data.map(w => [w.id, w.name, w.kind, w.status, w.stale,
                               w.id === currentWebsiteId ? "1" : "0"].join("|")).join(";");
    if (sig === sitesSig && list.childElementCount > 0) return;
    sitesSig = sig;
    list.innerHTML = "";
    document.getElementById("emptyWebsites").hidden = data.length > 0;
    for (const w of data) {
        const wrap = document.createElement("div");
        wrap.className = "list-item-wrap";
        const btn = document.createElement("button");
        btn.className = "list-item website-row" + (w.id === currentWebsiteId ? " active" : "");
        btn.innerHTML = `
            <span class="item-title">${escapeHtml(w.name)}</span>
            <span class="item-sub">${badge(w.kind === "eventbrite" ? "eventbrite" : "agentic", w.kind === "eventbrite" ? "Eventbrite" : "Uses AI")} ${badge(w.status, friendlyStatusLabel(w))} ${w.stale ? badge("stale", "Stale") : ""}</span>
        `;
        btn.onclick = () => openWebsite(w.id);
        const del = document.createElement("button");
        del.className = "list-del";
        del.title = "Delete website";
        del.textContent = "×";
        del.onclick = async () => await deleteWebsite(w.id, w.name);
        wrap.appendChild(btn);
        wrap.appendChild(del);
        list.appendChild(wrap);
    }
}

async function deleteWebsite(id, name) {
    if (!confirm(`Delete website "${name}"? This cannot be undone.`)) return;
    try {
        await fetchJSON(`/api/websites/${id}`, { method: "DELETE" });
        if (currentWebsiteId === id) {
            currentWebsiteId = null;
            document.getElementById("websitePanel").hidden = true;
            closeBrowserStream();
            stopPolling();
        }
        await loadWebsites();
    } catch (err) { alert("Failed to delete website: " + err.message); }
}

async function openWebsite(id) {
    currentWebsiteId = id;
    stopPolling();
    document.getElementById("websitePanel").hidden = false;
    await renderWebsite();
    pollers.website = setInterval(async () => {
        if (currentWebsiteId !== id) return;
        try { await renderWebsite(); } catch (_) { }
    }, 1000);
    const w = websitesCache.find(x => x.id === id);
    if (w && w.kind !== "eventbrite") openBrowserStream(id);
}

// ============================= Embedded browser view =============================
// Streams JPEG frames of the live page over WebSocket (server-configured rate,
// default ~4fps) and proxies the human's clicks/typing back to the real
// Playwright page while interactive.

let browserWs = null;
let browserStreamingWid = null;
let browserVW = 0, browserVH = 0; // live page viewport size (natural frame dims)

function browserWsUrl(id) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/api/browser/${id}/ws`;
}

function openBrowserStream(id) {
    closeBrowserStream();
    browserStreamingWid = id;
    const hint = document.getElementById("browserHint");
    hint.textContent = "Connecting to the live page…";
    try {
        browserWs = new WebSocket(browserWsUrl(id));
    } catch (_) {
        hint.textContent = "Live view unavailable (WebSocket failed to connect).";
        return;
    }
    browserWs.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_) { return; }
        const stage = document.getElementById("browserStage");
        const frame = document.getElementById("browserFrame");
        const ph = document.getElementById("browserPlaceholder");
        const int = Boolean(msg.interactive);
        const phase = msg.phase || "";
        if (msg.type === "frame") {
            browserVW = msg.vw || 0;
            browserVH = msg.vh || 0;
            frame.src = "data:image/jpeg;base64," + msg.data;
            ph.hidden = true;
            stage.classList.remove("placeholder");
            setBrowserInteractive(int, phase);
        } else if (msg.type === "empty") {
            browserVW = 0; browserVH = 0;
            frame.removeAttribute("src");
            ph.hidden = false;
            ph.textContent = "No live page right now — it appears after onboarding starts or a script runs.";
            stage.classList.add("placeholder");
            browserInteractive = false;
            document.getElementById("browserHint").innerHTML =
                "The live page appears here once onboarding starts or a script runs.";
            document.getElementById("browserDoneBtn").classList.add("off");
        } else {
            // ping heartbeat — keep the hint state accurate between frames
            setBrowserInteractive(int, phase);
        }
    };
    browserWs.onclose = (ev) => {
        const why = ev && (ev.code || ev.reason)
            ? ` (code ${ev.code}${ev.reason ? `: ${ev.reason}` : ""})` : "";
        browserWs = null;
        if (browserStreamingWid === id) {
            document.getElementById("browserHint").textContent =
                `Live view disconnected — reconnecting…${why}`;
            setTimeout(() => { if (browserStreamingWid === id) openBrowserStream(id); }, 2000);
        }
    };
    browserWs.onerror = () => {
        if (browserStreamingWid === id) {
            document.getElementById("browserHint").textContent =
                `Couldn't reach the live browser over WebSocket (${browserWsUrl(id)}). `
                + "If you're behind a reverse proxy/tunnel, it may not be forwarding WebSocket upgrades.";
        }
    };
}

function closeBrowserStream() {
    browserStreamingWid = null;
    if (browserWs) { try { browserWs.close(); } catch (_) { } browserWs = null; }
    document.getElementById("browserFrame").removeAttribute("src");
    const ph = document.getElementById("browserPlaceholder");
    ph.hidden = true;
    document.getElementById("browserStage").classList.remove("placeholder");
    setBrowserInteractive(false, "");
}

let browserInteractive = false;
function setBrowserInteractive(v, phase) {
    browserInteractive = v;
    const hint = document.getElementById("browserHint");
    const done = document.getElementById("browserDoneBtn");
    if (v) {
        hint.innerHTML = "It's your turn — click &amp; type to fix (Ctrl/Cmd+V pastes from your "
            + "clipboard), then <b>submit the form here</b>.";
        // During onboarding's approval phases the human uses Approve/Reject in
        // the Actions panel — the Done button belongs to scripted-run review.
        const onboardingApproval = phase === "manual_approval" || phase === "script_approval";
        done.classList.toggle("off", onboardingApproval);
    } else {
        hint.innerHTML = "AI is working on this page — you can watch live. Editing is available once it pauses for you.";
        done.classList.add("off");
    }
}

function browserSend(ev) {
    if (!browserWs || browserWs.readyState !== WebSocket.OPEN) return;
    // Scrolls are harmless while the agent works (view-only viewing), so they
    // bypass the interactive gate; clicks/type/key still require it.
    if (ev.type !== "scroll" && !browserInteractive) return;
    browserWs.send(JSON.stringify(ev));
}

// Paste the OS clipboard into the focused field of the live page.
async function doPaste() {
    if (!browserWs || browserWs.readyState !== WebSocket.OPEN) return;
    let text = "";
    try { text = await navigator.clipboard.readText(); } catch (_) { }
    if (!text) {
        document.getElementById("browserHint").textContent =
            "Clipboard empty or blocked (allow clipboard access for this site, then try again).";
        return;
    }
    browserSend({ type: "paste", text: text.slice(0, 10000) });
}

function browserCoord(e) {
    const frame = document.getElementById("browserFrame");
    const r = frame.getBoundingClientRect();
    // Map screen coords on the (possibly scaled) image back to viewport px.
    // Prefer the loaded image's intrinsic pixel dimensions (naturalWidth/Height)
    // — they ARE the screenshot size, so the mapping is exact even if the
    // page's reported viewport disagrees (DPI/zoom). Fall back to vw/vh.
    const nw = frame.naturalWidth || browserVW;
    const nh = frame.naturalHeight || browserVH;
    const sx = nw > 0 && r.width > 0 ? r.width / nw : 1;
    const sy = nh > 0 && r.height > 0 ? r.height / nh : 1;
    return { x: (e.clientX - r.left) / sx, y: (e.clientY - r.top) / sy };
}

// Click / double-click / scroll -> page proxy.
(function () {
    const stage = document.getElementById("browserStage");
    const streamOpen = () => browserWs && browserWs.readyState === WebSocket.OPEN;
    stage.addEventListener("mousedown", () => { if (browserInteractive) stage.focus(); });
    stage.addEventListener("click", (e) => {
        const { x, y } = browserCoord(e);
        browserSend({ type: "click", x, y });
    });
    stage.addEventListener("wheel", (e) => {
        if (!streamOpen()) return; // no live page: let the app page scroll normally
        e.preventDefault();
        const { x, y } = browserCoord(e);
        // Normalize per-notch wheel deltas (Firefox: 3, Safari: 40) to roughly
        // pixel units so a single notch scrolls a sensible amount everywhere.
        const rawDx = e.deltaX || 0, rawDy = e.deltaY || 0;
        const notch = Math.abs(rawDy) < 20 && Math.abs(rawDy) > 0 ? rawDy * 40 : rawDy;
        const notchX = Math.abs(rawDx) < 20 && Math.abs(rawDx) > 0 ? rawDx * 40 : rawDx;
        browserSend({ type: "scroll", x, y,
                      dx: Math.round(notchX), dy: Math.round(notch) });
    }, { passive: false });
    stage.addEventListener("keydown", (e) => {
        // Ctrl/Cmd+V over the live page → paste OS clipboard into the focused
        // field. Only hijack it while interactive, so the app's own inputs
        // (e.g. the reject-reason box) still get normal Ctrl+V behavior.
        if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "v" || e.key === "V")) {
            if (streamOpen() && browserInteractive) { e.preventDefault(); doPaste(); }
            return;
        }
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        e.preventDefault();
        // Named keys map 1:1 to Playwright keyboard keys.
        const named = { "Enter": "Enter", "Tab": "Tab", "Backspace": "Backspace",
            "Delete": "Delete", "Escape": "Escape", "Space": " ",
            "ArrowUp": "ArrowUp", "ArrowDown": "ArrowDown", "ArrowLeft": "ArrowLeft",
            "ArrowRight": "ArrowRight", "Home": "Home", "End": "End",
            "PageUp": "PageUp", "PageDown": "PageDown" };
        if (named[e.key]) {
            if (named[e.key] === " ") browserSend({ type: "type", text: " " });
            else browserSend({ type: "key", key: named[e.key] });
        } else if (e.key && e.key.length === 1) {
            browserSend({ type: "type", text: e.key });
        }
    });
    // Also catch paste triggered from context menus / some platforms where the
    // browser doesn't deliver a keydown (Cmd+V on macOS usually does, but this
    // is belt & braces).
    stage.addEventListener("paste", (e) => {
        if (!streamOpen() || !browserInteractive) return;
        e.preventDefault();
        doPaste();
    });
})();

// "Done" closes the review page (like the old 'close the tab' instruction).
document.getElementById("browserDoneBtn").onclick = async () => {
    if (!browserStreamingWid) return;
    try {
        await fetchJSON(`/api/websites/${browserStreamingWid}/done`, { method: "POST" });
        document.getElementById("browserDoneBtn").classList.add("off");
    } catch (err) { alert("Failed to finish review: " + err.message); }
};

function friendlyStatus(w) {
    // Plain-language summary of a website's current state for non-technical users.
    if (w.kind === "eventbrite") {
        return "Add events to send them as drafts to Eventbrite.";
    }
    if (w.status === "scripted" || w.status === "ready") {
        return "Ready to use — you can add events to this website.";
    }
    if (w.stale) {
        return "Settings changed and this website needs to be set up again.";
    }
    if (w.status === "scripting") {
        if (w.phase === "manual_approval") return "Setting up… please review the filled form and approve.";
        if (w.phase === "script_approval") return "Setting up… please review the automated fill and approve.";
        if (w.phase === "writing") return "Setting up… the AI is saving the routine.";
        if (w.phase === "running") return "Setting up… checking the automated fill.";
        return "Setting up… an AI is filling the website's form in a browser window.";
    }
    if (w.status === "error") {
        return "Something went wrong — check the activity log below, then set up again.";
    }
    return "Ready to set up this website — it has not been set up yet.";
}

function friendlyStatusLabel(w) {
    // Short badge text for the websites list.
    if (w.stale) return "Stale";
    if (w.status === "scripted" || w.status === "ready") return "Ready";
    if (w.status === "scripting") return "Setting up";
    if (w.status === "error") return "Needs attention";
    return "Not set up";
}

async function renderWebsite() {
    const w = await fetchJSON(`/api/websites/${currentWebsiteId}`);
    // Header fields: only touch an element when its value actually changed, so
    // the poll never disturbs selection/effects inside the right panel.
    const set = (sel, val) => {
        const el = document.getElementById(sel);
        if (el.textContent !== val) el.textContent = val;
    };
    set("wsName", w.name);
    set("wsUrl", w.url);
    const st = document.getElementById("wsStatus");
    if (st.textContent !== w.status || st.className !== "badge " + w.status) {
        st.textContent = w.status; st.className = "badge " + w.status;
    }
    const ph = document.getElementById("wsPhase");
    if (ph.textContent !== w.phase || ph.className !== "badge " + w.phase) {
        ph.textContent = w.phase; ph.className = "badge " + w.phase;
    }
    document.getElementById("wsStale").hidden = !w.stale;
    document.getElementById("wsEventbriteNote").hidden = w.kind !== "eventbrite";
    document.getElementById("browserPanel").hidden = w.kind === "eventbrite";
    set("wsStatusLine", friendlyStatus(w));
    document.getElementById("wsScriptsHead").hidden = w.kind === "eventbrite";
    document.getElementById("wsScripts").hidden = w.kind === "eventbrite";
    document.getElementById("wsLogHead").hidden = w.kind === "eventbrite";
    document.getElementById("wsLog").hidden = w.kind === "eventbrite";

    renderWebsiteActions(w);
    if (w.kind !== "eventbrite") {
        renderScripts(w.scripts);
    } else {
        document.getElementById("wsScripts").innerHTML = "";
    }
    fillEventSelects(); // re-populate selects recreated above (uses cached events)

    const log = document.getElementById("wsLog");
    // Append-only log rendering: never touch already-rendered nodes, so text
    // selection and any per-node effects survive the 1s poll. Rebuild from
    // scratch only when the visible website changes or the log was reset.
    const msgs = w.log || [];
    if (logWid !== currentWebsiteId) {
        log.innerHTML = "";
        logWid = currentWebsiteId;
        lastLogLen = {};
    }
    if (msgs.length < (lastLogLen[currentWebsiteId] || 0)) {
        log.innerHTML = ""; // server reset the log; start over
        lastLogLen[currentWebsiteId] = 0;
    }
    for (const msg of msgs.slice(lastLogLen[currentWebsiteId] || 0)) log.appendChild(renderMessage(msg));
    lastLogLen[currentWebsiteId] = msgs.length;
    // Only pin to the bottom when the user is already near it (don't yank the
    // scroll away from an old message they're reading).
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 8;
    if (atBottom) log.scrollTop = log.scrollHeight;

    await loadWebsites(); // refresh list badges/status
}

function fillEventSelects() {
    const rebuildAll = eventsSig !== lastBuiltSig;
    for (const sel of document.querySelectorAll(".event-select")) {
        if (!rebuildAll && sel.options.length) continue; // nothing new; leave open dropdowns alone
        rebuildSelect(sel);
    }
    if (rebuildAll) lastBuiltSig = eventsSig;
}

function rebuildSelect(sel) {
    sel.innerHTML = "";
    const place = document.createElement("option");
    place.value = ""; place.textContent = "Select an event…"; sel.appendChild(place);
    for (const ev of eventsCache) {
        const opt = document.createElement("option");
        opt.value = ev.id; opt.textContent = ev.title;
        sel.appendChild(opt);
    }
    if (eventsCache.some(e => e.id === selectedEventId)) sel.value = selectedEventId;
    sel.addEventListener("change", () => { selectedEventId = sel.value; });
}

function renderWebsiteActions(w) {
    const box = document.getElementById("wsActions");

    // Don't disturb the dropdown/selection if nothing about the actions changed.
    const ebSig = (w.added_events || []).map(e => e.event_id + ":" + e.url).join("|");
    const sig = [w.id, w.kind, w.status, w.phase, w.stale, w.venue_id, w.organizer_id, ebSig].join("|");
    if (sig === actionsSig && box.childElementCount > 0) return;
    actionsSig = sig;

    box.innerHTML = "";

    // Eventbrite sites: configure venue/organizer ids and add events directly
    // via the API (no agent, no scripts, no browser).
    if (w.kind === "eventbrite") {
        const cfgBtn = document.createElement("div");
        cfgBtn.className = "row gap small";
        cfgBtn.innerHTML = `<button class="btn" id="ebCfgBtn">Configure venue/organizer</button>`;
        box.appendChild(cfgBtn);
        cfgBtn.querySelector("#ebCfgBtn").onclick = () => {
            document.getElementById("ebVenueId").value = w.venue_id || "";
            document.getElementById("ebOrgId").value = w.organizer_id || "";
            ebConfigModal.showModal();
        };

        box.appendChild(actionWithEventSelect(w.id, "+ Add event (draft)", "addevent"));

        const added = document.createElement("div");
        added.className = "row-col gap";
        added.innerHTML = `<p class="muted">Added events (drafts):</p>`;
        const list = (w.added_events || []).length
            ? w.added_events.map(e => {
                const ev = eventsCache.find(x => x.id === e.event_id);
                return `<div class="row gap"><span class="joined">${escapeHtml(ev ? ev.title : e.event_id)}</span>`
                    + `<a class="link" href="${escapeHtml(e.url)}" target="_blank" rel="noopener">open draft ▸</a></div>`;
              }).join("")
            : `<p class="muted">None yet.</p>`;
        added.innerHTML += list;
        box.appendChild(added);
        return;
    }

    // Approval buttons while waiting on the agent.
    if (w.phase === "manual_approval" || w.phase === "script_approval") {
        const waiting = document.createElement("p");
        waiting.className = "muted";
        waiting.textContent = w.phase === "manual_approval"
            ? "The AI filled the form in a browser window. Look it over — if it looks right, Approve; if not, type a note and Reject."
            : "The saved routine ran and filled the form. Look it over — if it looks right, Approve; if not, type a note and Reject.";
        box.appendChild(waiting);

        const row = document.createElement("div");
        row.className = "row gap";
        row.innerHTML = `
            <button class="btn primary" data-action="approve">✓ Approve</button>
            <button class="btn" id="rejectBtn">✗ Reject</button>
        `;
        box.appendChild(row);

        // Reason box is always visible. Typing a reason is optional; if present
        // it is sent with the reject so the agent gets actionable feedback.
        const reasonBox = document.createElement("div");
        reasonBox.className = "row-col gap";
        reasonBox.innerHTML = `
            <textarea id="rejectReason" rows="2" placeholder="Optional: why are you rejecting? e.g. wrong category selected"></textarea>
        `;
        box.appendChild(reasonBox);

        row.querySelector("[data-action=approve]").onclick = async () => {
            await respondWebsite(w.id, "approve");
        };
        // Reject sends whatever reason is typed (may be empty).
        row.querySelector("#rejectBtn").onclick = async () => {
            const reason = reasonBox.querySelector("#rejectReason").value.trim();
            await respondWebsite(w.id, "reject", reason);
        };
        return;
    }

    if (w.status === "unscripted" || w.status === "error" || w.stale) {
        box.appendChild(actionWithEventSelect(w.id, "Set up this website (one time)", "onboard"));
        return;
    }
    if (w.status === "scripted") {
        box.appendChild(actionWithEventSelect(w.id, "Run script", "run"));
        const re = document.createElement("div");
        re.className = "row gap small";
        re.innerHTML = `<button class="btn" id="reOnboardBtn">Set up again</button>`;
        box.appendChild(re);
        re.querySelector("#reOnboardBtn").onclick = async () => {
            const eventId = document.querySelector(".event-select")?.value;
            if (!eventId) { alert("Select an event to onboard with."); return; }
            await onboardWebsite(w.id, eventId);
        };
        return;
    }
    if (w.status === "scripting") {
        const p = document.createElement("p");
        p.className = "muted";
        p.textContent = "An AI is filling the website's form in a browser window. Watch it, then approve.";
        box.appendChild(p);
        // Let the user abort a stuck/long-running onboarding so they can restart.
        const c = document.createElement("div");
        c.className = "row gap small";
        c.innerHTML = `<button class="btn" id="cancelOnboardBtn">Stop &amp; reset setup</button>`;
        box.appendChild(c);
        c.querySelector("#cancelOnboardBtn").onclick = async () => {
            await cancelOnboarding(w.id);
        };
    }
}

async function cancelOnboarding(wsId) {
    if (!confirm("Stop & reset setup? This stops the AI, closes the browser, and lets you start over.")) return;
    try {
        await fetchJSON(`/api/websites/${wsId}/cancel`, { method: "POST" });
    } catch (err) { alert("Failed to cancel: " + err.message); }
}

function actionWithEventSelect(wsId, label, kind) {
    const wrap = document.createElement("div");
    wrap.className = "row-form";
    wrap.innerHTML = `
        <select class="event-select" data-kind="${kind}"></select>
        <button class="btn primary" data-kind="${kind}">${escapeHtml(label)}</button>
    `;
    const btn = wrap.querySelector("button");
    btn.onclick = async () => {
        const sel = wrap.querySelector(".event-select");
        if (!sel.value) { alert("Select an event first."); return; }
        if (kind === "onboard") await onboardWebsite(wsId, sel.value);
        else if (kind === "run") await runWebsite(wsId, sel.value);
        else if (kind === "addevent") await addEventToEventbrite(wsId, sel.value);
    };
    return wrap;
}

async function addEventToEventbrite(wsId, eventId) {
    try {
        const r = await fetchJSON(`/api/websites/${wsId}/add-event`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ event_id: eventId }),
        });
        alert(r.eventbrite_url ? `Draft created: ${r.eventbrite_url}` : (r.message || "Done."));
    } catch (err) { alert("Failed to add to Eventbrite: " + err.message); }
}

async function configureEventbrite(wsId, venueId, orgId) {
    try {
        await fetchJSON(`/api/websites/${wsId}/configure-eventbrite`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ venue_id: venueId, organizer_id: orgId }),
        });
    } catch (err) { alert("Failed to save config: " + err.message); }
}

async function onboardWebsite(wsId, eventId) {
    try {
        await fetchJSON(`/api/websites/${wsId}/onboard`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ event_id: eventId }),
        });
    } catch (err) { alert("Couldn't start setup: " + err.message); }
}

async function runWebsite(wsId, eventId) {
    try {
        const r = await fetchJSON(`/api/websites/${wsId}/run`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ event_id: eventId }),
        });
        alert(r.message || "Script ran. Review and submit manually.");
    } catch (err) { alert("Script run failed: " + err.message); }
}

async function respondWebsite(wsId, action, reason) {
    try {
        await fetchJSON(`/api/websites/${wsId}/respond`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action, reason: reason || "" }),
        });
    } catch (err) { alert("Failed: " + err.message); }
}

function renderScripts(scripts) {
    const box = document.getElementById("wsScripts");

    // Don't disturb an open code view if nothing changed.
    const sig = scripts.map(s => s.id + ":" + s.status + ":" + s.updated_at).join("|");
    if (sig === scriptsSig && box.childElementCount > 0) return;
    scriptsSig = sig;

    box.innerHTML = "";
    if (!scripts.length) {
        box.innerHTML = `<p class="muted">No scripts yet.</p>`;
        return;
    }
    for (const s of scripts) {
        const item = document.createElement("details");
        item.className = "script-item";
        item.innerHTML = `
            <summary>
                <span class="item-title">${escapeHtml(s.label || "script")}</span>
                ${badge(s.status, s.status)} <span class="joined">${escapeHtml(formatDate(s.updated_at))}</span>
            </summary>
            <pre class="script-code">${escapeHtml(s.code || "(no code)")}</pre>
        `;
        box.appendChild(item);
    }
}

// ============================= Forms / dialogs =============================
const eventModal = document.getElementById("eventModal");
const settingsModal = document.getElementById("settingsModal");
const settingsForm = document.getElementById("settingsForm");
const websiteModal = document.getElementById("websiteModal");
const helpModal = document.getElementById("helpModal");
const ebConfigModal = document.getElementById("ebConfigModal");
const ebConfigForm = document.getElementById("ebConfigForm");

document.getElementById("refreshBtn").onclick = async () => {
    const btn = document.getElementById("refreshBtn");
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "↻ …";
    try {
        const r = await fetchJSON("/api/refresh", { method: "POST" });
        if (r.refreshed) {
            // Reload list data and re-render any open detail panels so the UI
            // reflects the freshly hydrated state.
            await loadEvents();
            await loadWebsites();
            if (currentWebsiteId) await openWebsite(currentWebsiteId);
            else if (currentEventId) await openEvent(currentEventId);
        }
        alert(r.message);
    } catch (err) {
        alert("Refresh failed: " + err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = old;
    }
};

document.getElementById("newEventBtn").onclick = async () => {
    resetEventModalMode();
    await prefillEventDefaults();
    eventModal.showModal();
};
document.getElementById("closeEventModal").onclick = () => eventModal.close();

// Fields that default from Settings but can be overridden per event.
const EVENT_DEFAULTED_FIELDS = [
    "website_url", "organizer", "organizer_first_name", "organizer_last_name",
    "organizer_phone_number", "organizer_email_address"];

async function prefillEventDefaults() {
    // Fill the New Event form's organizer/website fields from Settings defaults.
    let s;
    try { s = await fetchJSON("/api/settings"); }
    catch (_) { return; }
    const form = document.getElementById("eventForm");
    for (const name of EVENT_DEFAULTED_FIELDS) {
        const el = form.querySelector(`[name=${name}]`);
        if (el) el.value = s[name] || "";
    }
}
document.getElementById("newWebsiteBtn").onclick = () => { resetWebsiteModalMode(); websiteModal.showModal(); };
document.getElementById("closeWebsiteModal").onclick = () => websiteModal.close();
document.getElementById("helpBtn").onclick = () => helpModal.showModal();
document.getElementById("closeHelpModal").onclick = () => helpModal.close();
document.getElementById("closeEbConfigModal").onclick = () => ebConfigModal.close();

// ---- Edit-mode helpers (used by the shared New/Edit dialogs) ----
function resetEventModalMode() {
    editingEventId = null;
    // Clear any leftover values from a previous Edit/Create before starting fresh.
    document.getElementById("eventForm").reset();
    document.getElementById("eventModalTitle").textContent = "New Event";
    document.getElementById("eventModalNote").innerHTML =
        "Only fields that vary per event are shown here. Venue, location, cost,"
        + " categories and other global defaults come from <strong>⚙️ Settings</strong>; the"
        + " organizer/website fields below start from the Settings defaults and can be"
        + " changed for this event.";
    document.getElementById("eventModalSubmit").textContent = "Create Event";
}

function openEventEdit(id) {
    const ev = eventsCache.find(x => x.id === id);
    if (!ev) return;
    editingEventId = id;
    document.getElementById("eventModalTitle").textContent = "Edit Event";
    document.getElementById("eventModalNote").innerHTML =
        "Venue, location, cost and categories come from <strong>⚙️ Settings</strong>; the"
        + " organizer/website fields below are saved per event (leave blank to go back"
        + " to the Settings defaults).";
    document.getElementById("eventModalSubmit").textContent = "Save Changes";
    const form = document.getElementById("eventForm");
    form.querySelector("[name=title]").value = ev.title;
    form.querySelector("[name=short_summary]").value = ev.short_summary;
    form.querySelector("[name=long_summary]").value = ev.long_summary;
    form.querySelector("[name=image]").value = ev.image;
    form.querySelector("[name=start_date]").value = String(ev.start_date).slice(0, 16);
    form.querySelector("[name=end_date]").value = String(ev.end_date).slice(0, 16);
    for (const name of EVENT_DEFAULTED_FIELDS) {
        const el = form.querySelector(`[name=${name}]`);
        if (el) el.value = ev[name] || "";
    }
    eventModal.showModal();
}

document.getElementById("eventEditBtn").onclick = () => openEventEdit(currentEventId);

function resetWebsiteModalMode() {
    editingWebsiteId = null;
    document.getElementById("websiteModalTitle").textContent = "New Website";
    document.getElementById("websiteModalNote").textContent =
        "Add a site once and the AI figures it out, so every future event is added to it the same way. Eventbrite sites send events straight to Eventbrite instead.";
    document.getElementById("websiteModalSubmit").textContent = "Create Website";
    document.getElementById("websiteKind").disabled = false;
    document.querySelector("#websiteForm input[name=url]").required = true;
}

function openWebsiteEdit(id) {
    const w = websitesCache.find(x => x.id === id);
    if (!w) return;
    editingWebsiteId = id;
    document.getElementById("websiteModalTitle").textContent = "Edit Website";
    document.getElementById("websiteModalNote").innerHTML =
        "You can change the name and/or URL. The kind can't change after creation.<br><br>"
        + "<b>Warning:</b> changing the URL of a set-up site clears its saved routine — it will need to be set up again.";
    document.getElementById("websiteModalSubmit").textContent = "Save Changes";
    const form = document.getElementById("websiteForm");
    form.querySelector("[name=name]").value = w.name;
    form.querySelector("[name=url]").value = w.url;
    const kindSel = document.getElementById("websiteKind");
    kindSel.value = w.kind;
    kindSel.disabled = true; // kind is immutable after creation
    document.querySelector("#websiteForm input[name=url]").required = w.kind === "agentic";
    websiteModal.showModal();
}

document.getElementById("wsEditBtn").onclick = () => openWebsiteEdit(currentWebsiteId);

// Save venue/organizer from the Eventbrite config dialog.
ebConfigForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const wid = currentWebsiteId;
    if (!wid) return;
    const venue = document.getElementById("ebVenueId").value.trim();
    const org = document.getElementById("ebOrgId").value.trim();
    await configureEventbrite(wid, venue, org);
    ebConfigModal.close();
});

// Toggle URL requiredness/placeholder when the kind changes in the website modal.
(function () {
    const kindSel = document.querySelector("#websiteForm select[name=kind]");
    const urlInput = document.querySelector("#websiteForm input[name=url]");
    if (!kindSel || !urlInput) return;
    const update = () => {
        if (kindSel.value === "eventbrite") {
            urlInput.required = false;
            urlInput.placeholder = "Optional for Eventbrite — e.g. your public Eventbrite org page";
        } else {
            urlInput.required = true;
            urlInput.placeholder = "https://...";
        }
    };
    kindSel.addEventListener("change", update);
    update();
})();

// ---------- Website creation ----------
document.getElementById("websiteForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target));
    if (editingWebsiteId) {
        // ---- EDIT MODE ----
        const orig = websitesCache.find(w => w.id === editingWebsiteId);
        // Changing the URL of a scripted site wipes its routine.
        if (orig && orig.status === "scripted" && data.url && data.url !== orig.url
            && !confirm(`You changed the URL of "${orig.name}".\n\nThe saved routine points at the old address and won't work — this site will need to be set up again. Continue?`)) {
            return;
        }
        try {
            await fetchJSON(`/api/websites/${editingWebsiteId}`, {
                method: "PUT", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: data.name, url: data.url }),
            });
        } catch (err) { alert("Failed to edit website: " + err.message); return; }
        editingWebsiteId = null;
        e.target.reset();
        document.getElementById("websiteKind").disabled = false;
        websiteModal.close();
        if (currentWebsiteId) await renderWebsite();
        else switchView("websites");
        loadWebsites();
        return;
    }
    // ---- CREATE MODE ----
    // Only one Eventbrite site is supported — block a second one client-side.
    if (data.kind === "eventbrite" && websitesCache.some(w => w.kind === "eventbrite")) {
        alert("Only one Eventbrite website is supported.");
        return;
    }
    try {
        await fetchJSON("/api/websites", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data),
        });
    } catch (err) { alert("Failed to create website: " + err.message); return; }
    e.target.reset();
    websiteModal.close();
    switchView("websites");
    loadWebsites();
});

// ---------- Settings ----------
document.getElementById("settingsBtn").onclick = async () => {
    try { await loadSettingsForm(); settingsModal.showModal(); }
    catch (err) { alert("Failed to load settings: " + err.message); }
};
document.getElementById("closeSettingsModal").onclick = () => settingsModal.close();

async function loadSettingsForm() {
    const s = await fetchJSON("/api/settings");
    for (const el of settingsForm.elements) {
        if (el.name === "categories") el.value = (s.categories || []).join(", ");
        else if (el.name === "eventbrite_categories") {
            const d = s.eventbrite_categories || {};
            el.value = Object.entries(d).map(([k, v]) => `${k}=${v}`).join(", ");
        }
        else if (el.type === "checkbox") el.checked = Boolean(s[el.name]);
        else if (el.name in s) el.value = s[el.name];
    }
}

settingsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(settingsForm));
    data.open_to_public = settingsForm.open_to_public.checked;
    data.event_cost = Number(data.event_cost);
    try {
        await fetchJSON("/api/settings", {
            method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
        });
    } catch (err) { alert("Failed to save settings: " + err.message); return; }
    settingsModal.close();
});

// ---------- Event creation ----------
document.getElementById("eventForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const data = Object.fromEntries(new FormData(form));
    try {
        if (editingEventId) {
            await fetchJSON(`/api/events/${editingEventId}`, {
                method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
            });
        } else {
            await fetchJSON("/api/events", {
                method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
            });
        }
    } catch (err) { alert("Failed to save event: " + err.message); return; }
    editingEventId = null;
    form.reset();
    eventModal.close();
    await loadEvents();
    if (currentEventId && eventsCache.some(x => x.id === currentEventId)) await openEvent(currentEventId);
});

// ---------- Tooltip (floats above the dialog via position: fixed) ----------
document.querySelectorAll(".tip").forEach(tip => {
    const box = tip.querySelector(".tip-box");
    if (!box) return;
    function show() {
        tip.classList.add("show");
        const r = tip.getBoundingClientRect();
        const bw = box.offsetWidth, bh = box.offsetHeight;
        let top = r.top - bh - 12;
        if (top < 8) top = r.bottom + 12;
        let left = Math.max(8, Math.min(r.left, window.innerWidth - bw - 8));
        box.style.left = left + "px";
        box.style.top = top + "px";
    }
    function hide() { tip.classList.remove("show"); }
    tip.addEventListener("mouseenter", show);
    tip.addEventListener("mouseleave", hide);
    box.addEventListener("mouseenter", () => tip.classList.add("show"));
    box.addEventListener("mouseleave", hide);
});

// ============================= Boot =============================
loadEvents();
