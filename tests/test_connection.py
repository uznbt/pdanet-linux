import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux.connection import ConnectOptions, ConnectionError_, PdaNetConnection
from pdanet_linux.mock_server import MockPdaNetServer
from pdanet_linux.protocol import Transport
from pdanet_linux.pump import RateLimiter


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.server = MockPdaNetServer(host="127.0.0.1", port=0, echo=True).start()

    def tearDown(self):
        self.server.stop()

    def test_dry_run_wifi(self):
        logs = []
        options = ConnectOptions(
            mode="wifi",
            host="127.0.0.1",
            port=self.server.bound_port,
            dry_run=True,
            model="ConnTest",
            log=logs.append,
        )
        conn = PdaNetConnection(options)
        try:
            conn.open()
            self.assertTrue(conn.description.startswith("Wi-Fi 127.0.0.1"))
            self.assertEqual(conn.tun_name, "")

            # Wait for server thread to process handshake frames
            import time
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not self.server.models:
                time.sleep(0.05)

            self.assertIn("ConnTest", self.server.models)
            self.assertEqual(self.server.transport_flag, int(Transport.WIFI))

            messages = conn.dry_run_messages(0.1)
            self.assertIsInstance(messages, list)
        finally:
            conn.close()

    def test_unknown_mode(self):
        options = ConnectOptions(mode="carrier-pigeon", dry_run=True)
        conn = PdaNetConnection(options)
        with self.assertRaises(ConnectionError_):
            conn.open()

    def test_wifi_without_host(self):
        options = ConnectOptions(mode="wifi", host=None, dry_run=True)
        conn = PdaNetConnection(options)
        with self.assertRaises(ConnectionError_):
            conn.open()

    def test_set_throttle_updates_running_pump(self):
        options = ConnectOptions(throttle_kbps=0)
        conn = PdaNetConnection(options)
        conn.pump = SimpleNamespace(limiter=None)
        conn.set_throttle(512)
        self.assertEqual(conn.options.throttle_kbps, 512)
        self.assertIsInstance(conn.pump.limiter, RateLimiter)
        self.assertAlmostEqual(conn.pump.limiter.rate, 64000.0)
        conn.set_throttle(0)
        self.assertIsNone(conn.pump.limiter)
        self.assertEqual(conn.options.throttle_kbps, 0)


if __name__ == "__main__":
    unittest.main()
