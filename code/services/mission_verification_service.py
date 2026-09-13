from __future__ import annotations

import hashlib
import os

from model.rc2_mission import RC2Mission


class MissionVerificationService:
    """Copy verification and post-transfer integrity checks."""

    @staticmethod
    def _file_sha256(path: str) -> str:
        hasher = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(64 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def _bytes_sha256(payload: bytes) -> str:
        hasher = hashlib.sha256()
        hasher.update(payload)
        return hasher.hexdigest()

    @classmethod
    def verify_mtp_copy_via_pull(
        cls,
        *,
        rc_backend,
        mission: RC2Mission,
        source_path: str,
        dest_filename: str,
        size_tolerance_percent: float,
        size_tolerance_bytes: int,
    ) -> tuple[bool, str]:
        expected_size = os.path.getsize(source_path)

        # Prefer metadata-based verification first because full MTP readback can
        # fail transiently even after a successful write on unstable sessions.
        ok_size, size_or_error = rc_backend.get_file_size_from_path(
            mission.full_folder_path,
            dest_filename,
        )
        if ok_size:
            try:
                detected_size_meta = int(size_or_error)
            except (TypeError, ValueError):
                detected_size_meta = -1

            if detected_size_meta >= 0:
                size_diff = abs(detected_size_meta - expected_size)
                percent_diff = (
                    (size_diff / float(expected_size) * 100.0)
                    if expected_size > 0
                    else (0.0 if detected_size_meta == 0 else 100.0)
                )
                tolerance_bytes = max(
                    int(size_tolerance_bytes),
                    int(round(expected_size * (float(size_tolerance_percent) / 100.0))),
                )

                if size_diff <= tolerance_bytes:
                    return True, "ok"

        expected_hash = cls._file_sha256(source_path)
        ok_bytes, payload = rc_backend.read_file_bytes_from_path(
            mission.full_folder_path,
            dest_filename,
        )
        if not ok_bytes:
            return False, (
                "Unable to read destination file for verification.\n"
                f"Expected size (source): {expected_size} bytes\n"
                f"Metadata size check: {'ok' if ok_size else 'failed'} ({size_or_error})\n"
                f"Read error: {payload}"
            )

        destination_bytes = payload if isinstance(payload, bytes) else bytes(payload)
        detected_size = len(destination_bytes)
        detected_hash = cls._bytes_sha256(destination_bytes)
        size_diff = abs(detected_size - expected_size)
        percent_diff = (
            (size_diff / float(expected_size) * 100.0)
            if expected_size > 0
            else (0.0 if detected_size == 0 else 100.0)
        )
        if detected_size == expected_size and detected_hash == expected_hash:
            return True, "ok"

        return False, (
            "Destination content mismatch after pull verification.\n"
            f"Expected size (source): {expected_size} bytes\n"
            f"Detected size (destination): {detected_size} bytes\n"
            f"Difference: {size_diff} bytes ({percent_diff:.1f}%)\n"
            f"Expected SHA-256: {expected_hash}\n"
            f"Detected SHA-256: {detected_hash}"
        )
