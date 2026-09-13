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

// ---------------------------------------------------------------------
// Status / config
// ---------------------------------------------------------------------
async function refreshStatus() {
    const statusEl = document.getElementById("status");
    try {
        const res = await fetch("/api/status");
        const data = await res.json();
        statusEl.textContent = data.connected
            ? `RC-2 connected (${data.connection_mode})`
            : `RC-2 not connected (root: ${data.rc2_root || "not set"})`;
    } catch (err) {
        statusEl.textContent = "Error checking status";
    }
}

async function loadConfig() {
    try {
        const res = await fetch("/api/config");
        const data = await res.json();
        document.getElementById("rc2-root-input").value = data.rc2_root || "";
        document.getElementById("dummy-slot-display").textContent =
            data.dummy_slot_guid || "(none set)";
    } catch (err) {
        // leave fields blank on error
    }
}

async function saveRc2Root() {
    const root = document.getElementById("rc2-root-input").value.trim();
    if (!root) return;
    await fetch("/api/config/rc2-root", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root }),
    });
    refreshStatus();
}

async function setDummySlot() {
    if (!selectedMissionGuid) {
        setActionResult("Select an RC-2 mission first, then set it as the dummy slot.");
        return;
    }
    const res = await fetch("/api/config/dummy-slot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ guid: selectedMissionGuid }),
    });
    const data = await res.json();
    document.getElementById("dummy-slot-display").textContent = data.dummy_slot_guid || "(none set)";
    refreshMissions();
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
    } catch (err) {
        // leave copyMapByGuid as-is on error
    }
}

// ---------------------------------------------------------------------
// RC-2 mission list (left pane)
// ---------------------------------------------------------------------
async function refreshMissions(showLoading = true) {
    const listEl = document.getElementById("mission-list");
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
            renderPcTree();
        } catch (err) {
            // user cancelled the picker; nothing to do
        }
        return;
    }

    // Fallback for browsers without the File System Access API (e.g. Safari/iOS):
    // no persistent handle is possible, so re-pick each session via a plain
    // folder-scoped file input.
    const input = document.getElementById("pc-folder-fallback-input");
    input.click();
}

function handleFallbackFolderPick(fileList) {
    // Build a simple flat list from webkitRelativePath; no persistence across
    // sessions is possible with this fallback (Safari does not support the
    // File System Access API), so this must be re-chosen each visit.
    const files = Array.from(fileList).filter(f => f.name.toLowerCase().endsWith(".kmz"));
    const treeEl = document.getElementById("pc-tree");
    treeEl.innerHTML = "";
    for (const file of files) {
        const li = document.createElement("li");
        li.textContent = file.webkitRelativePath || file.name;
        li.addEventListener("click", () => {
            // No FileSystemFileHandle is available from this fallback input,
            // so this selection can be an upload source but not a
            // Copy RC-2 -> PC overwrite target (see copyRc2ToPc()).
            selectedPcFile = { name: file.name, getFile: async () => file, handle: null };
            renderPcSelectionHighlight(li);
        });
        treeEl.appendChild(li);
    }
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
        return;
    }

    setActionResult("Copying...");
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
            return;
        }
        setActionResult(`Copied to mission ${data.mission_guid} as ${data.dest_filename}`);
        refreshMissions();
        loadCopyMap();
    } catch (err) {
        setActionResult("Copy failed.");
    }
}

async function copyRc2ToPc() {
    if (!selectedMissionGuid) {
        setActionResult("Select an RC-2 mission first.");
        return;
    }
    if (!selectedPcFile) {
        setActionResult("Select a target file in the PC/Cloud folder tree first.");
        return;
    }

    if (!selectedPcFile.handle) {
        // Fallback path (Safari, or a file picked via the webkitdirectory
        // input): there's no writable file handle, so the best we can do
        // is a plain browser download rather than overwriting the chosen
        // file in place.
        setActionResult("Can't write directly to the selected file in this browser -- downloading instead.");
        window.location.href = `/api/download/${encodeURIComponent(selectedMissionGuid)}`;
        return;
    }

    setActionResult("Copying...");
    try {
        const res = await fetch(`/api/download/${encodeURIComponent(selectedMissionGuid)}`);
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            setActionResult(`Error: ${data.error || res.statusText}`);
            return;
        }
        const blob = await res.blob();

        const permission = await selectedPcFile.handle.requestPermission({ mode: "readwrite" });
        if (permission !== "granted") {
            setActionResult("Write permission was not granted for the selected file.");
            return;
        }

        const writable = await selectedPcFile.handle.createWritable();
        await writable.write(blob);
        await writable.close();

        setActionResult(`Copied RC-2 mission ${selectedMissionGuid} into ${selectedPcFile.name}`);
    } catch (err) {
        setActionResult("Copy failed.");
    }
}

async function associateSelected() {
    if (!selectedMissionGuid) {
        setActionResult("Select an RC-2 mission first.");
        return;
    }
    if (!selectedPcFile) {
        setActionResult("Select a PC/Cloud file first.");
        return;
    }

    setActionResult("Associating...");
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
            return;
        }
        setActionResult(data.message || "Associated.");
        await loadCopyMap();
        await refreshMissions(false);
        renderPreviewPane();
    } catch (err) {
        setActionResult("Associate failed.");
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
