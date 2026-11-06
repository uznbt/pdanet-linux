"""Bluetooth RFCOMM transport helpers for PdaNet.

PdaNet+ exposes its Bluetooth server as an RFCOMM service named
``PdaNet Server`` with the UUID ``00002A0B-0000-1000-8000-00805F9B34FB``.
The relevant Android code is ``com.pdanet.a`` in PdaNet+ 5.23::

    adapter.listenUsingRfcommWithServiceRecord(
        "PdaNet Server",
        UUID.fromString("00002A0B-0000-1000-8000-00805F9B34FB"))

The PdaNet framing is unchanged over RFCOMM; only the init transport flag
changes to ``4`` (the phone maps ``h & 4`` to Bluetooth).

This module deliberately uses the standard ``bluetoothctl`` command for
pairing/connection state and the Linux AF_BLUETOOTH socket API for the
RFCOMM data path, so it does not require PyBluez.
"""

from __future__ import annotations

import errno
import re
import shutil
import socket
import struct
import subprocess
import uuid as uuidlib
from dataclasses import dataclass
from typing import Callable

PDANET_UUID = uuidlib.UUID("00002A0B-0000-1000-8000-00805F9B34FB")
PDANET_SERVICE_NAME = "PdaNet Server"
SDP_PSM = 1
SDP_RFCOMM_UUID = 0x0003

_MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

LogFn = Callable[[str], None]


class BluetoothError(RuntimeError):
    pass


@dataclass(frozen=True)
class BluetoothDevice:
    address: str
    name: str = ""
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    uuids: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        if self.name:
            return f"{self.name} ({self.address})"
        return self.address

    @property
    def has_pdanet_service(self) -> bool:
        target = str(PDANET_UUID).lower()
        return any(u.lower() == target for u in self.uuids)


def describe_device(device: BluetoothDevice) -> str:
    """One-line diagnostic description used by logs and error messages."""
    state = [
        "paired" if device.paired else "unpaired",
        "trusted" if device.trusted else "untrusted",
        "connected" if device.connected else "not connected",
    ]
    if device.has_pdanet_service:
        state.append("PdaNet service advertised")
    else:
        state.append("no PdaNet service in SDP")
    return f"{device.label} [{', '.join(state)}]"


def _clean(text: str | None) -> str:
    return _ANSI_RE.sub("", text or "")


def bluetoothctl_path() -> str:
    found = shutil.which("bluetoothctl")
    if not found:
        raise BluetoothError(
            "bluetoothctl was not found. Install BlueZ (bluez/bluez-utils) "
            "and start the bluetooth service."
        )
    return found


def _run_bluetoothctl(
    args: list[str], *, timeout: float = 20.0
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [bluetoothctl_path(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BluetoothError(f"could not run bluetoothctl {' '.join(args)}: {exc}") from exc


def _output(proc: subprocess.CompletedProcess[str]) -> str:
    return _clean((proc.stdout or "") + "\n" + (proc.stderr or ""))


def _normalise_address(address: str) -> str:
    if not _MAC_RE.match(address.strip()):
        raise BluetoothError(f"invalid Bluetooth address: {address!r}")
    return address.strip().upper()


def parse_devices(text: str) -> list[BluetoothDevice]:
    """Parse ``bluetoothctl devices`` / ``devices Paired`` output."""
    devices: list[BluetoothDevice] = []
    for line in _clean(text).splitlines():
        match = re.match(
            r"^\s*Device\s+([0-9A-Fa-f:]{17})\s*(.*?)\s*$", line
        )
        if not match:
            continue
        address = match.group(1).upper()
        name = match.group(2).strip()
        devices.append(BluetoothDevice(address=address, name=name, paired=True))
    return devices


def parse_info(text: str, address: str = "") -> BluetoothDevice:
    """Parse ``bluetoothctl info <address>`` output."""
    name = ""
    paired = trusted = connected = False
    uuids: list[str] = []
    found_address = address.upper()
    for line in _clean(text).splitlines():
        stripped = line.strip()
        match = re.match(r"^Device\s+([0-9A-Fa-f:]{17})", stripped)
        if match:
            found_address = match.group(1).upper()
            continue
        if stripped.startswith("Name:"):
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Paired:"):
            paired = stripped.split(":", 1)[1].strip().lower() == "yes"
        elif stripped.startswith("Trusted:"):
            trusted = stripped.split(":", 1)[1].strip().lower() == "yes"
        elif stripped.startswith("Connected:"):
            connected = stripped.split(":", 1)[1].strip().lower() == "yes"
        elif stripped.startswith("UUID:"):
            match = re.search(
                r"\(([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\)",
                stripped,
            )
            if match:
                uuids.append(match.group(1).lower())
    return BluetoothDevice(
        address=found_address,
        name=name,
        paired=paired,
        trusted=trusted,
        connected=connected,
        uuids=tuple(uuids),
    )


def list_devices(*, paired_only: bool = False) -> list[BluetoothDevice]:
    args = ["devices"]
    if paired_only:
        args.append("Paired")
    proc = _run_bluetoothctl(args, timeout=20.0)
    text = _output(proc)
    devices = parse_devices(text)
    if paired_only:
        return devices
    # Non-paired entries are useful for diagnostics; mark them accurately.
    return devices


def adapter_state() -> dict[str, str]:
    """Return the default controller's ``bluetoothctl show`` fields."""
    proc = _run_bluetoothctl(["show"], timeout=15.0)
    text = _output(proc)
    state: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Controller "):
            parts = line.split(None, 1)
            if len(parts) == 2:
                state["Controller"] = parts[1].split()[0].strip()
        for key in ("Powered", "Discoverable", "Pairable", "Discovering"):
            if line.startswith(key + ":"):
                state[key] = line.split(":", 1)[1].strip()
    return state


def device_info(address: str) -> BluetoothDevice:
    address = _normalise_address(address)
    proc = _run_bluetoothctl(["info", address], timeout=20.0)
    text = _output(proc)
    if "Device " not in text:
        detail = text.strip() or "no output"
        raise BluetoothError(f"Bluetooth device {address} was not found: {detail}")
    info = parse_info(text, address)
    if not info.address:
        info = BluetoothDevice(address=address, name=info.name,
                               paired=info.paired, trusted=info.trusted,
                               connected=info.connected, uuids=info.uuids)
    return info


def select_device(selector: str | None = None) -> BluetoothDevice:
    """Resolve a MAC address or a case-insensitive name substring."""
    if selector:
        selector = selector.strip()
        if _MAC_RE.match(selector):
            return device_info(selector)
        candidates = [
            d
            for d in list_devices()
            if selector.lower() in d.name.lower()
            or selector.lower() in d.address.lower()
        ]
        if not candidates:
            raise BluetoothError(f"no Bluetooth device matches {selector!r}")
        if len(candidates) > 1:
            names = ", ".join(d.label for d in candidates)
            raise BluetoothError(
                f"multiple Bluetooth devices match {selector!r}: {names}; "
                "use the MAC address"
            )
        return device_info(candidates[0].address)

    devices = list_devices(paired_only=True)
    if not devices:
        raise BluetoothError(
            "no paired Bluetooth device found. Pair the phone first with "
            "bluetoothctl, then retry."
        )
    if len(devices) > 1:
        names = ", ".join(d.label for d in devices)
        raise BluetoothError(
            f"multiple paired Bluetooth devices ({names}); choose one with "
            "--bt-device ADDRESS"
        )
    return device_info(devices[0].address)


def ensure_connected(
    device: BluetoothDevice, *, timeout: float = 25.0, log: LogFn | None = None
) -> BluetoothDevice:
    log = log or (lambda _m: None)
    if not device.paired:
        raise BluetoothError(
            f"{device.label} is not paired. Pair it first (for example with "
            f"'bluetoothctl pair {device.address}' and 'bluetoothctl trust "
            f"{device.address}')."
        )
    if device.connected:
        return device

    log(f"connecting Bluetooth device {device.label}")
    proc = _run_bluetoothctl(["connect", device.address], timeout=timeout)
    output = _output(proc).strip()
    lowered = output.lower()
    if "failed to connect" in lowered or "not available" in lowered:
        if "key" in lowered or "auth" in lowered:
            hint = (
                " The Bluetooth link key is missing or invalid. Remove the "
                "pairing on both the phone and the PC, then pair again."
            )
        else:
            hint = ""
        raise BluetoothError(f"could not connect to {device.label}: {output}{hint}")

    info = device_info(device.address)
    if not info.connected:
        raise BluetoothError(
            f"Bluetooth link to {device.label} did not come up: {output or 'no output'}"
        )
    return info


def pair_device(
    device: BluetoothDevice | str,
    *,
    timeout: float = 45.0,
    log: LogFn | None = None,
) -> BluetoothDevice:
    """Pair and trust *device* through BlueZ.

    The phone still shows the normal pairing prompt; ``NoInputNoOutput``
    only tells BlueZ how to answer the PC side of a Just Works pairing.  If
    the phone requires a passkey that cannot be confirmed automatically, the
    error explains how to pair manually with ``bluetoothctl``.
    """
    log = log or (lambda _m: None)
    address = (
        device.address if isinstance(device, BluetoothDevice)
        else _normalise_address(device)
    )
    info = device_info(address)
    if not info.paired:
        log(f"pairing Bluetooth device {info.label}")
        proc = _run_bluetoothctl(
            [
                "--agent",
                "NoInputNoOutput",
                "--timeout",
                str(max(10, int(timeout))),
                "pair",
                address,
            ],
            timeout=timeout + 15.0,
        )
        output = _output(proc).strip()
        info = device_info(address)
        if not info.paired:
            detail = output or "no output from bluetoothctl"
            raise BluetoothError(
                f"could not pair {info.label}: {detail}. Pair it manually "
                f"with 'bluetoothctl' (pair {address}, then trust {address}) "
                "and try again."
            )
        log(f"paired Bluetooth device {info.label}")
    if not info.trusted:
        log(f"trusting Bluetooth device {info.label}")
        _run_bluetoothctl(["trust", address], timeout=20.0)
        info = device_info(address)
    return info


def ensure_paired(
    device: BluetoothDevice, *, timeout: float = 45.0, log: LogFn | None = None
) -> BluetoothDevice:
    """Return a paired device, pairing it first when necessary."""
    if device.paired:
        return device
    return pair_device(device, timeout=timeout, log=log)


_SCAN_DEVICE_RE = re.compile(r"\bDevice\s+([0-9A-Fa-f:]{17})\b")


def scan_devices(
    duration: float = 8.0,
    *,
    log: LogFn | None = None,
) -> list[BluetoothDevice]:
    """Scan for nearby Bluetooth devices and return everything BlueZ sees.

    The scan is best effort: known devices are still returned if the
    controller or the scan itself fails.  Paired devices are sorted first,
    followed by named devices, then by address.
    """
    log = log or (lambda _m: None)
    duration = max(1.0, min(float(duration), 30.0))
    log(f"scanning for Bluetooth devices for {duration:.0f}s")
    try:
        adapter = adapter_state()
    except BluetoothError as exc:
        log(f"could not read Bluetooth adapter state: {exc}")
    else:
        if adapter:
            controller = adapter.get("Controller", "default controller")
            flags = ", ".join(
                f"{key}={adapter[key]}"
                for key in ("Powered", "Discoverable", "Pairable")
                if key in adapter
            )
            log(
                f"Bluetooth adapter {controller}"
                + (f" ({flags})" if flags else "")
            )
            if adapter.get("Powered", "yes").lower() == "no":
                log("Bluetooth adapter is powered off; turn it on first")
        else:
            log("no Bluetooth adapter reported by BlueZ")
    try:
        proc = _run_bluetoothctl(
            ["--timeout", str(int(round(duration))), "scan", "on"],
            timeout=duration + 15.0,
        )
        scan_output = _output(proc)
        if proc.returncode != 0:
            log(f"bluetoothctl scan exited with status {proc.returncode}")
    except BluetoothError as exc:
        log(f"Bluetooth scan failed: {exc}")
        scan_output = ""
        proc = None

    # Surface the errors BlueZ prints during discovery; otherwise a scan
    # that found nothing looks exactly like a phone that is not there.
    for line in scan_output.splitlines():
        stripped = line.strip()
        if stripped and re.search(
            r"failed|error|not ready|not available|no default controller|blocked",
            stripped,
            re.IGNORECASE,
        ):
            log(f"bluetoothctl: {stripped}")

    advertised: set[str] = set()
    for line in scan_output.splitlines():
        match = _SCAN_DEVICE_RE.search(line)
        if match:
            advertised.add(match.group(1).upper())
    addresses: set[str] = set(advertised)
    log(f"discovery returned {len(advertised)} address(es)")
    if proc is not None and not advertised:
        log(
            "no advertisement seen: the phone must be discoverable and "
            "Bluetooth tethering must be enabled in PdaNet+"
        )

    try:
        known = list_devices(paired_only=False)
    except BluetoothError as exc:
        log(f"could not list devices known to BlueZ: {exc}")
        known = []
    log(
        f"BlueZ knows {len(known)} device(s); pairing state is read per device"
    )
    addresses.update(d.address for d in known)
    if not advertised and known:
        log(
            "using cached BlueZ devices; a full re-pair may be needed if "
            "the phone was forgotten on either side"
        )

    devices: list[BluetoothDevice] = []
    for address in sorted(addresses):
        try:
            info = device_info(address)
        except BluetoothError as exc:
            log(f"skipping {address}: {exc}")
            continue
        devices.append(info)

    devices.sort(
        key=lambda d: (
            0 if d.paired else 1,
            0 if d.name else 1,
            d.name.lower(),
            d.address,
        )
    )
    for device in devices:
        log(f"Bluetooth device: {describe_device(device)}")
    paired = sum(1 for d in devices if d.paired)
    log(
        f"found {len(devices)} Bluetooth device(s): {paired} paired, "
        f"{len(devices) - paired} unpaired"
    )
    if devices and not any(d.has_pdanet_service for d in devices):
        log(
            "no device advertises the PdaNet Server service; enable "
            "Bluetooth tethering in PdaNet+ before connecting"
        )
    return devices


# ---------------------------------------------------------------------------
# SDP lookup
# ---------------------------------------------------------------------------

def _de_sequence(payload: bytes) -> bytes:
    size = len(payload)
    if size < 0x100:
        return b"\x35" + bytes([size]) + payload
    if size < 0x10000:
        return b"\x36" + struct.pack(">H", size) + payload
    return b"\x37" + struct.pack(">I", size) + payload


def _de_uuid(uuid: uuidlib.UUID) -> bytes:
    return b"\x1c" + uuid.bytes


def _de_uuid16(value: int) -> bytes:
    return b"\x19" + struct.pack(">H", value)


def _sdp_request(pattern: bytes, transaction: int = 1) -> bytes:
    """Build an SDP Service Search Attribute Request."""
    # ProtocolDescriptorList (0x0004) and ServiceName (0x0100).
    attr_ids = b"\x09\x00\x04\x09\x01\x00"
    params = (
        _de_sequence(pattern)
        + b"\xff\xff"
        + _de_sequence(attr_ids)
    )
    return b"\x06" + struct.pack(">HH", transaction, len(params)) + params


def _parse_de(data: bytes, offset: int = 0) -> tuple[object, int]:
    """Parse one SDP data element and return (value, next_offset)."""
    if offset >= len(data):
        raise ValueError("truncated SDP data element")
    descriptor = data[offset]
    offset += 1
    type_index = (descriptor >> 3) & 0x1F
    size_index = descriptor & 0x07
    if size_index <= 4:
        size = 1 << size_index
    elif size_index == 5:
        if offset >= len(data):
            raise ValueError("truncated SDP length")
        size = data[offset]
        offset += 1
    elif size_index == 6:
        if offset + 2 > len(data):
            raise ValueError("truncated SDP length")
        size = struct.unpack_from(">H", data, offset)[0]
        offset += 2
    else:
        if offset + 4 > len(data):
            raise ValueError("truncated SDP length")
        size = struct.unpack_from(">I", data, offset)[0]
        offset += 4

    if offset + size > len(data):
        raise ValueError("truncated SDP value")
    raw = data[offset : offset + size]
    offset += size

    if type_index in (6, 7):  # sequence / alternative
        values: list[object] = []
        position = 0
        while position < len(raw):
            value, position = _parse_de(raw, position)
            values.append(value)
        return values, offset
    if type_index == 3:  # UUID
        return raw, offset
    if type_index in (1, 2):  # unsigned / signed integer
        return int.from_bytes(raw, "big", signed=(type_index == 2)), offset
    if type_index == 4:  # text
        return raw.decode("utf-8", "replace"), offset
    if type_index == 5:  # boolean
        return bool(raw), offset
    return raw, offset


def _uuid16(value: object) -> int | None:
    if not isinstance(value, (bytes, bytearray)):
        return None
    raw = bytes(value)
    if len(raw) == 2:
        return int.from_bytes(raw, "big")
    if len(raw) == 4:
        return int.from_bytes(raw, "big") & 0xFFFF
    if len(raw) == 16:
        base = bytes.fromhex("0000000000001000800000805f9b34fb")
        if raw[4:] == base[4:]:
            return int.from_bytes(raw[2:4], "big")
    return None


def _find_rfcomm_channel(value: object) -> int | None:
    """Find the RFCOMM channel in a ProtocolDescriptorList value."""
    if not isinstance(value, list):
        return None
    for protocol in value:
        if not isinstance(protocol, list) or not protocol:
            continue
        if _uuid16(protocol[0]) != SDP_RFCOMM_UUID:
            continue
        for parameter in protocol[1:]:
            if isinstance(parameter, int) and 1 <= parameter <= 30:
                return parameter
    return None


def parse_sdp_response(data: bytes) -> int | None:
    """Return the RFCOMM channel from an SDP Service Search Attribute Response."""
    if len(data) < 9 or data[0] != 0x07:
        return None
    _transaction, parameter_length = struct.unpack_from(">HH", data, 1)
    if parameter_length < 4:
        return None
    error_code, attribute_bytes = struct.unpack_from(">HH", data, 5)
    if error_code != 0:
        return None
    payload = data[9 : 9 + attribute_bytes]
    if not payload:
        return None
    try:
        records, _ = _parse_de(payload, 0)
    except ValueError:
        return None
    if not isinstance(records, list):
        return None
    for record in records:
        if not isinstance(record, list):
            continue
        for attribute in record:
            if not isinstance(attribute, list) or len(attribute) < 2:
                continue
            if attribute[0] == 0x0004:
                channel = _find_rfcomm_channel(attribute[1])
                if channel is not None:
                    return channel
    return None


def sdp_rfcomm_channel(address: str, *, timeout: float = 5.0) -> int | None:
    """Query the phone's SDP server for the PdaNet RFCOMM channel."""
    address = _normalise_address(address)
    if not hasattr(socket, "AF_BLUETOOTH") or not hasattr(socket, "BTPROTO_L2CAP"):
        return None

    patterns = [
        _de_uuid(PDANET_UUID),  # 128-bit form used by Android
        _de_uuid16(PDANET_UUID.time_low & 0xFFFF),  # 0x2A0B alias
    ]
    for pattern in patterns:
        sock = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP
        )
        try:
            sock.settimeout(timeout)
            sock.connect((address, SDP_PSM))
            sock.sendall(_sdp_request(pattern))
            response = sock.recv(65535)
        except OSError:
            continue
        finally:
            sock.close()
        channel = parse_sdp_response(response)
        if channel is not None:
            return channel
    return None


def sdptool_rfcomm_channel(
    address: str, *, timeout: float = 8.0
) -> int | None:
    """Fallback SDP lookup through the optional ``sdptool`` utility."""
    tool = shutil.which("sdptool")
    if not tool:
        return None
    address = _normalise_address(address)
    try:
        proc = subprocess.run(
            [tool, "browse", address], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = _clean(proc.stdout)
    records = re.split(r"\n(?=Service Name:)", text)
    for record in records:
        lowered = record.lower()
        if "pdanet" not in lowered and "2a0b" not in lowered:
            continue
        match = re.search(r"Channel:\s*(\d+)", record, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _open_socket(address: str, channel: int, timeout: float) -> socket.socket:
    sock = socket.socket(
        socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
    )
    try:
        sock.settimeout(timeout)
        sock.connect((address, channel))
        sock.settimeout(None)
        return sock
    except Exception:
        sock.close()
        raise


def open_rfcomm(
    address: str,
    *,
    channel: int = 0,
    timeout: float = 15.0,
    log: LogFn | None = None,
) -> tuple[socket.socket, int]:
    """Open the PdaNet RFCOMM stream.

    Returns ``(socket, channel)``.  If *channel* is zero, SDP is used first;
    when that fails, RFCOMM channels 1..30 are probed as a last resort.
    """
    log = log or (lambda _m: None)
    address = _normalise_address(address)

    if not hasattr(socket, "AF_BLUETOOTH"):
        raise BluetoothError(
            "this Python build has no AF_BLUETOOTH support; use the system "
            "python3 package"
        )

    if channel:
        log(f"using RFCOMM channel {channel} on {address}")
        return _open_socket(address, channel, timeout), channel

    found = sdp_rfcomm_channel(address)
    if found is None:
        found = sdptool_rfcomm_channel(address)
    if found is not None:
        log(f"SDP found PdaNet RFCOMM channel {found} on {address}")
        try:
            return _open_socket(address, found, timeout), found
        except OSError as exc:
            log(f"RFCOMM channel {found} failed ({exc}); probing other channels")

    log("SDP lookup did not find PdaNet; probing RFCOMM channels 1-30")
    last_error: Exception | None = None
    for candidate in range(1, 31):
        try:
            sock = _open_socket(address, candidate, min(timeout, 2.0))
        except OSError as exc:
            last_error = exc
            if exc.errno in (errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH):
                # The phone is not answering at all.  Trying 30 more channels
                # would only make the wait worse.
                if candidate >= 3:
                    break
            continue
        log(f"connected to RFCOMM channel {candidate}")
        return sock, candidate

    raise BluetoothError(
        f"could not open an RFCOMM connection to {address}"
        + (f": {last_error}" if last_error else "")
        + ". Check that the phone is paired, PdaNet Bluetooth mode is on, "
          "and the phone is connected."
    )


def status() -> dict[str, object]:
    """Best-effort Bluetooth environment information for ``doctor``."""
    info: dict[str, object] = {
        "bluetoothctl": shutil.which("bluetoothctl"),
        "sdptool": shutil.which("sdptool"),
        "af_bluetooth": hasattr(socket, "AF_BLUETOOTH"),
        "paired": 0,
        "error": "",
    }
    if not info["bluetoothctl"]:
        info["error"] = "bluetoothctl not installed"
        return info
    try:
        devices = list_devices(paired_only=True)
        info["paired"] = len(devices)
        info["devices"] = [d.label for d in devices]
    except BluetoothError as exc:
        info["error"] = str(exc)
    return info
