"""Browser check. Usage: e2e_browser.py PORT"""
import sys
from playwright.sync_api import sync_playwright
port = sys.argv[1]
base = f"http://127.0.0.1:{port}"
GUID = "00000001-1111-2222-3333-444444444441"
ok = True
def check(n, c):
    global ok; print(("PASS " if c else "FAIL ") + n); ok = ok and bool(c)
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome") if False else p.chromium.launch()
    pg = b.new_page()
    reqs = []
    pg.on("request", lambda r: reqs.append(r.url.replace(base, "")))
    pg.goto(base); pg.wait_for_selector("#mission-list li.mission", timeout=15000)
    check("page load listed the mission", pg.locator("#mission-list li.mission").count() == 1)
    check("page load used a FRESH listing (no ?cached)", any(u == "/api/missions" for u in reqs))
    scans0 = int(pg.evaluate("fetch('/__scans').then(r=>r.text())"))
    # pretend the user selected a mission and a PC file, then press Associate
    pg.evaluate(f"selectedMissionGuid = '{GUID}'; selectedPcFile = {{ name: 'plan-A.kmz' }};")
    reqs.clear()
    pg.click("#associate-btn"); pg.wait_for_timeout(1500)
    check("association POST was made", any("/associate" in u for u in reqs))
    check("copy map reloaded", "/api/copy-map" in reqs)
    check("NO /api/missions request after association", not any(u.startswith("/api/missions") and "/associate" not in u and "/thumbnail" not in u for u in reqs))
    txt = pg.locator("#mission-list li.mission .meta").first.inner_text()
    check("list now shows 'linked: plan-A.kmz'", "linked: plan-A.kmz" in txt)
    check("selection kept after redraw", pg.locator("#mission-list li.mission.selected").count() == 1)
    scans1 = int(pg.evaluate("fetch('/__scans').then(r=>r.text())"))
    check("association caused no device rescan", scans1 == scans0)
    reqs.clear()
    pg.click("#refresh-missions-btn"); pg.wait_for_timeout(1500)
    check("Refresh button asks for a fresh listing", "/api/missions" in reqs and not any("cached" in u for u in reqs))
    scans2 = int(pg.evaluate("fetch('/__scans').then(r=>r.text())"))
    check("Refresh button rescans the device", scans2 > scans1)
    b.close()
print("\nALL PASS" if ok else "\nSOME FAILED"); sys.exit(0 if ok else 1)
