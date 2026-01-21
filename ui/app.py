#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ui/app.py
Interfaz gráfica principal de la aplicación
"""

import tkinter as tk
from tkinter import ttk

from config import hardware as config
from core.hw import HW
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

        # Maximizar ventana
        self.state('zoomed')  # Windows
        try:
            self.state('zoomed')
        except:
            try:
                self.state('normal')
                self.attributes('-zoomed', True)  # Linux
            except:
                pass

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.hw = HW()
        self.event_handler = EventHandler(self.hw)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        # -------- MANUAL --------
        upd_ms = max(10, int(round(config.DT_PI * 1000)))

        manual = ManualView(
            nb,
            read_vadc=self.hw.read_vadc,
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

