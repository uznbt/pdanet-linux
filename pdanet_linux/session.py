"""PdaNet session: handshake and framed message I/O over a socket."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable

from .protocol import (
    HEADER_SIZE,
    Command,
    ServerHeader,
    ServerPacket,
    Transport,
    build_init,
    build_model,
    build_raw_ip,
    build_shutdown,
)

LogFn = Callable[[str], None]


class PdaNetError(RuntimeError):
    pass


class PdaNetSession:
    """Framed PdaNet session on an already connected stream socket."""

    def __init__(
        self,
        sock: socket.socket,
        *,
        transport: Transport | int = Transport.USB,
        model: str = "Linux",
        log: LogFn | None = None,
    ) -> None:
        self.sock = sock
        self.transport = Transport(transport)
        self.model = model
        self.log = log or (lambda _msg: None)
        self._rx = bytearray()
        self._closed = False

    # -- handshake -----------------------------------------------------
    def handshake(self, *, send_model: bool = True) -> None:
        """Send the two packets the Android tablet client sends first."""
        self._send(build_init(self.transport))
        self.log(f"sent init (transport flag {int(self.transport)})")
        if send_model:
            self._send(build_model(self.model))
            self.log(f"sent model name {self.model!r}")
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass

    # -- outgoing ------------------------------------------------------
    def _send(self, data: bytes) -> None:
        if self._closed:
            raise PdaNetError("session is closed")
        try:
            self.sock.sendall(data)
        except OSError as exc:
            raise PdaNetError(f"socket write failed: {exc}") from exc

    def send_raw_ip(self, packet: bytes) -> None:
        self._send(build_raw_ip(packet))

    def shutdown(self) -> None:
        """Best-effort type 16 notification (the phone tolerates EOF too)."""
        if self._closed:
            return
        try:
            self._send(build_shutdown())
        except PdaNetError:
            pass

    # -- incoming ------------------------------------------------------
    def feed(self, data: bytes) -> list[ServerPacket]:
        """Append received bytes and return all complete packets."""
        self._rx.extend(data)
        packets: list[ServerPacket] = []
        while len(self._rx) >= HEADER_SIZE:
            header = ServerHeader.unpack(self._rx)
            # The server caps non-init payloads at 10240; allow a little
            # slack for unusual status messages but never allocate blindly.
            if header.length > 65535:
                raise PdaNetError(
                    f"unreasonable PdaNet payload length {header.length}"
                )
            total = HEADER_SIZE + header.length
            if len(self._rx) < total:
                break
            payload = bytes(self._rx[HEADER_SIZE:total])
            del self._rx[:total]
            packets.append(ServerPacket(header, payload))
        return packets

    def recv_packets(self, timeout: float | None = None) -> list[ServerPacket]:
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(65536)
        finally:
            if timeout is not None:
                self.sock.settimeout(None)
        if not data:
            raise PdaNetError("phone closed the connection")
        return self.feed(data)

    def drain_status(self, duration: float = 1.0) -> list[ServerPacket]:
        """Read server messages for a short while after the handshake.

        The phone sends informational packets (types 29/30/31) during its
        own start-up; draining them is useful in ``--dry-run`` mode.
        """
        out: list[ServerPacket] = []
        deadline = time.monotonic() + duration
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                out.extend(self.recv_packets(timeout=remaining))
            except (TimeoutError, socket.timeout):
                break
            except PdaNetError:
                break
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass


def describe_packet(packet: ServerPacket) -> str:
    from .protocol import summarize

    if packet.command == Command.RAW_IP:
        return f"raw IP: {summarize(packet.payload)}"
    if packet.command == Command.MESSAGE:
        return f"message: {packet.payload.decode('utf-8', 'replace')!r}"
    if packet.command == Command.ERROR:
        return f"phone error: {packet.payload.decode('utf-8', 'replace')!r}"
    if packet.command == Command.STATS:
        return (
            "stats: "
            f"arg={packet.header.arg} status={packet.header.status} "
            f"extra={packet.header.extra0}"
        )
    return f"packet type {packet.command} ({len(packet.payload)} bytes)"
