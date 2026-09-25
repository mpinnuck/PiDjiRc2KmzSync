"""
battery_monitor_service.py
----------------------------
Reads the PowerBoost 1000C's LBO (Low Battery Output) pin via a Pi GPIO
input, so the web UI can show a low-battery warning.

Wiring (per the project's schematic) -- NOT a direct connection:
    LBO is actively pulled to BAT voltage (up to ~4.2V) when the battery
    is fine, not floating -- per Adafruit's own docs, it is "not suitable
    for direct connection to 3.3V logic GPIO." A direct wire from LBO to
    a Pi GPIO pin risks exceeding the Pi's 3.3V input tolerance and
    damaging the pin.

    The actual circuit, verified against Adafruit's own forum guidance
    for this exact pin:
        - A 100k ohm resistor from the Pi's 3.3V pin to the GPIO input
          (physical pin 7 / BCM GPIO4 -- see LBO_GPIO_PIN below).
        - A diode in series between that same GPIO node and LBO, anode
          toward the GPIO/resistor node, cathode toward LBO.
    When LBO is high (battery fine, near BAT voltage), the diode is
    reverse-biased and blocks it -- the GPIO pin only ever sees the safe
    3.3V from its own resistor. When LBO goes low (battery low), the
    diode conducts and lets LBO pull the GPIO node down, which reads as
    a low input here exactly as before. No software change is needed for
    this hardware fix -- pull_up=True below still works correctly
    alongside the external resistor (they simply combine in parallel).

Uses gpiozero (the Raspberry Pi Foundation's current recommended GPIO
library) rather than the older RPi.GPIO, since RPi.GPIO's direct
/dev/mem access doesn't work reliably on newer kernels' gpiochip
character-device model (relevant here since the Pi is running Debian
Trixie). gpiozero auto-selects whichever backend is actually available
(lgpio, RPi.GPIO, pigpio) at construction time.

Import of gpiozero itself is safe on any platform (pure Python) -- it's
only *constructing* a DigitalInputDevice that requires actual GPIO
hardware/backend, which is why that step (not the import) is wrapped in
try/except below. This means the service degrades gracefully rather than
crashing the app when running the dev server on a Mac, or on a Pi where
the lgpio backend package hasn't been installed yet.

Requires, on the Pi (not needed for Mac-side dev testing):
    sudo apt install python3-lgpio
    (or: pip install lgpio)
"""

from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

# Fixed by hardware wiring, not a runtime setting -- the PowerBoost 1000C's
# LBO pin is soldered/wired to this specific Pi GPIO (BCM numbering) and
# never changes without physically rewiring the board, so it's a plain
# constant rather than something exposed through ConfigManager or the UI.
# Physical pin 7 on the 40-pin header.
LBO_GPIO_PIN = 4

try:
    from gpiozero import DigitalInputDevice
except Exception:
    DigitalInputDevice = None  # type: ignore[assignment,misc]


class BatteryMonitorService:
    """Wraps a single GPIO input reading the PowerBoost's LBO pin."""

    def __init__(self, gpio_pin: int = LBO_GPIO_PIN) -> None:
        self._gpio_pin = gpio_pin
        self._device = None
        self._unavailable_reason: str | None = None
        self._connect()

    def _connect(self) -> None:
        if DigitalInputDevice is None:
            self._unavailable_reason = (
                "gpiozero is not installed -- battery monitoring disabled."
            )
            return
        try:
            # pull_up=True enables the Pi's internal pull-up in parallel
            # with the external 100k resistor described above; the pin
            # reads HIGH when isolated (diode reverse-biased, LBO high)
            # and LOW when the diode conducts (LBO pulled low).
            self._device = DigitalInputDevice(self._gpio_pin, pull_up=True)
        except Exception as exc:
            self._device = None
            self._unavailable_reason = (
                f"GPIO{self._gpio_pin} unavailable ({exc.__class__.__name__}: {exc}) "
                "-- battery monitoring disabled. Expected when not running on a Pi, "
                "or if a GPIO backend (e.g. lgpio) isn't installed."
            )
            _logger.warning(self._unavailable_reason)

    def is_available(self) -> bool:
        return self._device is not None

    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def is_low_battery(self) -> bool | None:
        """Returns True/False if the GPIO read succeeded, or None if the
        monitor isn't available (missing gpiozero, no GPIO backend, not
        running on a Pi, etc.) -- callers should treat None as "unknown",
        not as "battery is fine"."""
        if self._device is None:
            return None
        try:
            # is_active is True when pull_up=True and the pin reads LOW,
            # i.e. exactly when the PowerBoost is asserting "battery low".
            return bool(self._device.is_active)
        except Exception as exc:
            _logger.warning("Failed to read LBO GPIO%s: %s", self._gpio_pin, exc)
            return None

    def close(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None
