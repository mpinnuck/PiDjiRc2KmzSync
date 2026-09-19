"""
rc_backend.py
-------------
Concrete base class for all RC-2 device backends.

Owns ALL orchestration logic -- mission listing, file I/O, preview cache
management, path helpers. Concrete subclasses implement nine wire-level
primitives (_raw_*) that perform the actual device communication.

Hierarchy (this project):
    RCBackend (concrete base -- all orchestration here)
    ├── MacMTPBackend      -- _raw_* via pymtp (libmtp)
    └── PiMTPBackend       -- _raw_* via pymtp (libmtp)

Thread safety:
    Subclasses are responsible for any internal locking their _raw_*
    implementations require (e.g. MTP COM serialisation, pymtp session lock).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Tuple

try:
    from PIL import Image
except ImportError:
    Image = None

from config.config_manager import ConfigManager
from model.rc2_mission import RC2Mission
from services.mtp_date_normalizer import normalize_mtp_modify_date

# Folders present in the waypoint root that are not mission slots.
_NON_MISSION_FOLDERS = frozenset({"capability", "map_preview"})

_GUID_FOLDER_RE = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)

# Standard preview image suffixes in preference order.
_PREVIEW_SUFFIXES = (".jpg", ".jpeg", ".png")


class RCBackend(ABC):
    """
    Concrete base class for RC-2 device backends.

    All public methods are implemented here using the nine abstract _raw_*
    primitives. Subclasses must only implement those primitives.
    """

    # Subclasses set this to the default MTP root path for their platform.
    DEFAULT_ROOT: str = ""

    def __init__(self, config: ConfigManager) -> None:
        self._config = config
        # Per-session cache for preview folder listings to avoid repeated
        # device queries during a single refresh cycle.
        self._preview_items_cache: Dict[str, List[Dict[str, Any]]] = {}

    # ==================================================================
    # Abstract primitives -- subclasses implement these nine methods only
    # ==================================================================

    @abstractmethod
    def _raw_list_folder(
        self, path: str
    ) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        """
        List the contents of a folder on the device.

        Returns (success, result) where result is either:
        - List[Tuple[str, bool, str]]: (name, is_folder, modified_display)
        - str: error message on failure

        modified_display is a formatted datetime string or "" if unavailable.
        """

    @abstractmethod
    def _raw_read_file(
        self, folder_path: str, filename: str, local_dest: str
    ) -> Tuple[bool, str]:
        """
        Pull a file from the device to a local path.

        Returns (success, message).
        local_dest is an existing temp file path that will be overwritten.
        """

    @abstractmethod
    def _raw_write_file(
        self, dest_folder: str, local_source: str, dest_filename: str
    ) -> Tuple[bool, str]:
        """
        Push a local file to a device folder, naming it dest_filename.

        dest_filename may differ from the source basename -- the rename
        is the caller's responsibility via staging if the primitive cannot
        do it natively.

        Returns (success, message).
        """

    @abstractmethod
    def _raw_delete_file(
        self, folder_path: str, filename: str
    ) -> Tuple[bool, str]:
        """
        Delete a single file from a device folder.

        Returns (True, "NOT_FOUND") if the file does not exist.
        Returns (True, message) on successful delete.
        Returns (False, message) on error.
        """

    @abstractmethod
    def _raw_delete_folder(
        self, folder_path: str
    ) -> Tuple[bool, str]:
        """
        Delete a folder and all its contents recursively on the device.

        Returns (success, message).
        """

    @abstractmethod
    def _raw_create_folder(
        self, parent_path: str, name: str
    ) -> Tuple[bool, str]:
        """
        Create a subfolder named `name` under `parent_path` on the device.

        Returns (success, full_path_on_device).
        """

    @abstractmethod
    def _raw_probe(self, root: str) -> bool:
        """
        Return True if root is a reachable device folder. Must not raise.
        """

    @abstractmethod
    def _raw_get_status(self) -> Tuple[bool, str]:
        """
        Return (ready, message) describing the current connection state.
        """

    @abstractmethod
    def _raw_connection_mode(self) -> str:
        """
        Return the connection mode label, e.g. "MTP".
        """

    # ==================================================================
    # Public interface -- all implemented here, no overrides needed
    # ==================================================================

    # ------------------------------------------------------------------
    # Connection & mode
    # ------------------------------------------------------------------

    def is_connected(self, timeout_seconds: int | None = None) -> bool:
        """Best-effort connectivity probe. Must not raise."""
        root = self._root()
        if not root:
            return False

        # Allow one quick retry to smooth transient MTP probe blips.
        max_attempts = 2
        if timeout_seconds is None:
            deadline = None
        else:
            deadline = time.monotonic() + max(float(timeout_seconds), 0.0)

        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            try:
                if self._raw_probe(root):
                    return True
            except Exception:
                pass

            if attempt >= max_attempts:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.2)

        return False

    def get_connection_mode(self) -> str:
        return self._raw_connection_mode()

    def probe_root(self, path: str) -> bool:
        try:
            return self._raw_probe(path)
        except Exception:
            return False

    def get_status(self) -> Tuple[bool, str]:
        try:
            return self._raw_get_status()
        except Exception as exc:
            return False, str(exc)

    # ------------------------------------------------------------------
    # Mission listing
    # ------------------------------------------------------------------

    def list_missions(self, root: str) -> Tuple[List[RC2Mission], str | None]:
        """
        Enumerate all mission slots under the RC-2 waypoint root.

        Tries _raw_list_missions_bulk() first for performance (one device
        round-trip for all slots). Falls back to per-slot enumeration if
        bulk is not supported or fails.
        """
        # Offer subclasses a fast bulk path (e.g. single PS process on Windows).
        try:
            ok_bulk, bulk_result = self._raw_list_missions_bulk(root)
        except NotImplementedError:
            ok_bulk = False
            bulk_result = ""

        if ok_bulk and isinstance(bulk_result, list):
            missions = []
            for row in bulk_result:
                slot_name = str(row.get("Name") or "").strip()
                if not slot_name:
                    continue
                if not _is_guid_folder_name(slot_name):
                    continue
                kmz_name = str(row.get("KMZName") or "").strip()
                last_modified = normalize_mtp_modify_date(
                    str(row.get("ModifyDateDetail") or "")
                    or str(row.get("ModifyDate") or "")
                )
                missions.append(RC2Mission(
                    guid=slot_name,
                    kmz_name=kmz_name,
                    full_folder_path=_mtp_join(root, slot_name),
                    last_modified=last_modified,
                ))
            return _dedupe_missions_by_guid(missions), None

        # Per-slot fallback enumeration.
        ok, result = self._raw_list_folder(root)
        if not ok:
            bulk_error = (
                bulk_result if isinstance(bulk_result, str) and bulk_result.strip()
                else ""
            )
            suffix = f" | Bulk query failed: {bulk_error}" if bulk_error else ""
            return [], f"[RCBackend] Error listing missions: {result}{suffix}"

        items = result if isinstance(result, list) else []
        slot_entries = sorted(
            [
                (name, modified) for name, is_folder, modified in items
                if is_folder
                and name.strip().lower() not in _NON_MISSION_FOLDERS
                and _is_guid_folder_name(name)
            ],
            key=lambda t: t[0],
        )

        missions = []
        for slot_name, _slot_modified in slot_entries:
            slot_path = _mtp_join(root, slot_name)
            ok_slot, slot_result = self._raw_list_folder(slot_path)
            if not ok_slot:
                continue

            slot_items = slot_result if isinstance(slot_result, list) else []
            kmz_files = sorted(
                [
                    (name, modified) for name, is_folder, modified in slot_items
                    if not is_folder and name.strip().lower().endswith(".kmz")
                ],
                key=lambda t: t[0],
            )
            kmz_name = kmz_files[0][0] if kmz_files else ""
            last_modified = normalize_mtp_modify_date(kmz_files[0][1]) if kmz_files else ""
            missions.append(RC2Mission(
                guid=slot_name,
                kmz_name=kmz_name,
                full_folder_path=slot_path,
                last_modified=last_modified,
            ))

        return _dedupe_missions_by_guid(missions), None

    def _raw_list_missions_bulk(
        self, root: str
    ) -> Tuple[bool, List[Dict[str, Any]] | str]:
        """
        Optional override for backends that can enumerate all slots and their
        KMZ metadata in a single device round-trip (e.g. one PowerShell call).

        Returns (True, list_of_dicts) where each dict has keys:
            Name, KMZName, ModifyDate, ModifyDateDetail

        Raise NotImplementedError to fall back to per-slot enumeration.
        Default implementation raises NotImplementedError.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Slot file operations
    # ------------------------------------------------------------------

    def list_slot_files(self, mission: RC2Mission) -> Tuple[bool, List[str] | str]:
        ok, result = self._raw_list_folder(mission.full_folder_path)
        if not ok:
            return False, str(result)
        items = result if isinstance(result, list) else []
        names = sorted(
            name for name, _is_folder, _modified in items if name.strip()
        )
        return True, names

    def list_folder_items(
        self, path: str
    ) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        ok, result = self._raw_list_folder(path)
        if not ok:
            return False, str(result)
        items = result if isinstance(result, list) else []
        output = [
            (name, is_folder, normalize_mtp_modify_date(modified))
            for name, is_folder, modified in items
            if name.strip()
        ]
        return True, output

    def read_file_bytes(
        self, mission: RC2Mission, filename: str
    ) -> Tuple[bool, bytes | str]:
        return self._pull_to_bytes(mission.full_folder_path, filename)

    def read_file_bytes_from_path(
        self, folder: str, filename: str
    ) -> Tuple[bool, bytes | str]:
        return self._pull_to_bytes(folder, filename)

    def get_file_size_from_path(
        self, folder: str, filename: str
    ) -> Tuple[bool, int | str]:
        """
        Return file size in bytes for a file in a device folder.

        Default implementation does not provide size metadata and returns
        an explanatory message. Backends with native metadata access should
        override this method.
        """
        return False, "File size metadata is not available for this backend."

    def delete_file(
        self, mission: RC2Mission, filename: str
    ) -> Tuple[bool, str]:
        return self._raw_delete_file(mission.full_folder_path, filename)

    # ------------------------------------------------------------------
    # File transfer -- PC to RC-2
    # ------------------------------------------------------------------

    def copy_file_to_device(
        self,
        dest_folder: str,
        local_source_path: str,
        dest_filename: str,
    ) -> Tuple[bool, str]:
        if not os.path.isfile(local_source_path):
            return False, f"Source file not found:\n{local_source_path}"
        ok, out = self._raw_write_file(dest_folder, local_source_path, dest_filename)
        if not ok:
            return False, f"Copy to device failed:\n{out}"
        return True, f"Copied to {_mtp_join(dest_folder, dest_filename)}"

    def write_text_file(
        self,
        dest_folder: str,
        filename: str,
        content: str,
    ) -> Tuple[bool, str]:
        fd, temp_path = tempfile.mkstemp(
            prefix="djirc2kmzsync-txt-", suffix=".txt"
        )
        os.close(fd)
        try:
            with open(temp_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            ok, out = self._raw_write_file(dest_folder, temp_path, filename)
            if not ok:
                return False, f"Write to device failed:\n{out}"
            return True, f"Wrote {filename} to {_mtp_join(dest_folder, filename)}"
        except OSError as exc:
            return False, f"File operation failed:\n{exc}"
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # File transfer -- RC-2 to PC
    # ------------------------------------------------------------------

    def copy_file_from_device(
        self,
        src_folder: str,
        filename: str,
        local_dest_path: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, str]:
        # Always pull to a temp file first so local_dest_path is never partial.
        fd, temp_path = tempfile.mkstemp(
            prefix="djirc2kmzsync-pull-", suffix=".tmp"
        )
        os.close(fd)
        try:
            ok, out = self._raw_read_file(src_folder, filename, temp_path)
            if not ok:
                return False, out
            dest_dir = os.path.dirname(local_dest_path)
            if dest_dir:
                os.makedirs(dest_dir, exist_ok=True)
            if os.path.exists(local_dest_path):
                os.remove(local_dest_path)
            os.replace(temp_path, local_dest_path)
            # Temp file has been atomically promoted to destination.
            temp_path = ""
            return True, local_dest_path
        except OSError as exc:
            return False, str(exc)
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Mission management
    # ------------------------------------------------------------------

    def create_slot_folder(self, root: str, guid: str) -> Tuple[bool, str]:
        del root, guid
        return False, "Creating RC-2 mission folders is disabled. Copy KMZ files only into existing GUID slots."

    def delete_mission(self, mission: RC2Mission) -> Tuple[bool, str]:
        ok, out = self._raw_delete_folder(mission.full_folder_path)
        if not ok:
            return False, f"Delete failed:\n{out}"
        return True, f"Deleted mission {mission.guid}"

    # ------------------------------------------------------------------
    # Preview images
    # ------------------------------------------------------------------

    def get_preview_path(
        self,
        root: str,
        guid: str,
        copy_timeout_seconds: int | None = None,
        list_timeout_seconds: int | None = None,
        allow_live_fetch: bool = True,
    ) -> str | None:
        cache_base = _preview_cache_base(root, guid)
        cached = _find_cached_preview(cache_base, guid)
        if cached:
            return cached

        if not allow_live_fetch:
            return None

        preview_folder = _mtp_join(root, "map_preview")
        ok_items, item_result = self._list_folder_items_cached(
            preview_folder, timeout_seconds=list_timeout_seconds
        )
        if not ok_items:
            return None

        items = item_result if isinstance(item_result, list) else []
        preview_name = _choose_preview_name(guid, items)
        source_folder = preview_folder

        # Try nested <guid> subfolder if not found flat.
        if not preview_name:
            folder_names = [
                name for name, is_folder, _modified in items if is_folder
            ]
            nested = _choose_preview_folder(guid, folder_names)
            if nested:
                source_folder = _mtp_join(preview_folder, nested)
                ok_n, nested_result = self._list_folder_items_cached(
                    source_folder, timeout_seconds=list_timeout_seconds
                )
                if ok_n:
                    preview_name = _choose_preview_name(
                        guid, nested_result if isinstance(nested_result, list) else []
                    )

        # Direct probe fallback for unreliable IsFolder metadata.
        if not preview_name:
            source_folder = _mtp_join(preview_folder, guid)
            ok_p, probe_result = self._list_folder_items_cached(
                source_folder, timeout_seconds=list_timeout_seconds
            )
            if ok_p:
                preview_name = _choose_preview_name(
                    guid, probe_result if isinstance(probe_result, list) else []
                )

        if not preview_name:
            return None

        return self._fetch_and_cache_preview(
            cache_base=cache_base,
            source_folder=source_folder,
            preview_name=preview_name,
            copy_timeout_seconds=copy_timeout_seconds,
        )

    def clear_preview_cache(self, root: str) -> None:
        _clear_preview_cache(root)
        self._preview_items_cache.clear()

    def invalidate_cache(self) -> None:
        """Clear the preview items listing cache (call when rc2_folder changes)."""
        self._preview_items_cache.clear()

    # ------------------------------------------------------------------
    # Internal helpers shared by all subclasses
    # ------------------------------------------------------------------

    def _root(self) -> str:
        return (self._config.rc2_folder or "").strip()

    def _pull_to_bytes(
        self, folder_path: str, filename: str
    ) -> Tuple[bool, bytes | str]:
        fd, temp_path = tempfile.mkstemp(
            prefix="djirc2kmzsync-read-", suffix=".tmp"
        )
        os.close(fd)
        try:
            ok, out = self._raw_read_file(folder_path, filename, temp_path)
            if not ok:
                return False, out
            with open(temp_path, "rb") as fh:
                return True, fh.read()
        except OSError as exc:
            return False, str(exc)
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def join_folder_path(self, folder_path: str, child_name: str) -> str:
        """Join a child name to a backend path using transport-aware rules."""
        return _join_backend_path(folder_path, child_name)

    def inspect_metadata_history_candidates(
        self,
        mission: RC2Mission,
        kmz_name: str,
        *,
        time_budget_seconds_mtp: float = 8.0,
        max_depth_mtp: int = 1,
        max_folders_mtp: int = 24,
        max_scan_folders_mtp: int = 10,
        max_file_reads_mtp: int = 16,
        folder_hint_tokens: Tuple[str, ...] = (
            "history", "record", "mission", "meta", "index", "db", "database", "sqlite",
        ),
        folder_skip_tokens: Tuple[str, ...] = (
            "mediacache", "media_cache", "cachevideo", "video", "thumb", "thumbnail", "image",
        ),
    ) -> List[str]:
        lines: List[str] = []
        root = self._root()
        candidates: List[str] = []
        root_scheme = _path_scheme(root)
        use_mtp_limits = root_scheme == "mtp"
        deadline = (
            time.monotonic() + time_budget_seconds_mtp
            if use_mtp_limits else None
        )

        def _add_candidate(path: str | None) -> None:
            cleaned = (path or "").strip()
            if cleaned:
                candidates.append(cleaned)

        if root_scheme == "mtp":
            _add_candidate(root)
            parent_files = _mtp_parent_path(root, levels=1)
            parent_app = _mtp_parent_path(root, levels=2)
            _add_candidate(parent_files)
            if parent_files:
                ok, items = self.list_folder_items(parent_files)
                if ok and isinstance(items, list):
                    for name, is_folder, _ in items:
                        lowered = name.lower()
                        if is_folder and any(token in lowered for token in ("history", "record", "mission", "meta", "index", "cache")):
                            _add_candidate(self.join_folder_path(parent_files, name))
            _add_candidate(parent_app)
        else:
            _add_candidate(root)

        expand_depth = max_depth_mtp if use_mtp_limits else 2
        expand_max_folders = max_folders_mtp if use_mtp_limits else 80
        scan_folder_cap = max_scan_folders_mtp if use_mtp_limits else 20
        read_cap = max_file_reads_mtp if use_mtp_limits else 1000

        unique_candidates = _expand_folder_candidates(
            candidates,
            list_folder_items=self.list_folder_items,
            join_folder_path=self.join_folder_path,
            deadline=deadline,
            max_depth=expand_depth,
            max_folders=expand_max_folders,
            max_listings=scan_folder_cap,
            include_tokens=folder_hint_tokens if use_mtp_limits else None,
            skip_tokens=folder_skip_tokens if use_mtp_limits else None,
        )

        lines.append("Metadata/history probe:")
        lines.append(f"Candidate folders: {len(unique_candidates)} (including nested folders)")

        targets = [mission.guid.lower(), kmz_name.lower()]
        checked_files = 0
        hits: List[str] = []
        meta_exts = (".json", ".txt", ".xml", ".db", ".sqlite")

        timed_out = False
        for folder in unique_candidates[:scan_folder_cap]:
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                break
            ok, listed = self.list_folder_items(folder)
            if not ok:
                continue
            items = listed if isinstance(listed, list) else []
            files = [(name, modified) for name, is_folder, modified in items if not is_folder]
            interesting = [
                (name, modified) for name, modified in files
                if name.lower().endswith(meta_exts)
                or any(token in name.lower() for token in ("history", "mission", "meta", "index", "title", "record"))
            ]

            if interesting:
                preview = [
                    f"{name} [{modified or 'Unknown'}]"
                    for name, modified in interesting[:8]
                ]
                lines.append(f"- {folder}: {', '.join(preview)}{' ...' if len(interesting) > 8 else ''}")

            for filename, modified in interesting[:10]:
                if checked_files >= read_cap:
                    timed_out = True
                    break
                if deadline is not None and time.monotonic() > deadline:
                    timed_out = True
                    break
                checked_files += 1
                ok_bytes, payload = self.read_file_bytes_from_path(folder, filename)
                if not ok_bytes or not isinstance(payload, bytes):
                    continue

                if filename.lower().endswith((".db", ".sqlite")):
                    continue

                text = ""
                for encoding in ("utf-8", "utf-16", "latin-1"):
                    try:
                        text = payload.decode(encoding, errors="strict")
                        break
                    except UnicodeDecodeError:
                        continue

                if not text:
                    continue

                lowered = text.lower()
                if any(target in lowered for target in targets if target):
                    hits.append(f"{folder} | {filename} [{modified or 'Unknown'}]")
            if timed_out:
                break

        lines.append(f"Metadata files checked: {checked_files}")
        if timed_out:
            lines.append("Metadata scan was time-limited for responsiveness; results may be partial.")
        if hits:
            dedup_hits = []
            seen_hits = set()
            for hit in hits:
                if hit in seen_hits:
                    continue
                seen_hits.add(hit)
                dedup_hits.append(hit)
            lines.append("GUID/KMZ references found in:")
            for hit in dedup_hits[:12]:
                lines.append(f"  * {hit}")
        else:
            lines.append("No GUID/KMZ references found in inspected text metadata files.")
            lines.append("Likely source is a binary DJI app database/index not directly readable via this path.")

        return lines

    def inspect_binary_metadata_candidates(
        self,
        _mission: RC2Mission,
        _kmz_name: str,
        *,
        time_budget_seconds_mtp: float = 8.0,
        max_depth_mtp: int = 1,
        max_folders_mtp: int = 24,
        max_scan_folders_mtp: int = 10,
        folder_hint_tokens: Tuple[str, ...] = (
            "history", "record", "mission", "meta", "index", "db", "database", "sqlite",
        ),
        folder_skip_tokens: Tuple[str, ...] = (
            "mediacache", "media_cache", "cachevideo", "video", "thumb", "thumbnail", "image",
        ),
    ) -> List[str]:
        lines: List[str] = []
        root = self._root()
        candidates: List[str] = []
        root_scheme = _path_scheme(root)
        use_mtp_limits = root_scheme == "mtp"
        deadline = (
            time.monotonic() + time_budget_seconds_mtp
            if use_mtp_limits else None
        )

        def _add_candidate(path: str | None) -> None:
            cleaned = (path or "").strip()
            if cleaned:
                candidates.append(cleaned)

        if root_scheme == "mtp":
            _add_candidate(root)
            _add_candidate(_mtp_parent_path(root, levels=1))
            _add_candidate(_mtp_parent_path(root, levels=2))
        else:
            _add_candidate(root)

        expand_depth = max_depth_mtp if use_mtp_limits else 2
        expand_max_folders = max_folders_mtp if use_mtp_limits else 80
        scan_folder_cap = max_scan_folders_mtp if use_mtp_limits else 20

        unique_candidates = _expand_folder_candidates(
            candidates,
            list_folder_items=self.list_folder_items,
            join_folder_path=self.join_folder_path,
            deadline=deadline,
            max_depth=expand_depth,
            max_folders=expand_max_folders,
            max_listings=scan_folder_cap,
            include_tokens=folder_hint_tokens if use_mtp_limits else None,
            skip_tokens=folder_skip_tokens if use_mtp_limits else None,
        )

        lines.append("Binary metadata/index search:")
        lines.append(f"Candidate folders: {len(unique_candidates)} (including nested folders)")

        meta_exts = (".db", ".sqlite", ".sqlite3", ".db3", ".dat", ".idx", ".bin")
        skip_exts = (".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".aac", ".mp3", ".wav")
        name_tokens = ("database", "index", "metadata", "mission", "history", "record", "sqlite", "db")
        hits: List[str] = []

        timed_out = False
        for folder in unique_candidates[:scan_folder_cap]:
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                break
            ok, listed = self.list_folder_items(folder)
            if not ok:
                continue

            items = listed if isinstance(listed, list) else []
            matches = [
                (name, modified) for name, is_folder, modified in items
                if not is_folder and (
                    (not name.lower().endswith(skip_exts))
                    and (
                        name.lower().endswith(meta_exts)
                        or any(token in name.lower() for token in name_tokens)
                    )
                )
            ]

            if matches:
                preview = [
                    f"{name} [{modified or 'Unknown'}]"
                    for name, modified in matches[:8]
                ]
                lines.append(f"- {folder}: {', '.join(preview)}{' ...' if len(matches) > 8 else ''}")

            for name, modified in matches[:10]:
                hits.append(f"{folder} | {name} [{modified or 'Unknown'}]")

        if timed_out:
            lines.append("Binary index scan was time-limited for responsiveness; results may be partial.")
        if hits:
            lines.append(f"Best candidate: {hits[0]}")
            lines.append("Potential binary metadata/index files:")
            for hit in hits[:12]:
                lines.append(f"  * {hit}")
        else:
            lines.append("No obvious binary database/index filenames found in candidate folders.")

        return lines

    def _list_folder_items_cached(
        self,
        path: str,
        timeout_seconds: int | None = None,
    ) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        """
        Cached wrapper around _raw_list_folder for preview resolution.
        Cache is per-session and cleared by clear_preview_cache / invalidate_cache.
        """
        cached = self._preview_items_cache.get(path)
        if cached is not None:
            return True, cached

        ok, result = self._raw_list_folder(path)
        if ok:
            items = result if isinstance(result, list) else []
            self._preview_items_cache[path] = items
        return ok, result

    def _fetch_and_cache_preview(
        self,
        cache_base: str,
        source_folder: str,
        preview_name: str,
        copy_timeout_seconds: int | None = None,
    ) -> str | None:
        ext = os.path.splitext(preview_name)[1].lower() or ".jpg"
        cache_path = f"{cache_base}{ext}"
        temp_path = f"{cache_path}.{uuid.uuid4().hex}.tmp"
        try:
            ok, _ = self._raw_read_file(source_folder, preview_name, temp_path)
            if ok and _is_usable_preview(temp_path):
                _promote_preview(temp_path, cache_path)
                return cache_path
            return None
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass


# ===========================================================================
# Module-level pure helpers shared by all backends
# ===========================================================================

def _mtp_join(path: str, name: str) -> str:
    prefix = (path or "").strip()
    sep = "" if prefix.endswith("|") else "|"
    return f"{prefix}{sep}{name}"


def _join_backend_path(path: str, name: str) -> str:
    raw = (path or "").strip()
    if raw.lower().startswith("mtp:"):
        return _mtp_join(raw, name)
    return os.path.join(raw, name)


def _mtp_segments(path: str) -> List[str]:
    raw = (path or "").strip()
    if raw.lower().startswith("mtp:"):
        raw = raw[4:].strip()
    return [s.strip() for s in raw.split("|") if s.strip()]


def _mtp_parent_path(mtp_path: str, levels: int = 1) -> str | None:
    if _path_scheme(mtp_path) != "mtp":
        return None
    segments = _mtp_segments(mtp_path)
    if len(segments) <= levels:
        return None
    parent_segments = segments[:-levels]
    return "mtp:" + "|".join(parent_segments)


def _path_scheme(path: str) -> str:
    cleaned = (path or "").strip()
    if not cleaned:
        return ""
    sep = cleaned.find(":")
    if sep <= 0:
        return ""
    return cleaned[:sep].strip().lower()


def _dedupe_missions_by_guid(missions: List[RC2Mission]) -> List[RC2Mission]:
    deduped: Dict[str, RC2Mission] = {}
    ordered_keys: List[str] = []

    for mission in missions:
        raw_guid = (mission.guid or "").strip()
        if not raw_guid:
            continue

        key = raw_guid.lower()
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = mission
            ordered_keys.append(key)
            continue

        # Prefer rows that contain a KMZ name when duplicate shell entries are returned.
        has_kmz = bool((mission.kmz_name or "").strip())
        existing_has_kmz = bool((existing.kmz_name or "").strip())
        if has_kmz and not existing_has_kmz:
            deduped[key] = mission

    return [deduped[key] for key in ordered_keys]


def _is_guid_folder_name(name: str) -> bool:
    return bool(_GUID_FOLDER_RE.fullmatch((name or "").strip()))


def _expand_folder_candidates(
    seeds: List[str],
    *,
    list_folder_items: Callable[[str], Tuple[bool, List[Tuple[str, bool, str]] | str]],
    join_folder_path: Callable[[str, str], str],
    deadline: float | None,
    max_depth: int,
    max_folders: int,
    max_listings: int,
    include_tokens: Tuple[str, ...] | None,
    skip_tokens: Tuple[str, ...] | None,
) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    queue_items: List[Tuple[str, int]] = []
    listing_count = 0

    for path in seeds:
        cleaned = (path or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
        queue_items.append((cleaned, 0))

    index = 0
    while index < len(queue_items) and len(ordered) < max_folders:
        if deadline is not None and time.monotonic() > deadline:
            break
        if listing_count >= max_listings:
            break
        folder, depth = queue_items[index]
        index += 1
        if depth >= max_depth:
            continue

        ok, listed = list_folder_items(folder)
        listing_count += 1
        if not ok:
            continue

        items = listed if isinstance(listed, list) else []
        for name, is_folder, _ in items:
            if not is_folder:
                continue
            lowered = name.lower()
            if skip_tokens and any(token in lowered for token in skip_tokens):
                continue
            if include_tokens and not any(token in lowered for token in include_tokens):
                continue
            child = join_folder_path(folder, name)
            if child in seen:
                continue
            seen.add(child)
            ordered.append(child)
            if len(ordered) >= max_folders:
                break
            queue_items.append((child, depth + 1))

    return ordered


def _preview_cache_dir() -> str:
    cache_root = os.path.join(tempfile.gettempdir(), "djirc2kmzsync-previews")
    os.makedirs(cache_root, exist_ok=True)
    return cache_root


def _preview_cache_base(root: str, guid: str) -> str:
    root_hash = hashlib.sha1(root.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return os.path.join(_preview_cache_dir(), f"{root_hash}-{guid}")


def _find_cached_preview(cache_base: str, guid: str) -> str | None:
    for suffix in _PREVIEW_SUFFIXES:
        path = f"{cache_base}{suffix}"
        if _is_usable_preview(path):
            return path
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    return None


def _is_usable_preview(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        if os.path.getsize(path) <= 0:
            return False
    except OSError:
        return False
    if Image is not None:
        try:
            with Image.open(path) as img:
                img.verify()
        except Exception:
            return False
    return True


def _promote_preview(temp_path: str, cache_path: str) -> None:
    cache_base = os.path.splitext(cache_path)[0]
    for suffix in _PREVIEW_SUFFIXES:
        candidate = f"{cache_base}{suffix}"
        if candidate == cache_path:
            continue
        if os.path.exists(candidate):
            try:
                os.remove(candidate)
            except OSError:
                pass
    os.replace(temp_path, cache_path)


def _clear_preview_cache(root: str) -> None:
    cache_dir = _preview_cache_dir()
    root_hash = hashlib.sha1(root.encode("utf-8", errors="ignore")).hexdigest()[:12]
    prefix = root_hash + "-"
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix):
            continue
        full = os.path.join(cache_dir, name)
        if os.path.isfile(full):
            try:
                os.remove(full)
            except OSError:
                pass


def _choose_preview_name(guid: str, items: List[Tuple[str, bool, str]]) -> str | None:
    """Find a preview image filename for guid in a list of (name, is_folder, modified) tuples."""
    guid_lower = guid.strip().lower()
    file_names = [name for name, is_folder, _modified in items if not is_folder]
    for suffix in _PREVIEW_SUFFIXES:
        target = f"{guid_lower}{suffix}"
        for name in file_names:
            if name.strip().lower() == target:
                return name
    return None


def _choose_preview_folder(guid: str, names: List[str]) -> str | None:
    target = guid.strip().lower()
    for name in names:
        if str(name).strip().lower() == target:
            return str(name).strip()
    return None
