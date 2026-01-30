#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
core/hw.py
Hardware wrapper para GPIO, PWM, relé, válvula y sensor ADC
"""

import platform

try:
    from gpiozero import DigitalOutputDevice, PWMOutputDevice
except ImportError:
    from core.gpiozero_mock import DigitalOutputDevice, PWMOutputDevice

# Usar mocks en Windows, real en Raspberry Pi
if platform.system() == "Windows":
    from core.mocks import LGPIOFactory
    from core.smbus_mock import SMBus
else:
    from gpiozero.pins.lgpio import LGPIOFactory
    from smbus2 import SMBus

from config import hardware as config
from core.ads1115 import clamp, ads_read_v_once
from core.filters import ChannelFilterChain


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

        # Filtros live por canal (Median PtByPt + Mean PtByPt)
        self._live_filters = {}
        self._init_live_filters()

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

    def _init_live_filters(self) -> None:
        self._live_filters[int(config.ADS_CH_DUT_V)] = ChannelFilterChain(
            config.A0_MEDIAN_N, config.A0_MEAN_N
        )
        self._live_filters[int(config.ADS_CH_DUT_mA)] = ChannelFilterChain(
            config.A1_MEDIAN_N, config.A1_MEAN_N
        )
        self._live_filters[int(config.ADS_CH_REF)] = ChannelFilterChain(
            config.A2_MEDIAN_N, config.A2_MEAN_N
        )

    def read_channel_live_filtered(self, ch: int) -> float:
        v_raw = self.read_vadc(ch)
        if not bool(getattr(config, "FILTER_LIVE_ENABLE", True)):
            return float(v_raw)
        chain = self._live_filters.get(int(ch))
        if chain is None:
            chain = ChannelFilterChain(1, 1)
            self._live_filters[int(ch)] = chain
        return float(chain.update(v_raw))

    def close(self):
        try:
            self.set_pump(1.0)
            self.set_relay(False)
            self.set_valve(False)
            self.bus.close()
        except Exception:
            pass

