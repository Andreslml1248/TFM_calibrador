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
from core.network import get_primary_interface_by_kind, pick_preferred_interface
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
        self.server_bind_host = "0.0.0.0"

        _cal, loaded = load_calibration()
        if not loaded:
            messagebox.showwarning("Calibracion", "A0/A1/A2 no calibrado (usando defaults).")

        self.hw = HW()
        self.event_handler = EventHandler(self.hw)
        self.telemetry_server = ADSTelemetryServer(
            get_global_telemetry_snapshot(),
            host=self.server_bind_host,
        )
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

        self.btn_net_refresh = ttk.Button(net_controls, text="Actualizar IP", command=self._refresh_tx_state_label)
        self.btn_net_refresh.pack(side="left", padx=(0, 8))

        self.lbl_net_state = ttk.Label(
            net_controls,
            text="Servidor TCP escuchando en todas las interfaces (0.0.0.0)",
        )
        self.lbl_net_state.pack(side="left")

        self.lbl_labview_ip = tk.Label(
            self,
            text="IP Ethernet para LabVIEW: ---.---.---.---",
            fg="red",
            font=("Arial", 16, "bold"),
            anchor="w",
            padx=12,
            pady=4,
        )
        self.lbl_labview_ip.pack(fill="x", padx=8, pady=(0, 6))

        self.lbl_eth_dhcp = tk.Label(
            self,
            text="DHCP Ethernet para PC: no disponible",
            fg="black",
            font=("Arial", 12),
            anchor="w",
            padx=12,
            pady=2,
        )
        self.lbl_eth_dhcp.pack(fill="x", padx=8, pady=(0, 4))

        self.lbl_wifi_ip = tk.Label(
            self,
            text="IP WiFi: no disponible",
            fg="black",
            font=("Arial", 12),
            anchor="w",
            padx=12,
            pady=2,
        )
        self.lbl_wifi_ip.pack(fill="x", padx=8, pady=(0, 6))

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
        ethernet_if = get_primary_interface_by_kind("ethernet")
        wifi_if = get_primary_interface_by_kind("wifi")
        primary_if = ethernet_if or pick_preferred_interface("auto")

        self.lbl_net_state.configure(
            text=f"Servidor TCP escuchando en todas las interfaces ({self.server_bind_host})"
        )

        if ethernet_if is not None:
            ethernet_txt = f"IP Ethernet para LabVIEW: {ethernet_if.ipv4} ({ethernet_if.name})"
            ip_txt = ethernet_if.ipv4
            dhcp_txt = "DHCP Ethernet para PC: activo"
        elif primary_if is not None:
            ethernet_txt = (
                "IP Ethernet para LabVIEW: Ethernet no disponible"
                f" | interfaz activa: {primary_if.ipv4} ({primary_if.name})"
            )
            ip_txt = primary_if.ipv4
            dhcp_txt = "DHCP Ethernet para PC: no disponible"
        else:
            ethernet_txt = "IP Ethernet para LabVIEW: sin IPv4 disponible"
            ip_txt = "sin IP"
            dhcp_txt = "DHCP Ethernet para PC: no disponible"
        self.lbl_labview_ip.configure(text=ethernet_txt)
        self.lbl_eth_dhcp.configure(text=dhcp_txt)

        if wifi_if is not None:
            wifi_txt = f"IP WiFi: {wifi_if.ipv4} ({wifi_if.name})"
        else:
            wifi_txt = "IP WiFi: no disponible"
        self.lbl_wifi_ip.configure(text=wifi_txt)

        if active in CHANNEL_TO_PORT:
            txt = f"TX: A{active} disponible en puerto {CHANNEL_TO_PORT[active]} | IP recomendada: {ip_txt}"
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
