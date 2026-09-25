"""
config_manager.py
------------------
Server-side (Pi) configuration for the RC-2 connection.

Adapted from the desktop app's ConfigManager. Two differences:

1. No macOS/Windows filename branching -- this always runs on the Pi.
2. `pc_folder` is dropped entirely. In the desktop app it was a folder on
   the same machine running the GUI. In this relay, the browser (running
   on the iPad/iPhone/desktop, not the Pi) is the one with access to the
   iCloud/local KMZ files, so that configuration lives in the browser's
   own persistent storage (localStorage), not here. See
   client/static/app.js.

`rc2_folder` (e.g. "mtp:DJI RC 2|Internal shared storage|Android|data|dji.go.v5|files|waypoint")
and `dummy_slot_guid` (an existing RC-2 mission GUID used as the upload
target when no mission is explicitly selected) remain server-side config,
since they describe the RC-2 device state itself, not the browsing client.

The PowerBoost 1000C's LBO (Low Battery Output) GPIO pin is NOT here --
it's a fixed hardware wiring choice, not something that changes at
runtime, so it's a plain constant in
services/battery_monitor_service.py instead.
"""

import json
import logging
import os

_logger = logging.getLogger(__name__)

CONFIG_FILE = "kmz_sync_config.json"
CONFIG_FILE_ENV = "PIDJIRC2KMZSYNC_CONFIG_FILE"

_DEFAULTS = {
    "rc2_folder": "",
    "dummy_slot_guid": "",
    "rc2_refresh_retry_interval_seconds": 5,
}

_MIN_RETRY_SECONDS = 1
_MAX_RETRY_SECONDS = 300


def get_runtime_base_dir() -> str:
    return os.getcwd()


def get_config_filename() -> str:
    override = os.environ.get(CONFIG_FILE_ENV, "").strip()
    return override or CONFIG_FILE


def get_config_file_path() -> str:
    return os.path.join(get_runtime_base_dir(), get_config_filename())


class ConfigManager:
    def __init__(self):
        self._config: dict = dict(_DEFAULTS)
        self._load()

    @property
    def rc2_folder(self) -> str:
        return self._config.get("rc2_folder", "")

    @rc2_folder.setter
    def rc2_folder(self, value: str) -> None:
        self._config["rc2_folder"] = value

    @property
    def dummy_slot_guid(self) -> str:
        return str(self._config.get("dummy_slot_guid", "") or "")

    @dummy_slot_guid.setter
    def dummy_slot_guid(self, value: str) -> None:
        self._config["dummy_slot_guid"] = str(value or "").strip()

    @property
    def rc2_refresh_retry_interval_seconds(self) -> int:
        raw_value = self._config.get(
            "rc2_refresh_retry_interval_seconds",
            _DEFAULTS["rc2_refresh_retry_interval_seconds"],
        )
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            return _DEFAULTS["rc2_refresh_retry_interval_seconds"]
        return max(_MIN_RETRY_SECONDS, min(_MAX_RETRY_SECONDS, parsed))

    @rc2_refresh_retry_interval_seconds.setter
    def rc2_refresh_retry_interval_seconds(self, value: int) -> None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = _DEFAULTS["rc2_refresh_retry_interval_seconds"]
        self._config["rc2_refresh_retry_interval_seconds"] = max(
            _MIN_RETRY_SECONDS, min(_MAX_RETRY_SECONDS, parsed)
        )

    def save(self) -> None:
        config_path = get_config_file_path()
        try:
            with open(config_path, "w") as f:
                json.dump(self._config, f, indent=4)
        except OSError as e:
            _logger.warning("Failed to save config: %s", e)

    def _load(self) -> None:
        config_path = get_config_file_path()
        if not os.path.exists(config_path):
            self.save()
            return
        try:
            with open(config_path, "r") as f:
                loaded = json.load(f)
            self._config.update(loaded)
        except (OSError, json.JSONDecodeError) as e:
            _logger.warning("Failed to load config: %s", e)
