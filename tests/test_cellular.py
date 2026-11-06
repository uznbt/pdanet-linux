import contextlib
import io
import os
import subprocess
import sys
import unittest
from argparse import Namespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pdanet_linux import cellular, cli

SAMPLE = (
    "    mServiceState={mVoiceRegState=1(OUT_OF_SERVICE), "
    "mDataRegState=1(OUT_OF_SERVICE), mOperatorAlphaLong=, "
    "mOperatorAlphaShort=, getRilDataRadioTechnology=0(Unknown)}\n"
    "    mSignalStrength=SignalStrength:{mCdma=CellSignalStrengthCdma: "
    "level=0,mGsm=CellSignalStrengthGsm: rssi=2147483647 level=0,"
    "mLte=CellSignalStrengthLte: rssi=2147483647 rsrp=2147483647 "
    "rsrq=2147483647 rssnr=2147483647 level=0,"
    "mNr=CellSignalStrengthNr:{ ssRsrp = 2147483647 level = 0 },"
    "primary=CellSignalStrengthLte}\n"
    "    mServiceState={mVoiceRegState=0(IN_SERVICE), "
    "mDataRegState=0(IN_SERVICE), mOperatorAlphaLong=XL Axiata, "
    "mOperatorAlphaShort=XL Axiata, getRilDataRadioTechnology=14(LTE)}\n"
    "    mSignalStrength=SignalStrength:{mCdma=CellSignalStrengthCdma: "
    "level=0,mGsm=CellSignalStrengthGsm: rssi=2147483647 level=0,"
    "mLte=CellSignalStrengthLte: rssi=-63 rsrp=-92 rsrq=-11 rssnr=18 "
    "miuiLevel=5 level=4 parametersUseForLevel=0,"
    "mNr=CellSignalStrengthNr:{ ssRsrp = 2147483647 level = 0 },"
    "primary=CellSignalStrengthLte}\n"
)


class ParseTests(unittest.TestCase):
    def test_prefers_in_service_subscription(self):
        signal = cellular.parse_telephony_dump(SAMPLE)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.operator, "XL Axiata")
        self.assertEqual(signal.technology, "LTE")
        self.assertEqual(signal.level, 4)
        self.assertEqual(signal.rsrp, -92)
        self.assertEqual(signal.rsrq, -11)
        self.assertEqual(signal.snr, 18)
        self.assertEqual(signal.data_state, "IN_SERVICE")
        self.assertEqual(signal.bars, "4/4")
        self.assertIn("XL Axiata", signal.compact)
        self.assertIn("RSRP -92 dBm", signal.detail)

    def test_nr_fallback(self):
        output = (
            "mServiceState={mDataRegState=0(IN_SERVICE), "
            "mOperatorAlphaLong=Telkomsel, "
            "getRilDataRadioTechnology=20(NR)}\n"
            "mSignalStrength=SignalStrength:{mNr=CellSignalStrengthNr:{ "
            "ssRsrp = -95 ssRsrq = -12 ssSinr = 20 miuiLevel = 4 "
            "level = 3 },primary=CellSignalStrengthNr}\n"
        )
        signal = cellular.parse_telephony_dump(output)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.operator, "Telkomsel")
        self.assertEqual(signal.technology, "5G NR")
        self.assertEqual(signal.level, 3)
        self.assertEqual(signal.rsrp, -95)
        self.assertEqual(signal.snr, 20)

    def test_generic_level_fallback(self):
        output = (
            "mServiceState={mDataRegState=0(IN_SERVICE), "
            "mOperatorAlphaLong=Indosat, "
            "getRilDataRadioTechnology=3(UMTS)}\n"
            "mSignalStrength=SignalStrength:{mWcdma=CellSignalStrengthWcdma: "
            "ss=-80 level=2}\n"
        )
        signal = cellular.parse_telephony_dump(output)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.operator, "Indosat")
        self.assertEqual(signal.technology, "UMTS")
        self.assertEqual(signal.level, 2)

    def test_empty_dump(self):
        self.assertIsNone(cellular.parse_telephony_dump(""))
        self.assertIsNone(cellular.parse_telephony_dump("no telephony here\n"))

    def test_unknown_measurements_are_ignored(self):
        output = (
            "mServiceState={mDataRegState=0(IN_SERVICE), "
            "mOperatorAlphaLong=, getRilDataRadioTechnology=0(Unknown)}\n"
            "mSignalStrength=SignalStrength:{mLte=CellSignalStrengthLte: "
            "rsrp=2147483647 rsrq=2147483647 rssnr=2147483647 level=0,"
            "primary=CellSignalStrengthLte}\n"
        )
        signal = cellular.parse_telephony_dump(output)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.operator, "")
        self.assertEqual(signal.technology, "")
        self.assertIsNone(signal.rsrp)
        self.assertIsNone(signal.rsrq)
        self.assertIsNone(signal.snr)
        self.assertEqual(signal.level, 0)


class CliSignalTests(unittest.TestCase):
    def test_signal_command_prints_description(self):
        signal_value = cellular.CellularSignal(
            operator="XL Axiata",
            technology="LTE",
            level=4,
            rsrp=-92,
            rsrq=-10,
            snr=15,
            data_state="IN_SERVICE",
        )
        stdout = io.StringIO()
        with mock.patch.object(
            cli.cellular, "read_signal", return_value=signal_value
        ), contextlib.redirect_stdout(stdout):
            code = cli.cmd_signal(Namespace(serial=None))
        self.assertEqual(code, 0)
        self.assertIn("XL Axiata LTE 4/4", stdout.getvalue())
        self.assertIn("RSRP -92 dBm", stdout.getvalue())

    def test_signal_command_unavailable(self):
        stderr = io.StringIO()
        with mock.patch.object(
            cli.cellular, "read_signal", return_value=None
        ), contextlib.redirect_stderr(stderr):
            code = cli.cmd_signal(Namespace(serial=None))
        self.assertEqual(code, 1)
        self.assertIn("unavailable", stderr.getvalue())


class ReadSignalTests(unittest.TestCase):
    def test_uses_serial(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=SAMPLE, stderr=""
        )
        with mock.patch.object(cellular.shutil, "which", return_value="/usr/bin/adb"), \
             mock.patch.object(cellular.subprocess, "run", return_value=completed) as run:
            signal = cellular.read_signal("ABC123")

        self.assertIsNotNone(signal)
        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/adb",
                "-s",
                "ABC123",
                "shell",
                "dumpsys",
                "telephony.registry",
            ],
        )

    def test_missing_adb(self):
        with mock.patch.object(cellular.shutil, "which", return_value=None):
            self.assertIsNone(cellular.read_signal())

    def test_failed_adb(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="device offline"
        )
        with mock.patch.object(cellular.shutil, "which", return_value="/usr/bin/adb"), \
             mock.patch.object(cellular.subprocess, "run", return_value=completed):
            self.assertIsNone(cellular.read_signal())


if __name__ == "__main__":
    unittest.main()
