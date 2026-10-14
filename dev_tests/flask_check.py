"""Route-level and unit checks on the merged code with a fake RC-2.
Usage: python3 -I flask_check.py CODE_DIR
"""
import json
import os
import sys
import tempfile
import types

code_dir = sys.argv[1]
work = tempfile.mkdtemp(prefix="chk-")
os.environ["TMPDIR"] = work
tempfile.tempdir = work
ROOT = "mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint"
cfg = os.path.join(work, "cfg.json")
json.dump({"rc2_folder": ROOT}, open(cfg, "w"))
os.environ["PIDJIRC2KMZSYNC_CONFIG_FILE"] = cfg
sys.path.insert(0, code_dir)

class Folder:
    def __init__(s, i, p, n): s.folder_id, s.parent_id, s.name, s.storage_id = i, p, n.encode(), 1
class File:
    def __init__(s, i, p, n, size=1000, **kw):
        s.item_id, s.parent_id, s.filename, s.filesize = i, p, (n if isinstance(n, bytes) else n.encode()), size
        s.modificationdate, s.storage_id, s.filetype = 1760000000, 1, 0
GUID = "00000001-1111-2222-3333-444444444441"
folders = {1: Folder(1, 0, "Android"), 2: Folder(2, 1, "data"), 3: Folder(3, 2, "dji.go.v5"),
           4: Folder(4, 3, "files"), 5: Folder(5, 4, "waypoint"), 6: Folder(6, 5, "map_preview"),
           7: Folder(7, 5, GUID)}
files = [File(10, 7, GUID + ".kmz"), File(11, 6, GUID + ".jpg")]

SCANS = [0]
class Inner:
    LIBMTP_GetPartialObject = None
    LIBMTP_Get_Filemetadata = None
class FakeMTP:
    def __init__(s): s.mtp, s.device = Inner(), object()
    def detect_devices(s): return [1]
    def connect(s): pass
    def disconnect(s): pass
    def get_serialnumber(s): return "SN"
    def get_folder_list(s): return dict(folders)
    def get_filelisting(s):
        SCANS[0] += 1
        return list(files)
    def delete_object(s, i): pass
    def find_filetype(s, p): return 0
fake = types.ModuleType("pymtp"); fake.MTP = FakeMTP; fake.LIBMTP_File = File
sys.modules["pymtp"] = fake

from backends.mtp import pi_mtp_backend as B  # noqa: E402
def fake_pull(self, item_id, dest):
    from PIL import Image
    Image.new("RGB", (8, 8), (1, 2, 3)).save(dest, "JPEG"); return True
B.PiMTPBackend._pull_to_path_fast = fake_pull

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and bool(cond)

from flask_app.app import create_app  # noqa: E402
app = create_app()
c = app.test_client()

check("/api/ping answers", c.get("/api/ping").get_json() == {"ok": True})
r = c.get("/api/missions").get_json()
check("missions listed", [m["guid"] for m in r["missions"]] == [GUID])
r = c.get(f"/api/missions/{GUID}/thumbnail")
check("thumbnail served (200, image bytes)", r.status_code == 200 and len(r.data) > 0)
r = c.get("/api/missions/..%2F..%2Fetc/thumbnail")
check("path-traversal-looking id rejected (404)", r.status_code == 404)
r = c.get("/api/missions/not$a$guid/thumbnail")
check("junk id rejected (404)", r.status_code == 404)
r = c.get("/api/missions/ffffffff-0000-0000-0000-000000000000/thumbnail")
check("unknown but well-formed id -> 404, no crash", r.status_code == 404)

# per-mission cache clear leaves other previews alone
from backends.rc_backend import _preview_cache_base  # noqa: E402
vm = None
for rule in app.url_map.iter_rules():
    pass
import gc  # noqa: E402
from viewmodel.mission_viewmodel import MissionViewModel  # noqa: E402
vm = next(o for o in gc.get_objects() if isinstance(o, MissionViewModel))
base_a = _preview_cache_base(ROOT, GUID)
other = _preview_cache_base(ROOT, "someother")
open(other + ".jpg", "wb").write(b"x")
check("preview cached on disk for GUID", os.path.exists(base_a + ".jpg"))
vm._clear_preview_cache_for_guid(GUID)
check("clear removes that mission's preview", not os.path.exists(base_a + ".jpg"))
check("clear leaves other missions' previews", os.path.exists(other + ".jpg"))

# listing-cache patch helpers
be = vm._rc_backend
with be._session():
    be._all_files("test")
    n0 = len(be._file_cache)
    be._cache_add_file(File(99, 7, "new.kmz"))
    check("cache add visible in parent index", any(int(e.item_id) == 99 for e in be._files_by_parent_cache[7]))
    be._cache_remove_file(99)
    check("cache remove drops it again", len(be._file_cache) == n0 and
          not any(int(e.item_id) == 99 for e in be._files_by_parent_cache[7]))
    # device cannot report metadata -> must fall back to dropping the cache (honest sizes)
    class Sent: item_id = 99
    be._record_uploaded_file(folders[7], "new.kmz", Sent())
    check("no device metadata -> listing cache dropped (safe fallback)", be._file_cache is None)

# ---- cached vs fresh mission listing (the post-association / post-upload path) ----
c.get("/api/missions")                       # fresh: builds the index
scans_before = SCANS[0]
with be._session():                          # simulate an upload patched in place
    for e in list(be._file_cache):
        if int(e.item_id) == 10:
            be._cache_remove_file(10)
    be._cache_add_file(File(10, 7, "patched-by-upload.kmz"))
r = c.get("/api/missions?cached=1").get_json()
check("cached listing triggers no device rescan", SCANS[0] == scans_before)
check("cached listing shows the in-place patched file", r["missions"][0]["kmz_name"] == "patched-by-upload.kmz")
r = c.get("/api/missions").get_json()
check("plain listing (explicit Refresh) rescans the device", SCANS[0] > scans_before)
check("plain listing shows what the device really holds", r["missions"][0]["kmz_name"] == GUID + ".kmz")
r = c.get("/api/missions?cached=0").get_json()
check("cached=0 is treated as a fresh listing", r["missions"][0]["kmz_name"] == GUID + ".kmz")

# transfer-cache invalidation keeps the index, refresh drops it
with be._session():
    be._all_files("test")
be.invalidate_transfer_caches()
check("invalidate_transfer_caches keeps the index", be._file_cache is not None)
be.invalidate_cache()
check("invalidate_cache (refresh) drops the index", be._file_cache is None)
print("\nALL PASS" if ok else "\nSOME FAILED")
sys.exit(0 if ok else 1)
