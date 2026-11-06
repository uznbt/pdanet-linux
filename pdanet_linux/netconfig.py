"""TUN device and routing setup for the Linux PdaNet client.

Creating a TUN interface and changing routes needs CAP_NET_ADMIN.  The
normal application talks to a small root helper (see ``privileges.py`` and
``helper.py``); when the whole program is started as root this module is
used directly.

The full-tunnel mode uses a *separate routing table* (51820) plus one
policy rule instead of replacing the main default route.  This makes
cleanup reliable and leaves the normal network configuration intact:

    ip rule add pref 15000 to <private ranges> lookup main
    ip rule add pref 15010 lookup 51820
    ip route add default dev pdanet0 table 51820
    ip route add <phone>/32 via <gateway> dev <wifi> table 51820

The same policy also protects IPv6: table 51820 gets an unreachable
default, while ULA, link-local, multicast and global prefixes learned on
the local link stay in the main table.  This keeps IPv6 from silently
leaking over Wi-Fi when the phone only provides IPv4 through PdaNet.

Traffic to localhost is handled by the kernel's ``local`` table before our
rule, so ADB (127.0.0.1) keeps working.  The phone's own address gets a
bypass route in the same table so the PdaNet control socket never enters
the tunnel.
"""

from __future__ import annotations

import fcntl
import ipaddress
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field

TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000

DEFAULT_ADDRESS = "10.1.19.2/24"
DEFAULT_MTU = 1500
DEFAULT_TUN = "pdanet0"
DEFAULT_DNS = ("8.8.8.8",)
ROUTE_TABLE = 51820
# Policy rules must sort *before* the kernel's main table (preference 32766)
# or the normal Wi-Fi default route wins and the tunnel is never consulted.
# Keep the table number 51820 for familiarity, but use rule preferences well
# below 32766.  Common VPN/NetworkManager rules sit near 100-1000, so these
# values leave room for them while still overriding the main table.
RULE_PREF = 15010
# Private/multicast destinations stay on the normal network.  The phone's
# user-space stack drops RFC1918 destinations anyway, and keeping them in the
# main table preserves LAN access (printers, routers, local DNS, ...).
LAN_RULE_PREF = 15000
# Preferences used by earlier releases.  They were numerically after the main
# table and therefore ineffective whenever a normal default route existed.
# Cleanup removes them during upgrade so they cannot confuse diagnostics.
LEGACY_RULE_PREFS = (51819, 51820)
LAN_RANGES = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "224.0.0.0/4",
    "240.0.0.0/4",
)
# The PdaNet phone stack is IPv4-only.  The tunnel table gets an unreachable
# IPv6 default so traffic cannot leak over Wi-Fi's IPv6 route.  ULA,
# link-local and multicast destinations are kept in the main table, as are
# global prefixes learned on the local link.
IPV6_GLOBAL_UNICAST = "2000::/3"
IPV6_LAN_RANGES = ("fc00::/7", "fe80::/10", "ff00::/8")
LOCK_PATH = "/run/pdanet-linux.lock"

# PdaNet Desktop's "Block Microsoft Connections" prevents Windows Update,
# the Store and OneDrive from using the tethered link.  On Linux the same
# option blocks the corresponding update/telemetry names through a marked
# section in /etc/hosts.  The section is removed on disconnect and restored
# on the next start if a previous helper crashed.
HOSTS_PATH = "/etc/hosts"
HOSTS_BEGIN = "# BEGIN pdanet-linux microsoft block"
HOSTS_END = "# END pdanet-linux microsoft block"
MICROSOFT_HOSTS = (
    "windowsupdate.com",
    "www.windowsupdate.com",
    "download.windowsupdate.com",
    "update.microsoft.com",
    "windowsupdate.microsoft.com",
    "microsoftupdate.com",
    "www.microsoftupdate.com",
    "wustat.windows.com",
    "settings-win.data.microsoft.com",
    "telemetry.microsoft.com",
    "vortex.data.microsoft.com",
    "oneclient.sfx.ms",
)

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
_DEV_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,32}$")


class NetConfigError(RuntimeError):
    pass


def _strip_hosts_block(text: str) -> tuple[str, bool]:
    """Remove the marked Microsoft block from *text*."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    inside = False
    changed = False
    for line in lines:
        stripped = line.strip()
        if stripped == HOSTS_BEGIN:
            inside = True
            changed = True
            continue
        if inside:
            if stripped == HOSTS_END:
                inside = False
            continue
        out.append(line)
    return "".join(out), changed


def _write_text_atomic(path: str, text: str) -> None:
    target = os.path.realpath(path)
    mode = 0o644
    try:
        mode = os.stat(target).st_mode & 0o777
    except OSError:
        pass
    directory = os.path.dirname(target) or "."
    fd, tmp_name = tempfile.mkstemp(prefix=".hosts-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def apply_microsoft_block(path: str = HOSTS_PATH) -> bool:
    """Add the marked Microsoft block to *path*.

    Returns ``True`` when the file changed.  A missing file is created.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            original = handle.read()
    except FileNotFoundError:
        original = ""
    stripped, _changed = _strip_hosts_block(original)
    if stripped and not stripped.endswith("\n"):
        stripped += "\n"
    block = [HOSTS_BEGIN, "# Added while PdaNet Linux is connected."]
    block.extend(f"0.0.0.0 {name}" for name in MICROSOFT_HOSTS)
    block.extend((HOSTS_END, ""))
    updated = stripped + "\n".join(block)
    if updated == original:
        return False
    _write_text_atomic(path, updated)
    return True


def remove_microsoft_block(path: str = HOSTS_PATH) -> bool:
    """Remove the marked Microsoft block from *path*."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            original = handle.read()
    except FileNotFoundError:
        return False
    stripped, changed = _strip_hosts_block(original)
    if not changed:
        return False
    _write_text_atomic(path, stripped)
    return True



def _ip() -> str:
    for candidate in ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip", "/bin/ip"):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("ip")
    if not found:
        raise NetConfigError("the 'ip' command was not found (install iproute2)")
    return found


def _resolvectl() -> str | None:
    for candidate in ("/usr/bin/resolvectl", "/bin/resolvectl"):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("resolvectl")


def run_ip(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = [_ip(), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise NetConfigError(f"{' '.join(cmd)} failed: {detail}")
    return proc


def _global_ipv6_prefixes(output: str) -> list[str]:
    """Return on-link global IPv6 prefixes from ``ip -6 route show``.

    Only prefixes inside ``2000::/3`` are relevant.  The client installs a
    higher-priority rule for them before blocking the rest of global IPv6,
    so a LAN that uses provider-assigned global addresses keeps working.
    """
    result: list[str] = []
    seen: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if not parts or "/" not in parts[0] or "dev" not in parts:
            continue
        if parts[0] in ("default", "unreachable", "blackhole", "prohibit", "throw"):
            continue
        try:
            network = ipaddress.IPv6Network(parts[0], strict=False)
        except ValueError:
            continue
        if str(network) == IPV6_GLOBAL_UNICAST:
            continue
        if not network.subnet_of(ipaddress.IPv6Network(IPV6_GLOBAL_UNICAST)):
            continue
        prefix = str(network)
        if prefix not in seen:
            seen.add(prefix)
            result.append(prefix)
    return result


def validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise NetConfigError(f"invalid interface name: {name!r}")
    return name


@dataclass(frozen=True)
class BypassRoute:
    """A host route that must stay outside the tunnel."""

    address: str
    device: str | None = None
    gateway: str | None = None

    def validate(self) -> "BypassRoute":
        ipaddress.IPv4Address(self.address)
        if self.device is not None and not _DEV_RE.match(self.device):
            raise NetConfigError(f"invalid network device: {self.device!r}")
        if self.gateway is not None:
            ipaddress.IPv4Address(self.gateway)
        return self


@dataclass
class TunConfig:
    name: str = DEFAULT_TUN
    address: str = DEFAULT_ADDRESS
    mtu: int = DEFAULT_MTU
    route_all: bool = True
    dns: tuple[str, ...] = DEFAULT_DNS
    bypass: list[BypassRoute] = field(default_factory=list)
    block_microsoft: bool = False

    def validate(self) -> "TunConfig":
        validate_name(self.name)
        iface = ipaddress.IPv4Interface(self.address)
        # The phone only forwards packets whose source is 10.1.19.2
        # (com.pdanet.a.d compares the source address with this constant).
        if iface.ip != ipaddress.IPv4Address("10.1.19.2"):
            raise NetConfigError(
                "PdaNet requires the TUN address 10.1.19.2 "
                "(the phone drops packets from other source addresses)"
            )
        if not 68 <= self.mtu <= 65535:
            raise NetConfigError("MTU must be between 68 and 65535")
        for addr in self.dns:
            ipaddress.IPv4Address(addr)
        for route in self.bypass:
            route.validate()
        return self


@dataclass
class TunHandle:
    fd: int
    name: str
    configurator: "NetConfigurator | None" = None

    def close(self) -> None:
        if self.configurator is not None:
            self.configurator.down()  # also closes the fd and removes the link
            self.configurator = None
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


def open_tun_fd(name: str) -> tuple[int, str]:
    """Open /dev/net/tun and create an interface.  Requires CAP_NET_ADMIN."""
    validate_name(name)
    try:
        fd = os.open("/dev/net/tun", os.O_RDWR | os.O_CLOEXEC)
    except OSError as exc:
        raise NetConfigError(f"cannot open /dev/net/tun: {exc}") from exc
    try:
        request = struct.pack("16sH", name.encode(), IFF_TUN | IFF_NO_PI)
        result = fcntl.ioctl(fd, TUNSETIFF, request, True)
        actual = result[:16].split(b"\x00", 1)[0].decode(errors="replace")
        return fd, actual or name
    except OSError as exc:
        os.close(fd)
        raise NetConfigError(f"cannot create TUN interface {name!r}: {exc}") from exc


class NetConfigurator:
    """Create/configure a TUN interface and undo everything on close."""

    def __init__(self, config: TunConfig) -> None:
        self.config = config.validate()
        self.fd = -1
        self.name = ""
        self._lock_fd = -1
        self._rule_added = False
        self._lan_rule_added = False
        self._table_added = False
        self._dns_configured = False
        self._link_added = False
        self._hosts_blocked = False
        self._ipv6_guard = False

    def _lock(self) -> None:
        """Only one tunnel may own table 51820 at a time."""
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            self._lock_fd = fd
        except OSError as exc:
            raise NetConfigError(
                "another PdaNet tunnel is already active "
                f"(lock {LOCK_PATH}: {exc.strerror})"
            ) from exc

    def _unlock(self) -> None:
        if self._lock_fd >= 0:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = -1

    # -- helpers -------------------------------------------------------
    def _run(self, *args: str) -> None:
        run_ip(*args)

    def _route_add(self, target: str, *, table: int | None = None,
                   via: str | None = None, dev: str | None = None,
                   metric: int | None = None) -> bool:
        cmd = ["route", "replace", target]
        if via:
            cmd += ["via", via]
        if dev:
            cmd += ["dev", dev]
        if table is not None:
            cmd += ["table", str(table)]
        if metric is not None:
            cmd += ["metric", str(metric)]
        proc = run_ip(*cmd, check=False)
        return proc.returncode == 0

    # -- lifecycle -----------------------------------------------------
    def _del_rules(self, pref: int, *, limit: int = 64) -> None:
        """Delete every rule at *pref*.

        ``ip rule del pref N`` removes only one matching rule.  The LAN
        preservation setup installs one rule per private range, so a single
        delete leaves the rest behind and makes the next connection fail
        with ``RTNETLINK answers: File exists``.
        """
        for _ in range(limit):
            proc = run_ip("rule", "del", "pref", str(pref), check=False)
            if proc.returncode != 0:
                break

    def _del_rules6(self, pref: int, *, limit: int = 64) -> None:
        """Delete every IPv6 rule at *pref*."""
        for _ in range(limit):
            proc = run_ip(
                "-6", "rule", "del", "pref", str(pref), check=False
            )
            if proc.returncode != 0:
                break

    def _del_all_rules(self) -> None:
        """Delete current and legacy IPv4 policy rules at our preferences."""
        for pref in (LAN_RULE_PREF, RULE_PREF, *LEGACY_RULE_PREFS):
            self._del_rules(pref)

    def _del_all_rules6(self) -> None:
        """Delete current and legacy IPv6 policy rules at our preferences."""
        for pref in (LAN_RULE_PREF, RULE_PREF, *LEGACY_RULE_PREFS):
            self._del_rules6(pref)

    def _configure_ipv6_guard(self) -> None:
        """Block IPv6 internet without disturbing IPv6 LAN traffic.

        PdaNet carries IPv4 only.  If Wi-Fi has an IPv6 default route,
        applications could otherwise bypass the tunnel through IPv6.  The
        main table keeps ULA, link-local, multicast and on-link global
        prefixes; everything else goes to table 51820, whose default is
        unreachable.
        """
        probe = run_ip("-6", "route", "show", "table", "main", check=False)
        if probe.returncode != 0:
            # IPv6 is disabled or not available; nothing to protect.
            return
        self._ipv6_guard = True

        for network in IPV6_LAN_RANGES:
            run_ip(
                "-6", "rule", "add", "pref", str(LAN_RULE_PREF),
                "to", network, "lookup", "main", check=False,
            )
        for prefix in _global_ipv6_prefixes(probe.stdout):
            run_ip(
                "-6", "rule", "add", "pref", str(LAN_RULE_PREF),
                "to", prefix, "lookup", "main", check=False,
            )

        route = run_ip(
            "-6", "route", "replace", "unreachable", "default",
            "table", str(ROUTE_TABLE), check=False,
        )
        if route.returncode != 0:
            return
        rule = run_ip(
            "-6", "rule", "add", "pref", str(RULE_PREF),
            "lookup", str(ROUTE_TABLE), check=False,
        )
        if rule.returncode != 0 and "File exists" not in (
            rule.stderr or rule.stdout
        ):
            return

    def _remove_ipv6_guard(self) -> None:
        self._del_all_rules6()
        run_ip(
            "-6", "route", "flush", "table", str(ROUTE_TABLE), check=False
        )
        self._ipv6_guard = False

    def _remove_stale_link(self) -> None:
        """Remove a leftover PdaNet TUN from a previous crashed run.

        Only touch the configured interface when it carries our fixed
        address, so an unrelated interface with the same name is left alone.
        """
        if not self.config.name:
            return
        proc = run_ip("addr", "show", "dev", self.config.name, check=False)
        if proc.returncode == 0 and DEFAULT_ADDRESS.split("/", 1)[0] in proc.stdout:
            run_ip("link", "del", self.config.name, check=False)

    def up(self) -> int:
        self._lock()
        # Clear a block left behind by a crashed helper even if the option
        # is disabled for this connection.
        try:
            remove_microsoft_block()
        except OSError:
            pass
        # Clear leftovers from a previous crashed instance.  The lock above
        # guarantees that nobody else is using this table right now.
        if self.config.route_all:
            self._del_all_rules()
            self._del_all_rules6()
            run_ip("route", "flush", "table", str(ROUTE_TABLE), check=False)
            run_ip(
                "-6", "route", "flush", "table", str(ROUTE_TABLE),
                check=False,
            )
            self._remove_stale_link()

        # Try a few names so a stale interface does not block startup.
        last_error: Exception | None = None
        base = self.config.name
        candidates = [base] + [f"{base[:12]}{i}" for i in range(1, 10)]
        for candidate in candidates:
            try:
                self.fd, self.name = open_tun_fd(candidate)
                self._link_added = True
                break
            except NetConfigError as exc:
                last_error = exc
        if self.fd < 0:
            self._unlock()
            raise NetConfigError(f"could not create a TUN interface: {last_error}")

        try:
            self._run("link", "set", "dev", self.name, "mtu", str(self.config.mtu))
            self._run("addr", "replace", self.config.address, "dev", self.name)
            self._run("link", "set", "dev", self.name, "up")

            if self.config.route_all:
                # Put only our table's default route behind the policy rule;
                # the main table and its default route are left untouched.
                if not self._route_add("default", table=ROUTE_TABLE, dev=self.name):
                    raise NetConfigError("could not add default route to PdaNet table")
                self._table_added = True
                for bypass in self.config.bypass:
                    bypass.validate()
                    target = f"{bypass.address}/32"
                    self._route_add(
                        target,
                        table=ROUTE_TABLE,
                        via=bypass.gateway,
                        dev=bypass.device or self.name,
                    )
                proc = run_ip(
                    "rule", "add", "pref", str(RULE_PREF), "lookup", str(ROUTE_TABLE),
                    check=False,
                )
                if proc.returncode != 0 and "File exists" not in (
                    proc.stderr or proc.stdout
                ):
                    raise NetConfigError(
                        "could not install policy routing rule: "
                        + (proc.stderr or proc.stdout).strip()
                    )
                self._rule_added = True
                # Mark this before installing the per-range rules so a
                # partial failure still cleans up everything in down().
                self._lan_rule_added = True
                for network in LAN_RANGES:
                    proc = run_ip(
                        "rule", "add", "pref", str(LAN_RULE_PREF), "to", network,
                        "lookup", "main", check=False,
                    )
                    if proc.returncode != 0 and "File exists" not in (
                        proc.stderr or proc.stdout
                    ):
                        raise NetConfigError(
                            f"could not keep {network} on the local network: "
                            + (proc.stderr or proc.stdout).strip()
                        )

                self._configure_ipv6_guard()

            self._configure_dns()
            if self.config.block_microsoft:
                self._hosts_blocked = True
                try:
                    apply_microsoft_block()
                except OSError as exc:
                    raise NetConfigError(
                        f"could not update {HOSTS_PATH}: {exc}"
                    ) from exc
            return self.fd
        except Exception:
            self.down()
            raise

    def _configure_dns(self) -> None:
        if not self.config.dns:
            return
        resolvectl = _resolvectl()
        if not resolvectl:
            return
        try:
            subprocess.run(
                [resolvectl, "dns", self.name, *self.config.dns],
                capture_output=True, text=True, timeout=15, check=True,
            )
            subprocess.run(
                [resolvectl, "domain", self.name, "~."],
                capture_output=True, text=True, timeout=15, check=True,
            )
            self._dns_configured = True
        except (OSError, subprocess.SubprocessError):
            # DNS integration is best effort.  The tunnel still works and
            # the system resolver can still reach its normal server.
            pass

    def down(self) -> None:
        if self._hosts_blocked:
            try:
                remove_microsoft_block()
            except OSError:
                pass
            self._hosts_blocked = False
        if self._dns_configured:
            resolvectl = _resolvectl()
            if resolvectl:
                subprocess.run(
                    [resolvectl, "revert", self.name],
                    capture_output=True, text=True, timeout=15, check=False,
                )
            self._dns_configured = False
        if self.config.route_all:
            # Flush by preference instead of relying on the per-object flags:
            # up() may have failed halfway through the LAN rules, and a
            # previous crashed helper may have left extra rules behind.
            self._remove_ipv6_guard()
            self._del_all_rules()
            run_ip("route", "flush", "table", str(ROUTE_TABLE), check=False)
        self._rule_added = False
        self._lan_rule_added = False
        self._table_added = False
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        if self._link_added and self.name:
            run_ip("link", "del", self.name, check=False)
            self._link_added = False
        self._unlock()


def route_get(host: str) -> BypassRoute:
    """Return the route the kernel currently uses to reach *host*.

    Example output::

        $ ip route get 192.168.49.1
        192.168.49.1 dev wlan0 src 192.168.49.5 uid 1000
        $ ip route get 8.8.8.8
        8.8.8.8 via 192.168.1.1 dev wlan0 src 192.168.1.5 uid 1000
    """
    ipaddress.IPv4Address(host)
    proc = run_ip("route", "get", host, check=False)
    if proc.returncode != 0:
        raise NetConfigError(f"cannot determine route to {host}: {proc.stderr.strip()}")
    tokens = proc.stdout.split()
    dev = via = None
    for idx, token in enumerate(tokens[:-1]):
        if token == "dev":
            dev = tokens[idx + 1]
        elif token == "via":
            via = tokens[idx + 1]
    if not dev:
        raise NetConfigError(f"could not parse 'ip route get {host}': {proc.stdout.strip()}")
    return BypassRoute(address=host, device=dev, gateway=via)


def make_config(
    *,
    name: str = DEFAULT_TUN,
    address: str = DEFAULT_ADDRESS,
    mtu: int = DEFAULT_MTU,
    route_all: bool = True,
    dns: tuple[str, ...] = DEFAULT_DNS,
    bypass_host: str | None = None,
    block_microsoft: bool = False,
) -> TunConfig:
    bypass: list[BypassRoute] = []
    if route_all and bypass_host:
        bypass.append(route_get(bypass_host))
    return TunConfig(
        name=name,
        address=address,
        mtu=mtu,
        route_all=route_all,
        dns=dns,
        bypass=bypass,
        block_microsoft=block_microsoft,
    ).validate()
