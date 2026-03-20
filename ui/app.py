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
from core.telemetry import ADSTelemetryServer, get_global_telemetry_snapshot
from ui.views.auto import AutoView
from ui.views.manual import ManualView
from ui.event_handler import EventHandler



class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Calibrador de Presión")

        self._ensure_main_maximized()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        _cal, loaded = load_calibration()
        if not loaded:
            messagebox.showwarning("Calibracion", "A0/A1/A2 no calibrado (usando defaults).")

        self.hw = HW()
        self.event_handler = EventHandler(self.hw)
        self.telemetry_server = ADSTelemetryServer(get_global_telemetry_snapshot())
        self.telemetry_server.start()

        top_controls = ttk.Frame(self)
        top_controls.pack(fill="x", padx=8, pady=(6, 2))

        ttk.Label(top_controls, text="Telemetria TCP:").pack(side="left", padx=(0, 6))
        self.btn_tx_a0 = ttk.Button(top_controls, text="Enviar A0", command=lambda: self._set_tx_channel(0))
        self.btn_tx_a1 = ttk.Button(top_controls, text="Enviar A1", command=lambda: self._set_tx_channel(1))
        self.btn_tx_a2 = ttk.Button(top_controls, text="Enviar A2", command=lambda: self._set_tx_channel(2))
        self.btn_tx_stop = ttk.Button(top_controls, text="Detener transmision", command=lambda: self._set_tx_channel(None))
        self.lbl_tx_state = ttk.Label(top_controls, text="TX: OFF")
        self.btn_exit = ttk.Button(top_controls, text="X", width=3, command=self.on_close)

        self.btn_tx_a0.pack(side="left", padx=3)
        self.btn_tx_a1.pack(side="left", padx=3)
        self.btn_tx_a2.pack(side="left", padx=3)
        self.btn_tx_stop.pack(side="left", padx=3)
        self.lbl_tx_state.pack(side="left", padx=(8, 0))
        self.btn_exit.pack(side="right", padx=(6, 0))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        # -------- MANUAL --------
        upd_ms = max(10, int(round(config.DT_PI * 1000)))

        manual = ManualView(
            nb,
            read_vadc=self.hw.read_vadc,
            read_vadc_live=self.hw.read_channel_live_filtered,
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
            set_pump=self.hw.set_pump,
            set_relay=self.hw.set_relay,
            set_valve=self.hw.set_valve,
            request_event=self.event_handler.request_event,
            update_period_ms=upd_ms,
        )
        nb.add(auto, text="Automático")
        self._refresh_tx_state_label()
        self.after_idle(self._ensure_main_maximized)

    def _ensure_main_maximized(self):
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        self.geometry(f"{screen_width}x{screen_height}+0+0")
        self.update_idletasks()
        try:
            if str(self.tk.call("tk", "windowingsystem")).lower() == "x11":
                self.attributes("-fullscreen", True)
                return
        except tk.TclError:
            pass
        try:
            self.state("zoomed")
            return
        except tk.TclError:
            pass
        try:
            self.attributes("-zoomed", True)
            return
        except tk.TclError:
            pass
        self.geometry(f"{screen_width}x{screen_height}+0+0")

    def _set_tx_channel(self, channel):
        self.telemetry_server.set_active_channel(channel)
        self._refresh_tx_state_label()

    def _refresh_tx_state_label(self):
        active = self.telemetry_server.get_active_channel()
        if active == 0:
            txt = "TX: A0 -> puerto 5000"
        elif active == 1:
            txt = "TX: A1 -> puerto 5001"
        elif active == 2:
            txt = "TX: A2 -> puerto 5002"
        else:
            txt = "TX: OFF"
        self.lbl_tx_state.configure(text=txt)

    def on_close(self):
        try:
            self.telemetry_server.stop()
            self.hw.close()
        finally:
            self.destroy()

