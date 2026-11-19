// ---------------------------------------------------------------------
// Tiny IndexedDB helper for persisting a FileSystemDirectoryHandle.
// localStorage can't store handles (they're not JSON-serializable),
// so the PC/Cloud folder choice is persisted via IndexedDB instead.
// This is the "browser persistent storage" for the PC/Cloud folder.
// The dummy-slot GUID and RC-2 root are plain server-side config
// (see /api/config) since they describe the RC-2 device, not the
// browsing client.
// ---------------------------------------------------------------------
const DB_NAME = "pidjirc2kmzsync";
const STORE_NAME = "handles";
const HANDLE_KEY = "pc-cloud-folder";
const STATUS_POLL_INTERVAL_MS = 10000;
const STATUS_WATCHDOG_TIMEOUT_MS = STATUS_POLL_INTERVAL_MS * 1.5;

function openHandleDb() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, 1);
        req.onupgradeneeded = () => {
            req.result.createObjectStore(STORE_NAME);
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}

async function saveDirHandle(handle) {
    const db = await openHandleDb();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(STORE_NAME, "readwrite");
        tx.objectStore(STORE_NAME).put(handle, HANDLE_KEY);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
    });
}

async function loadDirHandle() {
    const db = await openHandleDb();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(STORE_NAME, "readonly");
        const req = tx.objectStore(STORE_NAME).get(HANDLE_KEY);
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => reject(req.error);
    });
}

// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------
const supportsDirectoryPicker = typeof window.showDirectoryPicker === "function";
let pcDirHandle = null;          // FileSystemDirectoryHandle, when supported
let selectedMissionGuid = null;  // null = "use dummy slot"
let pcFallbackFiles = [];        // files from the fallback folder input (no directory handle)
let selectedPcFile = null;       // { name, getFile: () => Promise<File>, handle: FileSystemFileHandle|null }
let lastMissionsData = [];       // last /api/missions response, for the preview pane
let copyMapByGuid = {};          // target_mission_guid (lowercased) -> summary row
let statusRefreshInProgress = false;
let lastKnownConnectionState = null;
let lastKnownLowBattery = null;
let batteryUnavailableLogged = false;
let piStatusWatchdogTimer = null;
let lastKnownPiConnectionState = null;

// ---------------------------------------------------------------------
// Activity log
// ---------------------------------------------------------------------
function logMessage(level, message) {
    const logEl = document.getElementById("activity-log");
    if (!logEl) return;
    const timestamp = new Date().toLocaleTimeString();
    logEl.value += `[${timestamp}] ${level.toUpperCase()}: ${message}\n`;
    logEl.scrollTop = logEl.scrollHeight;
}

function logInfo(message) {
    logMessage("info", message);
}

function logWarning(message) {
    logMessage("warning", message);
}

function logError(message) {
    logMessage("error", message);
}

function userErrorMessage(message) {
    if (!/NoDeviceConnected/i.test(message)) return message;
    const guidance = "RC-2 not connected. Plug the RC-2 into the updater, then press Refresh.";
    if (/RESTORE FAILED|Backup:|Could not remove partial file/i.test(message)) {
        return `${guidance}\n${message}`;
    }
    return guidance;
}

async function copyActivityLog() {
    const logText = document.getElementById("activity-log").value;
    try {
        await navigator.clipboard.writeText(logText);
        logInfo("Activity log copied to clipboard.");
    } catch (err) {
        const logEl = document.getElementById("activity-log");
        logEl.focus();
        logEl.select();
        document.execCommand("copy");
        logInfo("Activity log copied to clipboard.");
    }
}

function clearActivityLog() {
    document.getElementById("activity-log").value = "";
    logInfo("Activity log cleared.");
}

function openImageLightbox() {
    const previewImage = document.getElementById("preview-image");
    if (previewImage.style.display === "none" || !previewImage.src) return;
    const lightbox = document.getElementById("image-lightbox");
    document.getElementById("lightbox-image").src = previewImage.src;
    lightbox.hidden = false;
    document.getElementById("close-lightbox-btn").focus();
    logInfo("Opened large mission preview.");
}

function closeImageLightbox() {
    const lightbox = document.getElementById("image-lightbox");
    if (lightbox.hidden) return;
    lightbox.hidden = true;
    document.getElementById("preview-image").focus();
}

// ---------------------------------------------------------------------
// Status / config
// ---------------------------------------------------------------------
function setPiStatus(connected) {
    const piStatusEl = document.getElementById("pi-status");
    piStatusEl.classList.toggle("status-connected", connected);
    piStatusEl.classList.toggle("status-disconnected", !connected);
    piStatusEl.textContent = connected
        ? "PiRC2kmzUpdater connected"
        : "PiRC2kmzUpdater not connected";

    if (lastKnownPiConnectionState !== null && lastKnownPiConnectionState !== connected) {
        if (connected) {
            logInfo("Updater connection re-established.");
        } else {
            logWarning("Updater connection lost.");
        }
    }
    lastKnownPiConnectionState = connected;
}

function armPiStatusWatchdog() {
    if (piStatusWatchdogTimer !== null) {
        window.clearTimeout(piStatusWatchdogTimer);
    }
    piStatusWatchdogTimer = window.setTimeout(() => {
        setPiStatus(false);
    }, STATUS_WATCHDOG_TIMEOUT_MS);
}

// The "updater connected" badge answers one question only: is the Pi's web
// server responding? It polls /api/ping, which never touches MTP, so a slow
// RC-2 operation can't make it flap. RC-2 state comes from /api/status.
let pingInProgress = false;
async function pingServer() {
    if (pingInProgress) return;
    pingInProgress = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), STATUS_POLL_INTERVAL_MS);
    try {
        const res = await fetch("/api/ping", { cache: "no-store", signal: controller.signal });
        if (!res.ok) throw new Error(`Ping failed (${res.status})`);
        setPiStatus(true);
        armPiStatusWatchdog();
    } catch (err) {
        // Leave the badge to the watchdog: polling continues, and the next
        // successful ping restores the green state.
    } finally {
        window.clearTimeout(timeout);
        pingInProgress = false;
    }
}

async function refreshStatus() {
    if (statusRefreshInProgress) return;
    statusRefreshInProgress = true;
    const statusEl = document.getElementById("status");
    const batteryEl = document.getElementById("battery-status");
    try {
        const res = await fetch("/api/status");
        if (!res.ok) throw new Error(`Status request failed (${res.status})`);
        const data = await res.json();
        const connectionStateChanged = (
            lastKnownConnectionState !== null &&
            lastKnownConnectionState !== data.connected
        );
        if (lastKnownConnectionState === true && !data.connected) {
            logWarning("RC-2 connection lost.");
        } else if (lastKnownConnectionState === false && data.connected) {
            logInfo("RC-2 connection re-established.");
        }
        lastKnownConnectionState = data.connected;
        statusEl.classList.toggle("status-connected", data.connected);
        statusEl.classList.toggle("status-disconnected", !data.connected);
        statusEl.textContent = data.connected
            ? `RC-2 connected (${data.connection_mode})`
            : `RC-2 not connected (root: ${data.rc2_root || "not set"})`;

        // low_battery is null when the GPIO monitor isn't available (e.g.
        // running the dev server on a Mac, or gpiozero/lgpio missing on the
        // Pi) -- treat that as "unknown", not "battery is fine", and only
        // log the unavailability once rather than on every poll.
        if (data.low_battery === true && lastKnownLowBattery !== true) {
            logWarning("PowerBoost LBO reports low battery.");
        } else if (data.low_battery === false && lastKnownLowBattery === true) {
            logInfo("Battery no longer reported as low.");
        }
        if (data.battery_monitor_available === false && !batteryUnavailableLogged) {
            logWarning("Battery monitor unavailable (check LBO GPIO wiring/config).");
            batteryUnavailableLogged = true;
        } else if (data.battery_monitor_available === true) {
            batteryUnavailableLogged = false;
        }
        lastKnownLowBattery = data.low_battery;
        batteryEl.classList.toggle("battery-low", data.low_battery === true);

        if (connectionStateChanged) {
            refreshMissions(false);   // new RC-2 session: always a fresh listing
        }
    } catch (err) {
        if (lastKnownConnectionState === true) {
            logWarning("RC-2 connection lost: status check failed.");
        }
        lastKnownConnectionState = false;
        statusEl.classList.remove("status-connected");
        statusEl.classList.add("status-disconnected");
        statusEl.textContent = "Error checking status";
        // The Pi badge is controlled by the watchdog so polling continues
        // and a later successful response can restore the green state.
    } finally {
        statusRefreshInProgress = false;
    }
}

async function loadConfig() {
    try {
        const res = await fetch("/api/config");
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
        document.getElementById("rc2-root-input").value = data.rc2_root || "";
        document.getElementById("dummy-slot-display").textContent =
            data.dummy_slot_guid || "(none set)";
        logInfo("Configuration loaded.");
    } catch (err) {
        logError(`Configuration load failed: ${err.message}`);
    }
}

async function saveRc2Root() {
    const root = document.getElementById("rc2-root-input").value.trim();
    if (!root) {
        logWarning("Save RC-2 root ignored: no root was entered.");
        return;
    }
    try {
        const res = await fetch("/api/config/rc2-root", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Requested-With": "PiDjiRc2KmzSync" },
            body: JSON.stringify({ root }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        logInfo(`RC-2 root saved: ${root}`);
        refreshStatus();
    } catch (err) {
        logError(`Saving RC-2 root failed: ${err.message}`);
    }
}

async function setDummySlot() {
    if (!selectedMissionGuid) {
        setActionResult("Select an RC-2 mission first, then set it as the dummy slot.");
        logWarning("Set dummy slot ignored: no RC-2 mission is selected.");
        return;
    }
    try {
        const res = await fetch("/api/config/dummy-slot", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Requested-With": "PiDjiRc2KmzSync" },
            body: JSON.stringify({ guid: selectedMissionGuid }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        document.getElementById("dummy-slot-display").textContent = data.dummy_slot_guid || "(none set)";
        logInfo(`Dummy mission slot set to ${selectedMissionGuid}.`);
        refreshMissions(true, true);   // only the dummy flag changed; RC-2 untouched
    } catch (err) {
        logError(`Setting dummy slot failed: ${err.message}`);
    }
}

// ---------------------------------------------------------------------
// Mission <-> PC file associations (no file transfer, just a saved link)
// ---------------------------------------------------------------------
async function loadCopyMap() {
    try {
        const res = await fetch("/api/copy-map");
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
        copyMapByGuid = {};
        for (const row of (data.rows || [])) {
            const guid = (row.target_mission_guid || "").toLowerCase();
            if (guid) copyMapByGuid[guid] = row;
        }
        logInfo("Mission associations loaded.");
    } catch (err) {
        logError(`Mission association load failed: ${err.message}`);
    }
}

// ---------------------------------------------------------------------
// RC-2 mission list (left pane)
// ---------------------------------------------------------------------
// useCache=true asks the Pi to reuse its RC-2 listing cache instead of rescanning
// the device. Pass it after changes this app made itself (upload, dummy slot);
// leave it false for an explicit Refresh, page load and RC-2 reconnect.
async function refreshMissions(showLoading = true, useCache = false) {
    const listEl = document.getElementById("mission-list");
    logInfo(useCache ? "Updating RC-2 missions (cached)." : "Refreshing RC-2 missions.");
    if (showLoading) {
        listEl.innerHTML = "<li>Loading...</li>";
    }
    try {
        const res = await fetch(useCache ? "/api/missions?cached=1" : "/api/missions");
        const data = await res.json();

        if (!res.ok || data.error) {
            const error = data.error || `HTTP ${res.status}`;
            const errorRow = document.createElement("li");
            errorRow.textContent = userErrorMessage(error);
            listEl.replaceChildren(errorRow);
            lastMissionsData = [];
            logWarning(`RC-2 mission list warning: ${error}`);
            return;
        }

        lastMissionsData = data.missions;
        renderMissionList(!useCache);   // cached refresh: let the browser reuse its thumbnails
    } catch (err) {
        listEl.innerHTML = "<li>Error loading missions.</li>";
        lastMissionsData = [];
        logError(`Loading RC-2 missions failed: ${err.message}`);
    }
}

// "modified" = most recently modified first (default); "name" = linked PC file
// name ascending, falling back to the GUID for missions with no association.
let missionSortMode = "modified";

function missionModifiedTime(mission) {
    const m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})$/.exec(mission.last_modified || "");
    return m ? Date.UTC(+m[3], +m[2] - 1, +m[1], +m[4], +m[5], +m[6]) : 0;
}

function missionSortName(mission) {
    const linked = copyMapByGuid[mission.guid.toLowerCase()];
    return (linked && linked.source_filename) || mission.guid;
}

function sortedMissions() {
    const list = (lastMissionsData || []).slice();
    if (missionSortMode === "name") {
        list.sort((a, b) => missionSortName(a).localeCompare(missionSortName(b), undefined, { sensitivity: "base" })
            || a.guid.localeCompare(b.guid));
    } else {
        list.sort((a, b) => missionModifiedTime(b) - missionModifiedTime(a) || a.guid.localeCompare(b.guid));
    }
    return list;
}

// Draws the left-hand mission list from lastMissionsData and copyMapByGuid, with
// no server call. bustThumbs=false lets the browser reuse thumbnails it already has.
function renderMissionList(bustThumbs = true) {
    const listEl = document.getElementById("mission-list");
    const data = { missions: sortedMissions() };
    {
        listEl.innerHTML = "";

        for (const mission of data.missions) {
            const li = document.createElement("li");
            li.classList.add("mission");
            li.dataset.guid = mission.guid;
            if (mission.is_dummy) li.classList.add("dummy-slot");
            if (mission.guid === selectedMissionGuid) li.classList.add("selected");

            const label = mission.is_dummy ? `${mission.guid} (dummy slot)` : mission.guid;
            const linked = copyMapByGuid[mission.guid.toLowerCase()];
            const linkedNote = linked ? ` · linked: ${linked.source_filename}` : "";

            const img = document.createElement("img");
            img.className = "thumb";
            img.alt = "";
            img.src = `/api/missions/${encodeURIComponent(mission.guid)}/thumbnail` + (bustThumbs ? `?t=${Date.now()}` : "");
            img.addEventListener("error", () => { img.style.visibility = "hidden"; });

            const textDiv = document.createElement("div");
            textDiv.className = "mission-text";
            textDiv.append(document.createTextNode(label));
            const meta = document.createElement("span");
            meta.className = "meta";
            meta.textContent = `${mission.display_kmz_name} · ${mission.last_modified}${linkedNote}`;
            textDiv.appendChild(meta);

            li.appendChild(img);
            li.appendChild(textDiv);

            li.addEventListener("click", () => {
                selectedMissionGuid = (selectedMissionGuid === mission.guid) ? null : mission.guid;
                logInfo(selectedMissionGuid
                    ? `Selected RC-2 mission ${selectedMissionGuid}.`
                    : "RC-2 mission selection cleared; dummy slot will be used.");
                document.querySelectorAll("#mission-list li.mission").forEach(row => {
                    row.classList.toggle("selected", row.dataset.guid === selectedMissionGuid);
                });
                renderPreviewPane();
            });
            listEl.appendChild(li);
        }

        if (data.missions.length === 0) {
            listEl.innerHTML = "<li>No missions found on RC-2.</li>";
        }
    }
}

// ---------------------------------------------------------------------
// Mission preview pane (right)
// ---------------------------------------------------------------------
function renderPreviewPane() {
    const imgEl = document.getElementById("preview-image");
    const placeholderEl = document.getElementById("preview-placeholder");
    const infoEl = document.getElementById("preview-info");

    if (!selectedMissionGuid) {
        imgEl.style.display = "none";
        placeholderEl.style.display = "block";
        infoEl.innerHTML = "";
        return;
    }

    const mission = lastMissionsData.find(m => m.guid === selectedMissionGuid);

    imgEl.src = `/api/missions/${encodeURIComponent(selectedMissionGuid)}/thumbnail?t=${Date.now()}`;
    imgEl.style.display = "block";
    imgEl.onerror = () => {
        imgEl.style.display = "none";
        placeholderEl.textContent = "No preview image available for this mission.";
        placeholderEl.style.display = "block";
    };
    imgEl.onload = () => { placeholderEl.style.display = "none"; };

    const linked = copyMapByGuid[selectedMissionGuid.toLowerCase()];
    const rows = [
        ["GUID", selectedMissionGuid],
    ];
    if (mission) {
        rows.push(["KMZ file", mission.display_kmz_name]);
        rows.push(["Last modified", mission.last_modified]);
    }
    if (linked) {
        rows.push(["Linked PC file", linked.source_filename]);
    }

    infoEl.replaceChildren();
    for (const [label, value] of rows) {
        const dt = document.createElement("dt");
        const dd = document.createElement("dd");
        dt.textContent = label;
        dd.textContent = value;
        infoEl.append(dt, dd);
    }
}

// ---------------------------------------------------------------------
// PC / Cloud folder tree (right pane)
// ---------------------------------------------------------------------
async function choosePcFolder() {
    if (supportsDirectoryPicker) {
        // If a folder was saved in an earlier session, try to re-grant access
        // to it with a single permission prompt instead of re-picking it.
        try {
            const saved = await loadDirHandle().catch(() => null);
            if (saved && !pcDirHandle) {
                const perm = await saved.requestPermission({ mode: "readwrite" });
                if (perm === "granted") {
                    pcDirHandle = saved;
                    document.getElementById("pc-folder-display").textContent = saved.name;
                    document.getElementById("pc-folder-note").textContent = "";
                    logInfo(`PC/Cloud folder re-authorized: ${saved.name}.`);
                    renderPcTree();
                    return;
                }
            }
        } catch (err) {
            logWarning(`Could not re-authorize saved folder, choose it again: ${err.message}`);
        }
        try {
            // "readwrite" is requested up front: the Copy RC-2 -> PC action
            // needs to overwrite a chosen file in this folder, not just read it.
            const handle = await window.showDirectoryPicker({ mode: "readwrite" });
            pcDirHandle = handle;
            await saveDirHandle(handle);
            document.getElementById("pc-folder-display").textContent = handle.name;
            document.getElementById("pc-folder-note").textContent = "";
            logInfo(`PC/Cloud folder selected: ${handle.name}.`);
            renderPcTree();
        } catch (err) {
            logWarning(`PC/Cloud folder picker cancelled or failed: ${err.message}`);
        }
        return;
    }

    // Fallback for browsers without the File System Access API (e.g. Safari/iOS):
    // no persistent handle is possible, so re-pick each session via a plain
    // folder-scoped file input.
    const input = document.getElementById("pc-folder-fallback-input");
    input.click();
}

function buildFolderTreeFromFiles(files) {
    // Groups a flat FileList (each with webkitRelativePath, e.g.
    // "Air3s/Home/TestMission.kmz") into a nested tree structure, purely
    // by splitting that path string -- no filesystem API involved, so this
    // works identically in every browser, iPad Safari included.
    const root = { folders: new Map(), files: [] };
    for (const file of files) {
        const relPath = file.webkitRelativePath || file.name;
        const segments = relPath.split("/").filter(Boolean);
        segments.shift(); // drop the chosen root folder's own name, so this
                           // tree's shape matches buildTreeForDirHandle's
                           // (children of the chosen folder, not a wrapping
                           // node for the folder itself)
        const fileName = segments.pop() || file.name;
        let node = root;
        for (const segment of segments) {
            if (!node.folders.has(segment)) {
                node.folders.set(segment, { folders: new Map(), files: [] });
            }
            node = node.folders.get(segment);
        }
        node.files.push({ name: fileName, file });
    }
    return root;
}

function renderFolderTreeNode(node) {
    const ul = document.createElement("ul");

    const folderNames = Array.from(node.folders.keys()).sort((a, b) => a.localeCompare(b));
    for (const folderName of folderNames) {
        const li = document.createElement("li");
        li.classList.add("folder");
        li.textContent = folderName;
        li.appendChild(renderFolderTreeNode(node.folders.get(folderName)));
        ul.appendChild(li);
    }

    const sortedFiles = node.files.slice().sort((a, b) => a.name.localeCompare(b.name));
    for (const { name, file } of sortedFiles) {
        const li = document.createElement("li");
        li.textContent = name;
        li.addEventListener("click", () => {
            // No FileSystemFileHandle is available from this fallback input,
            // so this selection can be an upload source but not a
            // Copy RC-2 -> PC overwrite target (see copyRc2ToPc()).
            selectedPcFile = { name, getFile: async () => file, handle: null };
            logInfo(`Selected PC/Cloud file ${name}.`);
            renderPcSelectionHighlight(li);
        });
        ul.appendChild(li);
    }

    return ul;
}

function handleFallbackFolderPick(fileList) {
    // Build a real nested tree from webkitRelativePath. This is just string
    // splitting on data the fallback input already gives us -- it doesn't
    // need the File System Access API, so it renders identically on iPad
    // Safari as on desktop, unlike the tree persistence and Copy RC-2 -> PC
    // overwrite-in-place features below, which genuinely do need a real
    // FileSystemFileHandle and so stay desktop Chrome/Edge only.
    const files = Array.from(fileList).filter(f => f.name.toLowerCase().endsWith(".kmz"));
    logInfo(`PC/Cloud folder loaded with ${files.length} KMZ file(s).`);
    pcFallbackFiles = files;
    const treeEl = document.getElementById("pc-tree");
    treeEl.innerHTML = "";

    const tree = buildFolderTreeFromFiles(files);
    treeEl.appendChild(renderFolderTreeNode(tree));

    document.getElementById("pc-folder-display").textContent = "(chosen this session only)";
    document.getElementById("pc-folder-note").textContent =
        "Your browser doesn't support persistent folder access; re-choose the folder or files each visit. " +
        "Copy RC-2 -> PC also isn't available with this fallback -- it can only overwrite a file " +
        "picked via a real folder handle (desktop Chrome/Edge).";
}

function renderPcSelectionHighlight(selectedLi) {
    document.querySelectorAll("#pc-tree li").forEach(li => li.classList.remove("selected"));
    selectedLi.classList.add("selected");
}

async function renderPcTree() {
    const treeEl = document.getElementById("pc-tree");
    treeEl.innerHTML = "";
    if (!pcDirHandle) return;

    const rootUl = await buildTreeForDirHandle(pcDirHandle);
    treeEl.appendChild(rootUl);
    logInfo("PC/Cloud folder refreshed.");
}

async function buildTreeForDirHandle(dirHandle, depth = 0) {
    const ul = document.createElement("ul");
    if (depth > 6) return ul; // safety bound on pathological nesting

    const entries = [];
    for await (const [name, handle] of dirHandle.entries()) {
        entries.push([name, handle]);
    }
    entries.sort((a, b) => a[0].localeCompare(b[0]));

    for (const [name, handle] of entries) {
        if (handle.kind === "directory") {
            const li = document.createElement("li");
            li.classList.add("folder");
            li.textContent = name;
            const childUl = await buildTreeForDirHandle(handle, depth + 1);
            li.appendChild(childUl);
            ul.appendChild(li);
        } else if (name.toLowerCase().endsWith(".kmz")) {
            const li = document.createElement("li");
            li.textContent = name;
            li.addEventListener("click", () => {
                selectedPcFile = { name, getFile: () => handle.getFile(), handle };
                logInfo(`Selected PC/Cloud file ${name}.`);
                renderPcSelectionHighlight(li);
            });
            ul.appendChild(li);
        }
    }
    return ul;
}

// ---------------------------------------------------------------------
// Copy (upload) and download actions
// ---------------------------------------------------------------------
function setActionResult(text) {
    document.getElementById("action-result").textContent = text;
}

async function copySelectedToRc2() {
    if (!selectedPcFile) {
        setActionResult("Select a PC/Cloud KMZ file first.");
        logWarning("Copy to RC-2 ignored: no PC/Cloud KMZ file is selected.");
        return;
    }

    const destinationMission = selectedMissionGuid
        ? lastMissionsData.find(mission => mission.guid === selectedMissionGuid)
        : lastMissionsData.find(mission => mission.is_dummy);
    if (!destinationMission) {
        setActionResult("Select an RC-2 mission or configure a dummy slot.");
        return;
    }
    const destinationName = destinationMission?.display_kmz_name ||
        `${destinationMission?.guid || "dummy slot"}.kmz (resolved by RC-2)`;
    const targetGuid = destinationMission.guid;
    if (!window.confirm(`Copy ${selectedPcFile.name} to ${targetGuid}${destinationMission.is_dummy ? " (dummy slot)" : ""}?\nExisting mission content will be replaced.`)) return;
    setActionResult("Copying...");
    const button = document.getElementById("copy-btn");
    button.disabled = true;
    logInfo(`Copy selected KMZ -> RC-2: ${selectedPcFile.name} -> ${destinationName}`);
    try {
        const file = await selectedPcFile.getFile();
        const formData = new FormData();
        formData.append("file", file, selectedPcFile.name);
        formData.append("target_guid", targetGuid);

        const res = await fetch("/api/upload", { method: "POST", headers: { "X-Requested-With": "PiDjiRc2KmzSync" }, body: formData });
        const data = await res.json();
        if (!res.ok || data.error) {
            const error = data.error || `HTTP ${res.status}`;
            setActionResult(userErrorMessage(error));
            logError(`Copy to RC-2 failed: ${error}`);
            return;
        }
        setActionResult(`Copied to mission ${data.mission_guid} as ${data.dest_filename}`);
        logInfo(`Copied to mission ${data.mission_guid} as ${data.dest_filename}.`);
        logInfo(data.message);
        await refreshMissions(false, false);
        await loadCopyMap();
        renderPreviewPane();
    } catch (err) {
        setActionResult("Copy failed.");
        logError(`Copy to RC-2 failed: ${err.message}`);
    } finally {
        button.disabled = false;
    }
}

// Finds dummy.kmz in the root of the chosen PC/Cloud folder (not in subfolders).
async function findRootDummyFile() {
    if (pcDirHandle) {
        try {
            const handle = await pcDirHandle.getFileHandle("dummy.kmz");
            return await handle.getFile();
        } catch (err) {
            return null;
        }
    }
    return pcFallbackFiles.find(file => {
        const segments = (file.webkitRelativePath || file.name).split("/").filter(Boolean);
        return segments.length <= 2 && segments[segments.length - 1].toLowerCase() === "dummy.kmz";
    }) || null;
}

async function restoreDummy() {
    const destination = lastMissionsData.find(mission => mission.is_dummy);
    if (!destination) {
        setActionResult("Configure a dummy mission slot first.");
        return;
    }
    if (!window.confirm(`Restore dummy.kmz to ${destination.guid} (dummy slot)?\nExisting mission content will be replaced.`)) return;
    setActionResult("Restoring dummy...");
    const button = document.getElementById("restore-dummy-btn");
    button.disabled = true;
    logInfo("Restoring dummy.kmz into the dummy RC-2 mission.");
    try {
        const file = await findRootDummyFile();
        if (!file) {
            setActionResult("dummy.kmz not found in the root of the PC/Cloud folder.");
            logWarning("Restore dummy ignored: dummy.kmz is not in the root of the chosen PC/Cloud folder.");
            return;
        }
        const formData = new FormData();
        formData.append("file", file, "dummy.kmz");
        formData.append("target_guid", destination.guid);
        const res = await fetch("/api/upload", { method: "POST", headers: { "X-Requested-With": "PiDjiRc2KmzSync" }, body: formData });
        const data = await res.json();
        if (!res.ok || data.error) {
            const error = data.error || `HTTP ${res.status}`;
            setActionResult(userErrorMessage(error));
            logError(`Restore dummy failed: ${error}`);
            return;
        }
        setActionResult(`Restored dummy mission ${data.mission_guid} as ${data.dest_filename}`);
        logInfo(`Restored dummy mission ${data.mission_guid} as ${data.dest_filename}.`);
        logInfo(data.message);
        await refreshMissions(false, false);
        await loadCopyMap();
        renderPreviewPane();
    } catch (err) {
        setActionResult("Restore dummy failed.");
        logError(`Restore dummy failed: ${err.message}`);
    } finally {
        button.disabled = false;
    }
}

async function copyRc2ToPc() {
    if (!selectedMissionGuid) {
        setActionResult("Select an RC-2 mission first.");
        logWarning("Copy to PC ignored: no RC-2 mission is selected.");
        return;
    }
    if (!selectedPcFile) {
        setActionResult("Select a target file in the PC/Cloud folder tree first.");
        logWarning("Copy to PC ignored: no target PC/Cloud file is selected.");
        return;
    }

    const missionGuid = selectedMissionGuid;
    const targetFile = selectedPcFile;
    setActionResult("Copying...");
    logInfo(`Copying RC-2 mission ${missionGuid} to ${targetFile.name}.`);
    try {
        const res = await fetch(`/api/download/${encodeURIComponent(missionGuid)}`);
        if (!res.ok) {
            let error = `HTTP ${res.status}${res.statusText ? ` ${res.statusText}` : ""}`;
            try {
                const data = await res.json();
                if (data.error) error = data.error;
            } catch (err) {
                logWarning(`Could not read download error response: ${err.message}`);
            }
            setActionResult(userErrorMessage(error));
            logError(`Copy from RC-2 failed: ${error}`);
            return;
        }
        const blob = await res.blob();

        if (!targetFile.handle) {
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = `${missionGuid}.kmz`;
            document.body.appendChild(link);
            try {
                link.click();
            } finally {
                link.remove();
                window.setTimeout(() => URL.revokeObjectURL(url), 60000);
            }
            setActionResult("Download started. This browser cannot overwrite the selected file directly.");
            logWarning("Selected browser cannot overwrite the PC/Cloud file; download started instead.");
            return;
        }

        const permission = await targetFile.handle.requestPermission({ mode: "readwrite" });
        if (permission !== "granted") {
            setActionResult("Write permission was not granted for the selected file.");
            logWarning("Copy from RC-2 cancelled: write permission was not granted.");
            return;
        }

        const writable = await targetFile.handle.createWritable();
        await writable.write(blob);
        await writable.close();

        setActionResult(`Copied RC-2 mission ${missionGuid} into ${targetFile.name}`);
        logInfo(`Copied RC-2 mission ${missionGuid} into ${targetFile.name}.`);
    } catch (err) {
        setActionResult("Copy failed.");
        logError(`Copy from RC-2 failed: ${err.message}`);
    }
}

async function associateSelected() {
    if (!selectedMissionGuid) {
        setActionResult("Select an RC-2 mission first.");
        logWarning("Association ignored: no RC-2 mission is selected.");
        return;
    }
    if (!selectedPcFile) {
        setActionResult("Select a PC/Cloud file first.");
        logWarning("Association ignored: no PC/Cloud file is selected.");
        return;
    }

    setActionResult("Associating...");
    logInfo(`Associating mission ${selectedMissionGuid} with ${selectedPcFile.name}.`);
    try {
        const res = await fetch(`/api/missions/${encodeURIComponent(selectedMissionGuid)}/associate`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Requested-With": "PiDjiRc2KmzSync" },
            body: JSON.stringify({
                source_filename: selectedPcFile.name,
                source_relative_path: selectedPcFile.name,
            }),
        });
        const data = await res.json();
        if (!res.ok || data.error) {
            const error = data.error || `HTTP ${res.status}`;
            setActionResult(userErrorMessage(error));
            logError(`Mission association failed: ${error}`);
            return;
        }
        setActionResult(data.message || "Associated.");
        logInfo(data.message || "Mission association saved.");
        await loadCopyMap();
        renderMissionList(false);   // associations are browser-side data: redraw only, no RC-2 refresh
        renderPreviewPane();
    } catch (err) {
        setActionResult("Associate failed.");
        logError(`Mission association failed: ${err.message}`);
    }
}

// ---------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------
document.getElementById("save-rc2-root-btn").addEventListener("click", saveRc2Root);
document.getElementById("set-dummy-slot-btn").addEventListener("click", setDummySlot);
document.getElementById("choose-pc-folder-btn").addEventListener("click", choosePcFolder);
document.getElementById("sort-missions-btn").addEventListener("click", (e) => {
    missionSortMode = missionSortMode === "modified" ? "name" : "modified";
    e.currentTarget.textContent = missionSortMode === "modified" ? "Sort: Newest" : "Sort: KMZ name";
    renderMissionList(false);
});
document.getElementById("refresh-missions-btn").addEventListener("click", () => refreshMissions(true, false));
document.getElementById("refresh-pc-btn").addEventListener("click", renderPcTree);
document.getElementById("copy-btn").addEventListener("click", copySelectedToRc2);
document.getElementById("download-btn").addEventListener("click", copyRc2ToPc);
document.getElementById("associate-btn").addEventListener("click", associateSelected);
document.getElementById("restore-dummy-btn").addEventListener("click", restoreDummy);
document.getElementById("copy-log-btn").addEventListener("click", copyActivityLog);
document.getElementById("clear-log-btn").addEventListener("click", clearActivityLog);
document.getElementById("preview-image").addEventListener("click", openImageLightbox);
document.getElementById("close-lightbox-btn").addEventListener("click", closeImageLightbox);
document.getElementById("image-lightbox").addEventListener("click", (event) => {
    if (event.target.id === "image-lightbox") closeImageLightbox();
});
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeImageLightbox();
});

document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (button && !["copy-log-btn", "clear-log-btn", "copy-btn"].includes(button.id)) {
        logInfo(`Action started: ${button.textContent.trim()}`);
    }
});

window.addEventListener("error", (event) => {
    logError(`Uncaught error: ${event.message}`);
});

window.addEventListener("unhandledrejection", (event) => {
    logError(`Unhandled promise rejection: ${event.reason?.message || event.reason}`);
});

if (!supportsDirectoryPicker) {
    // Inject a hidden webkitdirectory input for the Safari/iOS fallback path.
    const fallbackInput = document.createElement("input");
    fallbackInput.type = "file";
    fallbackInput.id = "pc-folder-fallback-input";
    if ("webkitdirectory" in fallbackInput) {
        fallbackInput.webkitdirectory = true;
    } else {
        fallbackInput.multiple = true;
        fallbackInput.accept = ".kmz";
        document.getElementById("choose-pc-folder-btn").textContent = "Choose KMZ files...";
    }
    fallbackInput.style.display = "none";
    fallbackInput.addEventListener("change", (e) => handleFallbackFolderPick(e.target.files));
    document.body.appendChild(fallbackInput);
}

(async function init() {
    logInfo("Application started.");
    window.setInterval(pingServer, STATUS_POLL_INTERVAL_MS);
    window.setInterval(refreshStatus, STATUS_POLL_INTERVAL_MS);
    pingServer();
    await loadConfig();
    await refreshStatus();
    await loadCopyMap();
    await refreshMissions();
    renderPreviewPane();

    if (supportsDirectoryPicker) {
        const savedHandle = await loadDirHandle().catch(() => null);
        if (savedHandle) {
            const permission = await savedHandle.queryPermission({ mode: "readwrite" });
            if (permission === "granted") {
                pcDirHandle = savedHandle;
                document.getElementById("pc-folder-display").textContent = savedHandle.name;
                renderPcTree();
            } else {
                document.getElementById("pc-folder-display").textContent =
                    `${savedHandle.name} (click Choose folder, then Allow, to reauthorize)`;
            }
        }
    }
})();
