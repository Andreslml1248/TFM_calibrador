"""
core/smbus_mock.py
Mock simple de SMBus para Windows.
Permite ejecutar la app sin hardware I2C real.
"""


class SMBus:
    def __init__(self, bus=1):
        self.bus = bus
        self._ina219_config = 0x399F
        self._ina219_calibration = 0x0000
        self._ina219_current_raw = 1000
        self._ina219_shunt_raw = 120

    def write_i2c_block_data(self, addr, register, data):
        if int(addr) == 0x40 and len(data) >= 2:
            value = ((int(data[0]) & 0xFF) << 8) | (int(data[1]) & 0xFF)
            if int(register) == 0x00:
                self._ina219_config = value
            elif int(register) == 0x05:
                self._ina219_calibration = value
        return None

    def read_i2c_block_data(self, addr, register, length):
        if int(addr) == 0x40 and int(length) >= 2:
            if int(register) == 0x00:
                value = self._ina219_config
            elif int(register) == 0x01:
                value = self._ina219_shunt_raw & 0xFFFF
            elif int(register) == 0x04:
                value = self._ina219_current_raw & 0xFFFF
            elif int(register) == 0x05:
                value = self._ina219_calibration
            else:
                value = 0x0000
            return [(value >> 8) & 0xFF, value & 0xFF] + ([0] * max(0, int(length) - 2))

        # Devuelve ceros para evitar fallos en lecturas
        return [0] * int(length)

    def close(self):
        return None
