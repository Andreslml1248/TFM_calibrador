#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
core/ina219.py
Lectura centralizada de corriente DUT usando INA219 por I2C.
"""

import time
from typing import Iterable, Optional


class INA219Sensor:
    REG_CONFIG = 0x00
    REG_SHUNT_VOLTAGE = 0x01
    REG_BUS_VOLTAGE = 0x02
    REG_POWER = 0x03
    REG_CURRENT = 0x04
    REG_CALIBRATION = 0x05

    DEFAULT_CONFIG = 0x399F  # 32V, 320mV, 12-bit, continuo

    def __init__(
        self,
        bus,
        *,
        addresses: Optional[Iterable[int]] = None,
        shunt_ohms: float = 0.1,
        max_current_a: float = 0.4,
        read_log_period_s: float = 1.0,
        detect_retry_s: float = 1.0,
    ):
        self.bus = bus
        self.addresses = tuple(int(addr) for addr in (addresses or tuple(range(0x40, 0x50))))
        self.shunt_ohms = max(1e-6, float(shunt_ohms))
        self.max_current_a = max(1e-6, float(max_current_a))
        self.read_log_period_s = max(0.0, float(read_log_period_s))
        self.detect_retry_s = max(0.1, float(detect_retry_s))

        self.address: Optional[int] = None
        self.available: bool = False
        self._last_read_log_ts = 0.0
        self._last_error_log_ts = 0.0
        self._next_detect_retry_ts = 0.0

        min_current_lsb_a = 0.04096 / (65535.0 * self.shunt_ohms)
        requested_lsb_a = self.max_current_a / 32767.0
        self.current_lsb_a = max(min_current_lsb_a, requested_lsb_a)
        self.calibration_word = int(0.04096 / (self.current_lsb_a * self.shunt_ohms))
        if not (1 <= self.calibration_word <= 0xFFFF):
            raise ValueError("INA219 calibration word out of range.")

    def detect(self, force: bool = False) -> Optional[int]:
        now = time.monotonic()
        if self.available and self.address is not None and not force:
            return self.address
        if not force and now < self._next_detect_retry_ts:
            return None

        self._next_detect_retry_ts = now + self.detect_retry_s

        for addr in self.addresses:
            try:
                _ = self._read_u16(addr, self.REG_CONFIG)
                self._configure_device(addr)
            except Exception:
                continue

            self.address = int(addr)
            self.available = True
            print(f"[INA219] detected at 0x{self.address:02X}")
            return self.address

        self.address = None
        self.available = False
        self._log_error("not detected on I2C")
        return None

    def read_current_ma(self) -> float:
        addr = self.detect(force=False)
        if addr is None:
            raise RuntimeError("INA219 not detected on I2C.")

        try:
            raw_current = self._read_s16(addr, self.REG_CURRENT)
            current_ma = float(raw_current) * self.current_lsb_a * 1000.0
        except Exception as exc:
            self.address = None
            self.available = False
            self._log_error(f"read failed: {exc}")
            raise RuntimeError(f"INA219 read failed: {exc}") from exc

        self._log_read(current_ma)
        return float(current_ma)

    def _configure_device(self, addr: int) -> None:
        self._write_u16(addr, self.REG_CONFIG, self.DEFAULT_CONFIG)
        self._write_u16(addr, self.REG_CALIBRATION, self.calibration_word)

    def _write_u16(self, addr: int, register: int, value: int) -> None:
        word = int(value) & 0xFFFF
        self.bus.write_i2c_block_data(
            int(addr),
            int(register),
            [(word >> 8) & 0xFF, word & 0xFF],
        )

    def _read_u16(self, addr: int, register: int) -> int:
        data = self.bus.read_i2c_block_data(int(addr), int(register), 2)
        return ((int(data[0]) & 0xFF) << 8) | (int(data[1]) & 0xFF)

    def _read_s16(self, addr: int, register: int) -> int:
        raw = self._read_u16(addr, register)
        if raw & 0x8000:
            raw -= 1 << 16
        return int(raw)

    def _log_read(self, current_ma: float) -> None:
        now = time.monotonic()
        if self.read_log_period_s <= 0.0 or (now - self._last_read_log_ts) >= self.read_log_period_s:
            self._last_read_log_ts = now
            print(f"[INA219] current={float(current_ma):.3f} mA")

    def _log_error(self, message: str) -> None:
        now = time.monotonic()
        if (now - self._last_error_log_ts) >= 1.0:
            self._last_error_log_ts = now
            print(f"[INA219] {message}")
