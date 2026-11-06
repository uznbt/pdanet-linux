import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux import wifi


class TerseTests(unittest.TestCase):
    def test_split_terse_escapes(self):
        fields = wifi.split_terse(r"a\:b:c\\d:e")
        self.assertEqual(fields, ["a:b", "c\\d", "e"])

    def test_interfaces_filters_wifi(self):
        output = "wlan0:wifi:connected\nlo:loopback:connected\np2p0:wifi-p2p:unavailable\n"
        with mock.patch.object(wifi, "_run", return_value=output):
            found = wifi.interfaces()
        self.assertEqual([item.name for item in found], ["wlan0"])


class ScanTests(unittest.TestCase):
    def test_scan_deduplicates_and_sorts(self):
        output = (
            "*:Phone\\:5G:88:WPA2\n"
            " :Neighbor:44:--\n"
            ":Phone\\:5G:60:WPA2\n"
            "::10:\n"
        )
        calls = []

        def fake_run(args, timeout=60.0):
            calls.append(args)
            return output

        with mock.patch.object(wifi, "_run", side_effect=fake_run):
            networks = wifi.scan("wlan0")
        self.assertEqual([n.ssid for n in networks], ["Phone:5G", "Neighbor"])
        self.assertTrue(networks[0].active)
        self.assertEqual(networks[0].signal, 88)
        self.assertEqual(networks[1].security, "--")
        self.assertIn("ifname", calls[0])
        self.assertIn("wlan0", calls[0])
        self.assertEqual(calls[0][-2:], ["--rescan", "yes"])

    def test_scan_empty(self):
        with mock.patch.object(wifi, "_run", return_value=""):
            self.assertEqual(wifi.scan(), [])


class ConnectTests(unittest.TestCase):
    def test_connect_builds_nmcli_command(self):
        calls = []

        def fake_run(args, timeout=60.0):
            calls.append(args)
            return "Device 'wlan0' successfully activated"

        with mock.patch.object(wifi, "_run", side_effect=fake_run):
            output = wifi.connect("Phone", "secret123", interface="wlan0")
        self.assertIn("successfully", output)
        self.assertEqual(calls[0][:4], ["--wait", "60", "device", "wifi"])
        self.assertIn("Phone", calls[0])
        self.assertIn("password", calls[0])
        self.assertIn("secret123", calls[0])
        self.assertIn("wlan0", calls[0])

    def test_connect_rejects_empty_ssid(self):
        with self.assertRaises(wifi.WifiError):
            wifi.connect("  ")

    def test_gateway_parses_nmcli_field(self):
        with mock.patch.object(
            wifi, "_run", return_value="IP4.GATEWAY:192.168.49.1\n"
        ):
            self.assertEqual(wifi.gateway("wlan0"), "192.168.49.1")

    def test_gateway_missing(self):
        with mock.patch.object(wifi, "_run", return_value="IP4.GATEWAY:\n"):
            with self.assertRaises(wifi.WifiError):
                wifi.gateway("wlan0")


class ShareTests(unittest.TestCase):
    def test_share_start_creates_ap_with_shared_ipv4(self):
        calls = []

        def fake_run(args, timeout=60.0):
            calls.append(list(args))
            return ""

        with mock.patch.object(wifi, "_run", side_effect=fake_run), mock.patch.object(
            wifi, "_supports_ap", return_value=True
        ):
            wifi.share_start("My Share", "password1", interface="wlan0")
        add = [call for call in calls if call[:2] == ["connection", "add"]][0]
        self.assertIn("802-11-wireless.mode", add)
        self.assertIn("ap", add)
        self.assertIn("ipv4.method", add)
        self.assertIn("shared", add)
        self.assertEqual(calls[-1], ["connection", "up", wifi.SHARE_NAME])

    def test_share_start_validation(self):
        with self.assertRaises(wifi.WifiError):
            wifi.share_start("", "password1")
        with self.assertRaises(wifi.WifiError):
            wifi.share_start("x" * 33, "password1")
        with self.assertRaises(wifi.WifiError):
            wifi.share_start("Share", "short")

    def test_share_start_rejects_ap_unsupported(self):
        with mock.patch.object(wifi, "_supports_ap", return_value=False):
            with self.assertRaises(wifi.WifiError):
                wifi.share_start("Share", "password1", interface="wlan0")

    def test_share_start_cleans_profile_when_up_fails(self):
        calls = []

        def fake_run(args, timeout=60.0):
            calls.append(list(args))
            if args[:2] == ["connection", "up"]:
                raise wifi.WifiError("AP failed")
            return ""

        with mock.patch.object(wifi, "_run", side_effect=fake_run), mock.patch.object(
            wifi, "_supports_ap", return_value=True
        ):
            with self.assertRaises(wifi.WifiError):
                wifi.share_start("Share", "password1", interface="wlan0")
        self.assertIn(["connection", "delete", wifi.SHARE_NAME], calls)

    def test_share_active(self):
        output = "pdanet-share:wlan0:activated\nWired:eth0:activated\n"
        with mock.patch.object(wifi, "_run", return_value=output):
            self.assertTrue(wifi.share_active())
        with mock.patch.object(wifi, "_run", return_value="Wired:eth0:activated\n"):
            self.assertFalse(wifi.share_active())

    def test_share_stop_down_and_delete(self):
        calls = []

        def fake_run(args, timeout=60.0):
            calls.append(list(args))
            return ""

        with mock.patch.object(wifi, "share_active", return_value=True), mock.patch.object(
            wifi, "_run", side_effect=fake_run
        ):
            self.assertTrue(wifi.share_stop())
        self.assertIn(["connection", "down", wifi.SHARE_NAME], calls)
        self.assertIn(["connection", "delete", wifi.SHARE_NAME], calls)


if __name__ == "__main__":
    unittest.main()
