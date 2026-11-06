"""Connection diagnostics modelled after PdaNet Desktop's Internet Test.

The Windows client verifies the phone's Internet access and can copy the
result to the clipboard.  We check the layers that matter on Linux:
the tunnel interface, system DNS, UDP DNS through the tunnel, TCP and
HTTPS.  ICMP is intentionally not tested because PdaNet does not forward
it.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import struct
import time
from dataclasses import dataclass
from typing import Callable

DEFAULT_DNS = "8.8.8.8"
DEFAULT_HOST = "example.com"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    elapsed_ms: float

    def line(self) -> str:
        state = "OK" if self.ok else "FAIL"
        return f"[{state}] {self.name}: {self.detail} ({self.elapsed_ms:.0f} ms)"


def _timed(name: str, func: Callable[[], str]) -> CheckResult:
    started = time.monotonic()
    try:
        detail = func()
        ok = True
    except Exception as exc:  # noqa: BLE001 - diagnostics for the user
        detail = str(exc) or exc.__class__.__name__
        ok = False
    return CheckResult(
        name=name,
        ok=ok,
        detail=detail,
        elapsed_ms=(time.monotonic() - started) * 1000.0,
    )


def _dns_query(host: str) -> tuple[int, bytes]:
    txid = int.from_bytes(bytes([0x50, 0x44]), "big")  # "PD"
    packet = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    for label in host.rstrip(".").split("."):
        packet += bytes([len(label)]) + label.encode("ascii")
    packet += b"\x00" + struct.pack(">HH", 1, 1)
    return txid, packet


def check_tunnel(interface_prefix: str = "pdanet") -> str:
    try:
        names = {name for _index, name in socket.if_nameindex()}
    except OSError as exc:
        raise RuntimeError(f"cannot list interfaces: {exc}") from exc
    matches = sorted(name for name in names if name.startswith(interface_prefix))
    if not matches:
        raise RuntimeError(f"no {interface_prefix}* TUN interface is up")
    return "interface " + ", ".join(matches)


def check_dns(host: str = DEFAULT_HOST, timeout: float = 8.0) -> str:
    infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    addresses = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise RuntimeError(f"{host} did not resolve to IPv4")
    return f"{host} -> {', '.join(addresses[:3])}"


def check_udp_dns(
    server: str = DEFAULT_DNS,
    host: str = DEFAULT_HOST,
    timeout: float = 8.0,
    port: int = 53,
) -> str:
    txid, query = _dns_query(host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(query, (server, port))
        data, peer = sock.recvfrom(512)
    finally:
        sock.close()
    if len(data) < 12:
        raise RuntimeError(f"short DNS reply ({len(data)} bytes) from {server}")
    reply_id, flags = struct.unpack(">HH", data[:4])
    if reply_id != txid:
        raise RuntimeError(f"DNS reply id mismatch ({reply_id:#x})")
    if not flags & 0x8000:
        raise RuntimeError("DNS reply is not a response")
    rcode = flags & 0x000F
    if rcode:
        raise RuntimeError(f"DNS server returned error code {rcode}")
    return f"{len(data)} byte reply from {peer[0]}:{peer[1]}"


def check_tcp(host: str = DEFAULT_HOST, port: int = 443, timeout: float = 8.0) -> str:
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        peer = sock.getpeername()
    finally:
        sock.close()
    return f"connected to {peer[0]}:{peer[1]}"


def check_https(url: str = f"https://{DEFAULT_HOST}/", timeout: float = 10.0) -> str:
    if not url.startswith("https://"):
        raise ValueError("HTTPS check needs an https:// URL")
    rest = url[len("https://") :]
    host_port, _, path = rest.partition("/")
    path = "/" + path
    host, _, port_text = host_port.partition(":")
    port = int(port_text) if port_text else 443
    context = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    try:
        conn.request("HEAD", path, headers={"User-Agent": "pdanet-linux-selftest"})
        response = conn.getresponse()
        response.read(1024)
        status = response.status
    finally:
        conn.close()
    if not 200 <= status < 400:
        raise RuntimeError(f"HTTP {status}")
    return f"HTTP {status} from {host}"


def run_checks(
    *,
    host: str = DEFAULT_HOST,
    dns_server: str = DEFAULT_DNS,
    timeout: float = 8.0,
) -> list[CheckResult]:
    return [
        _timed("Tunnel", lambda: check_tunnel()),
        _timed("DNS", lambda: check_dns(host, timeout)),
        _timed("UDP DNS", lambda: check_udp_dns(dns_server, host, timeout)),
        _timed("TCP", lambda: check_tcp(host, 443, timeout)),
        _timed("HTTPS", lambda: check_https(f"https://{host}/", timeout + 2)),
    ]


def format_report(results: list[CheckResult]) -> str:
    return "\n".join(result.line() for result in results)


def all_ok(results: list[CheckResult]) -> bool:
    return bool(results) and all(result.ok for result in results)
