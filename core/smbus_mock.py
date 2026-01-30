"""
core/smbus_mock.py
Mock simple de SMBus para Windows.
Permite ejecutar la app sin hardware I2C real.
"""


class SMBus:
    def __init__(self, bus=1):
        self.bus = bus

    def write_i2c_block_data(self, addr, register, data):
        # No-op en mock
        return None

    def read_i2c_block_data(self, addr, register, length):
        # Devuelve ceros para evitar fallos en lecturas
        return [0] * int(length)

    def close(self):
        return None
