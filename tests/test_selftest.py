import os
import socket
import struct
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux import selftest


class DnsPacketTests(unittest.TestCase):
    def test_query_layout(self):
        txid, packet = selftest._dns_query("example.com")
        self.assertEqual(txid, 0x5044)
        self.assertEqual(packet[:2], b"PD")
        self.assertEqual(struct.unpack(">HHHHHH", packet[:12])[1:3], (0x0100, 1))
        self.assertIn(b"\x07example\x03com\x00", packet)
        self.assertTrue(packet.endswith(b"\x00\x00\x01\x00\x01"))

    def test_udp_dns_against_local_server(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        self.addCleanup(server.close)

        def serve():
            data, peer = server.recvfrom(512)
            reply = data[:2] + b"\x81\x80" + b"\x00\x01\x00\x00\x00\x00\x00\x00"
            server.sendto(reply, peer)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        detail = selftest.check_udp_dns(
            "127.0.0.1", "example.com", timeout=2.0, port=port
        )
        thread.join(timeout=2)
        self.assertIn("12 byte reply", detail)
        self.assertIn("127.0.0.1", detail)


class CheckHelpersTests(unittest.TestCase):
    def test_format_and_all_ok(self):
        results = [
            selftest.CheckResult("Tunnel", True, "interface pdanet0", 1.0),
            selftest.CheckResult("DNS", False, "no route", 2.0),
        ]
        report = selftest.format_report(results)
        self.assertIn("[OK] Tunnel: interface pdanet0", report)
        self.assertIn("[FAIL] DNS: no route", report)
        self.assertFalse(selftest.all_ok(results))
        self.assertTrue(selftest.all_ok(results[:1]))
        self.assertFalse(selftest.all_ok([]))

    def test_check_tunnel_finds_interface(self):
        with mock.patch.object(
            selftest.socket, "if_nameindex", return_value=[(1, "lo"), (2, "pdanet0")]
        ):
            self.assertEqual(selftest.check_tunnel(), "interface pdanet0")

    def test_check_tunnel_reports_missing(self):
        with mock.patch.object(
            selftest.socket, "if_nameindex", return_value=[(1, "lo")]
        ):
            with self.assertRaises(RuntimeError):
                selftest.check_tunnel()

    def test_timed_marks_failure(self):
        def boom():
            raise RuntimeError("nope")

        result = selftest._timed("Failing", boom)
        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "nope")


if __name__ == "__main__":
    unittest.main()
