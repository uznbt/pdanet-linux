import os
import socket
import struct
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux import bt, transport
from pdanet_linux.protocol import Transport, build_init


SAMPLE_DEVICES = """\
Device 7C:2A:DB:B9:3D:65 Redmi Note 9 Pro
Device D8:0F:B5:15:A7:E1 huafit S12(ID-A7E1)
"""

SAMPLE_INFO = """\
Device 7C:2A:DB:B9:3D:65 (public)
\tName: Redmi Note 9 Pro
\tAlias: Redmi Note 9 Pro
\tPaired: yes
\tBonded: yes
\tTrusted: yes
\tBlocked: no
\tConnected: yes
\tLegacyPairing: no
\tUUID: Vendor specific           (00002a0b-0000-1000-8000-00805f9b34fb)
\tUUID: Audio Source              (0000110a-0000-1000-8000-00805f9b34fb)
\tModalias: bluetooth:v038Fp1200d1436
"""


class DeviceParsingTests(unittest.TestCase):
    def test_parse_devices(self):
        devices = bt.parse_devices(SAMPLE_DEVICES)
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0].address, "7C:2A:DB:B9:3D:65")
        self.assertEqual(devices[0].name, "Redmi Note 9 Pro")
        self.assertTrue(devices[0].paired)

    def test_parse_info(self):
        info = bt.parse_info(SAMPLE_INFO)
        self.assertEqual(info.address, "7C:2A:DB:B9:3D:65")
        self.assertEqual(info.name, "Redmi Note 9 Pro")
        self.assertTrue(info.paired)
        self.assertTrue(info.trusted)
        self.assertTrue(info.connected)
        self.assertTrue(info.has_pdanet_service)
        self.assertIn("0000110a-0000-1000-8000-00805f9b34fb", info.uuids)

    def test_parse_info_without_pdanet_uuid(self):
        info = bt.parse_info(SAMPLE_INFO.replace("00002a0b", "0000ffff"))
        self.assertFalse(info.has_pdanet_service)

    def test_select_device_by_address(self):
        target = bt.BluetoothDevice(
            address="7C:2A:DB:B9:3D:65", name="Redmi", paired=True
        )
        with mock.patch.object(bt, "device_info", return_value=target) as info:
            self.assertEqual(
                bt.select_device("7c:2a:db:b9:3d:65"), target
            )
        info.assert_called_once_with("7c:2a:db:b9:3d:65")

    def test_select_device_by_name(self):
        target = bt.BluetoothDevice(
            address="7C:2A:DB:B9:3D:65", name="Redmi Note 9 Pro", paired=True
        )
        with mock.patch.object(bt, "list_devices", return_value=[target]), \
             mock.patch.object(bt, "device_info", return_value=target):
            self.assertEqual(bt.select_device("redmi"), target)

    def test_select_device_rejects_ambiguous_name(self):
        a = bt.BluetoothDevice(address="11:22:33:44:55:66", name="Phone A")
        b = bt.BluetoothDevice(address="11:22:33:44:55:77", name="Phone B")
        with mock.patch.object(bt, "list_devices", return_value=[a, b]):
            with self.assertRaises(bt.BluetoothError):
                bt.select_device("phone")

    def test_select_device_single_paired_default(self):
        target = bt.BluetoothDevice(
            address="7C:2A:DB:B9:3D:65", name="Redmi", paired=True
        )
        with mock.patch.object(bt, "list_devices", return_value=[target]), \
             mock.patch.object(bt, "device_info", return_value=target):
            self.assertEqual(bt.select_device(), target)


class SdpTests(unittest.TestCase):
    @staticmethod
    def _u16(value):
        return b"\x09" + struct.pack(">H", value)

    @staticmethod
    def _u8(value):
        return b"\x08" + bytes([value])

    def _response(self, channel):
        l2cap = bt._de_sequence(bt._de_uuid16(0x0100) + self._u16(3))
        rfcomm = bt._de_sequence(bt._de_uuid16(0x0003) + self._u8(channel))
        protocol_list = l2cap + rfcomm
        attribute = bt._de_sequence(self._u16(0x0004) + bt._de_sequence(protocol_list))
        record = bt._de_sequence(attribute)
        attribute_list = bt._de_sequence(record)
        return (
            b"\x07"
            + struct.pack(">HH", 1, 4 + len(attribute_list))
            + b"\x00\x00"
            + struct.pack(">H", len(attribute_list))
            + attribute_list
        )

    def test_request_contains_uuid(self):
        request = bt._sdp_request(bt._de_uuid(bt.PDANET_UUID))
        self.assertEqual(request[0], 0x06)
        self.assertIn(bytes.fromhex("00002a0b"), request)
        # Attribute IDs requested: ProtocolDescriptorList (0x0004)
        # and ServiceName (0x0100).
        self.assertIn(bytes.fromhex("090004090100"), request)

    def test_parse_response_channel(self):
        self.assertEqual(bt.parse_sdp_response(self._response(7)), 7)

    def test_parse_response_rejects_bad_pdu(self):
        self.assertIsNone(bt.parse_sdp_response(b"\x07\x00"))
        self.assertIsNone(bt.parse_sdp_response(b"\x06" + b"\x00" * 20))

    def test_parse_response_without_protocol_list(self):
        attribute = bt._de_sequence(self._u16(0x0100) + bt._de_sequence(b"\x00"))
        record = bt._de_sequence(attribute)
        attribute_list = bt._de_sequence(record)
        response = (
            b"\x07"
            + struct.pack(">HH", 1, 4 + len(attribute_list))
            + b"\x00\x00"
            + struct.pack(">H", len(attribute_list))
            + attribute_list
        )
        self.assertIsNone(bt.parse_sdp_response(response))


class SocketTests(unittest.TestCase):
    def test_open_rfcomm_explicit_channel(self):
        fake = mock.Mock()
        with mock.patch.object(bt.socket, "socket", return_value=fake):
            sock, channel = bt.open_rfcomm(
                "7C:2A:DB:B9:3D:65", channel=3, timeout=1
            )
        self.assertIs(sock, fake)
        self.assertEqual(channel, 3)
        fake.connect.assert_called_once_with(("7C:2A:DB:B9:3D:65", 3))
        fake.settimeout.assert_any_call(None)

    def test_open_rfcomm_rejects_bad_address(self):
        with self.assertRaises(bt.BluetoothError):
            bt.open_rfcomm("not-an-address", channel=1)

    def test_connect_bluetooth_wraps_errors(self):
        with mock.patch.object(
            bt, "select_device", side_effect=bt.BluetoothError("no device")
        ):
            with self.assertRaises(transport.TransportError):
                transport.connect_bluetooth()

    def test_connect_bluetooth_returns_endpoint(self):
        chosen = bt.BluetoothDevice(
            address="7C:2A:DB:B9:3D:65", name="Redmi", paired=True
        )
        fake = mock.Mock()
        with mock.patch.object(bt, "select_device", return_value=chosen), \
             mock.patch.object(bt, "ensure_paired", return_value=chosen) as paired, \
             mock.patch.object(bt, "ensure_connected", return_value=chosen), \
             mock.patch.object(bt, "open_rfcomm", return_value=(fake, 2)):
            endpoint = transport.connect_bluetooth("7C:2A:DB:B9:3D:65")
        self.assertIs(endpoint.sock, fake)
        self.assertIn("Bluetooth", endpoint.description)
        self.assertIn("RFCOMM 2", endpoint.description)
        paired.assert_called_once_with(chosen, log=mock.ANY)


class AdapterStateTests(unittest.TestCase):
    def test_parse_show(self):
        output = (
            "Controller 58:A0:23:99:6D:11 (public)\n"
            "\tPowered: yes\n"
            "\tDiscoverable: no\n"
            "\tPairable: yes\n"
            "\tDiscovering: no\n"
        )
        with mock.patch.object(
            bt, "_run_bluetoothctl",
            return_value=subprocess.CompletedProcess(
                ["bluetoothctl"], 0, output, ""
            ),
        ):
            state = bt.adapter_state()
        self.assertEqual(state["Controller"], "58:A0:23:99:6D:11")
        self.assertEqual(state["Powered"], "yes")
        self.assertEqual(state["Discoverable"], "no")
        self.assertEqual(state["Pairable"], "yes")


class DiscoverAndPairTests(unittest.TestCase):
    def test_scan_merges_advertised_and_known_devices(self):
        scan_output = (
            "\x1b[0;92m[NEW]\x1b[0m Device 7C:2A:DB:B9:3D:65 "
            "Redmi Note 9 Pro\n"
            "[NEW] Device 11:22:33:44:55:66 Nearby Phone\n"
        )
        redmi = bt.BluetoothDevice(
            address="7C:2A:DB:B9:3D:65", name="Redmi Note 9 Pro", paired=True
        )
        nearby = bt.BluetoothDevice(
            address="11:22:33:44:55:66", name="Nearby Phone"
        )
        known = bt.BluetoothDevice(
            address="AA:BB:CC:DD:EE:FF", name="Old Phone", paired=True
        )
        with mock.patch.object(
            bt, "_run_bluetoothctl",
            return_value=subprocess.CompletedProcess(
                ["bluetoothctl"], 0, scan_output, ""
            ),
        ), mock.patch.object(bt, "list_devices", return_value=[known]), \
             mock.patch.object(bt, "device_info", side_effect=[redmi, nearby, known]):
            devices = bt.scan_devices(duration=1)

        self.assertEqual(
            {d.address for d in devices},
            {
                "7C:2A:DB:B9:3D:65",
                "AA:BB:CC:DD:EE:FF",
                "11:22:33:44:55:66",
            },
        )
        self.assertTrue(devices[0].paired)
        self.assertTrue(devices[1].paired)
        self.assertFalse(devices[2].paired)

    def test_scan_logs_device_details(self):
        scan_output = "Device 7C:2A:DB:B9:3D:65 Redmi Note 9 Pro\n"
        redmi = bt.BluetoothDevice(
            address="7C:2A:DB:B9:3D:65",
            name="Redmi Note 9 Pro",
            paired=True,
            trusted=True,
            connected=False,
            uuids=(str(bt.PDANET_UUID),),
        )
        unpaired = bt.BluetoothDevice(
            address="11:22:33:44:55:66", name="Nearby Phone"
        )
        logs: list[str] = []
        with mock.patch.object(
            bt, "_run_bluetoothctl",
            return_value=subprocess.CompletedProcess(
                ["bluetoothctl"], 0, scan_output, ""
            ),
        ), mock.patch.object(
            bt, "list_devices", return_value=[unpaired]
        ), mock.patch.object(
            bt, "device_info", side_effect=[redmi, unpaired]
        ):
            bt.scan_devices(duration=1, log=logs.append)

        text = "\n".join(logs)
        self.assertIn("discovery returned 1 address(es)", text)
        self.assertIn("BlueZ knows 1 device(s)", text)
        self.assertIn("Redmi Note 9 Pro (7C:2A:DB:B9:3D:65)", text)
        self.assertIn("paired", text)
        self.assertIn("trusted", text)
        self.assertIn("PdaNet service advertised", text)
        self.assertIn("Nearby Phone (11:22:33:44:55:66)", text)
        self.assertIn("no PdaNet service in SDP", text)
        self.assertIn("found 2 Bluetooth device(s): 1 paired, 1 unpaired", text)

    def test_scan_logs_bluetoothctl_errors_and_hints(self):
        logs: list[str] = []
        with mock.patch.object(
            bt, "_run_bluetoothctl",
            return_value=subprocess.CompletedProcess(
                ["bluetoothctl"],
                1,
                "",
                "Failed to start discovery: org.bluez.Error.NotReady",
            ),
        ), mock.patch.object(bt, "list_devices", return_value=[]):
            devices = bt.scan_devices(duration=1, log=logs.append)

        self.assertEqual(devices, [])
        text = "\n".join(logs)
        self.assertIn("exited with status 1", text)
        self.assertIn("NotReady", text)
        self.assertIn("no advertisement seen", text)
        self.assertIn("found 0 Bluetooth device(s): 0 paired, 0 unpaired", text)

    def test_describe_device(self):
        device = bt.BluetoothDevice(
            address="7C:2A:DB:B9:3D:65",
            name="Redmi",
            paired=True,
            trusted=False,
            connected=True,
            uuids=(str(bt.PDANET_UUID),),
        )
        text = bt.describe_device(device)
        self.assertIn("paired", text)
        self.assertIn("untrusted", text)
        self.assertIn("connected", text)
        self.assertIn("PdaNet service advertised", text)

    def test_pair_device_pairs_and_trusts(self):
        unpaired = bt.BluetoothDevice(
            address="7C:2A:DB:B9:3D:65", name="Redmi", paired=False
        )
        paired = bt.BluetoothDevice(
            address="7C:2A:DB:B9:3D:65", name="Redmi", paired=True, trusted=False
        )
        trusted = bt.BluetoothDevice(
            address="7C:2A:DB:B9:3D:65", name="Redmi", paired=True, trusted=True
        )
        with mock.patch.object(
            bt, "device_info", side_effect=[unpaired, paired, trusted]
        ), mock.patch.object(
            bt, "_run_bluetoothctl",
            return_value=subprocess.CompletedProcess(["bluetoothctl"], 0, "", ""),
        ) as run:
            result = bt.pair_device(unpaired)

        self.assertTrue(result.trusted)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any("pair" in call for call in calls))
        self.assertTrue(any("trust" in call for call in calls))
        self.assertTrue(any("NoInputNoOutput" in call for call in calls))

    def test_pair_device_reports_failure(self):
        unpaired = bt.BluetoothDevice(
            address="7C:2A:DB:B9:3D:65", name="Redmi", paired=False
        )
        with mock.patch.object(bt, "device_info", return_value=unpaired), \
             mock.patch.object(
                 bt, "_run_bluetoothctl",
                 return_value=subprocess.CompletedProcess(
                     ["bluetoothctl"], 1, "", "Failed to pair: authentication failed"
                 ),
             ):
            with self.assertRaises(bt.BluetoothError) as raised:
                bt.pair_device(unpaired)
        self.assertIn("pair", str(raised.exception).lower())

    def test_ensure_paired_leaves_paired_device_alone(self):
        paired = bt.BluetoothDevice(
            address="7C:2A:DB:B9:3D:65", name="Redmi", paired=True
        )
        with mock.patch.object(bt, "pair_device") as pair:
            self.assertIs(bt.ensure_paired(paired), paired)
        pair.assert_not_called()


class BluetoothInitTests(unittest.TestCase):
    def test_init_flag_is_four(self):
        packet = build_init(Transport.BLUETOOTH)
        self.assertEqual(packet[:4], b"\x00\x00\x00\x0f")
        self.assertEqual(packet[-4:], b"\x00\x00\x00\x04")
        self.assertEqual(Transport.BLUETOOTH, 4)


if __name__ == "__main__":
    unittest.main()
