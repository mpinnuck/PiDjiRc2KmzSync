"""
linux_mtp_backend.py
---------------------
Linux (Raspberry Pi) MTP backend -- implements the nine _raw_* primitives
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

Simplified:
    - _raw_read_file()'s retry logic: the Mac version has several nested
      retry loops (cached item_id fallback, multiple resolve-and-retry
      passes) tuned from real-world macOS session flakiness. This port
      keeps one reconnect-and-retry cycle, which should cover the same
      "stale session" failure mode; if bench testing on the Pi turns up
      the same flakiness Mac needed extra retries for, port more of
      MacMTPBackend._raw_read_file's retry structure back in.

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
    _mtp_join,
    _mtp_segments,
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
except Exception:
    _pymtp = None

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


class LinuxMTPBackend(RCBackend):
    """
    Linux MTP backend using pymtp (libmtp). Implements the nine
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
            os.environ.get("PIDJIRC2KMZSYNC_MTP_TRANSFER_RECONNECT_IDLE_SECONDS", "15")
        )
        self._folder_cache: list | None = None
        self._folders_by_parent_cache: dict[int, list] | None = None
        self._file_cache: list | None = None
        self._files_by_parent_cache: dict[int, list] | None = None
        self._logged_global_file_index = False
        self._last_read_item_ids: dict[str, int] = {}

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
                            modified = datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")
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
                self.invalidate_transfer_caches()

                entry = self._find_file(folder_path, filename)
                if entry is None:
                    # One reconnect-and-retry cycle for a stale/incomplete
                    # cache. See module docstring re: simplified vs Mac original.
                    self._disconnect()
                    self._ensure_connected()
                    self.invalidate_transfer_caches()
                    entry = self._find_file(folder_path, filename)

                if entry is None:
                    return False, f"MTP file not found: {filename}"

                item_id = int(entry.item_id)
                if self._pull_to_path_fast(item_id, local_dest):
                    self._mark_transfer_activity()
                    return True, local_dest

                # One retry after a fresh reconnect.
                self._disconnect()
                self._ensure_connected()
                self.invalidate_transfer_caches()
                entry_retry = self._find_file(folder_path, filename)
                if entry_retry is None:
                    return False, f"MTP file not found after retry: {filename}"
                if self._pull_to_path_fast(int(entry_retry.item_id), local_dest):
                    self._mark_transfer_activity()
                    return True, local_dest

                return False, f"MTP pull failed for {filename} after retry"
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
                self.invalidate_transfer_caches()
                folder = self._resolve_folder(dest_folder)
                if folder is None:
                    return False, f"Destination folder not found: {dest_folder}"

                existing = self._find_file_in_folder(int(folder.folder_id), dest_filename)
                if existing is not None:
                    try:
                        self._quiet(self._mtp.delete_object, int(existing.item_id))
                    except Exception:
                        pass
                    self._file_cache = None
                    self._files_by_parent_cache = None
                    self._wait_for_file_delete(int(folder.folder_id), dest_filename)

                send_fn = self._mtp.mtp.LIBMTP_Send_File_From_File

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
                    self._disconnect()
                    self._ensure_connected()
                    self.invalidate_transfer_caches()
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
                        self._file_cache = None
                        self._files_by_parent_cache = None
                        self._wait_for_file_delete(int(folder_retry.folder_id), dest_filename)

                    retry_ret = _send_once(folder_retry)
                    if retry_ret != 0:
                        raise RuntimeError(
                            "LIBMTP_Send_File_From_File returned "
                            f"{first_ret} (initial) and {retry_ret} (after reconnect) "
                            f"for '{os.path.basename(local_source)}' to '{dest_filename}'"
                        )

                self._file_cache = None
                self._files_by_parent_cache = None
                self._last_read_item_ids = {}
                self._mark_transfer_activity()
            return True, f"Copied to {_mtp_join(dest_folder, dest_filename)}"
        except Exception as exc:
            return False, f"MTP write failed:\n{self._fmt_exc(exc)}"

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
                self._file_cache = None
                self._files_by_parent_cache = None
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
                self._file_cache = None
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
            with self._session():
                devices = self._quiet(self._mtp.detect_devices)
                if not devices:
                    self._disconnect()
                    return False
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
            return False, "Native MTP unavailable: install pymtp and libmtp-dev."
        root = self._root() or self.DEFAULT_ROOT
        ok, _ = self._raw_list_folder(root)
        if ok:
            return True, "MTP RC-2 waypoint path is reachable."
        return False, (
            "MTP RC-2 waypoint path is not reachable. "
            "Ensure RC-2 is connected via USB-C and unlocked."
        )

    def _raw_connection_mode(self) -> str:
        return "MTP"

    # ==================================================================
    # Session management
    # ==================================================================

    def close(self) -> None:
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
        self._connected = False
        self._folder_cache = None
        self._folders_by_parent_cache = None
        self._file_cache = None
        self._files_by_parent_cache = None
        self._logged_global_file_index = False
        self._last_read_item_ids = {}

    def _mark_transfer_activity(self) -> None:
        self._last_transfer_monotonic = time.monotonic()

    def _refresh_session_before_transfer(self, op_name: str) -> None:
        if self._last_transfer_monotonic is None:
            return
        idle_for = time.monotonic() - self._last_transfer_monotonic
        if idle_for < self._transfer_reconnect_idle_seconds:
            return
        self._disconnect()
        self._ensure_connected()

    def invalidate_transfer_caches(self) -> None:
        self._folder_cache = None
        self._folders_by_parent_cache = None
        self._file_cache = None
        self._files_by_parent_cache = None

    def invalidate_cache(self) -> None:
        super().invalidate_cache()
        self.invalidate_transfer_caches()
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
                raise

    @contextmanager
    def _session(self):
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
                list(folders_map.values()) if hasattr(folders_map, "values")
                else list(folders_map)
            )
        except Exception as exc:
            if self._is_disconnect_error(exc):
                self._disconnect()
                self._ensure_connected()
                folders_map = self._quiet(self._mtp.get_folder_list)
                values = (
                    list(folders_map.values()) if hasattr(folders_map, "values")
                    else list(folders_map)
                )
            else:
                with _suppress_stdio():
                    root_ptr = self._mtp.mtp.LIBMTP_Get_Folder_List(self._mtp.device)
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
            _LOG.info("Building global MTP file index (reason=%s).", reason)
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

    def _files_for_parent(self, parent_id: int) -> list:
        return list(self._files_by_parent().get(parent_id, []))

    def _child_entries(self, parent_id: int) -> tuple[list, list]:
        folders = list(self._folders_by_parent().get(parent_id, []))
        files = self._files_for_parent(parent_id)
        return folders, files

    def _resolve_folder(self, path: str):
        """Navigate to a folder by pipe-separated MTP path segments,
        starting from the device root (parent_id == 0)."""
        segments = self._path_segments(path)
        if not segments:
            return None

        by_parent = self._folders_by_parent()
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
                matches = [
                    f for f in current
                    if _decode(getattr(f, "name", "")).strip().lower() == wanted
                ]
                if not matches:
                    return self._resolve_folder_by_suffix(segments)
                current = matches
            else:
                current = next_level

        return current[0] if current else self._resolve_folder_by_suffix(segments)

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
            folder for folder in all_folders
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
        for entry in self._files_for_parent(folder_id):
            if _decode(getattr(entry, "filename", "")).strip().lower() == wanted:
                return entry
        return None

    def _delete_folder_recursive(self, folder_id: int) -> None:
        child_folders, child_files = self._child_entries(folder_id)
        for entry in child_files:
            self._quiet(self._mtp.delete_object, int(entry.item_id))
        for folder in child_folders:
            self._delete_folder_recursive(int(folder.folder_id))
        self._quiet(self._mtp.delete_object, folder_id)

    # ==================================================================
    # File pull helpers -- RC-2 blocks plain MTP GetObject, so partial
    # (chunked) reads via GetPartialObject are the primary path.
    # ==================================================================

    def _pull_to_path_via_partial(self, item_id: int, local_dest: str) -> bool:
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
                        _LOG.warning("GetPartialObject item_id=%s offset=%s returned %s", item_id, offset, ret)
                        return False
                    n = int(buf_len.value)
                    if n <= 0:
                        break
                    out_fh.write(ctypes.string_at(buf, n))
                    total_bytes += n
                    offset += n
                    if callable(free_mem) and bool(buf):
                        try:
                            self._quiet(free_mem, ctypes.cast(buf, ctypes.c_void_p))
                        except Exception:
                            pass
                    if n < chunk_size:
                        break
        except Exception as exc:
            _LOG.warning("GetPartialObject pull item_id=%s failed: %s", item_id, self._fmt_exc(exc))
            return False

        if total_bytes <= 0:
            return False
        return os.path.isfile(local_dest) and os.path.getsize(local_dest) > 0

    def _pull_to_path_fast(self, item_id: int, local_dest: str) -> bool:
        if self._pull_to_path_via_partial(item_id, local_dest):
            return True
        try:
            self._quiet(self._mtp.get_file_to_file, int(item_id), local_dest)
            return os.path.isfile(local_dest) and os.path.getsize(local_dest) > 0
        except Exception:
            return False

    # ==================================================================
    # Path helpers
    # ==================================================================

    @staticmethod
    def _path_segments(path: str) -> List[str]:
        raw = (path or "").strip()
        if raw.lower().startswith("mtp:"):
            raw = raw[4:].strip()
        raw = raw.replace("\\", "/")
        parts = []
        for chunk in raw.split("|"):
            for part in chunk.split("/"):
                s = part.strip()
                if s:
                    parts.append(s)
        for idx, segment in enumerate(parts):
            if segment.lower() == "android":
                if idx > 0:
                    parts = parts[idx:]
                break
        return parts

    # ==================================================================
    # Helpers
    # ==================================================================

    def _unavailable(self) -> str:
        return (
            "Native MTP unavailable. "
            "Install pymtp (pip install pymtp) and libmtp-dev "
            "(sudo apt install libmtp-dev)."
        )

    @staticmethod
    def _fmt_exc(exc: Exception) -> str:
        msg = str(exc).strip() or repr(exc)
        return f"{exc.__class__.__name__}: {msg}"
