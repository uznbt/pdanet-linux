import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux.autoconnect import UsbAutoConnect


class AutoConnectTests(unittest.TestCase):
    def test_first_update_only_primes(self):
        auto = UsbAutoConnect(enabled=True)
        self.assertIsNone(auto.update(["phone-1"]))
        self.assertIsNone(auto.update(["phone-1"]))

    def test_new_device_triggers_when_enabled(self):
        auto = UsbAutoConnect(enabled=True)
        auto.update([])
        self.assertEqual(auto.update(["phone-1"]), "phone-1")
        self.assertIsNone(auto.update(["phone-1"]))

    def test_disabled_does_not_trigger(self):
        auto = UsbAutoConnect(enabled=False)
        auto.update([])
        self.assertIsNone(auto.update(["phone-1"]))
        auto.set_enabled(True)
        self.assertIsNone(auto.update(["phone-1"]))
        self.assertEqual(auto.update(["phone-1", "phone-2"]), "phone-2")

    def test_enabling_does_not_fire_for_present_device(self):
        auto = UsbAutoConnect(enabled=False)
        auto.update(["phone-1"])
        auto.set_enabled(True)
        self.assertIsNone(auto.update(["phone-1"]))

    def test_unplug_then_replug_is_new(self):
        auto = UsbAutoConnect(enabled=True)
        auto.update(["phone-1"])
        auto.update([])
        self.assertEqual(auto.update(["phone-1"]), "phone-1")

    def test_forget_allows_retry(self):
        auto = UsbAutoConnect(enabled=True)
        auto.update([])
        self.assertEqual(auto.update(["phone-1"]), "phone-1")
        auto.forget("phone-1")
        self.assertEqual(auto.update(["phone-1"]), "phone-1")

    def test_multiple_new_devices_sorted(self):
        auto = UsbAutoConnect(enabled=True)
        auto.update([])
        self.assertEqual(auto.update(["b", "a"]), "a")


if __name__ == "__main__":
    unittest.main()
