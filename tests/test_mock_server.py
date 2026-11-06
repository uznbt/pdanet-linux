import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux.mock_server import MockPdaNetServer
from pdanet_linux.protocol import Command, Transport, build_init, build_model, build_raw_ip, build_shutdown
from pdanet_linux.session import PdaNetSession


class MockServerTests(unittest.TestCase):
    def setUp(self):
        self.server = MockPdaNetServer(host="127.0.0.1", port=0, echo=True).start()

    def tearDown(self):
        self.server.stop()

    def test_handshake_and_echo(self):
        sock = socket.create_connection(("127.0.0.1", self.server.bound_port), timeout=2.0)
        with sock:
            session = PdaNetSession(sock, transport=Transport.USB, model="TestLinux")
            session.handshake(send_model=True)

            ip_packet = bytes([0x45, 0x00, 0x00, 0x14, 0x00, 0x01, 0x00, 0x00, 64, 1, 0, 0,
                               10, 1, 19, 2, 8, 8, 8, 8])
            session.send_raw_ip(ip_packet)

            # Receive echo from server
            data = sock.recv(4096)
            packets = session.feed(data)
            self.assertEqual(len(packets), 1)
            self.assertEqual(packets[0].command, Command.RAW_IP)
            self.assertEqual(packets[0].payload, ip_packet)

            session.close()

        self.assertEqual(self.server.transport_flag, int(Transport.USB))
        self.assertIn("TestLinux", self.server.models)
        self.assertEqual(len(self.server.received), 1)
        self.assertEqual(self.server.received[0], ip_packet)


if __name__ == "__main__":
    unittest.main()

