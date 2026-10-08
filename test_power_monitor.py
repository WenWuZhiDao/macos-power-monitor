import unittest
from unittest.mock import patch

import power_monitor as monitor


class PowerTelemetryTests(unittest.TestCase):
    def sample(self, **fields):
        info = {"Voltage": 12000, "Amperage": 4000,
                "ExternalConnected": True, **fields}
        with patch.object(monitor, "read_battery", return_value=info):
            return monitor.sample_once()

    def test_macos27_telemetry(self):
        sample = self.sample(
            BatteryData={"BatteryPower": 48256},
            PowerTelemetryData={"SystemPowerIn": 62456, "SystemLoad": 13286,
                                "AccumulatedSystemLoad": 491269543})
        self.assertEqual(sample["total"], 62.456)
        self.assertEqual(sample["system"], 13.286)

    def test_legacy_watts_and_partial_fallback(self):
        sample = self.sample(BatteryData={"AdapterPower": 60.5},
                             PowerTelemetryData={"SystemPowerIn": 62456,
                                                 "SystemLoad": 13286})
        self.assertEqual(sample["total"], 60.5)
        self.assertEqual(sample["system"], 13.286)
        sample = self.sample(BatteryData={"AdapterPower": 0, "SystemPower": 0})
        self.assertEqual((sample["total"], sample["system"]), (0, 0))

    def test_missing_fields_remain_unknown_on_ac(self):
        sample = self.sample(PowerTelemetryData={"SystemLoad": "invalid"})
        self.assertIsNone(sample["total"])
        self.assertIsNone(sample["system"])

    def test_battery_fallback(self):
        sample = self.sample(ExternalConnected=False, Amperage=-1000)
        self.assertEqual((sample["total"], sample["system"]), (0, 12))


if __name__ == "__main__":
    unittest.main()
