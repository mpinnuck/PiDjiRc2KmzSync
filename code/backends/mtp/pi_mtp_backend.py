"""
pi_mtp_backend.py
---------------------
Raspberry Pi Debian/Linux MTP backend -- implements the nine _raw_* primitives
via pymtp (libmtp). Ported from the desktop app's MacMTPBackend
(backends/mtp/mac_mtp_backend.py), which uses the same pymtp/libmtp
stack -- libmtp is a cross-platform native library, so the vast majority
of the session/traversal/transfer logic carries over unchanged. Only a
few things are genuinely macOS-specific and have been adapted or dropped:

Adapted:
    - _preload_libmtp(): searched Homebrew paths on macOS; now searches
      standard Debian/Raspberry Pi OS locations instead.
    - _release_host_camera_hold() -> _release_host_mtp_hold(): the Mac
      version used `ioreg -c IOUSBHostInterface` to find PIDs holding
      the RC-2's USB interface exclusively (ptpcamerad/mscamerad), then
      SIGTERM/SIGKILL'd them. On Linux (the Pi, this module's actual
      deployment target) the equivalent culprits are GVFS's
      gvfs-mtp-volume-monitor / gvfs-gphoto2-volume-monitor (and the
      gvfsd-mtp worker they spawn) plus stray gphoto2/mtpfs/jmtpfs/
      android-file-transfer processes, found by process name via `pgrep`
      rather than USB interface introspection.

      In practice this module is often run directly on a Mac for local
      development/testing before deploying to the Pi, so
      _find_competing_process_pids() dispatches on platform.system():
      on Darwin it reuses the original ioreg-based camera-daemon lookup
      (_find_camera_owner_pids_macos, ported near-verbatim from
      MacMTPBackend); on Linux it uses the pgrep-based lookup above.
      Same release sequence (SIGTERM, then SIGKILL if still present)
      either way.

Synced from the desktop app (2026-10 performance work):
    - Listing caches are kept for the life of a connection and patched in
      place on upload/delete (_cache_add_file/_cache_remove_file) instead of
      being dropped, so a copy no longer triggers a full device rescan.
      Only a user refresh (invalidate_cache) drops them.
    - _raw_read_file() uses the desktop's full retry structure (cached
      item_id fallback, resolve-and-retry passes, reconnect on disconnect).
    - get_preview_paths_bulk(): all missing previews in one session pass.
    - The pre-transfer reconnect (about 16 s on the RC-2) is off by default;
      set PIDJIRC2KMZSYNC_MTP_TRANSFER_RECONNECT_IDLE_SECONDS=15 to restore
      the old behaviour.
    - Pi-only parts are unchanged: Debian libmtp paths, gvfs/pgrep process
      release, and the USB-enumeration check in _raw_probe().

Kept unchanged (the RC-2-specific behavior that actually matters):
    - GetPartialObject chunked reads in _pull_to_path_via_partial(),
      since RC-2 firmware blocks plain MTP GetObject but allows
      GetPartialObject.
    - All folder/file tree traversal and caching logic.

Requires:
    sudo apt install libmtp-dev
    pip install pymtp

Setup on the RC-2 side:
    Connect via USB-C. Accept any "Allow USB file access" prompt on
    the RC-2 screen if one appears.
"""

from __future__ import annotations

import ctypes
import glob
import logging
import os
import platform
import re
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from ctypes.util import find_library
from datetime import datetime
from typing import List, Tuple

from backends.rc_backend import (
    RCBackend,
    _find_cached_preview,
    _is_usable_preview,
    _mtp_join,
    _mtp_segments,
    _preview_cache_base,
    _promote_preview,
)
from config.config_manager import ConfigManager

os.environ.setdefault("LIBMTP_DEBUG", "0")


def _preload_libmtp() -> None:
    """Load libmtp into the global symbol table before importing pymtp.

    On Linux this is usually unnecessary (the dynamic linker resolves
    libmtp.so via the standard search path once libmtp-dev/libmtp9 is
    installed), but preloading explicitly avoids any ctypes symbol
    resolution surprises, matching the desktop app's macOS approach.
    """
    candidate_paths: list[str] = []

    found = find_library("mtp")
    if found:
        candidate_paths.append(found)

    for search_dir in (
        "/usr/lib/aarch64-linux-gnu",   # 64-bit Raspberry Pi OS
        "/usr/lib/arm-linux-gnueabihf", # 32-bit Raspberry Pi OS
        "/usr/lib",
        "/usr/local/lib",
    ):
        candidate_paths.extend(
            sorted(glob.glob(f"{search_dir}/libmtp.so*"), reverse=True)
        )

    for candidate in candidate_paths:
        if not candidate:
            continue
        try:
            ctypes.CDLL(candidate, mode=ctypes.RTLD_GLOBAL)
            return
        except OSError:
            continue


try:
    _preload_libmtp()
    import pymtp as _pymtp
except Exception as exc:
    _pymtp = None
    _PYMTP_IMPORT_ERROR = exc
else:
    _PYMTP_IMPORT_ERROR = None

if _pymtp is not None:
    _pymtp.__DEBUG__ = 0


_LOG = logging.getLogger(__name__)


def _decode(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


@contextmanager
def _suppress_stdio():
    """Suppress native library writes to stdout/stderr."""
    try:
        import sys
        sys.stdout.flush()
        sys.stderr.flush()
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        saved_stdout_fd = os.dup(1)
        saved_stderr_fd = os.dup(2)
    except OSError:
        yield
        return
    try:
        os.dup2(devnull_fd, 1)
        os.dup2(devnull_fd, 2)
        yield
    finally:
        try:
            os.dup2(saved_stdout_fd, 1)
            os.dup2(saved_stderr_fd, 2)
        finally:
            for fd in (saved_stdout_fd, saved_stderr_fd, devnull_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass


def _walk_folder_tree(root_ptr):
    """Walk a native LIBMTP folder linked list (depth-first)."""
    stack = [root_ptr] if root_ptr else []
    while stack:
        node_ptr = stack.pop()
        if not node_ptr:
            continue
        node = node_ptr.contents
        yield node
        if node.sibling:
            stack.append(node.sibling)
        if node.child:
            stack.append(node.child)


class PiMTPBackend(RCBackend):
    """
    Raspberry Pi MTP backend using pymtp (libmtp). Implements the nine
    _raw_* primitives; all orchestration lives in RCBackend.
    """

    DEFAULT_ROOT = (
        "mtp:DJI RC 2|Internal shared storage|Android|data"
        "|dji.go.v5|files|waypoint"
    )

    # Linux processes known to grab MTP/PTP USB devices exclusively,
    # blocking pymtp/libmtp from opening a session. GVFS auto-mounts any
    # MTP device it sees (gvfs-mtp-volume-monitor spawns a per-device
    # gvfsd-mtp worker); gphoto2/mtpfs/jmtpfs/android-file-transfer are
    # included in case a user has one running manually.
    _MTP_COMPETING_PROCESSES = frozenset({
        "gvfsd-mtp",
        "gvfs-mtp-volume-monitor",
        "gvfs-gphoto2-volume-monitor",
        "gvfsd-gphoto2",
        "gphoto2",
        "mtpfs",
        "jmtpfs",
        "simple-mtpfs",
        "android-file-transfer",
    })

    # macOS equivalent, only relevant when this module is run on a Mac for
    # local development/testing before deploying to the Pi -- ptpcamerad
    # and friends grab the RC-2's USB interface the same way GVFS does on
    # Linux. Ported from the desktop app's MacMTPBackend._CAMERA_DAEMONS.
    _MACOS_CAMERA_DAEMONS = frozenset({
        "ptpcamera",
        "ptpcameraagent",
        "ptpcamerad",
        "mscamerad",
    })

    def __init__(self, config: ConfigManager) -> None:
        super().__init__(config)
        self._available: bool = _pymtp is not None
        self._mtp = _pymtp.MTP() if _pymtp is not None else None
        self._connected = False
        self._connection_lock = threading.RLock()
        self._last_transfer_monotonic: float | None = None
        self._transfer_reconnect_idle_seconds = float(
            os.environ.get("PIDJIRC2KMZSYNC_MTP_TRANSFER_RECONNECT_IDLE_SECONDS", "0")
        )
        # Folder and file listing caches valid for the life of one connection.
        self._folder_cache: list | None = None
        self._folders_by_parent_cache: dict[int, list] | None = None
        self._file_cache: list | None = None
        self._files_by_parent_cache: dict[int, list] | None = None
        # Strict safe mode avoids cold-cache global file indexing for non-ID
        # lookups (for example folder browsing); operations requiring item IDs
        # can still populate the global file index.
        self._strict_safe_mode = os.environ.get(
            "PIDJIRC2KMZSYNC_MTP_STRICT_SAFE_MODE", "0"
        ).strip() in {"1", "true", "yes", "on"}
        self._logged_global_file_index = False
        self._last_read_item_ids: dict[str, int] = {}
        self._last_fast_pull_exception: Exception | None = None

    # ==================================================================
    # _raw_* primitives
    # ==================================================================

    def _raw_list_folder(
        self, path: str
    ) -> Tuple[bool, List[Tuple[str, bool, str]] | str]:
        if not self._available:
            return False, self._unavailable()
        try:
            with self._session():
                folder = self._resolve_folder(path)
                if folder is None:
                    # A transient probe/list race can cache an incomplete tree.
                    # Refresh caches and retry once, then reconnect and retry once.
                    self._folder_cache = None
                    self._folders_by_parent_cache = None
                    folder = self._resolve_folder(path)
                    if folder is None:
                        self._disconnect()
                        self._ensure_connected()
                        folder = self._resolve_folder(path)
                    if folder is None:
                        return False, f"MTP path not found: {path}"
                child_folders, child_files = self._child_entries(int(folder.folder_id))
                rows: List[Tuple[str, bool, str]] = []
                for entry in child_folders:
                    name = _decode(getattr(entry, "name", "")).strip()
                    if name:
                        rows.append((name, True, ""))
                for entry in child_files:
                    name = _decode(getattr(entry, "filename", "")).strip()
                    if not name:
                        continue
                    modified = ""
                    try:
                        ts = int(getattr(entry, "modificationdate", 0))
                        if ts > 0:
                            modified = datetime.fromtimestamp(ts).strftime(
                                "%d/%m/%Y %H:%M:%S"
                            )
                    except Exception:
                        pass
                    rows.append((name, False, modified))
                rows.sort(key=lambda r: r[0].lower())
                return True, rows
        except Exception as exc:
            return False, self._fmt_exc(exc)

    def _raw_read_file(
        self, folder_path: str, filename: str, local_dest: str
    ) -> Tuple[bool, str]:
        if not self._available:
            return False, self._unavailable()
        try:
            with self._session():
                self._refresh_session_before_transfer("read")
                retry_attempts = 2
                cache_key = self._read_item_cache_key(folder_path, filename)
                cached_item_id = self._last_read_item_ids.get(cache_key)
                entry = self._find_file(folder_path, filename)
                if entry is None:
                    if cached_item_id is not None:
                        _LOG.warning(
                            "MTP read initial lookup did not find %s; trying cached item_id=%s.",
                            filename,
                            cached_item_id,
                        )
                        if self._pull_to_path_fast(cached_item_id, local_dest):
                            self._mark_transfer_activity()
                            return True, local_dest

                    _LOG.warning(
                        "MTP read initial lookup did not find %s; retrying resolve with cache refresh.",
                        filename,
                    )
                    for attempt in range(1, retry_attempts + 1):
                        _LOG.warning(
                            "MTP read resolve retry %s/%s for %s: refreshing caches and re-resolving source file.",
                            attempt,
                            retry_attempts,
                            filename,
                        )
                        self._drop_listing_caches()
                        entry = self._find_file(folder_path, filename)
                        if entry is not None:
                            break

                        if cached_item_id is not None:
                            _LOG.warning(
                                "MTP read resolve retry %s/%s for %s: trying cached item_id=%s.",
                                attempt,
                                retry_attempts,
                                filename,
                                cached_item_id,
                            )
                            if self._pull_to_path_fast(cached_item_id, local_dest):
                                self._mark_transfer_activity()
                                return True, local_dest

                        if attempt < retry_attempts:
                            time.sleep(0.2)

                if entry is None:
                    return False, f"MTP file not found after retries: {filename}"

                first_item_id = int(entry.item_id)
                self._last_read_item_ids[cache_key] = first_item_id
                cached_item_id = first_item_id
                if self._pull_to_path_fast(first_item_id, local_dest):
                    self._mark_transfer_activity()
                    return True, local_dest

                first_pull_exc = self._last_fast_pull_exception
                if first_pull_exc is not None:
                    _LOG.warning(
                        "MTP read in-session failed for %s (item_id=%s): %s",
                        filename,
                        first_item_id,
                        self._fmt_exc(first_pull_exc),
                    )
                    if self._is_disconnect_error(first_pull_exc):
                        _LOG.warning(
                            "MTP read detected disconnect-like failure for %s; reconnecting session once before retries.",
                            filename,
                        )
                        self._disconnect()
                        self._ensure_connected()
                        self._drop_listing_caches()

                _LOG.warning(
                    "MTP read fast attempt failed for %s (item_id=%s); entering in-session retries.",
                    filename,
                    first_item_id,
                )

                missing_after_retry = False
                for attempt in range(1, retry_attempts + 1):
                    _LOG.warning(
                        "MTP read retry %s/%s for %s: refreshing caches and re-resolving source file.",
                        attempt,
                        retry_attempts,
                        filename,
                    )
                    self._drop_listing_caches()

                    entry_retry = self._find_file(folder_path, filename)
                    if entry_retry is None:
                        if cached_item_id is not None:
                            _LOG.warning(
                                "MTP read retry %s/%s for %s: file lookup missing; trying cached item_id=%s.",
                                attempt,
                                retry_attempts,
                                filename,
                                cached_item_id,
                            )
                            pulled_cached = self._pull_to_path_fast(
                                cached_item_id,
                                local_dest,
                            )
                            if pulled_cached:
                                _LOG.warning(
                                    "MTP read retry %s/%s for %s succeeded using cached item_id=%s.",
                                    attempt,
                                    retry_attempts,
                                    filename,
                                    cached_item_id,
                                )
                                self._mark_transfer_activity()
                                return True, local_dest

                            cached_pull_exc = self._last_fast_pull_exception
                            if cached_pull_exc is not None:
                                _LOG.warning(
                                    "MTP read retry %s/%s for %s cached item_id=%s failed: %s",
                                    attempt,
                                    retry_attempts,
                                    filename,
                                    cached_item_id,
                                    self._fmt_exc(cached_pull_exc),
                                )
                                if self._is_disconnect_error(cached_pull_exc):
                                    _LOG.warning(
                                        "MTP read retry %s/%s for %s saw disconnect-like cached pull failure; reconnecting session before next attempt.",
                                        attempt,
                                        retry_attempts,
                                        filename,
                                    )
                                    self._disconnect()
                                    self._ensure_connected()
                                    self._drop_listing_caches()

                        _LOG.warning(
                            "MTP read retry %s/%s for %s: file not found after cache refresh.",
                            attempt,
                            retry_attempts,
                            filename,
                        )
                        missing_after_retry = True
                        if attempt < retry_attempts:
                            time.sleep(0.2)
                        continue

                    missing_after_retry = False
                    retry_item_id = int(entry_retry.item_id)
                    pulled = self._pull_to_path_fast(retry_item_id, local_dest)

                    if pulled:
                        self._last_read_item_ids[cache_key] = retry_item_id
                        _LOG.warning(
                            "MTP read retry %s/%s for %s succeeded (item_id=%s).",
                            attempt,
                            retry_attempts,
                            filename,
                            retry_item_id,
                        )
                        self._mark_transfer_activity()
                        return True, local_dest

                    retry_pull_exc = self._last_fast_pull_exception
                    if retry_pull_exc is not None:
                        _LOG.warning(
                            "MTP read retry %s/%s for %s failed (item_id=%s): %s",
                            attempt,
                            retry_attempts,
                            filename,
                            retry_item_id,
                            self._fmt_exc(retry_pull_exc),
                        )
                        if self._is_disconnect_error(retry_pull_exc):
                            _LOG.warning(
                                "MTP read retry %s/%s for %s saw disconnect-like failure; reconnecting session before next attempt.",
                                attempt,
                                retry_attempts,
                                filename,
                            )
                            self._disconnect()
                            self._ensure_connected()
                            self._drop_listing_caches()
                    else:
                        _LOG.warning(
                            "MTP read retry %s/%s for %s failed (item_id=%s).",
                            attempt,
                            retry_attempts,
                            filename,
                            retry_item_id,
                        )

                    if attempt < retry_attempts:
                        time.sleep(0.2)

                if missing_after_retry:
                    return False, f"MTP file not found after retries: {filename}"
                return False, "MTP pull failed for " f"{filename} after {retry_attempts + 1} attempts"
        except Exception as exc:
            return False, f"MTP read failed:\n{self._fmt_exc(exc)}"

    def _raw_write_file(
        self, dest_folder: str, local_source: str, dest_filename: str
    ) -> Tuple[bool, str]:
        if not os.path.isfile(local_source):
            return False, f"Source file not found:\n{local_source}"
        if not self._available:
            return False, self._unavailable()

        try:
            with self._session():
                self._refresh_session_before_transfer("write")
                folder = self._resolve_folder(dest_folder)
                if folder is None:
                    return False, f"Destination folder not found: {dest_folder}"

                # Delete existing file silently before sending.
                existing = self._find_file_in_folder(
                    int(folder.folder_id), dest_filename
                )
                if existing is not None:
                    deleted = False
                    try:
                        self._quiet(self._mtp.delete_object, int(existing.item_id))
                        deleted = True
                    except Exception as exc:
                        # Stale item IDs can occur with lagging MTP indices.
                        # Continue and let the subsequent write proceed.
                        _LOG.debug(
                            "Pre-write delete failed for %s: %s",
                            dest_filename,
                            self._fmt_exc(exc),
                        )
                    if deleted:
                        self._cache_remove_file(int(existing.item_id))
                    else:
                        # Deletions can be asynchronous over MTP; poll briefly
                        # so the upload does not race the old object state.
                        self._drop_listing_caches()
                        self._wait_for_file_delete(int(folder.folder_id), dest_filename)

                send_fn = self._mtp.mtp.LIBMTP_Send_File_From_File
                sent: dict = {}

                def _send_once(target_folder) -> int:
                    filetype = self._quiet(self._mtp.find_filetype, local_source)
                    filesize = os.stat(local_source).st_size
                    filename_bytes = dest_filename.encode("utf-8")
                    metadata = _pymtp.LIBMTP_File(
                        filename=filename_bytes,
                        filetype=filetype,
                        filesize=filesize,
                    )
                    metadata.parent_id = int(target_folder.folder_id)
                    metadata.storage_id = int(target_folder.storage_id)
                    sent["metadata"] = metadata
                    return self._quiet(
                        send_fn,
                        self._mtp.device,
                        local_source.encode("utf-8"),
                        ctypes.pointer(metadata),
                        None,
                        None,
                    )

                first_ret = _send_once(folder)
                if first_ret != 0:
                    try:
                        self._quiet(self._mtp.debug_stack)
                    except Exception:
                        pass

                    # A stale long-lived session can fail writes; reconnect and
                    # retry once with freshly resolved folder metadata.
                    self._disconnect()
                    self._ensure_connected()
                    folder_retry = self._resolve_folder(dest_folder)
                    if folder_retry is None:
                        raise RuntimeError(
                            f"Destination folder not found after reconnect: {dest_folder}"
                        )

                    existing_retry = self._find_file_in_folder(
                        int(folder_retry.folder_id), dest_filename
                    )
                    if existing_retry is not None:
                        try:
                            self._quiet(self._mtp.delete_object, int(existing_retry.item_id))
                        except Exception:
                            pass
                        self._drop_listing_caches()
                        self._wait_for_file_delete(int(folder_retry.folder_id), dest_filename)

                    retry_ret = _send_once(folder_retry)
                    folder = folder_retry
                    if retry_ret != 0:
                        try:
                            self._quiet(self._mtp.debug_stack)
                        except Exception:
                            pass
                        raise RuntimeError(
                            "LIBMTP_Send_File_From_File returned "
                            f"{first_ret} (initial) and {retry_ret} (after reconnect) "
                            f"for '{os.path.basename(local_source)}' "
                            f"to '{dest_filename}'"
                        )
                self._record_uploaded_file(folder, dest_filename, sent.get("metadata"))
                self._last_read_item_ids = {}
                self._mark_transfer_activity()
            return True, f"Copied to {_mtp_join(dest_folder, dest_filename)}"
        except Exception as exc:
            return False, f"MTP write failed:\n{self._fmt_exc(exc)}"

    def get_file_size_from_path(
        self, folder: str, filename: str
    ) -> Tuple[bool, int | str]:
        if not self._available:
            return False, self._unavailable()
        try:
            with self._session():
                entry = self._find_file(folder, filename)
                if entry is None:
                    return False, f"MTP file not found: {filename}"
                size = int(getattr(entry, "filesize", 0))
                if size < 0:
                    size = 0
                return True, size
        except Exception as exc:
            return False, f"MTP size lookup failed:\n{self._fmt_exc(exc)}"

    def _raw_delete_file(
        self, folder_path: str, filename: str
    ) -> Tuple[bool, str]:
        if not self._available:
            return False, self._unavailable()
        try:
            with self._session():
                entry = self._find_file(folder_path, filename)
                if entry is None:
                    return True, "NOT_FOUND"
                self._quiet(self._mtp.delete_object, int(entry.item_id))
                self._cache_remove_file(int(entry.item_id))
            return True, f"Deleted {filename}"
        except Exception as exc:
            return False, f"MTP delete failed:\n{self._fmt_exc(exc)}"

    def _raw_delete_folder(self, folder_path: str) -> Tuple[bool, str]:
        if not self._available:
            return False, self._unavailable()
        try:
            with self._session():
                folder = self._resolve_folder(folder_path)
                if folder is None:
                    return False, f"Folder not found: {folder_path}"
                self._delete_folder_recursive(int(folder.folder_id))
                self._folder_cache = None
                self._folders_by_parent_cache = None
                self._file_cache   = None
                self._files_by_parent_cache = None
            segments = _mtp_segments(folder_path)
            name = segments[-1] if segments else folder_path
            return True, f"Deleted {name}"
        except Exception as exc:
            return False, f"MTP folder delete failed:\n{self._fmt_exc(exc)}"

    def _raw_create_folder(
        self, parent_path: str, name: str
    ) -> Tuple[bool, str]:
        if not self._available:
            return False, self._unavailable()
        try:
            with self._session():
                parent = self._resolve_folder(parent_path)
                if parent is None:
                    return False, f"Parent folder not found: {parent_path}"
                self._quiet(
                    self._mtp.create_folder,
                    name,
                    parent=int(parent.folder_id),
                    storage=int(parent.storage_id),
                )
                self._folder_cache = None
                self._folders_by_parent_cache = None
            return True, _mtp_join(parent_path, name)
        except Exception as exc:
            return False, f"MTP create folder failed:\n{self._fmt_exc(exc)}"

    def _raw_probe(self, root: str) -> bool:
        if not self._available:
            return False
        try:
            # Check USB enumeration before reusing the current session. A
            # cable removal can leave pymtp's session object marked connected
            # even though the RC-2 has disappeared from the USB bus.
            with self._connection_lock:
                devices = self._quiet(self._mtp.detect_devices)
                if not devices:
                    self._disconnect()
                    return False
            with self._session():
                # _session() already pinged the device (get_serialnumber), so
                # liveness is proven. Reuse the cached folder tree: the status
                # poll runs every few seconds and rebuilding the tree each time
                # made every copy that overlapped a poll re-read it from the RC-2.
                if self._waypoint_folder_exists():
                    return True
                # Not in the cached tree (possibly stale/incomplete): rebuild once.
                self._folder_cache = None
                self._folders_by_parent_cache = None
                exists = self._waypoint_folder_exists()
                if not exists:
                    self._folder_cache = None
                    self._folders_by_parent_cache = None
                return exists
        except Exception:
            return False

    def _waypoint_folder_exists(self) -> bool:
        for folder in self._all_folders():
            name = _decode(getattr(folder, "name", "")).strip().lower()
            if name == "waypoint":
                return True
        return False

    def _raw_get_status(self) -> Tuple[bool, str]:
        if not self._available:
            return False, "Native MTP unavailable: install pymtp and libmtp."
        root = self._root() or self.DEFAULT_ROOT
        ok, _ = self._raw_list_folder(root)
        if ok:
            return True, "MTP RC-2 waypoint path is reachable."
        return False, (
            "MTP RC-2 waypoint path is not reachable. "
            "Ensure RC-2 is connected via USB and unlocked."
        )

    def _raw_connection_mode(self) -> str:
        return "MTP"

    def get_preview_paths_bulk(
        self,
        root: str,
        guids: list[str],
        copy_timeout_seconds: int | None = None,
        list_timeout_seconds: int | None = None,
    ) -> dict[str, str | None]:
        del copy_timeout_seconds, list_timeout_seconds  # not used by this backend path

        result: dict[str, str | None] = {}
        missing: list[str] = []
        for guid in guids:
            cache_base = _preview_cache_base(root, guid)
            cached = _find_cached_preview(cache_base, guid)
            result[guid] = cached
            if not cached:
                missing.append(guid)

        if not missing:
            return result

        preview_folder_path = _mtp_join(root, "map_preview")
        try:
            with self._session():
                preview_folder = self._resolve_folder(preview_folder_path)
                if preview_folder is None:
                    return result

                preview_folder_id = int(preview_folder.folder_id)
                child_folders, child_files = self._child_entries(preview_folder_id)

                top_files_by_name = {
                    _decode(getattr(entry, "filename", "")).strip().lower(): entry
                    for entry in child_files
                }
                subfolders_by_name = {
                    _decode(getattr(folder, "name", "")).strip().lower(): folder
                    for folder in child_folders
                }

                nested_files_by_guid: dict[str, dict[str, object]] = {}
                for guid in missing:
                    folder = subfolders_by_name.get(guid.strip().lower())
                    if folder is None:
                        continue
                    nested_id = int(getattr(folder, "folder_id", -1))
                    if nested_id < 0:
                        continue
                    _, nested_files = self._child_entries(nested_id)
                    nested_files_by_guid[guid] = {
                        _decode(getattr(entry, "filename", "")).strip().lower(): entry
                        for entry in nested_files
                    }

                selected_entries: dict[str, tuple[int, str]] = {}
                for guid in missing:
                    guid_lower = guid.strip().lower()
                    entry = None
                    selected_name = ""
                    for suffix in (".jpg", ".jpeg", ".png"):
                        candidate_name = f"{guid_lower}{suffix}"
                        top_match = top_files_by_name.get(candidate_name)
                        if top_match is not None:
                            entry = top_match
                            selected_name = candidate_name
                            break
                        nested_map = nested_files_by_guid.get(guid)
                        if nested_map is not None:
                            nested_match = nested_map.get(candidate_name)
                            if nested_match is not None:
                                entry = nested_match
                                selected_name = candidate_name
                                break

                    if entry is None:
                        result[guid] = None
                        continue

                    item_id = int(getattr(entry, "item_id", -1))
                    if item_id < 0:
                        result[guid] = None
                        continue

                    selected_entries[guid] = (item_id, selected_name)

                if not selected_entries:
                    return result

                # Fast in-session pulls (partial-read first, GetObject fallback)
                # are robust on RC-2 and avoid any competing CLI sessions.
                for guid, (item_id, selected_name) in selected_entries.items():
                    cache_base = _preview_cache_base(root, guid)
                    ext = os.path.splitext(selected_name)[1].lower() or ".jpg"
                    cache_path = f"{cache_base}{ext}"
                    temp_path = f"{cache_path}.{os.getpid()}.{guid}.tmp"
                    try:
                        if self._pull_to_path_fast(item_id, temp_path) and _is_usable_preview(temp_path):
                            _promote_preview(temp_path, cache_path)
                            result[guid] = cache_path
                            continue
                        result[guid] = None
                    finally:
                        if os.path.exists(temp_path):
                            try:
                                os.remove(temp_path)
                            except OSError:
                                pass
        except Exception:
            # Preserve existing behavior: preview failures are non-fatal.
            return result

        return result

    # ==================================================================
    # Session management
    # ==================================================================

    def close(self) -> None:
        """Close the persistent MTP session (call on app shutdown)."""
        with self._connection_lock:
            self._disconnect()

    def _disconnect(self) -> None:
        if self._mtp is None:
            self._connected = False
            return
        try:
            self._quiet(self._mtp.disconnect)
        except Exception:
            pass
        self._connected   = False
        self._folder_cache = None
        self._folders_by_parent_cache = None
        self._file_cache   = None
        self._files_by_parent_cache = None
        self._logged_global_file_index = False
        self._last_read_item_ids = {}

    def _mark_transfer_activity(self) -> None:
        self._last_transfer_monotonic = time.monotonic()

    def _refresh_session_before_transfer(self, op_name: str) -> None:
        # A full reconnect costs ~16s and can re-enumerate only part of the
        # device, so it is opt-in. Failed reads/writes already reconnect and
        # retry on demand.
        if self._transfer_reconnect_idle_seconds <= 0:
            return
        if self._last_transfer_monotonic is None:
            return
        idle_for = time.monotonic() - self._last_transfer_monotonic
        if idle_for < self._transfer_reconnect_idle_seconds:
            return
        _LOG.debug(
            "MTP transfer preflight reconnect for %s after %.1fs idle.",
            op_name,
            idle_for,
        )
        self._disconnect()
        self._ensure_connected()

    def invalidate_transfer_caches(self) -> None:
        """Called before copy operations; intentionally keeps the listing cache.

        Each full device listing costs several seconds on the RC-2, so the index
        built at refresh time is reused and patched in place by writes/deletes.
        Refresh (invalidate_cache) and read-retry paths drop it explicitly.
        """

    def _drop_listing_caches(self) -> None:
        self._folder_cache = None
        self._folders_by_parent_cache = None
        self._file_cache = None
        self._files_by_parent_cache = None

    def _cache_remove_file(self, item_id: int) -> None:
        if self._file_cache is not None:
            self._file_cache = [
                e for e in self._file_cache if int(getattr(e, "item_id", -1)) != item_id
            ]
        if self._files_by_parent_cache is not None:
            for parent_id, entries in list(self._files_by_parent_cache.items()):
                self._files_by_parent_cache[parent_id] = [
                    e for e in entries if int(getattr(e, "item_id", -1)) != item_id
                ]

    def _cache_add_file(self, entry) -> None:
        if self._file_cache is not None:
            self._file_cache.append(entry)
        if self._files_by_parent_cache is not None:
            self._files_by_parent_cache.setdefault(
                int(getattr(entry, "parent_id", -1)), []
            ).append(entry)

    def _device_file_entry(self, item_id: int):
        """Fetch one object's metadata from the device (cheap, unlike a full listing)."""
        get_meta = getattr(self._mtp.mtp, "LIBMTP_Get_Filemetadata", None)
        if get_meta is None or item_id <= 0:
            return None
        try:
            get_meta.restype = ctypes.POINTER(_pymtp.LIBMTP_File)
            ptr = self._quiet(get_meta, self._mtp.device, ctypes.c_uint32(item_id))
            if not ptr:
                return None
            raw = ptr.contents
            entry = _pymtp.LIBMTP_File(
                item_id=int(raw.item_id),
                parent_id=int(raw.parent_id),
                storage_id=int(raw.storage_id),
                filename=raw.filename,
                filesize=int(raw.filesize),
                modificationdate=int(raw.modificationdate),
                filetype=int(raw.filetype),
            )
            destroy = getattr(self._mtp.mtp, "LIBMTP_destroy_file_t", None)
            if destroy is not None:
                try:
                    destroy(ptr)
                except Exception:
                    pass
            return entry
        except Exception as exc:
            _LOG.debug("Get_Filemetadata(%s) failed: %s", item_id, self._fmt_exc(exc))
            return None

    def _record_uploaded_file(self, folder, dest_filename: str, sent_metadata) -> None:
        """Patch the listing cache for a file just uploaded, using device-reported metadata.

        Falls back to dropping the cache (next lookup re-lists) if the device
        cannot report the new object, so size verification stays honest.
        """
        new_id = int(getattr(sent_metadata, "item_id", 0) or 0)
        entry = self._device_file_entry(new_id)
        if entry is None:
            self._drop_listing_caches()
            return
        self._cache_remove_file(new_id)
        self._cache_add_file(entry)

    def invalidate_cache(self) -> None:
        """Clear preview + listing caches so refresh sees newly created missions."""
        super().invalidate_cache()
        self._drop_listing_caches()
        self._last_read_item_ids = {}

    def _wait_for_file_delete(self, folder_id: int, filename: str, timeout: float = 2.5) -> None:
        delete_deadline = time.monotonic() + timeout
        while time.monotonic() < delete_deadline:
            if self._find_file_in_folder(folder_id, filename) is None:
                return
            time.sleep(0.1)
            self._file_cache = None
            self._files_by_parent_cache = None

    def _ensure_connected(self) -> None:
        if self._mtp is None:
            raise RuntimeError("Native MTP backend unavailable (pymtp not installed)")
        if self._connected:
            return
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                if not self._release_host_mtp_hold():
                    _LOG.warning(
                        "A competing process still appears to hold the MTP "
                        "interface after release attempt; connect may fail."
                    )
                try:
                    self._quiet(self._mtp.detect_devices)
                except Exception:
                    pass
                self._quiet(self._mtp.connect)
                self._connected = True
                self._folder_cache = None
                self._folders_by_parent_cache = None
                self._file_cache = None
                self._files_by_parent_cache = None
                self._logged_global_file_index = False
                return
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.35)
        self._connected = False
        raise last_exc or RuntimeError("Unable to connect to MTP device")

    @classmethod
    def _find_competing_process_pids(cls) -> list[tuple[int, str]]:
        """Return (pid, name) pairs for known MTP-competing processes.

        Dispatches by platform: this module targets Linux (the Pi) in
        production, but running it directly on macOS for local dev/testing
        is common enough (see class docstring) that it's worth detecting
        the actual macOS camera daemons rather than silently finding
        nothing.
        """
        if platform.system() == "Darwin":
            return cls._find_camera_owner_pids_macos()
        return cls._find_competing_process_pids_linux()

    @classmethod
    def _find_competing_process_pids_linux(cls) -> list[tuple[int, str]]:
        owners: list[tuple[int, str]] = []
        for name in cls._MTP_COMPETING_PROCESSES:
            try:
                proc = subprocess.run(
                    ["pgrep", "-x", name],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=2,
                )
            except Exception:
                continue
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    owners.append((int(line), name))
                except ValueError:
                    continue
        return owners

    @classmethod
    def _find_camera_owner_pids_macos(cls) -> list[tuple[int, str]]:
        """Find macOS camera-daemon PIDs claiming the RC-2's USB interface.

        Ported from the desktop app's MacMTPBackend._find_camera_owner_pids.
        idVendor 11427 / idProduct 4129 (decimal) identify the RC-2's USB
        device; ioreg's IOUSBHostInterface nodes report the exclusive
        owner PID/process name once a camera daemon has claimed it.
        """
        try:
            proc = subprocess.run(
                ["ioreg", "-r", "-l", "-w", "0", "-c", "IOUSBHostInterface"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
        except Exception:
            return []

        text = proc.stdout or ""
        if not text:
            return []

        owners: set[tuple[int, str]] = set()
        vendor_seen = False
        product_seen = False

        for line in text.splitlines():
            if '"idVendor" = 11427' in line:
                vendor_seen = True
            if '"idProduct" = 4129' in line:
                product_seen = True

            if '"UsbExclusiveOwner"' not in line:
                continue
            if not (vendor_seen and product_seen):
                vendor_seen = False
                product_seen = False
                continue

            match = re.search(r'pid\s+(\d+),\s*([^"\\]+)', line)
            if match:
                pid = int(match.group(1))
                name = match.group(2).strip().lower()
                if any(name.startswith(candidate) for candidate in cls._MACOS_CAMERA_DAEMONS):
                    owners.add((pid, name))

            vendor_seen = False
            product_seen = False

        return sorted(owners)

    def _release_host_mtp_hold(self) -> bool:
        """Release Linux processes that hold the MTP/PTP USB interface
        exclusively, so pymtp/libmtp can open its own session.

        Returns True if no competing process remains (or none was found),
        False if one or more processes still hold the interface after the
        release attempt.
        """
        owners = self._find_competing_process_pids()
        if not owners:
            return True

        _LOG.info(
            "Detected processes that may hold the RC-2 MTP interface: %s",
            ",".join(f"{name}:{pid}" for pid, name in owners),
        )

        for pid, _name in owners:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue

        time.sleep(0.2)

        stubborn = self._find_competing_process_pids()
        for pid, _name in stubborn:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                continue

        time.sleep(0.2)

        final = self._find_competing_process_pids()
        if final:
            _LOG.warning(
                "MTP interface claim persists after release attempt: %s",
                ",".join(f"{name}:{pid}" for pid, name in final),
            )
            return False

        _LOG.info("Released MTP interface hold; waiting briefly for USB re-enumeration.")
        time.sleep(0.5)
        return True

    def _ensure_healthy(self) -> None:
        """Verify session is alive; reconnect if stale."""
        if not self._connected or self._mtp is None:
            self._ensure_connected()
            return
        try:
            self._quiet(self._mtp.get_serialnumber)
        except Exception as exc:
            if self._is_disconnect_error(exc):
                self._disconnect()
                self._ensure_connected()
            else:
                # Keep the existing session for non-disconnect errors.
                raise

    @contextmanager
    def _session(self):
        """Acquire the connection lock, ensure session health, yield."""
        with self._connection_lock:
            self._ensure_healthy()
            try:
                yield
            except Exception as exc:
                if self._is_disconnect_error(exc):
                    self._disconnect()
                raise

    @staticmethod
    def _is_disconnect_error(exc: Exception) -> bool:
        text = (str(exc) or "").lower()
        name = type(exc).__name__.lower()
        return (
            "nodevice" in name
            or "notconnected" in name
            or "unable to initialize" in text
            or "open session" in text
            or ("usb" in text and "connection" in text)
        )

    def _quiet(self, method, *args, **kwargs):
        with _suppress_stdio():
            return method(*args, **kwargs)

    # ==================================================================
    # Native device traversal
    # ==================================================================

    def _all_folders(self) -> list:
        if self._folder_cache is not None:
            return self._folder_cache
        try:
            folders_map = self._quiet(self._mtp.get_folder_list)
            values = (
                list(folders_map.values())
                if hasattr(folders_map, "values")
                else list(folders_map)
            )
        except Exception as exc:
            if self._is_disconnect_error(exc):
                self._disconnect()
                self._ensure_connected()
                folders_map = self._quiet(self._mtp.get_folder_list)
                values = (
                    list(folders_map.values())
                    if hasattr(folders_map, "values")
                    else list(folders_map)
                )
            else:
                # Fallback: walk native linked list directly.
                with _suppress_stdio():
                    root_ptr = self._mtp.mtp.LIBMTP_Get_Folder_List(
                        self._mtp.device
                    )
                values = list(_walk_folder_tree(root_ptr))
        self._folder_cache = values
        by_parent: dict[int, list] = {}
        for folder in values:
            parent_id = int(getattr(folder, "parent_id", -1))
            by_parent.setdefault(parent_id, []).append(folder)
        self._folders_by_parent_cache = by_parent
        return values

    def _all_files(self, reason: str = "general") -> list:
        if self._file_cache is not None:
            return self._file_cache
        if not self._logged_global_file_index:
            _LOG.info(
                "Building global MTP file index (reason=%s). This can be expensive on data-heavy devices.",
                reason,
            )
            self._logged_global_file_index = True
        try:
            files = list(self._quiet(self._mtp.get_filelisting))
        except Exception as exc:
            if self._is_disconnect_error(exc):
                self._disconnect()
                self._ensure_connected()
                files = list(self._quiet(self._mtp.get_filelisting))
            else:
                raise
        self._file_cache = files
        by_parent: dict[int, list] = {}
        for entry in files:
            parent_id = int(getattr(entry, "parent_id", -1))
            by_parent.setdefault(parent_id, []).append(entry)
        self._files_by_parent_cache = by_parent
        return files

    def _files_by_parent(self) -> dict[int, list]:
        if self._files_by_parent_cache is not None:
            return self._files_by_parent_cache
        self._all_files(reason="parent_lookup")
        return self._files_by_parent_cache or {}

    def _folders_by_parent(self) -> dict[int, list]:
        if self._folders_by_parent_cache is not None:
            return self._folders_by_parent_cache
        self._all_folders()
        return self._folders_by_parent_cache or {}

    def _files_for_parent(self, parent_id: int, *, require_ids: bool = False) -> list:
        if self._strict_safe_mode and not require_ids:
            cached = self._files_by_parent_cache or {}
            return list(cached.get(parent_id, []))
        return list(self._files_by_parent().get(parent_id, []))

    def _root_folders(self) -> list:
        with self._connection_lock:
            return list(self._folders_by_parent().get(0, []))

    def _child_entries(self, parent_id: int, *, require_file_ids: bool = False) -> tuple[list, list]:
        folders = list(self._folders_by_parent().get(parent_id, []))
        files = self._files_for_parent(parent_id, require_ids=require_file_ids)
        return folders, files

    def _resolve_folder(self, path: str):
        """
        Navigate to a folder by the pipe-separated MTP path segments,
        starting from the device root folders (parent_id == 0).
        Handles both mtp:DJI RC 2|...  and bare Android/data/... paths.
        """
        segments = self._path_segments(path)
        if not segments:
            return None

        by_parent = self._folders_by_parent()
        # Start from device root (parent_id == 0).
        current = by_parent.get(0, [])

        for segment in segments:
            wanted = segment.strip().lower()
            next_level = []
            for folder in current:
                folder_id = int(getattr(folder, "folder_id", -1))
                for child in by_parent.get(folder_id, []):
                    if _decode(getattr(child, "name", "")).strip().lower() == wanted:
                        next_level.append(child)
            if not next_level:
                # Check current level itself for the first segment.
                matches = [
                    f for f in current
                    if _decode(getattr(f, "name", "")).strip().lower() == wanted
                ]
                if not matches:
                    return None
                current = matches
            else:
                current = next_level

        if current:
            return current[0]

        fallback = self._resolve_folder_by_suffix(segments)
        if fallback is not None:
            return fallback

        return current[0] if current else None

    def _resolve_folder_by_suffix(self, segments: List[str]):
        """Resolve a folder by matching the trailing segments anywhere in the tree."""
        if not segments:
            return None

        wanted = [segment.strip().lower() for segment in segments if segment.strip()]
        if not wanted:
            return None

        folders_by_parent = self._folders_by_parent()
        all_folders = self._all_folders()

        candidates = [
            folder
            for folder in all_folders
            if _decode(getattr(folder, "name", "")).strip().lower() == wanted[-1]
        ]

        for folder in candidates:
            current = folder
            matched = True
            for wanted_name in reversed(wanted[:-1]):
                parent_id = int(getattr(current, "parent_id", -1))
                parents = folders_by_parent.get(parent_id, [])
                parent_match = None
                for parent in parents:
                    if _decode(getattr(parent, "name", "")).strip().lower() == wanted_name:
                        parent_match = parent
                        break
                if parent_match is None:
                    matched = False
                    break
                current = parent_match

            if matched:
                return folder

        return None

    def _find_file(self, folder_path: str, filename: str):
        folder = self._resolve_folder(folder_path)
        if folder is None:
            return None
        return self._find_file_in_folder(int(folder.folder_id), filename)

    def _find_file_in_folder(self, folder_id: int, filename: str):
        wanted = filename.strip().lower()
        child_files = self._files_for_parent(folder_id, require_ids=True)
        for entry in child_files:
            if _decode(getattr(entry, "filename", "")).strip().lower() == wanted:
                return entry
        return None

    def _delete_folder_recursive(self, folder_id: int) -> None:
        child_folders, child_files = self._child_entries(folder_id, require_file_ids=True)
        for entry in child_files:
            self._quiet(self._mtp.delete_object, int(entry.item_id))
        for folder in child_folders:
            self._delete_folder_recursive(int(folder.folder_id))
        self._quiet(self._mtp.delete_object, folder_id)

    # ==================================================================
    # File pull helpers
    # ==================================================================

    def _pull_to_path_via_partial(self, item_id: int, local_dest: str) -> bool:
        """Pull by object id using chunked LIBMTP_GetPartialObject reads."""
        chunk_size = 65536
        get_partial = getattr(self._mtp.mtp, "LIBMTP_GetPartialObject", None)
        if get_partial is None:
            return False

        free_mem = getattr(self._mtp.mtp, "LIBMTP_FreeMemory", None)

        try:
            get_partial.restype = ctypes.c_int
        except Exception:
            pass

        total_bytes = 0
        chunk_count = 0
        offset = 0

        try:
            with open(local_dest, "wb") as out_fh:
                while True:
                    buf = ctypes.POINTER(ctypes.c_ubyte)()
                    buf_len = ctypes.c_uint32(0)
                    ret = self._quiet(
                        get_partial,
                        self._mtp.device,
                        ctypes.c_uint32(int(item_id)),
                        ctypes.c_uint64(offset),
                        ctypes.c_uint32(chunk_size),
                        ctypes.byref(buf),
                        ctypes.byref(buf_len),
                    )

                    if int(ret) != 0:
                        _LOG.warning(
                            "GetPartialObject item_id=%s offset=%s returned %s",
                            item_id,
                            offset,
                            ret,
                        )
                        return False

                    n = int(buf_len.value)
                    if n <= 0:
                        break

                    out_fh.write(ctypes.string_at(buf, n))
                    total_bytes += n
                    chunk_count += 1
                    offset += n

                    if callable(free_mem) and bool(buf):
                        try:
                            self._quiet(free_mem, ctypes.cast(buf, ctypes.c_void_p))
                        except Exception:
                            pass

                    if n < chunk_size:
                        break
        except Exception as exc:
            _LOG.warning(
                "GetPartialObject pull item_id=%s failed: %s",
                item_id,
                self._fmt_exc(exc),
            )
            return False

        if total_bytes <= 0:
            _LOG.warning(
                "GetPartialObject item_id=%s returned 0 bytes total.",
                item_id,
            )
            return False

        _LOG.debug(
            "GetPartialObject item_id=%s assembled %s bytes in %s chunk(s).",
            item_id,
            total_bytes,
            chunk_count,
        )
        return os.path.isfile(local_dest) and os.path.getsize(local_dest) > 0

    def _pull_to_path_fast(self, item_id: int, local_dest: str) -> bool:
        """Single-attempt pull path using partial reads then GetObject fallback."""
        self._last_fast_pull_exception = None

        # DJI RC-2 firmware can reject GetObject while allowing GetPartialObject.
        if self._pull_to_path_via_partial(item_id, local_dest):
            return True

        try:
            self._quiet(self._mtp.get_file_to_file, int(item_id), local_dest)
            return os.path.isfile(local_dest) and os.path.getsize(local_dest) > 0
        except Exception as exc:
            self._last_fast_pull_exception = exc
            return False

    # ==================================================================
    # Path helpers
    # ==================================================================

    @staticmethod
    def _path_segments(path: str) -> List[str]:
        """
        Convert an mtp: or bare path to a list of folder name segments.

        mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint
        → ["DJI RC 2", "Internal shared storage", "Android", "data",
           "dji.go.v5", "files", "waypoint"]

        Android/data/dji.go.v5/files/waypoint
        → ["Android", "data", "dji.go.v5", "files", "waypoint"]
        """
        raw = (path or "").strip()
        if raw.lower().startswith("mtp:"):
            raw = raw[4:].strip()
        # Normalise: pipe and slash are both valid separators.
        raw = raw.replace("\\", "/")
        parts = []
        for chunk in raw.split("|"):
            for part in chunk.split("/"):
                s = part.strip()
                if s:
                    parts.append(s)

        # macOS libmtp folder traversal starts at storage roots (e.g. Android),
        # not at Explorer-style labels like "DJI RC 2|Internal shared storage".
        # If Android appears later in the path, trim everything before it.
        for idx, segment in enumerate(parts):
            if segment.lower() == "android":
                if idx > 0:
                    parts = parts[idx:]
                break
        return parts

    @staticmethod
    def _read_item_cache_key(folder_path: str, filename: str) -> str:
        return f"{folder_path.strip().lower()}|{filename.strip().lower()}"

    # ==================================================================
    # Helpers
    # ==================================================================

    def _unavailable(self) -> str:
        return (
            "Native MTP unavailable. "
            "Install pymtp (pip install pymtp) and libmtp (brew install libmtp)."
        )

    @staticmethod
    def _fmt_exc(exc: Exception) -> str:
        msg = str(exc).strip() or repr(exc)
        return f"{exc.__class__.__name__}: {msg}"

