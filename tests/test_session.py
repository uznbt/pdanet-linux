import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux.protocol import (
    HEADER_SIZE,
    Command,
    ServerHeader,
    Transport,
    ClientHeader,
)
from pdanet_linux.session import PdaNetError, PdaNetSession, describe_packet


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.client, self.phone = socket.socketpair()
        self.addCleanup(self.client.close)
        self.addCleanup(self.phone.close)
        self.messages = []
        self.session = PdaNetSession(
            self.client,
            transport=Transport.USB,
            model="Testbox",
            log=self.messages.append,
        )

    def _read_exact(self, size):
        data = b""
        while len(data) < size:
            chunk = self.phone.recv(size - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def test_handshake_wire_bytes(self):
        self.session.handshake()
        init = self._read_exact(32)
        model_header = self._read_exact(32)
        model_payload = self._read_exact(len("Testbox".encode("utf-16le")))

        self.assertEqual(ClientHeader.unpack(init).command, Command.INIT)
        self.assertEqual(ClientHeader.unpack(init).flags, int(Transport.USB))
        self.assertEqual(ClientHeader.unpack(model_header).command, Command.MODEL)
        self.assertEqual(ClientHeader.unpack(model_header).length, 14)
        self.assertEqual(model_payload.decode("utf-16le"), "Testbox")
        self.assertEqual(len(self.messages), 2)

    def test_handshake_without_model(self):
        self.session.handshake(send_model=False)
        init = self._read_exact(32)
        self.assertEqual(ClientHeader.unpack(init).command, Command.INIT)
        self.phone.settimeout(0.05)
        with self.assertRaises(TimeoutError):
            self.phone.recv(1)

    def test_send_raw_ip(self):
        ip = bytes.fromhex("4500001400000000401100000a01130208080808")
        self.session.send_raw_ip(ip)
        header = ClientHeader.unpack(self._read_exact(HEADER_SIZE))
        payload = self._read_exact(header.length)
        self.assertEqual(header.command, Command.RAW_IP)
        self.assertEqual(payload, ip)

    def test_shutdown_sends_type_16(self):
        self.session.shutdown()
        header = ClientHeader.unpack(self._read_exact(HEADER_SIZE))
        self.assertEqual(header.command, Command.SHUTDOWN)

    def test_feed_partial_and_multiple_packets(self):
        ip1 = b"\x45" + b"\x00" * 19
        ip2 = b"\x45" + b"\x01" * 19
        wire = (
            ServerHeader(command=Command.RAW_IP, length=len(ip1)).pack()
            + ip1
            + ServerHeader(command=Command.MESSAGE, length=3).pack()
            + b"abc"
        )
        # Split in the middle of the first header and again mid payload.
        self.assertEqual(self.session.feed(wire[:10]), [])
        self.assertEqual(self.session.feed(wire[10:40]), [])
        packets = self.session.feed(wire[40:])
        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[0].command, Command.RAW_IP)
        self.assertEqual(packets[0].payload, ip1)
        self.assertEqual(packets[1].command, Command.MESSAGE)
        self.assertEqual(packets[1].payload, b"abc")

    def test_feed_rejects_absurd_length(self):
        wire = ServerHeader(command=Command.RAW_IP, length=1 << 20).pack() + b"x"
        with self.assertRaises(PdaNetError):
            self.session.feed(wire)

    def test_recv_packets_closed_socket(self):
        self.phone.close()
        with self.assertRaises(PdaNetError):
            self.session.recv_packets(timeout=0.5)

    def test_describe_packet(self):
        raw = bytes.fromhex("4500001400000000401100000a01130208080808")
        packet = PdaNetSession.feed(
            self.session, ServerHeader(command=Command.RAW_IP, length=len(raw)).pack() + raw
        )[0]
        description = describe_packet(packet)
        self.assertIn("raw IP", description)
        self.assertIn("10.1.19.2", description)
        self.assertIn("8.8.8.8", description)
        message = PdaNetSession.feed(
            self.session, ServerHeader(command=Command.MESSAGE, length=2).pack() + b"hi"
        )[0]
        self.assertEqual(describe_packet(message), "message: 'hi'")

    def test_send_after_close_raises(self):
        self.session.close()
        with self.assertRaises(PdaNetError):
            self.session.send_raw_ip(b"\x45" + b"\x00" * 19)


if __name__ == "__main__":
    unittest.main()
