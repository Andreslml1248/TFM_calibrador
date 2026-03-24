# mode_auto.py
# -*- coding: utf-8 -*-

import time
import tkinter as tk
from tkinter import ttk, messagebox
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any, List
import os
from datetime import datetime

from config import hardware as config
from core.control import PIController, PIConfig, PIWorker
from core.export_manager import ExportManager, ExportSyncResult
from core.filters import MedianPtByPt
from ui.numeric_keypad import open_numeric_keypad_dialog

# Matplotlib embebido en Tk (para gráfica tipo Excel)
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import numpy as np


# ============================================================
# CONFIG / RUNTIME
# ============================================================
@dataclass
class AutoConfig:
    dut_mode: str = "A1"          # A0 / A1
    sig_min: float = 4.0
    sig_max: float = 20.0

    p_min_kpa: float = 0.0
    p_max_kpa: float = 200.0

    n_points: int = 5             # 2 / 3 / 5
    direction: str = "UP"         # UP / DOWN / BOTH

    settle_time_s: float = 5.0
    settle_time_max_s: float = 10.0

    # ====== CONDICIONES DE CONTROL (se editan en ventana aparte) ======
    deadband_kpa: float = float(getattr(config, "AUTO_STATIC_HOLD_BAND_KPA", 1.0))
    inband_up_s: float = float(getattr(config, "AUTO_STATIC_HOLD_DELAY_S", 1.5))
    inband_down_s: float = float(getattr(config, "AUTO_STATIC_HOLD_DELAY_S", 1.5))

    # Retardo extra opcional antes de cerrar EV en bajada.
    # Por defecto se deja a 0 para usar el mismo criterio estatico del modo manual.
    valve_close_delay_s: float = 0.0

    # Límites y feedforward del PI
    u_min: float = 0.0
    u_max: float = 1.0
    u_ff:  float = 0.380

    p_max_seguridad_kpa: float = config.P_MAX_SEGURIDAD_KPA


@dataclass
class AutoRuntime:
    running: bool = False
    points: List[float] = None
    step_index: int = 0

    p_zero_kpa: float = 0.0
    tare_done: bool = False

    state: str = "IDLE"
    t_state: Optional[float] = None

    last_p: float = 0.0
    last_u: float = 1.0


# ============================================================
# STATES
# ============================================================
IDLE = "IDLE"

ZERO_VENT = "ZERO_VENT"
ZERO_HOLD = "ZERO_HOLD"

GOTO_SP = "GOTO_SP"
IN_BAND_WAIT_UP = "IN_BAND_WAIT_UP"
IN_BAND_WAIT_DOWN = "IN_BAND_WAIT_DOWN"
DOWN_CLOSE_DELAY = "DOWN_CLOSE_DELAY"
HOLD_MEASURE = "HOLD_MEASURE"


# ============================================================
# VIEW
# ============================================================
class AutoView(ttk.Frame):
    _A0_SIG_MIN_DEFAULT = 0.0
    _A0_SIG_MAX_DEFAULT = 10.0
    _A1_SIG_MIN_DEFAULT = 4.0
    _A1_SIG_MAX_DEFAULT = 20.0
    _PRESSURE_MIN_KPA = 0.0
    _PRESSURE_MAX_KPA = 200.0
    _UNIT_TO_KPA = {
        "kPa": 1.0,
        "bar": 100.0,
        "mbar": 0.1,
        "MPa": 1000.0,
        "psi": 6.894757,
        "kgf/cm²": 98.0665,
        "mmH2O": 0.00980665,
        "cmH2O": 0.0980665,
        "inH2O": 0.2490889,
        "mmHg": 0.133322,
        "inHg": 3.386389,
    }
    _PRESSURE_UNITS = (
        "psi",
        "bar",
        "mbar",
        "kPa",
        "MPa",
        "kgf/cm²",
        "mmH2O",
        "cmH2O",
        "inH2O",
        "mmHg",
        "inHg",
    )

    def __init__(
        self,
        master,
        *,
        read_vadc: Callable[[int], float],
        read_vadc_live: Callable[[int], float],
        set_pump: Callable[[float], None],
        set_relay: Callable[[bool], None],
        set_valve: Callable[[bool], None],
        request_event: Callable[[str, Optional[Dict[str, Any]]], None],
        export_manager: Optional[ExportManager] = None,
        usb_state_var: Optional[tk.StringVar] = None,
        retry_usb_export: Optional[Callable[[], None]] = None,
        get_usb_status_colors: Optional[Callable[[], tuple[str, str]]] = None,
        update_period_ms: int = 100,
    ):
        super().__init__(master)

        self.read_vadc = read_vadc
        self.read_vadc_live = read_vadc_live
        self.set_pump = set_pump
        self.set_relay = set_relay
        self.set_valve = set_valve
        self.request_event = request_event
        self.export_manager = export_manager
        self.usb_state_var = usb_state_var or tk.StringVar(value="USB: --")
        self.retry_usb_export = retry_usb_export
        self.get_usb_status_colors = get_usb_status_colors
        self.update_period_ms = update_period_ms
        self._screen_width = max(1, int(self.winfo_screenwidth()))
        self._screen_height = max(1, int(self.winfo_screenheight()))
        self._ui_scale = self._compute_ui_scale()

        self.cfg = AutoConfig()
        self.rt = AutoRuntime(points=[])

        # RESULTADOS (solo se añade esto, no cambia control)
        self.results: List[Dict[str, Any]] = []
        self._results_win: Optional[tk.Toplevel] = None
        self._settings_window: Optional[tk.Toplevel] = None
        self._settings_snapshot: Optional[Dict[str, Any]] = None
        self.btn_seq_points = None
        self.btn_seq_dir = None
        self.btn_start = None
        self.btn_zero = None
        self.btn_stop_cfg = None
        self.btn_settings = None
        self.btn_pressure_unit = None
        self.btn_sig_min = None
        self.btn_sig_max = None
        self.btn_pmin = None
        self.btn_pmax = None
        self.btn_npts = None
        self.btn_dir = None
        self.btn_tsettle = None
        self.btn_tmax = None
        self.lbl_status = None
        self.lbl_cycle = None
        self.lbl_flow_notice = None
        self.lbl_usb_state = None
        self.btn_usb_retry = None
        self._plot_host = None
        self._fig_registered: Optional[Figure] = None
        self._ax_registered = None
        self._canvas_registered: Optional[FigureCanvasTkAgg] = None
        self._line_registered = None

        # Variables Tk de configuracion
        self.var_mode = tk.StringVar(value=self.cfg.dut_mode)
        self.var_sigmin_label = tk.StringVar(value="I min")
        self.var_sigmax_label = tk.StringVar(value="I max")
        self.var_pressure_unit = tk.StringVar(value="kPa")
        self.var_pmin_label = tk.StringVar(value="P min (kPa)")
        self.var_pmax_label = tk.StringVar(value="P max (kPa)")
        self.var_sig_min = tk.StringVar(value=f"{self.cfg.sig_min:.3f}")
        self.var_sig_max = tk.StringVar(value=f"{self.cfg.sig_max:.3f}")
        self.var_pmin = tk.StringVar(value=self._fmt_display_pressure(self.cfg.p_min_kpa))
        self.var_pmax = tk.StringVar(value=self._fmt_display_pressure(self.cfg.p_max_kpa))
        self.var_npts = tk.StringVar(value=str(self.cfg.n_points))
        self.var_dir = tk.StringVar(value=self.cfg.direction)
        self.var_tsettle = tk.StringVar(value=self._fmt_display_pressure(self.cfg.settle_time_s))
        self.var_tmax = tk.StringVar(value=self._fmt_display_pressure(self.cfg.settle_time_max_s))

        # Variables Tk de vista
        self.var_temp = tk.StringVar(value="TEMP: --.- C")
        self.var_flow_notice = tk.StringVar(value="")
        self.var_p_source = tk.StringVar(value="0.00 kPa")
        self.var_dut_pressure = tk.StringVar(value="0.00 kPa")
        self.var_sig = tk.StringVar(value="0.000 mA")
        self.var_err = tk.StringVar(value="+0.00 %")
        pi_u_min, pi_u_max = self._effective_u_bounds(config.PI_CFG.u_min, config.PI_CFG.u_max)

        # PI (base IGUAL a config; en START aplicamos overrides desde cfg)
        self.pi = PIController(PIConfig(
            dt=config.PI_CFG.dt,
            u_min=pi_u_min,
            u_max=pi_u_max,
            deadband_kpa=float(getattr(config.PI_CFG, "deadband_kpa", 0.5)),
            u_ff=max(pi_u_min, min(float(config.PI_CFG.u_ff), pi_u_max)),
            kp_low=float(config.PI_CFG.kp_low),
            ki_low=float(config.PI_CFG.ki_low),
            kp_mid=float(config.PI_CFG.kp_mid),
            ki_mid=float(config.PI_CFG.ki_mid),
            kp_high=float(config.PI_CFG.kp_high),
            ki_high=float(config.PI_CFG.ki_high),
            hold_band_kpa=float(getattr(config.PI_CFG, "hold_band_kpa", 0.0)),
            kp_hold=float(getattr(config.PI_CFG, "kp_hold", 0.0)),
            ki_hold=float(getattr(config.PI_CFG, "ki_hold", 0.0)),
            i_decay_in_deadband=0.97
        ))
        self.pi_worker = PIWorker(self.pi, period_s=float(config.PI_CFG.dt))
        self.pi_worker.start()

        # dt real del PI
        self._last_tick_ts: Optional[float] = None

        # Ventana control (si está abierta)
        self._control_win: Optional[tk.Toplevel] = None

        self._build_ui()
        self._build_registered_plot()
        self._on_mode_changed()
        self._update_pressure_unit_ui()
        self._refresh_sequence_summary()
        self._refresh_registered_plot()
        self._update_action_buttons()
        self._set_status_text("IDLE")
        self._set_cycle_text("0/0")
        self._safe_outputs(valve_open=True)
        self.after(self.update_period_ms, self._tick)

    # ========================================================
    # UI
    # ========================================================
    def _build_ui_legacy(self):
        ttk.Label(self, text="MODO AUTOMÁTICO", font=("Arial", 16, "bold")).pack(pady=8)

        self.lbl_temp = ttk.Label(self, textvariable=self.var_temp, font=("Arial", 11, "bold"))
        self.lbl_temp.place(relx=1.0, x=-12, y=10, anchor="ne")

        self.lbl_status = ttk.Label(self, text="IDLE", font=("Arial", 12, "bold"))
        self.var_flow_notice = tk.StringVar(value="")
        self.lbl_flow_notice = tk.Label(
            self,
            textvariable=self.var_flow_notice,
            font=("Arial", 12, "bold"),
            fg="#b00020",
            bg="#fff3cd",
            justify="center",
            anchor="center",
            wraplength=560,
            relief="solid",
            bd=1,
            padx=8,
            pady=6,
        )
        self.lbl_flow_notice.pack(fill="x", padx=10, pady=(0, 6))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        left_panel = ttk.Frame(body)
        left_panel.pack(fill="both", expand=True)


        frm = ttk.LabelFrame(self, text="Configuración")
        frm.pack(in_=left_panel, fill="x", pady=(0, 8))

        # DUT
        self.var_mode = tk.StringVar(value="A1")
        self.var_sigmin_label = tk.StringVar(value="I mín")
        self.var_sigmax_label = tk.StringVar(value="I máx")
        self.var_pressure_unit = tk.StringVar(value="kPa")
        self.var_pmin_label = tk.StringVar(value="P min (kPa)")
        self.var_pmax_label = tk.StringVar(value="P max (kPa)")
        ttk.Radiobutton(frm, text="Transmisor de presion P/I", variable=self.var_mode, value="A1", command=self._on_mode_changed).grid(row=0, column=0, sticky="w", padx=6)
        ttk.Radiobutton(frm, text="Transmisor de presion P/V", variable=self.var_mode, value="A0", command=self._on_mode_changed).grid(row=0, column=1, sticky="w", padx=6)
        self.btn_pressure_unit = ttk.Button(frm, text=self.var_pressure_unit.get(), width=10, command=self._open_pressure_unit_selector)
        self.btn_pressure_unit.grid(row=0, column=2, padx=6, pady=2, sticky="w")

        # Señal / Presión
        self.var_sig_min = tk.StringVar(value="4.0")
        self.var_sig_max = tk.StringVar(value="20.0")
        self.var_pmin = tk.StringVar(value=self._fmt_display_pressure(self.cfg.p_min_kpa))
        self.var_pmax = tk.StringVar(value=self._fmt_display_pressure(self.cfg.p_max_kpa))

        ttk.Label(frm, textvariable=self.var_sigmin_label).grid(row=1, column=0, padx=6, pady=2, sticky="e")
        self.btn_sig_min = ttk.Button(frm, text=f"[{self.var_sig_min.get()}]", command=lambda: self._open_edit_dialog(self.var_sig_min, "Señal min", 0, 100, self.btn_sig_min))
        self.btn_sig_min.grid(row=1, column=1, padx=6, pady=2, sticky="w")

        ttk.Label(frm, textvariable=self.var_sigmax_label).grid(row=1, column=2, padx=6, pady=2, sticky="e")
        self.btn_sig_max = ttk.Button(frm, text=f"[{self.var_sig_max.get()}]", command=lambda: self._open_edit_dialog(self.var_sig_max, "Señal max", 0, 100, self.btn_sig_max))
        self.btn_sig_max.grid(row=1, column=3, padx=6, pady=2, sticky="w")

        ttk.Label(frm, textvariable=self.var_pmin_label).grid(row=2, column=0, padx=6, pady=2, sticky="e")
        self.btn_pmin = ttk.Button(frm, text=f"[{self.var_pmin.get()}]", command=self._open_edit_dialog_pmin)
        self.btn_pmin.grid(row=2, column=1, padx=6, pady=2, sticky="w")

        ttk.Label(frm, textvariable=self.var_pmax_label).grid(row=2, column=2, padx=6, pady=2, sticky="e")
        self.btn_pmax = ttk.Button(frm, text=f"[{self.var_pmax.get()}]", command=self._open_edit_dialog_pmax)
        self.btn_pmax.grid(row=2, column=3, padx=6, pady=2, sticky="w")

        # Secuencia
        self.var_npts = tk.StringVar(value="5")
        self.var_dir = tk.StringVar(value="BOTH")
        ttk.Label(frm, text="Puntos").grid(row=3, column=0, padx=6, pady=2, sticky="e")
        self.btn_npts = ttk.Button(frm, text=self.var_npts.get(), width=8, command=self._open_npts_selector)
        self.btn_npts.grid(row=3, column=1, padx=6, pady=2, sticky="w")
        ttk.Label(frm, text="Dirección").grid(row=3, column=2, padx=6, pady=2, sticky="e")
        self.btn_dir = ttk.Button(frm, text=self._direction_label(self.var_dir.get()), width=8, command=self._open_direction_selector)
        self.btn_dir.grid(row=3, column=3, padx=6, pady=2, sticky="w")

        # Tiempos (NO control)
        self.var_tsettle = tk.StringVar(value="5")
        self.var_tmax = tk.StringVar(value="10")

        ttk.Label(frm, text="Asentamiento (s)").grid(row=4, column=0, padx=6, pady=2, sticky="e")
        self.btn_tsettle = ttk.Button(frm, text=f"[{self.var_tsettle.get()}]", command=lambda: self._open_edit_dialog(self.var_tsettle, "Asentamiento (s)", 0, 60, self.btn_tsettle))
        self.btn_tsettle.grid(row=4, column=1, padx=6, pady=2, sticky="w")

        ttk.Label(frm, text="tiempo asentamiento Pmax").grid(row=4, column=2, padx=6, pady=2, sticky="e")
        self.btn_tmax = ttk.Button(frm, text=f"[{self.var_tmax.get()}]", command=lambda: self._open_edit_dialog(self.var_tmax, "P máx (s)", 0, 60, self.btn_tmax))
        self.btn_tmax.grid(row=4, column=3, padx=6, pady=2, sticky="w")

        btns = ttk.Frame(self)
        btns.pack(in_=left_panel, fill="x", pady=(0, 8))

        ttk.Button(btns, text="P=0", command=self._do_tare).grid(row=0, column=0, padx=8)
        ttk.Button(btns, text="START", command=self._start).grid(row=0, column=1, padx=8)
        ttk.Button(btns, text="STOP", command=self._stop).grid(row=0, column=2, padx=8)
        self.lbl_status.pack(in_=left_panel, fill="x", pady=(0, 4))

        self._on_mode_changed()
        self._update_pressure_unit_ui()

    def _compute_ui_scale(self) -> float:
        scale_w = float(self._screen_width) / 920.0
        scale_h = float(self._screen_height) / 540.0
        scale = min(scale_w, scale_h)
        if self._screen_height <= 480:
            scale = min(scale, 0.88)
        return max(0.78, min(scale, 1.0))

    def _sp(self, value: float, minimum: int = 0) -> int:
        return max(int(minimum), int(round(float(value) * self._ui_scale)))

    def _sf(self, size: int, weight: str = "normal") -> tuple[str, int, str]:
        return "Arial", max(8, int(round(float(size) * self._ui_scale))), weight

    def _sw(self, chars: int, minimum: int = 1) -> int:
        char_scale = max(0.82, self._ui_scale)
        return max(int(minimum), int(round(float(chars) * char_scale)))

    @staticmethod
    def _widget_exists(widget) -> bool:
        try:
            return widget is not None and bool(widget.winfo_exists())
        except Exception:
            return False

    def _get_dialog_parent_window(self):
        for candidate in (self._settings_window, self._control_win, self._results_win):
            if self._widget_exists(candidate):
                return candidate
        return self.winfo_toplevel()

    @staticmethod
    def _set_button_enabled(button, enabled: bool):
        if button is None:
            return
        state = "normal" if enabled else "disabled"
        try:
            button.state(["!disabled"] if enabled else ["disabled"])
            return
        except Exception:
            pass
        try:
            button.configure(state=state)
        except Exception:
            pass

    def _plot_signal_axis_label(self) -> str:
        return "Voltaje (V)" if self.var_mode.get().strip().upper() == "A0" else "Corriente (mA)"

    def _sequence_points_text(self) -> str:
        pts = self.var_npts.get().strip() or str(self.cfg.n_points)
        return f"[{pts} PTS]"

    def _refresh_sequence_summary(self) -> None:
        if self._widget_exists(self.btn_seq_points):
            self.btn_seq_points.configure(text=self._sequence_points_text())
        if self._widget_exists(self.btn_seq_dir):
            self.btn_seq_dir.configure(text=self._direction_label(self.var_dir.get()))

    def _set_status_text(self, text: str) -> None:
        if self._widget_exists(self.lbl_status):
            self.lbl_status.configure(text=text)

    def _set_cycle_text(self, text: str) -> None:
        if self._widget_exists(self.lbl_cycle):
            self.lbl_cycle.configure(text=text)

    def _update_action_buttons(self) -> None:
        running = bool(self.rt.running)
        self._set_button_enabled(self.btn_start, not running)
        self._set_button_enabled(self.btn_stop_cfg, running)
        self._set_button_enabled(self.btn_zero, True)
        self._set_button_enabled(self.btn_settings, not running)

    def _retry_usb_export(self) -> None:
        if callable(self.retry_usb_export):
            self.retry_usb_export()

    def _refresh_usb_widgets(self) -> None:
        if self._widget_exists(self.lbl_usb_state):
            bg = "#1f2937"
            fg = "#cbd5e1"
            if callable(self.get_usb_status_colors):
                try:
                    bg, fg = self.get_usb_status_colors()
                except Exception:
                    bg, fg = "#1f2937", "#cbd5e1"
            self.lbl_usb_state.configure(bg=bg, fg=fg)
        if self._widget_exists(self.btn_usb_retry):
            self.btn_usb_retry.configure(state="normal" if callable(self.retry_usb_export) else "disabled")

    def _update_cycle_indicator(self) -> None:
        total = len(self.rt.points)
        if total <= 0:
            self._set_cycle_text("0/0")
            return
        if self.rt.running:
            current = min(self.rt.step_index + 1, total)
        else:
            current = min(len(self.results), total)
        self._set_cycle_text(f"{current}/{total}")

    def _build_ui(self):
        sp = self._sp
        sf = self._sf
        sw = self._sw

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        shell = tk.Frame(self, bg="#0f1218", bd=2, relief="groove")
        shell.grid(row=0, column=0, sticky="nsew", padx=sp(6, 4), pady=sp(6, 4))
        shell.grid_rowconfigure(1, weight=1)
        shell.grid_columnconfigure(0, weight=1)

        header = tk.Frame(shell, bg="#171b24", bd=1, relief="groove")
        header.grid(row=0, column=0, sticky="ew", padx=sp(8, 4), pady=(sp(8, 4), sp(6, 3)))
        header.grid_columnconfigure(0, weight=1)

        title_wrap = tk.Frame(header, bg="#171b24")
        title_wrap.grid(row=0, column=0, sticky="w", padx=sp(10, 4), pady=sp(7, 3))
        tk.Label(title_wrap, text="MODO:", font=sf(16, "bold"), bg="#171b24", fg="#f1f5f9").pack(side="left")
        tk.Label(title_wrap, text=" AUTOMATICO", font=sf(20, "bold"), bg="#171b24", fg="#ffffff").pack(side="left")

        tk.Label(
            header,
            textvariable=self.var_temp,
            font=sf(18, "bold"),
            bg="#0c1018",
            fg="#f8fafc",
            bd=1,
            relief="groove",
            padx=sp(12, 6),
            pady=sp(4, 2),
        ).grid(row=0, column=1, sticky="e", padx=sp(8, 4), pady=sp(6, 3))

        body = tk.Frame(shell, bg="#0f1218")
        body.grid(row=1, column=0, sticky="nsew", padx=sp(8, 4), pady=(0, sp(6, 3)))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=3, uniform="mainbody")
        body.grid_columnconfigure(1, weight=2, uniform="mainbody")

        live_panel = tk.Frame(body, bg="#080b11", bd=2, relief="groove")
        live_panel.grid(row=0, column=0, sticky="nsew", padx=(0, sp(6, 3)))
        live_panel.grid_columnconfigure(0, weight=1)

        ref_hdr = tk.Frame(live_panel, bg="#080b11")
        ref_hdr.grid(row=0, column=0, sticky="ew", padx=sp(12, 6), pady=(sp(8, 4), sp(4, 2)))
        ref_hdr.grid_columnconfigure(0, weight=1)
        tk.Label(ref_hdr, text="PRES. REF.", font=sf(11, "bold"), bg="#080b11", fg="#f3f4f6").grid(row=0, column=0, sticky="w")
        tk.Label(
            ref_hdr,
            text="MPX5600DP",
            font=sf(12, "bold"),
            bg="#121826",
            fg="#b6c2cf",
            bd=1,
            relief="groove",
            padx=sp(8, 4),
            pady=sp(3, 1),
        ).grid(row=0, column=1, sticky="e")

        tk.Label(
            live_panel,
            textvariable=self.var_p_source,
            font=sf(42, "bold"),
            bg="#080b11",
            fg="#5ab0ff",
            anchor="e",
            justify="right",
        ).grid(row=1, column=0, sticky="ew", padx=sp(10, 4), pady=(sp(2, 1), sp(4, 2)))

        tk.Frame(live_panel, bg="#3a4150", height=sp(2, 1)).grid(row=2, column=0, sticky="ew", padx=sp(12, 6), pady=(0, sp(8, 4)))

        dut_hdr = tk.Frame(live_panel, bg="#080b11")
        dut_hdr.grid(row=3, column=0, sticky="ew", padx=sp(12, 6), pady=(0, sp(3, 1)))
        tk.Label(dut_hdr, text="PRES. DUT", font=sf(10, "bold"), bg="#080b11", fg="#f3f4f6").pack(side="left")

        tk.Label(
            live_panel,
            textvariable=self.var_dut_pressure,
            font=sf(36, "bold"),
            bg="#080b11",
            fg="#f8fafc",
            anchor="e",
            justify="right",
        ).grid(row=4, column=0, sticky="ew", padx=sp(10, 4), pady=(0, sp(2, 1)))

        tk.Label(
            live_panel,
            textvariable=self.var_sig,
            font=sf(13, "bold"),
            bg="#080b11",
            fg="#cbd5e1",
            anchor="center",
        ).grid(row=5, column=0, sticky="ew", padx=sp(10, 4), pady=(0, sp(6, 3)))

        self.lbl_flow_notice = tk.Label(
            live_panel,
            textvariable=self.var_flow_notice,
            font=sf(10, "bold"),
            fg="#fca5a5",
            bg="#201216",
            justify="center",
            anchor="center",
            wraplength=max(240, self._screen_width // 2),
            bd=1,
            relief="groove",
            padx=sp(8, 4),
            pady=sp(4, 2),
        )
        self.lbl_flow_notice.grid(row=6, column=0, sticky="ew", padx=sp(12, 6), pady=(0, sp(6, 3)))

        footer = tk.Frame(live_panel, bg="#0b0f16", bd=1, relief="groove")
        footer.grid(row=7, column=0, sticky="ew", padx=sp(12, 6), pady=(0, sp(10, 4)))
        footer.grid_columnconfigure(0, weight=9, uniform="errstate")
        footer.grid_columnconfigure(1, weight=5, uniform="errstate")

        err_box = tk.Frame(footer, bg="#0b0f16")
        err_box.grid(row=0, column=0, sticky="nsew", padx=(sp(8, 4), sp(2, 1)), pady=sp(6, 3))
        tk.Label(err_box, text="ERROR", font=sf(8, "bold"), bg="#0b0f16", fg="#f3f4f6").pack(anchor="w")
        tk.Label(err_box, textvariable=self.var_err, font=sf(30, "bold"), bg="#0b0f16", fg="#22c55e").pack(anchor="center")

        state_box = tk.Frame(footer, bg="#0b0f16")
        state_box.grid(row=0, column=1, sticky="nsew", padx=(sp(2, 1), sp(8, 4)), pady=sp(6, 3))
        tk.Label(state_box, text="ESTADO", font=sf(11, "bold"), bg="#0b0f16", fg="#f3f4f6").pack(anchor="center")
        self.lbl_status = tk.Label(state_box, text="IDLE", font=sf(14, "bold"), bg="#0b0f16", fg="#f8fafc", justify="center")
        self.lbl_status.pack(anchor="center")
        self.lbl_cycle = tk.Label(state_box, text="0/0", font=sf(12, "bold"), bg="#0b0f16", fg="#cbd5e1", justify="center")
        self.lbl_cycle.pack(anchor="center")

        plot_panel = tk.LabelFrame(
            body,
            text="CURVA DE CALIBRACION",
            font=sf(14, "bold"),
            bg="#080b11",
            fg="#f3f4f6",
            bd=2,
            relief="groove",
            labelanchor="n",
        )
        plot_panel.grid(row=0, column=1, sticky="nsew")
        plot_panel.grid_rowconfigure(0, weight=1)
        plot_panel.grid_columnconfigure(0, weight=1)
        self._plot_host = plot_panel

        controls = tk.Frame(shell, bg="#141922", bd=1, relief="groove")
        controls.grid(row=2, column=0, sticky="ew", padx=sp(8, 4), pady=(0, 0))
        controls.grid_columnconfigure(0, weight=3)
        controls.grid_columnconfigure(1, weight=5)

        usb_box = tk.Frame(controls, bg="#141922")
        usb_box.grid(row=0, column=0, sticky="w", padx=sp(10, 4), pady=sp(2, 1))

        self.lbl_usb_state = tk.Label(
            usb_box,
            textvariable=self.usb_state_var,
            font=sf(10, "bold"),
            bg="#1f2937",
            fg="#cbd5e1",
            bd=1,
            relief="groove",
            padx=sp(8, 4),
            pady=sp(3, 1),
            anchor="w",
        )
        self.lbl_usb_state.pack(side="left", padx=(0, sp(3, 2)))

        self.btn_usb_retry = tk.Button(
            usb_box,
            text="USB",
            command=self._retry_usb_export,
            font=sf(11, "bold"),
            width=sw(4, 3),
            bg="#1b2130",
            fg="#f8fafc",
            activebackground="#334155",
            activeforeground="#ffffff",
            bd=2,
            relief="raised",
            padx=sp(4, 2),
            pady=sp(3, 1),
        )
        self.btn_usb_retry.pack(side="left")

        btns = tk.Frame(controls, bg="#141922")
        btns.grid(row=0, column=1, sticky="e", padx=sp(10, 4), pady=sp(2, 1))

        def make_action_button(text, command, bg, fg="#ffffff", width=11, font_size=18, pad_x=6, pad_y=3):
            return tk.Button(
                btns,
                text=text,
                command=command,
                font=sf(font_size, "bold"),
                width=sw(width, 4),
                bg=bg,
                fg=fg,
                activebackground=bg,
                activeforeground=fg,
                bd=2,
                relief="raised",
                padx=sp(pad_x, 2),
                pady=sp(pad_y, 2),
            )

        self.btn_start = make_action_button("INICIAR", self._start, "#1f9d45", width=9, font_size=15, pad_x=5, pad_y=3)
        self.btn_zero = make_action_button("P=0", self._do_tare, "#fbbf24", fg="#111827", width=6, font_size=15, pad_x=5, pad_y=3)
        self.btn_stop_cfg = make_action_button("DETENER", self._stop, "#dc2626", width=9, font_size=15, pad_x=5, pad_y=3)
        self.btn_settings = make_action_button("\u2699", self._open_settings_window, "#111827", width=3, font_size=15, pad_x=4, pad_y=3)

        self.btn_start.pack(side="left", padx=sp(4, 2))
        self.btn_zero.pack(side="left", padx=sp(4, 2))
        self.btn_stop_cfg.pack(side="left", padx=sp(4, 2))
        self.btn_settings.pack(side="left", padx=(sp(4, 2), 0))
        self._refresh_usb_widgets()

    def _build_registered_plot(self):
        plot_box = self._plot_host
        if plot_box is None:
            return

        fig = Figure(
            figsize=(
                max(4.0, 6.0 * max(self._ui_scale, 0.80)),
                max(2.8, 4.0 * max(self._ui_scale, 0.78)),
            ),
            dpi=90,
        )
        fig.patch.set_facecolor("#080b11")
        ax = fig.add_subplot(111)
        ax.set_facecolor("#080b11")
        ax.set_title("Registro por puntos", color="#f8fafc", fontsize=max(10, self._sp(13, 10)), fontweight="bold")
        ax.set_xlabel(self._plot_signal_axis_label(), color="#e2e8f0", fontsize=max(8, self._sp(10, 8)))
        ax.set_ylabel("Presion (kPa)", color="#e2e8f0", fontsize=max(8, self._sp(10, 8)))
        ax.yaxis.labelpad = max(8, self._sp(10, 8))
        ax.tick_params(axis="x", colors="#e2e8f0", labelsize=max(7, self._sp(9, 7)))
        ax.tick_params(axis="y", colors="#e2e8f0", labelsize=max(7, self._sp(9, 7)))
        ax.grid(True, alpha=0.25, color="#94a3b8")
        for spine in ax.spines.values():
            spine.set_color("#94a3b8")
        ax.set_xlim(float(self.cfg.sig_min), float(self.cfg.sig_max))
        ax.set_ylim(float(self.cfg.p_min_kpa), max(float(self.cfg.p_max_kpa), 1.0))

        line_registered, = ax.plot(
            [],
            [],
            color="#38bdf8",
            linewidth=1.8,
            marker="o",
            markersize=max(5, self._sp(6, 5)),
        )
        fig.subplots_adjust(left=0.17, right=0.95, top=0.90, bottom=0.16)

        canvas = FigureCanvasTkAgg(fig, master=plot_box)
        canvas.get_tk_widget().grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(self._sp(6, 3), self._sp(8, 4)),
            pady=(self._sp(4, 2), self._sp(4, 2)),
        )
        canvas.draw()

        self._fig_registered = fig
        self._ax_registered = ax
        self._canvas_registered = canvas
        self._line_registered = line_registered

    def _refresh_registered_plot(self) -> None:
        if self._ax_registered is None or self._canvas_registered is None or self._line_registered is None:
            return

        x = [float(row.get("dut_eng", 0.0)) for row in self.results]
        y = [float(row.get("p_kpa", 0.0)) for row in self.results]

        self._line_registered.set_data(x, y)
        self._ax_registered.set_xlabel(
            self._plot_signal_axis_label(),
            color="#e2e8f0",
            fontsize=max(8, self._sp(10, 8)),
        )

        sig_min, sig_max = self._get_live_signal_bounds()
        p_min, p_max = self._get_live_pressure_bounds()

        if x:
            x_min = min(min(x), sig_min)
            x_max = max(max(x), sig_max)
        else:
            x_min = sig_min
            x_max = sig_max
        if abs(x_max - x_min) < 1e-9:
            x_pad = max(0.5, abs(x_max) * 0.1 + 0.1)
        else:
            x_pad = max(0.1, (x_max - x_min) * 0.08)

        if y:
            y_min = min(min(y), p_min)
            y_max = max(max(y), p_max)
        else:
            y_min = p_min
            y_max = p_max
        if abs(y_max - y_min) < 1e-9:
            y_pad = max(1.0, abs(y_max) * 0.1 + 0.5)
        else:
            y_pad = max(0.5, (y_max - y_min) * 0.08)

        self._ax_registered.set_xlim(x_min - x_pad, x_max + x_pad)
        self._ax_registered.set_ylim(max(0.0, y_min - y_pad), y_max + y_pad)
        self._canvas_registered.draw_idle()

    def _close_settings_window(self, clear_snapshot: bool = False) -> None:
        win = self._settings_window
        self._settings_window = None
        for attr in (
            "btn_pressure_unit",
            "btn_sig_min",
            "btn_sig_max",
            "btn_pmin",
            "btn_pmax",
            "btn_npts",
            "btn_dir",
            "btn_tsettle",
            "btn_tmax",
        ):
            setattr(self, attr, None)
        if clear_snapshot:
            self._settings_snapshot = None
        if not self._widget_exists(win):
            return
        try:
            win.grab_release()
        except Exception:
            pass
        try:
            win.destroy()
        except Exception:
            pass

    def _capture_settings_snapshot(self) -> Dict[str, Any]:
        return {
            "var_mode": self.var_mode.get(),
            "var_pressure_unit": self.var_pressure_unit.get(),
            "var_sig_min": self.var_sig_min.get(),
            "var_sig_max": self.var_sig_max.get(),
            "var_pmin": self.var_pmin.get(),
            "var_pmax": self.var_pmax.get(),
            "var_npts": self.var_npts.get(),
            "var_dir": self.var_dir.get(),
            "var_tsettle": self.var_tsettle.get(),
            "var_tmax": self.var_tmax.get(),
            "cfg_dut_mode": str(self.cfg.dut_mode),
            "cfg_sig_min": float(self.cfg.sig_min),
            "cfg_sig_max": float(self.cfg.sig_max),
            "cfg_p_min_kpa": float(self.cfg.p_min_kpa),
            "cfg_p_max_kpa": float(self.cfg.p_max_kpa),
            "cfg_n_points": int(self.cfg.n_points),
            "cfg_direction": str(self.cfg.direction),
            "cfg_settle_time_s": float(self.cfg.settle_time_s),
            "cfg_settle_time_max_s": float(self.cfg.settle_time_max_s),
        }

    def _restore_settings_snapshot(self) -> None:
        snap = self._settings_snapshot
        if not snap:
            return

        self.var_mode.set(str(snap["var_mode"]))
        self.var_pressure_unit.set(str(snap["var_pressure_unit"]))
        self.var_sig_min.set(str(snap["var_sig_min"]))
        self.var_sig_max.set(str(snap["var_sig_max"]))
        self.var_pmin.set(str(snap["var_pmin"]))
        self.var_pmax.set(str(snap["var_pmax"]))
        self.var_npts.set(str(snap["var_npts"]))
        self.var_dir.set(str(snap["var_dir"]))
        self.var_tsettle.set(str(snap["var_tsettle"]))
        self.var_tmax.set(str(snap["var_tmax"]))

        self.cfg.dut_mode = str(snap["cfg_dut_mode"])
        self.cfg.sig_min = float(snap["cfg_sig_min"])
        self.cfg.sig_max = float(snap["cfg_sig_max"])
        self.cfg.p_min_kpa = float(snap["cfg_p_min_kpa"])
        self.cfg.p_max_kpa = float(snap["cfg_p_max_kpa"])
        self.cfg.n_points = int(snap["cfg_n_points"])
        self.cfg.direction = str(snap["cfg_direction"])
        self.cfg.settle_time_s = float(snap["cfg_settle_time_s"])
        self.cfg.settle_time_max_s = float(snap["cfg_settle_time_max_s"])

        self._update_pressure_unit_ui()
        self._on_mode_changed()
        self._refresh_sequence_summary()
        self._refresh_registered_plot()

    def _prepare_popup_same_as_main(self, window: tk.Toplevel) -> None:
        window.resizable(False, False)
        try:
            window.attributes("-topmost", True)
        except tk.TclError:
            pass
        main_window = self.winfo_toplevel()
        main_window.update_idletasks()
        window.transient(main_window)
        screen_width = max(260, int(main_window.winfo_screenwidth()))
        screen_height = max(240, int(main_window.winfo_screenheight()))

        def _apply_maximized():
            try:
                window.geometry(f"{screen_width}x{screen_height}+0+0")
            except Exception:
                pass
            try:
                if str(window.tk.call("tk", "windowingsystem")).lower() == "x11":
                    window.attributes("-fullscreen", True)
                    return
            except tk.TclError:
                pass
            try:
                window.state("zoomed")
                return
            except tk.TclError:
                pass
            try:
                window.attributes("-zoomed", True)
                return
            except tk.TclError:
                pass
            try:
                window.geometry(f"{screen_width}x{screen_height}+0+0")
            except Exception:
                pass

        _apply_maximized()
        window.lift()
        window.focus_force()
        window.grab_set()
        window.after_idle(_apply_maximized)

    def _save_settings_window(self) -> None:
        try:
            self._pull_cfg()
        except Exception as e:
            messagebox.showerror("CONFIGURACION", str(e), parent=self._settings_window)
            return

        self._refresh_sequence_summary()
        self._refresh_registered_plot()
        self._close_settings_window(clear_snapshot=True)

    def _cancel_settings_window(self) -> None:
        self._restore_settings_snapshot()
        self._close_settings_window(clear_snapshot=True)

    def _open_settings_window(self) -> None:
        if self._widget_exists(self._settings_window):
            self._settings_window.lift()
            self._settings_window.focus_force()
            return

        if self._settings_snapshot is None:
            self._settings_snapshot = self._capture_settings_snapshot()

        win = tk.Toplevel(self)
        self._settings_window = win
        win.title("Configuracion automatica")
        self._prepare_popup_same_as_main(win)

        sp = self._sp
        sf = self._sf
        sw = self._sw
        win.configure(bg="#0f1218")

        def make_value_button(parent, text, command, width=8):
            return tk.Button(
                parent,
                text=text,
                command=command,
                font=sf(15, "bold"),
                bg="#090c12",
                fg="#f8fafc",
                activebackground="#171b24",
                activeforeground="#ffffff",
                width=sw(width, 5),
                bd=2,
                relief="raised",
                padx=sp(4, 2),
                pady=sp(3, 1),
            )

        def make_action_button(parent, text, command, bg, fg="#ffffff"):
            return tk.Button(
                parent,
                text=text,
                command=command,
                font=sf(15, "bold"),
                bg=bg,
                fg=fg,
                activebackground=bg,
                activeforeground=fg,
                bd=2,
                relief="raised",
                padx=sp(6, 3),
                pady=sp(4, 2),
            )

        shell = tk.Frame(win, bg="#0f1218", bd=2, relief="groove")
        shell.pack(fill="both", expand=True, padx=sp(6, 4), pady=sp(6, 4))
        shell.grid_rowconfigure(1, weight=1)
        shell.grid_columnconfigure(0, weight=1)

        header = tk.Frame(shell, bg="#171b24", bd=1, relief="groove")
        header.grid(row=0, column=0, sticky="ew", padx=sp(8, 4), pady=(sp(8, 4), sp(6, 3)))
        header.grid_columnconfigure(0, weight=1)

        title_wrap = tk.Frame(header, bg="#171b24")
        title_wrap.grid(row=0, column=0, sticky="w", padx=sp(10, 4), pady=sp(7, 3))
        tk.Label(title_wrap, text="MODO:", font=sf(16, "bold"), bg="#171b24", fg="#f1f5f9").pack(side="left")
        tk.Label(title_wrap, text=" AUTOMATICO", font=sf(20, "bold"), bg="#171b24", fg="#ffffff").pack(side="left")
        tk.Label(title_wrap, text="  CONFIGURACION", font=sf(13, "bold"), bg="#121826", fg="#b6c2cf", bd=1, relief="groove", padx=sp(8, 4), pady=sp(2, 1)).pack(side="left", padx=(sp(10, 4), 0))

        tk.Label(
            header,
            textvariable=self.var_temp,
            font=sf(18, "bold"),
            bg="#0c1018",
            fg="#f8fafc",
            bd=1,
            relief="groove",
            padx=sp(12, 6),
            pady=sp(4, 2),
        ).grid(row=0, column=1, sticky="e", padx=sp(8, 4), pady=sp(6, 3))

        frm = tk.Frame(shell, bg="#0f1218")
        frm.grid(row=1, column=0, sticky="nsew", padx=sp(8, 4), pady=(0, sp(6, 3)))
        frm.grid_columnconfigure(0, weight=1)
        frm.grid_rowconfigure(0, weight=1)

        body = tk.Frame(frm, bg="#0f1218")
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1, uniform="auto_settings")
        body.grid_columnconfigure(1, weight=1, uniform="auto_settings")
        body.grid_rowconfigure(1, weight=1)

        dut_box = tk.LabelFrame(
            body,
            text="DUT",
            font=sf(13, "bold"),
            bg="#080b11",
            fg="#f3f4f6",
            bd=2,
            relief="groove",
            labelanchor="n",
        )
        dut_box.grid(row=0, column=0, sticky="new", padx=(0, sp(10, 5)), pady=(0, sp(10, 5)))
        dut_box.grid_columnconfigure(0, weight=1)
        tk.Radiobutton(
            dut_box,
            text="Transmisor de presion P/I",
            variable=self.var_mode,
            value="A1",
            command=self._on_mode_changed,
            font=sf(12, "bold"),
            bg="#080b11",
            fg="#f8fafc",
            selectcolor="#111827",
            activebackground="#080b11",
            activeforeground="#ffffff",
            highlightthickness=0,
            anchor="w",
            justify="left",
        ).grid(row=0, column=0, sticky="w", padx=sp(12, 6), pady=(sp(10, 5), sp(6, 3)))
        tk.Radiobutton(
            dut_box,
            text="Transmisor de presion P/V",
            variable=self.var_mode,
            value="A0",
            command=self._on_mode_changed,
            font=sf(12, "bold"),
            bg="#080b11",
            fg="#f8fafc",
            selectcolor="#111827",
            activebackground="#080b11",
            activeforeground="#ffffff",
            highlightthickness=0,
            anchor="w",
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=sp(12, 6), pady=(sp(4, 2), sp(12, 6)))

        seq_box = tk.LabelFrame(
            body,
            text="SECUENCIA",
            font=sf(13, "bold"),
            bg="#080b11",
            fg="#f3f4f6",
            bd=2,
            relief="groove",
            labelanchor="n",
        )
        seq_box.grid(row=1, column=0, sticky="nsew", padx=(0, sp(10, 5)))
        seq_box.grid_columnconfigure(1, weight=1)

        label_kwargs = {
            "font": sf(11, "bold"),
            "bg": "#080b11",
            "fg": "#f3f4f6",
            "anchor": "w",
        }

        tk.Label(seq_box, text="Puntos", **label_kwargs).grid(row=0, column=0, sticky="w", padx=sp(10, 5), pady=(sp(12, 6), sp(6, 3)))
        self.btn_npts = make_value_button(seq_box, self.var_npts.get(), self._open_npts_selector)
        self.btn_npts.grid(row=0, column=1, sticky="ew", padx=(sp(6, 3), sp(10, 5)), pady=(sp(12, 6), sp(6, 3)))

        tk.Label(seq_box, text="Direccion", **label_kwargs).grid(row=1, column=0, sticky="w", padx=sp(10, 5), pady=sp(6, 3))
        self.btn_dir = make_value_button(seq_box, self._direction_label(self.var_dir.get()), self._open_direction_selector, width=10)
        self.btn_dir.grid(row=1, column=1, sticky="ew", padx=(sp(6, 3), sp(10, 5)), pady=sp(6, 3))

        tk.Label(seq_box, text="Asentamiento (s)", **label_kwargs).grid(row=2, column=0, sticky="w", padx=sp(10, 5), pady=sp(6, 3))
        self.btn_tsettle = make_value_button(
            seq_box,
            f"[{self.var_tsettle.get()}]",
            lambda: self._open_edit_dialog(self.var_tsettle, "Asentamiento (s)", 0, 60, self.btn_tsettle),
        )
        self.btn_tsettle.grid(row=2, column=1, sticky="ew", padx=(sp(6, 3), sp(10, 5)), pady=sp(6, 3))

        tk.Label(seq_box, text="Asentamiento Pmax (s)", **label_kwargs).grid(row=3, column=0, sticky="w", padx=sp(10, 5), pady=(sp(6, 3), sp(12, 6)))
        self.btn_tmax = make_value_button(
            seq_box,
            f"[{self.var_tmax.get()}]",
            lambda: self._open_edit_dialog(self.var_tmax, "P max (s)", 0, 60, self.btn_tmax),
        )
        self.btn_tmax.grid(row=3, column=1, sticky="ew", padx=(sp(6, 3), sp(10, 5)), pady=(sp(6, 3), sp(12, 6)))

        range_box = tk.LabelFrame(
            body,
            text="RANGOS",
            font=sf(13, "bold"),
            bg="#080b11",
            fg="#f3f4f6",
            bd=2,
            relief="groove",
            labelanchor="n",
        )
        range_box.grid(row=0, column=1, rowspan=2, sticky="nsew", pady=(0, sp(10, 5)))
        range_box.grid_columnconfigure(1, weight=1)
        range_box.grid_columnconfigure(2, weight=0)

        unit_kwargs = {
            "font": sf(11, "bold"),
            "bg": "#080b11",
            "fg": "#cbd5e1",
            "anchor": "w",
        }

        tk.Label(range_box, text="Unidad", **label_kwargs).grid(row=0, column=0, sticky="w", padx=sp(10, 5), pady=(sp(12, 6), sp(6, 3)))
        self.btn_pressure_unit = make_value_button(range_box, self.var_pressure_unit.get(), self._open_pressure_unit_selector, width=7)
        self.btn_pressure_unit.grid(row=0, column=1, columnspan=2, sticky="ew", padx=sp(6, 3), pady=(sp(12, 6), sp(6, 3)))

        tk.Label(range_box, textvariable=self.var_pmin_label, **label_kwargs).grid(row=1, column=0, sticky="w", padx=sp(10, 5), pady=sp(6, 3))
        self.btn_pmin = make_value_button(range_box, f"[{self.var_pmin.get()}]", self._open_edit_dialog_pmin)
        self.btn_pmin.grid(row=1, column=1, sticky="ew", padx=sp(6, 3), pady=sp(6, 3))

        tk.Label(range_box, textvariable=self.var_pmax_label, **label_kwargs).grid(row=2, column=0, sticky="w", padx=sp(10, 5), pady=sp(6, 3))
        self.btn_pmax = make_value_button(range_box, f"[{self.var_pmax.get()}]", self._open_edit_dialog_pmax)
        self.btn_pmax.grid(row=2, column=1, sticky="ew", padx=sp(6, 3), pady=sp(6, 3))

        tk.Label(range_box, textvariable=self.var_sigmin_label, **label_kwargs).grid(row=3, column=0, sticky="w", padx=sp(10, 5), pady=sp(6, 3))
        self.btn_sig_min = make_value_button(
            range_box,
            f"[{self.var_sig_min.get()}]",
            lambda: self._open_edit_dialog(self.var_sig_min, "Senal min", 0, 100, self.btn_sig_min),
        )
        self.btn_sig_min.grid(row=3, column=1, sticky="ew", padx=sp(6, 3), pady=sp(6, 3))

        tk.Label(range_box, textvariable=self.var_sigmax_label, **label_kwargs).grid(row=4, column=0, sticky="w", padx=sp(10, 5), pady=(sp(6, 3), sp(12, 6)))
        self.btn_sig_max = make_value_button(
            range_box,
            f"[{self.var_sig_max.get()}]",
            lambda: self._open_edit_dialog(self.var_sig_max, "Senal max", 0, 100, self.btn_sig_max),
        )
        self.btn_sig_max.grid(row=4, column=1, sticky="ew", padx=sp(6, 3), pady=(sp(6, 3), sp(12, 6)))

        btns = tk.Frame(shell, bg="#141922", bd=1, relief="groove")
        btns.grid(row=2, column=0, sticky="ew", padx=sp(8, 4), pady=(0, 0))
        btns.grid_columnconfigure(0, weight=1)
        btns.grid_columnconfigure(1, weight=1)
        make_action_button(btns, "GUARDAR", self._save_settings_window, "#1f9d45").grid(
            row=0, column=0, sticky="ew", padx=(sp(10, 5), sp(5, 3)), pady=sp(8, 4)
        )
        make_action_button(btns, "CANCELAR", self._cancel_settings_window, "#dc2626").grid(
            row=0, column=1, sticky="ew", padx=(sp(5, 3), sp(10, 5)), pady=sp(8, 4)
        )

        def _on_close():
            self._cancel_settings_window()

        win.protocol("WM_DELETE_WINDOW", _on_close)
        self._on_mode_changed()
        self._update_pressure_unit_ui()
        self._refresh_sequence_summary()
        win.focus_force()

    # ========================================================
    # Modal Edit Dialog
    # ========================================================
    def _open_edit_dialog(self, var: tk.StringVar, label: str, min_val: float, max_val: float, button: ttk.Button):
        """
        Abre un diálogo modal para editar un valor numérico con teclado integrado.
        Optimizado para pantalla táctil en Raspberry Pi.
        """
        dialog = tk.Toplevel(self)
        dialog.title(f"Editar: {label}")
        dialog.geometry("320x420")
        dialog.resizable(False, False)

        # CRÍTICO para pantalla táctil: establecer atributos antes de geometry
        dialog.attributes("-topmost", True)

        # Hacer el dialog modal (bloquea eventos en la ventana principal)
        dialog.transient(self.winfo_toplevel())

        # Centrar respecto a la ventana principal (no la pantalla física)
        dialog.update_idletasks()

        # Obtener tamaño y posición de la ventana principal
        main_window = self.master if self.master else self
        main_x = main_window.winfo_x()
        main_y = main_window.winfo_y()
        main_width = main_window.winfo_width()
        main_height = main_window.winfo_height()

        # Calcular centro de la ventana principal
        center_x = main_x + main_width // 2
        center_y = main_y + main_height // 2

        # Posicionar modal en el centro
        modal_width = 320
        modal_height = 420
        x = max(0, center_x - modal_width // 2)
        y = max(0, center_y - modal_height // 2)

        dialog.geometry(f"{modal_width}x{modal_height}+{x}+{y}")

        # CRÍTICO: Capture el foco ANTES de crear los widgets
        dialog.focus_force()
        dialog.grab_set()
        dialog.update_idletasks()
        dialog.update()

        # Frame principal con padding mínimo
        frm = ttk.Frame(dialog, padding=8)
        frm.pack(fill="both", expand=True)

        # Etiqueta pequeña
        ttk.Label(frm, text=label, font=("Arial", 11, "bold")).pack(pady=(0, 2))
        ttk.Label(frm, text=f"Rango: {min_val} - {max_val}", font=("Arial", 8)).pack(pady=(0, 8))

        # Entry para editar
        var_edit = tk.StringVar(value=var.get())
        entry_font = tkFont.Font(family="Arial", size=14, weight="bold")
        entry = tk.Entry(frm, textvariable=var_edit, justify="center", relief="solid", borderwidth=2)
        entry.config(font=entry_font)
        entry.pack(fill="x", ipady=10, pady=(0, 10))
        entry.select_range(0, len(var_edit.get()))
        entry.focus()
        replace_on_first_input = True

        # Frame para teclado numérico
        kbd_frm = ttk.LabelFrame(frm, text="Teclado", padding=6)
        kbd_frm.pack(fill="both", expand=True, pady=(0, 8))
        def add_digit(digit):
            nonlocal replace_on_first_input
            if replace_on_first_input:
                var_edit.set(str(digit))
                replace_on_first_input = False
            else:
                current = var_edit.get()
                var_edit.set(current + str(digit))
            entry.focus()
            entry.update()

        def add_decimal():
            nonlocal replace_on_first_input
            if replace_on_first_input:
                var_edit.set("0.")
                replace_on_first_input = False
            else:
                current = var_edit.get()
                if "." not in current:
                    var_edit.set(current + ".")
            entry.focus()
            entry.update()

        def delete_last():
            nonlocal replace_on_first_input
            if replace_on_first_input:
                var_edit.set("")
            else:
                current = var_edit.get()
                var_edit.set(current[:-1] if current else "")
            entry.focus()
            entry.update()

        def clear_all():
            nonlocal replace_on_first_input
            var_edit.set("")
            replace_on_first_input = False
            entry.focus()
            entry.update()

        # Crear botones del teclado - REDUCIDOS
        btn_font = tkFont.Font(family="Arial", size=10, weight="bold")
        btn_width = 3
        btn_height = 1

        # Fila 1: 7, 8, 9
        row_frm = ttk.Frame(kbd_frm)
        row_frm.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Button(row_frm, text="7", width=btn_width, height=btn_height, command=lambda: add_digit(7),
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="8", width=btn_width, height=btn_height, command=lambda: add_digit(8),
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="9", width=btn_width, height=btn_height, command=lambda: add_digit(9),
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")

        # Fila 2: 4, 5, 6
        row_frm = ttk.Frame(kbd_frm)
        row_frm.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Button(row_frm, text="4", width=btn_width, height=btn_height, command=lambda: add_digit(4),
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="5", width=btn_width, height=btn_height, command=lambda: add_digit(5),
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="6", width=btn_width, height=btn_height, command=lambda: add_digit(6),
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")

        # Fila 3: 1, 2, 3
        row_frm = ttk.Frame(kbd_frm)
        row_frm.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Button(row_frm, text="1", width=btn_width, height=btn_height, command=lambda: add_digit(1),
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="2", width=btn_width, height=btn_height, command=lambda: add_digit(2),
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="3", width=btn_width, height=btn_height, command=lambda: add_digit(3),
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")

        # Fila 4: 0, punto, borrar
        row_frm = ttk.Frame(kbd_frm)
        row_frm.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Button(row_frm, text="0", width=btn_width, height=btn_height, command=lambda: add_digit(0),
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text=".", width=btn_width, height=btn_height, command=add_decimal,
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="←", width=btn_width, height=btn_height, command=delete_last,
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")

        # Borrar todo - botón compacto
        ttk.Button(kbd_frm, text="Borrar todo", command=clear_all).pack(fill="x", padx=2, pady=3)

        # Frame para botones de guardar/cancelar
        action_frm = ttk.Frame(frm)
        action_frm.pack(fill="x", pady=(6, 0))

        def on_save():
            try:
                valor = float(var_edit.get().strip().replace(",", "."))

                # Validar rango
                if valor < min_val or valor > max_val:
                    raise ValueError(f"Valor fuera de rango [{min_val}, {max_val}]")

                # Guardar en la variable
                var.set(str(valor))

                # Actualizar el botón inmediatamente
                button.config(text=f"[{valor}]")
                self._refresh_sequence_summary()
                self._refresh_registered_plot()

                dialog.destroy()
            except ValueError as e:
                messagebox.showerror("Error", f"Valor inválido: {str(e)}")

        def on_cancel():
            dialog.destroy()

        # Botones de acción compactos
        ttk.Button(action_frm, text="✓ Guardar", command=on_save).pack(side="left", padx=2, pady=2, fill="both", expand=True)
        ttk.Button(action_frm, text="✕ Cancelar", command=on_cancel).pack(side="left", padx=2, pady=2, fill="both", expand=True)

        # Enter para guardar
        entry.bind("<Return>", lambda e: on_save())
        # Escape para cancelar
        entry.bind("<Escape>", lambda e: on_cancel())

        # Esperar a que el modal se cierre
        dialog.wait_window()

    def _open_edit_dialog(self, var: tk.StringVar, label: str, min_val: float, max_val: float, button: ttk.Button):
        def _save(raw_value: str) -> None:
            valor = float(raw_value.strip().replace(",", "."))
            if valor < min_val or valor > max_val:
                raise ValueError(f"Valor fuera de rango [{min_val}, {max_val}]")

            var.set(str(valor))
            if self._widget_exists(button):
                button.config(text=f"[{valor}]")
            self._refresh_sequence_summary()
            self._refresh_registered_plot()

        open_numeric_keypad_dialog(
            self,
            title=label,
            range_text=f"Rango: {min_val} - {max_val}",
            initial_value=var.get(),
            on_save=_save,
            error_title="Error",
            error_mode="error",
        )

    def _update_button_display(self):
        """
        Actualiza el texto de los botones para mostrar el valor actual.
        Esto es un placeholder que se puede mejorar si es necesario.
        """
        pass

    def _on_mode_changed(self):
        mode = self.var_mode.get().strip()
        if mode not in ("A0", "A1"):
            mode = "A1"
            self.var_mode.set(mode)
        self.cfg.dut_mode = mode

        def _parse_sig(var: tk.StringVar, fallback: float) -> float:
            try:
                return float(var.get().strip().replace(",", "."))
            except Exception:
                return float(fallback)

        def _is_close_pair(vmin: float, vmax: float, rmin: float, rmax: float, tol: float = 1e-6) -> bool:
            return abs(vmin - rmin) <= tol and abs(vmax - rmax) <= tol

        sig_min = _parse_sig(self.var_sig_min, self.cfg.sig_min)
        sig_max = _parse_sig(self.var_sig_max, self.cfg.sig_max)

        if mode == "A0":
            self.var_sigmin_label.set("V mín")
            self.var_sigmax_label.set("V máx")
            if _is_close_pair(sig_min, sig_max, self._A1_SIG_MIN_DEFAULT, self._A1_SIG_MAX_DEFAULT):
                sig_min = self._A0_SIG_MIN_DEFAULT
                sig_max = self._A0_SIG_MAX_DEFAULT
                self.var_sig_min.set(f"{sig_min:.3f}")
                self.var_sig_max.set(f"{sig_max:.3f}")
        else:
            self.var_sigmin_label.set("I mín")
            self.var_sigmax_label.set("I máx")
            if _is_close_pair(sig_min, sig_max, self._A0_SIG_MIN_DEFAULT, self._A0_SIG_MAX_DEFAULT):
                sig_min = self._A1_SIG_MIN_DEFAULT
                sig_max = self._A1_SIG_MAX_DEFAULT
                self.var_sig_min.set(f"{sig_min:.3f}")
                self.var_sig_max.set(f"{sig_max:.3f}")

        self.cfg.sig_min = float(sig_min)
        self.cfg.sig_max = float(sig_max)
        if self._widget_exists(self.btn_sig_min):
            self.btn_sig_min.configure(text=f"[{self.var_sig_min.get()}]")
        if self._widget_exists(self.btn_sig_max):
            self.btn_sig_max.configure(text=f"[{self.var_sig_max.get()}]")
        self._refresh_registered_plot()

    def _pressure_display_to_kpa(self, display_value: float, unit: Optional[str] = None) -> float:
        active_unit = (unit or self.var_pressure_unit.get().strip() or "kPa")
        factor = float(self._UNIT_TO_KPA.get(active_unit, 1.0))
        return float(display_value) * factor

    def _pressure_kpa_to_display(self, kpa_value: float, unit: Optional[str] = None) -> float:
        active_unit = (unit or self.var_pressure_unit.get().strip() or "kPa")
        factor = float(self._UNIT_TO_KPA.get(active_unit, 1.0))
        return float(kpa_value) / factor if abs(factor) > 1e-12 else float(kpa_value)

    def _fmt_display_pressure(self, value: float) -> str:
        txt = f"{float(value):.4f}".rstrip("0").rstrip(".")
        return txt if txt else "0"

    def _parse_display_pressure_kpa(self, raw: str, field_name: str, unit: Optional[str] = None) -> float:
        try:
            display_value = float(raw.strip().replace(",", "."))
        except Exception:
            raise ValueError(f"{field_name}: valor inválido.")

        value_kpa = self._pressure_display_to_kpa(display_value, unit=unit)
        if value_kpa < self._PRESSURE_MIN_KPA or value_kpa > self._PRESSURE_MAX_KPA:
            active_unit = unit or self.var_pressure_unit.get().strip() or "kPa"
            raise ValueError(
                f"{field_name}: fuera de rango físico 0-200 kPa. "
                f"({display_value:.4f} {active_unit} = {value_kpa:.4f} kPa)"
            )
        return float(value_kpa)

    def _sync_pressure_display_from_kpa(self):
        self.var_pmin.set(self._fmt_display_pressure(self._pressure_kpa_to_display(self.cfg.p_min_kpa)))
        self.var_pmax.set(self._fmt_display_pressure(self._pressure_kpa_to_display(self.cfg.p_max_kpa)))
        if self._widget_exists(self.btn_pmin):
            self.btn_pmin.configure(text=f"[{self.var_pmin.get()}]")
        if self._widget_exists(self.btn_pmax):
            self.btn_pmax.configure(text=f"[{self.var_pmax.get()}]")

    def _update_pressure_unit_ui(self):
        unit = self.var_pressure_unit.get().strip() or "kPa"
        if unit not in self._UNIT_TO_KPA:
            unit = "kPa"
        self.var_pressure_unit.set(unit)
        self.var_pmin_label.set(f"P min ({unit})")
        self.var_pmax_label.set(f"P max ({unit})")
        if self._widget_exists(self.btn_pressure_unit):
            self.btn_pressure_unit.configure(text=unit)
        self._sync_pressure_display_from_kpa()

    def _set_pressure_unit(self, new_unit: str):
        current_unit = self.var_pressure_unit.get().strip() or "kPa"
        if current_unit not in self._UNIT_TO_KPA:
            current_unit = "kPa"
        try:
            self.cfg.p_min_kpa = self._parse_display_pressure_kpa(self.var_pmin.get(), "P min", unit=current_unit)
            self.cfg.p_max_kpa = self._parse_display_pressure_kpa(self.var_pmax.get(), "P max", unit=current_unit)
        except ValueError:
            pass
        self.var_pressure_unit.set(new_unit)
        self._update_pressure_unit_ui()
        self._refresh_registered_plot()

    def _open_edit_dialog_pmin(self):
        unit = self.var_pressure_unit.get().strip() or "kPa"
        min_val = self._pressure_kpa_to_display(self._PRESSURE_MIN_KPA, unit=unit)
        max_val = self._pressure_kpa_to_display(self._PRESSURE_MAX_KPA, unit=unit)
        self._open_edit_dialog(self.var_pmin, f"P min ({unit})", min_val, max_val, self.btn_pmin)
        self.cfg.p_min_kpa = self._parse_display_pressure_kpa(self.var_pmin.get(), "P min", unit=unit)
        self._refresh_registered_plot()

    def _open_edit_dialog_pmax(self):
        unit = self.var_pressure_unit.get().strip() or "kPa"
        min_val = self._pressure_kpa_to_display(self._PRESSURE_MIN_KPA, unit=unit)
        max_val = self._pressure_kpa_to_display(self._PRESSURE_MAX_KPA, unit=unit)
        self._open_edit_dialog(self.var_pmax, f"P max ({unit})", min_val, max_val, self.btn_pmax)
        self.cfg.p_max_kpa = self._parse_display_pressure_kpa(self.var_pmax.get(), "P max", unit=unit)
        self._refresh_registered_plot()

    def _open_pressure_unit_selector(self):
        current = self.var_pressure_unit.get().strip() or "kPa"
        try:
            idx = self._PRESSURE_UNITS.index(current)
        except ValueError:
            idx = self._PRESSURE_UNITS.index("kPa")
        self._open_overlay_list_selector(
            label="Unidad de presion",
            options=list(self._PRESSURE_UNITS),
            current_index=idx,
            on_save_index=lambda selected_idx: self._set_pressure_unit(self._PRESSURE_UNITS[selected_idx]),
        )
        return

        parent_window = self._get_dialog_parent_window()
        dialog = tk.Toplevel(parent_window)
        dialog.title("Seleccionar unidad")
        dialog.geometry("280x360")
        dialog.resizable(False, False)
        dialog.transient(parent_window)
        previous_grab = dialog.grab_current()
        parent_disabled = False
        try:
            parent_window.wm_attributes("-disabled", True)
            parent_disabled = True
        except tk.TclError:
            pass
        dialog.focus_force()
        dialog.grab_set()
        dialog.lift(parent_window)

        frm = ttk.Frame(dialog, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Unidad de presión", font=("Arial", 11, "bold")).pack(anchor="w", pady=(0, 6))

        list_frm = ttk.Frame(frm)
        list_frm.pack(fill="both", expand=True)

        yscroll = ttk.Scrollbar(list_frm, orient="vertical")
        yscroll.pack(side="right", fill="y")

        lst_units = tk.Listbox(list_frm, exportselection=False, yscrollcommand=yscroll.set, height=11)
        lst_units.pack(side="left", fill="both", expand=True)
        yscroll.configure(command=lst_units.yview)

        for unit in self._PRESSURE_UNITS:
            lst_units.insert("end", unit)

        current = self.var_pressure_unit.get().strip() or "kPa"
        try:
            idx = self._PRESSURE_UNITS.index(current)
        except ValueError:
            idx = self._PRESSURE_UNITS.index("kPa")
        lst_units.selection_set(idx)
        lst_units.activate(idx)
        lst_units.see(idx)

        action_frm = ttk.Frame(frm)
        action_frm.pack(fill="x", pady=(8, 0))

        def _close_dialog():
            try:
                dialog.grab_release()
            except Exception:
                pass
            try:
                dialog.destroy()
            finally:
                if parent_disabled:
                    try:
                        parent_window.wm_attributes("-disabled", False)
                    except tk.TclError:
                        pass
                try:
                    if self._widget_exists(parent_window):
                        parent_window.lift()
                        parent_window.focus_force()
                except Exception:
                    pass
                try:
                    if previous_grab is not None and bool(previous_grab.winfo_exists()):
                        previous_grab.grab_set()
                except Exception:
                    pass

        def on_save():
            sel = lst_units.curselection()
            if not sel:
                return
            self._set_pressure_unit(self._PRESSURE_UNITS[int(sel[0])])
            _close_dialog()

        def on_cancel():
            _close_dialog()

        ttk.Button(action_frm, text="Guardar", command=on_save).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(action_frm, text="Cancelar", command=on_cancel).pack(side="left", fill="x", expand=True, padx=(4, 0))

        lst_units.bind("<Double-Button-1>", lambda _e: on_save())
        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        dialog.wait_window()

    def _open_overlay_list_selector(
        self,
        *,
        label: str,
        options: List[str],
        current_index: int,
        on_save_index: Callable[[int], None],
        display_options: Optional[List[str]] = None,
    ) -> None:
        parent_window = self._get_dialog_parent_window()
        parent_window.update_idletasks()
        previous_grab = parent_window.grab_current()
        display_values = display_options if display_options is not None else options

        screen_width = max(320, int(parent_window.winfo_width() or parent_window.winfo_screenwidth()))
        screen_height = max(320, int(parent_window.winfo_height() or parent_window.winfo_screenheight()))
        width = min(max(self._sp(320, 280), 280), max(280, screen_width - self._sp(24, 16)))
        height = min(max(self._sp(420, 340), 340), max(340, screen_height - self._sp(24, 16)))

        overlay = tk.Frame(parent_window, bg="#05070b", highlightthickness=0, bd=0)
        overlay.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)
        overlay.lift()

        shell = tk.Frame(
            overlay,
            bg="#0d1117",
            highlightthickness=1,
            highlightbackground="#303844",
            bd=0,
            padx=self._sp(10, 8),
            pady=self._sp(10, 8),
        )
        shell.place(relx=0.5, rely=0.5, anchor="center", width=width, height=height)

        def _close_overlay() -> None:
            try:
                overlay.grab_release()
            except Exception:
                pass
            try:
                overlay.destroy()
            finally:
                try:
                    if previous_grab is not None and bool(previous_grab.winfo_exists()):
                        previous_grab.grab_set()
                except Exception:
                    pass
                try:
                    if self._widget_exists(parent_window):
                        parent_window.focus_force()
                except Exception:
                    pass

        tk.Label(
            shell,
            text=label,
            font=("Arial", self._sp(16, 13), "bold"),
            bg="#0d1117",
            fg="#f3f4f6",
            anchor="w",
        ).pack(fill="x", pady=(0, self._sp(8, 5)))

        list_frm = tk.Frame(shell, bg="#0d1117")
        list_frm.pack(fill="both", expand=True)

        yscroll = ttk.Scrollbar(list_frm, orient="vertical")
        yscroll.pack(side="right", fill="y")

        lst_values = tk.Listbox(
            list_frm,
            exportselection=False,
            yscrollcommand=yscroll.set,
            height=min(11, max(3, len(options))),
            bg="#161b22",
            fg="#f3f4f6",
            selectbackground="#2563eb",
            selectforeground="#ffffff",
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground="#3a4452",
            font=("Arial", self._sp(15, 12), "bold"),
        )
        lst_values.pack(side="left", fill="both", expand=True)
        yscroll.configure(command=lst_values.yview)

        for option in display_values:
            lst_values.insert("end", option)

        idx = max(0, min(int(current_index), len(options) - 1 if options else 0))
        if options:
            lst_values.selection_set(idx)
            lst_values.activate(idx)
            lst_values.see(idx)

        def _consume_overlay_click(_event=None) -> str:
            try:
                lst_values.focus_set()
            except Exception:
                pass
            return "break"

        overlay.bind("<ButtonPress>", _consume_overlay_click)
        overlay.bind("<FocusIn>", _consume_overlay_click)
        try:
            overlay.grab_set_global()
        except tk.TclError:
            overlay.grab_set()
        overlay.focus_force()

        actions = tk.Frame(shell, bg="#0d1117")
        actions.pack(fill="x", pady=(self._sp(10, 6), 0))
        actions.grid_columnconfigure(0, weight=1, uniform="overlay_actions")
        actions.grid_columnconfigure(1, weight=1, uniform="overlay_actions")

        def _save() -> None:
            sel = lst_values.curselection()
            if not sel:
                return
            on_save_index(int(sel[0]))
            _close_overlay()

        def _cancel() -> None:
            _close_overlay()

        tk.Button(
            actions,
            text="Guardar",
            command=_save,
            bg="#123019",
            fg="#86efac",
            activebackground="#184222",
            activeforeground="#86efac",
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground="#4ade80",
            highlightcolor="#4ade80",
            font=("Arial", self._sp(15, 12), "bold"),
            padx=self._sp(6, 4),
            pady=self._sp(6, 4),
        ).grid(row=0, column=0, sticky="ew", padx=(0, self._sp(6, 4)))
        tk.Button(
            actions,
            text="Cancelar",
            command=_cancel,
            bg="#11161d",
            fg="#c6ccd4",
            activebackground="#1a222d",
            activeforeground="#c6ccd4",
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground="#3a4452",
            highlightcolor="#3a4452",
            font=("Arial", self._sp(15, 12), "bold"),
            padx=self._sp(6, 4),
            pady=self._sp(6, 4),
        ).grid(row=0, column=1, sticky="ew", padx=(self._sp(6, 4), 0))

        lst_values.bind("<Double-Button-1>", lambda _e: _save())
        lst_values.focus_set()
        parent_window.wait_window(overlay)

    def _open_list_selector(self, *, title: str, label: str, options: List[str], current_value: str, on_save_value: Callable[[str], None]):
        del title
        try:
            idx = options.index(current_value)
        except ValueError:
            idx = 0
        self._open_overlay_list_selector(
            label=label,
            options=options,
            current_index=idx,
            on_save_index=lambda selected_idx: on_save_value(options[selected_idx]),
        )

    def _open_npts_selector(self):
        options = ["2", "3", "5"]
        self._open_list_selector(
            title="Seleccionar puntos",
            label="Cantidad de puntos",
            options=options,
            current_value=self.var_npts.get().strip(),
            on_save_value=self._set_npts_value,
        )

    def _set_npts_value(self, value: str):
        self.var_npts.set(value)
        if self._widget_exists(self.btn_npts):
            self.btn_npts.configure(text=value)
        self._refresh_sequence_summary()

    def _direction_label(self, value: str) -> str:
        mapping = {
            "UP": "SUBIDA",
            "DOWN": "BAJADA",
            "BOTH": "AMBOS",
        }
        return mapping.get((value or "").strip().upper(), "AMBOS")

    def _open_direction_selector(self):
        options = ["UP", "DOWN", "BOTH"]
        current_value = self.var_dir.get().strip().upper()
        try:
            idx = options.index(current_value)
        except ValueError:
            idx = 0
        self._open_overlay_list_selector(
            label="Dirección",
            options=options,
            current_index=idx,
            on_save_index=lambda selected_idx: self._set_direction_value(options[selected_idx]),
            display_options=[self._direction_label(option) for option in options],
        )

    def _set_direction_value(self, value: str):
        self.var_dir.set(value)
        if self._widget_exists(self.btn_dir):
            self.btn_dir.configure(text=self._direction_label(value))
        self._refresh_sequence_summary()

    # ========================================================
    # Control window
    # ========================================================
    def _open_control_window(self):
        if self._control_win is not None and self._control_win.winfo_exists():
            self._control_win.lift()
            self._control_win.focus_force()
            return

        win = tk.Toplevel(self)
        self._control_win = win
        win.title("Condiciones de control")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.transient(self.winfo_toplevel())

        self.var_deadband = tk.StringVar(value=f"{self.cfg.deadband_kpa:.3f}")
        self.var_inband_up = tk.StringVar(value=f"{self.cfg.inband_up_s:.3f}")
        self.var_inband_down = tk.StringVar(value=f"{self.cfg.inband_down_s:.3f}")
        self.var_u_min = tk.StringVar(value=f"{self.cfg.u_min:.3f}")
        self.var_u_max = tk.StringVar(value=f"{self.cfg.u_max:.3f}")
        self.var_u_ff = tk.StringVar(value=f"{self.cfg.u_ff:.3f}")

        frm = ttk.Frame(win, padding=20)
        frm.grid(row=0, column=0)

        # Fuente más grande
        lbl_font = ("Arial", 13)
        entry_font = ("Arial", 14, "bold")
        def _control_entry_row(row: int, label: str, var: tk.StringVar):
            ttk.Label(frm, text=label, font=lbl_font).grid(row=row, column=0, sticky="e", padx=12, pady=10)
            entry = tk.Entry(
                frm,
                textvariable=var,
                justify="center",
                relief="solid",
                borderwidth=2,
                takefocus=True,
                width=18,
                font=entry_font,
            )
            entry.grid(row=row, column=1, sticky="ew", padx=12, pady=10)
            entry.bind("<Button-1>", lambda _e, w=entry: w.focus_set())
            entry.bind("<FocusIn>", lambda _e, w=entry: w.icursor("end"))
            return entry

        r = 0
        self.entry_deadband = _control_entry_row(r, "Banda muerta (kPa)", self.var_deadband)
        r += 1

        self.entry_inband_up = _control_entry_row(r, "Tiempo en banda SUBIDA (s)", self.var_inband_up)
        r += 1

        self.entry_inband_down = _control_entry_row(r, "Tiempo en banda BAJADA (s)", self.var_inband_down)
        r += 1

        ttk.Separator(frm).grid(row=r, column=0, columnspan=2, sticky="we", pady=12)
        r += 1

        self.entry_u_min = _control_entry_row(r, "U minima", self.var_u_min)
        r += 1

        self.entry_u_max = _control_entry_row(r, "U maxima", self.var_u_max)
        r += 1

        self.entry_u_ff = _control_entry_row(r, "U feedforward (Uff)", self.var_u_ff)
        r += 1

        ttk.Label(frm, text="Nota: en BAJADA la electroválvula se cierra 0.5 s después de llegar al deadband.", font=("Arial", 10))\
            .grid(row=r, column=0, columnspan=2, sticky="w", padx=6, pady=(15, 5))
        r += 1

        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=2, pady=(15, 0))

        ttk.Button(btns, text="Guardar", command=self._save_control_window).grid(row=0, column=0, padx=12, ipady=8)
        self._control_close_btn = ttk.Button(btns, text="Cerrar", command=win.destroy)
        self._control_close_btn.grid(row=0, column=1, padx=12, ipady=8)

        def _on_close():
            try:
                try:
                    win.grab_release()
                except tk.TclError:
                    pass
                win.destroy()
            finally:
                self._control_win = None

        self._control_close_btn.configure(command=_on_close)
        win.focus_force()
        win.grab_set()
        self.entry_deadband.focus_set()
        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _save_control_window(self):
        try:
            dead = float(self.var_deadband.get())
            inu = float(self.var_inband_up.get())
            ind = float(self.var_inband_down.get())
            umin = float(self.var_u_min.get())
            umax = float(self.var_u_max.get())
            uff = float(self.var_u_ff.get())

            if dead < 0:
                raise ValueError("La banda muerta debe ser >= 0.")
            if inu < 0 or ind < 0:
                raise ValueError("Los tiempos en banda deben ser >= 0.")
            if not (0.0 <= umin <= 1.0) or not (0.0 <= umax <= 1.0):
                raise ValueError("Umin/Umax deben estar entre 0.0 y 1.0.")
            if umin >= umax:
                raise ValueError("Umin debe ser < Umax.")
            if not (0.0 <= uff <= 1.0):
                raise ValueError("Uff debe estar entre 0.0 y 1.0.")

            self.cfg.deadband_kpa = float(dead)
            self.cfg.inband_up_s = float(inu)
            self.cfg.inband_down_s = float(ind)
            self.cfg.u_min = float(umin)
            self.cfg.u_max = float(umax)
            self.cfg.u_ff = float(uff)

            messagebox.showinfo("Control", "Condiciones de control guardadas.")
        except Exception as e:
            messagebox.showerror("Control", str(e))

    # ========================================================
    # CONFIG / POINTS
    # ========================================================
    def _pull_cfg(self):
        self.cfg.dut_mode = self.var_mode.get().strip().upper()
        self.cfg.sig_min = float(self.var_sig_min.get().strip().replace(",", "."))
        self.cfg.sig_max = float(self.var_sig_max.get().strip().replace(",", "."))
        self.cfg.p_min_kpa = self._parse_display_pressure_kpa(self.var_pmin.get(), "P min")
        self.cfg.p_max_kpa = self._parse_display_pressure_kpa(self.var_pmax.get(), "P max")
        self.cfg.n_points = int(self.var_npts.get().strip())
        self.cfg.direction = self.var_dir.get().strip().upper()
        self.cfg.settle_time_s = float(self.var_tsettle.get().strip().replace(",", "."))
        self.cfg.settle_time_max_s = float(self.var_tmax.get().strip().replace(",", "."))

        if self.cfg.p_max_kpa <= self.cfg.p_min_kpa:
            raise ValueError("P max debe ser mayor que P min.")
        if self.cfg.sig_max <= self.cfg.sig_min:
            raise ValueError("Señal max debe ser mayor que señal min.")
        if self.cfg.n_points not in (2, 3, 5):
            raise ValueError("N puntos debe ser 2, 3 o 5.")
        if self.cfg.direction not in ("UP", "DOWN", "BOTH"):
            raise ValueError("Dirección debe ser UP/DOWN/BOTH.")
        if self.cfg.settle_time_s < 0 or self.cfg.settle_time_max_s < 0:
            raise ValueError("Tiempos deben ser >= 0.")

        if self.cfg.deadband_kpa < 0:
            raise ValueError("Banda muerta debe ser >= 0.")
        if self.cfg.inband_up_s < 0 or self.cfg.inband_down_s < 0:
            raise ValueError("Tiempos en banda deben ser >= 0.")
        if not (0.0 <= self.cfg.u_min <= 1.0) or not (0.0 <= self.cfg.u_max <= 1.0):
            raise ValueError("Umin/Umax deben estar entre 0.0 y 1.0.")
        if self.cfg.u_min >= self.cfg.u_max:
            raise ValueError("Umin debe ser < Umax.")
        if not (0.0 <= self.cfg.u_ff <= 1.0):
            raise ValueError("Uff debe estar entre 0.0 y 1.0.")

    def _build_points(self) -> List[float]:
        pmax = float(self.cfg.p_max_kpa)
        n = int(self.cfg.n_points)

        if n == 2:
            base = [0.0, pmax]
        elif n == 3:
            base = [0.0, 0.5 * pmax, pmax]
        else:
            base = [0.0, 0.25 * pmax, 0.5 * pmax, 0.75 * pmax, pmax]

        if self.cfg.direction == "DOWN":
            return list(reversed(base))
        if self.cfg.direction == "BOTH":
            return base + list(reversed(base[:-1]))
        return base

    # ========================================================
    # TARA
    # ========================================================
    def _do_tare(self):
        try:
            p_corr = self._read_pressure_corr_kpa()
            self.rt.p_zero_kpa = p_corr
            self.rt.tare_done = True
            messagebox.showinfo("TARA", f"Tara OK.\nPcorr={p_corr:.2f} kPa → P≈0 desde ahora.")
        except Exception as e:
            messagebox.showerror("TARA", str(e))

    # ========================================================
    # START / STOP
    # ========================================================
    def _start(self):
        try:
            self._pull_cfg()
            self._clear_flow_notice()

            # (control igual)
            self.pi.cfg.deadband_kpa = float(self.cfg.deadband_kpa)
            pi_u_min, pi_u_max = self._effective_u_bounds(self.cfg.u_min, self.cfg.u_max)
            self.pi.cfg.u_min = pi_u_min
            self.pi.cfg.u_max = pi_u_max
            self.pi.cfg.u_ff = max(pi_u_min, min(float(self.cfg.u_ff), pi_u_max))

            if not self.rt.tare_done:
                p_corr = self._read_pressure_corr_kpa()
                self.rt.p_zero_kpa = p_corr
                self.rt.tare_done = True

            self.rt.points = self._build_points()
            self.rt.step_index = 0
            self.rt.running = True
            self.pi_worker.reset()
            self.pi_worker.unfreeze()

            # reset resultados
            self.results = []
            self._refresh_registered_plot()

            self._last_tick_ts = None
            first_sp = float(self.rt.points[0]) if self.rt.points else 0.0
            if abs(first_sp) <= 1e-9:
                self._goto_state(HOLD_MEASURE)
            else:
                self._goto_state(GOTO_SP)
            self._set_status_text(f"RUNNING | {self.rt.state}")
            self._update_cycle_indicator()
            self._update_action_buttons()

        except Exception as e:
            messagebox.showerror("AUTO", str(e))

    def _stop(self):
        self.rt.running = False
        self._safe_outputs(valve_open=True)
        self._goto_state(IDLE)
        self._set_status_text("STOPPED")
        self._clear_flow_notice()
        self._last_tick_ts = None
        self._update_cycle_indicator()
        self._update_action_buttons()

    # ========================================================
    # STATE MACHINE
    # ========================================================
    def _goto_state(self, st: str):
        self.rt.state = st
        self.rt.t_state = time.time()

    def _current_sp(self) -> float:
        if not self.rt.points:
            return 0.0
        return float(self.rt.points[self.rt.step_index])

    def _current_control_sp(self) -> float:
        sp = self._current_sp()
        if self.rt.step_index == 0 and abs(sp) <= 1e-9:
            return sp
        if self._is_down_phase():
            return max(0.0, sp - 0.5)
        return sp

    def _apply_active_zone(self, sp_nominal: float, sp_ctrl: float, pv_kpa: float) -> None:
        error_now = float(sp_ctrl) - float(pv_kpa)
        self.pi_worker.set_zone_from_sp(
            zone_sp_kpa=float(sp_nominal),
            error_now=error_now,
        )

    def _is_point_within_hold_band(self, sp_nominal: float, pv_kpa: float) -> bool:
        return abs(float(sp_nominal) - float(pv_kpa)) <= float(self.cfg.deadband_kpa)

    def _is_max_point(self, sp: float) -> bool:
        return abs(sp - max(self.rt.points)) < 1e-9 if self.rt.points else False

    def _current_hold_wait_s(self, sp: float) -> float:
        if self.rt.step_index == 0 and abs(float(sp)) <= 1e-9:
            return 0.0
        if self._is_max_point(sp):
            return float(self.cfg.settle_time_max_s)
        return float(self.cfg.settle_time_s)

    def _up_sequence_len(self) -> int:
        if not self.rt.points:
            return 0
        if self.cfg.direction == "BOTH":
            return (len(self.rt.points) + 1) // 2
        return len(self.rt.points)

    def _sync_down_points_from_up_results(self) -> None:
        if self.cfg.direction != "BOTH" or not self.rt.points:
            return

        up_len = self._up_sequence_len()
        if up_len < 2:
            return

        up_results = [row for row in self.results if str(row.get("phase", "up")) != "down"]
        if len(up_results) < up_len:
            return

        down_targets = [
            max(0.0, float(row.get("p_kpa", row.get("sp_kpa", 0.0))))
            for row in reversed(up_results[:up_len - 1])
        ]
        expected_down_len = len(self.rt.points) - up_len
        if len(down_targets) != expected_down_len:
            return

        for offset, target in enumerate(down_targets):
            self.rt.points[up_len + offset] = target

    def _advance_point(self):
        prev_index = self.rt.step_index
        self.rt.step_index += 1
        self.pi_worker.reset()
        self.rt.t_state = time.time()

        if (
            self.cfg.direction == "BOTH"
            and prev_index == self._up_sequence_len() - 1
            and self.rt.step_index == self._up_sequence_len()
        ):
            self._sync_down_points_from_up_results()

        if self.rt.step_index >= len(self.rt.points):
            self.rt.running = False
            self._safe_outputs(valve_open=True)
            self._goto_state(IDLE)
            self._set_status_text("FINISHED")
            self._clear_flow_notice()
            self._update_cycle_indicator()
            self._update_action_buttons()
            export_result = self._export_results_pdf()
            if export_result:
                pdf_path, sync_result = export_result
                self._show_export_feedback(pdf_path, sync_result)

            return

        if self._should_show_down_notice(prev_index):
            self._show_flow_notice()

        self._goto_state(GOTO_SP)
        self._update_cycle_indicator()

    def _is_down_step(self) -> bool:
        if not self.rt.points or self.rt.step_index <= 0:
            return False
        prev_sp = float(self.rt.points[self.rt.step_index - 1])
        curr_sp = float(self.rt.points[self.rt.step_index])
        return curr_sp < prev_sp

    def _is_down_phase(self) -> bool:
        # En BOTH, cualquier tramo con setpoint decreciente usa la misma logica que DOWN.
        return self._is_down_step()

    def _should_show_down_notice(self, prev_index: int) -> bool:
        if self.cfg.direction not in ("DOWN", "BOTH"):
            return False
        if prev_index < 0 or prev_index >= len(self.rt.points):
            return False
        if self.rt.step_index <= 0 or self.rt.step_index >= len(self.rt.points):
            return False
        prev_sp = float(self.rt.points[prev_index])
        curr_sp = float(self.rt.points[self.rt.step_index])
        max_sp = max(float(p) for p in self.rt.points) if self.rt.points else 0.0
        return abs(prev_sp - max_sp) < 1e-9 and curr_sp < prev_sp

    def _show_flow_notice(self):
        self.var_flow_notice.set("Abra la valvula reguladora de flujo de la salida de presion")
        if self._widget_exists(self.lbl_flow_notice):
            self.lbl_flow_notice.update_idletasks()

    def _clear_flow_notice(self):
        if hasattr(self, "var_flow_notice"):
            self.var_flow_notice.set("")

    def _current_result_phase(self) -> str:
        if self.cfg.direction == "DOWN":
            return "down"
        return "down" if self._is_down_phase() else "up"

    def _plot_results_scatter(self, ax, x: np.ndarray, y: np.ndarray, size: float):
        phases = [str(r.get("phase", "up")) for r in self.results]
        has_up = False
        has_down = False

        for idx, phase in enumerate(phases):
            if phase == "down":
                has_down = True
            else:
                has_up = True

        if has_up:
            up_idx = [i for i, phase in enumerate(phases) if phase != "down"]
            ax.scatter(x[up_idx], y[up_idx], s=size, alpha=0.7, color="blue", label="Subida")
        if has_down:
            down_idx = [i for i, phase in enumerate(phases) if phase == "down"]
            ax.scatter(x[down_idx], y[down_idx], s=size, alpha=0.7, color="red", label="Bajada")

    def _get_live_signal_bounds(self) -> tuple[float, float]:
        try:
            sig_min = float(self.var_sig_min.get().strip().replace(",", "."))
        except Exception:
            sig_min = float(self.cfg.sig_min)
        try:
            sig_max = float(self.var_sig_max.get().strip().replace(",", "."))
        except Exception:
            sig_max = float(self.cfg.sig_max)
        return float(sig_min), float(sig_max)

    def _get_live_pressure_bounds(self) -> tuple[float, float]:
        try:
            p_min = self._parse_display_pressure_kpa(self.var_pmin.get(), "P min")
        except Exception:
            p_min = float(self.cfg.p_min_kpa)
        try:
            p_max = self._parse_display_pressure_kpa(self.var_pmax.get(), "P max")
        except Exception:
            p_max = float(self.cfg.p_max_kpa)
        return float(p_min), float(p_max)

    @staticmethod
    def _dut_est_pressure_kpa(x_meas: float, x_min: float, x_max: float, p_min: float, p_max: float) -> float:
        den = x_max - x_min
        if abs(den) < 1e-9:
            return float(p_min)
        return float(p_min + (x_meas - x_min) * (p_max - p_min) / den)

    def _apply_live_snapshot(self, *, p_kpa: float, dut_p_kpa: float, dut_eng: float, mode: str, err_pct: float) -> None:
        self.var_p_source.set(f"{p_kpa:,.2f} kPa".replace(",", ""))
        self.var_dut_pressure.set(f"{dut_p_kpa:,.2f} kPa".replace(",", ""))
        if mode == "A0":
            self.var_sig.set(f"{dut_eng:,.3f} V".replace(",", ""))
        else:
            self.var_sig.set(f"{dut_eng:,.3f} mA".replace(",", ""))
        self.var_err.set(f"{err_pct:+,.2f} %".replace(",", ""))

    # ========================================================
    # LOOP
    # ========================================================
    def _tick(self):
        try:
            self._refresh_usb_widgets()
            try:
                hw = getattr(self.winfo_toplevel(), "hw", None)
                reader = getattr(hw, "get_cached_temperature_c", None)
                if not callable(reader):
                    raise RuntimeError("temperature cache unavailable")
                temp_c = reader()
                if temp_c is None:
                    raise RuntimeError("temperature cache empty")
                temp_c = float(temp_c)
                self.var_temp.set(f"TEMP: {temp_c:.1f} C")
            except Exception:
                self.var_temp.set("TEMP: --.- C")

            live_read_error = None
            mode_live = (self.var_mode.get().strip().upper() or self.cfg.dut_mode or "A1")
            p = 0.0
            try:
                p_corr = self._read_pressure_corr_kpa()
                p = max(0.0, p_corr - self.rt.p_zero_kpa)
                self.rt.last_p = p
                dut_eng = float(self._dut_vadc_to_eng(self._read_dut_vadc(), mode_live))
                sig_min_live, sig_max_live = self._get_live_signal_bounds()
                p_min_live, p_max_live = self._get_live_pressure_bounds()
                dut_p_kpa = self._dut_est_pressure_kpa(
                    x_meas=dut_eng,
                    x_min=sig_min_live,
                    x_max=sig_max_live,
                    p_min=p_min_live,
                    p_max=p_max_live,
                )
                err_pct = self._error_percent_fluke_style(p, dut_eng)
                self._apply_live_snapshot(
                    p_kpa=float(p),
                    dut_p_kpa=float(dut_p_kpa),
                    dut_eng=float(dut_eng),
                    mode=mode_live,
                    err_pct=float(err_pct),
                )
            except Exception as e:
                live_read_error = e
                if not self.rt.running:
                    self.var_p_source.set("0.00 kPa")
                    self.var_dut_pressure.set("0.00 kPa")
                    self.var_sig.set("0.000 V" if mode_live == "A0" else "0.000 mA")
                    self.var_err.set("+0.00 %")

            if not self.rt.running:
                self._update_cycle_indicator()
                self._update_action_buttons()
                return

            if live_read_error is not None:
                raise live_read_error

            now = time.time()
            if self._last_tick_ts is None:
                dt_pi = None
            else:
                dt_pi = now - self._last_tick_ts
                dt_pi = max(0.02, min(dt_pi, 0.20))
            self._last_tick_ts = now

            sp_nominal = self._current_sp()
            sp = sp_nominal
            sp_ctrl = self._current_control_sp()
            if p >= float(self.cfg.p_max_seguridad_kpa):
                raise RuntimeError(f"OVERPRESSURE: P={p:.2f} kPa")

            st = self.rt.state
            t = self.rt.t_state or now
            dt_st = now - t

            if st == ZERO_VENT:
                self.set_pump(1.0)
                self.set_relay(False)
                self.set_valve(True)

                if abs(p - 0.0) <= float(self.cfg.deadband_kpa):
                    self._goto_state(ZERO_HOLD)

            elif st == ZERO_HOLD:
                self.set_pump(1.0)
                self.set_relay(False)
                self.set_valve(False)

                if dt_st >= float(self.cfg.settle_time_s):
                    self._goto_state(GOTO_SP)

            elif st == GOTO_SP:
                is_down = self._is_down_phase()

                if not is_down:
                    self.set_valve(True)
                    self.set_relay(True)
                    self._apply_active_zone(sp_nominal=sp_nominal, sp_ctrl=sp_ctrl, pv_kpa=p)

                    u = self.pi_worker.step_now(sp_kpa=sp_ctrl, p_kpa=p, dt=dt_pi)
                    self.rt.last_u = float(u)
                    self.set_pump(u)

                    if self._is_point_within_hold_band(sp_nominal=sp_nominal, pv_kpa=p):
                        self._goto_state(IN_BAND_WAIT_UP)

                else:
                    self.set_pump(1.0)
                    self.set_relay(False)
                    self.pi_worker.freeze()

                    self.set_valve(True)
                    self.rt.last_u = 1.0

                    if self._is_point_within_hold_band(sp_nominal=sp_nominal, pv_kpa=p):
                        self._goto_state(IN_BAND_WAIT_DOWN)

            elif st == IN_BAND_WAIT_UP:
                self.set_valve(True)
                self.set_relay(True)
                self._apply_active_zone(sp_nominal=sp_nominal, sp_ctrl=sp_ctrl, pv_kpa=p)

                u = self.pi_worker.step_now(sp_kpa=sp_ctrl, p_kpa=p, dt=dt_pi)
                self.rt.last_u = float(u)
                self.set_pump(u)

                if not self._is_point_within_hold_band(sp_nominal=sp_nominal, pv_kpa=p):
                    self._goto_state(GOTO_SP)
                else:
                    if dt_st >= float(self.cfg.inband_up_s):
                        self.set_pump(1.0)
                        self.set_relay(False)
                        self.pi_worker.freeze()
                        self.set_valve(False)
                        self._goto_state(HOLD_MEASURE)

            elif st == IN_BAND_WAIT_DOWN:
                self.set_pump(1.0)
                self.set_relay(False)
                self.pi_worker.freeze()
                self.set_valve(True)

                if not self._is_point_within_hold_band(sp_nominal=sp_nominal, pv_kpa=p):
                    self._goto_state(GOTO_SP)
                else:
                    if dt_st >= float(self.cfg.inband_down_s):
                        if float(self.cfg.valve_close_delay_s) > 0.0:
                            self._goto_state(DOWN_CLOSE_DELAY)
                        else:
                            self.set_valve(False)
                            self._goto_state(HOLD_MEASURE)

            elif st == DOWN_CLOSE_DELAY:
                self.set_pump(1.0)
                self.set_relay(False)
                self.pi_worker.freeze()
                self.set_valve(True)

                if dt_st >= float(self.cfg.valve_close_delay_s):
                    self.set_valve(False)
                    self._goto_state(HOLD_MEASURE)

            elif st == HOLD_MEASURE:
                self.set_valve(False)
                self.set_pump(1.0)
                self.set_relay(False)

                wait = self._current_hold_wait_s(sp_nominal)
                if dt_st >= wait:
                    # ✅ AQUÍ SOLO AÑADIMOS MEDICIÓN Y REGISTRO (no cambia control)
                    try:
                        self._record_point_result(sp_kpa=float(sp_nominal))
                    except Exception as e:
                        # si falla medición, aborta con error claro
                        raise RuntimeError(f"Fallo medición punto (SP={sp:.2f}): {e}")

                    self.pi_worker.unfreeze()
                    self._advance_point()

            else:
                self._safe_outputs(valve_open=True)
                self._goto_state(IDLE)

            self._set_status_text(f"RUNNING | {self.rt.state}")
            self._update_cycle_indicator()

        except Exception as e:
            self._safe_outputs(valve_open=True)
            self.rt.running = False
            self._goto_state(IDLE)
            self._set_status_text("ERROR")
            self._update_action_buttons()
            self.request_event("EV_AUTO_FAIL", {"error": str(e)})

        finally:
            self.after(self.update_period_ms, self._tick)

    # ========================================================
    # RESULTADOS (solo añadido)
    # ========================================================
    def _record_point_result(self, sp_kpa: float):
        """
        Toma N_SAMPLES_MEASURE muestras (ref + dut) y guarda un registro.
        No toca control, solo lee y registra.
        """
        n = int(getattr(config, "N_SAMPLES_MEASURE", 50))
        use_med = bool(getattr(config, "MEASURE_MEDIAN_ENABLE", True))
        med_n = int(getattr(config, "MEASURE_MEDIAN_N", 3))

        med_ref = MedianPtByPt(med_n) if use_med else None
        med_dut = MedianPtByPt(med_n) if use_med else None

        p_list: List[float] = []
        dut_list: List[float] = []
        vadc_ref_list: List[float] = []
        vadc_dut_list: List[float] = []

        mode = (self.cfg.dut_mode or "A1").upper()
        ch_dut = config.ADS_CH_DUT_V if mode == "A0" else config.ADS_CH_DUT_mA

        for _ in range(max(1, n)):
            vadc_ref_raw = float(self.read_vadc(config.ADS_CH_REF))
            vadc_ref = med_ref.update(vadc_ref_raw) if med_ref else vadc_ref_raw
            p_corr = float(self._mpx_vadc_to_kpa(vadc_ref))
            p = max(0.0, p_corr - float(self.rt.p_zero_kpa))

            vadc_dut_raw = float(self.read_vadc(ch_dut))
            vadc_dut = med_dut.update(vadc_dut_raw) if med_dut else vadc_dut_raw
            dut_eng = float(self._dut_vadc_to_eng(vadc_dut, mode))

            vadc_ref_list.append(vadc_ref)
            vadc_dut_list.append(vadc_dut)
            p_list.append(p)
            dut_list.append(dut_eng)

        p_mean = float(np.mean(p_list)) if p_list else 0.0
        dut_mean = float(np.mean(dut_list)) if dut_list else 0.0
        p_std = float(np.std(p_list, ddof=1)) if len(p_list) > 1 else 0.0
        dut_std = float(np.std(dut_list, ddof=1)) if len(dut_list) > 1 else 0.0

        span_pct = self._span_percent(dut_mean)
        err_pct = self._error_percent_fluke_style(p_mean, dut_mean)

        row = {
            "i": int(self.rt.step_index),
            "sp_kpa": float(sp_kpa),
            "p_kpa": float(p_mean),
            "p_std": float(p_std),
            "phase": self._current_result_phase(),
            "dut_mode": mode,
            "dut_eng": float(dut_mean),
            "dut_std": float(dut_std),
            "span_pct": float(span_pct),
            "err_pct": float(err_pct),
            "u_last": float(self.rt.last_u),
        }
        self.results.append(row)
        self._refresh_registered_plot()
        self._update_cycle_indicator()

    def _span_percent(self, dut_eng: float) -> float:
        sig_min = float(self.cfg.sig_min)
        sig_max = float(self.cfg.sig_max)
        span = sig_max - sig_min
        if abs(span) < 1e-12:
            return 0.0
        return 100.0 * (float(dut_eng) - sig_min) / span

    def _error_percent_fluke_style(self, p_kpa: float, dut_eng: float) -> float:
        pmin = float(self.cfg.p_min_kpa)
        pmax = float(self.cfg.p_max_kpa)
        sig_min = float(self.cfg.sig_min)
        sig_max = float(self.cfg.sig_max)

        p_span = pmax - pmin
        sig_span = sig_max - sig_min
        if abs(p_span) < 1e-12 or abs(sig_span) < 1e-12:
            return 0.0

        p_pct = 100.0 * (float(p_kpa) - pmin) / p_span
        sig_pct = 100.0 * (float(dut_eng) - sig_min) / sig_span
        return sig_pct - p_pct

    def _results_output_dir(self) -> str:
        if self.export_manager is not None:
            return self.export_manager.ensure_results_dir()

        base_dir = os.getcwd()
        results_dir = os.path.join(base_dir, str(config.RESULTS_DIR))
        os.makedirs(results_dir, exist_ok=True)
        return results_dir

    def _compute_results_fit(self) -> tuple[np.ndarray, np.ndarray, float, float, float]:
        x = np.array([r["p_kpa"] for r in self.results], dtype=float)
        y = np.array([r["dut_eng"] for r in self.results], dtype=float)
        m, b = np.polyfit(x, y, 1)
        y_hat = m * x + b

        ss_res = float(np.sum((y - y_hat) ** 2))
        ss_tot = float(np.sum((y - float(np.mean(y))) ** 2))
        r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-12 else 0.0
        return x, y, float(m), float(b), r2

    def _build_results_pdf_figure(self) -> Figure:
        x, y, m, b, r2 = self._compute_results_fit()

        from matplotlib.gridspec import GridSpec

        fig_pdf = Figure(figsize=(10, 12), dpi=100)
        gs = GridSpec(3, 1, figure=fig_pdf, height_ratios=[1, 1.5, 2], hspace=0.3)

        ax_title = fig_pdf.add_subplot(gs[0])
        ax_title.axis("off")
        titulo = f"Calibracion - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        ax_title.text(0.5, 0.7, titulo, ha="center", va="center", fontsize=14, fontweight="bold")
        ax_title.text(0.5, 0.3, f"DUT Mode: {self.results[0]['dut_mode']}", ha="center", va="center", fontsize=10)

        ax_table = fig_pdf.add_subplot(gs[1])
        ax_table.axis("tight")
        ax_table.axis("off")

        table_data = [["#", "SP (kPa)", "P med (kPa)", "sP", f"DUT ({self.results[0]['dut_mode']})", "sDUT", "%SPAN", "%ERROR", "u"]]
        for r in self.results:
            table_data.append([
                str(r["i"]),
                f"{r['sp_kpa']:.2f}",
                f"{r['p_kpa']:.2f}",
                f"{r['p_std']:.3f}",
                f"{r['dut_eng']:.3f}",
                f"{r['dut_std']:.3f}",
                f"{r['span_pct']:.2f}",
                f"{r['err_pct']:+.2f}",
                f"{r['u_last']:.3f}",
            ])

        table = ax_table.table(
            cellText=table_data,
            cellLoc="center",
            loc="center",
            colWidths=[0.08, 0.12, 0.12, 0.08, 0.12, 0.08, 0.1, 0.1, 0.1],
        )
        table.auto_set_font_size(False)
        table.set_fontsize(8)
        table.scale(1, 1.5)

        for i in range(len(table_data[0])):
            table[(0, i)].set_facecolor("#4CAF50")
            table[(0, i)].set_text_props(weight="bold", color="white")

        ax_plot = fig_pdf.add_subplot(gs[2])
        self._plot_results_scatter(ax_plot, x, y, 50)
        ax_plot.plot(x, (m * x) + b, "k-", linewidth=2, label="Ajuste lineal")
        ax_plot.set_xlabel("Presion medida (kPa)", fontsize=10)
        ax_plot.set_ylabel(f"DUT ({'mA' if self.results[0]['dut_mode'] == 'A1' else 'V'})", fontsize=10)
        ax_plot.grid(True, alpha=0.3)
        ax_plot.legend(fontsize=9)
        ax_plot.set_title(f"y = {m:.6f}x + {b:.6f}    R2 = {r2:.6f}", fontsize=10, fontweight="bold")
        return fig_pdf

    def _save_results_pdf_local(self) -> Optional[str]:
        if not self.results:
            return None

        try:
            results_dir = self._results_output_dir()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"calibracion_{timestamp}.pdf"
            filepath = os.path.join(results_dir, filename)
            fig_pdf = self._build_results_pdf_figure()
            try:
                fig_pdf.savefig(filepath, format="pdf", dpi=300, bbox_inches="tight")
            finally:
                fig_pdf.clear()
            return filepath
        except Exception as e:
            messagebox.showerror("Error", f"Fallo al exportar: {e}")
            return None

    def _export_results_pdf(self) -> Optional[tuple[str, Optional[ExportSyncResult]]]:
        filepath = self._save_results_pdf_local()
        if not filepath:
            return None

        sync_result = None
        if self.export_manager is not None:
            try:
                sync_result = self.export_manager.register_export(filepath)
            except Exception as e:
                messagebox.showwarning("USB", f"PDF guardado localmente.\nLa cola USB no pudo actualizarse:\n{e}")
        return filepath, sync_result

    def _show_export_feedback(self, local_path: str, sync_result: Optional[ExportSyncResult]) -> None:
        if sync_result and sync_result.preferred_usb_path:
            messagebox.showinfo(
                "Exportar",
                f"PDF guardado en:\n{local_path}\n\nCopia USB:\n{sync_result.preferred_usb_path}",
            )
            return

        if sync_result and not sync_result.usb_detected:
            messagebox.showinfo(
                "Exportar",
                f"PDF guardado localmente en:\n{local_path}\n\nUSB no detectada. Se copiara automaticamente al conectarla.",
            )
            return

        if sync_result and sync_result.last_error:
            messagebox.showwarning(
                "Exportar",
                f"PDF guardado localmente en:\n{local_path}\n\nLa copia a la USB quedo pendiente.\n{sync_result.last_error}",
            )
            return

        messagebox.showinfo("Exportar", f"PDF guardado en:\n{local_path}")

    def _retry_pending_usb_exports(self) -> None:
        if self.export_manager is None:
            messagebox.showinfo("USB", "La exportacion USB no esta disponible en esta sesion.")
            return

        sync_result = self.export_manager.sync_pending_exports()
        if sync_result.copied_count > 0:
            messagebox.showinfo("USB", f"Se copiaron {sync_result.copied_count} archivo(s) a la USB.")
            return
        if sync_result.pending_count > 0 and not sync_result.usb_detected:
            messagebox.showinfo("USB", "No hay una USB detectada. Los archivos siguen pendientes.")
            return
        if sync_result.pending_count > 0 and sync_result.last_error:
            messagebox.showwarning("USB", f"La exportacion sigue pendiente.\n{sync_result.last_error}")
            return
        messagebox.showinfo("USB", "No hay archivos pendientes de exportacion.")

    def _show_results_window(self):
        """
        Ventana final con:
        - Tabla de resultados (compacta)
        - Gráfica lineal con ecuación y R²
        - Boton para reintentar USB y cerrar
        Optimizada para pantalla 7"
        """
        if not self.results:
            return

        # si ya existe, cerrarla y reconstruir para exportar la nueva serie
        if self._results_win is not None:
            try:
                if self._results_win.winfo_exists():
                    self._results_win.destroy()
            except tk.TclError:
                pass
            finally:
                self._results_win = None

        win = tk.Toplevel(self)
        self._results_win = win
        win.title("Resultados de calibración (Auto)")
        # Adaptado a pantalla 7": 800x480 o menos
        win.geometry("800x470")

        # Dar foco a la ventana
        win.lift()
        win.focus_force()

        # ---- Layout principal con scroll
        main_canvas = tk.Canvas(win, bg="white")
        main_canvas.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(win, orient="vertical", command=main_canvas.yview)
        scrollbar.pack(side="right", fill="y")

        main_canvas.configure(yscrollcommand=scrollbar.set)

        # Frame dentro del canvas
        top = ttk.Frame(main_canvas, padding=3)
        main_canvas.create_window((0, 0), window=top, anchor="nw")

        # ---- Tabla (altura fija, compacta)
        frm_tbl = ttk.LabelFrame(top, text="Tabla de resultados", padding=1)
        frm_tbl.pack(fill="x", expand=False, pady=(0, 1))

        cols = ("i", "sp_kpa", "p_kpa", "p_std", "dut", "dut_std", "span_pct", "err_pct", "u_last")
        # Altura reducida y fuente más pequeña
        tv = ttk.Treeview(frm_tbl, columns=cols, show="headings", height=4)
        tv.pack(side="left", fill="both", expand=True)

        vsb = ttk.Scrollbar(frm_tbl, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")

        tv.heading("i", text="#")
        tv.heading("sp_kpa", text="SP (kPa)")
        tv.heading("p_kpa", text="P med (kPa)")
        tv.heading("p_std", text="σP")
        tv.heading("dut", text=f"DUT ({self.results[0]['dut_mode']})")
        tv.heading("dut_std", text="σDUT")
        tv.heading("span_pct", text="%SPAN")
        tv.heading("err_pct", text="%ERROR")
        tv.heading("u_last", text="u")

        # Columnas más estrechas para caber en 7"
        for c in cols:
            tv.column(c, width=75, anchor="center")
        tv.column("i", width=30)

        # Fuente pequeña para la tabla
        style = ttk.Style()
        style.configure("Treeview", rowheight=18, font=("Arial", 8))
        style.configure("Treeview.Heading", font=("Arial", 8, "bold"))

        for r in self.results:
            dut_txt = f"{r['dut_eng']:.3f}" if r["dut_mode"] == "A0" else f"{r['dut_eng']:.3f}"
            tv.insert(
                "", "end",
                values=(
                    r["i"],
                    f"{r['sp_kpa']:.2f}",
                    f"{r['p_kpa']:.2f}",
                    f"{r['p_std']:.3f}",
                    dut_txt,
                    f"{r['dut_std']:.3f}",
                    f"{r['span_pct']:.2f}",
                    f"{r['err_pct']:+.2f}",
                    f"{r['u_last']:.3f}",
                )
            )

        # ---- Gráfica (reducida)
        frm_plot = ttk.LabelFrame(top, text="Gráfica lineal + ecuación", padding=1)
        frm_plot.pack(fill="both", expand=False, pady=(0, 1))

        # Datos
        x = np.array([r["p_kpa"] for r in self.results], dtype=float)
        y = np.array([r["dut_eng"] for r in self.results], dtype=float)

        # Ajuste lineal y = m x + b
        m, b = np.polyfit(x, y, 1)
        y_hat = m * x + b

        # R²
        ss_res = float(np.sum((y - y_hat) ** 2))
        ss_tot = float(np.sum((y - float(np.mean(y))) ** 2))
        r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-12 else 0.0

        # Figura más pequeña (4" x 1.8")
        fig = Figure(figsize=(4, 1.8), dpi=100)
        ax = fig.add_subplot(111)
        self._plot_results_scatter(ax, x, y, 40)
        ax.plot(x, y_hat, "k-", linewidth=1.5)
        ax.set_xlabel("P (kPa)", fontsize=8)
        ax.set_ylabel("DUT (mA/V)", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.3)

        eq = f"y={m:.4f}x+{b:.4f} | R²={r2:.4f}"
        ax.set_title(eq, fontsize=8, fontweight="bold")
        fig.tight_layout(pad=0.5)

        canvas = FigureCanvasTkAgg(fig, master=frm_plot)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)

        # ---- Botones de acción (compactos)
        frm_btns = ttk.Frame(top)
        frm_btns.pack(fill="x", pady=1)

        # Función para exportar PDF
        def do_export_pdf():
            try:
                # Obtener directorio de ejecución
                base_dir = os.getcwd()
                results_dir = os.path.join(base_dir, "resultados_calibracion")

                # Crear directorio si no existe
                if not os.path.exists(results_dir):
                    os.makedirs(results_dir)

                # Generar nombre con timestamp
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"calibracion_{timestamp}.pdf"
                filepath = os.path.join(results_dir, filename)

                # Crear figura con tabla y gráfica
                from matplotlib.gridspec import GridSpec
                fig_pdf = Figure(figsize=(10, 12), dpi=100)
                gs = GridSpec(3, 1, figure=fig_pdf, height_ratios=[1, 1.5, 2], hspace=0.3)

                # ---- Subtítulo con información
                ax_title = fig_pdf.add_subplot(gs[0])
                ax_title.axis('off')
                titulo = f"Calibración - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                ax_title.text(0.5, 0.7, titulo, ha='center', va='center', fontsize=14, fontweight='bold')
                ax_title.text(0.5, 0.3, f"DUT Mode: {self.results[0]['dut_mode']}", ha='center', va='center', fontsize=10)

                # ---- Tabla de resultados
                ax_table = fig_pdf.add_subplot(gs[1])
                ax_table.axis('tight')
                ax_table.axis('off')

                # Preparar datos de la tabla
                table_data = [['#', 'SP (kPa)', 'P med (kPa)', 'σP', f"DUT ({self.results[0]['dut_mode']})", 'σDUT', '%SPAN', '%ERROR', 'u']]
                for r in self.results:
                    dut_txt = f"{r['dut_eng']:.3f}"
                    table_data.append([
                        str(r["i"]),
                        f"{r['sp_kpa']:.2f}",
                        f"{r['p_kpa']:.2f}",
                        f"{r['p_std']:.3f}",
                        dut_txt,
                        f"{r['dut_std']:.3f}",
                        f"{r['span_pct']:.2f}",
                        f"{r['err_pct']:+.2f}",
                        f"{r['u_last']:.3f}",
                    ])

                # Crear tabla
                table = ax_table.table(cellText=table_data, cellLoc='center', loc='center',
                                      colWidths=[0.08, 0.12, 0.12, 0.08, 0.12, 0.08, 0.1, 0.1, 0.1])
                table.auto_set_font_size(False)
                table.set_fontsize(8)
                table.scale(1, 1.5)

                # Estilo de encabezado
                for i in range(len(table_data[0])):
                    table[(0, i)].set_facecolor('#4CAF50')
                    table[(0, i)].set_text_props(weight='bold', color='white')

                # ---- Gráfica
                ax_plot = fig_pdf.add_subplot(gs[2])
                self._plot_results_scatter(ax_plot, x, y, 50)
                ax_plot.plot(x, y_hat, "k-", linewidth=2, label="Ajuste lineal")
                ax_plot.set_xlabel("Presión medida (kPa)", fontsize=10)
                ax_plot.set_ylabel(f"DUT ({'mA' if self.results[0]['dut_mode'] == 'A1' else 'V'})", fontsize=10)
                ax_plot.grid(True, alpha=0.3)
                ax_plot.legend(fontsize=9)

                eq = f"y = {m:.6f}x + {b:.6f}    R² = {r2:.6f}"
                ax_plot.set_title(eq, fontsize=10, fontweight="bold")

                # Guardar PDF
                fig_pdf.savefig(filepath, format="pdf", dpi=300, bbox_inches="tight")
                return filepath
            except Exception as e:
                messagebox.showerror("Error", f"Fallo al exportar: {e}")
                return None

        # Exportar PDF automáticamente
        pdf_path = do_export_pdf()
        if pdf_path:
            messagebox.showinfo("Exportar", f"PDF guardado en:\n{pdf_path}")

        # Solo botón Cerrar
        self._results_close_btn = ttk.Button(frm_btns, text="Cerrar", command=win.destroy)
        self._results_close_btn.pack(side="left", padx=2)

        # Actualizar scroll region
        top.update_idletasks()
        main_canvas.configure(scrollregion=main_canvas.bbox("all"))

        # Bind de scroll con mouse wheel
        def _on_mousewheel(event):
            main_canvas.yview_scroll(int(-1*(event.delta/120)), "units")

        main_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _on_close():
            try:
                main_canvas.unbind_all("<MouseWheel>")
                win.destroy()
            finally:
                self._results_win = None

        self._results_close_btn.configure(command=_on_close)
        win.protocol("WM_DELETE_WINDOW", _on_close)

    # ========================================================
    # DUT UTILS (lectura en vivo con calibración lineal config.py)
    # ========================================================
    def _read_dut_vadc(self) -> float:
        mode = (self.cfg.dut_mode or "A1").upper()
        ch = config.ADS_CH_DUT_V if mode == "A0" else config.ADS_CH_DUT_mA
        return float(self.read_vadc_live(ch))

    def _dut_vadc_to_eng(self, vadc: float, mode: str) -> float:
        mode = (mode or "A1").upper()
        if mode == "A0":
            if bool(getattr(config, "USE_A0_CAL", True)):
                return float(getattr(config, "A0_CAL_M", 1.0)) * float(vadc) + float(getattr(config, "A0_CAL_B", 0.0))
            return float(vadc)

        if bool(getattr(config, "USE_A1_CAL", True)):
            return float(getattr(config, "A1_CAL_M", 1.0)) * float(vadc) + float(getattr(config, "A1_CAL_B", 0.0))
        return float(vadc)

    def _dut_text_live(self) -> str:
        mode = (self.cfg.dut_mode or "A1").upper()
        vadc = self._read_dut_vadc()

        if mode == "A0":
            vin = self._dut_vadc_to_eng(vadc, mode)
            return f"DUT(A0)= {vin:5.3f} V | Vadc={vadc:5.3f} V"

        ima = self._dut_vadc_to_eng(vadc, mode)
        return f"DUT(A1)= {ima:6.2f} mA | Vadc={vadc:5.3f} V"

    def _effective_u_bounds(self, u_min: float, u_max: float) -> tuple[float, float]:
        u_min_eff = max(0.0, min(float(u_min), 1.0))
        u_max_eff = max(0.0, min(float(u_max), 1.0))
        if bool(getattr(config, "BOMBA_ACTIVE_LOW", False)):
            pwm_hw_min = max(0.0, min(float(getattr(config, "PWM_HW_MIN_HOLD", 0.20)), 1.0))
            u_max_eff = min(u_max_eff, 1.0 - pwm_hw_min)
        if u_max_eff < u_min_eff:
            u_max_eff = u_min_eff
        return u_min_eff, u_max_eff

    # ========================================================
    # PRESSURE UTILS
    # ========================================================
    def _read_pressure_corr_kpa(self) -> float:
        vadc = float(self.read_vadc_live(config.ADS_CH_REF))
        return float(self._mpx_vadc_to_kpa(vadc))

    def _mpx_vadc_to_kpa(self, vadc: float) -> float:
        p = config.MPX_M * vadc + config.MPX_B
        if p < 0:
            p = 0.0
        if config.USE_2PT:
            p = config.GAIN_2PT * p + config.OFFSET_2PT
        return float(p)

    # ========================================================
    # SAFE OUTPUTS
    # ========================================================
    def _safe_outputs(self, valve_open: bool = True):
        try:
            self.set_pump(1.0)
        except Exception:
            pass
        try:
            self.set_relay(False)
        except Exception:
            pass
        try:
            self.set_valve(bool(valve_open))
        except Exception:
            pass
        try:
            self.pi_worker.reset()
            self.pi_worker.freeze()
        except Exception:
            pass

    def destroy(self):
        try:
            self._close_settings_window()
        except Exception:
            pass
        try:
            if self._widget_exists(self._control_win):
                self._control_win.destroy()
        except Exception:
            pass
        try:
            self.pi_worker.stop()
        except Exception:
            pass
        super().destroy()

