"""Privilege handling: direct root mode or root helper over pkexec/sudo."""

from __future__ import annotations

import array
import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import netconfig

LogFn = Callable[[str], None]


class PrivilegeError(RuntimeError):
    pass


def helper_candidates() -> list[str]:
    paths = [
        os.environ.get("PDANET_HELPER", ""),
        "/usr/local/libexec/pdanet-linux-helper",
        "/usr/libexec/pdanet-linux-helper",
        "/usr/lib/pdanet-linux/pdanet-linux-helper",
    ]
    repo = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "tools", "pdanet-linux-helper")
    )
    if os.environ.get("PDANET_ALLOW_DEV_HELPER") == "1":
        paths.append(repo)
    return [p for p in paths if p and os.path.isfile(p) and os.access(p, os.X_OK)]


def find_helper() -> str | None:
    candidates = helper_candidates()
    return candidates[0] if candidates else None


def _runtime_dir() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        path = os.path.join(base, "pdanet-linux")
    else:
        path = os.path.join(tempfile.gettempdir(), f"pdanet-linux-{os.getuid()}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _read_line(conn: socket.socket, limit: int = 65536) -> bytes:
    data = bytearray()
    while len(data) < limit:
        chunk = conn.recv(1)
        if not chunk:
            raise PrivilegeError("privileged helper closed the control socket")
        if chunk == b"\n":
            return bytes(data)
        data.extend(chunk)
    raise PrivilegeError("protocol line too long")


def _recv_fd(conn: socket.socket) -> int:
    fdsize = array.array("i", [0]).itemsize
    msg, ancdata, _flags, _addr = conn.recvmsg(1, socket.CMSG_SPACE(fdsize))
    if not msg:
        raise PrivilegeError("privileged helper did not send the TUN fd")
    for level, ctype, data in ancdata:
        if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
            arr = array.array("i")
            usable = len(data) - (len(data) % fdsize)
            arr.frombytes(data[:usable])
            if arr:
                return int(arr[0])
    raise PrivilegeError("privileged helper sent no file descriptor")


@dataclass
class HelperTun:
    fd: int
    name: str
    conn: socket.socket
    proc: subprocess.Popen[bytes]
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.conn.close()  # helper sees EOF and removes the interface
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


def _choose_launcher(privilege: str) -> tuple[str, bool]:
    """Return (launcher, capture_stderr)."""
    if privilege == "auto":
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
            privilege = "pkexec"
        elif sys.stdin.isatty():
            privilege = "sudo"
        else:
            privilege = "pkexec"
    if privilege == "pkexec":
        exe = shutil.which("pkexec")
        if not exe:
            raise PrivilegeError("pkexec was not found; install polkit or run as root")
        return exe, True
    if privilege == "sudo":
        exe = shutil.which("sudo")
        if not exe:
            raise PrivilegeError("sudo was not found; install sudo or run as root")
        return exe, False
    if privilege == "root":
        if os.geteuid() != 0:
            raise PrivilegeError(
                "--privilege root requires starting the client as root"
            )
        return "", False
    raise PrivilegeError(f"unknown privilege mode {privilege!r}")


def _stderr_text(proc: subprocess.Popen[bytes]) -> str:
    if proc.stderr is None:
        return ""
    try:
        return proc.stderr.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def open_tun(
    config: netconfig.TunConfig,
    *,
    privilege: str = "auto",
    log: LogFn | None = None,
) -> "netconfig.TunHandle | HelperTun":
    """Create and configure a TUN device.

    If the current process is root the configuration is done in-process.
    Otherwise a root helper is started through pkexec/sudo and receives the
    TUN fd over a Unix socket.
    """
    log = log or (lambda _m: None)
    config.validate()

    if os.geteuid() == 0:
        configurator = netconfig.NetConfigurator(config)
        fd = configurator.up()
        log(f"TUN {configurator.name} configured as root")
        return netconfig.TunHandle(fd=fd, name=configurator.name, configurator=configurator)

    helper = find_helper()
    if not helper:
        raise PrivilegeError(
            "the privileged helper is not installed. Run './tools/install.sh' "
            "from the pdanet-linux directory, or start the client as root."
        )

    launcher, capture = _choose_launcher(privilege)
    token = secrets.token_hex(32)
    runtime = _runtime_dir()
    socket_path = os.path.join(runtime, f"helper-{os.getpid()}-{secrets.token_hex(4)}.sock")
    if len(socket_path) > 100:
        socket_path = os.path.join(runtime, f"h-{secrets.token_hex(4)}.sock")

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    os.chmod(socket_path, 0o600)
    server.listen(1)
    server.settimeout(0.5)

    cmd = [launcher, helper, "--socket", socket_path, "--token", token]
    kwargs: dict = {}
    if capture:
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        server.close()
        _unlink(socket_path)
        raise PrivilegeError(f"cannot start privileged helper: {exc}") from exc

    conn: socket.socket | None = None
    try:
        deadline = time.monotonic() + 90.0
        while True:
            try:
                conn, _ = server.accept()
                break
            except TimeoutError:
                if proc.poll() is not None:
                    detail = _stderr_text(proc)
                    raise PrivilegeError(
                        "the privileged helper exited before connecting"
                        + (f": {detail}" if detail else "")
                    )
                if time.monotonic() >= deadline:
                    raise PrivilegeError(
                        "timed out waiting for the privileged helper "
                        "(was authentication cancelled?)"
                    )
        conn.settimeout(30.0)
        hello = _read_line(conn)
        if not hello.startswith(b"HELLO "):
            raise PrivilegeError("unexpected helper greeting")
        received = hello[6:].decode(errors="replace")
        if not hmac.compare_digest(received, token):
            raise PrivilegeError("helper authentication failed")

        payload = {
            "name": config.name,
            "address": config.address,
            "mtu": config.mtu,
            "route_all": config.route_all,
            "dns": list(config.dns),
            "block_microsoft": config.block_microsoft,
            "role": config.role,
            "server_nat": config.server_nat,
            "bypass": [
                {
                    "address": route.address,
                    "device": route.device,
                    "gateway": route.gateway,
                }
                for route in config.bypass
            ],
        }
        conn.sendall(b"CONFIG " + json.dumps(payload).encode() + b"\n")
        reply = _read_line(conn)
        if not reply.startswith(b"OK "):
            message = reply.decode("utf-8", "replace")
            if message.startswith("ERR "):
                try:
                    message = json.loads(message[4:]).get("message", message)
                except (ValueError, AttributeError):
                    pass
            raise PrivilegeError(f"helper failed: {message}")
        fd = _recv_fd(conn)
        info = json.loads(reply[3:].decode())
        name = str(info.get("ifname") or config.name)
        log(f"TUN {name} configured via {os.path.basename(launcher)} helper")
        return HelperTun(fd=fd, name=name, conn=conn, proc=proc)
    except Exception:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        if capture and proc.stderr is not None:
            err = _stderr_text(proc)
            if err:
                log("helper stderr: " + err)
        raise
    finally:
        server.close()
        _unlink(socket_path)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def status() -> dict[str, object]:
    return {
        "euid": os.geteuid(),
        "helper": find_helper(),
        "pkexec": shutil.which("pkexec"),
        "sudo": shutil.which("sudo"),
        "tun": os.path.exists("/dev/net/tun"),
        "tun_access": os.access("/dev/net/tun", os.R_OK | os.W_OK),
    }
