"""NetworkManager helpers for Wi-Fi setup and PdaNet Wi-Fi Share.

The Windows client uses WLANAPI to join the phone's hotspot and the
``Hosted Network`` feature to share the PdaNet connection.  On Linux both
jobs belong to NetworkManager, so this module only wraps ``nmcli``:

* :func:`scan` / :func:`connect` replace the desktop's WiFi pairing dialog.
* :func:`share_start` / :func:`share_stop` create a real access point with
  ``ipv4.method shared`` (NetworkManager runs dnsmasq and the NAT rules).

No root helper is needed here: NetworkManager asks polkit for permission
when the GUI starts or stops the hotspot.
"""

from __future__ import annotations

import ipaddress
import shutil
import subprocess
from dataclasses import dataclass

SHARE_NAME = "pdanet-share"
SHARE_PREFIX = "PdaNet"


class WifiError(RuntimeError):
    pass


@dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    signal: int = 0
    security: str = ""
    active: bool = False

    @property
    def label(self) -> str:
        bits = [self.ssid]
        if self.signal:
            bits.append(f"{self.signal}%")
        if self.security and self.security not in ("--", ""):
            bits.append(self.security)
        if self.active:
            bits.insert(1, "connected")
        return " - ".join(bits)


@dataclass(frozen=True)
class WifiInterface:
    name: str
    state: str


def nmcli_path() -> str:
    found = shutil.which("nmcli")
    if not found:
        raise WifiError(
            "nmcli was not found. Install NetworkManager and its command "
            "line tools to use Wi-Fi features."
        )
    return found


def _run(args: list[str], *, timeout: float = 60.0) -> str:
    try:
        proc = subprocess.run(
            [nmcli_path(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WifiError(str(exc)) from exc
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise WifiError(output.strip() or f"nmcli exited with {proc.returncode}")
    return output.strip()


def _unquote(value: str) -> str:
    """Split one ``nmcli -t`` line, honouring backslash escapes."""
    fields: list[str] = []
    buf: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            buf.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    fields.append("".join(buf))
    return "\x00".join(fields)


def split_terse(line: str) -> list[str]:
    return _unquote(line).split("\x00")


def interfaces() -> list[WifiInterface]:
    output = _run(["-t", "-f", "DEVICE,TYPE,STATE", "device", "status"])
    result: list[WifiInterface] = []
    for line in output.splitlines():
        fields = split_terse(line)
        if len(fields) >= 3 and fields[1] == "wifi":
            result.append(WifiInterface(name=fields[0], state=fields[2]))
    return result


def default_interface() -> str:
    found = interfaces()
    if not found:
        raise WifiError("no Wi-Fi interface was found")
    for item in found:
        if item.state.startswith("connected"):
            return item.name
    return found[0].name


def scan(interface: str | None = None, *, rescan: bool = True) -> list[WifiNetwork]:
    args = ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
    if interface:
        args += ["ifname", interface]
    args += ["--rescan", "yes" if rescan else "no"]
    output = _run(args, timeout=45.0)
    seen: dict[str, WifiNetwork] = {}
    for line in output.splitlines():
        fields = split_terse(line)
        if len(fields) < 4:
            continue
        in_use, ssid, signal, security = fields[:4]
        ssid = ssid.strip()
        if not ssid:
            continue
        try:
            strength = int(signal)
        except ValueError:
            strength = 0
        network = WifiNetwork(
            ssid=ssid,
            signal=strength,
            security=security.strip(),
            active=in_use.strip() == "*",
        )
        previous = seen.get(ssid)
        if previous is None or network.signal > previous.signal:
            seen[ssid] = network
    return sorted(
        seen.values(),
        key=lambda item: (not item.active, -item.signal, item.ssid.lower()),
    )


def connect(
    ssid: str,
    password: str | None = None,
    *,
    interface: str | None = None,
    timeout: float = 60.0,
) -> str:
    """Join *ssid*, letting NetworkManager store the profile for later."""
    ssid = ssid.strip()
    if not ssid:
        raise WifiError("the network name (SSID) cannot be empty")
    args = ["--wait", str(max(10, int(timeout))), "device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    if interface:
        args += ["ifname", interface]
    return _run(args, timeout=timeout + 15.0)


def gateway(interface: str | None = None) -> str:
    """Return the IPv4 gateway of the active Wi-Fi link (the phone)."""
    target = interface or default_interface()
    output = _run(["-t", "-f", "IP4.GATEWAY", "device", "show", target])
    for line in output.splitlines():
        key, _, value = line.partition(":")
        if key == "IP4.GATEWAY" and value.strip():
            return value.split()[0]
    raise WifiError(f"the Wi-Fi link {target} has no IPv4 gateway")


def _supports_ap(interface: str) -> bool:
    try:
        output = _run(
            ["-t", "-f", "WIFI-PROPERTIES.AP", "device", "show", interface],
            timeout=20.0,
        )
    except WifiError:
        return True
    for line in output.splitlines():
        if line.startswith("WIFI-PROPERTIES.AP:"):
            return line.split(":", 1)[1].strip().lower() not in ("no", "tidak")
    return True


def share_start(
    ssid: str,
    password: str,
    *,
    interface: str | None = None,
) -> str:
    """Create and activate a WPA2 access point with shared IPv4.

    NetworkManager's ``ipv4.method shared`` enables forwarding, dnsmasq and
    NAT.  The PdaNet policy route sends the forwarded traffic through the
    phone, while the phone's replies return to the hotspot clients.
    """
    ssid = ssid.strip()
    if not ssid:
        raise WifiError("the network name (SSID) cannot be empty")
    if len(ssid) > 32:
        raise WifiError("the network name (SSID) is limited to 32 characters")
    if not 8 <= len(password) <= 63:
        raise WifiError("the WPA2 password must be 8 to 63 characters")
    target = interface or default_interface()
    if not _supports_ap(target):
        raise WifiError(f"{target} does not support access point mode")
    # Start from a clean profile so a previous failed attempt cannot leak
    # stale settings into the new hotspot.
    try:
        _run(["connection", "delete", SHARE_NAME], timeout=25.0)
    except WifiError:
        pass
    _run(
        [
            "connection",
            "add",
            "type",
            "wifi",
            "ifname",
            target,
            "con-name",
            SHARE_NAME,
            "ssid",
            ssid,
            "802-11-wireless.mode",
            "ap",
            "wifi-sec.key-mgmt",
            "wpa-psk",
            "wifi-sec.psk",
            password,
            "ipv4.method",
            "shared",
            "ipv6.method",
            "ignore",
            "autoconnect",
            "no",
        ],
        timeout=30.0,
    )
    try:
        return _run(["connection", "up", SHARE_NAME], timeout=60.0)
    except WifiError:
        # Do not leave a half-created profile behind for the next attempt.
        try:
            _run(["connection", "delete", SHARE_NAME], timeout=25.0)
        except WifiError:
            pass
        raise


def share_stop() -> bool:
    """Deactivate and delete the hotspot profile.  Returns True if it existed."""
    existed = share_active()
    try:
        _run(["connection", "down", SHARE_NAME], timeout=30.0)
    except WifiError:
        pass
    try:
        _run(["connection", "delete", SHARE_NAME], timeout=30.0)
    except WifiError:
        existed = False
    return existed


@dataclass(frozen=True)
class ShareInfo:
    """Runtime details of the active NetworkManager hotspot."""

    interface: str
    address: str
    network: str
    prefix: int
    mac: str = ""

    @property
    def subnet(self) -> str:
        return f"{self.address}/{self.prefix}"


def _device_field(interface: str, key: str, *, timeout: float = 20.0) -> str:
    output = _run(["-t", "-f", key, "device", "show", interface], timeout=timeout)
    for line in output.splitlines():
        name, _, value = line.partition(":")
        if name == key or name.startswith(key + "["):
            return value.strip()
    return ""


def interface_mac(interface: str) -> str:
    """Return the MAC address of *interface* (best effort)."""
    try:
        with open(f"/sys/class/net/{interface}/address", encoding="ascii") as handle:
            value = handle.read().strip().upper()
        if value:
            return value
    except OSError:
        pass
    return _device_field(interface, "GENERAL.HWADDR").upper()


def interface_ipv4(interface: str) -> tuple[str, str, int]:
    """Return ``(address, network, prefix)`` for *interface*.

    NetworkManager prints addresses as ``IP4.ADDRESS[1]:10.42.0.1/24``.
    """
    value = _device_field(interface, "IP4.ADDRESS")
    if not value:
        raise WifiError(f"{interface} has no IPv4 address")
    # Multiple addresses are separated by " | " in nmcli terse output.
    first = value.split("|", 1)[0].strip()
    address, _, prefix_text = first.partition("/")
    try:
        prefix = int(prefix_text) if prefix_text else 24
        network = str(
            ipaddress.IPv4Interface(f"{address}/{prefix}").network
        )
    except ValueError as exc:
        raise WifiError(f"invalid IPv4 address on {interface}: {first}") from exc
    return address, network, prefix


def share_interface() -> str | None:
    """Return the interface carrying the ``pdanet-share`` connection."""
    try:
        output = _run(
            ["-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
            timeout=20.0,
        )
    except WifiError:
        return None
    for line in output.splitlines():
        fields = split_terse(line)
        if len(fields) >= 2 and fields[0] == SHARE_NAME and fields[1]:
            return fields[1]
    return None


def share_info() -> ShareInfo | None:
    """Return the active hotspot's address/network, or None when inactive."""
    interface = share_interface()
    if interface is None:
        return None
    address, network, prefix = interface_ipv4(interface)
    return ShareInfo(
        interface=interface,
        address=address,
        network=network,
        prefix=prefix,
        mac=interface_mac(interface),
    )


def share_active() -> bool:
    try:
        output = _run(
            ["-t", "-f", "NAME,DEVICE,STATE", "connection", "show", "--active"],
            timeout=20.0,
        )
    except WifiError:
        return False
    for line in output.splitlines():
        fields = split_terse(line)
        if fields and fields[0] == SHARE_NAME:
            return True
    return False
