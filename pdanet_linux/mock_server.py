"""Tiny PdaNet protocol simulator for tests and offline development.

It is *not* a phone emulator: it only exercises the framing/handshake code.
By default each received raw IPv4 packet is echoed back with a type 64
server header, which is enough to verify the TUN pump.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading

from .protocol import (
    HEADER_SIZE,
    ClientHeader,
    Command,
    ServerHeader,
    summarize,
)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("connection closed")
        data.extend(chunk)
    return bytes(data)


class MockPdaNetServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, echo: bool = True) -> None:
        self.host = host
        self.port = port
        self.echo = echo
        self.bound_port = 0
        self.models: list[str] = []
        self.transport_flag: int | None = None
        self.received: list[bytes] = []
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "MockPdaNetServer":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(1)
        sock.settimeout(0.5)
        self.bound_port = int(sock.getsockname()[1])
        self._sock = sock
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                try:
                    self._session(conn)
                except (EOFError, OSError, ValueError):
                    pass

    def _session(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        while not self._stop.is_set():
            header = ClientHeader.unpack(_recv_exact(conn, HEADER_SIZE))
            payload = _recv_exact(conn, header.length) if header.length else b""
            if header.command == Command.INIT:
                self.transport_flag = header.flags
                print(f"mock: init flags={header.flags}", file=sys.stderr)
            elif header.command == Command.MODEL:
                model = payload.decode("utf-16le", "replace")
                self.models.append(model)
                print(f"mock: model={model!r}", file=sys.stderr)
            elif header.command == Command.RAW_IP:
                self.received.append(payload)
                print(f"mock: <- {summarize(payload)}", file=sys.stderr)
                if self.echo and payload:
                    reply = ServerHeader(
                        command=Command.RAW_IP, length=len(payload)
                    ).pack() + payload
                    conn.sendall(reply)
            elif header.command == Command.SHUTDOWN:
                print("mock: shutdown", file=sys.stderr)
                return
            else:
                print(
                    f"mock: type={header.command} len={header.length}",
                    file=sys.stderr,
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PdaNet protocol simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18740)
    parser.add_argument("--no-echo", action="store_true")
    args = parser.parse_args(argv)
    server = MockPdaNetServer(args.host, args.port, echo=not args.no_echo).start()
    print(
        f"mock PdaNet server on {args.host}:{server.bound_port} "
        f"(echo={not args.no_echo}); Ctrl+C to stop",
        file=sys.stderr,
    )
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
