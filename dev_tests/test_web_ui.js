const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../code/client/static/app.js"), "utf8")
    .split("// Wiring")[0];

function makeUI(fetch) {
    const elements = {
        "activity-log": { value: "", scrollTop: 0, scrollHeight: 0 },
        "action-result": { textContent: "" },
        "pi-status": { textContent: "", classList: { toggle() {} } },
        "mission-list": { replaceChildren(child) { this.child = child; } },
    };
    const timers = new Map();
    const downloads = [];
    const revoked = [];
    let timerId = 0;
    const context = vm.createContext({
        AbortController,
        fetch,
        URL: {
            createObjectURL(blob) { downloads.push({ blob }); return "blob:test"; },
            revokeObjectURL(url) { revoked.push(url); },
        },
        window: {
            setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
            clearTimeout(id) { timers.delete(id); },
        },
        document: {
            getElementById(id) { return elements[id]; },
            createElement(tag) {
                if (tag !== "a") return { textContent: "" };
                return {
                    click() { downloads[downloads.length - 1].link = this; },
                    remove() {},
                };
            },
            body: { appendChild() {} },
        },
    });
    vm.runInContext(source, context);
    return { context, elements, timers, downloads, revoked };
}

test("fallback download shows device errors without navigating or downloading", async () => {
    const ui = makeUI(async () => ({
        ok: false, status: 400,
        json: async () => ({ error: "MTP read failed: NoDeviceConnected: technical details" }),
    }));
    vm.runInContext('selectedMissionGuid = "mission"; selectedPcFile = { name: "target.kmz" };', ui.context);
    await vm.runInContext("copyRc2ToPc()", ui.context);
    assert.equal(ui.elements["action-result"].textContent, "RC-2 not connected. Plug the RC-2 into the updater, then press Refresh.");
    assert.match(ui.elements["activity-log"].value, /NoDeviceConnected: technical details/);
    assert.equal(ui.downloads.length, 0);
});

test("fallback download uses the fetched blob and keeps the selected mission filename", async () => {
    const blob = new Blob(["kmz"]);
    const ui = makeUI(async () => ({ ok: true, blob: async () => blob }));
    vm.runInContext('selectedMissionGuid = "mission"; selectedPcFile = { name: "target.kmz" };', ui.context);
    await vm.runInContext("copyRc2ToPc()", ui.context);
    assert.equal(ui.downloads.length, 1);
    assert.equal(ui.downloads[0].blob, blob);
    assert.equal(ui.downloads[0].link.download, "mission.kmz");
    assert.match(ui.elements["action-result"].textContent, /Download started/);
    const cleanup = [...ui.timers.values()].find(timer => timer.delay === 60000);
    cleanup.fn();
    assert.equal(ui.revoked[0], "blob:test");
});

test("writable file handles still overwrite the selected file", async () => {
    const blob = new Blob(["kmz"]);
    const ui = makeUI(async () => ({ ok: true, blob: async () => blob }));
    let written;
    let closed = false;
    ui.context.target = {
        name: "target.kmz",
        handle: {
            requestPermission: async () => "granted",
            createWritable: async () => ({
                write: async value => { written = value; },
                close: async () => { closed = true; },
            }),
        },
    };
    vm.runInContext('selectedMissionGuid = "mission"; selectedPcFile = target;', ui.context);
    await vm.runInContext("copyRc2ToPc()", ui.context);
    assert.equal(written, blob);
    assert.equal(closed, true);
    assert.equal(ui.downloads.length, 0);
    assert.match(ui.elements["action-result"].textContent, /Copied RC-2 mission/);
});

test("download network failures stay on the page and are logged", async () => {
    const ui = makeUI(async () => { throw new Error("network unavailable"); });
    vm.runInContext('selectedMissionGuid = "mission"; selectedPcFile = { name: "target.kmz" };', ui.context);
    await vm.runInContext("copyRc2ToPc()", ui.context);
    assert.equal(ui.elements["action-result"].textContent, "Copy failed.");
    assert.match(ui.elements["activity-log"].value, /network unavailable/);
    assert.equal(ui.downloads.length, 0);
});

test("mission errors retain technical text in log", async () => {
    const ui = makeUI(async () => ({
        ok: true, json: async () => ({ error: "NoDeviceConnected: raw diagnostic" }),
    }));
    await vm.runInContext("refreshMissions(false)", ui.context);
    assert.equal(ui.elements["mission-list"].child.textContent, "RC-2 not connected. Plug the RC-2 into the updater, then press Refresh.");
    assert.match(ui.elements["activity-log"].value, /NoDeviceConnected: raw diagnostic/);
});

test("connection errors do not hide failed recovery and backup details", () => {
    const ui = makeUI(async () => {});
    const message = vm.runInContext(
        'userErrorMessage("NoDeviceConnected\\nRESTORE FAILED: Manual recovery is required.\\nBackup: /backup.kmz")',
        ui.context,
    );
    assert.match(message, /RC-2 not connected/);
    assert.match(message, /RESTORE FAILED/);
    assert.match(message, /Backup: \/backup.kmz/);
});

test("a stalled ping times out and a later ping restores the updater state", async () => {
    let mode = "ok";
    const ui = makeUI((_url, options) => {
        if (mode === "ok") return Promise.resolve({ ok: true });
        return new Promise((_resolve, reject) => {
            options.signal.addEventListener("abort", () => reject(new Error("aborted")));
        });
    });
    await vm.runInContext("pingServer()", ui.context);
    assert.equal(ui.elements["pi-status"].textContent, "PiRC2kmzUpdater connected");
    mode = "stalled";
    const pending = vm.runInContext("pingServer()", ui.context);
    [...ui.timers.values()].find(timer => timer.delay === 10000).fn();
    await pending;
    [...ui.timers.values()].find(timer => timer.delay === 15000).fn();
    assert.equal(ui.elements["pi-status"].textContent, "PiRC2kmzUpdater not connected");
    assert.match(ui.elements["activity-log"].value, /Updater connection lost/);
    mode = "ok";
    await vm.runInContext("pingServer()", ui.context);
    assert.equal(ui.elements["pi-status"].textContent, "PiRC2kmzUpdater connected");
    assert.match(ui.elements["activity-log"].value, /Updater connection re-established/);
});
