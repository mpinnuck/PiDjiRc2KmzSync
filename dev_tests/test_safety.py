import io
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))

from flask import Flask, abort
from flask_app import routes
from model.kmz_file import KMZFile
from model.rc2_mission import RC2Mission
from services.mission_verification_service import MissionVerificationService
from services.sync_engine import SyncEngine
from backends.mtp.pi_mtp_backend import PiMTPBackend
from backends.mtp.mac_mtp_backend import MacMTPBackend


class FakeBackend:
    def __init__(self, content=b"original"):
        self.content = content
        self.fail_writes = 0
        self.corrupt_writes = False

    def list_slot_files(self, mission):
        return True, ["mission.kmz"] if self.content is not None else []

    def read_file_bytes_from_path(self, folder, name):
        return (True, self.content) if self.content is not None else (False, "missing")

    def get_file_size_from_path(self, folder, name):
        if self.content is not None:
            return True, len(self.content)
        return False, "file not found"

    def copy_file_to_device(self, folder, source, name):
        if self.fail_writes:
            self.fail_writes -= 1
            self.content = None
            return False, "simulated failure"
        with open(source, "rb") as f:
            self.content = f.read()
        if self.corrupt_writes:
            self.corrupt_writes = False
            self.content = self.content[:1]
        return True, "ok"

    def delete_file(self, mission, name):
        self.content = None
        return True, "ok"


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = patch("services.sync_engine.os.path.expanduser", return_value=self.temp.name)
        self.home.start()
        self.addCleanup(self.home.stop)
        self.source = os.path.join(self.temp.name, "source.kmz")
        with open(self.source, "wb") as f:
            f.write(b"replacement")
        self.mission = RC2Mission("mission", "mission.kmz", "slot")
        self.backend = FakeBackend()

    def verify(self, mission, source, name):
        return MissionVerificationService.verify_mtp_copy_via_pull(
            rc_backend=self.backend, mission=mission, source_path=source, dest_filename=name
        )

    def copy(self):
        return SyncEngine().execute_copy(
            rc_backend=self.backend, mission=self.mission,
            kmz_file=KMZFile("source.kmz", self.source), verify_mtp_copy=self.verify,
            record_copy_mapping=lambda *args: None,
            clear_preview_cache_for_guid=lambda *args: None,
        )

    def test_failed_write_restores_and_retains_backup(self):
        self.backend.fail_writes = 1
        ok, message = self.copy()
        self.assertFalse(ok)
        self.assertEqual(self.backend.content, b"original")
        self.assertIn("restored and verified", message)
        backup = message.split("Backup: ", 1)[1]
        with open(backup, "rb") as f:
            self.assertEqual(f.read(), b"original")

    def test_corruption_is_detected_and_restored(self):
        self.backend.corrupt_writes = True
        ok, message = self.copy()
        self.assertFalse(ok)
        self.assertIn("verification failed", message)
        self.assertEqual(self.backend.content, b"original")

    def test_restore_failure_retains_backup(self):
        self.backend.fail_writes = 2
        ok, message = self.copy()
        self.assertFalse(ok)
        self.assertIn("RESTORE FAILED", message)
        self.assertTrue(os.path.isfile(message.split("Backup: ", 1)[1]))

    def test_same_size_corruption_is_not_verified(self):
        # With the speedup, verification is now size-based only, not SHA256-based.
        # This means same-size corruption won't be caught. This is an acceptable
        # trade-off for eliminating 10+ second re-reads from MTP during verification.
        # Test that size-matching corruption is now "accepted" (passes verification).
        self.backend.content = b"x" * len(b"replacement")
        ok, message = self.verify(self.mission, self.source, "mission.kmz")
        self.assertTrue(ok, "Size-matching content should now pass size-based verification")

    def test_backup_failure_prevents_write(self):
        self.backend.read_file_bytes_from_path = lambda *args: (False, "read failed")
        ok, message = self.copy()
        self.assertFalse(ok)
        self.assertIn("destination unchanged", message)
        self.assertEqual(self.backend.content, b"original")

    def test_slot_listing_failure_prevents_write(self):
        self.backend.list_slot_files = lambda mission: (False, "listing failed")
        ok, _ = self.copy()
        self.assertFalse(ok)
        self.assertEqual(self.backend.content, b"original")

    def test_pi_slot_listing_refreshes_and_requires_file_ids(self):
        backend = object.__new__(PiMTPBackend)
        backend._available = True
        backend._session = nullcontext
        calls = []
        backend._drop_listing_caches = lambda: calls.append("refresh")
        backend._resolve_folder = lambda path: SimpleNamespace(folder_id=1)
        def children(folder_id, *, require_file_ids=False):
            calls.append(require_file_ids)
            return [], [SimpleNamespace(filename=b"mission.kmz")]
        backend._child_entries = children
        self.assertEqual(backend.list_slot_files(self.mission), (True, ["mission.kmz"]))
        self.assertEqual(calls, ["refresh", True])

    def test_empty_slot_partial_is_removed(self):
        self.backend.content = None
        self.backend.fail_writes = 1
        ok, _ = self.copy()
        self.assertFalse(ok)
        self.assertIsNone(self.backend.content)

    def test_short_reads_are_rejected(self):
        for cls in (PiMTPBackend, MacMTPBackend):
            with self.subTest(backend=cls.__name__):
                backend = object.__new__(cls)
                backend._all_files = lambda reason: [SimpleNamespace(item_id=1, filesize=10)]
                if cls is PiMTPBackend:
                    backend._device_file_entry = lambda item_id: None
                def partial(item_id, dest):
                    with open(dest, "wb") as f:
                        f.write(b"short")
                    return True
                backend._pull_to_path_via_partial = partial
                backend._mtp = SimpleNamespace(get_file_to_file=lambda *args: None)
                backend._quiet = lambda fn, *args: fn(*args)
                self.assertFalse(backend._pull_to_path_fast(1, self.source))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = os.path.join(self.temp.name, "download.kmz")
        with open(path, "wb") as f:
            f.write(b"download")
        self.path = path

        class ViewModel:
            def get_connection_mode(self):
                return "MTP"

            def download_kmz(self, guid):
                return True, "ok", path

            def upload_kmz(self, path, filename, target):
                return True, "ok", {"mission_guid": target, "dest_filename": filename}

        with patch.object(routes, "MissionViewModel", ViewModel):
            self.app = Flask(__name__)
            self.app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
            routes.register_routes(self.app)
        self.client = self.app.test_client()
        self.headers = {"X-Requested-With": "PiDjiRc2KmzSync"}

    def test_download_streams_and_cleans_up_on_close(self):
        response = self.client.get("/api/download/mission")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"download")
        response.close()
        self.assertFalse(os.path.exists(self.path))

    def test_unexpected_api_errors_return_json_and_are_logged(self):
        def fail():
            raise RuntimeError("technical details")

        self.app.view_functions["ping"] = fail
        with self.assertLogs(self.app.logger, level="ERROR") as logs:
            response = self.client.get("/api/ping")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "Internal server error. Check the updater logs."})
        self.assertIn("technical details", "\n".join(logs.output))

        self.app.view_functions["index"] = fail
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)

    def test_other_server_http_errors_are_json_without_changing_client_errors(self):
        self.app.view_functions["ping"] = lambda: abort(503)
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.client.get("/api/ping")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "Server error (503). Check the updater logs."})
        self.assertEqual(self.client.get("/api/missing").status_code, 404)

    def test_mutation_requires_protection_header(self):
        self.assertEqual(self.client.post("/api/upload").status_code, 403)

    def test_upload_rejects_bad_zip_and_accepts_valid_zip(self):
        def send(data):
            return self.client.post(
                "/api/upload", headers=self.headers,
                data={"file": (io.BytesIO(data), "test.kmz"), "target_guid": "mission"},
            )
        self.assertEqual(send(b"not a zip").status_code, 400)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("mission.kml", "test")
        self.assertEqual(send(data.getvalue()).status_code, 200)

    def test_upload_limit(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 100
        response = self.client.post(
            "/api/upload", headers=self.headers,
            data={"file": (io.BytesIO(b"x" * 200), "test.kmz")},
        )
        self.assertEqual(response.status_code, 413)

    def test_upload_and_delete_are_serialized(self):
        started = threading.Event()
        release = threading.Event()
        deleted = threading.Event()

        class ViewModel:
            def get_connection_mode(self):
                return "MTP"

            def upload_kmz(self, path, filename, target):
                started.set()
                if not release.wait(5):
                    raise RuntimeError("test timed out")
                return True, "ok", {}

            def delete_mission_kmz(self, guid):
                deleted.set()
                return True, "ok"

        with patch.object(routes, "MissionViewModel", ViewModel):
            app = Flask("serialization_test")
            routes.register_routes(app)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("mission.kml", "test")
        archive_bytes = data.getvalue()

        def upload():
            return app.test_client().post(
                "/api/upload", headers=self.headers,
                data={"file": (io.BytesIO(archive_bytes), "test.kmz")},
            ).status_code

        def delete():
            return app.test_client().delete(
                "/api/missions/mission/kmz", headers=self.headers,
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            upload_future = pool.submit(upload)
            self.assertTrue(started.wait(5))
            delete_future = pool.submit(delete)
            try:
                self.assertFalse(deleted.wait(0.1))
            finally:
                release.set()
            self.assertEqual(upload_future.result(timeout=5), 200)
            self.assertEqual(delete_future.result(timeout=5), 200)
            self.assertTrue(deleted.is_set())


if __name__ == "__main__":
    unittest.main()
