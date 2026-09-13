from __future__ import annotations

import os
import shutil
import tempfile
from typing import Callable

from model.kmz_file import KMZFile
from model.rc2_mission import RC2Mission


class SyncEngine:
    """Orchestrates high-level sync workflows between PC and RC mission slots."""

    @staticmethod
    def _prepare_fresh_transfer_state(rc_backend) -> None:
        """Best-effort hook to avoid stale backend caches around copy operations."""
        invalidate = getattr(rc_backend, "invalidate_transfer_caches", None)
        if callable(invalidate):
            try:
                invalidate()
            except Exception:
                # Copy operations should continue even if cache invalidation fails.
                pass

    @staticmethod
    def resolve_destination_filename(rc_backend, mission: RC2Mission) -> tuple[str, bool]:
        """
        Resolve destination filename using live slot contents when possible.

        Returns (dest_filename, slot_has_existing_kmz).
        """
        fallback = (mission.kmz_name or "").strip() or f"{mission.guid}.kmz"
        try:
            ok_list, listed = rc_backend.list_slot_files(mission)
        except Exception:
            return fallback, bool((mission.kmz_name or "").strip())

        if not ok_list:
            return fallback, bool((mission.kmz_name or "").strip())

        names = listed if isinstance(listed, list) else []
        kmz_candidates = sorted(
            [name for name in names if str(name).strip().lower().endswith(".kmz")]
        )
        if kmz_candidates:
            return kmz_candidates[0], True
        return f"{mission.guid}.kmz", False

    def execute_copy(
        self,
        *,
        rc_backend,
        mission: RC2Mission,
        kmz_file: KMZFile,
        verify_mtp_copy: Callable[[RC2Mission, str, str], tuple[bool, str]],
        record_copy_mapping: Callable[[KMZFile, RC2Mission, str], None],
        clear_preview_cache_for_guid: Callable[[str], None],
    ) -> tuple[bool, str]:
        self._prepare_fresh_transfer_state(rc_backend)
        target_mission = mission
        dest_filename, _ = self.resolve_destination_filename(rc_backend, target_mission)

        if not os.path.isfile(kmz_file.full_path):
            return False, f"Source file not found:\n{kmz_file.full_path}"

        ok, out = rc_backend.copy_file_to_device(
            target_mission.full_folder_path, kmz_file.full_path, dest_filename
        )
        if not ok:
            return False, f"Copy failed:\n{out}"

        if rc_backend.get_connection_mode() == "MTP":
            verified, verify_msg = verify_mtp_copy(target_mission, kmz_file.full_path, dest_filename)
            if not verified:
                return False, f"MTP copy verification failed:\n{verify_msg}"

        msg = (
            f"Copied '{kmz_file.filename}'\n"
            f"  -> mission  : {target_mission.guid}\n"
            f"  -> saved as : {dest_filename}"
        )
        record_copy_mapping(kmz_file, target_mission, dest_filename)
        clear_preview_cache_for_guid(target_mission.guid)
        return True, msg

    def execute_copy_from_mission(
        self,
        *,
        rc_backend,
        pc_root: str,
        mission: RC2Mission,
        target_kmz_file: KMZFile | None,
        record_copy_mapping: Callable[[KMZFile, RC2Mission, str], None],
    ) -> tuple[bool, str]:
        if not pc_root or not os.path.isdir(pc_root):
            return False, f"PC KMZ folder not found:\n{pc_root}"

        target_filename = target_kmz_file.filename if target_kmz_file is not None else f"{mission.guid}.kmz"
        dest_path = os.path.join(pc_root, target_filename)
        source_filename = f"{mission.guid}.kmz"

        fd, temp_path = tempfile.mkstemp(prefix="djirc2kmzsync-copyback-", suffix=".kmz")
        os.close(fd)
        try:
            self._prepare_fresh_transfer_state(rc_backend)
            ok_list, listed = rc_backend.list_slot_files(mission)
            if not ok_list:
                return False, f"Failed to list mission files:\n{listed}"

            names = listed if isinstance(listed, list) else []
            by_lower = {str(name).strip().lower(): str(name).strip() for name in names}
            expected_lower = source_filename.lower()
            if expected_lower not in by_lower:
                available_kmz = [str(name).strip() for name in names if str(name).strip().lower().endswith(".kmz")]
                if available_kmz:
                    available_note = ", ".join(sorted(available_kmz)[:6])
                    return False, (
                        "Copy from device failed:\n"
                        f"Expected mission KMZ not found: {source_filename}\n"
                        f"Available KMZ files: {available_note}"
                    )
                return False, (
                    "Copy from device failed:\n"
                    f"Expected mission KMZ not found: {source_filename}"
                )

            source_filename = by_lower[expected_lower]
            ok, out = rc_backend.copy_file_from_device(
                mission.full_folder_path, source_filename, temp_path
            )
            if not ok:
                return False, f"Copy from device failed:\n{out}"

            os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
            if os.path.exists(dest_path):
                os.remove(dest_path)
            shutil.copy2(temp_path, dest_path)
        except OSError as exc:
            return False, f"Copy-back failed:\n{exc}"
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

        record_copy_mapping(
            KMZFile(filename=target_filename, full_path=dest_path),
            mission,
            source_filename,
        )
        return True, (
            f"Copied mission '{source_filename}'\n"
            f"  -> target file: {target_filename}\n"
            f"  -> location   : {dest_path}"
        )

    @staticmethod
    def confirm_copy_message(mission: RC2Mission, kmz_file: KMZFile) -> str:
        dest_filename = mission.kmz_name if mission.kmz_name else f"{mission.guid}.kmz"
        return (
            f"Copy mission into slot:\n"
            f"  {mission.guid}\n\n"
            f"With source file:\n"
            f"  {kmz_file.filename}\n\n"
            f"Destination filename will be:\n"
            f"  {dest_filename}"
        )
