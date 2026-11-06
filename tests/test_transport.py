import os
import socket
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux import transport


def _completed(stdout="", stderr="", returncode=0):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


DEVICES_OUTPUT = """List of devices attached
R58M123ABC\tdevice product:beyond1lte model:SM_G973F device:beyond1 transport_id:1
emulator-5554\tunauthorized transport_id:2
"""


class AdbTests(unittest.TestCase):
    def test_list_devices_parsing(self):
        with mock.patch.object(
            transport, "_run_adb", return_value=_completed(DEVICES_OUTPUT)
        ):
            devices = transport.list_devices()
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0].serial, "R58M123ABC")
        self.assertEqual(devices[0].state, "device")
        self.assertEqual(devices[0].model, "SM_G973F")
        self.assertEqual(devices[0].label, "SM G973F (R58M123ABC)")
        self.assertEqual(devices[1].state, "unauthorized")

    def test_list_devices_error(self):
        with mock.patch.object(
            transport, "_run_adb", return_value=_completed("", "adb server died", 1)
        ):
            with self.assertRaises(transport.TransportError):
                transport.list_devices()

    def test_choose_only_device(self):
        with mock.patch.object(
            transport, "list_devices", return_value=[
                transport.AdbDevice("abc", "device")
            ]
        ):
            self.assertEqual(transport.choose_device().serial, "abc")

    def test_choose_none(self):
        with mock.patch.object(transport, "list_devices", return_value=[]):
            with self.assertRaises(transport.TransportError) as ctx:
                transport.choose_device()
            self.assertIn("no adb device", str(ctx.exception))

    def test_choose_unauthorized(self):
        with mock.patch.object(
            transport, "list_devices", return_value=[
                transport.AdbDevice("abc", "unauthorized")
            ]
        ):
            with self.assertRaises(transport.TransportError) as ctx:
                transport.choose_device()
            self.assertIn("approve", str(ctx.exception))

    def test_choose_multiple_needs_serial(self):
        devices = [
            transport.AdbDevice("a", "device"),
            transport.AdbDevice("b", "device"),
        ]
        with mock.patch.object(transport, "list_devices", return_value=devices):
            with self.assertRaises(transport.TransportError) as ctx:
                transport.choose_device()
            self.assertIn("multiple", str(ctx.exception))
            self.assertEqual(transport.choose_device("b").serial, "b")

    def test_choose_unknown_serial(self):
        with mock.patch.object(
            transport, "list_devices", return_value=[
                transport.AdbDevice("a", "device")
            ]
        ):
            with self.assertRaises(transport.TransportError):
                transport.choose_device("zzz")


class DiscoveryTests(unittest.TestCase):
    def test_discovery_request_layout(self):
        request = transport._discovery_request("Linux")
        self.assertEqual(len(request), 64)
        marker, _, _ = struct.unpack_from("<III", request, 0)
        self.assertEqual(marker, 5014)
        self.assertEqual(request[32:37], b"Linux")

    def test_discovery_request_truncates_long_model(self):
        request = transport._discovery_request("x" * 100)
        self.assertEqual(request[32:63], b"x" * 31)
        self.assertEqual(request[63], 0)

    def test_parse_discovery(self):
        data = bytearray(64)
        struct.pack_into("<I", data, 0, 5014)
        data[8:12] = socket.inet_aton("192.168.49.5")
        data[12:29] = b"aa:bb:cc:dd:ee:ff"
        data[32:40] = b"Phone123"
        result = transport._parse_discovery(bytes(data), "192.168.49.1")
        self.assertIsNotNone(result)
        self.assertEqual(result.host, "192.168.49.1")
        self.assertEqual(result.name, "Phone123")
        self.assertEqual(result.client_ip, "192.168.49.5")
        self.assertEqual(result.mac, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(result.marker, 5014)

    def test_parse_discovery_rejects_bad_packets(self):
        self.assertIsNone(transport._parse_discovery(b"\x00" * 10, "1.2.3.4"))
        wrong = bytearray(64)
        struct.pack_into("<I", wrong, 0, 12345)
        self.assertIsNone(transport._parse_discovery(bytes(wrong), "1.2.3.4"))

    def test_parse_discovery_zero_client_ip(self):
        data = bytearray(64)
        struct.pack_into("<I", data, 0, 5014)
        result = transport._parse_discovery(bytes(data), "1.2.3.4")
        self.assertEqual(result.client_ip, "")

class WifiConnectTests(unittest.TestCase):
    def test_connect_wifi(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        self.addCleanup(server.close)
        endpoint = transport.connect_wifi("127.0.0.1", port, timeout=2)
        self.addCleanup(endpoint.close)
        peer, _ = server.accept()
        self.addCleanup(peer.close)
        self.assertEqual(endpoint.bypass_host, "127.0.0.1")
        self.assertIn(f"127.0.0.1:{port}", endpoint.description)
        endpoint.sock.sendall(b"hello")
        self.assertEqual(peer.recv(5), b"hello")

    def test_connect_wifi_failure(self):
        with self.assertRaises(transport.TransportError):
            transport.connect_wifi("127.0.0.1", 1, timeout=0.2)


class UsbConnectTests(unittest.TestCase):
    def test_connect_usb_uses_first_working_forward(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.addCleanup(listener.close)

        calls = []

        def fake_adb(args, timeout=20.0):
            calls.append(args)
            if "forward" in args and "tcp:8739" in args:
                return _completed("", "cannot bind", 1)
            return _completed()

        from pdanet_linux import transport as mod

        with mock.patch.object(mod, "choose_device", return_value=mod.AdbDevice("abc", "device")), \
             mock.patch.object(mod, "_run_adb", side_effect=fake_adb), \
             mock.patch.object(mod, "_free_tcp_port", return_value=port):
            endpoint = mod.connect_usb(local_port=port, log=lambda _m: None)
            # The client connects to 127.0.0.1:port, which is our listener, so
            # accept it to avoid a dangling connection.
            peer, _ = listener.accept()
            self.addCleanup(peer.close)
            self.addCleanup(endpoint.close)
            self.assertIn("localabstract:pdanet", endpoint.description)
            endpoint.close()
        flat = [" ".join(c) for c in calls]
        self.assertTrue(any("localabstract:pdanet" in c for c in flat))
        self.assertTrue(any("--remove" in c for c in flat))

    def test_connect_usb_all_attempts_fail(self):
        def fake_adb(args, timeout=20.0):
            return _completed("", "no device", 1)

        with mock.patch.object(
            transport, "choose_device", return_value=transport.AdbDevice("abc", "device")
        ), mock.patch.object(transport, "_run_adb", side_effect=fake_adb):
            with self.assertRaises(transport.TransportError) as ctx:
                transport.connect_usb(local_port=0, log=lambda _m: None)
            self.assertIn("could not reach", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
