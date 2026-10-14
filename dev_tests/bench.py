"""Fake-RC-2 benchmark. Usage: python3 -I bench.py CODE_DIR LABEL

Runs the same four scenarios against whichever code tree it is pointed at and
prints how many expensive device operations each one cost. Latencies are
scaled-down stand-ins (full listing 0.30s, reconnect 0.50s, object pull 0.05s,
USB round trip 0.005s) so the relative cost is visible without real hardware.
"""
import json
import os
import sys
import tempfile
import threading
import time
import types

code_dir, label = sys.argv[1], sys.argv[2]
work = tempfile.mkdtemp(prefix="bench-")
os.environ["TMPDIR"] = work
tempfile.tempdir = work
cfg = os.path.join(work, "cfg.json")
ROOT = "mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint"
json.dump({"rc2_folder": ROOT}, open(cfg, "w"))
os.environ["PIDJIRC2KMZSYNC_CONFIG_FILE"] = cfg
os.environ["PIDJIRC2KMZSYNC_BATTERY_GPIO_DISABLED"] = "1"
sys.path.insert(0, code_dir)

COUNTS = {}
def bump(k):
    COUNTS[k] = COUNTS.get(k, 0) + 1

LAT = {"filelisting": 0.30, "connect": 0.50, "pull": 0.05, "usb": 0.005}

# ---- fake device tree -------------------------------------------------------
class Folder:
    def __init__(self, folder_id, parent_id, name):
        self.folder_id, self.parent_id, self.name, self.storage_id = folder_id, parent_id, name.encode(), 1
class File:
    def __init__(self, item_id, parent_id, filename, size=1000):
        self.item_id, self.parent_id, self.filename, self.filesize = item_id, parent_id, filename.encode(), size
        self.modificationdate, self.storage_id, self.filetype = 1760000000, 1, 0

GUIDS = [f"0000000{i}-1111-2222-3333-44444444444{i}" for i in range(8)]
folders, files = {}, []
nid = [100]
def nxt():
    nid[0] += 1
    return nid[0]
a = Folder(1, 0, "Android"); folders[1] = a
d = Folder(2, 1, "data"); folders[2] = d
g = Folder(3, 2, "dji.go.v5"); folders[3] = g
f = Folder(4, 3, "files"); folders[4] = f
w = Folder(5, 4, "waypoint"); folders[5] = w
mp = Folder(6, 5, "map_preview"); folders[6] = mp
for guid in GUIDS:
    sid = nxt()
    folders[sid] = Folder(sid, 5, guid)
    files.append(File(nxt(), sid, f"{guid}.kmz"))
    files.append(File(nxt(), 6, f"{guid}.jpg"))
# unrelated bulk so a "full scan" has something to chew on
for i in range(300):
    files.append(File(nxt(), 3, f"junk{i}.bin"))

class FakeInner:
    LIBMTP_GetPartialObject = None
    LIBMTP_Get_Filemetadata = None
class FakeMTP:
    def __init__(self):
        self.mtp = FakeInner(); self.device = object()
    def detect_devices(self): bump("detect_devices"); time.sleep(LAT["usb"]); return [1]
    def connect(self): bump("connect"); time.sleep(LAT["connect"])
    def disconnect(self): bump("disconnect")
    def get_serialnumber(self): bump("usb_roundtrip"); time.sleep(LAT["usb"]); return "SN"
    def get_folder_list(self): bump("get_folder_list"); time.sleep(0.05); return dict(folders)
    def get_filelisting(self): bump("get_filelisting_FULL_SCAN"); time.sleep(LAT["filelisting"]); return list(files)
    def delete_object(self, i): pass
    def find_filetype(self, p): return 0
fake = types.ModuleType("pymtp")
fake.MTP = FakeMTP
fake.LIBMTP_File = File
sys.modules["pymtp"] = fake

from backends.mtp import pi_mtp_backend as B  # noqa: E402

def fake_pull(self, item_id, dest):
    bump("object_pull"); time.sleep(LAT["pull"])
    try:
        from PIL import Image
        Image.new("RGB", (8, 8), (10, 20, 30)).save(dest, "JPEG")
    except ImportError:
        open(dest, "wb").write(b"\xff\xd8\xff\xe0" + b"0" * 64 + b"\xff\xd9")
    return True
B.PiMTPBackend._pull_to_path_fast = fake_pull

# The bench targets the Pi backend even when run on a Mac.
from backends import backend_factory  # noqa: E402
backend_factory.MacMTPBackend = B.PiMTPBackend
from viewmodel.mission_viewmodel import MissionViewModel  # noqa: E402
vm = MissionViewModel()

def scenario(name, fn):
    COUNTS.clear()
    t = time.monotonic()
    fn()
    dt = time.monotonic() - t
    print(f"  {name:<46} {dt:6.2f}s  " + ", ".join(f"{k}={v}" for k, v in sorted(COUNTS.items())))

def thumbs():
    out = {}
    def one(gd):
        out[gd] = vm.get_preview_image_path(gd)
    ts = [threading.Thread(target=one, args=(gd,)) for gd in GUIDS]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert all(v[0] for v in out.values()), out

print(f"== {label}")
scenario("1 first refresh (list 8 missions)", lambda: vm.list_missions())
scenario("2 8 thumbnails requested together (cold cache)", thumbs)
scenario("3 refresh again + 8 thumbnails (warm cache)", lambda: (vm.list_missions(), thumbs()))

def copy_preflight():
    be = vm._rc_backend
    be._last_transfer_monotonic = time.monotonic() - 60   # idle for a minute
    be.invalidate_transfer_caches()                        # what sync_engine does before a copy
    ok, out = be._raw_read_file(
        ROOT + "|" + GUIDS[0], GUIDS[0] + ".kmz", os.path.join(work, "out.kmz")
    ) if False else (True, "")
    # exercise the real pre-copy sequence: session refresh + file lookup
    with be._session():
        be._refresh_session_before_transfer("write")
        be._find_file(ROOT + "|" + GUIDS[0], GUIDS[0] + ".kmz")
scenario("4 copy after 60s idle (preflight + file lookup)", copy_preflight)


def list_cached():
    try:
        vm.list_missions(use_cache=True)
    except TypeError:          # older code without the cached option
        vm.list_missions()
scenario("5 list after upload/association (cached if supported)", list_cached)
scenario("6 explicit Refresh button (always fresh)", lambda: vm.list_missions())


def status_probe_then_copy_lookup():
    # /api/status polls the probe every few seconds; it must not force a
    # rescan of the folder tree that the next copy then pays for.
    be = vm._rc_backend
    vm.list_missions(use_cache=True)
    assert be.is_connected(timeout_seconds=3)
    COUNTS.clear()
    with be._session():
        be._find_file(ROOT + "|" + GUIDS[0], GUIDS[0] + ".kmz")
    assert "get_folder_list" not in COUNTS and "get_filelisting_FULL_SCAN" not in COUNTS, COUNTS
scenario("7 status probe, then copy file lookup (no rescan)", status_probe_then_copy_lookup)
