"""Command line interface for the unofficial PdaNet Linux client."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import sys
import time

from . import __version__, bt, cellular, netconfig, privileges, selftest, transport, wifi
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
        print("Wi-Fi Share is active" if wifi.share_active() else "Wi-Fi Share is not active")
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
    if not tunnel_up:
        print(
            "warning: no pdanet* tunnel is up; the hotspot works but has no "
            "Internet until you connect PdaNet.",
            file=sys.stderr,
        )
    return 0


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
