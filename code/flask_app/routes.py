"""
routes.py

HTTP routes. Each route is a thin translation layer: parse the request,
call MissionViewModel, translate the result (or exception) into an HTTP
response. No RCBackend / MTP-specific logic should ever appear here.

Endpoints
---------
GET  /                              -> UI shell
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

from flask import Flask, jsonify, request, send_file, send_from_directory

from viewmodel.mission_viewmodel import MissionViewModel

ALLOWED_EXTENSIONS = {".kmz"}


def register_routes(app: Flask) -> None:
    viewmodel = MissionViewModel()

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    # ------------------------------------------------------------------
    # Status / config
    # ------------------------------------------------------------------

    @app.route("/api/status")
    def status():
        battery = viewmodel.get_battery_status()
        return jsonify({
            "connected": viewmodel.is_connected(timeout_seconds=3),
            "connection_mode": viewmodel.get_connection_mode(),
            "rc2_root": viewmodel.get_rc2_root(),
            "low_battery": battery["low_battery"],
            "battery_monitor_available": battery["battery_monitor_available"],
        })

    @app.route("/api/config", methods=["GET"])
    def get_config():
        return jsonify({
            "rc2_root": viewmodel.get_rc2_root(),
            "dummy_slot_guid": viewmodel.get_dummy_slot_guid(),
        })

    @app.route("/api/config/rc2-root", methods=["POST"])
    def set_rc2_root():
        data = request.get_json(silent=True) or {}
        root = str(data.get("root") or "").strip()
        if not root:
            return jsonify({"error": "root is required, e.g. mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint"}), 400
        viewmodel.set_rc2_root(root)
        return jsonify({"status": "ok", "rc2_root": viewmodel.get_rc2_root()})

    @app.route("/api/config/dummy-slot", methods=["POST"])
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
        missions, err = viewmodel.list_missions()
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
        file.save(tmp_path)

        try:
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
    def download_mission(guid):
        ok, msg, temp_path = viewmodel.download_kmz(guid)
        if not ok:
            return jsonify({"error": msg}), 400

        download_name = f"{guid}.kmz"

        @app.after_request
        def _cleanup(response):
            # Best-effort cleanup; Flask has already read the file into the
            # response by the time after_request runs for send_file.
            try:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            return response

        return send_file(temp_path, as_attachment=True, download_name=download_name)

    # ------------------------------------------------------------------
    # Optional: clear a mission slot's KMZ
    # ------------------------------------------------------------------

    @app.route("/api/missions/<guid>/kmz", methods=["DELETE"])
    def delete_mission_kmz(guid):
        ok, msg = viewmodel.delete_mission_kmz(guid)
        if not ok:
            return jsonify({"error": msg}), 400
        return jsonify({"status": "ok", "message": msg})
