"""PdaNet wire protocol.

The protocol was reconstructed from the decompiled Android package
``com.pdanet`` 5.23 (APK ``PdaNet5232.apk``).

Every message starts with a 32 byte header made of eight 32 bit integers.
There are two *different* header layouts / byte orders:

client -> phone (``com.pdanet.b.e.a()`` in the APK)
    a, b, c, d, e, f, g, h     encoded **big endian**

phone -> client (``com.pdanet.b.e.c()`` in the APK)
    i, j, b, c, d, h, k, l     encoded **little endian**

The asymmetry is real: the Android tablet client pre-swaps the client side
integers before serialising them with a little-endian helper, and the
server reads them with a plain big-endian ``DataInputStream``.  For the
server -> client direction the server writes little endian and the client
reads big endian then swaps.  Writing/reading the logical values directly
with ``>8I`` / ``<8I`` (as done below) reproduces the observed wire format.

Fields used for the raw-IP tunnel:

client -> phone
    a = command (15 init, 302 model, 64 raw IPv4, 16 shutdown)
    b = client token / version (0 from the tablet client)
    c = connection id (0 for raw IP mode)
    d = payload length
    h = transport flags (64 Wi-Fi, 256 USB, 4 Bluetooth, 128/512 other)

phone -> client
    i = command (64 raw IPv4, 29/30/31 status messages, ...)
    d = payload length

The raw IPv4 payload starts immediately after the header.  The phone only
accepts packets whose source address is 10.1.19.2 (the address the Android
tablet client assigns to its TUN interface) and it implements TCP/UDP in
user space.  ICMP is intentionally not handled by the phone.
"""

from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass
from enum import IntEnum

HEADER_SIZE = 32
CLIENT_HEADER = struct.Struct(">8I")
SERVER_HEADER = struct.Struct("<8I")

#: Marker used in the 64 byte UDP discovery handshake.  The phone client
#: accepts any value between 5000 and 10000; the existing client code uses
#: 5014 for its own requests, so the reverse server answers with the same
#: value.  The field is little endian on the wire (see build_discovery_reply).
DISCOVERY_MARKER = 5014
DISCOVERY_SIZE = 64

# Maximum payload accepted by the phone for a non-init packet.  The server
# rejects headers with ``length > 10240`` (com.pdanet.d, "Corrupted data
# flow").  A 1500 byte MTU is well below this.
MAX_SERVER_PAYLOAD = 10240
MAX_RAW_IP = 65535


class Command(IntEnum):
    """Commands seen in the APK."""

    INIT = 15
    SHUTDOWN = 16
    CLOSE_CONNECTION = 27
    MODEL = 302
    RAW_IP = 64

    # Server -> client status / proxy commands.
    TCP_OPEN = 20
    TCP_DATA = 21
    TCP_ACK = 22
    TCP_CLOSE = 23
    UDP_CLOSE = 24
    MESSAGE = 29
    STATS = 30
    ERROR = 31


class Transport(IntEnum):
    """Values for the ``h`` field of the init packet.

    The phone maps the bits to an internal transport id:
    64 -> 3 (Wi-Fi), 4 -> 2 (Bluetooth), 256 -> 1 (USB),
    128 -> 4, 512 -> 5.  The tablet client uses 64 for Wi-Fi.
    """

    WIFI = 64
    BLUETOOTH = 4
    USB = 256
    USB_ALT = 128
    WIFI_ALT = 512


@dataclass(frozen=True, slots=True)
class ClientHeader:
    """Logical client -> phone header."""

    command: int = 0
    token: int = 0
    connection: int = 0
    length: int = 0
    extra0: int = 0
    extra1: int = 0
    extra2: int = 0
    flags: int = 0

    def pack(self) -> bytes:
        return CLIENT_HEADER.pack(
            self.command,
            self.token,
            self.connection,
            self.length,
            self.extra0,
            self.extra1,
            self.extra2,
            self.flags,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "ClientHeader":
        if len(data) < HEADER_SIZE:
            raise ValueError(f"need {HEADER_SIZE} bytes, got {len(data)}")
        return cls(*CLIENT_HEADER.unpack_from(data))


@dataclass(frozen=True, slots=True)
class ServerHeader:
    """Logical phone -> client header."""

    command: int = 0
    arg: int = 0
    connection: int = 0
    sequence: int = 0
    length: int = 0
    status: int = 0
    extra0: int = 0
    extra1: int = 0

    def pack(self) -> bytes:
        return SERVER_HEADER.pack(
            self.command,
            self.arg,
            self.connection,
            self.sequence,
            self.length,
            self.status,
            self.extra0,
            self.extra1,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "ServerHeader":
        if len(data) < HEADER_SIZE:
            raise ValueError(f"need {HEADER_SIZE} bytes, got {len(data)}")
        return cls(*SERVER_HEADER.unpack_from(data))


@dataclass(frozen=True, slots=True)
class ServerPacket:
    header: ServerHeader
    payload: bytes = b""

    @property
    def command(self) -> int:
        return self.header.command

    def __repr__(self) -> str:  # keep logs compact
        return (
            f"ServerPacket(command={self.command}, length={len(self.payload)}, "
            f"connection={self.header.connection})"
        )


@dataclass(frozen=True, slots=True)
class ClientPacket:
    """A packet sent by the phone to a PdaNet server (reverse mode)."""

    header: ClientHeader
    payload: bytes = b""

    @property
    def command(self) -> int:
        return self.header.command

    def __repr__(self) -> str:  # keep logs compact
        return (
            f"ClientPacket(command={self.command}, length={len(self.payload)}, "
            f"flags={self.header.flags})"
        )


def build_init(transport: Transport | int = Transport.USB) -> bytes:
    """Build the first packet sent after connecting.

    The Android tablet client sets ``a=15`` and ``h=64``.  For USB the
    server checks bit 256, so that is what we send by default.
    """
    return ClientHeader(command=Command.INIT, flags=int(transport)).pack()


def build_model(model: str) -> bytes:
    """Build the optional type 302 packet carrying the client model name.

    The phone displays this string in its client list.  The payload is
    UTF-16LE with no BOM and no terminator (``Build.MODEL.getBytes`` in the
    tablet client).
    """
    payload = model.encode("utf-16le")
    if len(payload) > MAX_SERVER_PAYLOAD:
        raise ValueError("model name too long")
    return ClientHeader(command=Command.MODEL, length=len(payload)).pack() + payload


def build_raw_ip(packet: bytes) -> bytes:
    """Wrap a raw IPv4 packet for the phone."""
    if not packet:
        raise ValueError("empty IP packet")
    if len(packet) > MAX_SERVER_PAYLOAD:
        raise ValueError("IP packet too large for PdaNet")
    return ClientHeader(command=Command.RAW_IP, length=len(packet)).pack() + packet


def build_shutdown() -> bytes:
    return ClientHeader(command=Command.SHUTDOWN).pack()


def build_server_raw_ip(
    packet: bytes, *, sequence: int = 0, connection: int = 0
) -> bytes:
    """Wrap a raw IPv4 packet for a PdaNet *client* (reverse mode).

    Phone clients read raw IP from a type 64 server header, exactly like the
    normal desktop client does.  The header is little endian on the wire.
    """
    if not is_ipv4(packet):
        raise ValueError("not an IPv4 packet")
    if len(packet) > MAX_SERVER_PAYLOAD:
        raise ValueError("IP packet too large for PdaNet")
    return (
        ServerHeader(
            command=Command.RAW_IP,
            connection=connection,
            sequence=sequence,
            length=len(packet),
        ).pack()
        + packet
    )


def build_discovery_reply(
    *,
    client_ip: str,
    server_ip: str,
    mac: str = "",
    name: str = "PdaNet Linux",
    marker: int = DISCOVERY_MARKER,
) -> bytes:
    """Build the 64 byte answer sent to a PdaNet phone client.

    The phone client sends its request to UDP 8000, reads three integers with
    a big-endian reader and byte-swaps them (``com.foxfi.ap.b``).  The phone
    server therefore writes all three as little endian and copies IP
    addresses as raw network-order bytes.  Mirroring that layout here keeps
    the packet compatible with both the phone and our own
    :func:`pdanet_linux.transport._parse_discovery`.
    """
    if not 5000 <= marker <= 10000:
        raise ValueError("discovery marker must be between 5000 and 10000")
    client = ipaddress.IPv4Address(client_ip).packed
    server = ipaddress.IPv4Address(server_ip).packed
    packet = bytearray(DISCOVERY_SIZE)
    struct.pack_into("<I", packet, 0, marker)
    packet[4:8] = server
    packet[8:12] = client
    mac_raw = mac.encode("ascii", "replace")[:17]
    packet[12 : 12 + len(mac_raw)] = mac_raw
    name_raw = name.encode("utf-8", "replace")[:31]
    packet[32 : 32 + len(name_raw)] = name_raw
    return bytes(packet)


def parse_client_packet(data: bytes) -> ClientPacket:
    """Parse one complete phone -> server packet.

    *data* must include the 32 byte header and the full payload.  The caller
    is responsible for framing; :class:`pdanet_linux.server.PdaNetServer`
    keeps a receive buffer for that.
    """
    if len(data) < HEADER_SIZE:
        raise ValueError(f"need at least {HEADER_SIZE} bytes")
    header = ClientHeader.unpack(data)
    if header.length > MAX_SERVER_PAYLOAD:
        raise ValueError(f"payload too large: {header.length}")
    if len(data) != HEADER_SIZE + header.length:
        raise ValueError(
            f"packet length mismatch: header says {header.length}, "
            f"got {len(data) - HEADER_SIZE}"
        )
    return ClientPacket(header=header, payload=data[HEADER_SIZE:])


def is_ipv4(packet: bytes) -> bool:
    return bool(packet) and (packet[0] >> 4) == 4


def parse_ipv4_addresses(packet: bytes) -> tuple[str, str, int] | None:
    """Return (source, destination, protocol) for a plain IPv4 packet.

    This is only used for diagnostics and tests.  It deliberately does not
    try to validate the checksum - the Linux kernel already did that before
    the packet reached our TUN device.
    """
    if len(packet) < 20 or (packet[0] >> 4) != 4:
        return None
    ihl = (packet[0] & 0x0F) * 4
    if len(packet) < ihl:
        return None
    src = ".".join(str(b) for b in packet[12:16])
    dst = ".".join(str(b) for b in packet[16:20])
    return src, dst, packet[9]


def summarize(packet: bytes) -> str:
    """Human readable one-line summary for logs."""
    info = parse_ipv4_addresses(packet)
    if info is None:
        return f"{len(packet)} byte non-IPv4 packet"
    src, dst, proto = info
    proto_name = {1: "ICMP", 6: "TCP", 17: "UDP"}.get(proto, str(proto))
    return f"IPv4 {src} -> {dst} {proto_name} ({len(packet)} bytes)"
