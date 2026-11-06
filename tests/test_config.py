import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux import config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"

    def test_defaults_when_missing(self):
        settings = config.load(self.path)
        self.assertEqual(settings.mode, "usb")
        self.assertFalse(settings.auto_connect_usb)
        self.assertTrue(settings.close_to_tray)
        self.assertEqual(settings.wifi_port, 8740)

    def test_round_trip_and_permissions(self):
        settings = config.Settings(
            auto_connect_usb=True,
            throttle_kbps=2048,
            block_microsoft=True,
            wifi_share_ssid="My Share",
        )
        config.save(settings, self.path)
        loaded = config.load(self.path)
        self.assertTrue(loaded.auto_connect_usb)
        self.assertEqual(loaded.throttle_kbps, 2048)
        self.assertTrue(loaded.block_microsoft)
        self.assertEqual(loaded.wifi_share_ssid, "My Share")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["throttle_kbps"], 2048)

    def test_close_to_tray_round_trip(self):
        config.save(config.Settings(close_to_tray=False), self.path)
        self.assertFalse(config.load(self.path).close_to_tray)
        config.save(config.Settings(close_to_tray=True), self.path)
        self.assertTrue(config.load(self.path).close_to_tray)

    def test_corrupt_file_falls_back(self):
        self.path.write_text("{not json")
        self.assertEqual(config.load(self.path), config.Settings())
        self.path.write_text("[]")
        self.assertEqual(config.load(self.path), config.Settings())

    def test_validation_clamps_and_repairs(self):
        settings = config.Settings(
            mode="carrier-pigeon",
            privilege="maybe",
            throttle_kbps=-4,
            wifi_port=99999,
            mtu=10,
            dns=" 8.8.8.8 ",
        ).validated()
        self.assertEqual(settings.mode, "usb")
        self.assertEqual(settings.privilege, "auto")
        self.assertEqual(settings.throttle_kbps, 0)
        self.assertEqual(settings.wifi_port, 8740)
        self.assertEqual(settings.mtu, 1500)
        self.assertEqual(settings.dns, "8.8.8.8")

    def test_throttle_upper_bound(self):
        settings = config.Settings(throttle_kbps=10_000_000).validated()
        self.assertEqual(settings.throttle_kbps, 100_000)

    def test_unknown_keys_ignored(self):
        self.path.write_text(
            json.dumps({"auto_connect_usb": True, "future_option": 42})
        )
        settings = config.load(self.path)
        self.assertTrue(settings.auto_connect_usb)

    def test_diagnostic_log(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name}):
            config.append_log("[12:00:00] connected")
            target = config.log_path()
            self.assertEqual(
                target, Path(self.tmp.name) / "pdanet-linux" / "pdanet.log"
            )
            self.assertIn("connected", target.read_text())
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
