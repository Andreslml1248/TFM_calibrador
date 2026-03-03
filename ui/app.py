#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ui/app.py
Interfaz gráfica principal de la aplicación
"""

import tkinter as tk
from tkinter import ttk, messagebox

from config import hardware as config
from core.hw import HW
from core.calibration import load_calibration
from ui.views.auto import AutoView
from ui.views.manual import ManualView
from ui.event_handler import EventHandler



class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Calibrador de Presión")

        # Obtener dimensiones de la pantalla
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()

        # Establecer geometría a tamaño máximo de pantalla
        self.geometry(f"{screen_width}x{screen_height}+0+0")

        # Maximizar ventana (compatible con Windows, Linux y Raspberry Pi)
        try:
            self.state('zoomed')  # Windows
        except:
            try:
                self.attributes('-zoomed', True)  # Linux
            except:
                try:
                    # Fallback: usar estado normal y forzar tamaño máximo
                    self.state('normal')
                    self.update_idletasks()
                except:
                    pass

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        _cal, loaded = load_calibration()
        if not loaded:
            messagebox.showwarning("Calibracion", "A0/A1/A2 no calibrado (usando defaults).")

        self.hw = HW()
        self.event_handler = EventHandler(self.hw)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        # -------- MANUAL --------
        upd_ms = max(10, int(round(config.DT_PI * 1000)))

        manual = ManualView(
            nb,
            read_vadc=self.hw.read_vadc,
            read_vadc_live=self.hw.read_channel_live_filtered,
            update_temperature_control=getattr(self.hw, "update_temperature_control", None),
            set_pump=self.hw.set_pump,
            set_relay=self.hw.set_relay,
            set_valve=self.hw.set_valve,
            request_event=self.event_handler.request_event,
            update_period_ms=upd_ms,
        )
        nb.add(manual, text="Manual")

        # -------- AUTOMÁTICO --------
        auto = AutoView(
            nb,
            read_vadc=self.hw.read_vadc,
            read_vadc_live=self.hw.read_channel_live_filtered,
            update_temperature_control=getattr(self.hw, "update_temperature_control", None),
            set_pump=self.hw.set_pump,
            set_relay=self.hw.set_relay,
            set_valve=self.hw.set_valve,
            request_event=self.event_handler.request_event,
            update_period_ms=upd_ms,
        )
        nb.add(auto, text="Automático")

    def on_close(self):
        try:
            self.hw.close()
        finally:
            self.destroy()

