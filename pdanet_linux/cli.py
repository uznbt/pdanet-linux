"""Command line interface for the unofficial PdaNet Linux client."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import time

from . import (
    __version__,
    bt,
    cellular,
    netconfig,
    privileges,
    selftest,
    server as pdanet_server,
    transport,
    wifi,
)
from .connection import ConnectOptions, PdaNetConnection
from .protocol import summarize
from .pump import Stats, format_bytes
from .session import PdaNetError

PROG = "pdanet-linux"


def log_line(verbose: bool):
    def log(message: str) -> None:
        if verbose:
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] {message}", file=sys.stderr, flush=True)
        else:
            print(message, file=sys.stderr, flush=True)

    return log


def _default_model() -> str:
    try:
        return socket.gethostname() or "Linux"
    except OSError:
        return "Linux"


def cmd_devices(_args: argparse.Namespace) -> int:
    devices = transport.list_devices()
    if not devices:
        print("No adb devices. Connect the phone by USB and enable USB debugging.")
        return 1
    for device in devices:
        marker = "ready" if device.state == "device" else device.state
        print(f"{device.serial}\t{marker}\t{device.label}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    results = transport.discover_wifi(model=args.model, timeout=args.timeout)
    if not results:
        print(
            "No PdaNet hotspot answered on UDP 8000. "
            "Connect to the phone's Wi-Fi hotspot and try again."
        )
        return 1
    for result in results:
        print(
            f"{result.host}\t{result.name or '(unnamed)'}"
            f"\tclient_ip={result.client_ip or '?'}\tmac={result.mac or '?'}"
        )
    return 0


def cmd_bt_devices(_args: argparse.Namespace) -> int:
    devices = transport.list_bluetooth_devices(paired_only=True)
    if not devices:
        print(
            "No paired Bluetooth devices. Pair the phone first with "
            "'bluetoothctl pair ADDRESS', or run 'pdanet-linux bt-scan'."
        )
        return 1
    for device in devices:
        try:
            info = bt.device_info(device.address)
        except bt.BluetoothError:
            info = device
        print(bt.describe_device(info))
    return 0


def cmd_signal(args: argparse.Namespace) -> int:
    signal_value = cellular.read_signal(args.serial)
    if signal_value is None:
        print(
            "Cellular signal unavailable. Connect an authorized adb device "
            "with USB debugging enabled and try again.",
            file=sys.stderr,
        )
        return 1
    print(signal_value.description or signal_value.compact)
    return 0


def cmd_bt_scan(args: argparse.Namespace) -> int:
    devices = transport.scan_bluetooth_devices(
        duration=args.timeout, log=log_line(args.verbose)
    )
    if not devices:
        print(
            "No Bluetooth devices found. Make sure the phone's Bluetooth is "
            "on and its settings screen is open so it is discoverable."
        )
        return 1
    for device in devices:
        print(bt.describe_device(device))
    return 0


def _stats_printer(interval: float = 5.0):
    last = 0.0

    def on_stats(stats: Stats) -> None:
        nonlocal last
        now = time.monotonic()
        if now - last < interval:
            return
        last = now
        print(stats.summary(), file=sys.stderr, flush=True)

    return on_stats


def cmd_connect(args: argparse.Namespace) -> int:
    wants_bluetooth = bool(args.bluetooth or args.bt_device)
    wants_usb = bool(args.usb or args.serial)
    if args.serial and wants_bluetooth:
        print("error: --serial is for USB mode, not Bluetooth", file=sys.stderr)
        return 2
    selected_modes = sum((wants_usb, bool(args.host), wants_bluetooth))
    if selected_modes > 1:
        print(
            "error: choose only one of --usb/--serial, --host, or --bluetooth",
            file=sys.stderr,
        )
        return 2
    if args.host:
        mode = "wifi"
    elif wants_bluetooth:
        mode = "bluetooth"
    elif args.usb or args.serial:
        mode = "usb"
    else:
        # Auto-detect: prefer USB when exactly one authorized device exists.
        try:
            devices = [d for d in transport.list_devices() if d.state == "device"]
        except transport.TransportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if len(devices) == 1:
            mode = "usb"
        elif len(devices) > 1:
            print(
                "error: multiple adb devices found; use --serial SERIAL",
                file=sys.stderr,
            )
            for device in devices:
                print(f"  {device.serial}\t{device.label}", file=sys.stderr)
            return 2
        else:
            print(
                "error: no adb device found. Use --usb with a connected phone, "
                "--bluetooth for Bluetooth, or --host PHONE_IP for Wi-Fi mode.",
                file=sys.stderr,
            )
            return 1

    dns: tuple[str, ...]
    if args.no_dns:
        dns = ()
    elif args.dns:
        dns = tuple(args.dns)
    else:
        dns = netconfig.DEFAULT_DNS

    if mode == "usb":
        transport_flag = args.usb_flag
    elif mode == "wifi":
        transport_flag = args.wifi_flag
    else:
        transport_flag = args.bt_flag

    log = log_line(args.verbose)
    options = ConnectOptions(
        mode=mode,
        serial=args.serial,
        host=args.host,
        port=args.port,
        local_port=args.local_port,
        bt_device=args.bt_device,
        bt_channel=args.bt_channel,
        transport_flag=transport_flag,
        model=args.model or _default_model(),
        tun_name=args.tun,
        mtu=args.mtu,
        route_all=not args.no_route,
        dns=dns,
        throttle_kbps=max(0, args.throttle),
        block_microsoft=args.block_microsoft,
        privilege=args.privilege,
        dry_run=args.dry_run,
        verbose=args.verbose,
        log=log,
    )
    conn = PdaNetConnection(options)
    stop_requested = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        conn.stop()

    old_int = signal.signal(signal.SIGINT, stop)
    old_term = signal.signal(signal.SIGTERM, stop)
    try:
        conn.open()
        if args.dry_run:
            print(f"connected via {conn.description}")
            print("handshake OK")
            for message in conn.dry_run_messages(1.5):
                print(message)
            return 0
        print(f"connected via {conn.description}")
        print(f"TUN {conn.tun_name}: 10.1.19.2/24, MTU {args.mtu}")
        if args.no_route:
            print("no routes were added (--no-route)")
        else:
            print("all IPv4 traffic is routed through the phone (table 51820)")
        print("press Ctrl+C to disconnect")
        reason = conn.run(on_stats=_stats_printer())
        print(f"disconnected: {reason}")
        print(conn.stats.summary())
        return 0 if not stop_requested else 0
    finally:
        conn.close()
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def cmd_test(args: argparse.Namespace) -> int:
    results = selftest.run_checks(
        host=args.host, dns_server=args.dns, timeout=args.timeout
    )
    print(selftest.format_report(results))
    return 0 if selftest.all_ok(results) else 1


def _wifi_interface(value: str | None) -> str:
    return value or wifi.default_interface()


def cmd_wifi_scan(args: argparse.Namespace) -> int:
    interface = _wifi_interface(args.interface)
    networks = wifi.scan(interface, rescan=not args.no_rescan)
    if not networks:
        print(f"No Wi-Fi network found on {interface}.")
        return 1
    for network in networks:
        state = "connected" if network.active else ""
        security = network.security or "open"
        print(f"{network.ssid}\t{network.signal}%\t{security}\t{state}".rstrip())
    return 0


def cmd_wifi_connect(args: argparse.Namespace) -> int:
    output = wifi.connect(
        args.ssid, args.password, interface=args.interface, timeout=args.timeout
    )
    if output:
        print(output)
    try:
        address = wifi.gateway(args.interface)
    except wifi.WifiError:
        return 0
    print(f"gateway: {address}")
    print(f"next: {PROG} connect --host {address}")
    return 0


def cmd_share(args: argparse.Namespace) -> int:
    if args.share_action == "status":
        try:
            info = wifi.share_info()
        except wifi.WifiError as exc:
            print(f"Wi-Fi Share check failed: {exc}", file=sys.stderr)
            return 1
        if info is not None:
            print(
                f"Wi-Fi Share is active on {info.interface} "
                f"({info.subnet})"
            )
        else:
            print("Wi-Fi Share is not active")
        return 0
    if args.share_action == "stop":
        if wifi.share_stop():
            print("Wi-Fi Share stopped")
        else:
            print("Wi-Fi Share was not active")
        return 0
    try:
        selftest.check_tunnel()
        tunnel_up = True
    except Exception:  # noqa: BLE001 - advisory only
        tunnel_up = False
    output = wifi.share_start(
        args.ssid, args.password, interface=args.interface
    )
    if output:
        print(output)
    print(f"Wi-Fi Share started: {args.ssid}")
    print(
        "On the phone: Settings > Wi-Fi, select this network and enter the "
        "same password."
    )
    print(
        "For PdaNet client mode (phone uses this computer's Internet), run "
        f"'{PROG} serve start'."
    )
    if not tunnel_up:
        print(
            "note: no pdanet* tunnel is up. The hotspot still shares any "
            "other Internet connection on this computer; connect PdaNet "
            "first if this computer has no Internet of its own.",
            file=sys.stderr,
        )
    return 0


# ----------------------------------------------------------------------
# Reverse PdaNet server
# ----------------------------------------------------------------------
def _serve_state_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        directory = os.path.join(base, "pdanet-linux")
    else:
        directory = os.path.join(
            tempfile.gettempdir(), f"pdanet-linux-{os.getuid()}"
        )
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return os.path.join(directory, "server.json")


def _read_serve_state() -> dict | None:
    try:
        with open(_serve_state_path(), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_serve_state(value: dict) -> None:
    path = _serve_state_path()
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _remove_serve_state() -> None:
    try:
        os.unlink(_serve_state_path())
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_is_pdanet(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return b"pdanet" in handle.read().lower()
    except OSError:
        # /proc may be unavailable; the PID check above is still useful.
        return True


def _wait_share_info(timeout: float = 20.0) -> wifi.ShareInfo:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            info = wifi.share_info()
        except wifi.WifiError as exc:
            last = exc
            info = None
        if info is not None:
            return info
        time.sleep(0.5)
    detail = f": {last}" if last else ""
    raise wifi.WifiError(
        "the hotspot was activated but did not get an IPv4 address"
        + detail
    )


def _serve_sigterm(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def _serve_status() -> int:
    state = _read_serve_state()
    if state is None:
        print("Reverse PdaNet server is not running")
    else:
        pid = int(state.get("pid") or 0)
        if not _pid_alive(pid) or not _pid_is_pdanet(pid):
            _remove_serve_state()
            print("Reverse PdaNet server is not running (stale state removed)")
        else:
            print(f"Reverse PdaNet server is running (pid {pid})")
            if state.get("ssid"):
                print(f"  Hotspot : {state['ssid']}")
                if state.get("password"):
                    print(f"  Password: {state['password']}")
            if state.get("interface"):
                print(
                    f"  Laptop  : {state.get('address', '?')} on "
                    f"{state['interface']} ({state.get('network', '?')})"
                )
            print(
                f"  Ports   : TCP {state.get('port', '?')}, "
                f"UDP {state.get('discovery_port', '?')}"
            )
            print(
                "  Phone   : connect to the hotspot, then open PdaNet+ > "
                "'Connect to a PdaNet Hotspot'."
            )
    try:
        info = wifi.share_info()
    except wifi.WifiError:
        info = None
    if info is not None:
        print(f"Hotspot is active on {info.interface} ({info.subnet})")
    else:
        print("Hotspot is not active")
    return 0


def _serve_stop() -> int:
    state = _read_serve_state()
    if state is None:
        print("Reverse PdaNet server is not running")
        return 0
    pid = int(state.get("pid") or 0)
    if not _pid_alive(pid) or not _pid_is_pdanet(pid):
        _remove_serve_state()
        print("Reverse PdaNet server is not running (stale state removed)")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"cannot stop process {pid}: {exc}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            _remove_serve_state()
            print("Reverse PdaNet server stopped")
            return 0
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    _remove_serve_state()
    print("Reverse PdaNet server stopped (forced)")
    return 0


def _serve_start(args: argparse.Namespace) -> int:
    existing = _read_serve_state()
    if existing is not None:
        pid = int(existing.get("pid") or 0)
        if _pid_alive(pid) and _pid_is_pdanet(pid):
            print(
                f"Reverse PdaNet server is already running (pid {pid}); "
                f"use '{PROG} serve stop' first.",
                file=sys.stderr,
            )
            return 1
        _remove_serve_state()

    previous_sigterm = signal.signal(signal.SIGTERM, _serve_sigterm)
    started_hotspot = False
    tun = None
    srv: pdanet_server.PdaNetServer | None = None
    info: wifi.ShareInfo | None = None
    ssid = args.ssid
    password: str | None = None
    try:
        if args.no_hotspot:
            info = wifi.share_info()
            if info is None:
                raise wifi.WifiError(
                    "--no-hotspot requires an active NetworkManager hotspot "
                    f"named '{wifi.SHARE_NAME}'"
                )
            print(
                f"Using the existing hotspot on {info.interface} "
                f"({info.subnet})"
            )
        else:
            info = wifi.share_info()
            if info is not None:
                print(
                    f"Using the existing hotspot on {info.interface} "
                    f"({info.subnet})"
                )
            else:
                password = args.password or secrets.token_urlsafe(9)
                if not 8 <= len(password) <= 63:
                    raise wifi.WifiError(
                        "the WPA2 password must be 8 to 63 characters"
                    )
                wifi.share_start(
                    ssid, password, interface=args.interface
                )
                started_hotspot = True
                info = _wait_share_info()

        allowed = [info.network, "127.0.0.0/8", *args.allow]
        config = netconfig.make_config(
            role=netconfig.ROLE_SERVER,
            name=args.tun,
            mtu=args.mtu,
            server_nat=not args.no_nat,
        )
        tun = privileges.open_tun(
            config, privilege=args.privilege, log=log_line(args.verbose)
        )
        srv = pdanet_server.PdaNetServer(
            tun.fd,
            tun_name=tun.name,
            host=args.listen,
            port=args.port,
            discovery_port=args.discovery_port,
            server_ip=info.address,
            mac=info.mac,
            name=args.name or _default_model(),
            allowed_networks=allowed,
            log=log_line(args.verbose),
            verbose=args.verbose,
        )
        srv.start()
        _write_serve_state(
            {
                "pid": os.getpid(),
                "started": time.time(),
                "ssid": ssid if started_hotspot else "",
                "password": password or "",
                "interface": info.interface,
                "address": info.address,
                "network": info.network,
                "mac": info.mac,
                "port": srv.bound_port,
                "discovery_port": srv.discovery_bound_port,
                "tun": tun.name,
            }
        )

        print("Reverse PdaNet server is running.")
        if started_hotspot:
            print(f"  Hotspot : {ssid}  (WPA2 password: {password})")
        else:
            print(
                f"  Hotspot : existing on {info.interface} ({info.subnet})"
            )
        print(
            f"  Laptop  : {info.address} on {info.interface}"
            + (f" ({info.mac})" if info.mac else "")
        )
        print(f"  TUN     : {tun.name} 10.1.19.1/24")
        print(
            f"  Ports   : TCP {srv.bound_port}, "
            f"UDP {srv.discovery_bound_port}"
        )
        print(
            "  Phone   : connect to the laptop hotspot, open PdaNet+, then "
            "choose 'Connect to a PdaNet Hotspot'."
        )
        print(
            "  Note    : the laptop needs its own Internet upstream; one "
            "Wi-Fi adapter cannot also join the phone's hotspot."
        )
        print("Press Ctrl+C to stop.")
        try:
            srv.wait()
        except KeyboardInterrupt:
            print("\nstopping reverse PdaNet server...", file=sys.stderr)
        return 0
    finally:
        if srv is not None:
            srv.stop()
        if tun is not None:
            tun.close()
        if started_hotspot and not args.keep_hotspot:
            try:
                wifi.share_stop()
            except wifi.WifiError as exc:
                print(f"warning: could not stop the hotspot: {exc}", file=sys.stderr)
        state = _read_serve_state()
        if state is not None and int(state.get("pid") or 0) == os.getpid():
            _remove_serve_state()
        signal.signal(signal.SIGTERM, previous_sigterm)


def cmd_serve(args: argparse.Namespace) -> int:
    if args.serve_action == "status":
        return _serve_status()
    if args.serve_action == "stop":
        return _serve_stop()
    return _serve_start(args)


def cmd_doctor(_args: argparse.Namespace) -> int:
    print(f"{PROG} {__version__}")
    print(f"python: {sys.version.split()[0]} ({sys.executable})")
    print(f"uid: {os.geteuid()} ({os.getuid()})")

    adb = shutil.which("adb")
    print(f"adb: {adb or 'MISSING'}")
    if adb:
        try:
            devices = transport.list_devices()
            if devices:
                for device in devices:
                    print(f"  device: {device.serial} [{device.state}] {device.label}")
            else:
                print("  device: none")
        except transport.TransportError as exc:
            print(f"  adb error: {exc}")

    print(f"/dev/net/tun: {os.path.exists('/dev/net/tun')}")
    print(f"ip: {shutil.which('ip') or 'MISSING'}")
    print(f"resolvectl: {shutil.which('resolvectl') or 'not available (DNS left unchanged)'}")
    print(f"nmcli: {shutil.which('nmcli') or 'not installed (WiFi pairing/share unavailable)'}")
    print(f"iptables: {shutil.which('iptables') or 'not installed (reverse PdaNet unavailable)'}")
    try:
        with open(netconfig.IP_FORWARD_PATH, encoding="ascii") as handle:
            forwarding = handle.read().strip()
        print(f"ip_forward: {forwarding or 'unknown'}")
    except OSError as exc:
        print(f"ip_forward: unreadable ({exc})")
    print(
        "ip_forward crash marker: "
        + ("present" if os.path.exists(netconfig.IP_FORWARD_STATE_PATH) else "absent")
    )
    print(f"notify-send: {shutil.which('notify-send') or 'not installed (GUI notifications disabled)'}")
    bt_info = bt.status()
    print(f"bluetoothctl: {bt_info['bluetoothctl'] or 'MISSING'}")
    print(f"sdptool: {bt_info['sdptool'] or 'not available (using SDP/RFCOMM probe)'}")
    if bt_info.get("error"):
        print(f"  Bluetooth error: {bt_info['error']}")
    elif bt_info.get("paired"):
        devices = ", ".join(bt_info.get("devices", []))  # type: ignore[arg-type]
        print(f"  paired Bluetooth devices: {bt_info['paired']} ({devices})")
    else:
        print("  paired Bluetooth devices: none")
    info = privileges.status()
    print(f"helper: {info['helper'] or 'NOT INSTALLED'}")
    print(f"pkexec: {info['pkexec'] or 'missing'}")
    print(f"sudo: {info['sudo'] or 'missing'}")
    print(f"polkit helper lock: {os.path.exists(netconfig.LOCK_PATH)}")
    try:
        proc = netconfig.run_ip("rule", "show", check=False)
        ours = [
            line for line in proc.stdout.splitlines()
            if f"lookup {netconfig.ROUTE_TABLE}" in line
            or str(netconfig.RULE_PREF) in line
            or str(netconfig.LAN_RULE_PREF) in line
        ]
        if ours:
            for line in ours[:4]:
                print("pdanet routing rule: " + line)
        else:
            print("pdanet routing rule: not present")
    except netconfig.NetConfigError as exc:
        print(f"routing check failed: {exc}")

    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        print(f"GTK: {Gtk.MAJOR_VERSION}.{Gtk.MINOR_VERSION}")
        print(f"libadwaita: {Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}")
    except Exception as exc:
        print(f"GTK/libadwaita: unavailable ({exc})")
    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    try:
        from .gui import main as gui_main
    except Exception as exc:
        print(f"error: the GTK GUI is unavailable: {exc}", file=sys.stderr)
        return 1
    return gui_main([])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Unofficial PdaNet USB/Wi-Fi/Bluetooth client for Linux",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_dev = sub.add_parser("devices", help="list adb devices")
    p_dev.set_defaults(func=cmd_devices)

    p_disc = sub.add_parser("discover", help="find a PdaNet Wi-Fi hotspot")
    p_disc.add_argument("--timeout", type=float, default=3.0)
    p_disc.add_argument("--model", default=_default_model())
    p_disc.set_defaults(func=cmd_discover)

    p_bt = sub.add_parser("bt-devices", help="list paired Bluetooth devices")
    p_bt.set_defaults(func=cmd_bt_devices)

    p_sig = sub.add_parser(
        "signal", help="show the phone's cellular signal through adb"
    )
    p_sig.add_argument("--serial", help="adb serial number")
    p_sig.set_defaults(func=cmd_signal)

    p_bts = sub.add_parser("bt-scan", help="scan for nearby Bluetooth devices")
    p_bts.add_argument("--timeout", type=float, default=8.0,
                       help="scan duration in seconds (default 8)")
    p_bts.add_argument("-v", "--verbose", action="store_true")
    p_bts.set_defaults(func=cmd_bt_scan)

    p_con = sub.add_parser("connect", help="connect and run the TUN tunnel")
    p_con.add_argument("--usb", action="store_true", help="use USB/ADB")
    p_con.add_argument("--serial", help="adb serial number (implies USB)")
    p_con.add_argument("--host", help="phone IP for Wi-Fi mode")
    p_con.add_argument("--bluetooth", action="store_true",
                       help="use Bluetooth RFCOMM")
    p_con.add_argument("--bt-device",
                       help="Bluetooth MAC address or device-name substring")
    p_con.add_argument("--bt-channel", type=int, default=0,
                       help="RFCOMM channel (0 = discover through SDP)")
    p_con.add_argument("--port", type=int, default=transport.WIFI_PORT)
    p_con.add_argument("--local-port", type=int, default=0,
                       help="local adb forward port (0 = pick a free port)")
    p_con.add_argument("--usb-flag", type=int, default=256,
                       help="transport flag in the init packet (default 256)")
    p_con.add_argument("--wifi-flag", type=int, default=64,
                       help="transport flag for Wi-Fi (default 64)")
    p_con.add_argument("--bt-flag", type=int, default=4,
                       help="transport flag for Bluetooth (default 4)")
    p_con.add_argument("--model", help="name shown on the phone")
    p_con.add_argument("--tun", default=netconfig.DEFAULT_TUN)
    p_con.add_argument("--mtu", type=int, default=netconfig.DEFAULT_MTU)
    p_con.add_argument("--no-route", action="store_true",
                       help="do not route all IPv4 traffic through the tunnel")
    p_con.add_argument("--dns", action="append", metavar="IP",
                       help="DNS server to use inside the tunnel (repeatable)")
    p_con.add_argument("--no-dns", action="store_true",
                       help="leave the system DNS configuration unchanged")
    p_con.add_argument("--throttle", type=int, default=0, metavar="KBPS",
                       help="limit total tunnel throughput in kbit/s "
                            "(0 = unlimited; PdaNet Desktop calls this "
                            "Throttle Connection)")
    p_con.add_argument("--block-microsoft", action="store_true",
                       help="block Microsoft update/telemetry names in "
                            "/etc/hosts while connected")
    p_con.add_argument("--privilege", choices=["auto", "pkexec", "sudo", "root"],
                       default="auto", help="how to obtain CAP_NET_ADMIN")
    p_con.add_argument("--dry-run", action="store_true",
                       help="only connect/handshake, do not create a TUN device")
    p_con.add_argument("-v", "--verbose", action="store_true",
                       help="log every IP packet (slow)")
    p_con.set_defaults(func=cmd_connect)

    p_test = sub.add_parser(
        "test", help="run the Internet Test (tunnel, DNS, UDP DNS, TCP, HTTPS)"
    )
    p_test.add_argument("--host", default=selftest.DEFAULT_HOST,
                        help=f"host name to test (default {selftest.DEFAULT_HOST})")
    p_test.add_argument("--dns", default=selftest.DEFAULT_DNS,
                        help=f"DNS server for the UDP test (default {selftest.DEFAULT_DNS})")
    p_test.add_argument("--timeout", type=float, default=8.0)
    p_test.set_defaults(func=cmd_test)

    p_ws = sub.add_parser("wifi-scan", help="scan for Wi-Fi networks with nmcli")
    p_ws.add_argument("--interface", help="Wi-Fi interface (default: auto)")
    p_ws.add_argument("--no-rescan", action="store_true",
                      help="use NetworkManager's cached scan results")
    p_ws.set_defaults(func=cmd_wifi_scan)

    p_wc = sub.add_parser("wifi-connect", help="join a Wi-Fi network with nmcli")
    p_wc.add_argument("ssid", help="network name (SSID)")
    p_wc.add_argument("-p", "--password", help="WPA/WPA2 passphrase")
    p_wc.add_argument("--interface", help="Wi-Fi interface (default: auto)")
    p_wc.add_argument("--timeout", type=float, default=60.0)
    p_wc.set_defaults(func=cmd_wifi_connect)

    p_share = sub.add_parser(
        "share", help="turn this computer into a Wi-Fi hotspot (WiFi Share)"
    )
    share_sub = p_share.add_subparsers(dest="share_action", required=True)
    p_share_start = share_sub.add_parser("start", help="create and activate the hotspot")
    p_share_start.add_argument("--ssid", default="PdaNet Share")
    p_share_start.add_argument("-p", "--password", required=True,
                               help="WPA2 password (8-63 characters)")
    p_share_start.add_argument("--interface", help="Wi-Fi interface (default: auto)")
    p_share_start.set_defaults(func=cmd_share)
    p_share_stop = share_sub.add_parser("stop", help="deactivate and delete the hotspot")
    p_share_stop.set_defaults(func=cmd_share)
    p_share_status = share_sub.add_parser("status", help="show hotspot state")
    p_share_status.set_defaults(func=cmd_share)

    p_serve = sub.add_parser(
        "serve",
        help="run a reverse PdaNet server (a phone uses this computer's "
             "Internet through PdaNet client mode)",
    )
    serve_sub = p_serve.add_subparsers(dest="serve_action", required=True)
    p_serve_start = serve_sub.add_parser(
        "start", help="start the hotspot, TUN and reverse PdaNet server"
    )
    p_serve_start.add_argument("--ssid", default="PdaNet Laptop",
                               help="hotspot name (default: PdaNet Laptop)")
    p_serve_start.add_argument("-p", "--password",
                               help="WPA2 password (default: generated)")
    p_serve_start.add_argument("--interface",
                               help="Wi-Fi interface for the hotspot "
                                    "(default: auto)")
    p_serve_start.add_argument("--no-hotspot", action="store_true",
                               help="use the existing pdanet-share hotspot "
                                    "instead of creating one")
    p_serve_start.add_argument("--keep-hotspot", action="store_true",
                               help="leave the hotspot active on exit")
    p_serve_start.add_argument("--listen", default="0.0.0.0",
                               help="TCP listen address (default: 0.0.0.0)")
    p_serve_start.add_argument("--port", type=int,
                               default=pdanet_server.DEFAULT_TCP_PORT,
                               help="TCP port (default: 8740)")
    p_serve_start.add_argument("--discovery-port", type=int,
                               default=pdanet_server.DEFAULT_DISCOVERY_PORT,
                               help="UDP discovery port (default: 8000)")
    p_serve_start.add_argument("--tun", default=netconfig.DEFAULT_TUN,
                               help="TUN interface name (default: pdanet0)")
    p_serve_start.add_argument("--mtu", type=int, default=netconfig.DEFAULT_MTU)
    p_serve_start.add_argument("--name",
                               help="server name shown by PdaNet "
                                    "(default: hostname)")
    p_serve_start.add_argument("--allow", action="append", default=[],
                               metavar="CIDR",
                               help="extra client subnet allowed to connect "
                                    "(can be repeated)")
    p_serve_start.add_argument("--no-nat", action="store_true",
                               help="do not add iptables MASQUERADE rules")
    p_serve_start.add_argument("--privilege",
                               choices=["auto", "pkexec", "sudo", "root"],
                               default="auto",
                               help="how to obtain CAP_NET_ADMIN")
    p_serve_start.add_argument("-v", "--verbose", action="store_true",
                               help="log every IP packet (slow)")
    p_serve_start.set_defaults(func=cmd_serve)
    p_serve_status = serve_sub.add_parser(
        "status", help="show reverse server state"
    )
    p_serve_status.set_defaults(func=cmd_serve)
    p_serve_stop = serve_sub.add_parser(
        "stop", help="stop a reverse server started by 'serve start'"
    )
    p_serve_stop.set_defaults(func=cmd_serve)

    p_doc = sub.add_parser("doctor", help="check the local environment")
    p_doc.set_defaults(func=cmd_doctor)

    p_gui = sub.add_parser("gui", help="start the GTK4 application")
    p_gui.set_defaults(func=cmd_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except (
        PdaNetError,
        transport.TransportError,
        bt.BluetoothError,
        privileges.PrivilegeError,
        netconfig.NetConfigError,
        pdanet_server.ServerError,
        wifi.WifiError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
