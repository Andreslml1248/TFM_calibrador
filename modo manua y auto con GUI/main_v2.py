#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
main_v2.py
- NO modifica: config.py, control_pi.py, mode_manual.py
- Usa:
    ✔ mode_manual.py (estable)
    ✔ mode_auto.py (corregido con punto 0 kPa correcto)
"""

import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional, Dict, Any

from smbus2 import SMBus
from gpiozero import DigitalOutputDevice, PWMOutputDevice
from gpiozero.pins.lgpio import LGPIOFactory

import config

# Vistas
from mode_manual import ManualView
from mode_auto import AutoView


# ============================
# ADS1115 low-level
# ============================
def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def ads_cfg_word(ch: int, pga_v: float, sps: int) -> int:
    osb = 1 << 15
    mux_map = {0: 0b100, 1: 0b101, 2: 0b110, 3: 0b111}
    mux = mux_map.get(ch, 0b110) << 12

    pga_map = {6.144: 0b000, 4.096: 0b001, 2.048: 0b010,
               1.024: 0b011, 0.512: 0b100, 0.256: 0b101}
    pga = pga_map.get(pga_v, 0b001) << 9

    mode = 1 << 8
    dr_map = {8: 0b000, 16: 0b001, 32: 0b010, 64: 0b011,
              128: 0b100, 250: 0b101, 475: 0b110, 860: 0b111}
    dr = dr_map.get(sps, 0b100) << 5

    return osb | mux | pga | mode | dr | 0b11


def ads_read_v_once(bus: SMBus, ch: int) -> float:
    cfg = ads_cfg_word(ch, config.ADS_PGA_V, config.ADS_SPS)
    bus.write_i2c_block_data(
        config.ADS_ADDR, 0x01,
        [(cfg >> 8) & 0xFF, cfg & 0xFF]
    )
    time.sleep(config.ADS_CONV_DELAY_S)
    d = bus.read_i2c_block_data(config.ADS_ADDR, 0x00, 2)
    raw = (d[0] << 8) | d[1]
    if raw & 0x8000:
        raw -= 1 << 16
    return (raw / 32768.0) * config.ADS_PGA_V


# ============================
# Hardware wrapper
# ============================
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


# ============================
# App principal
# ============================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Calibrador de Presión")
        self.geometry("1000x600")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.hw = HW()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        # -------- Eventos --------
        def request_event(name: str, payload: Optional[Dict[str, Any]] = None):
            if name == "EV_OVERPRESSURE":
                messagebox.showerror(
                    "OVERPRESSURE",
                    "Presión excede límite de seguridad.\nSistema detenido."
                )
                self.hw.set_pump(1.0)
                self.hw.set_relay(False)
                self.hw.set_valve(False)

            elif name in ("EV_SENSOR_FAIL_CRITICAL", "EV_AUTO_FAIL"):
                err = payload.get("error") if payload else ""
                messagebox.showerror("ERROR CRÍTICO", err)
                self.hw.set_pump(1.0)
                self.hw.set_relay(False)
                self.hw.set_valve(True)

        # -------- MANUAL --------
        upd_ms = max(10, int(round(config.DT_PI * 1000)))

        manual = ManualView(
            nb,
            read_vadc=self.hw.read_vadc,
            set_pump=self.hw.set_pump,
            set_relay=self.hw.set_relay,
            set_valve=self.hw.set_valve,
            request_event=request_event,
            update_period_ms=upd_ms,
        )
        nb.add(manual, text="Manual")

        # -------- AUTOMÁTICO --------
        auto = AutoView(
            nb,
            read_vadc=self.hw.read_vadc,
            set_pump=self.hw.set_pump,
            set_relay=self.hw.set_relay,
            set_valve=self.hw.set_valve,
            request_event=request_event,
            update_period_ms=upd_ms,
        )
        nb.add(auto, text="Automático")

    def on_close(self):
        try:
            self.hw.close()
        finally:
            self.destroy()


# ============================
# MAIN
# ============================
def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()