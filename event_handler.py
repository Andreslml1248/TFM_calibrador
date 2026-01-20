#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
event_handler.py
Manejo centralizado de eventos del sistema
"""

from typing import Optional, Dict, Any
from tkinter import messagebox
from hw import HW


class EventHandler:
    def __init__(self, hw: HW):
        self.hw = hw

    def request_event(self, name: str, payload: Optional[Dict[str, Any]] = None):
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

