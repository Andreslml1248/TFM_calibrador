#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ui/app.py
Interfaz grafica principal de la aplicacion
"""

import os
import tkinter as tk
from tkinter import ttk, messagebox

from config import hardware as config
from core.hw import HW
from core.calibration import load_calibration
from core.export_manager import ExportManager, ExportSyncResult
from core.network import NetworkAddresses, format_labview_status, get_network_addresses
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

        self.export_manager = ExportManager(os.getcwd())
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

        nb = ttk.Notebook(self, style="Main.TNotebook")
        nb.pack(fill="both", expand=True, padx=0, pady=0)
        self.nb = nb
        self.btn_exit.place(in_=self, relx=1.0, x=-self._sp(6, 4), y=self._sp(3, 2), anchor="ne")
        self.btn_exit.lift()

        self.var_usb_state = tk.StringVar(value="USB: inicializando")
        self.var_labview_state = tk.StringVar(value="LAB OFF | W -- | E --")
        self._usb_status_bg = "#1f2937"
        self._usb_status_fg = "#cbd5e1"
        self._network_addresses = NetworkAddresses()
        self.lbl_tx_state = None

        upd_ms = max(10, int(round(config.DT_PI * 1000)))

        manual = ManualView(
            nb,
            read_vadc=self.hw.read_vadc,
            read_vadc_live=self.hw.read_channel_live_filtered,
            read_dut_current_ma=self.hw.read_dut_current_ma,
            read_dut_current_ma_live=self.hw.read_dut_current_ma_live_filtered,
            set_pump=self.hw.set_pump,
            set_relay=self.hw.set_relay,
            set_valve=self.hw.set_valve,
            request_event=self.event_handler.request_event,
            export_manager=self.export_manager,
            usb_state_var=self.var_usb_state,
            labview_state_var=self.var_labview_state,
            retry_usb_export=self._retry_pending_exports,
            get_usb_status_colors=self._get_usb_status_colors,
            update_period_ms=upd_ms,
        )
        nb.add(manual, text="Manual")

        auto = AutoView(
            nb,
            read_vadc=self.hw.read_vadc,
            read_vadc_live=self.hw.read_channel_live_filtered,
            read_dut_current_ma=self.hw.read_dut_current_ma,
            read_dut_current_ma_live=self.hw.read_dut_current_ma_live_filtered,
            set_pump=self.hw.set_pump,
            set_relay=self.hw.set_relay,
            set_valve=self.hw.set_valve,
            request_event=self.event_handler.request_event,
            export_manager=self.export_manager,
            usb_state_var=self.var_usb_state,
            labview_state_var=self.var_labview_state,
            retry_usb_export=self._retry_pending_exports,
            get_usb_status_colors=self._get_usb_status_colors,
            update_period_ms=upd_ms,
        )
        nb.add(auto, text="Automatico")

        self._apply_network_status(get_network_addresses())
        self._refresh_tx_state_label()
        self._apply_export_status(self.export_manager.sync_pending_exports())
        self.after_idle(self._ensure_main_maximized)
        self.after(int(config.USB_EXPORT_POLL_INTERVAL_MS), self._poll_export_status)

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
        self._update_labview_state()
        return txt

    def _apply_network_status(self, addresses: NetworkAddresses) -> None:
        self._network_addresses = addresses
        self._update_labview_state()

    def _update_labview_state(self) -> None:
        active = self.telemetry_server.get_active_channel()
        self.var_labview_state.set(format_labview_status(active, self._network_addresses))

    def _apply_export_status(self, sync_result: ExportSyncResult) -> None:
        self.var_usb_state.set(self.export_manager.format_status_text(sync_result))
        if sync_result.last_error:
            self._usb_status_bg = "#7c2d12"
            self._usb_status_fg = "#ffedd5"
        elif sync_result.usb_detected:
            self._usb_status_bg = "#14532d"
            self._usb_status_fg = "#ecfccb"
        elif sync_result.pending_count > 0:
            self._usb_status_bg = "#78350f"
            self._usb_status_fg = "#fef3c7"
        else:
            self._usb_status_bg = "#1f2937"
            self._usb_status_fg = "#cbd5e1"

    def _get_usb_status_colors(self) -> tuple[str, str]:
        return self._usb_status_bg, self._usb_status_fg

    def _poll_export_status(self) -> None:
        try:
            sync_result = self.export_manager.sync_pending_exports()
            self._apply_export_status(sync_result)
            self._apply_network_status(get_network_addresses())
        finally:
            try:
                if self.winfo_exists():
                    self.after(int(config.USB_EXPORT_POLL_INTERVAL_MS), self._poll_export_status)
            except tk.TclError:
                pass

    def _retry_pending_exports(self) -> None:
        sync_result = self.export_manager.sync_pending_exports()
        self._apply_export_status(sync_result)

        if sync_result.copied_count > 0:
            messagebox.showinfo("USB", f"Se copiaron {sync_result.copied_count} archivo(s) a la USB.")
            return
        if sync_result.pending_count > 0 and not sync_result.usb_detected:
            messagebox.showwarning("USB", "No hay una USB detectada. Los archivos siguen pendientes.")
            return
        if sync_result.pending_count > 0 and sync_result.last_error:
            messagebox.showwarning("USB", f"La copia a la USB sigue pendiente.\n{sync_result.last_error}")
            return
        messagebox.showinfo("USB", "No hay archivos pendientes de exportacion.")

    def on_close(self):
        try:
            self.telemetry_server.stop()
            self.hw.close()
        finally:
            self.destroy()
