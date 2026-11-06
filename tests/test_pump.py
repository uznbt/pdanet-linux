import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux.protocol import Command, ClientHeader, ServerHeader
from pdanet_linux.pump import (
    RateLimiter,
    Stats,
    TunnelPump,
    format_bytes,
    format_duration,
)
from pdanet_linux.session import PdaNetSession


def _recv_exact(sock, size, timeout=2.0):
    sock.settimeout(timeout)
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


class PumpTests(unittest.TestCase):
    def setUp(self):
        self.net_client, self.net_phone = socket.socketpair()
        self.tun_a, self.tun_b = socket.socketpair()
        self.addCleanup(self.net_client.close)
        self.addCleanup(self.net_phone.close)
        self.addCleanup(self.tun_a.close)
        self.addCleanup(self.tun_b.close)
        self.session = PdaNetSession(self.net_client, transport=256, model="x")
        self.pump = TunnelPump(self.session, self.tun_a.fileno())
        self.thread = threading.Thread(target=self.pump.run, daemon=True)

    def tearDown(self):
        self.pump.stop()
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def test_bidirectional_relay(self):
        self.thread.start()
        ip_out = bytes.fromhex("4500001400000000401100000a01130208080808")
        self.tun_b.sendall(ip_out)

        header = ClientHeader.unpack(_recv_exact(self.net_phone, 32))
        self.assertEqual(header.command, Command.RAW_IP)
        self.assertEqual(_recv_exact(self.net_phone, header.length), ip_out)

        ip_in = bytes.fromhex("4500001400000000400600000808080a0a011302")
        self.net_phone.sendall(
            ServerHeader(command=Command.RAW_IP, length=len(ip_in)).pack() + ip_in
        )
        self.assertEqual(_recv_exact(self.tun_b, len(ip_in)), ip_in)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.pump.stats.received_packets < 1:
            time.sleep(0.01)
        self.assertEqual(self.pump.stats.sent_packets, 1)
        self.assertEqual(self.pump.stats.sent_bytes, len(ip_out))
        self.assertEqual(self.pump.stats.received_packets, 1)
        self.assertEqual(self.pump.stats.received_bytes, len(ip_in))

    def test_status_message_is_logged_not_forwarded(self):
        logs = []
        self.pump.log = logs.append
        self.thread.start()
        ip = b"\x45" + b"\x00" * 19
        self.net_phone.sendall(
            ServerHeader(command=Command.MESSAGE, length=4).pack() + b"ping"
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any("ping" in line for line in logs):
            time.sleep(0.01)
        self.assertTrue(any("message" in line for line in logs))
        # The TUN side received nothing.
        self.tun_b.settimeout(0.1)
        with self.assertRaises(TimeoutError):
            self.tun_b.recv(64)

    def test_stop_terminates_run(self):
        self.thread.start()
        time.sleep(0.05)
        self.pump.stop()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def test_phone_disconnect_stops_pump(self):
        self.thread.start()
        time.sleep(0.05)
        self.net_phone.close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())


class StatsTests(unittest.TestCase):
    def test_format_helpers(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(2048), "2.0 KiB")
        self.assertEqual(format_duration(65), "1:05")
        self.assertEqual(format_duration(3725), "1:02:05")

    def test_stats_reset_and_summary(self):
        stats = Stats()
        stats.reset()
        stats.sent_bytes = 1024
        stats.sent_packets = 2
        stats.received_bytes = 512
        stats.received_packets = 1
        self.assertIn("sent 1.0 KiB (2 pkt)", stats.summary())
        self.assertIn("recv 512 B (1 pkt)", stats.summary())


class RateLimiterTests(unittest.TestCase):
    def test_consume_with_tokens(self):
        now = [0.0]
        sleeps = []

        def clock():
            return now[0]

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        limiter = RateLimiter(
            1000.0, capacity=1000.0, clock=clock, sleep=sleep
        )
        limiter.consume(500)
        self.assertEqual(limiter.tokens, 500)
        limiter.consume(600)
        self.assertEqual(sleeps, [0.1])
        self.assertEqual(limiter.tokens, 0)

    def test_consume_more_than_capacity(self):
        now = [0.0]
        sleeps = []

        def clock():
            return now[0]

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        limiter = RateLimiter(
            1000.0, capacity=1000.0, clock=clock, sleep=sleep
        )
        limiter.consume(2500)
        self.assertEqual(limiter.capacity, 2500.0)
        self.assertEqual(limiter.tokens, 0)
        self.assertAlmostEqual(sum(sleeps), 1.5)

    def test_rejects_non_positive_rate(self):
        with self.assertRaises(ValueError):
            RateLimiter(0)
        with self.assertRaises(ValueError):
            RateLimiter(-1)

    def test_ignore_empty_consume(self):
        limiter = RateLimiter(1000.0, capacity=100.0)
        limiter.consume(0)
        self.assertEqual(limiter.tokens, 100.0)


if __name__ == "__main__":
    unittest.main()
