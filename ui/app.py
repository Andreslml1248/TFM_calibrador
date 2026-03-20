#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ui/app.py
Interfaz grafica principal de la aplicacion
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
        self.title("Calibrador de Presion")
        self.configure(bg="#0f1218")
        self._screen_width = max(1, int(self.winfo_screenwidth()))
        self._screen_height = max(1, int(self.winfo_screenheight()))
        self._ui_scale = self._compute_ui_scale()

        self._ensure_main_maximized()
        self._configure_notebook_style()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        _cal, loaded = load_calibration()
        if not loaded:
            messagebox.showwarning("Calibracion", "A0/A1/A2 no calibrado (usando defaults).")

        self.hw = HW()
        self.event_handler = EventHandler(self.hw)
        self.telemetry_server = ADSTelemetryServer(get_global_telemetry_snapshot())
        self.telemetry_server.start()

        self.btn_exit = tk.Button(
            self,
            text="X",
            width=3,
            command=self.on_close,
            font=("Arial", max(8, self._sp(11, 8)), "bold"),
            bg="#111827",
            fg="#f8fafc",
            activebackground="#dc2626",
            activeforeground="#ffffff",
            bd=1,
            relief="raised",
        )
        self.btn_exit.place(relx=1.0, x=-self._sp(6, 4), y=self._sp(6, 4), anchor="ne")

        nb = ttk.Notebook(self, style="Main.TNotebook")
        nb.pack(fill="both", expand=True, padx=0, pady=0)
        self.nb = nb

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
        nb.add(auto, text="Automatico")

        self._refresh_tx_state_label()
        self.after_idle(self._ensure_main_maximized)

    def _compute_ui_scale(self) -> float:
        scale_w = float(self._screen_width) / 920.0
        scale_h = float(self._screen_height) / 540.0
        scale = min(scale_w, scale_h)
        if self._screen_height <= 480:
            scale = min(scale, 0.88)
        return max(0.80, min(scale, 1.0))

    def _sp(self, value: float, minimum: int = 0) -> int:
        return max(int(minimum), int(round(float(value) * self._ui_scale)))

    def _configure_notebook_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        tab_font = ("Arial", max(9, self._sp(13, 9)), "bold")
        tab_pad_x = self._sp(18, 12)
        tab_pad_y = self._sp(7, 5)
        style.configure("Main.TNotebook", background="#0f1218", borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure(
            "Main.TNotebook.Tab",
            background="#1b2130",
            foreground="#e5e7eb",
            padding=(tab_pad_x, tab_pad_y),
            font=tab_font,
        )
        style.map(
            "Main.TNotebook.Tab",
            background=[("selected", "#2563eb"), ("active", "#334155")],
            foreground=[("selected", "#ffffff"), ("active", "#ffffff")],
        )

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
        label = getattr(self, "lbl_tx_state", None)
        if label is not None:
            label.configure(text=txt)
        return txt

    def on_close(self):
        try:
            self.telemetry_server.stop()
            self.hw.close()
        finally:
            self.destroy()
