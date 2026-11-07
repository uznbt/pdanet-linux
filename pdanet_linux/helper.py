"""Privileged TUN helper.

The helper is started by the unprivileged application through ``pkexec``
(or ``sudo``).  It connects back to a Unix socket owned by the user,
creates the TUN interface, configures addresses/routes/DNS, passes the TUN
file descriptor over ``SCM_RIGHTS`` and then waits.  When the application
closes the control socket (normal exit or crash), the helper removes the
routes and the interface.

The helper deliberately accepts only a small, validated JSON config.  It
never executes a shell.
"""

from __future__ import annotations

import argparse
import array
import json
import os
import re
import socket
import sys
import traceback

from . import netconfig


def _read_line(conn: socket.socket, limit: int = 65536) -> bytes:
    data = bytearray()
    while len(data) < limit:
        chunk = conn.recv(1)
        if not chunk:
            raise EOFError("control socket closed")
        if chunk == b"\n":
            return bytes(data)
        data.extend(chunk)
    raise ValueError("control line too long")


def _send_json(conn: socket.socket, prefix: str, value: dict) -> None:
    conn.sendall((prefix + " " + json.dumps(value) + "\n").encode())


def _config_from_json(raw: dict) -> netconfig.TunConfig:
    bypass = [
        netconfig.BypassRoute(
            address=str(item["address"]),
            device=item.get("device"),
            gateway=item.get("gateway"),
        )
        for item in raw.get("bypass", [])
    ]
    return netconfig.TunConfig(
        name=str(raw.get("name", netconfig.DEFAULT_TUN)),
        address=str(raw.get("address", netconfig.DEFAULT_ADDRESS)),
        mtu=int(raw.get("mtu", netconfig.DEFAULT_MTU)),
        route_all=bool(raw.get("route_all", True)),
        dns=tuple(str(item) for item in raw.get("dns", netconfig.DEFAULT_DNS)),
        bypass=bypass,
        block_microsoft=bool(raw.get("block_microsoft", False)),
        role=str(raw.get("role", netconfig.ROLE_CLIENT)),
        server_nat=bool(raw.get("server_nat", True)),
    ).validate()


def serve(socket_path: str, token: str) -> int:
    if os.geteuid() != 0:
        print("pdanet helper must run as root", file=sys.stderr)
        return 1
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(socket_path)
    except OSError as exc:
        print(f"pdanet helper: cannot connect to {socket_path}: {exc}", file=sys.stderr)
        return 1

    configurator: netconfig.NetConfigurator | None = None
    try:
        conn.sendall(f"HELLO {token}\n".encode())
        line = _read_line(conn).decode()
        if not line.startswith("CONFIG "):
            raise ValueError("expected CONFIG message")
        config = _config_from_json(json.loads(line[len("CONFIG "):]))
        configurator = netconfig.NetConfigurator(config)
        fd = configurator.up()
        _send_json(
            conn,
            "OK",
            {
                "ifname": configurator.name,
                "address": config.address,
                "mtu": config.mtu,
                "route_all": config.route_all,
                "role": config.role,
            },
        )
        # Send exactly one byte plus the fd.  The parent uses recvmsg().
        conn.sendmsg(
            [b"F"],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))],
        )
        os.close(fd)
        # The configurator must not close this fd number again: the helper may
        # reuse it for an unrelated file and the cleanup would close the wrong
        # descriptor.  The interface is removed explicitly on disconnect.
        configurator.fd = -1
        # Keep the interface alive until the user process disconnects.
        while True:
            data = conn.recv(4096)
            if not data:
                break
        return 0
    except Exception as exc:
        try:
            _send_json(conn, "ERR", {"message": str(exc)})
        except OSError:
            pass
        print(f"pdanet helper: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        if configurator is not None:
            configurator.down()
        try:
            conn.close()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PdaNet Linux privileged helper")
    parser.add_argument("--socket", required=True, help="user control socket")
    parser.add_argument("--token", required=True, help="one-time authentication token")
    args = parser.parse_args(argv)
    # The token is part of a line protocol; restrict it to hex.
    if not re.fullmatch(r"[0-9a-f]{32,128}", args.token):
        print("pdanet helper: invalid token", file=sys.stderr)
        return 2
    return serve(args.socket, args.token)


if __name__ == "__main__":
    raise SystemExit(main())
