#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
core/hw.py
Hardware wrapper para GPIO, PWM, relé, válvula y sensor ADC
"""

import platform
from smbus2 import SMBus
from gpiozero import DigitalOutputDevice, PWMOutputDevice

# Usar mock en Windows, real en Raspberry Pi
if platform.system() == "Windows":
    from core.mocks import LGPIOFactory
else:
    from gpiozero.pins.lgpio import LGPIOFactory

from config import hardware as config
from core.ads1115 import clamp, ads_read_v_once


class HW:
    def __init__(self):
        self.factory = LGPIOFactory()

        self.rele_bomba = DigitalOutputDevice(
            config.RELE_BOMBA_PIN, pin_factory=self.factory
        )
        self.pwm_bomba = PWMOutputDevice(
            config.PWM_PIN,
            frequency=config.PWM_FREQ_HZ,
            pin_factory=self.factory
        )

        self.valvula = None
        if config.USE_VALVULA:
            self.valvula = DigitalOutputDevice(
                config.VALV_PIN, pin_factory=self.factory
            )

        self.bus = SMBus(config.ADS_I2C_BUS)

        # Estado seguro inicial
        self.set_pump(1.0)
        self.set_relay(False)
        self.set_valve(True)

    # ---------- Actuadores ----------
    def set_relay(self, on: bool):
        self.rele_bomba.on() if on else self.rele_bomba.off()

    def set_pump(self, u_cmd: float):
        u = clamp(float(u_cmd), 0.0, 1.0)
        pwm_hw = (1.0 - u) if config.BOMBA_ACTIVE_LOW else u
        self.pwm_bomba.value = clamp(pwm_hw, 0.0, 1.0)

    def set_valve(self, open_: bool):
        if not self.valvula:
            return
        if config.VALV_ACTIVE_HIGH:
            self.valvula.on() if open_ else self.valvula.off()
        else:
            self.valvula.off() if open_ else self.valvula.on()

    # ---------- Lecturas ----------
    def read_vadc(self, ch: int) -> float:
        return float(ads_read_v_once(self.bus, int(ch)))

    def close(self):
        try:
            self.set_pump(1.0)
            self.set_relay(False)
            self.set_valve(False)
            self.bus.close()
        except Exception:
            pass

