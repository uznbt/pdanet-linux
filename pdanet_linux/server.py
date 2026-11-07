"""Reverse PdaNet server: let a phone use this computer's Internet.

Normal PdaNet has the phone as the server and the computer as the client.
This module implements the opposite direction, compatible with the
``Connect to a PdaNet Hotspot`` mode in the Android app:

* answer the phone's UDP discovery probe on port 8000;
* accept its TCP session on port 8740;
* relay raw IPv4 packets between the phone's VPN TUN (10.1.19.2) and a
  local TUN interface (10.1.19.1);
* let the kernel forward and NAT those packets to the laptop's normal
  upstream connection.

The privileged part (TUN address, IPv4 forwarding and the firewall/NAT
rules) lives in :mod:`pdanet_linux.netconfig`.  This module only handles
the PdaNet protocol and never needs root itself.
"""

from __future__ import annotations

import ipaddress
import os
import select
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .protocol import (
    DISCOVERY_MARKER,
    DISCOVERY_SIZE,
    HEADER_SIZE,
    MAX_SERVER_PAYLOAD,
    ClientHeader,
    Command,
    build_discovery_reply,
    build_server_raw_ip,
    is_ipv4,
    parse_client_packet,
    parse_ipv4_addresses,
    summarize,
)

LogFn = Callable[[str], None]

DEFAULT_TCP_PORT = 8740
DEFAULT_DISCOVERY_PORT = 8000
DEFAULT_CLIENT_IP = "10.1.19.2"


class ServerError(RuntimeError):
    pass


@dataclass
class ServerStats:
    """Counters for the reverse server.

    ``sent`` describes packets travelling from the laptop's Internet to the
    phone (downloads on the phone).  ``received`` describes packets uploaded
    by the phone.
    """

    started_at: float = 0.0
    connected_at: float = 0.0
    client_address: str = ""
    client_model: str = ""
    sent_packets: int = 0
    sent_bytes: int = 0
    received_packets: int = 0
    received_bytes: int = 0
    discovery_requests: int = 0

    @property
    def connected(self) -> bool:
        return self.connected_at > 0.0

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started_at) if self.started_at else 0.0

    @property
    def connection_elapsed(self) -> float:
        if not self.connected_at:
            return 0.0
        return max(0.0, time.monotonic() - self.connected_at)

    @property
    def sent_rate(self) -> float:
        return self.sent_bytes / self.elapsed if self.elapsed else 0.0

    @property
    def received_rate(self) -> float:
        return self.received_bytes / self.elapsed if self.elapsed else 0.0

    def reset(self) -> None:
        self.started_at = time.monotonic()
        self.connected_at = 0.0
        self.client_address = ""
        self.client_model = ""
        self.sent_packets = self.sent_bytes = 0
        self.received_packets = self.received_bytes = 0
        self.discovery_requests = 0

    def summary(self) -> str:
        from .pump import format_bytes, format_duration

        if not self.connected:
            client = "waiting for phone"
        else:
            client = self.client_address or "phone"
            if self.client_model:
                client += f" ({self.client_model})"
        return (
            f"client {client} | up {format_duration(self.elapsed)} | "
            f"to phone {format_bytes(self.sent_bytes)} "
            f"({self.sent_packets} pkt) | "
            f"from phone {format_bytes(self.received_bytes)} "
            f"({self.received_packets} pkt)"
        )


def _networks(values: Iterable[str] | None) -> tuple[ipaddress.IPv4Network, ...]:
    if not values:
        # Safe default for library users: only local processes may connect.
        values = ("127.0.0.0/8",)
    result: list[ipaddress.IPv4Network] = []
    for value in values:
        try:
            result.append(ipaddress.IPv4Network(value, strict=False))
        except ValueError as exc:
            raise ValueError(f"invalid allowed network {value!r}") from exc
    return tuple(result)


def _address_allowed(
    address: str, networks: tuple[ipaddress.IPv4Network, ...]
) -> bool:
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _local_ip_for(peer: str) -> str:
    """Best-effort source address the kernel would use for *peer*."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((peer, 9))
        return str(probe.getsockname()[0])
    except OSError:
        return "0.0.0.0"
    finally:
        probe.close()


class DiscoveryResponder:
    """Answer the Android phone client's UDP discovery probe on port 8000."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_DISCOVERY_PORT,
        server_ip: str | None = None,
        mac: str = "",
        name: str = "PdaNet Linux",
        marker: int = DISCOVERY_MARKER,
        allowed_networks: Iterable[str] | None = None,
        log: LogFn | None = None,
        stats: ServerStats | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.server_ip = server_ip
        self.mac = mac
        self.name = name
        self.marker = marker
        self.allowed = _networks(allowed_networks)
        self.log = log or (lambda _m: None)
        self.stats = stats
        self.bound_port = 0
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            raise ServerError(
                f"cannot listen for PdaNet discovery on UDP {self.port}: {exc}"
            ) from exc
        sock.settimeout(0.5)
        self._sock = sock
        self.bound_port = int(sock.getsockname()[1])
        self._thread = threading.Thread(
            target=self._run, name="pdanet-discovery", daemon=True
        )
        self._thread.start()
        self.log(
            f"discovery responder listening on UDP {self.host}:{self.bound_port}"
        )

    def _run(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._stop.is_set():
            try:
                data, address = sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                break
            if len(data) != DISCOVERY_SIZE:
                self.log(
                    f"discovery: ignored {len(data)} byte packet from "
                    f"{address[0]} (expected {DISCOVERY_SIZE})"
                )
                continue
            if not _address_allowed(address[0], self.allowed):
                self.log(
                    f"discovery: ignored probe from {address[0]} "
                    "(outside the hotspot subnet)"
                )
                continue
            server_ip = self.server_ip or _local_ip_for(address[0])
            try:
                reply = build_discovery_reply(
                    client_ip=address[0],
                    server_ip=server_ip,
                    mac=self.mac,
                    name=self.name,
                    marker=self.marker,
                )
            except ValueError as exc:
                self.log(f"discovery: cannot build reply: {exc}")
                continue
            try:
                sock.sendto(reply, address)
            except OSError as exc:
                self.log(f"discovery: reply to {address[0]} failed: {exc}")
                continue
            if self.stats is not None:
                self.stats.discovery_requests += 1
            self.log(
                f"discovery: answered {address[0]}:{address[1]} "
                f"with server {server_ip} ({self.name})"
            )

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None


@dataclass
class _Session:
    sock: socket.socket
    address: tuple[str, int]
    started_at: float = field(default_factory=time.monotonic)
    model: str = ""
    closed: bool = False


class PdaNetServer:
    """TCP + TUN relay for the Android PdaNet client mode."""

    def __init__(
        self,
        tun_fd: int,
        *,
        tun_name: str = "pdanet0",
        host: str = "0.0.0.0",
        port: int = DEFAULT_TCP_PORT,
        discovery_port: int | None = DEFAULT_DISCOVERY_PORT,
        server_ip: str | None = None,
        mac: str = "",
        name: str = "PdaNet Linux",
        allowed_networks: Iterable[str] | None = None,
        log: LogFn | None = None,
        verbose: bool = False,
    ) -> None:
        self.tun_fd = tun_fd
        self.tun_name = tun_name
        self.host = host
        self.port = port
        self.discovery_port = discovery_port
        self.server_ip = server_ip
        self.mac = mac
        self.name = name
        self.allowed = _networks(allowed_networks)
        self.log = log or (lambda _m: None)
        self.verbose = verbose
        self.stats = ServerStats()
        self.bound_port = 0
        self._listener: socket.socket | None = None
        self._discovery: DiscoveryResponder | None = None
        self._session: _Session | None = None
        self._session_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._wake_read = -1
        self._wake_write = -1
        self._sequence = 0

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._listener is not None:
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.host, self.port))
            listener.listen(4)
        except OSError as exc:
            listener.close()
            raise ServerError(
                f"cannot listen for PdaNet clients on TCP {self.port}: {exc}"
            ) from exc
        listener.settimeout(0.5)
        self._listener = listener
        self.bound_port = int(listener.getsockname()[1])

        if self.discovery_port is not None:
            self._discovery = DiscoveryResponder(
                host=self.host,
                port=self.discovery_port,
                server_ip=self.server_ip,
                mac=self.mac,
                name=self.name,
                allowed_networks=[str(net) for net in self.allowed],
                log=self.log,
                stats=self.stats,
            )
            try:
                self._discovery.start()
            except Exception:
                listener.close()
                self._listener = None
                self._discovery = None
                raise

        self._wake_read, self._wake_write = os.pipe()
        os.set_blocking(self._wake_read, False)
        self._stop.clear()
        self.stats.reset()
        self.log(
            f"reverse PdaNet server listening on TCP {self.host}:{self.bound_port}"
            f" (TUN {self.tun_name}, phone {DEFAULT_CLIENT_IP})"
        )
        for target, thread_name in (
            (self._accept_loop, "pdanet-accept"),
            (self._tun_loop, "pdanet-server-tun"),
        ):
            thread = threading.Thread(target=target, name=thread_name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def wait(self) -> None:
        """Block until :meth:`stop` is called (used by the CLI)."""
        while not self._stop.wait(0.5):
            pass

    def stop(self) -> None:
        if self._listener is None and not self._threads:
            return
        self._stop.set()
        if self._wake_write >= 0:
            try:
                os.write(self._wake_write, b"x")
            except OSError:
                pass
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        if self._discovery is not None:
            self._discovery.stop()
            self._discovery = None
        with self._session_lock:
            session = self._session
        if session is not None:
            self._close_session(session, "server stopping")
        for thread in self._threads:
            thread.join(timeout=3.0)
        self._threads.clear()
        if self._wake_read >= 0:
            try:
                os.close(self._wake_read)
            except OSError:
                pass
            self._wake_read = -1
        if self._wake_write >= 0:
            try:
                os.close(self._wake_write)
            except OSError:
                pass
            self._wake_write = -1
        self.stats.connected_at = 0.0
        self.log("reverse PdaNet server stopped")

    # -- properties ----------------------------------------------------
    @property
    def running(self) -> bool:
        return self._listener is not None

    @property
    def client_address(self) -> str:
        with self._session_lock:
            session = self._session
        return f"{session.address[0]}:{session.address[1]}" if session else ""

    @property
    def discovery_bound_port(self) -> int:
        return self._discovery.bound_port if self._discovery else 0

    # -- TCP accept / session -----------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                break
            try:
                sock, address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            if not _address_allowed(address[0], self.allowed):
                self.log(
                    f"rejected PdaNet client from {address[0]} "
                    "(outside the hotspot subnet)"
                )
                try:
                    sock.close()
                except OSError:
                    pass
                continue
            with self._session_lock:
                current = self._session
                if current is None:
                    session = _Session(sock=sock, address=address)
                    self._session = session
                else:
                    session = None
            if session is None:
                self.log(
                    f"rejected {address[0]}:{address[1]}: "
                    f"already connected to {current.address[0]}"
                )
                try:
                    sock.close()
                except OSError:
                    pass
                continue
            # Reap finished session threads so long-running servers do not
            # accumulate dead thread objects across phone reconnects.
            self._threads = [t for t in self._threads if t.is_alive()]
            self.stats.client_address = f"{address[0]}:{address[1]}"
            self.stats.client_model = ""
            self.stats.connected_at = time.monotonic()
            self.log(f"phone connected from {address[0]}:{address[1]}")
            thread = threading.Thread(
                target=self._session_loop,
                args=(session,),
                name="pdanet-session",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def _session_loop(self, session: _Session) -> None:
        sock = session.sock
        # A Wi-Fi hotspot can briefly stall while the phone roams or sleeps.
        # 3 seconds is long enough for a burst to drain, while stop() closes
        # the socket to unblock recv immediately.
        sock.settimeout(3.0)
        buffer = bytearray()
        reason = "phone disconnected"
        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    continue
                except OSError as exc:
                    reason = f"connection error: {exc}"
                    break
                if not chunk:
                    break
                buffer.extend(chunk)
                while len(buffer) >= HEADER_SIZE:
                    header = ClientHeader.unpack(buffer[:HEADER_SIZE])
                    if header.length > MAX_SERVER_PAYLOAD:
                        reason = (
                            f"phone sent an oversized packet "
                            f"({header.length} bytes)"
                        )
                        return
                    total = HEADER_SIZE + header.length
                    if len(buffer) < total:
                        break
                    raw = bytes(buffer[:total])
                    del buffer[:total]
                    try:
                        packet = parse_client_packet(raw)
                    except ValueError as exc:
                        reason = f"protocol error: {exc}"
                        return
                    if not self._handle_client_packet(session, packet):
                        reason = "phone closed the PdaNet session"
                        return
        finally:
            self._close_session(session, reason)

    def _handle_client_packet(self, session: _Session, packet) -> bool:
        command = packet.command
        if command == Command.INIT:
            self.log(
                f"phone init: flags={packet.header.flags} "
                f"token={packet.header.token}"
            )
            if packet.header.flags != 64:
                self.log(
                    "warning: phone did not announce Wi-Fi transport "
                    f"(flags={packet.header.flags})"
                )
            return True
        if command == Command.MODEL:
            try:
                model = packet.payload.decode("utf-16le", "replace")
            except Exception:
                model = ""
            session.model = model
            self.stats.client_model = model
            self.log(f"phone model: {model or '(unknown)'}")
            return True
        if command == Command.RAW_IP:
            if not is_ipv4(packet.payload):
                self.log("ignored non-IPv4 packet from phone")
                return True
            info = parse_ipv4_addresses(packet.payload)
            if info is None or info[0] != DEFAULT_CLIENT_IP:
                source = info[0] if info else "?"
                self.log(
                    f"dropped packet from phone with unexpected source {source} "
                    f"(expected {DEFAULT_CLIENT_IP})"
                )
                return True
            try:
                written = os.write(self.tun_fd, packet.payload)
            except OSError as exc:
                self.log(f"TUN write failed: {exc}")
                return False
            if written != len(packet.payload):
                self.log(
                    f"short TUN write ({written}/{len(packet.payload)} bytes)"
                )
            self.stats.received_packets += 1
            self.stats.received_bytes += len(packet.payload)
            if self.verbose:
                self.log(f"<- {summarize(packet.payload)}")
            return True
        if command in (Command.SHUTDOWN, Command.CLOSE_CONNECTION):
            return False
        if command in (Command.MESSAGE, Command.ERROR, Command.STATS):
            self.log(
                f"phone message: command={int(command)} "
                f"length={len(packet.payload)}"
            )
            return True
        self.log(
            f"ignored phone command {int(command)} "
            f"({len(packet.payload)} bytes)"
        )
        return True

    def _close_session(self, session: _Session, reason: str) -> None:
        with self._session_lock:
            if session.closed:
                return
            session.closed = True
            current = self._session is session
            if current:
                self._session = None
        try:
            session.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            session.sock.close()
        except OSError:
            pass
        if session.address[0]:
            self.log(
                f"phone {session.address[0]}:{session.address[1]} disconnected "
                f"({reason})"
            )
        if current:
            self.stats.connected_at = 0.0

    # -- TUN -> phone ---------------------------------------------------
    def _tun_loop(self) -> None:
        while not self._stop.is_set():
            try:
                readable, _w, _x = select.select(
                    [self.tun_fd, self._wake_read], [], [], 0.5
                )
            except (OSError, ValueError):
                break
            if self._wake_read in readable:
                try:
                    while os.read(self._wake_read, 4096):
                        pass
                except OSError:
                    pass
                break
            if self.tun_fd not in readable:
                continue
            try:
                packet = os.read(self.tun_fd, 65535)
            except InterruptedError:
                continue
            except OSError as exc:
                self.log(f"TUN read failed: {exc}")
                break
            if not packet:
                continue
            with self._session_lock:
                session = self._session
            if session is None:
                # No phone yet.  Dropping is correct: there is nowhere to
                # send the packet, and queueing it would leak memory.
                continue
            if not is_ipv4(packet):
                continue
            self._sequence = (self._sequence + 1) & 0xFFFFFFFF
            try:
                frame = build_server_raw_ip(packet, sequence=self._sequence)
            except ValueError as exc:
                self.log(f"cannot wrap TUN packet: {exc}")
                continue
            try:
                with self._send_lock:
                    session.sock.sendall(frame)
            except OSError as exc:
                self._close_session(session, f"send failed: {exc}")
                continue
            self.stats.sent_packets += 1
            self.stats.sent_bytes += len(packet)
            if self.verbose:
                self.log(f"-> {summarize(packet)}")
