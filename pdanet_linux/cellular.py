"""Read the phone's cellular signal from Android telephony telemetry.

PdaNet itself does not carry radio metadata: the TUN device only moves IP
packets.  When an authorized adb device is available we can still show the
phone's operator, radio technology, signal bars and RSRP in the dashboard
and tray.  The values are best effort and never affect the tunnel.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, replace

ADB_TIMEOUT = 12.0
_UNKNOWN = 2147483647

# TelephonyManager.NETWORK_TYPE_* values that are useful in the UI.  The
# dumpsys text already contains a name, but some Android builds report
# "Unknown" there while the numeric value is still valid.
_RADIO_TECHNOLOGY = {
    1: "GPRS",
    2: "EDGE",
    3: "UMTS",
    4: "CDMA",
    5: "EVDO",
    6: "EVDO",
    7: "1xRTT",
    8: "HSDPA",
    9: "HSUPA",
    10: "HSPA",
    11: "iDEN",
    12: "EVDO",
    13: "eHRPD",
    14: "LTE",
    15: "HSPA+",
    16: "GSM",
    17: "TD-SCDMA",
    18: "LTE",
    19: "LTE",
    20: "5G NR",
}

_SERVICE_RE = re.compile(r"mServiceState=\{([^\n]*)\}")
_SIGNAL_RE = re.compile(r"mSignalStrength=SignalStrength:\{([^\n]*)\}")


@dataclass(frozen=True)
class CellularSignal:
    """One subscription's cellular status."""

    operator: str = ""
    technology: str = ""
    level: int = 0
    rsrp: int | None = None
    rsrq: int | None = None
    snr: int | None = None
    data_state: str = ""

    @property
    def bars(self) -> str:
        return f"{max(0, min(4, int(self.level)))}/4"

    @property
    def compact(self) -> str:
        """Short form for the tray menu and tooltip."""
        parts = []
        if self.operator:
            parts.append(self.operator)
        if self.technology:
            parts.append(self.technology)
        parts.append(self.bars)
        return " ".join(parts)

    @property
    def detail(self) -> str:
        """Measurement line for the dashboard."""
        parts = []
        if self.rsrp is not None:
            parts.append(f"RSRP {self.rsrp} dBm")
        if self.rsrq is not None:
            parts.append(f"RSRQ {self.rsrq} dB")
        if self.snr is not None:
            parts.append(f"SNR {self.snr} dB")
        if not parts and self.data_state:
            parts.append(self.data_state)
        return " · ".join(parts)

    @property
    def description(self) -> str:
        return " · ".join(part for part in (self.compact, self.detail) if part)


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def _pair(pattern: str, text: str) -> tuple[int | None, str]:
    match = re.search(pattern, text)
    if not match:
        return None, ""
    return int(match.group(1)), match.group(2).strip()


def _int(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _valid_rsrp(value: int | None) -> int | None:
    if value is None or value >= 0 or value < -200:
        return None
    return value


def _valid_rsrq(value: int | None) -> int | None:
    if value is None or value >= 0 or value < -30:
        return None
    return value


def _valid_snr(value: int | None) -> int | None:
    if value is None or value < 0 or value > 40:
        return None
    return value


def _parse_service(line: str) -> dict[str, object]:
    operator = _first(r"mOperatorAlphaLong=([^,]*)", line)
    if not operator:
        operator = _first(r"mOperatorAlphaShort=([^,]*)", line)
    if not operator:
        operator = _first(r"mOperatorAlphaLongRaw=([^,]*)", line)

    _data_code, data_state = _pair(
        r"mDataRegState=(\d+)\(([^)]+)\)", line
    )
    if not data_state:
        data_state = (
            "IN_SERVICE" if "IN_SERVICE" in line else ""
        )

    tech_code, tech_name = _pair(
        r"getRilDataRadioTechnology=(\d+)\(([^)]+)\)", line
    )
    if not tech_name or tech_name.lower() == "unknown":
        tech_name = _RADIO_TECHNOLOGY.get(tech_code or -1, "")
    if tech_name == "NR":
        tech_name = "5G NR"

    return {
        "operator": operator,
        "technology": tech_name,
        "data_state": data_state,
    }


def _lte_segment(line: str) -> str:
    match = re.search(
        r"mLte=CellSignalStrengthLte:\s*(.*?)"
        r"(?=,mNr=|,mTdscdma=|,mWcdma=|,mGsm=|,mCdma=|,primary=|\})",
        line,
    )
    return match.group(1) if match else ""


def _nr_segment(line: str) -> str:
    match = re.search(r"mNr=CellSignalStrengthNr:\{(.*?)\}", line)
    return match.group(1) if match else ""


def _last_level(text: str, *, spaced: bool = False) -> int | None:
    pattern = r"\blevel\s*=\s*(\d+)" if spaced else r"\blevel=(\d+)"
    values = re.findall(pattern, text)
    if not values:
        return None
    try:
        return int(values[-1])
    except ValueError:
        return None


def _parse_signal(line: str) -> CellularSignal | None:
    lte = _lte_segment(line)
    if lte:
        return CellularSignal(
            level=max(0, min(4, _last_level(lte) or 0)),
            rsrp=_valid_rsrp(_int(r"\brsrp=(-?\d+)", lte)),
            rsrq=_valid_rsrq(_int(r"\brsrq=(-?\d+)", lte)),
            snr=_valid_snr(_int(r"\brssnr=(-?\d+)", lte)),
        )

    nr = _nr_segment(line)
    if nr:
        return CellularSignal(
            level=max(0, min(4, _last_level(nr, spaced=True) or 0)),
            rsrp=_valid_rsrp(
                _int(r"\bssRsrp\s*=\s*(-?\d+)", nr)
            ),
            rsrq=_valid_rsrq(
                _int(r"\bssRsrq\s*=\s*(-?\d+)", nr)
            ),
            snr=_valid_snr(
                _int(r"\bssSinr\s*=\s*(-?\d+)", nr)
            ),
        )

    # Older phones expose only a generic level field (GSM/CDMA).
    level = _last_level(line)
    if level is None:
        return None
    return CellularSignal(level=max(0, min(4, level)))


def _event_positions(text: str) -> list[tuple[int, int, str]]:
    events: list[tuple[int, int, str]] = []
    for match in _SERVICE_RE.finditer(text):
        events.append((match.start(), 0, match.group(1)))
    for match in _SIGNAL_RE.finditer(text):
        events.append((match.start(), 1, match.group(1)))
    events.sort()
    return events


def parse_telephony_dump(output: str) -> CellularSignal | None:
    """Parse ``dumpsys telephony.registry`` output.

    The dump normally has one service-state/signal pair per subscription.
    Pair them in order and prefer the subscription whose data state is
    ``IN_SERVICE``, then the strongest one.  This avoids showing the idle
    SIM when the active data SIM is the second subscription.
    """
    current: dict[str, object] = {}
    candidates: list[CellularSignal] = []
    for _position, kind, body in _event_positions(output):
        if kind == 0:
            current = _parse_service(body)
            continue
        signal = _parse_signal(body)
        if signal is None:
            continue
        candidates.append(
            replace(
                signal,
                operator=str(current.get("operator", "")),
                technology=str(current.get("technology", "")),
                data_state=str(current.get("data_state", "")),
            )
        )

    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.data_state.upper() == "IN_SERVICE",
            item.level,
            item.rsrp is not None,
        ),
    )


def read_signal(
    serial: str | None = None,
    *,
    adb: str | None = None,
) -> CellularSignal | None:
    """Return the phone's current cellular signal, or ``None``.

    A missing adb, an unauthorized device, or an OEM that does not expose
    telephony telemetry is not an error: the tunnel continues to work.
    """
    executable = adb or shutil.which("adb")
    if not executable:
        return None
    command = [executable]
    if serial:
        command += ["-s", serial]
    command += ["shell", "dumpsys", "telephony.registry"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=ADB_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return parse_telephony_dump(completed.stdout)
