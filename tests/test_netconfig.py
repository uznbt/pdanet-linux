import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux import netconfig


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["ip"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class ValidationTests(unittest.TestCase):
    def test_validate_name(self):
        self.assertEqual(netconfig.validate_name("pdanet0"), "pdanet0")
        for bad in ("", "a" * 16, "bad name", "x;rm", "../x"):
            with self.assertRaises(netconfig.NetConfigError):
                netconfig.validate_name(bad)

    def test_default_config(self):
        config = netconfig.TunConfig()
        self.assertEqual(config.validate().address, "10.1.19.2/24")

    def test_address_must_match_phone_expectation(self):
        with self.assertRaises(netconfig.NetConfigError):
            netconfig.TunConfig(address="10.1.19.3/24").validate()

    def test_mtu_bounds(self):
        with self.assertRaises(netconfig.NetConfigError):
            netconfig.TunConfig(mtu=20).validate()
        netconfig.TunConfig(mtu=1500).validate()

    def test_rule_preferences_precede_main_table(self):
        # main is consulted at preference 32766; a larger number would make
        # the policy table unreachable whenever a normal default route exists.
        self.assertLess(netconfig.LAN_RULE_PREF, netconfig.RULE_PREF)
        self.assertLess(netconfig.RULE_PREF, 32766)

    def test_dns_validation(self):
        with self.assertRaises(ValueError):
            netconfig.TunConfig(dns=("not-an-ip",)).validate()

    def test_bypass_validation(self):
        with self.assertRaises(netconfig.NetConfigError):
            netconfig.BypassRoute(address="192.168.1.1", device="bad dev").validate()

    def test_server_config_requires_server_address_and_no_default_route(self):
        config = netconfig.TunConfig(
            role=netconfig.ROLE_SERVER,
            address=netconfig.SERVER_ADDRESS,
            route_all=False,
            dns=(),
        ).validate()
        self.assertEqual(config.role, netconfig.ROLE_SERVER)
        with self.assertRaises(netconfig.NetConfigError):
            netconfig.TunConfig(
                role=netconfig.ROLE_SERVER,
                address="10.1.19.2/24",
                route_all=False,
            ).validate()
        with self.assertRaises(netconfig.NetConfigError):
            netconfig.TunConfig(
                role=netconfig.ROLE_SERVER,
                address=netconfig.SERVER_ADDRESS,
                route_all=True,
            ).validate()

    def test_make_server_config_ignores_client_defaults(self):
        config = netconfig.make_config(
            role=netconfig.ROLE_SERVER, route_all=True, dns=("1.1.1.1",)
        )
        self.assertEqual(config.address, netconfig.SERVER_ADDRESS)
        self.assertFalse(config.route_all)
        self.assertEqual(config.dns, ())
        self.assertFalse(config.block_microsoft)


class RouteGetTests(unittest.TestCase):
    def test_route_with_gateway(self):
        out = "8.8.8.8 via 192.168.1.1 dev wlan0 src 192.168.1.5 uid 1000\n"
        with mock.patch.object(netconfig, "run_ip", return_value=_completed(out)):
            route = netconfig.route_get("8.8.8.8")
        self.assertEqual(route.address, "8.8.8.8")
        self.assertEqual(route.device, "wlan0")
        self.assertEqual(route.gateway, "192.168.1.1")

    def test_route_without_gateway(self):
        out = "192.168.49.1 dev wlan0 src 192.168.49.5 uid 1000\n"
        with mock.patch.object(netconfig, "run_ip", return_value=_completed(out)):
            route = netconfig.route_get("192.168.49.1")
        self.assertIsNone(route.gateway)
        self.assertEqual(route.device, "wlan0")

    def test_route_parse_failure(self):
        with mock.patch.object(netconfig, "run_ip", return_value=_completed("weird\n")):
            with self.assertRaises(netconfig.NetConfigError):
                netconfig.route_get("8.8.8.8")

    def test_route_command_failure(self):
        with mock.patch.object(
            netconfig, "run_ip", return_value=_completed("", "unreachable", 2)
        ):
            with self.assertRaises(netconfig.NetConfigError):
                netconfig.route_get("8.8.8.8")


class ConfiguratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(
            netconfig, "LOCK_PATH", os.path.join(self.tmp.name, "lock")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_global_ipv6_prefixes(self):
        output = """
::1 dev lo proto kernel metric 256 pref medium
fe80::/64 dev wlan0 proto kernel metric 256 pref medium
2001:db8:1::/64 dev wlan0 proto ra metric 1024 pref medium
2001:db8:2::/64 via fe80::1 dev wlan0 proto ra metric 1024 pref medium
default via fe80::1 dev wlan0 proto ra metric 1024 pref medium
2000::/3 dev wlan0 metric 1024 pref medium
unreachable 2001:db8:dead::/48 metric 4278198272 pref medium
"""
        self.assertEqual(
            netconfig._global_ipv6_prefixes(output),
            ["2001:db8:1::/64", "2001:db8:2::/64"],
        )

    def test_ipv6_guard_installs_lan_exceptions_and_block(self):
        calls = []

        def fake_run_ip(*args, check=True):
            calls.append(args)
            if args[:3] == ("-6", "route", "show"):
                return _completed(
                    "2001:db8:1::/64 dev wlan0 proto ra metric 1024\n"
                    "fe80::/64 dev wlan0 proto kernel metric 256\n"
                )
            return _completed()

        configurator = netconfig.NetConfigurator(netconfig.TunConfig())
        with mock.patch.object(netconfig, "run_ip", side_effect=fake_run_ip):
            configurator._configure_ipv6_guard()

        flat = [" ".join(call) for call in calls]
        for network in netconfig.IPV6_LAN_RANGES:
            self.assertTrue(
                any(
                    f"-6 rule add pref {netconfig.LAN_RULE_PREF} "
                    f"to {network} lookup main" in c
                    for c in flat
                )
            )
        self.assertTrue(
            any(
                f"-6 rule add pref {netconfig.LAN_RULE_PREF} "
                "to 2001:db8:1::/64 lookup main" in c
                for c in flat
            )
        )
        self.assertFalse(
            any("fe80::/64 lookup main" in c for c in flat if "rule add" in c)
        )
        self.assertTrue(
            any(
                f"-6 route replace unreachable default table "
                f"{netconfig.ROUTE_TABLE}" in c
                for c in flat
            )
        )
        self.assertTrue(
            any(
                f"-6 rule add pref {netconfig.RULE_PREF} "
                f"lookup {netconfig.ROUTE_TABLE}" in c
                for c in flat
            )
        )
        self.assertTrue(configurator._ipv6_guard)

    def test_ipv6_guard_skips_when_ipv6_is_unavailable(self):
        calls = []

        def fake_run_ip(*args, check=True):
            calls.append(args)
            if args[:3] == ("-6", "route", "show"):
                return _completed("", "Address family not supported", 2)
            return _completed()

        configurator = netconfig.NetConfigurator(netconfig.TunConfig())
        with mock.patch.object(netconfig, "run_ip", side_effect=fake_run_ip):
            configurator._configure_ipv6_guard()

        self.assertEqual(len(calls), 1)
        self.assertFalse(configurator._ipv6_guard)

    def test_up_and_down_commands(self):
        calls = []
        status = iter([0, 0, 0, 0])

        def fake_run_ip(*args, check=True):
            calls.append(args)
            return _completed(returncode=next(status, 0))

        config = netconfig.TunConfig(
            bypass=[netconfig.BypassRoute("192.168.49.1", "wlan0", "192.168.49.1")]
        )
        with mock.patch.object(netconfig, "open_tun_fd", return_value=(55, "pdanet0")), \
             mock.patch.object(netconfig, "run_ip", side_effect=fake_run_ip) as run, \
             mock.patch.object(netconfig, "_resolvectl", return_value=None):
            configurator = netconfig.NetConfigurator(config)
            fd = configurator.up()
            self.assertEqual(fd, 55)
            configurator.down()

        flat = [" ".join(call) for call in calls]
        self.assertTrue(any("link set dev pdanet0 mtu 1500" in c for c in flat))
        self.assertTrue(any("addr replace 10.1.19.2/24 dev pdanet0" in c for c in flat))
        self.assertTrue(any("route replace default dev pdanet0 table 51820" in c for c in flat))
        self.assertTrue(
            any(
                "route replace 192.168.49.1/32 via 192.168.49.1 dev wlan0 table 51820"
                in c
                for c in flat
            )
        )
        self.assertTrue(
            any(
                f"rule add pref {netconfig.RULE_PREF} "
                f"lookup {netconfig.ROUTE_TABLE}" in c
                for c in flat
            )
        )
        self.assertTrue(
            any(
                f"rule add pref {netconfig.LAN_RULE_PREF} "
                "to 10.0.0.0/8 lookup main" in c
                for c in flat
            )
        )
        self.assertTrue(
            any(
                f"rule add pref {netconfig.LAN_RULE_PREF} "
                "to 192.168.0.0/16 lookup main" in c
                for c in flat
            )
        )
        self.assertTrue(any("rule del pref 51820" in c for c in flat))
        self.assertTrue(any("rule del pref 51819" in c for c in flat))
        self.assertTrue(
            any(
                f"rule del pref {netconfig.RULE_PREF}" in c for c in flat
            )
        )
        self.assertTrue(
            any(
                f"rule del pref {netconfig.LAN_RULE_PREF}" in c
                for c in flat
            )
        )
        self.assertTrue(any("route flush table 51820" in c for c in flat))
        self.assertTrue(
            any(
                f"-6 route replace unreachable default table "
                f"{netconfig.ROUTE_TABLE}" in c
                for c in flat
            )
        )
        self.assertTrue(
            any(
                f"-6 rule add pref {netconfig.RULE_PREF} "
                f"lookup {netconfig.ROUTE_TABLE}" in c
                for c in flat
            )
        )
        self.assertTrue(
            any(
                f"-6 rule del pref {netconfig.RULE_PREF}" in c
                for c in flat
            )
        )
        self.assertTrue(
            any(
                f"-6 rule del pref {netconfig.LAN_RULE_PREF}" in c
                for c in flat
            )
        )
        self.assertTrue(any("-6 rule del pref 51820" in c for c in flat))
        self.assertTrue(any("-6 rule del pref 51819" in c for c in flat))
        self.assertTrue(any("-6 route flush table 51820" in c for c in flat))
        self.assertTrue(any("link del pdanet0" in c for c in flat))
        self.assertEqual(configurator._lock_fd, -1)
        self.assertEqual(configurator.fd, -1)

    def test_server_up_and_down_installs_forwarding_and_nat(self):
        ip_calls = []
        ipt_calls = []

        def fake_run_ip(*args, check=True):
            ip_calls.append(args)
            return _completed()

        def fake_iptables(*args, check=True):
            ipt_calls.append(args)
            return _completed()

        config = netconfig.TunConfig(
            role=netconfig.ROLE_SERVER,
            address=netconfig.SERVER_ADDRESS,
            route_all=False,
            dns=(),
        )
        with mock.patch.object(netconfig, "open_tun_fd", return_value=(58, "pdanet0")), \
             mock.patch.object(netconfig, "run_ip", side_effect=fake_run_ip), \
             mock.patch.object(netconfig, "run_iptables", side_effect=fake_iptables), \
             mock.patch.object(
                 netconfig.NetConfigurator, "_read_forwarding", return_value="0"
             ), \
             mock.patch.object(
                 netconfig.NetConfigurator, "_write_forwarding"
             ) as write_forwarding, \
             mock.patch.object(
                 netconfig.NetConfigurator, "_read_forwarding_marker",
                 return_value=None
             ), \
             mock.patch.object(
                 netconfig.NetConfigurator, "_write_forwarding_marker"
             ) as write_marker, \
             mock.patch.object(
                 netconfig.NetConfigurator, "_remove_forwarding_marker"
             ) as remove_marker:
            configurator = netconfig.NetConfigurator(config)
            self.assertEqual(configurator.up(), 58)
            configurator.down()

        flat_ip = [" ".join(call) for call in ip_calls]
        self.assertTrue(any("addr replace 10.1.19.1/24 dev pdanet0" in c for c in flat_ip))
        self.assertFalse(any("route replace default" in c for c in flat_ip))
        self.assertFalse(any("rule add" in c for c in flat_ip))
        self.assertTrue(any("rule del" in c for c in flat_ip))
        self.assertTrue(
            any("route flush table 51820" in c for c in flat_ip)
        )
        self.assertTrue(any("link del pdanet0" in c for c in flat_ip))

        flat_ipt = [" ".join(call) for call in ipt_calls]
        self.assertTrue(
            any(
                "filter -A PDANET_SRV -i pdanet0 -s 10.1.19.2/32 -j ACCEPT"
                in c
                for c in flat_ipt
            )
        )
        self.assertTrue(
            any(
                "filter -I FORWARD 1 -j PDANET_SRV" in c for c in flat_ipt
            )
        )
        self.assertTrue(
            any(
                "nat -A PDANET_SRV -s 10.1.19.0/24 ! -o pdanet0 "
                "-j MASQUERADE" in c
                for c in flat_ipt
            )
        )
        self.assertTrue(
            any("nat -I POSTROUTING 1 -j PDANET_SRV" in c for c in flat_ipt)
        )
        self.assertTrue(
            any("filter -X PDANET_SRV" in c for c in flat_ipt)
        )
        self.assertEqual(
            [call.args[0] for call in write_forwarding.call_args_list],
            ["1", "0"],
        )
        write_marker.assert_called_once_with("0")
        remove_marker.assert_called_once_with()
        self.assertFalse(configurator._server_rules)
        self.assertFalse(configurator._forwarding_changed)

    def test_forwarding_marker_restores_crashed_previous_value(self):
        config = netconfig.TunConfig(
            role=netconfig.ROLE_SERVER,
            address=netconfig.SERVER_ADDRESS,
            route_all=False,
            dns=(),
        )
        configurator = netconfig.NetConfigurator(config)
        with mock.patch.object(
            netconfig.NetConfigurator, "_read_forwarding", return_value="1"
        ), mock.patch.object(
            netconfig.NetConfigurator, "_read_forwarding_marker",
            return_value="0",
        ), mock.patch.object(
            netconfig.NetConfigurator, "_write_forwarding"
        ) as write_forwarding, mock.patch.object(
            netconfig.NetConfigurator, "_write_forwarding_marker"
        ) as write_marker, mock.patch.object(
            netconfig.NetConfigurator, "_remove_forwarding_marker"
        ) as remove_marker:
            configurator._enable_forwarding()
            self.assertTrue(configurator._forwarding_changed)
            # Forwarding was already on; only the marker says what to
            # restore, so do not overwrite it during startup.
            write_forwarding.assert_not_called()
            configurator._restore_forwarding()
        write_forwarding.assert_called_once_with("0")
        write_marker.assert_not_called()
        remove_marker.assert_called_once_with()
        self.assertFalse(configurator._forwarding_changed)

    def test_forwarding_marker_is_written_when_forwarding_is_enabled(self):
        config = netconfig.TunConfig(
            role=netconfig.ROLE_SERVER,
            address=netconfig.SERVER_ADDRESS,
            route_all=False,
            dns=(),
        )
        configurator = netconfig.NetConfigurator(config)
        with mock.patch.object(
            netconfig.NetConfigurator, "_read_forwarding", return_value="0"
        ), mock.patch.object(
            netconfig.NetConfigurator, "_read_forwarding_marker",
            return_value=None,
        ), mock.patch.object(
            netconfig.NetConfigurator, "_write_forwarding"
        ) as write_forwarding, mock.patch.object(
            netconfig.NetConfigurator, "_write_forwarding_marker"
        ) as write_marker:
            configurator._enable_forwarding()
        write_marker.assert_called_once_with("0")
        write_forwarding.assert_called_once_with("1")
        self.assertTrue(configurator._forwarding_changed)

    def test_server_nat_can_be_disabled(self):
        ipt_calls = []

        def fake_iptables(*args, check=True):
            ipt_calls.append(args)
            return _completed()

        config = netconfig.TunConfig(
            role=netconfig.ROLE_SERVER,
            address=netconfig.SERVER_ADDRESS,
            route_all=False,
            dns=(),
            server_nat=False,
        )
        with mock.patch.object(netconfig, "open_tun_fd", return_value=(59, "pdanet0")), \
             mock.patch.object(netconfig, "run_ip", return_value=_completed()), \
             mock.patch.object(netconfig, "run_iptables", side_effect=fake_iptables), \
             mock.patch.object(
                 netconfig.NetConfigurator, "_read_forwarding", return_value="1"
             ), \
             mock.patch.object(
                 netconfig.NetConfigurator, "_write_forwarding"
             ), \
             mock.patch.object(
                 netconfig.NetConfigurator, "_read_forwarding_marker",
                 return_value=None
             ), \
             mock.patch.object(
                 netconfig.NetConfigurator, "_write_forwarding_marker"
             ), \
             mock.patch.object(
                 netconfig.NetConfigurator, "_remove_forwarding_marker"
             ):
            configurator = netconfig.NetConfigurator(config)
            configurator.up()
            configurator.down()

        flat = [" ".join(call) for call in ipt_calls]
        self.assertTrue(any("filter -N PDANET_SRV" in c for c in flat))
        self.assertFalse(any("nat -N PDANET_SRV" in c for c in flat))
        self.assertFalse(any("MASQUERADE" in c for c in flat))

    def test_del_rules_removes_every_entry_at_pref(self):
        remaining = 3
        calls = []

        def fake_run_ip(*args, check=True):
            nonlocal remaining
            calls.append(args)
            if remaining:
                remaining -= 1
                return _completed()
            return _completed("", "No such file or directory", 2)

        configurator = netconfig.NetConfigurator(netconfig.TunConfig())
        with mock.patch.object(netconfig, "run_ip", side_effect=fake_run_ip):
            configurator._del_rules(netconfig.LAN_RULE_PREF)

        self.assertEqual(remaining, 0)
        self.assertEqual(len(calls), 4)

    def test_remove_stale_link_only_when_address_matches(self):
        calls = []

        def fake_run_ip(*args, check=True):
            calls.append(args)
            if args[:2] == ("addr", "show"):
                return _completed("inet 10.1.19.2/24 scope global pdanet0\n")
            return _completed()

        configurator = netconfig.NetConfigurator(netconfig.TunConfig())
        with mock.patch.object(netconfig, "run_ip", side_effect=fake_run_ip):
            configurator._remove_stale_link()

        self.assertEqual(calls[0], ("addr", "show", "dev", "pdanet0"))
        self.assertEqual(calls[1], ("link", "del", "pdanet0"))

    def test_remove_stale_link_ignores_other_interface(self):
        calls = []

        def fake_run_ip(*args, check=True):
            calls.append(args)
            if args[:2] == ("addr", "show"):
                return _completed("inet 192.168.1.5/24 scope global pdanet0\n")
            return _completed()

        configurator = netconfig.NetConfigurator(netconfig.TunConfig())
        with mock.patch.object(netconfig, "run_ip", side_effect=fake_run_ip):
            configurator._remove_stale_link()

        self.assertEqual(len(calls), 1)

    def test_up_failure_cleans_up(self):
        calls = []

        def fake_run_ip(*args, check=True):
            calls.append(args)
            if args[:2] == ("link", "set"):
                if check:
                    raise netconfig.NetConfigError("boom")
                return _completed("", "boom", 1)
            return _completed()

        config = netconfig.TunConfig()
        with mock.patch.object(netconfig, "open_tun_fd", return_value=(56, "pdanet0")), \
             mock.patch.object(netconfig, "run_ip", side_effect=fake_run_ip), \
             mock.patch.object(netconfig, "_resolvectl", return_value=None):
            configurator = netconfig.NetConfigurator(config)
            with self.assertRaises(netconfig.NetConfigError):
                configurator.up()
        flat = [" ".join(call) for call in calls]
        self.assertTrue(any("link del pdanet0" in c for c in flat))
        self.assertEqual(configurator.fd, -1)

    def test_second_instance_is_rejected(self):
        import fcntl
        lock = os.open(netconfig.LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mock.patch.object(netconfig, "open_tun_fd", return_value=(57, "pdanet0")), \
             mock.patch.object(netconfig, "run_ip", return_value=_completed()):
            configurator = netconfig.NetConfigurator(netconfig.TunConfig())
            with self.assertRaises(netconfig.NetConfigError):
                configurator.up()


class MicrosoftBlockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "hosts")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("127.0.0.1 localhost\n192.168.1.5 printer\n")

    def _read(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def test_apply_and_remove(self):
        original = self._read()
        self.assertTrue(netconfig.apply_microsoft_block(self.path))
        text = self._read()
        self.assertIn(netconfig.HOSTS_BEGIN, text)
        self.assertIn(netconfig.HOSTS_END, text)
        self.assertIn("0.0.0.0 windowsupdate.com", text)
        self.assertIn("127.0.0.1 localhost", text)
        self.assertTrue(netconfig.remove_microsoft_block(self.path))
        self.assertEqual(self._read(), original)

    def test_apply_is_idempotent(self):
        self.assertTrue(netconfig.apply_microsoft_block(self.path))
        once = self._read()
        self.assertFalse(netconfig.apply_microsoft_block(self.path))
        self.assertEqual(self._read(), once)
        self.assertEqual(once.count(netconfig.HOSTS_BEGIN), 1)

    def test_remove_without_block(self):
        self.assertFalse(netconfig.remove_microsoft_block(self.path))

    def test_missing_file_created(self):
        path = os.path.join(self.tmp.name, "new-hosts")
        self.assertTrue(netconfig.apply_microsoft_block(path))
        self.assertIn(netconfig.HOSTS_BEGIN, self._read_path(path))
        self.assertTrue(netconfig.remove_microsoft_block(path))
        self.assertEqual(self._read_path(path), "")

    @staticmethod
    def _read_path(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()


if __name__ == "__main__":
    unittest.main()
