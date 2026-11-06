"""USB plug-in detection for the desktop "Auto Connect" option.

PdaNet's Windows tray client connects automatically when a phone is
plugged in.  The GUI polls ``adb devices`` in a background thread; this
small state machine decides when a *new* authorized device appeared so a
manual disconnect does not immediately reconnect in a loop.
"""

from __future__ import annotations


class UsbAutoConnect:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = bool(enabled)
        self._seen: set[str] = set()
        self._primed = False

    def set_enabled(self, enabled: bool) -> None:
        """Enable/disable without treating already-present devices as new."""
        self.enabled = bool(enabled)

    def update(self, ready_serials: list[str] | set[str]) -> str | None:
        """Feed the current set of authorized serials.

        Returns the serial of a newly appeared device that should trigger a
        connection, or ``None``.  The first call only primes the state.
        """
        ready = {str(serial) for serial in ready_serials if serial}
        if not self._primed:
            self._seen = ready
            self._primed = True
            return None
        new = ready - self._seen
        self._seen = ready
        if not self.enabled or not new:
            return None
        # Stable order makes tests deterministic when two phones appear at
        # the same time (the GUI only ever connects to one).
        return sorted(new)[0]

    def forget(self, serial: str | None = None) -> None:
        """Forget a device after a failed attempt, allowing a retry later."""
        if serial is None:
            self._seen.clear()
        else:
            self._seen.discard(serial)
