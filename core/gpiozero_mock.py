"""
core/gpiozero_mock.py
Mock simple de gpiozero para Windows (sin hardware).
"""


class DigitalOutputDevice:
    def __init__(self, *args, **kwargs):
        self.value = 0

    def on(self):
        self.value = 1

    def off(self):
        self.value = 0

    def close(self):
        return None


class PWMOutputDevice:
    def __init__(self, *args, **kwargs):
        self.value = 0.0
        self.frequency = kwargs.get("frequency", 0)

    def close(self):
        return None
