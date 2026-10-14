"""
mission_viewmodel.py
---------------------
ViewModel for the Flask web relay. Adapted from the desktop app's
SyncViewModel (viewmodel/sync_viewmodel.py) -- same underlying
RCBackend/SyncEngine/CopyMapService, but:

- No pc_folder / PC-side file listing here. The browser (client-side JS)
  owns the "PC/Cloud" file list and folder tree; this ViewModel only ever
  receives an already-uploaded temp file path from Flask for a copy-to-RC2
  operation, or hands back a temp file path for Flask to stream on a
  copy-from-RC2 (download) operation.
- Returns plain dicts/tuples that flask_app/routes.py can serialize to
  JSON directly, rather than Tkinter-facing objects.

Contains no Flask/HTTP-specific code -- could equally back a CLI.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from typing import Any, Dict, List, Tuple

from backends.backend_factory import BackendFactory
from backends.rc_backend import _preview_cache_base
from config.config_manager import ConfigManager
from model.kmz_file import KMZFile
from model.rc2_mission import RC2Mission
from services.battery_monitor_service import BatteryMonitorService
from services.copy_map_service import CopyMapService
from services.mission_verification_service import MissionVerificationService
from services.sync_engine import SyncEngine


# Mission slot names on the RC-2 are GUIDs. Anything else is rejected before it
# is used to build a cache file name or an MTP path.
_GUID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_PREVIEW_EXTENSIONS = (".jpg", ".jpeg", ".png")


class MissionViewModel:
    MTP_SIZE_TOLERANCE_PERCENT = 10.0
    MTP_SIZE_TOLERANCE_BYTES = 4096

    def __init__(self) -> None:
        self._config = ConfigManager()
        self._rc_backend = BackendFactory.create_rc(self._config.rc2_folder, self._config)
        self._sync_engine = SyncEngine()
        self._copy_map_service = CopyMapService()
        self._verification_service = MissionVerificationService()
        self._battery_monitor = BatteryMonitorService()
        self._last_error: str | None = None
        # Mission GUIDs from the most recent listing, so a thumbnail request
        # does not have to re-list every mission on the RC-2 first.
        self._known_guids: set[str] = set()
        # Thumbnails for every list row are requested at once. Only one
        # request talks to the RC-2; the rest wait and then hit the disk cache.
        self._preview_fetch_lock = threading.Lock()
        # GUIDs that had no preview image on the last fetch pass; cleared on refresh.
        self._preview_misses: set[str] = set()

    # ------------------------------------------------------------------
    # RC-2 root / connection
    # ------------------------------------------------------------------

    def get_rc2_root(self) -> str:
        return self._config.rc2_folder

    def set_rc2_root(self, path: str) -> None:
        cleaned = (path or "").strip()
        self._config.rc2_folder = cleaned
        self._config.save()
        old_backend = self._rc_backend
        self._rc_backend = BackendFactory.create_rc(cleaned, self._config)
        self._known_guids = set()
        self._preview_misses = set()
        close_old = getattr(old_backend, "close", None)
        if callable(close_old):
            try:
                close_old()
            except Exception:
                pass

    def is_connected(self, timeout_seconds: int | None = None) -> bool:
        if not self._config.rc2_folder.strip():
            return False
        return self._rc_backend.is_connected(timeout_seconds=timeout_seconds)

    def get_connection_mode(self) -> str:
        return self._rc_backend.get_connection_mode()

    def get_status(self) -> Tuple[bool, str]:
        return self._rc_backend.get_status()

    # ------------------------------------------------------------------
    # Battery (LBO -- Low Battery Output from the PowerBoost 1000C)
    # ------------------------------------------------------------------

    def get_battery_status(self) -> Dict[str, Any]:
        """Returns a dict with:
            low_battery: True/False, or None if the monitor is unavailable
            battery_monitor_available: whether the GPIO read is working at all
        A None low_battery should be treated by callers as "unknown", not
        as "battery is fine" -- see BatteryMonitorService.is_low_battery."""
        return {
            "low_battery": self._battery_monitor.is_low_battery(),
            "battery_monitor_available": self._battery_monitor.is_available(),
        }

    # ------------------------------------------------------------------
    # Dummy slot config
    # ------------------------------------------------------------------

    def get_dummy_slot_guid(self) -> str:
        return self._config.dummy_slot_guid

    def set_dummy_slot_guid(self, guid: str) -> None:
        self._config.dummy_slot_guid = guid
        self._config.save()

    # ------------------------------------------------------------------
    # Mission listing
    # ------------------------------------------------------------------

    def list_missions(self, use_cache: bool = False) -> Tuple[List[Dict[str, Any]], str | None]:
        """Returns (missions_as_dicts, error). Each dict is JSON-serializable.

        By default the backend's listing cache is dropped first, so the result
        reflects the RC-2 right now (explicit Refresh, page load, reconnect).
        With use_cache=True the warm cache is reused: no device rescan. Use it
        after changes the app made itself (upload, restore dummy, dummy slot):
        uploads and deletes patch the cache in place. Associations are
        local-only and never need a device listing.
        """
        if not use_cache:
            invalidate = getattr(self._rc_backend, "invalidate_cache", None)
            if callable(invalidate):
                try:
                    invalidate()
                except Exception:
                    pass

        missions, err = self._rc_backend.list_missions(self._config.rc2_folder)
        if not err:
            self._known_guids = {m.guid for m in missions}
        if not use_cache:
            self._preview_misses = set()
        dummy_guid = self._config.dummy_slot_guid.strip().lower()

        result = [
            {
                "guid": m.guid,
                "kmz_name": m.kmz_name,
                "display_kmz_name": m.display_kmz_name,
                "last_modified": m.display_last_modified,
                "is_empty": m.is_empty,
                "is_dummy": bool(dummy_guid) and m.guid.strip().lower() == dummy_guid,
            }
            for m in missions
        ]
        return result, err

    def _find_mission(self, missions: List[RC2Mission], guid: str) -> RC2Mission | None:
        wanted = (guid or "").strip().lower()
        for m in missions:
            if m.guid.strip().lower() == wanted:
                return m
        return None

    def _resolve_target_mission(self, target_guid: str | None) -> Tuple[RC2Mission | None, str | None]:
        """Resolve the mission to copy into: the explicitly selected GUID,
        or the configured dummy slot if none was selected.

        Returns (mission, error)."""
        missions, err = self._rc_backend.list_missions(self._config.rc2_folder)
        if err:
            return None, err

        cleaned_target = (target_guid or "").strip()
        if cleaned_target:
            mission = self._find_mission(missions, cleaned_target)
            if mission is None:
                return None, f"Selected RC-2 mission not found: {cleaned_target}"
            return mission, None

        dummy_guid = self._config.dummy_slot_guid.strip()
        if not dummy_guid:
            return None, "No RC-2 mission selected and no dummy mission slot is configured."

        mission = self._find_mission(missions, dummy_guid)
        if mission is None:
            return None, f"Configured dummy mission slot not found on RC-2: {dummy_guid}"
        return mission, None

    # ------------------------------------------------------------------
    # Copy PC/Cloud KMZ -> RC-2 (requirement 6)
    # ------------------------------------------------------------------

    def upload_kmz(
        self,
        local_temp_path: str,
        original_filename: str,
        target_guid: str | None,
    ) -> Tuple[bool, str, Dict[str, Any] | None]:
        """
        Copy an already-uploaded local temp file onto the RC-2.

        - target_guid set    -> copy into that mission's GUID slot, renamed
                                 to that slot's existing kmz name or, for an
                                 empty slot, to "<target_guid>.kmz".
        - target_guid empty  -> copy into the configured dummy mission slot,
                                 renamed the same way.

        Returns (success, message, result_dict) where result_dict has
        keys: mission_guid, dest_filename.
        """
        mission, err = self._resolve_target_mission(target_guid)
        if mission is None:
            return False, err or "Could not resolve a target RC-2 mission.", None

        kmz_file = KMZFile(filename=original_filename, full_path=local_temp_path)

        ok, msg = self._sync_engine.execute_copy(
            rc_backend=self._rc_backend,
            mission=mission,
            kmz_file=kmz_file,
            verify_mtp_copy=self._verify_mtp_copy_via_pull,
            record_copy_mapping=self._record_copy_mapping,
            clear_preview_cache_for_guid=self._clear_preview_cache_for_guid,
        )
        if not ok:
            return False, msg, None

        dest_filename, _ = self._sync_engine.resolve_destination_filename(self._rc_backend, mission)
        return True, msg, {"mission_guid": mission.guid, "dest_filename": dest_filename}

    def get_preview_image_path(self, guid: str) -> Tuple[bool, str]:
        """
        Return a local cached path to the mission's preview/thumbnail image.

        Cheap path first: a preview already on the Pi's disk is returned with
        no RC-2 access at all. Otherwise one request at a time fetches every
        missing preview in a single MTP pass (get_preview_paths_bulk) so the
        other thumbnail requests that arrive together are served from disk.
        """
        guid = (guid or "").strip()
        if not _GUID_RE.match(guid):
            return False, "Invalid mission id."
        root = self._config.rc2_folder

        path = self._rc_backend.get_preview_path(root, guid, allow_live_fetch=False)
        if path:
            return True, path

        with self._preview_fetch_lock:
            path = self._rc_backend.get_preview_path(root, guid, allow_live_fetch=False)
            if path:
                return True, path
            if guid in self._preview_misses:
                return False, "No preview image available for this mission."

            guids = set(self._known_guids)
            if guid not in guids:
                missions, err = self._rc_backend.list_missions(root)
                if err:
                    return False, err
                guids = {m.guid for m in missions}
                self._known_guids = guids
                if guid not in guids:
                    return False, f"RC-2 mission not found: {guid}"

            bulk = getattr(self._rc_backend, "get_preview_paths_bulk", None)
            if callable(bulk):
                results = bulk(root, sorted(guids))
                for g, found in results.items():
                    if not found:
                        self._preview_misses.add(g)
                path = results.get(guid)
            else:
                path = self._rc_backend.get_preview_path(root, guid)
                if not path:
                    self._preview_misses.add(guid)

        if not path:
            return False, "No preview image available for this mission."
        return True, path

    # ------------------------------------------------------------------
    # Associate a PC/Cloud file with an RC-2 mission (no file transfer --
    # just records the link so the UI can show/reuse it later)
    # ------------------------------------------------------------------

    def associate_pc_file(
        self,
        guid: str,
        source_filename: str,
        source_relative_path: str | None,
    ) -> Tuple[bool, str]:
        missions, err = self._rc_backend.list_missions(self._config.rc2_folder)
        if err:
            return False, err
        mission = self._find_mission(missions, guid)
        if mission is None:
            return False, f"RC-2 mission not found: {guid}"

        dest_filename, _ = self._sync_engine.resolve_destination_filename(self._rc_backend, mission)
        full_path = (source_relative_path or "").strip() or source_filename

        self._copy_map_service.record_mapping(
            source_filename=source_filename,
            source_full_path=full_path,
            target_mission_guid=mission.guid,
            target_kmz_filename=dest_filename,
            target_folder_path=mission.full_folder_path,
            connection_mode=self._rc_backend.get_connection_mode(),
        )
        return True, f"Associated {source_filename} with mission {mission.guid}"

    def get_copy_map_summary(self) -> List[Dict[str, str]]:
        rows, _updated_at, _note = self._copy_map_service.get_summary()
        return rows

    def _verify_mtp_copy_via_pull(
        self,
        mission: RC2Mission,
        source_path: str,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        return self._verification_service.verify_mtp_copy_via_pull(
            rc_backend=self._rc_backend,
            mission=mission,
            source_path=source_path,
            dest_filename=dest_filename,
            size_tolerance_percent=self.MTP_SIZE_TOLERANCE_PERCENT,
            size_tolerance_bytes=self.MTP_SIZE_TOLERANCE_BYTES,
        )

    def _record_copy_mapping(self, kmz_file: KMZFile, mission: RC2Mission, dest_filename: str) -> None:
        self._copy_map_service.record_mapping(
            source_filename=kmz_file.filename,
            source_full_path=kmz_file.full_path,
            target_mission_guid=mission.guid,
            target_kmz_filename=dest_filename,
            target_folder_path=mission.full_folder_path,
            connection_mode=self._rc_backend.get_connection_mode(),
        )

    def _clear_preview_cache_for_guid(self, guid: str) -> None:
        """Drop only this mission's cached preview so the next view re-fetches it.

        (Previously this cleared the whole preview cache after every upload,
        forcing every thumbnail to be fetched from the RC-2 again.)
        """
        guid = (guid or "").strip()
        self._preview_misses.discard(guid)
        if not _GUID_RE.match(guid):
            return
        base = _preview_cache_base(self._config.rc2_folder, guid)
        for ext in _PREVIEW_EXTENSIONS:
            try:
                os.remove(f"{base}{ext}")
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Copy RC-2 mission KMZ -> browser (requirement 8)
    # ------------------------------------------------------------------

    def download_kmz(self, guid: str) -> Tuple[bool, str, str | None]:
        """
        Pull a mission's KMZ from the RC-2 to a local temp file.

        Returns (success, message_or_error, temp_file_path).
        Caller (Flask route) is responsible for streaming the temp file
        back to the browser and cleaning it up afterwards.
        """
        missions, err = self._rc_backend.list_missions(self._config.rc2_folder)
        if err:
            return False, err, None

        mission = self._find_mission(missions, guid)
        if mission is None:
            return False, f"RC-2 mission not found: {guid}", None

        if mission.is_empty:
            return False, f"Mission {guid} has no KMZ file to download.", None

        fd, temp_path = tempfile.mkstemp(prefix="pidjirc2kmzsync-dl-", suffix=".kmz")
        os.close(fd)

        ok, out = self._rc_backend.copy_file_from_device(
            mission.full_folder_path, mission.kmz_name, temp_path
        )
        if not ok:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False, out, None

        return True, f"Pulled {mission.kmz_name} from mission {guid}", temp_path

    # ------------------------------------------------------------------
    # Delete (nice-to-have, not in the original 8 requirements but cheap
    # to expose since RCBackend already supports it)
    # ------------------------------------------------------------------

    def delete_mission_kmz(self, guid: str) -> Tuple[bool, str]:
        missions, err = self._rc_backend.list_missions(self._config.rc2_folder)
        if err:
            return False, err
        mission = self._find_mission(missions, guid)
        if mission is None:
            return False, f"RC-2 mission not found: {guid}"
        if mission.is_empty:
            return False, "Mission slot is already empty."
        return self._rc_backend.delete_file(mission, mission.kmz_name)
