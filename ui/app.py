#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ui/app.py
Interfaz grafica principal de la aplicacion
"""

import tkinter as tk
from tkinter import ttk, messagebox

from config import hardware as config
from core.calibration import load_calibration
from core.hw import HW
from core.network import get_network_interfaces, pick_preferred_interface
from core.telemetry import ADSTelemetryServer, CHANNEL_TO_PORT, get_global_telemetry_snapshot
from ui.event_handler import EventHandler
from ui.views.auto import AutoView
from ui.views.manual import ManualView


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Calibrador de Presion")
        self._net_refresh_after_id = None

        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        self.geometry(f"{screen_width}x{screen_height}+0+0")

        try:
            self.state("zoomed")
        except Exception:
            try:
                self.attributes("-zoomed", True)
            except Exception:
                try:
                    self.state("normal")
                    self.update_idletasks()
                except Exception:
                    pass

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
        self.btn_tx_stop = ttk.Button(
            top_controls,
            text="Detener transmision",
            command=lambda: self._set_tx_channel(None),
        )
        self.lbl_tx_state = ttk.Label(top_controls, text="TX: OFF")

        self.btn_tx_a0.pack(side="left", padx=3)
        self.btn_tx_a1.pack(side="left", padx=3)
        self.btn_tx_a2.pack(side="left", padx=3)
        self.btn_tx_stop.pack(side="left", padx=3)
        self.lbl_tx_state.pack(side="left", padx=(8, 0))

        net_controls = ttk.Frame(self)
        net_controls.pack(fill="x", padx=8, pady=(0, 6))

        ttk.Label(net_controls, text="Interfaz LabVIEW:").pack(side="left", padx=(0, 6))
        self.var_net_pref = tk.StringVar(value="ethernet")
        self.cmb_net_pref = ttk.Combobox(
            net_controls,
            state="readonly",
            width=12,
            textvariable=self.var_net_pref,
            values=("ethernet", "wifi", "auto"),
        )
        self.cmb_net_pref.pack(side="left", padx=(0, 6))
        self.cmb_net_pref.bind("<<ComboboxSelected>>", lambda _event: self._refresh_tx_state_label())

        self.btn_net_refresh = ttk.Button(
            net_controls,
            text="Actualizar IP",
            command=self._refresh_tx_state_label,
        )
        self.btn_net_refresh.pack(side="left", padx=(0, 8))

        self.lbl_net_state = ttk.Label(net_controls, text="Red: buscando interfaz...")
        self.lbl_net_state.pack(side="left")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        upd_ms = max(10, int(round(config.DT_PI * 1000)))

        manual = ManualView(
            nb,
            read_vadc=self.hw.read_vadc,
            read_vadc_live=self.hw.read_channel_live_filtered,
            set_pump=self.hw.set_pump,
            get_pump_freq_hz=self.hw.get_pump_frequency_hz,
            set_pump_freq_hz=self.hw.set_pump_frequency_hz,
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

    def _set_tx_channel(self, channel):
        self.telemetry_server.set_active_channel(channel)
        self._refresh_tx_state_label()

    def _refresh_tx_state_label(self):
        active = self.telemetry_server.get_active_channel()
        preferred = self.var_net_pref.get().strip().lower()
        selected_if = pick_preferred_interface(preferred)
        all_ifaces = get_network_interfaces()

        if selected_if is not None:
            net_txt = f"Red: {selected_if.kind.upper()} {selected_if.ipv4} ({selected_if.name})"
            ip_txt = selected_if.ipv4
        else:
            wanted = "Ethernet" if preferred == "ethernet" else "WiFi" if preferred == "wifi" else "Auto"
            net_txt = f"Red: sin interfaz {wanted}"
            if all_ifaces:
                fallback = all_ifaces[0]
                net_txt += f" | disponible {fallback.ipv4} ({fallback.name})"
            ip_txt = "sin IP"

        self.lbl_net_state.configure(text=net_txt)

        if active in CHANNEL_TO_PORT:
            txt = f"TX: A{active} -> {ip_txt}:{CHANNEL_TO_PORT[active]}"
        else:
            txt = "TX: OFF"
        self.lbl_tx_state.configure(text=txt)

        if self._net_refresh_after_id is None:
            self._schedule_network_refresh()

    def _schedule_network_refresh(self):
        def _refresh():
            self._net_refresh_after_id = None
            if self.winfo_exists():
                self._refresh_tx_state_label()

        self._net_refresh_after_id = self.after(5000, _refresh)

    def on_close(self):
        try:
            if self._net_refresh_after_id is not None:
                self.after_cancel(self._net_refresh_after_id)
                self._net_refresh_after_id = None
            self.telemetry_server.stop()
            self.hw.close()
        finally:
            self.destroy()
