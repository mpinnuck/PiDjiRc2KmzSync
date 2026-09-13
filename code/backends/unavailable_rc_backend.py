from __future__ import annotations

from typing import List, Tuple

from backends.rc_backend import RCBackend
from model.rc2_mission import RC2Mission


class UnavailableRCBackend(RCBackend):
    """RC backend that reports a fixed unsupported-backend error."""

    def __init__(self, message: str) -> None:
        self._message = (message or "Requested RC backend is unavailable.").strip()

    def _raw_list_folder(self, path: str) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        return False, self._message

    def _raw_read_file(self, folder_path: str, filename: str, local_dest: str) -> Tuple[bool, str]:
        return False, self._message

    def _raw_write_file(self, dest_folder: str, local_source: str, dest_filename: str) -> Tuple[bool, str]:
        return False, self._message

    def _raw_delete_file(self, folder_path: str, filename: str) -> Tuple[bool, str]:
        return False, self._message

    def _raw_delete_folder(self, folder_path: str) -> Tuple[bool, str]:
        return False, self._message

    def _raw_create_folder(self, parent_path: str, name: str) -> Tuple[bool, str]:
        return False, self._message

    def _raw_probe(self, root: str) -> bool:
        return False

    def _raw_get_status(self) -> Tuple[bool, str]:
        return False, self._message

    def _raw_connection_mode(self) -> str:
        return "Unavailable"

    def is_connected(self, timeout_seconds: int | None = None) -> bool:
        return False

    def get_connection_mode(self) -> str:
        return "Unavailable"

    def probe_root(self, path: str) -> bool:
        return False

    def list_missions(self, root: str) -> Tuple[List[RC2Mission], str | None]:
        return [], self._message

    def list_slot_files(self, mission: RC2Mission) -> Tuple[bool, List[str] | str]:
        return False, self._message

    def list_folder_items(
        self, path: str
    ) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        return False, self._message

    def read_file_bytes(
        self, mission: RC2Mission, filename: str
    ) -> Tuple[bool, bytes | str]:
        return False, self._message

    def read_file_bytes_from_path(
        self, folder: str, filename: str
    ) -> Tuple[bool, bytes | str]:
        return False, self._message

    def delete_file(self, mission: RC2Mission, filename: str) -> Tuple[bool, str]:
        return False, self._message

    def copy_file_to_device(
        self,
        dest_folder: str,
        local_source_path: str,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        return False, self._message

    def write_text_file(
        self,
        dest_folder: str,
        filename: str,
        content: str,
    ) -> Tuple[bool, str]:
        return False, self._message

    def copy_file_from_device(
        self,
        src_folder: str,
        filename: str,
        local_dest_path: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, str]:
        return False, self._message

    def create_slot_folder(self, root: str, guid: str) -> Tuple[bool, str]:
        return False, self._message

    def delete_mission(self, mission: RC2Mission) -> Tuple[bool, str]:
        return False, self._message

    def get_preview_path(
        self,
        root: str,
        guid: str,
        copy_timeout_seconds: int | None = None,
        list_timeout_seconds: int | None = None,
        allow_live_fetch: bool = True,
    ) -> str | None:
        return None

    def clear_preview_cache(self, root: str) -> None:
        return None

    def get_status(self) -> Tuple[bool, str]:
        return False, self._message
