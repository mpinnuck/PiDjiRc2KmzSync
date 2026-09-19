"""
backend_factory.py
------------------
Factory that constructs the correct RCBackend for the Pi relay.

MTP is the only connection path this relay uses.
"""

from __future__ import annotations

import platform

from backends.mtp.mac_mtp_backend import MacMTPBackend
from backends.mtp.pi_mtp_backend import PiMTPBackend
from backends.rc_backend import RCBackend
from backends.unavailable_rc_backend import UnavailableRCBackend
from config.config_manager import ConfigManager


class BackendFactory:
    """
    Constructs the correct RCBackend for the given RC-2 path.

    Usage:
        rc_backend = BackendFactory.create_rc(config.rc2_folder, config)
    """

    @staticmethod
    def create_rc(path: str, config: ConfigManager) -> RCBackend:
        cleaned = (path or "").strip()
        scheme = BackendFactory.path_scheme(cleaned)

        if scheme == "mtp":
            backend_class = (
                MacMTPBackend if platform.system() == "Darwin" else PiMTPBackend
            )
            return backend_class(config)

        if not cleaned:
            return UnavailableRCBackend(
                "No RC-2 path configured yet. Set an 'mtp:' path to continue, "
                f"e.g. {PiMTPBackend.DEFAULT_ROOT}"
            )

        return UnavailableRCBackend(
            f"RC-2 path must use the 'mtp:' prefix. Got: {cleaned!r}"
        )

    @staticmethod
    def path_scheme(path: str) -> str:
        cleaned = (path or "").strip()
        if not cleaned:
            return ""
        sep = cleaned.find(":")
        if sep <= 0:
            return ""
        return cleaned[:sep].strip().lower()
