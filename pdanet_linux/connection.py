"""High-level connection lifecycle shared by the CLI and the GTK app."""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field
from typing import Callable

from . import netconfig, privileges, transport
from .protocol import Command, Transport
from .pump import RateLimiter, Stats, TunnelPump
from .session import PdaNetSession, describe_packet

LogFn = Callable[[str], None]
StatsFn = Callable[[Stats], None]


class ConnectionError_(RuntimeError):
    pass


@dataclass
class ConnectOptions:
    mode: str = "usb"  # "usb" or "wifi"
    serial: str | None = None
    host: str | None = None
    port: int = transport.WIFI_PORT
    local_port: int = 0
    bt_device: str | None = None
    bt_channel: int = 0
    transport_flag: int | None = None
    model: str = "Linux"
    tun_name: str = netconfig.DEFAULT_TUN
    mtu: int = netconfig.DEFAULT_MTU
    route_all: bool = True
    dns: tuple[str, ...] = netconfig.DEFAULT_DNS
    throttle_kbps: int = 0
    block_microsoft: bool = False
    privilege: str = "auto"
    dry_run: bool = False
    verbose: bool = False
    log: LogFn | None = field(default=None, repr=False)


class PdaNetConnection:
    def __init__(self, options: ConnectOptions) -> None:
        self.options = options
        self.log: LogFn = options.log or (lambda _m: None)
        self.endpoint: transport.Endpoint | None = None
        self.session: PdaNetSession | None = None
        self.tun: netconfig.TunHandle | privileges.HelperTun | None = None
        self.pump: TunnelPump | None = None
        self.stats = Stats()
        self.stop_event = threading.Event()
        self._closed = False

    @property
    def description(self) -> str:
        return self.endpoint.description if self.endpoint else ""

    @property
    def tun_name(self) -> str:
        return self.tun.name if self.tun else ""

    def open(self) -> None:
        opts = self.options
        if opts.mode == "usb":
            flag = opts.transport_flag if opts.transport_flag is not None else int(Transport.USB)
            self.endpoint = transport.connect_usb(
                serial=opts.serial, local_port=opts.local_port, log=self.log
            )
        elif opts.mode == "wifi":
            if not opts.host:
                raise ConnectionError_("Wi-Fi mode needs a phone address")
            flag = opts.transport_flag if opts.transport_flag is not None else int(Transport.WIFI)
            self.endpoint = transport.connect_wifi(opts.host, opts.port)
        elif opts.mode == "bluetooth":
            flag = (
                opts.transport_flag
                if opts.transport_flag is not None
                else int(Transport.BLUETOOTH)
            )
            self.endpoint = transport.connect_bluetooth(
                device=opts.bt_device,
                channel=opts.bt_channel,
                log=self.log,
            )
        else:
            raise ConnectionError_(f"unknown mode {opts.mode!r}")

        self.log(f"connected: {self.endpoint.description}")
        self.session = PdaNetSession(
            self.endpoint.sock,
            transport=flag,
            model=opts.model,
            log=self.log,
        )
        self.session.handshake()

        if opts.dry_run:
            self.log("dry run: handshake sent, not creating a TUN device")
            return

        try:
            config = netconfig.make_config(
                name=opts.tun_name,
                mtu=opts.mtu,
                route_all=opts.route_all,
                dns=opts.dns,
                bypass_host=self.endpoint.bypass_host if opts.route_all else None,
                block_microsoft=opts.block_microsoft,
            )
            self.tun = privileges.open_tun(
                config, privilege=opts.privilege, log=self.log
            )
        except Exception:
            self.close()
            raise

        self.pump = TunnelPump(
            self.session,
            self.tun.fd,
            stats=self.stats,
            stop_event=self.stop_event,
            log=self.log,
            on_stats=None,
            verbose=opts.verbose,
            rate_limit_bps=(opts.throttle_kbps * 1000.0 / 8.0)
            if opts.throttle_kbps > 0
            else None,
        )

    def run(self, on_stats: StatsFn | None = None) -> str:
        if self.pump is None:
            raise ConnectionError_("connection is not ready (dry run?)")
        self.pump.on_stats = on_stats
        return self.pump.run()

    def set_throttle(self, kbps: int) -> None:
        """Change the throttle limit without reconnecting."""
        kbps = max(0, int(kbps))
        self.options.throttle_kbps = kbps
        if self.pump is None:
            return
        self.pump.limiter = (
            RateLimiter(kbps * 1000.0 / 8.0) if kbps > 0 else None
        )

    def dry_run_messages(self, duration: float = 1.5) -> list[str]:
        if self.session is None:
            return []
        out: list[str] = []
        for packet in self.session.drain_status(duration):
            out.append(describe_packet(packet))
            if packet.command == Command.RAW_IP:
                # A dry run must not silently throw away tunnel traffic; the
                # phone only sends this after a client data packet, though.
                pass
        return out

    def stop(self) -> None:
        self.stop_event.set()
        if self.pump is not None:
            self.pump.stop()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_event.set()
        if self.session is not None:
            try:
                self.session.shutdown()
            except Exception:
                pass
            self.session.close()
        if self.endpoint is not None:
            self.endpoint.close()
        if self.tun is not None:
            self.tun.close()
        self.session = None
        self.endpoint = None
        self.tun = None
        self.pump = None

    def __enter__(self) -> "PdaNetConnection":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
