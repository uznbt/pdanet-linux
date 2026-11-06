import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux.protocol import (
    ClientHeader,
    Command,
    ServerHeader,
    Transport,
    build_init,
    build_model,
    build_raw_ip,
    parse_ipv4_addresses,
)


class ProtocolTests(unittest.TestCase):
    def test_init_wifi_golden_bytes(self):
        # The Android tablet client sends a=swap(15), h=swap(64) through a
        # little-endian serialiser, which is exactly this big-endian layout.
        expected = (
            "0000000f" + "00000000" * 6 + "00000040"
        )
        self.assertEqual(build_init(Transport.WIFI).hex(), expected)

    def test_init_usb_golden_bytes(self):
        expected = "0000000f" + "00000000" * 6 + "00000100"
        self.assertEqual(build_init(Transport.USB).hex(), expected)

    def test_model_packet(self):
        packet = build_model("Linux")
        self.assertEqual(packet[:4].hex(), "0000012e")  # 302 big endian
        self.assertEqual(packet[12:16].hex(), "0000000a")  # 10 bytes
        self.assertEqual(packet[32:], "Linux".encode("utf-16le"))
        self.assertEqual(len(packet), 32 + 10)

    def test_raw_ip_packet(self):
        ip = bytes.fromhex(
            "4500001400000000401100000a01130208080808"
        )
        packet = build_raw_ip(ip)
        self.assertEqual(packet[:4].hex(), "00000040")
        self.assertEqual(packet[12:16].hex(), "00000014")
        self.assertEqual(packet[32:], ip)

    def test_client_header_roundtrip(self):
        header = ClientHeader(command=64, length=1500, flags=256)
        self.assertEqual(ClientHeader.unpack(header.pack()), header)
        self.assertEqual(header.pack()[:4], b"\x00\x00\x00\x40")

    def test_server_header_is_little_endian(self):
        header = ServerHeader(command=Command.RAW_IP, length=1500)
        wire = header.pack()
        self.assertEqual(wire[:4], b"\x40\x00\x00\x00")
        self.assertEqual(wire[16:20], b"\xdc\x05\x00\x00")
        self.assertEqual(ServerHeader.unpack(wire), header)

    def test_parse_ipv4(self):
        ip = bytes.fromhex(
            "4500001400000000401100000a01130208080808"
        )
        self.assertEqual(parse_ipv4_addresses(ip), ("10.1.19.2", "8.8.8.8", 17))
        self.assertIsNone(parse_ipv4_addresses(b"nope"))


if __name__ == "__main__":
    unittest.main()
