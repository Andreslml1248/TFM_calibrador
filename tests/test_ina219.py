import unittest

from core.ina219 import INA219Sensor


class FakeBus:
    def __init__(self, devices=None):
        self.devices = devices or {}
        self.writes = []

    def write_i2c_block_data(self, addr, register, data):
        addr_i = int(addr)
        reg_i = int(register)
        value = ((int(data[0]) & 0xFF) << 8) | (int(data[1]) & 0xFF)
        if addr_i not in self.devices:
            raise OSError("No device")
        self.devices[addr_i][reg_i] = value
        self.writes.append((addr_i, reg_i, value))

    def read_i2c_block_data(self, addr, register, length):
        addr_i = int(addr)
        reg_i = int(register)
        if addr_i not in self.devices:
            raise OSError("No device")
        value = int(self.devices[addr_i].get(reg_i, 0)) & 0xFFFF
        return [(value >> 8) & 0xFF, value & 0xFF][: int(length)]


class INA219SensorTests(unittest.TestCase):
    def test_detect_scans_candidate_addresses(self) -> None:
        bus = FakeBus(
            {
                0x42: {
                    INA219Sensor.REG_CONFIG: 0x399F,
                    INA219Sensor.REG_CURRENT: 1000,
                }
            }
        )
        sensor = INA219Sensor(
            bus,
            addresses=(0x40, 0x41, 0x42),
            shunt_ohms=0.1,
            max_current_a=0.4,
            read_log_period_s=999.0,
        )

        found = sensor.detect()

        self.assertEqual(found, 0x42)
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.address, 0x42)
        self.assertIn((0x42, INA219Sensor.REG_CALIBRATION, sensor.calibration_word), bus.writes)

    def test_read_current_ma_returns_engineering_value(self) -> None:
        bus = FakeBus(
            {
                0x40: {
                    INA219Sensor.REG_CONFIG: 0x399F,
                    INA219Sensor.REG_CURRENT: 1000,
                }
            }
        )
        sensor = INA219Sensor(
            bus,
            addresses=(0x40,),
            shunt_ohms=0.1,
            max_current_a=0.4,
            read_log_period_s=999.0,
        )

        current_ma = sensor.read_current_ma()

        expected_ma = 1000.0 * sensor.current_lsb_a * 1000.0
        self.assertAlmostEqual(current_ma, expected_ma, places=6)

    def test_read_current_ma_raises_when_sensor_is_missing(self) -> None:
        bus = FakeBus()
        sensor = INA219Sensor(
            bus,
            addresses=(0x40,),
            shunt_ohms=0.1,
            max_current_a=0.4,
            read_log_period_s=999.0,
        )

        with self.assertRaises(RuntimeError):
            sensor.read_current_ma()


if __name__ == "__main__":
    unittest.main()
