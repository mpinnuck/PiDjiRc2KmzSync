from __future__ import annotations

import os

from model.rc2_mission import RC2Mission


class MissionVerificationService:
    """Copy verification and post-transfer integrity checks."""

    @classmethod
    def verify_mtp_copy_via_pull(
        cls,
        *,
        rc_backend,
        mission: RC2Mission,
        source_path: str,
        dest_filename: str,
    ) -> tuple[bool, str]:
        """Verify copy by checking file exists and size matches (metadata only).
        
        This avoids re-reading the entire file from the device, which saves 10+ seconds
        per copy over slow MTP connections.
        """
        expected_size = os.path.getsize(source_path)
        
        ok, size_or_error = rc_backend.get_file_size_from_path(
            mission.full_folder_path, dest_filename
        )
        if not ok:
            return False, (
                "Unable to verify file size after copy.\n"
                f"Expected size (source): {expected_size} bytes\n"
                f"Size lookup error: {size_or_error}"
            )
        
        detected_size = int(size_or_error) if isinstance(size_or_error, int) else 0
        
        if detected_size == expected_size:
            return True, "ok"
        
        size_diff = abs(detected_size - expected_size)
        percent_diff = (
            (size_diff / float(expected_size) * 100.0)
            if expected_size > 0
            else (0.0 if detected_size == 0 else 100.0)
        )
        return False, (
            "Destination size mismatch after copy verification.\n"
            f"Expected size (source): {expected_size} bytes\n"
            f"Detected size (destination): {detected_size} bytes\n"
            f"Difference: {size_diff} bytes ({percent_diff:.1f}%)"
        )
