"""Serve the Pi app against a fake RC-2 for a browser test. Usage: e2e_server.py CODE_DIR PORT"""
import json, os, sys, tempfile, types
code_dir, port = sys.argv[1], int(sys.argv[2])
work = tempfile.mkdtemp(prefix="e2e-"); os.environ["TMPDIR"] = work; tempfile.tempdir = work
ROOT = "mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint"
cfg = os.path.join(work, "cfg.json"); json.dump({"rc2_folder": ROOT}, open(cfg, "w"))
os.environ["PIDJIRC2KMZSYNC_CONFIG_FILE"] = cfg
sys.path.insert(0, code_dir)
class Folder:
    def __init__(s, i, p, n): s.folder_id, s.parent_id, s.name, s.storage_id = i, p, n.encode(), 1
class File:
    def __init__(s, i, p, n, size=1000, **kw):
        s.item_id, s.parent_id, s.filename, s.filesize = i, p, (n if isinstance(n, bytes) else n.encode()), size
        s.modificationdate, s.storage_id, s.filetype = 1760000000, 1, 0
GUID = "00000001-1111-2222-3333-444444444441"
folders = {1: Folder(1,0,"Android"), 2: Folder(2,1,"data"), 3: Folder(3,2,"dji.go.v5"), 4: Folder(4,3,"files"),
           5: Folder(5,4,"waypoint"), 6: Folder(6,5,"map_preview"), 7: Folder(7,5,GUID)}
files = [File(10,7,GUID+".kmz"), File(11,6,GUID+".jpg")]
SCANS = [0]
class Inner: LIBMTP_GetPartialObject = None; LIBMTP_Get_Filemetadata = None
class FakeMTP:
    def __init__(s): s.mtp, s.device = Inner(), object()
    def detect_devices(s): return [1]
    def connect(s): pass
    def disconnect(s): pass
    def get_serialnumber(s): return "SN"
    def get_folder_list(s): return dict(folders)
    def get_filelisting(s): SCANS[0] += 1; return list(files)
    def delete_object(s, i): pass
    def find_filetype(s, p): return 0
fake = types.ModuleType("pymtp"); fake.MTP = FakeMTP; fake.LIBMTP_File = File; sys.modules["pymtp"] = fake
from backends.mtp import pi_mtp_backend as B
def fake_pull(self, item_id, dest):
    from PIL import Image; Image.new("RGB", (8, 8), (1, 2, 3)).save(dest, "JPEG"); return True
B.PiMTPBackend._pull_to_path_fast = fake_pull
from flask_app.app import create_app
app = create_app()
@app.route("/__scans")
def _scans(): return str(SCANS[0])
app.run(host="127.0.0.1", port=port, threaded=True)
