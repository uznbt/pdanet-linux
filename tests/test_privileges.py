import array
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from pdanet_linux import helper, privileges


class LineTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def test_read_line(self):
        self.b.sendall(b"HELLO abc\n")
        self.assertEqual(privileges._read_line(self.a), b"HELLO abc")

    def test_read_line_eof(self):
        self.b.close()
        with self.assertRaises(privileges.PrivilegeError):
            privileges._read_line(self.a)

    def test_read_line_too_long(self):
        self.b.sendall(b"x" * 32)
        with self.assertRaises(privileges.PrivilegeError):
            privileges._read_line(self.a, limit=16)

    def test_recv_fd(self):
        fd = os.open("/dev/null", os.O_RDONLY)
        self.addCleanup(os.close, fd)
        self.b.sendmsg(
            [b"F"],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))],
        )
        received = privileges._recv_fd(self.a)
        self.addCleanup(os.close, received)
        self.assertGreaterEqual(received, 0)
        self.assertEqual(os.read(received, 1), b"")

    def test_recv_fd_without_ancdata(self):
        self.b.sendall(b"F")
        with self.assertRaises(privileges.PrivilegeError):
            privileges._recv_fd(self.a)


class HelperConfigTests(unittest.TestCase):
    def test_config_from_json(self):
        raw = {
            "name": "pdanet9",
            "address": "10.1.19.2/24",
            "mtu": 1400,
            "route_all": True,
            "dns": ["8.8.8.8"],
            "bypass": [
                {"address": "192.168.49.1", "device": "wlan0", "gateway": "192.168.49.1"}
            ],
        }
        config = helper._config_from_json(raw)
        self.assertEqual(config.name, "pdanet9")
        self.assertEqual(config.mtu, 1400)
        self.assertEqual(config.dns, ("8.8.8.8",))
        self.assertEqual(config.bypass[0].device, "wlan0")
        self.assertFalse(config.block_microsoft)

    def test_config_from_json_block_microsoft(self):
        config = helper._config_from_json(
            {"address": "10.1.19.2/24", "block_microsoft": True}
        )
        self.assertTrue(config.block_microsoft)

    def test_config_rejects_wrong_address(self):
        with self.assertRaises(Exception):
            helper._config_from_json({"address": "10.1.19.3/24"})

    def test_config_rejects_bad_bypass(self):
        with self.assertRaises(Exception):
            helper._config_from_json(
                {"bypass": [{"address": "not-an-ip"}]}
            )

    def test_main_rejects_invalid_token(self):
        with mock.patch.object(helper, "serve") as serve:
            rc = helper.main(["--socket", "/tmp/x", "--token", "bad token"])
        self.assertEqual(rc, 2)
        serve.assert_not_called()


class HelperServeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.socket_path = os.path.join(self.tmp.name, "helper.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.socket_path)
        self.server.listen(1)
        self.server.settimeout(3)
        self.addCleanup(self.server.close)
        self.token = "a" * 64
        self.down_called = False
        self.fake_fd = os.open("/dev/null", os.O_RDWR)
        outer = self

        class FakeConfigurator:
            def __init__(self, config):
                self.config = config
                self.name = "pdtest0"
                self.fd = outer.fake_fd

            def up(self):
                return self.fd

            def down(self):
                outer.down_called = True
                if self.fd >= 0:
                    try:
                        os.close(self.fd)
                    except OSError:
                        pass
                    self.fd = -1

        self.fake_configurator = FakeConfigurator
        self.result = None
        self.thread = threading.Thread(target=self._run_helper, daemon=True)

    def _run_helper(self):
        with mock.patch.object(helper.os, "geteuid", return_value=0), \
             mock.patch.object(helper.netconfig, "NetConfigurator",
                               self.fake_configurator):
            self.result = helper.serve(self.socket_path, self.token)

    def _line(self, conn):
        data = bytearray()
        while True:
            chunk = conn.recv(1)
            if not chunk:
                raise EOFError
            if chunk == b"\n":
                return bytes(data)
            data.extend(chunk)

    def test_full_control_protocol_and_fd_passing(self):
        self.thread.start()
        conn, _ = self.server.accept()
        self.addCleanup(conn.close)
        conn.settimeout(3)
        hello = self._line(conn)
        self.assertEqual(hello, b"HELLO " + self.token.encode())
        config = {
            "name": "pdtest0",
            "address": "10.1.19.2/24",
            "mtu": 1500,
            "route_all": True,
            "dns": ["8.8.8.8"],
            "bypass": [],
        }
        conn.sendall(b"CONFIG " + json.dumps(config).encode() + b"\n")
        reply = self._line(conn)
        self.assertTrue(reply.startswith(b"OK "), reply)
        info = json.loads(reply[3:])
        self.assertEqual(info["ifname"], "pdtest0")

        received = privileges._recv_fd(conn)
        self.addCleanup(os.close, received)
        self.assertEqual(os.read(received, 1), b"")

        conn.close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.result, 0)
        self.assertTrue(self.down_called)

    def test_bad_config_returns_error(self):
        self.thread.start()
        conn, _ = self.server.accept()
        self.addCleanup(conn.close)
        conn.settimeout(3)
        self._line(conn)
        conn.sendall(b"CONFIG {\"address\": \"10.1.19.3/24\"}\n")
        reply = self._line(conn)
        self.assertTrue(reply.startswith(b"ERR "), reply)
        conn.close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.result, 1)


class HelperCandidatesTests(unittest.TestCase):
    def test_explicit_helper_env(self):
        with mock.patch.dict(os.environ, {"PDANET_HELPER": "/bin/sh"}):
            self.assertIn("/bin/sh", privileges.helper_candidates())

    def test_missing_helper_env_is_ignored(self):
        with mock.patch.dict(os.environ, {"PDANET_HELPER": "/does/not/exist"}):
            self.assertNotIn("/does/not/exist", privileges.helper_candidates())

    def test_status_shape(self):
        with mock.patch.object(
            privileges, "find_helper", return_value="/usr/bin/true"
        ), mock.patch.object(
            privileges, "_sudo_passwordless", return_value=True
        ):
            info = privileges.status()
        for key in (
            "euid",
            "helper",
            "pkexec",
            "sudo",
            "passwordless",
            "tun",
            "tun_access",
        ):
            self.assertIn(key, info)
        self.assertTrue(info["passwordless"])


class LauncherTests(unittest.TestCase):
    def test_sudo_passwordless_detects_nopasswd(self):
        completed = subprocess.CompletedProcess(
            ["sudo"], 0, stdout="Options: !authenticate\n", stderr=""
        )
        with mock.patch.object(
            privileges.shutil, "which", return_value="/usr/bin/sudo"
        ), mock.patch.object(
            privileges.subprocess, "run", return_value=completed
        ) as run:
            self.assertTrue(privileges._sudo_passwordless("/x/helper"))
        args, kwargs = run.call_args
        self.assertEqual(
            args[0], ["/usr/bin/sudo", "-n", "-ll", "/x/helper"]
        )
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["text"])
        self.assertFalse(kwargs["check"])

    def test_sudo_passwordless_rejects_rule_that_needs_a_password(self):
        completed = subprocess.CompletedProcess(
            ["sudo"], 0, stdout="(ALL) ALL\n", stderr=""
        )
        with mock.patch.object(
            privileges.shutil, "which", return_value="/usr/bin/sudo"
        ), mock.patch.object(
            privileges.subprocess, "run", return_value=completed
        ):
            self.assertFalse(privileges._sudo_passwordless("/x/helper"))

    def test_sudo_passwordless_handles_failure(self):
        completed = subprocess.CompletedProcess(
            ["sudo"], 1, stdout="", stderr="password is required"
        )
        with mock.patch.object(
            privileges.shutil, "which", return_value="/usr/bin/sudo"
        ), mock.patch.object(
            privileges.subprocess, "run", return_value=completed
        ):
            self.assertFalse(privileges._sudo_passwordless("/x/helper"))

    def test_sudo_passwordless_without_sudo(self):
        with mock.patch.object(privileges.shutil, "which", return_value=None):
            self.assertFalse(privileges._sudo_passwordless("/x/helper"))

    def test_sudo_passwordless_handles_timeout(self):
        with mock.patch.object(
            privileges.shutil, "which", return_value="/usr/bin/sudo"
        ), mock.patch.object(
            privileges.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["sudo"], 10),
        ):
            self.assertFalse(privileges._sudo_passwordless("/x/helper"))

    @staticmethod
    def _which(name):
        return {
            "sudo": "/usr/bin/sudo",
            "pkexec": "/usr/bin/pkexec",
        }.get(name)

    def test_auto_prefers_passwordless_helper(self):
        with mock.patch.object(
            privileges, "_sudo_passwordless", return_value=True
        ), mock.patch.object(
            privileges.shutil, "which", side_effect=self._which
        ), mock.patch.dict(
            os.environ, {"DISPLAY": ":0"}, clear=False
        ):
            argv, capture = privileges._choose_launcher("auto", "/x/helper")
        self.assertEqual(argv, ["/usr/bin/sudo", "-n"])
        self.assertFalse(capture)

    def test_auto_uses_pkexec_in_a_gui_without_passwordless(self):
        with mock.patch.object(
            privileges, "_sudo_passwordless", return_value=False
        ), mock.patch.object(
            privileges.shutil, "which", side_effect=self._which
        ), mock.patch.dict(
            os.environ, {"DISPLAY": ":0"}, clear=False
        ):
            argv, capture = privileges._choose_launcher("auto", "/x/helper")
        self.assertEqual(argv, ["/usr/bin/pkexec"])
        self.assertTrue(capture)

    def test_explicit_passwordless_uses_non_interactive_sudo(self):
        with mock.patch.object(
            privileges.shutil, "which", side_effect=self._which
        ):
            argv, capture = privileges._choose_launcher("passwordless")
        self.assertEqual(argv, ["/usr/bin/sudo", "-n"])
        self.assertFalse(capture)

    def test_root_launcher_has_no_prefix(self):
        with mock.patch.object(privileges.os, "geteuid", return_value=0):
            argv, capture = privileges._choose_launcher("root")
        self.assertEqual(argv, [])
        self.assertFalse(capture)


if __name__ == "__main__":
    unittest.main()
