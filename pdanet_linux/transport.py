"""Transports used to reach the PdaNet server on the phone.

USB is implemented with ``adb forward``.  PdaNet listens either on TCP
port 8739 (normal USB mode) or on the Android abstract Unix socket
``pdanet`` (when the phone-side ``useUnixSocket`` setting is enabled).  We
try both, because a forwarding rule can be created for a socket that does
not exist and only fails when the host connects.

Wi-Fi mode connects directly to the phone on TCP 8740.  The phone also
answers a 64 byte UDP discovery datagram on port 8000; the source address
of that reply is the phone's IP, which is handy when the hotspot uses an
unusual subnet.
"""

from __future__ import annotations

import select
import shutil
import socket
import struct
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from . import bt

ADB_USB_PORT = 8739
WIFI_PORT = 8740
DISCOVERY_PORT = 8000
DISCOVERY_MARKER = 5014

LogFn = Callable[[str], None]


class TransportError(RuntimeError):
    pass


@dataclass
class Endpoint:
    sock: socket.socket
    description: str
    #: Host that must bypass the tunnel (Wi-Fi phone, None for USB/ADB).
    bypass_host: str | None = None
    _cleanup: Callable[[], None] = field(default=lambda: None, repr=False)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self._cleanup()
        except Exception:
            pass


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    device: str = ""
    transport_id: str = ""

    @property
    def label(self) -> str:
        name = self.model.replace("_", " ") if self.model else self.device
        if name and name != self.serial:
            return f"{name} ({self.serial})"
        return self.serial


def adb_path() -> str:
    found = shutil.which("adb")
    if not found:
        raise TransportError(
            "adb was not found. Install it with e.g. 'sudo apt install "
            "android-tools-adb' and enable USB debugging on the phone."
        )
    return found


def _run_adb(args: list[str], *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    adb = adb_path()
    try:
        return subprocess.run(
            [adb, *args], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TransportError(f"could not run adb {' '.join(args)}: {exc}") from exc


def list_devices() -> list[AdbDevice]:
    """Return devices reported by ``adb devices -l``."""
    proc = _run_adb(["devices", "-l"])
    if proc.returncode != 0:
        raise TransportError(f"adb devices failed: {proc.stderr.strip()}")
    devices: list[AdbDevice] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        props: dict[str, str] = {}
        for item in parts[2:]:
            if ":" in item:
                key, value = item.split(":", 1)
                props[key] = value
        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                model=props.get("model", ""),
                product=props.get("product", ""),
                device=props.get("device", ""),
                transport_id=props.get("transport_id", ""),
            )
        )
    return devices


def choose_device(serial: str | None = None) -> AdbDevice:
    devices = list_devices()
    if serial:
        for device in devices:
            if device.serial == serial:
                if device.state != "device":
                    raise TransportError(
                        f"device {serial} is in state {device.state!r}. "
                        "Unlock the phone and approve the USB debugging prompt."
                    )
                return device
        raise TransportError(f"adb device {serial!r} not found")
    ready = [d for d in devices if d.state == "device"]
    if not ready:
        if devices:
            states = ", ".join(f"{d.serial}={d.state}" for d in devices)
            raise TransportError(
                f"no usable adb device ({states}). Unlock the phone and approve "
                "the USB debugging prompt."
            )
        raise TransportError(
            "no adb device found. Connect the phone by USB, enable Developer "
            "options -> USB debugging, and approve the prompt."
        )
    if len(ready) > 1:
        names = ", ".join(d.serial for d in ready)
        raise TransportError(
            f"multiple adb devices are connected ({names}); choose one with --serial"
        )
    return ready[0]


def _free_tcp_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


@dataclass
class _Forward:
    serial: str
    local_port: int
    remote: str
    removed: bool = False

    def remove(self) -> None:
        if self.removed:
            return
        self.removed = True
        _run_adb(
            ["-s", self.serial, "forward", "--remove", f"tcp:{self.local_port}"],
            timeout=10,
        )


def _connect_host(port: int, timeout: float) -> socket.socket:
    last: Exception | None = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=1.5)
            sock.settimeout(None)
            return sock
        except OSError as exc:
            last = exc
            time.sleep(0.1)
    raise TransportError(f"adb forward is up but the PdaNet server did not answer: {last}")


def connect_usb(
    *,
    serial: str | None = None,
    local_port: int = 0,
    log: LogFn | None = None,
) -> Endpoint:
    """Set up ``adb forward`` and connect to the PdaNet USB server."""
    log = log or (lambda _m: None)
    device = choose_device(serial)
    port = local_port or _free_tcp_port()
    forward: _Forward | None = None
    attempts: list[str] = []

    # The normal server listens on TCP 8739.  With useUnixSocket=1 it
    # listens on abstract socket "pdanet" instead.  ADB can forward to
    # either; we have to try both because the forward itself succeeds even
    # when the remote socket is absent.
    for remote in (f"tcp:{ADB_USB_PORT}", "localabstract:pdanet"):
        forward = _Forward(device.serial, port, remote)
        _run_adb(
            ["-s", device.serial, "forward", "--remove", f"tcp:{port}"],
            timeout=10,
        )
        proc = _run_adb(
            ["-s", device.serial, "forward", f"tcp:{port}", remote], timeout=15
        )
        if proc.returncode != 0:
            attempts.append(f"{remote}: {proc.stderr.strip() or proc.stdout.strip()}")
            continue
        log(f"adb forward tcp:{port} -> {remote} ({device.serial})")
        try:
            sock = _connect_host(port, timeout=2.0)
            return Endpoint(
                sock=sock,
                description=f"USB/ADB {device.label} -> {remote}",
                _cleanup=forward.remove,
            )
        except TransportError as exc:
            attempts.append(f"{remote}: {exc}")
            try:
                forward.remove()
            except Exception:
                pass

    raise TransportError(
        "could not reach the PdaNet USB server. "
        "Make sure PdaNet is running on the phone and USB tethering mode is "
        "selected. Tried: " + "; ".join(attempts)
    )


def connect_wifi(host: str, port: int = WIFI_PORT, *, timeout: float = 6.0) -> Endpoint:
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise TransportError(f"cannot connect to PdaNet at {host}:{port}: {exc}") from exc
    sock.settimeout(None)
    return Endpoint(
        sock=sock,
        description=f"Wi-Fi {host}:{port}",
        bypass_host=host,
    )


def list_bluetooth_devices(*, paired_only: bool = True) -> list[bt.BluetoothDevice]:
    """Return Bluetooth devices known to BlueZ."""
    return bt.list_devices(paired_only=paired_only)


def scan_bluetooth_devices(
    duration: float = 8.0, *, log: LogFn | None = None
) -> list[bt.BluetoothDevice]:
    """Return paired and nearby Bluetooth devices."""
    return bt.scan_devices(duration=duration, log=log)


def connect_bluetooth(
    device: str | None = None,
    channel: int = 0,
    *,
    timeout: float = 20.0,
    log: LogFn | None = None,
) -> Endpoint:
    """Connect to the PdaNet Bluetooth RFCOMM service.

    *device* may be a Bluetooth MAC address or a name substring.  When it is
    omitted and exactly one device is paired, that device is used.  *channel*
    zero means discover the RFCOMM channel through SDP.  An unpaired device
    is paired and trusted first.
    """
    log = log or (lambda _m: None)
    try:
        selected = bt.select_device(device)
        log(f"Bluetooth device: {selected.label}")
        selected = bt.ensure_paired(selected, log=log)
        selected = bt.ensure_connected(selected, timeout=max(timeout, 25.0), log=log)
        sock, found = bt.open_rfcomm(
            selected.address, channel=channel, timeout=timeout, log=log
        )
    except bt.BluetoothError as exc:
        raise TransportError(str(exc)) from exc
    return Endpoint(
        sock=sock,
        description=f"Bluetooth {selected.label} RFCOMM {found}",
        bypass_host=None,
    )


@dataclass(frozen=True)
class DiscoveryResult:
    host: str
    name: str = ""
    client_ip: str = ""
    mac: str = ""
    marker: int = 0


def _discovery_request(model: str) -> bytes:
    packet = bytearray(64)
    struct.pack_into("<III", packet, 0, DISCOVERY_MARKER, 0, 0)
    raw = model.encode("utf-8", "replace")[:31]
    packet[32 : 32 + len(raw)] = raw
    return bytes(packet)


def _parse_discovery(data: bytes, host: str) -> DiscoveryResult | None:
    if len(data) != 64:
        return None
    marker = struct.unpack_from("<I", data, 0)[0]
    if not (5000 <= marker <= 10000):
        return None
    name = data[32:64].split(b"\x00", 1)[0].decode("utf-8", "replace")
    mac = data[12:29].split(b"\x00", 1)[0].decode("ascii", "replace")
    client_ip = socket.inet_ntoa(data[8:12])
    if client_ip == "0.0.0.0":
        client_ip = ""
    return DiscoveryResult(
        host=host, name=name, client_ip=client_ip, mac=mac, marker=marker
    )


def discover_wifi(
    *,
    model: str = "Linux",
    timeout: float = 3.0,
    port: int = DISCOVERY_PORT,
) -> list[DiscoveryResult]:
    """Best-effort UDP discovery of a PdaNet hotspot.

    Sends the same 64 byte datagram as the Android tablet client and
    returns every valid answer.  The reply's source address is used as the
    phone's IP because the reply fields are encoded in a peculiar
    mixed-endian form.
    """
    results: dict[str, DiscoveryResult] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 0))
        request = _discovery_request(model)
        destinations = [("255.255.255.255", port), ("192.168.49.1", port)]
        deadline = time.monotonic() + timeout
        for dest in destinations:
            try:
                sock.sendto(request, dest)
            except OSError:
                pass
        sock.setblocking(False)
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([sock], [], [], min(remaining, 0.5))
            if not ready:
                continue
            try:
                data, addr = sock.recvfrom(512)
            except OSError:
                continue
            parsed = _parse_discovery(data, addr[0])
            if parsed is not None:
                results[parsed.host] = parsed
    return list(results.values())
