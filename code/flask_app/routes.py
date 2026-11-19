"""
routes.py

HTTP routes. Each route is a thin translation layer: parse the request,
call MissionViewModel, translate the result (or exception) into an HTTP
response. No RCBackend / MTP-specific logic should ever appear here.

Endpoints
---------
GET  /                              -> UI shell
GET  /api/ping                      -> liveness only (no MTP, no locks): drives the
                                        "updater connected" badge
GET  /api/status                    -> RC-2 connection status + battery low flag
GET  /api/config                    -> rc2_root, dummy_slot_guid
POST /api/config/rc2-root           -> { root: "mtp:DJI RC 2|..." }
POST /api/config/dummy-slot         -> { guid: "<existing RC-2 mission GUID>" }
GET  /api/missions                  -> list RC-2 mission slots (left pane)
GET  /api/missions/<guid>/thumbnail -> mission preview/thumbnail image
POST /api/missions/<guid>/associate -> link a PC/Cloud file to a mission,
                                        no file transfer (just records it,
                                        same store the desktop app's copy
                                        history uses)
GET  /api/copy-map                  -> mission<->PC file associations,
                                        for the UI to show/reuse them
POST /api/upload                    -> multipart KMZ + optional target_guid
                                        (requirement 6 + dummy-slot logic)
GET  /api/download/<guid>           -> stream a mission's KMZ back (requirement 8);
                                        the browser writes these bytes into the
                                        PC/Cloud file selected in the folder tree,
                                        overwriting it in place, rather than
                                        this route producing a fresh download
DELETE /api/missions/<guid>/kmz     -> clear a mission slot's KMZ (optional extra)
"""

import os
import tempfile
import threading
import zipfile
from functools import wraps

from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException, InternalServerError, RequestEntityTooLarge

from viewmodel.mission_viewmodel import MissionViewModel

ALLOWED_EXTENSIONS = {".kmz"}


def register_routes(app: Flask) -> None:
    viewmodel = MissionViewModel()
    device_operation_lock = threading.RLock()

    def serialized(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            with device_operation_lock:
                return func(*args, **kwargs)
        return wrapped

    @app.before_request
    def require_same_origin_header():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("X-Requested-With") != "PiDjiRc2KmzSync":
                return jsonify({"error": "Missing request protection header."}), 403

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_error):
        return jsonify({"error": "Request exceeds the 64 MiB limit."}), 413

    @app.errorhandler(InternalServerError)
    def internal_server_error(error):
        if error.original_exception is None:
            app.logger.error("Server error: %s", error)
        return jsonify({"error": "Internal server error. Check the updater logs."}), 500

    @app.errorhandler(HTTPException)
    def http_error(error):
        if error.code and error.code >= 500:
            app.logger.error("Server error: %s", error)
            response = error.get_response()
            response.set_data(app.json.dumps({"error": f"Server error ({error.code}). Check the updater logs."}))
            response.content_type = "application/json"
            return response
        return error

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    # ------------------------------------------------------------------
    # Status / config
    # ------------------------------------------------------------------

    # Liveness only. This must never touch MTP, the viewmodel or any lock:
    # the UI's "updater connected" badge is driven by it, and it has to keep
    # answering promptly even while a slow MTP operation is in progress.
    @app.route("/api/ping")
    def ping():
        return jsonify({"ok": True})

    # The RC-2 probe goes through the MTP session lock, so it can wait behind
    # a long transfer. Only one status poll probes at a time; any poll that
    # arrives meanwhile returns the last known RC-2 state (flagged "stale")
    # instead of tying up another server thread waiting on the same lock.
    status_probe_lock = threading.Lock()
    last_rc2_state = {"connected": False, "mode": viewmodel.get_connection_mode()}

    @app.route("/api/status")
    def status():
        battery = viewmodel.get_battery_status()
        stale = True
        if status_probe_lock.acquire(blocking=False):
            try:
                last_rc2_state["connected"] = viewmodel.is_connected(timeout_seconds=3)
                last_rc2_state["mode"] = viewmodel.get_connection_mode()
                stale = False
            finally:
                status_probe_lock.release()
        return jsonify({
            "connected": last_rc2_state["connected"],
            "connection_mode": last_rc2_state["mode"],
            "rc2_root": viewmodel.get_rc2_root(),
            "low_battery": battery["low_battery"],
            "battery_monitor_available": battery["battery_monitor_available"],
            "stale": stale,
        })

    @app.route("/api/config", methods=["GET"])
    def get_config():
        return jsonify({
            "rc2_root": viewmodel.get_rc2_root(),
            "dummy_slot_guid": viewmodel.get_dummy_slot_guid(),
        })

    @app.route("/api/config/rc2-root", methods=["POST"])
    @serialized
    def set_rc2_root():
        data = request.get_json(silent=True) or {}
        root = str(data.get("root") or "").strip()
        if not root:
            return jsonify({"error": "root is required, e.g. mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint"}), 400
        viewmodel.set_rc2_root(root)
        return jsonify({"status": "ok", "rc2_root": viewmodel.get_rc2_root()})

    @app.route("/api/config/dummy-slot", methods=["POST"])
    @serialized
    def set_dummy_slot():
        data = request.get_json(silent=True) or {}
        guid = str(data.get("guid") or "").strip()
        if not guid:
            return jsonify({"error": "guid is required"}), 400
        viewmodel.set_dummy_slot_guid(guid)
        return jsonify({"status": "ok", "dummy_slot_guid": viewmodel.get_dummy_slot_guid()})

    # ------------------------------------------------------------------
    # Mission listing (left pane)
    # ------------------------------------------------------------------

    @app.route("/api/missions", methods=["GET"])
    def list_missions():
        # ?cached=1 reuses the backend's listing cache instead of forcing an
        # RC-2 rescan. Without it the listing is always fresh, so an older
        # cached copy of app.js keeps working unchanged.
        use_cache = (request.args.get("cached") or "").strip().lower() in {"1", "true", "yes"}
        missions, err = viewmodel.list_missions(use_cache=use_cache)
        if err:
            return jsonify({"missions": missions, "error": err}), 200
        return jsonify({"missions": missions})

    # ------------------------------------------------------------------
    # Mission preview thumbnail
    # ------------------------------------------------------------------

    @app.route("/api/missions/<guid>/thumbnail", methods=["GET"])
    def mission_thumbnail(guid):
        ok, result = viewmodel.get_preview_image_path(guid)
        if not ok:
            return jsonify({"error": result}), 404
        return send_file(result)

    # ------------------------------------------------------------------
    # Associate a PC/Cloud file with an RC-2 mission (no transfer)
    # ------------------------------------------------------------------

    @app.route("/api/missions/<guid>/associate", methods=["POST"])
    @serialized
    def associate_mission(guid):
        data = request.get_json(silent=True) or {}
        source_filename = str(data.get("source_filename") or "").strip()
        source_relative_path = str(data.get("source_relative_path") or "").strip() or None
        if not source_filename:
            return jsonify({"error": "source_filename is required"}), 400

        ok, msg = viewmodel.associate_pc_file(guid, source_filename, source_relative_path)
        if not ok:
            return jsonify({"error": msg}), 400
        return jsonify({"status": "ok", "message": msg})

    @app.route("/api/copy-map", methods=["GET"])
    def copy_map():
        return jsonify({"rows": viewmodel.get_copy_map_summary()})

    # ------------------------------------------------------------------
    # Upload: PC/Cloud KMZ -> RC-2 (requirement 6, dummy-slot logic)
    # ------------------------------------------------------------------

    @app.route("/api/upload", methods=["POST"])
    @serialized
    def upload_mission():
        """
        Copy a KMZ picked in the browser onto the RC-2.

        Form fields:
            file        - the .kmz file (required)
            target_guid - the selected RC-2 mission's GUID (optional).
                          If omitted/empty, the configured dummy mission
                          slot is used instead.
        """
        if "file" not in request.files:
            return jsonify({"error": "No file part in request"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400

        _, ext = os.path.splitext(file.filename)
        if ext.lower() not in ALLOWED_EXTENSIONS:
            return jsonify({"error": "Only .kmz files are supported"}), 400

        target_guid = (request.form.get("target_guid") or "").strip()

        fd, tmp_path = tempfile.mkstemp(prefix="pidjirc2kmzsync-up-", suffix=".kmz")
        os.close(fd)
        try:
            file.save(tmp_path)
            try:
                with zipfile.ZipFile(tmp_path) as archive:
                    entries = archive.infolist()
                    if not entries or len(entries) > 2048:
                        return jsonify({"error": "KMZ must contain between 1 and 2048 entries."}), 400
                    if sum(entry.file_size for entry in entries) > 256 * 1024 * 1024:
                        return jsonify({"error": "KMZ uncompressed size exceeds 256 MiB."}), 400
                    bad_entry = archive.testzip()
                    if bad_entry:
                        return jsonify({"error": f"KMZ contains a corrupt entry: {bad_entry}"}), 400
            except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
                return jsonify({"error": f"Invalid or unsupported KMZ archive: {exc}"}), 400
            ok, msg, result = viewmodel.upload_kmz(tmp_path, file.filename, target_guid or None)
            if not ok:
                return jsonify({"error": msg}), 400
            return jsonify({"status": "ok", "message": msg, **(result or {})})
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    # ------------------------------------------------------------------
    # Download: RC-2 mission KMZ -> browser (requirement 8)
    # ------------------------------------------------------------------

    @app.route("/api/download/<guid>", methods=["GET"])
    @serialized
    def download_mission(guid):
        ok, msg, temp_path = viewmodel.download_kmz(guid)
        if not ok:
            return jsonify({"error": msg}), 400

        download_name = f"{guid}.kmz"

        def cleanup():
            try:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError as exc:
                app.logger.warning("Cannot remove download temporary file %s: %s", temp_path, exc)

        try:
            response = send_file(temp_path, as_attachment=True, download_name=download_name)
        except Exception:
            cleanup()
            raise
        response.direct_passthrough = False
        response.call_on_close(cleanup)
        return response

    # ------------------------------------------------------------------
    # Optional: clear a mission slot's KMZ
    # ------------------------------------------------------------------

    @app.route("/api/missions/<guid>/kmz", methods=["DELETE"])
    @serialized
    def delete_mission_kmz(guid):
        ok, msg = viewmodel.delete_mission_kmz(guid)
        if not ok:
            return jsonify({"error": msg}), 400
        return jsonify({"status": "ok", "message": msg})
