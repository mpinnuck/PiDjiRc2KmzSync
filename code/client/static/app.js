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
let selectedPcFile = null;       // { name, getFile: () => Promise<File>, handle: FileSystemFileHandle|null }
let lastMissionsData = [];       // last /api/missions response, for the preview pane
let copyMapByGuid = {};          // target_mission_guid (lowercased) -> summary row
let statusRefreshInProgress = false;
let lastKnownConnectionState = null;
let piStatusWatchdogTimer = null;

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
}

function armPiStatusWatchdog() {
    if (piStatusWatchdogTimer !== null) {
        window.clearTimeout(piStatusWatchdogTimer);
    }
    piStatusWatchdogTimer = window.setTimeout(() => {
        setPiStatus(false);
    }, STATUS_WATCHDOG_TIMEOUT_MS);
}

async function refreshStatus() {
    if (statusRefreshInProgress) return;
    statusRefreshInProgress = true;
    const statusEl = document.getElementById("status");
    try {
        const res = await fetch("/api/status");
        if (!res.ok) throw new Error(`Status request failed (${res.status})`);
        const data = await res.json();
        setPiStatus(true);
        armPiStatusWatchdog();
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
        if (connectionStateChanged) {
            refreshMissions(false);
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
            headers: { "Content-Type": "application/json" },
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
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ guid: selectedMissionGuid }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        document.getElementById("dummy-slot-display").textContent = data.dummy_slot_guid || "(none set)";
        logInfo(`Dummy mission slot set to ${selectedMissionGuid}.`);
        refreshMissions();
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
async function refreshMissions(showLoading = true) {
    const listEl = document.getElementById("mission-list");
    logInfo("Refreshing RC-2 missions.");
    if (showLoading) {
        listEl.innerHTML = "<li>Loading...</li>";
    }
    try {
        const res = await fetch("/api/missions");
        const data = await res.json();
        listEl.innerHTML = "";

        if (data.error) {
            listEl.innerHTML = `<li>${data.error}</li>`;
            lastMissionsData = [];
            logWarning(`RC-2 mission list warning: ${data.error}`);
            return;
        }

        lastMissionsData = data.missions;

        for (const mission of data.missions) {
            const li = document.createElement("li");
            li.classList.add("mission");
            li.dataset.guid = mission.guid;
            if (mission.is_dummy) li.classList.add("dummy-slot");
            if (mission.guid === selectedMissionGuid) li.classList.add("selected");

            const label = mission.is_dummy ? `${mission.guid} (dummy slot)` : mission.guid;
            const linked = copyMapByGuid[mission.guid.toLowerCase()];
            const linkedNote = linked ? ` &middot; linked: ${linked.source_filename}` : "";

            const img = document.createElement("img");
            img.className = "thumb";
            img.alt = "";
            img.src = `/api/missions/${encodeURIComponent(mission.guid)}/thumbnail?t=${Date.now()}`;
            img.addEventListener("error", () => { img.style.visibility = "hidden"; });

            const textDiv = document.createElement("div");
            textDiv.className = "mission-text";
            textDiv.innerHTML = `${label}<span class="meta">${mission.display_kmz_name} &middot; ${mission.last_modified}${linkedNote}</span>`;

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
    } catch (err) {
        listEl.innerHTML = "<li>Error loading missions.</li>";
        lastMissionsData = [];
        logError(`Loading RC-2 missions failed: ${err.message}`);
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

    infoEl.innerHTML = rows
        .map(([label, value]) => `<dt>${label}</dt><dd>${value}</dd>`)
        .join("");
}

// ---------------------------------------------------------------------
// PC / Cloud folder tree (right pane)
// ---------------------------------------------------------------------
async function choosePcFolder() {
    if (supportsDirectoryPicker) {
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
    const treeEl = document.getElementById("pc-tree");
    treeEl.innerHTML = "";

    const tree = buildFolderTreeFromFiles(files);
    treeEl.appendChild(renderFolderTreeNode(tree));

    document.getElementById("pc-folder-display").textContent = "(chosen this session only)";
    document.getElementById("pc-folder-note").textContent =
        "Your browser doesn't support persistent folder access; re-choose the folder each visit. " +
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

    setActionResult("Copying...");
    const destinationMission = selectedMissionGuid
        ? lastMissionsData.find(mission => mission.guid === selectedMissionGuid)
        : lastMissionsData.find(mission => mission.is_dummy);
    const destinationName = destinationMission?.display_kmz_name ||
        `${destinationMission?.guid || "dummy slot"}.kmz (resolved by RC-2)`;
    logInfo(`Copy selected KMZ -> RC-2: ${selectedPcFile.name} -> ${destinationName}`);
    try {
        const file = await selectedPcFile.getFile();
        const formData = new FormData();
        formData.append("file", file, selectedPcFile.name);
        if (selectedMissionGuid) {
            formData.append("target_guid", selectedMissionGuid);
        }
        // If no mission is selected, target_guid is omitted and the server
        // falls back to the configured dummy mission slot.

        const res = await fetch("/api/upload", { method: "POST", body: formData });
        const data = await res.json();
        if (data.error) {
            setActionResult(`Error: ${data.error}`);
            logError(`Copy to RC-2 failed: ${data.error}`);
            return;
        }
        setActionResult(`Copied to mission ${data.mission_guid} as ${data.dest_filename}`);
        logInfo(`Copied to mission ${data.mission_guid} as ${data.dest_filename}.`);
        refreshMissions();
        loadCopyMap();
    } catch (err) {
        setActionResult("Copy failed.");
        logError(`Copy to RC-2 failed: ${err.message}`);
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

    if (!selectedPcFile.handle) {
        // Fallback path (Safari, or a file picked via the webkitdirectory
        // input): there's no writable file handle, so the best we can do
        // is a plain browser download rather than overwriting the chosen
        // file in place.
        setActionResult("Can't write directly to the selected file in this browser -- downloading instead.");
        logWarning("Selected browser cannot overwrite the PC/Cloud file; starting a browser download instead.");
        window.location.href = `/api/download/${encodeURIComponent(selectedMissionGuid)}`;
        return;
    }

    setActionResult("Copying...");
    logInfo(`Copying RC-2 mission ${selectedMissionGuid} to ${selectedPcFile.name}.`);
    try {
        const res = await fetch(`/api/download/${encodeURIComponent(selectedMissionGuid)}`);
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            setActionResult(`Error: ${data.error || res.statusText}`);
            logError(`Copy from RC-2 failed: ${data.error || res.statusText}`);
            return;
        }
        const blob = await res.blob();

        const permission = await selectedPcFile.handle.requestPermission({ mode: "readwrite" });
        if (permission !== "granted") {
            setActionResult("Write permission was not granted for the selected file.");
            logWarning("Copy from RC-2 cancelled: write permission was not granted.");
            return;
        }

        const writable = await selectedPcFile.handle.createWritable();
        await writable.write(blob);
        await writable.close();

        setActionResult(`Copied RC-2 mission ${selectedMissionGuid} into ${selectedPcFile.name}`);
        logInfo(`Copied RC-2 mission ${selectedMissionGuid} into ${selectedPcFile.name}.`);
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
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                source_filename: selectedPcFile.name,
                source_relative_path: selectedPcFile.name,
            }),
        });
        const data = await res.json();
        if (data.error) {
            setActionResult(`Error: ${data.error}`);
            logError(`Mission association failed: ${data.error}`);
            return;
        }
        setActionResult(data.message || "Associated.");
        logInfo(data.message || "Mission association saved.");
        await loadCopyMap();
        await refreshMissions(false);
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
document.getElementById("refresh-missions-btn").addEventListener("click", refreshMissions);
document.getElementById("refresh-pc-btn").addEventListener("click", renderPcTree);
document.getElementById("copy-btn").addEventListener("click", copySelectedToRc2);
document.getElementById("download-btn").addEventListener("click", copyRc2ToPc);
document.getElementById("associate-btn").addEventListener("click", associateSelected);
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
    fallbackInput.webkitdirectory = true;
    fallbackInput.style.display = "none";
    fallbackInput.addEventListener("change", (e) => handleFallbackFolderPick(e.target.files));
    document.body.appendChild(fallbackInput);
}

(async function init() {
    logInfo("Application started.");
    window.setInterval(refreshStatus, STATUS_POLL_INTERVAL_MS);
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
                    `${savedHandle.name} (click Choose folder to reauthorize)`;
            }
        }
    }
})();
