#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
core/hw.py
Hardware wrapper para GPIO, PWM, relé, válvula y sensor ADC
"""

import glob
import os
import platform
import time
from typing import Optional

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
        self._temp_device_file: Optional[str] = None
        self._last_temp_update_ts: float = 0.0
        self._temp_update_period_s: float = 1.0
        self._last_temp_c: Optional[float] = None
        self._fan_pwm_cmd: float = clamp(float(config.FAN_PWM_MIN), 0.0, 1.0)

        self.rele_bomba = DigitalOutputDevice(
            config.RELE_BOMBA_PIN, pin_factory=self.factory
        )
        self.pwm_bomba = PWMOutputDevice(
            config.PWM_PIN,
            frequency=config.PWM_FREQ_HZ,
            pin_factory=self.factory
        )
        self.pwm_fan = None
        try:
            self.pwm_fan = PWMOutputDevice(
                config.FAN_PWM_PIN,
                frequency=config.FAN_PWM_FREQ_HZ,
                pin_factory=self.factory
            )
        except Exception:
            self.pwm_fan = None

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
        self.set_fan(config.FAN_PWM_MIN)
        self._maybe_update_temperature_control(force=True)

    # ---------- Actuadores ----------
    def set_relay(self, on: bool):
        self.rele_bomba.on() if on else self.rele_bomba.off()

    def set_pump(self, u_cmd: float):
        u = clamp(float(u_cmd), 0.0, 1.0)
        pwm_hw = (1.0 - u) if config.BOMBA_ACTIVE_LOW else u
        self.pwm_bomba.value = clamp(pwm_hw, 0.0, 1.0)

    def set_fan(self, pwm_cmd: float):
        pwm = clamp(float(pwm_cmd), 0.0, 1.0)
        self._fan_pwm_cmd = pwm
        if self.pwm_fan is None:
            return
        self.pwm_fan.value = pwm

    def set_valve(self, open_: bool):
        if not self.valvula:
            return
        if config.VALV_ACTIVE_HIGH:
            self.valvula.on() if open_ else self.valvula.off()
        else:
            self.valvula.off() if open_ else self.valvula.on()

    # ---------- Lecturas ----------
    def read_vadc(self, ch: int) -> float:
        self._maybe_update_temperature_control()
        return float(ads_read_v_once(self.bus, int(ch)))

    def read_temperature_c(self) -> float:
        if platform.system() == "Windows":
            self._last_temp_c = float(config.TEMP_TARGET_C)
            return self._last_temp_c

        device_file = self._get_temp_device_file()
        if not device_file:
            raise RuntimeError("DS18B20 no detectado")

        with open(device_file, "r", encoding="ascii") as fh:
            lines = [line.strip() for line in fh.readlines()]

        if len(lines) < 2 or not lines[0].endswith("YES"):
            raise RuntimeError("Lectura DS18B20 invalida")

        pos = lines[1].find("t=")
        if pos < 0:
            raise RuntimeError("DS18B20 sin campo t=")

        temp_c = float(lines[1][pos + 2:]) / 1000.0
        self._last_temp_c = temp_c
        return temp_c

    def compute_fan_pwm(self, temp_c: float) -> float:
        temp = float(temp_c)
        pwm_min = clamp(float(config.FAN_PWM_MIN), 0.0, 1.0)
        pwm_max = clamp(float(config.FAN_PWM_MAX), pwm_min, 1.0)
        t_on = float(config.TEMP_TARGET_C)
        t_full = max(t_on, float(config.TEMP_FULLSPEED_C))
        t_off = t_on - abs(float(config.TEMP_HYST_C))

        if temp <= t_off:
            return pwm_min

        if temp < t_on and self._fan_pwm_cmd <= pwm_min:
            return pwm_min

        if t_full <= t_on:
            return pwm_max if temp >= t_on else pwm_min

        if temp >= t_full:
            return pwm_max

        frac = (temp - t_on) / (t_full - t_on)
        return pwm_min + frac * (pwm_max - pwm_min)

    def _get_temp_device_file(self) -> Optional[str]:
        if self._temp_device_file and os.path.exists(self._temp_device_file):
            return self._temp_device_file

        matches = glob.glob("/sys/bus/w1/devices/28-*/w1_slave")
        self._temp_device_file = matches[0] if matches else None
        return self._temp_device_file

    def _maybe_update_temperature_control(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_temp_update_ts) < self._temp_update_period_s:
            return

        self._last_temp_update_ts = now
        try:
            temp_c = self.read_temperature_c()
            self.set_fan(self.compute_fan_pwm(temp_c))
        except Exception:
            if bool(getattr(config, "FAN_FAILSAFE_FULLSPEED", True)):
                self.set_fan(getattr(config, "FAN_FAILSAFE_PWM", 1.0))
            else:
                self.set_fan(config.FAN_PWM_MIN)

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
            self.set_fan(config.FAN_PWM_MIN)
            self.set_relay(False)
            self.set_valve(False)
            self.bus.close()
        except Exception:
            pass

