import os
import socket
import struct
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux import protocol, server
from pdanet_linux.protocol import (
    ClientHeader,
    Command,
    ServerHeader,
    Transport,
    build_discovery_reply,
    build_init,
    build_model,
    build_raw_ip,
    build_server_raw_ip,
)


def _packet(source="10.1.19.2", destination="8.8.8.8") -> bytes:
    src = bytes(int(part) for part in source.split("."))
    dst = bytes(int(part) for part in destination.split("."))
    return bytes.fromhex("450000140000000040110000") + src + dst


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("connection closed")
        data.extend(chunk)
    return bytes(data)


class ProtocolServerModeTests(unittest.TestCase):
    def test_server_raw_ip_golden_bytes(self):
        packet = _packet()
        frame = build_server_raw_ip(packet, sequence=3)
        self.assertEqual(frame[:4], b"\x40\x00\x00\x00")
        self.assertEqual(struct.unpack_from("<I", frame, 16)[0], len(packet))
        self.assertEqual(struct.unpack_from("<I", frame, 12)[0], 3)
        self.assertEqual(frame[32:], packet)

    def test_server_raw_ip_rejects_non_ipv4(self):
        with self.assertRaises(ValueError):
            build_server_raw_ip(b"not an ip packet")

    def test_discovery_reply_layout(self):
        reply = build_discovery_reply(
            client_ip="10.42.0.7",
            server_ip="10.42.0.1",
            mac="AA:BB:CC:DD:EE:FF",
            name="Laptop",
        )
        self.assertEqual(len(reply), 64)
        self.assertEqual(struct.unpack_from("<I", reply, 0)[0], 5014)
        self.assertEqual(reply[4:8], socket.inet_aton("10.42.0.1"))
        self.assertEqual(reply[8:12], socket.inet_aton("10.42.0.7"))
        self.assertEqual(
            reply[12:29].rstrip(b"\x00"), b"AA:BB:CC:DD:EE:FF"
        )
        self.assertEqual(reply[32:].rstrip(b"\x00"), b"Laptop")

    def test_parse_client_packet(self):
        raw = ClientHeader(command=Command.MODEL, length=4).pack() + b"test"
        packet = protocol.parse_client_packet(raw)
        self.assertEqual(packet.command, Command.MODEL)
        self.assertEqual(packet.payload, b"test")
        with self.assertRaises(ValueError):
            protocol.parse_client_packet(raw[:-1])


class _ServerFixture(unittest.TestCase):
    def setUp(self):
        self.tun_local, self.tun_peer = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_SEQPACKET
        )
        self.tun_peer.settimeout(5.0)
        self.srv = server.PdaNetServer(
            self.tun_local.fileno(),
            host="127.0.0.1",
            port=0,
            discovery_port=0,
            server_ip="127.0.0.1",
            mac="AA:BB:CC:DD:EE:FF",
            name="Test Laptop",
            allowed_networks=["127.0.0.0/8"],
        )
        self.srv.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.srv.stop()
        for sock in (self.tun_local, self.tun_peer):
            try:
                sock.close()
            except OSError:
                pass

    def _connect(self):
        phone = socket.create_connection(
            ("127.0.0.1", self.srv.bound_port), timeout=5.0
        )
        phone.settimeout(5.0)
        self.addCleanup(phone.close)
        self.assertTrue(_wait_until(lambda: self.srv.client_address != ""))
        return phone


class DiscoveryTests(_ServerFixture):
    def test_discovery_reply_is_compatible_with_phone_client(self):
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(client.close)
        client.bind(("127.0.0.1", 0))
        client.settimeout(5.0)
        request = bytearray(64)
        struct.pack_into("<I", request, 0, 5014)
        request[32:37] = b"Phone"
        client.sendto(bytes(request), ("127.0.0.1", self.srv.discovery_bound_port))
        reply, _addr = client.recvfrom(128)
        self.assertEqual(len(reply), 64)
        self.assertEqual(struct.unpack_from("<I", reply, 0)[0], 5014)
        self.assertEqual(reply[4:8], socket.inet_aton("127.0.0.1"))
        self.assertEqual(reply[8:12], socket.inet_aton("127.0.0.1"))
        self.assertTrue(_wait_until(lambda: self.srv.stats.discovery_requests == 1))
        self.assertFalse(self.srv.stats.connected)


class RelayTests(_ServerFixture):
    def test_phone_init_model_and_raw_ip_reach_tun(self):
        phone = self._connect()
        packet = _packet()
        phone.sendall(build_init(Transport.WIFI))
        phone.sendall(build_model("Test Phone"))
        phone.sendall(build_raw_ip(packet))
        self.assertEqual(self.tun_peer.recv(65535), packet)
        self.assertTrue(
            _wait_until(lambda: self.srv.stats.client_model == "Test Phone")
        )
        self.assertTrue(
            _wait_until(lambda: self.srv.stats.received_packets == 1)
        )
        self.assertEqual(self.srv.stats.received_bytes, len(packet))

    def test_tun_packet_is_wrapped_for_phone(self):
        phone = self._connect()
        phone.sendall(build_init(Transport.WIFI))
        packet = _packet(source="10.1.19.2", destination="1.1.1.1")
        self.tun_peer.sendall(packet)
        header = _recv_exact(phone, 32)
        decoded = ServerHeader.unpack(header)
        self.assertEqual(decoded.command, Command.RAW_IP)
        self.assertEqual(decoded.length, len(packet))
        self.assertEqual(_recv_exact(phone, len(packet)), packet)
        self.assertTrue(_wait_until(lambda: self.srv.stats.sent_packets == 1))

    def test_packet_from_wrong_source_is_dropped(self):
        phone = self._connect()
        phone.sendall(build_init(Transport.WIFI))
        phone.sendall(build_raw_ip(_packet(source="10.1.19.99")))
        with self.assertRaises(TimeoutError):
            self.tun_peer.recv(65535)
        self.assertEqual(self.srv.stats.received_packets, 0)

    def test_stop_closes_client_and_ports(self):
        phone = self._connect()
        port = self.srv.bound_port
        self.srv.stop()
        self.assertFalse(self.srv.running)
        self.assertEqual(phone.recv(1), b"")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5)


class NetworkFilterTests(unittest.TestCase):
    def test_address_filter(self):
        networks = server._networks(["10.42.0.0/24", "192.168.1.0/24"])
        self.assertTrue(server._address_allowed("10.42.0.9", networks))
        self.assertFalse(server._address_allowed("10.42.1.9", networks))
        self.assertTrue(server._address_allowed("192.168.1.2", networks))
        self.assertFalse(server._address_allowed("8.8.8.8", networks))

    def test_default_is_loopback_only(self):
        networks = server._networks(None)
        self.assertTrue(server._address_allowed("127.0.0.1", networks))
        self.assertFalse(server._address_allowed("192.168.1.10", networks))


if __name__ == "__main__":
    unittest.main()
