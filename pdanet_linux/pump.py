"""Bidirectional raw-IP pump between a TUN device and a PdaNet session."""

from __future__ import annotations

import os
import selectors
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .protocol import Command, summarize
from .session import PdaNetError, PdaNetSession, describe_packet

LogFn = Callable[[str], None]
StatsFn = Callable[["Stats"], None]


@dataclass
class Stats:
    started_at: float = 0.0
    sent_packets: int = 0
    sent_bytes: int = 0
    received_packets: int = 0
    received_bytes: int = 0

    def reset(self) -> None:
        self.started_at = time.monotonic()
        self.sent_packets = self.sent_bytes = 0
        self.received_packets = self.received_bytes = 0

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started_at) if self.started_at else 0.0

    @property
    def sent_rate(self) -> float:
        return self.sent_bytes / self.elapsed if self.elapsed else 0.0

    @property
    def received_rate(self) -> float:
        return self.received_bytes / self.elapsed if self.elapsed else 0.0

    def summary(self) -> str:
        return (
            f"up {format_duration(self.elapsed)} | "
            f"sent {format_bytes(self.sent_bytes)} ({self.sent_packets} pkt) | "
            f"recv {format_bytes(self.received_bytes)} ({self.received_packets} pkt)"
        )


def format_bytes(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class RateLimiter:
    """Shared token bucket used by the desktop client's Throttle option.

    Windows PdaNet has a "Throttle Connection" checkbox for older phones
    that drop the USB link at full speed.  Applying a small artificial
    delay in the pump has the same effect without touching the USB stack.
    """

    def __init__(
        self,
        bytes_per_second: float,
        *,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if bytes_per_second <= 0:
            raise ValueError("rate limit must be positive")
        self.rate = float(bytes_per_second)
        self.capacity = float(capacity or max(self.rate * 0.25, 65536))
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()

    @property
    def tokens(self) -> float:
        return self._tokens

    def consume(self, amount: int) -> None:
        if amount <= 0:
            return
        if amount > self.capacity:
            # A bucket smaller than one packet would wait forever.  Grow it
            # to the largest packet seen; the long-run rate is unchanged.
            self.capacity = float(amount)
        while True:
            now = self._clock()
            self._tokens = min(
                self.capacity,
                self._tokens + max(0.0, now - self._updated) * self.rate,
            )
            self._updated = now
            if self._tokens >= amount:
                self._tokens -= amount
                return
            self._sleep((amount - self._tokens) / self.rate)



class TunnelPump:
    """Pump packets until stopped, the socket closes, or the TUN fails."""

    def __init__(
        self,
        session: PdaNetSession,
        tun_fd: int,
        *,
        stats: Stats | None = None,
        stop_event: threading.Event | None = None,
        log: LogFn | None = None,
        on_stats: StatsFn | None = None,
        verbose: bool = False,
        rate_limit_bps: float | None = None,
    ) -> None:
        self.session = session
        self.tun_fd = tun_fd
        self.stats = stats or Stats()
        self.stats.reset()
        self.stop_event = stop_event or threading.Event()
        self.log = log or (lambda _m: None)
        self.on_stats = on_stats
        self.verbose = verbose
        self.limiter = RateLimiter(rate_limit_bps) if rate_limit_bps else None
        self._last_stats = 0.0
        self._packets_since_log = 0

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> str:
        """Run the pump; returns a human readable stop reason."""
        sel = selectors.DefaultSelector()
        sel.register(self.tun_fd, selectors.EVENT_READ, "tun")
        sel.register(self.session.sock, selectors.EVENT_READ, "net")
        reason = "stopped"
        self.log("tunnel is up (TUN <-> PdaNet)")
        try:
            while not self.stop_event.is_set():
                events = sel.select(timeout=1.0)
                if not events:
                    self._maybe_report()
                    continue
                for key, _mask in events:
                    if key.data == "tun":
                        if not self._read_tun():
                            reason = "TUN device closed"
                            return reason
                    else:
                        if not self._read_net():
                            reason = "phone closed the connection"
                            return reason
                self._maybe_report()
            return reason
        finally:
            sel.close()
            self._maybe_report(force=True)
            self.log("tunnel stopped")

    # -- directions ----------------------------------------------------
    def _read_tun(self) -> bool:
        try:
            packet = os.read(self.tun_fd, 65535)
        except InterruptedError:
            return True
        except OSError as exc:
            self.log(f"TUN read failed: {exc}")
            return False
        if not packet:
            return False
        if self.verbose:
            self.log(f"-> {summarize(packet)}")
        try:
            if self.limiter is not None:
                self.limiter.consume(len(packet))
            self.session.send_raw_ip(packet)
        except PdaNetError as exc:
            self.log(str(exc))
            return False
        self.stats.sent_packets += 1
        self.stats.sent_bytes += len(packet)
        return True

    def _read_net(self) -> bool:
        try:
            data = self.session.sock.recv(65536)
        except InterruptedError:
            return True
        except OSError as exc:
            self.log(f"PdaNet read failed: {exc}")
            return False
        if not data:
            return False
        try:
            packets = self.session.feed(data)
        except PdaNetError as exc:
            self.log(str(exc))
            return False
        for packet in packets:
            if packet.command == Command.RAW_IP:
                if self.limiter is not None:
                    self.limiter.consume(len(packet.payload))
                if not self._write_tun(packet.payload):
                    return False
                self.stats.received_packets += 1
                self.stats.received_bytes += len(packet.payload)
                if self.verbose:
                    self.log(f"<- {summarize(packet.payload)}")
            elif packet.command in (Command.MESSAGE, Command.ERROR, Command.STATS):
                self.log(describe_packet(packet))
            elif self.verbose:
                self.log(describe_packet(packet))
        return True

    def _write_tun(self, packet: bytes) -> bool:
        try:
            written = os.write(self.tun_fd, packet)
        except InterruptedError:
            return True
        except OSError as exc:
            self.log(f"TUN write failed: {exc}")
            return False
        if written != len(packet):
            self.log(f"short TUN write ({written}/{len(packet)} bytes)")
            return False
        return True

    # -- reporting -----------------------------------------------------
    def _maybe_report(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_stats < 1.0:
            return
        self._last_stats = now
        if self.on_stats is not None:
            self.on_stats(self.stats)
