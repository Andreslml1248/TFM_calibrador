# mode_manual.py
# -*- coding: utf-8 -*-

import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox, font as tkFont
from dataclasses import dataclass
from collections import deque
from queue import SimpleQueue, Empty
from typing import Callable, Optional, Dict, Any

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from config import hardware as config
from core.control import PIController, PIConfig, PIWorker
from core.calibration import two_point_cal, save_calibration


# =========================
# Utilidades de conversiÃ³n
# =========================
def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def mpx_vadc_to_kpa(vadc: float) -> float:
    """Convierte VADC (ADS) -> presiÃ³n kPa usando polinomio + 2PT si aplica."""
    p_raw = config.MPX_M * vadc + config.MPX_B
    if p_raw < 0:
        p_raw = 0.0
    if config.USE_2PT:
        p_corr = config.GAIN_2PT * p_raw + config.OFFSET_2PT
    else:
        p_corr = p_raw
    if p_corr < 0:
        p_corr = 0.0
    return float(p_corr)


def dut_vadc_to_eng(vadc: float, dut_mode: str) -> float:
    """
    Convierte VADC (ADS) a ingenierÃ­a:
      - A0 -> Vin (V): Vin = gain*VADC + offset
      - A1 -> ImA (mA): ImA = gain*VADC + offset
    """
    if dut_mode == "A0":
        if config.USE_A0_CAL:
            return config.A0_CAL_M * vadc + config.A0_CAL_B
        return vadc
    else:
        if config.USE_A1_CAL:
            return config.A1_CAL_M * vadc + config.A1_CAL_B
        return vadc


# =========================
# Contexto manual (simple)
# =========================
@dataclass
class ManualConfig:
    sp_kpa: float = 60.0
    sp_unit: str = "kPa"
    dut_mode: str = "A1"  # "A0" o "A1"
    p_min_kpa: float = 0.0
    p_max_kpa: float = 200.0
    sig_min: float = 4.0
    sig_max: float = 20.0
    p_max_seguridad_kpa: float = config.P_MAX_SEGURIDAD_KPA


@dataclass
class ManualRuntime:
    running: bool = False
    target_reached: bool = False
    p_zero_kpa: float = 0.0
    last_update_ts: float = 0.0
    in_band_since_ts: Optional[float] = None


# =========================
# Frame GUI del modo manual
# =========================
class ManualView(ttk.Frame):
    _A0_SIG_MIN_DEFAULT = 0.0
    _A0_SIG_MAX_DEFAULT = 10.0
    _A1_SIG_MIN_DEFAULT = 4.0
    _A1_SIG_MAX_DEFAULT = 20.0
    _MANUAL_UP_OFFSET_KPA = 3.0
    _PRESSURE_MIN_KPA = 0.0
    _PRESSURE_MAX_KPA = 200.0
    _PRESSURE_SAFETY_MAX_KPA = max(500.0, float(getattr(config, "P_MAX_SEGURIDAD_KPA", 0.0)))
    _UNIT_TO_KPA = {
        "kPa": 1.0,
        "bar": 100.0,
        "mbar": 0.1,
        "MPa": 1000.0,
        "psi": 6.894757,
        "kgf/cmÂ²": 98.0665,
        "mmH2O": 0.00980665,
        "cmH2O": 0.0980665,
        "inH2O": 0.2490889,
        "mmHg": 0.133322,
        "inHg": 3.386389,
    }
    _SP_UNITS = (
        "psi",
        "bar",
        "mbar",
        "kPa",
        "MPa",
        "kgf/cmÂ²",
        "mmH2O",
        "cmH2O",
        "inH2O",
        "mmHg",
        "inHg",
    )
    _LIVE_PLOT_WINDOW_S = 60.0
    _LIVE_PLOT_MAX_POINTS = 600
    _LIVE_PLOT_MIN_REDRAW_S = 0.25

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
        update_period_ms: int = 100,
    ):
        super().__init__(master)
        self.read_vadc = read_vadc
        self.read_vadc_live = read_vadc_live
        self.set_pump = set_pump
        self.set_relay = set_relay
        self.set_valve = set_valve
        self.request_event = request_event
        self.update_period_ms = update_period_ms
        self._screen_width = max(1, int(self.winfo_screenwidth()))
        self._screen_height = max(1, int(self.winfo_screenheight()))
        self._ui_scale = self._compute_ui_scale()
        self._tx_refresh_after_id: Optional[str] = None
        self._tx_refresh_period_ms = max(
            20,
            int(round(float(getattr(config, "TELEMETRY_FORCE_REFRESH_S", 0.05)) * 1000.0)),
        )
        self._manual_hold_band_kpa = max(
            0.0,
            float(getattr(config, "MANUAL_STATIC_HOLD_BAND_KPA", 1.0)),
        )
        self._manual_hold_delay_s = max(
            0.0,
            float(getattr(config, "MANUAL_STATIC_HOLD_DELAY_S", 1.0)),
        )
        pi_u_min, pi_u_max = self._effective_u_bounds(config.PI_CFG.u_min, config.PI_CFG.u_max)

        # PI Ãºnico (sirve manual y auto)
        self.pi = PIController(PIConfig(
            dt=config.PI_CFG.dt,
            u_min=pi_u_min,
            u_max=pi_u_max,
            deadband_kpa=config.PI_CFG.deadband_kpa,
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

        self.cfg = ManualConfig()
        self.rt = ManualRuntime()
        self._runtime_lock = threading.Lock()
        self._runtime_stop_evt = threading.Event()
        self._runtime_worker: Optional[threading.Thread] = None
        self._runtime_event_queue = SimpleQueue()
        self._runtime_overpressure_latched = False
        self._runtime_fault_latched = False
        self._runtime_snapshot: Dict[str, Any] = {
            "p_kpa": 0.0,
            "dut_p_kpa": 0.0,
            "dut_eng": 0.0,
            "span_pct": 0.0,
            "err_pct": 0.0,
            "dut_mode": self.cfg.dut_mode,
            "u_text": "u=0.000",
        }
        self._live_plot_lock = threading.Lock()
        self._live_plot_t0: Optional[float] = None
        self._live_plot_last_draw_ts: float = 0.0
        self._live_plot_t = deque(maxlen=self._LIVE_PLOT_MAX_POINTS)
        self._live_plot_p_pat = deque(maxlen=self._LIVE_PLOT_MAX_POINTS)
        self._live_plot_p_dut = deque(maxlen=self._LIVE_PLOT_MAX_POINTS)
        self._live_plot_queue = SimpleQueue()
        self._live_plot_after_id: Optional[str] = None
        self._fig_live: Optional[Figure] = None
        self._ax_live = None
        self._canvas_live: Optional[FigureCanvasTkAgg] = None
        self._line_live_pat = None
        self._line_live_dut = None
        self._settings_window: Optional[tk.Toplevel] = None
        self._calibration_window: Optional[tk.Toplevel] = None

        # Variables Tk
        self.var_sp = tk.StringVar(value=f"{self.cfg.sp_kpa:.2f}")
        self.var_sp_unit = tk.StringVar(value=self.cfg.sp_unit)
        self.var_sp_label = tk.StringVar(value=f"SP ({self.cfg.sp_unit}):")
        self.var_pmin = tk.StringVar(value=f"{self.cfg.p_min_kpa:.2f}")
        self.var_pmax = tk.StringVar(value=f"{self.cfg.p_max_kpa:.2f}")
        self.var_sigmin = tk.StringVar(value=f"{self.cfg.sig_min:.3f}")
        self.var_sigmax = tk.StringVar(value=f"{self.cfg.sig_max:.3f}")
        self.var_pmaxseg = tk.StringVar(value=f"{self.cfg.p_max_seguridad_kpa:.1f}")
        self.var_mode = tk.StringVar(value=self.cfg.dut_mode)
        self.var_settings_pressure_unit = tk.StringVar(value=self.cfg.sp_unit)
        self.var_settings_signal_unit = tk.StringVar(
            value="V" if self.cfg.dut_mode == "A0" else "mA"
        )

        # Lecturas en vivo
        self.var_p_source = tk.StringVar(value="0.00 kPa")
        self.var_dut_pressure = tk.StringVar(value="0.00 kPa")
        self.var_sig = tk.StringVar(value="0.000 mA")
        self.var_span = tk.StringVar(value="0.0 %")
        self.var_err = tk.StringVar(value="0.0 %")
        self.var_pwm = tk.StringVar(value="u=0.000")
        self.var_temp = tk.StringVar(value="TEMP: --.- C")
        self.var_tol = tk.StringVar(value=f"+/- {float(getattr(config, 'TOL_KPA_DEFAULT', 1.0)):.2f} kPa")

        self.btn_settings = None
        self.btn_pmin = None
        self.btn_pmax = None
        self.btn_sigmin = None
        self.btn_sigmax = None
        self.btn_pmaxseg = None
        self.btn_sp_unit_popup = None
        self.lbl_sigmin = None
        self.lbl_sigmax = None
        self._tx_buttons: Dict[Any, tk.Button] = {}
        self._tx_badge = None
        self._plot_host = None
        self._settings_snapshot: Optional[Dict[str, Any]] = None

        self._build_ui_compact()
        self._build_live_plot()
        self._apply_state_config()

        self._safe_outputs()
        self._schedule_live_plot_poll()
        self._start_runtime_worker()
        self.after(self.update_period_ms, self._tick)
        self._schedule_tx_refresh()

    # -------------------------
    # UI compacta (SIN scroll)
    # -------------------------
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

    def _build_ui_compact(self):
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
        tk.Label(title_wrap, text=" MANUAL", font=sf(20, "bold"), bg="#171b24", fg="#ffffff").pack(side="left")

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
        body.grid_columnconfigure(0, weight=5)
        body.grid_columnconfigure(1, weight=3)

        live_panel = tk.Frame(body, bg="#080b11", bd=2, relief="groove")
        live_panel.grid(row=0, column=0, sticky="nsew", padx=(0, sp(6, 3)))
        live_panel.grid_columnconfigure(0, weight=1)
        self.frm_live = live_panel

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
            anchor="center",
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
            anchor="center",
        ).grid(row=4, column=0, sticky="ew", padx=sp(10, 4), pady=(0, sp(2, 1)))

        tk.Label(
            live_panel,
            textvariable=self.var_sig,
            font=sf(13, "bold"),
            bg="#080b11",
            fg="#cbd5e1",
            anchor="center",
        ).grid(row=5, column=0, sticky="ew", padx=sp(10, 4), pady=(0, sp(2, 1)))

        footer = tk.Frame(live_panel, bg="#0b0f16", bd=1, relief="groove")
        footer.grid(row=6, column=0, sticky="ew", padx=sp(12, 6), pady=(sp(4, 2), sp(10, 4)))
        footer.grid_columnconfigure(0, weight=3, uniform="errtol")
        footer.grid_columnconfigure(1, weight=1, uniform="errtol")

        err_box = tk.Frame(footer, bg="#0b0f16")
        err_box.grid(row=0, column=0, sticky="nsew", padx=(sp(8, 4), sp(2, 1)), pady=sp(6, 3))
        tk.Label(err_box, text="ERROR", font=sf(8, "bold"), bg="#0b0f16", fg="#f3f4f6").pack(anchor="w")
        tk.Label(err_box, textvariable=self.var_err, font=sf(30, "bold"), bg="#0b0f16", fg="#22c55e").pack(anchor="center")

        tol_box = tk.Frame(footer, bg="#0b0f16")
        tol_box.grid(row=0, column=1, sticky="nsew", padx=(sp(2, 1), sp(8, 4)), pady=sp(6, 3))
        tk.Label(tol_box, text="TOL", font=sf(14, "bold"), bg="#0b0f16", fg="#f3f4f6").pack(anchor="w", padx=(0, 0))
        tk.Label(tol_box, textvariable=self.var_tol, font=sf(20, "bold"), bg="#0b0f16", fg="#f8fafc", justify="left", anchor="w").pack(anchor="w", fill="x", padx=(0, 0))

        plot_panel = tk.LabelFrame(
            body,
            text="TENDENCIA DE PRESION",
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
        self.frm_cfg = controls

        sp_box = tk.Frame(controls, bg="#141922")
        sp_box.grid(row=0, column=0, sticky="w", padx=sp(10, 4), pady=sp(2, 1))
        tk.Label(sp_box, text="SETPOINT:", font=sf(10, "bold"), bg="#141922", fg="#fbbf24").grid(row=0, column=0, sticky="w", padx=(0, sp(6, 3)))
        self.btn_sp = tk.Button(
            sp_box,
            text=f"[{self.var_sp.get()}]",
            command=lambda: self._open_edit_dialog_sp(),
            font=sf(19, "bold"),
            bg="#090c12",
            fg="#f8fafc",
            activebackground="#171b24",
            activeforeground="#ffffff",
            width=sw(7, 5),
            bd=2,
            relief="raised",
            padx=sp(4, 2),
            pady=sp(2, 1),
        )
        self.btn_sp.grid(row=0, column=1, sticky="w")
        self.btn_sp_unit = tk.Button(
            sp_box,
            text=self.var_sp_unit.get(),
            width=sw(5, 4),
            command=self._open_sp_unit_selector,
            font=sf(17, "bold"),
            bg="#090c12",
            fg="#e2e8f0",
            activebackground="#171b24",
            activeforeground="#ffffff",
            bd=2,
            relief="raised",
            padx=sp(3, 1),
            pady=sp(2, 1),
        )
        self.btn_sp_unit.grid(row=0, column=2, sticky="w", padx=(sp(6, 3), 0))

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
        self.btn_stop_cfg = make_action_button("DETENER", self._stop_and_back, "#dc2626", width=9, font_size=15, pad_x=5, pad_y=3)
        self.btn_fft = make_action_button("FFT", self._open_fft_window, "#111827", width=5, font_size=15, pad_x=5, pad_y=3)
        self.btn_settings = make_action_button("\u2699", self._open_settings_window, "#111827", width=3, font_size=15, pad_x=4, pad_y=3)

        self.btn_start.pack(side="left", padx=sp(4, 2))
        self.btn_zero.pack(side="left", padx=sp(4, 2))
        self.btn_stop_cfg.pack(side="left", padx=sp(4, 2))
        self.btn_fft.pack(side="left", padx=sp(4, 2))
        self.btn_settings.pack(side="left", padx=(sp(4, 2), 0))

        tx_bar = tk.Frame(shell, bg="#0f1218")
        tx_bar.grid(row=3, column=0, sticky="ew", padx=sp(8, 4), pady=(sp(2, 1), sp(6, 3)))
        tx_center = tk.Frame(tx_bar, bg="#0f1218")
        tx_center.pack(anchor="center")

        self._tx_buttons = {}
        for label, channel in (("A0", 0), ("A1", 1), ("A2", 2), ("OFF", None)):
            btn = tk.Button(
                tx_center,
                text=label,
                command=lambda ch=channel: self._set_tx_channel(ch),
                font=sf(13, "bold"),
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
            btn.pack(side="left", padx=sp(2, 1))
            self._tx_buttons[channel] = btn

        self._on_mode_changed()
        self._update_sp_unit_ui()
        self._refresh_local_tx_buttons()

    def _build_live_plot(self):
        plot_box = self._plot_host
        if plot_box is None:
            return
        plot_box.grid_rowconfigure(0, weight=1)
        plot_box.grid_columnconfigure(0, weight=1)

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
        ax.set_title("Patron vs DUT", color="#f8fafc", fontsize=max(10, self._sp(13, 10)), fontweight="bold")
        ax.set_xlabel("Tiempo (s)", color="#e2e8f0", fontsize=max(8, self._sp(10, 8)))
        ax.set_ylabel("Presion", color="#e2e8f0", fontsize=max(8, self._sp(10, 8)))
        ax.tick_params(axis="x", colors="#e2e8f0", labelsize=max(7, self._sp(9, 7)))
        ax.tick_params(axis="y", colors="#e2e8f0", labelsize=max(7, self._sp(9, 7)))
        ax.grid(True, alpha=0.25, color="#94a3b8")
        for spine in ax.spines.values():
            spine.set_color("#94a3b8")
        ax.set_xlim(0.0, self._LIVE_PLOT_WINDOW_S)
        ax.set_ylim(0.0, 1.0)

        line_pat, = ax.plot([], [], color="#0b5aa2", linewidth=1.6, label="Patron")
        line_dut, = ax.plot([], [], color="#d97706", linewidth=1.4, label="DUT")
        legend = ax.legend(loc="upper left", fontsize=max(7, self._sp(8, 7)))
        legend.get_frame().set_facecolor("#0f172a")
        legend.get_frame().set_edgecolor("#475569")
        for text in legend.get_texts():
            text.set_color("#f8fafc")
        fig.subplots_adjust(left=0.12, right=0.98, top=0.90, bottom=0.16)

        canvas = FigureCanvasTkAgg(fig, master=plot_box)
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        canvas.draw()

        self._fig_live = fig
        self._ax_live = ax
        self._canvas_live = canvas
        self._line_live_pat = line_pat
        self._line_live_dut = line_dut

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

    def _set_tx_channel(self, channel: Optional[int]) -> None:
        try:
            top = self.winfo_toplevel()
            server = getattr(top, "telemetry_server", None)
            setter = getattr(server, "set_active_channel", None)
            if callable(setter):
                setter(channel)
            refresh = getattr(top, "_refresh_tx_state_label", None)
            if callable(refresh):
                refresh()
        finally:
            self._refresh_local_tx_buttons()

    def _refresh_local_tx_buttons(self) -> None:
        active = self._get_active_tx_channel()
        for channel, button in self._tx_buttons.items():
            if not self._widget_exists(button):
                continue
            is_active = (active == channel) or (channel is None and active is None)
            button.configure(
                bg="#2563eb" if is_active else "#1b2130",
                fg="#ffffff" if is_active else "#f8fafc",
                relief="sunken" if is_active else "raised",
            )

    @staticmethod
    def _widget_exists(widget) -> bool:
        try:
            return widget is not None and bool(widget.winfo_exists())
        except Exception:
            return False

    def _prepare_popup_window(self, window: tk.Toplevel, width: int, height: int) -> None:
        window.resizable(False, False)
        try:
            window.attributes("-topmost", True)
        except tk.TclError:
            pass
        window.transient(self.winfo_toplevel())
        window.update_idletasks()

        main_window = self.winfo_toplevel()
        main_x = main_window.winfo_x()
        main_y = main_window.winfo_y()
        main_width = main_window.winfo_width()
        main_height = main_window.winfo_height()
        width = min(int(width), max(260, main_width - 20))
        height = min(int(height), max(240, main_height - 20))
        center_x = main_x + main_width // 2
        center_y = main_y + main_height // 2
        x = max(0, center_x - width // 2)
        y = max(0, center_y - height // 2)

        window.geometry(f"{width}x{height}+{x}+{y}")
        window.lift()
        window.focus_force()
        window.grab_set()

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

    def _update_sp_unit_ui(self):
        unit = self.var_sp_unit.get().strip() or "kPa"
        if unit not in self._UNIT_TO_KPA:
            unit = "kPa"
        self.var_sp_unit.set(unit)
        self.cfg.sp_unit = unit
        self.var_settings_pressure_unit.set(unit)
        self.var_sp_label.set(f"SP ({unit}):")
        if self._widget_exists(self.btn_sp_unit):
            self.btn_sp_unit.configure(text=unit)
        if self._widget_exists(self.btn_sp_unit_popup):
            self.btn_sp_unit_popup.configure(text=unit)
        self._sync_pressure_display_from_kpa()

    def _update_settings_signal_ui(self, mode: Optional[str] = None):
        mode = (mode or self.var_mode.get().strip() or "A1").upper()
        if mode == "A0":
            label_min = "V mÃ­n"
            label_max = "V mÃ¡x"
            unit = "V"
        else:
            label_min = "I mÃ­n"
            label_max = "I mÃ¡x"
            unit = "mA"

        self.var_settings_signal_unit.set(unit)
        if self._widget_exists(self.lbl_sigmin):
            self.lbl_sigmin.configure(text=label_min)
        if self._widget_exists(self.lbl_sigmax):
            self.lbl_sigmax.configure(text=label_max)
        if self._widget_exists(self.btn_sigmin):
            self.btn_sigmin.configure(text=f"[{self.var_sigmin.get()}]")
        if self._widget_exists(self.btn_sigmax):
            self.btn_sigmax.configure(text=f"[{self.var_sigmax.get()}]")

    def _pressure_display_to_kpa(self, display_value: float) -> float:
        unit = self.var_sp_unit.get().strip() or "kPa"
        factor = float(self._UNIT_TO_KPA.get(unit, 1.0))
        return float(display_value) * factor

    def _pressure_kpa_to_display(self, kpa_value: float) -> float:
        unit = self.var_sp_unit.get().strip() or "kPa"
        factor = float(self._UNIT_TO_KPA.get(unit, 1.0))
        return float(kpa_value) / factor if abs(factor) > 1e-12 else float(kpa_value)

    def _fmt_display_pressure(self, value: float) -> str:
        txt = f"{float(value):.4f}".rstrip("0").rstrip(".")
        return txt if txt else "0"

    def _parse_display_pressure_kpa(
        self,
        raw: str,
        field_name: str,
        min_kpa: Optional[float] = None,
        max_kpa: Optional[float] = None,
    ) -> float:
        min_kpa = self._PRESSURE_MIN_KPA if min_kpa is None else float(min_kpa)
        max_kpa = self._PRESSURE_MAX_KPA if max_kpa is None else float(max_kpa)
        try:
            display_value = float(raw.strip().replace(",", "."))
        except Exception:
            raise ValueError(f"{field_name}: valor invÃ¡lido.")

        value_kpa = self._pressure_display_to_kpa(display_value)
        if value_kpa < min_kpa or value_kpa > max_kpa:
            unit = self.var_sp_unit.get().strip() or "kPa"
            raise ValueError(
                f"{field_name}: fuera de rango fÃ­sico {min_kpa:g}-{max_kpa:g} kPa. "
                f"({display_value:.4f} {unit} = {value_kpa:.4f} kPa)"
            )
        return float(value_kpa)

    def _sync_pressure_display_from_kpa(self):
        self.var_sp.set(self._fmt_display_pressure(self._pressure_kpa_to_display(self.cfg.sp_kpa)))
        self.var_pmin.set(self._fmt_display_pressure(self._pressure_kpa_to_display(self.cfg.p_min_kpa)))
        self.var_pmax.set(self._fmt_display_pressure(self._pressure_kpa_to_display(self.cfg.p_max_kpa)))
        self.var_pmaxseg.set(self._fmt_display_pressure(self._pressure_kpa_to_display(self.cfg.p_max_seguridad_kpa)))

        if self._widget_exists(self.btn_sp):
            self.btn_sp.configure(text=f"[{self.var_sp.get()}]")
        if self._widget_exists(self.btn_pmin):
            self.btn_pmin.configure(text=f"[{self.var_pmin.get()}]")
        if self._widget_exists(self.btn_pmax):
            self.btn_pmax.configure(text=f"[{self.var_pmax.get()}]")
        if self._widget_exists(self.btn_pmaxseg):
            self.btn_pmaxseg.configure(text=f"[{self.var_pmaxseg.get()}]")

    # -------------------------
    # Estados internos
    # -------------------------
    def _apply_state_config(self):
        self.rt.running = False
        self.rt.target_reached = False
        self.rt.in_band_since_ts = None
        self.pi_worker.reset()
        self.pi_worker.freeze()
        self.rt.last_update_ts = 0.0
        self._safe_outputs(valve_open=True)
        self._set_config_widgets_state(enabled=True)
        self._set_button_enabled(self.btn_stop_cfg, False)

    def _apply_state_run(self):
        self.rt.running = True
        self.rt.target_reached = False
        self.rt.in_band_since_ts = None
        self._reset_live_plot()
        self.pi_worker.reset()
        self.pi_worker.unfreeze()
        self.rt.last_update_ts = 0.0
        self.set_valve(True)
        self.set_relay(True)
        self._set_config_widgets_state(enabled=False)
        self._set_button_enabled(self.btn_stop_cfg, True)

    def _set_config_widgets_state(self, enabled: bool):
        state = "normal" if enabled else "disabled"

        def set_state_recursive(w):
            for c in w.winfo_children():
                cls = c.winfo_class()
                if cls in ("TEntry", "Entry"):
                    c.configure(state=state)
                elif cls in ("TRadiobutton", "Radiobutton"):
                    c.configure(state=state)
                elif cls in ("TCombobox", "Combobox"):
                    c.configure(state=state)
                set_state_recursive(c)

        set_state_recursive(self.frm_cfg)

        # SP siempre editable incluso en RUN
        if not enabled:
            try:
                for lf in self.frm_cfg.winfo_children():
                    if isinstance(lf, ttk.LabelFrame) and lf.cget("text") == "Control":
                        for e in lf.winfo_children():
                            if isinstance(e, ttk.Entry):
                                e.configure(state="normal")
            except Exception:
                pass

        if enabled:
            self._set_button_enabled(self.btn_start, True)
            self._set_button_enabled(self.btn_zero, True)
        else:
            self._set_button_enabled(self.btn_start, False)
            self._set_button_enabled(self.btn_zero, True)

    # -------------------------
    # Modal Edit Dialog
    # -------------------------
    def _open_edit_dialog(self, var: tk.StringVar, label: str, min_val: float, max_val: float, button: ttk.Button):
        """
        Abre un diÃ¡logo modal para editar un valor numÃ©rico con teclado integrado.
        Optimizado para pantalla tÃ¡ctil en Raspberry Pi.
        """
        dialog = tk.Toplevel(self)
        dialog.title(f"Editar: {label}")
        dialog.geometry("320x420")
        dialog.resizable(False, False)

        dialog.attributes("-topmost", True)
        dialog.transient(self.winfo_toplevel())

        dialog.update_idletasks()

        main_window = self.master if self.master else self
        main_x = main_window.winfo_x()
        main_y = main_window.winfo_y()
        main_width = main_window.winfo_width()
        main_height = main_window.winfo_height()

        center_x = main_x + main_width // 2
        center_y = main_y + main_height // 2

        modal_width = 320
        modal_height = 420
        x = max(0, center_x - modal_width // 2)
        y = max(0, center_y - modal_height // 2)

        dialog.geometry(f"{modal_width}x{modal_height}+{x}+{y}")

        dialog.focus_force()
        dialog.grab_set()
        dialog.update_idletasks()
        dialog.update()

        frm = ttk.Frame(dialog, padding=8)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=label, font=("Arial", 11, "bold")).pack(pady=(0, 2))
        ttk.Label(frm, text=f"Rango: {min_val} - {max_val}", font=("Arial", 8)).pack(pady=(0, 8))

        var_edit = tk.StringVar(value=var.get())
        entry_font = tkFont.Font(family="Arial", size=14, weight="bold")
        entry = tk.Entry(frm, textvariable=var_edit, justify="center", relief="solid", borderwidth=2)
        entry.config(font=entry_font)
        entry.pack(fill="x", ipady=10, pady=(0, 10))
        entry.select_range(0, len(var_edit.get()))
        entry.focus()
        replace_on_first_input = True

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
        tk.Button(row_frm, text="â†", width=btn_width, height=btn_height, command=delete_last,
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")

        # Borrar todo - botÃ³n compacto
        ttk.Button(kbd_frm, text="Borrar todo", command=clear_all).pack(fill="x", padx=2, pady=3)

        # Frame para botones de guardar/cancelar
        action_frm = ttk.Frame(frm)
        action_frm.pack(fill="x", pady=(6, 0))

        def on_save():
            try:
                valor = float(var_edit.get().strip().replace(",", "."))

                if valor < min_val or valor > max_val:
                    raise ValueError(f"Valor fuera de rango [{min_val}, {max_val}]")

                var.set(str(valor))
                button.config(text=f"[{valor}]")

                dialog.destroy()
            except ValueError as e:
                messagebox.showerror("Error", f"Valor invÃ¡lido: {str(e)}")

        def on_cancel():
            dialog.destroy()

        ttk.Button(action_frm, text="✓ Guardar", command=on_save).pack(side="left", padx=2, pady=2, fill="both", expand=True)
        ttk.Button(action_frm, text="✕ Cancelar", command=on_cancel).pack(side="left", padx=2, pady=2, fill="both", expand=True)

        entry.bind("<Return>", lambda e: on_save())
        entry.bind("<Escape>", lambda e: on_cancel())

        dialog.wait_window()

    def _close_settings_window(self, clear_snapshot: bool = False):
        win = self._settings_window
        self._settings_window = None
        self.btn_pmin = None
        self.btn_pmax = None
        self.btn_sigmin = None
        self.btn_sigmax = None
        self.btn_pmaxseg = None
        self.btn_sp_unit_popup = None
        self.lbl_sigmin = None
        self.lbl_sigmax = None
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
            "var_sp": self.var_sp.get(),
            "var_sp_unit": self.var_sp_unit.get(),
            "var_pmin": self.var_pmin.get(),
            "var_pmax": self.var_pmax.get(),
            "var_sigmin": self.var_sigmin.get(),
            "var_sigmax": self.var_sigmax.get(),
            "var_pmaxseg": self.var_pmaxseg.get(),
            "cfg_sp_kpa": float(self.cfg.sp_kpa),
            "cfg_sp_unit": str(self.cfg.sp_unit),
            "cfg_dut_mode": str(self.cfg.dut_mode),
            "cfg_p_min_kpa": float(self.cfg.p_min_kpa),
            "cfg_p_max_kpa": float(self.cfg.p_max_kpa),
            "cfg_sig_min": float(self.cfg.sig_min),
            "cfg_sig_max": float(self.cfg.sig_max),
            "cfg_p_max_seguridad_kpa": float(self.cfg.p_max_seguridad_kpa),
        }

    def _restore_settings_snapshot(self):
        snap = self._settings_snapshot
        if not snap:
            return

        self.var_mode.set(str(snap["var_mode"]))
        self.var_sp_unit.set(str(snap["var_sp_unit"]))
        self.var_sp.set(str(snap["var_sp"]))
        self.var_pmin.set(str(snap["var_pmin"]))
        self.var_pmax.set(str(snap["var_pmax"]))
        self.var_sigmin.set(str(snap["var_sigmin"]))
        self.var_sigmax.set(str(snap["var_sigmax"]))
        self.var_pmaxseg.set(str(snap["var_pmaxseg"]))

        self.cfg.sp_kpa = float(snap["cfg_sp_kpa"])
        self.cfg.sp_unit = str(snap["cfg_sp_unit"])
        self.cfg.dut_mode = str(snap["cfg_dut_mode"])
        self.cfg.p_min_kpa = float(snap["cfg_p_min_kpa"])
        self.cfg.p_max_kpa = float(snap["cfg_p_max_kpa"])
        self.cfg.sig_min = float(snap["cfg_sig_min"])
        self.cfg.sig_max = float(snap["cfg_sig_max"])
        self.cfg.p_max_seguridad_kpa = float(snap["cfg_p_max_seguridad_kpa"])

        self._update_sp_unit_ui()
        self._update_settings_signal_ui(self.cfg.dut_mode)
        self._sync_pressure_display_from_kpa()

    def _save_settings_window(self):
        try:
            self._pull_config_from_ui()
            self._validate_config()
        except Exception as e:
            messagebox.showerror("CONFIGURACION", str(e), parent=self._settings_window)
            return

        self._close_settings_window(clear_snapshot=True)

    def _cancel_settings_window(self):
        self._restore_settings_snapshot()
        self._close_settings_window(clear_snapshot=True)

    def _open_settings_window(self):
        if self._widget_exists(self._settings_window):
            self._settings_window.lift()
            self._settings_window.focus_force()
            return

        if self._settings_snapshot is None:
            self._settings_snapshot = self._capture_settings_snapshot()

        win = tk.Toplevel(self)
        win.title("Configuracion manual")
        self._prepare_popup_same_as_main(win)
        self._settings_window = win

        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        frm.grid_columnconfigure(0, weight=1)
        frm.grid_rowconfigure(1, weight=1)

        title = ttk.Label(frm, text="CONFIGURACION DEL DUT", font=("Arial", 18, "bold"))
        title.grid(row=0, column=0, sticky="ew", pady=(0, 16))

        body = ttk.Frame(frm)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1, uniform="settings")
        body.grid_columnconfigure(1, weight=1, uniform="settings")
        body.grid_rowconfigure(1, weight=1)

        mode_box = ttk.LabelFrame(body, text="DUT")
        mode_box.grid(row=0, column=0, sticky="new", padx=(0, 10), pady=(0, 10))
        mode_box.grid_columnconfigure(0, weight=1)

        ttk.Radiobutton(
            mode_box,
            text="P/I (4-20 mA)",
            value="A1",
            variable=self.var_mode,
            command=self._on_mode_changed,
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 6))
        ttk.Radiobutton(
            mode_box,
            text="P/V (0-10 V)",
            value="A0",
            variable=self.var_mode,
            command=self._on_mode_changed,
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(4, 12))

        cal_box = ttk.Frame(body)
        cal_box.grid(row=1, column=0, sticky="new", padx=(0, 10))
        cal_box.grid_columnconfigure(0, weight=1)
        ttk.Button(
            cal_box,
            text="CALIBRACION 2 PUNTOS",
            command=self._open_calibration_2pt_from_settings,
        ).grid(row=0, column=0, sticky="ew", pady=(8, 0), ipady=8)

        rng_box = ttk.LabelFrame(body, text="Rangos")
        rng_box.grid(row=0, column=1, rowspan=2, sticky="nsew", pady=(0, 10))
        rng_box.grid_columnconfigure(1, weight=1)
        rng_box.grid_columnconfigure(2, weight=0)

        ttk.Label(rng_box, text="P min").grid(row=0, column=0, sticky="w", padx=10, pady=(12, 6))
        self.btn_pmin = ttk.Button(rng_box, text=f"[{self.var_pmin.get()}]", command=self._open_edit_dialog_pmin)
        self.btn_pmin.grid(row=0, column=1, sticky="ew", padx=6, pady=(12, 6))
        ttk.Label(rng_box, textvariable=self.var_settings_pressure_unit).grid(row=0, column=2, sticky="w", padx=(6, 10), pady=(12, 6))

        ttk.Label(rng_box, text="P max").grid(row=1, column=0, sticky="w", padx=10, pady=6)
        self.btn_pmax = ttk.Button(rng_box, text=f"[{self.var_pmax.get()}]", command=self._open_edit_dialog_pmax)
        self.btn_pmax.grid(row=1, column=1, sticky="ew", padx=6, pady=6)
        ttk.Label(rng_box, textvariable=self.var_settings_pressure_unit).grid(row=1, column=2, sticky="w", padx=(6, 10), pady=6)

        self.lbl_sigmin = ttk.Label(rng_box, text="I mÃ­n")
        self.lbl_sigmin.grid(row=2, column=0, sticky="w", padx=10, pady=6)
        self.btn_sigmin = ttk.Button(
            rng_box,
            text=f"[{self.var_sigmin.get()}]",
            command=lambda: self._open_edit_dialog(self.var_sigmin, "SeÃ±al mÃ­n", 0, 100, self.btn_sigmin),
        )
        self.btn_sigmin.grid(row=2, column=1, sticky="ew", padx=6, pady=6)
        ttk.Label(rng_box, textvariable=self.var_settings_signal_unit).grid(row=2, column=2, sticky="w", padx=(6, 10), pady=6)

        self.lbl_sigmax = ttk.Label(rng_box, text="I mÃ¡x")
        self.lbl_sigmax.grid(row=3, column=0, sticky="w", padx=10, pady=6)
        self.btn_sigmax = ttk.Button(
            rng_box,
            text=f"[{self.var_sigmax.get()}]",
            command=lambda: self._open_edit_dialog(self.var_sigmax, "SeÃ±al mÃ¡x", 0, 100, self.btn_sigmax),
        )
        self.btn_sigmax.grid(row=3, column=1, sticky="ew", padx=6, pady=6)
        ttk.Label(rng_box, textvariable=self.var_settings_signal_unit).grid(row=3, column=2, sticky="w", padx=(6, 10), pady=6)

        ttk.Label(rng_box, text="P seg").grid(row=4, column=0, sticky="w", padx=10, pady=6)
        self.btn_pmaxseg = ttk.Button(rng_box, text=f"[{self.var_pmaxseg.get()}]", command=self._open_edit_dialog_pmaxseg)
        self.btn_pmaxseg.grid(row=4, column=1, sticky="ew", padx=6, pady=6)
        ttk.Label(rng_box, textvariable=self.var_settings_pressure_unit).grid(row=4, column=2, sticky="w", padx=(6, 10), pady=6)

        self.btn_sp_unit_popup = ttk.Button(
            rng_box,
            text=self.var_sp_unit.get(),
            command=self._open_sp_unit_selector,
        )
        self.btn_sp_unit_popup.grid(row=5, column=1, columnspan=2, sticky="ew", padx=6, pady=(12, 12), ipady=6)

        actions = ttk.Frame(frm)
        actions.grid(row=2, column=0, pady=(8, 0))
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_columnconfigure(1, weight=1)

        ttk.Button(actions, text="GUARDAR", command=self._save_settings_window).grid(
            row=0, column=0, sticky="ew", padx=(0, 10), ipady=8
        )
        ttk.Button(actions, text="CANCELAR", command=self._cancel_settings_window).grid(
            row=0, column=1, sticky="ew", padx=(10, 0), ipady=8
        )

        def _on_close():
            self._cancel_settings_window()

        win.protocol("WM_DELETE_WINDOW", _on_close)
        self._update_sp_unit_ui()
        self._update_settings_signal_ui(self.var_mode.get())
        self._on_mode_changed()

    def _open_calibration_2pt_from_settings(self):
        self._close_settings_window(clear_snapshot=False)
        self.after_idle(lambda: self._open_calibration_2pt(return_to_settings=True))

    def _close_calibration_2pt_window(self):
        win = self._calibration_window
        if not self._widget_exists(win):
            self._calibration_window = None
            return

        handler = getattr(win, "_manual_close_handler", None)
        if callable(handler):
            handler()
            return

        self._calibration_window = None
        try:
            win.grab_release()
        except Exception:
            pass
        try:
            win.destroy()
        except Exception:
            pass

    # -------------------------
    # Calibracion 2 puntos (A0/A1)
    # -------------------------
    def _open_calibration_2pt(self, return_to_settings: bool = False):
        try:
            if self._widget_exists(self._calibration_window):
                self._calibration_window.lift()
                self._calibration_window.focus_force()
                return

            win = tk.Toplevel(self)
            win.title("Calibracion 2 puntos (A0/A1/A2)")
            self._calibration_window = win
            win.grid_rowconfigure(0, weight=1)
            win.grid_columnconfigure(0, weight=1)
            frm = ttk.Frame(win, padding=10)
            frm.grid(row=0, column=0, sticky="nsew")
            frm.grid_columnconfigure(0, weight=1)
            frm.grid_rowconfigure(1, weight=1)

            var_chan = tk.StringVar(value="A0")
            var_x1 = tk.StringVar(value="--")
            var_x2 = tk.StringVar(value="--")
            var_y1 = tk.StringVar(value="0.000")
            var_y2 = tk.StringVar(value="0.000")
            var_m = tk.StringVar(value="--")
            var_b = tk.StringVar(value="--")
            var_units = tk.StringVar(value="V")
            var_pwm_pct = tk.StringVar(value="0.0")
            var_pwm_p = tk.StringVar(value="0.00")
            var_pwm_state = tk.StringVar(value="PWM OFF")
            pwm_enabled = {"on": False}
            pwm_refresh_after = {"id": None}

            def _update_units():
                mode = var_chan.get().strip().upper()
                if mode == "A0":
                    var_units.set("V")
                elif mode == "A1":
                    var_units.set("mA")
                else:
                    var_units.set("kPa")
                if mode == "A0":
                    var_m.set(f"{config.A0_CAL_M:.6f}")
                    var_b.set(f"{config.A0_CAL_B:.6f}")
                elif mode == "A1":
                    var_m.set(f"{config.A1_CAL_M:.6f}")
                    var_b.set(f"{config.A1_CAL_B:.6f}")
                else:
                    var_m.set(f"{config.GAIN_2PT:.6f}")
                    var_b.set(f"{config.OFFSET_2PT:.6f}")

            title = ttk.Label(frm, text="CALIBRACION 2 PUNTOS", font=("Arial", 16, "bold"))
            title.grid(row=0, column=0, sticky="ew", pady=(0, 8))

            content = ttk.Frame(frm)
            content.grid(row=1, column=0, sticky="nsew")
            content.grid_columnconfigure(0, weight=1, uniform="cal")
            content.grid_columnconfigure(1, weight=1, uniform="cal")
            content.grid_rowconfigure(0, weight=1)

            left_col = ttk.Frame(content)
            left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
            left_col.grid_columnconfigure(0, weight=1)

            right_col = ttk.Frame(content)
            right_col.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
            right_col.grid_columnconfigure(0, weight=1)

            chan_frame = ttk.LabelFrame(left_col, text="Canal")
            chan_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            chan_frame.grid_columnconfigure(0, weight=1)

            chan_box = ttk.Frame(chan_frame)
            chan_box.grid(row=0, column=0, sticky="w", padx=8, pady=8)
            chan_btns = {}

            def _set_chan(mode_sel: str):
                var_chan.set(mode_sel)

            def _refresh_chan_buttons(*_):
                current = var_chan.get().strip().upper()
                for mode_sel, btn in chan_btns.items():
                    if mode_sel == current:
                        btn.configure(relief="sunken", bg="#d9edf7")
                    else:
                        btn.configure(relief="raised", bg="#f0f0f0")

            for mode_sel in ("A0", "A1", "A2"):
                btn = tk.Button(
                    chan_box,
                    text=mode_sel,
                    width=4,
                    height=1,
                    font=("Arial", 10, "bold"),
                    command=lambda m=mode_sel: _set_chan(m),
                )
                btn.pack(side="left", padx=3)
                chan_btns[mode_sel] = btn

            var_chan.trace_add("write", _refresh_chan_buttons)
            _refresh_chan_buttons()

            p1_box = ttk.LabelFrame(left_col, text="Punto 1")
            p1_box.grid(row=1, column=0, sticky="ew", pady=(0, 8))
            p1_box.grid_columnconfigure(1, weight=1)

            ttk.Label(p1_box, text="y real").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
            btn_y1 = ttk.Button(p1_box, text=f"[{var_y1.get()}]")
            btn_y1.grid(row=0, column=1, sticky="ew", padx=6, pady=(8, 4))
            btn_y1.configure(
                command=lambda: self._open_edit_dialog(
                    var_y1,
                    f"Punto 1 (y_real) [{var_units.get()}]",
                    -1000.0,
                    1000.0,
                    btn_y1
                )
            )
            ttk.Label(p1_box, textvariable=var_units).grid(row=0, column=2, sticky="w", padx=(0, 8), pady=(8, 4))
            ttk.Button(p1_box, text="Capturar x1", command=lambda: _capture_point(1)).grid(
                row=1, column=0, columnspan=3, sticky="ew", padx=8, pady=4
            )
            ttk.Label(p1_box, text="x1 (Vadc):").grid(row=2, column=0, sticky="w", padx=8, pady=(4, 8))
            ttk.Label(p1_box, textvariable=var_x1).grid(row=2, column=1, columnspan=2, sticky="w", padx=6, pady=(4, 8))

            p2_box = ttk.LabelFrame(left_col, text="Punto 2")
            p2_box.grid(row=2, column=0, sticky="ew")
            p2_box.grid_columnconfigure(1, weight=1)

            ttk.Label(p2_box, text="y real").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
            btn_y2 = ttk.Button(p2_box, text=f"[{var_y2.get()}]")
            btn_y2.grid(row=0, column=1, sticky="ew", padx=6, pady=(8, 4))
            btn_y2.configure(
                command=lambda: self._open_edit_dialog(
                    var_y2,
                    f"Punto 2 (y_real) [{var_units.get()}]",
                    -1000.0,
                    1000.0,
                    btn_y2
                )
            )
            ttk.Label(p2_box, textvariable=var_units).grid(row=0, column=2, sticky="w", padx=(0, 8), pady=(8, 4))
            ttk.Button(p2_box, text="Capturar x2", command=lambda: _capture_point(2)).grid(
                row=1, column=0, columnspan=3, sticky="ew", padx=8, pady=4
            )
            ttk.Label(p2_box, text="x2 (Vadc):").grid(row=2, column=0, sticky="w", padx=8, pady=(4, 8))
            ttk.Label(p2_box, textvariable=var_x2).grid(row=2, column=1, columnspan=2, sticky="w", padx=6, pady=(4, 8))

            pwm_box = ttk.LabelFrame(right_col, text="Control bomba PWM")
            pwm_box.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            pwm_box.grid_columnconfigure(1, weight=1)
            pwm_box.grid_columnconfigure(2, weight=1)

            ttk.Label(pwm_box, text="PWM (%)").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
            btn_pwm_pct = ttk.Button(pwm_box, text=f"[{var_pwm_pct.get()}]")
            btn_pwm_pct.grid(row=0, column=1, sticky="ew", padx=6, pady=(8, 4))
            btn_pwm_toggle = ttk.Button(pwm_box, text="PWM ON")
            btn_pwm_toggle.grid(row=0, column=2, sticky="ew", padx=6, pady=(8, 4))
            ttk.Label(pwm_box, text="P actual (kPa)").grid(row=1, column=0, sticky="w", padx=8, pady=4)
            ttk.Label(pwm_box, textvariable=var_pwm_p).grid(row=1, column=1, sticky="w", padx=6, pady=4)
            ttk.Label(pwm_box, textvariable=var_pwm_state).grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 8))

            result_box = ttk.LabelFrame(right_col, text="Coeficientes")
            result_box.grid(row=1, column=0, sticky="ew", pady=(0, 8))
            result_box.grid_columnconfigure(1, weight=1)

            ttk.Label(result_box, text="m:").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
            ttk.Label(result_box, textvariable=var_m).grid(row=0, column=1, sticky="w", padx=6, pady=(8, 4))
            ttk.Label(result_box, text="b:").grid(row=1, column=0, sticky="w", padx=8, pady=(4, 8))
            ttk.Label(result_box, textvariable=var_b).grid(row=1, column=1, sticky="w", padx=6, pady=(4, 8))

            def _capture_point(idx: int):
                mode = var_chan.get().strip().upper()
                if mode == "A0":
                    ch = config.ADS_CH_DUT_V
                elif mode == "A1":
                    ch = config.ADS_CH_DUT_mA
                else:
                    ch = config.ADS_CH_REF
                x = float(self.read_vadc(ch))
                if idx == 1:
                    var_x1.set(f"{x:.6f}")
                else:
                    var_x2.set(f"{x:.6f}")

            def _calc_and_save():
                try:
                    x1 = float(var_x1.get())
                    x2 = float(var_x2.get())
                    y1 = float(var_y1.get().strip().replace(",", "."))
                    y2 = float(var_y2.get().strip().replace(",", "."))
                    mode = var_chan.get().strip().upper()
                    if mode == "A2":
                        p1 = config.MPX_A2 * x1 * x1 + config.MPX_B2 * x1 + config.MPX_C2
                        p2 = config.MPX_A2 * x2 * x2 + config.MPX_B2 * x2 + config.MPX_C2
                        if p1 < 0:
                            p1 = 0.0
                        if p2 < 0:
                            p2 = 0.0
                        m, b = two_point_cal(p1, y1, p2, y2)
                    else:
                        m, b = two_point_cal(x1, y1, x2, y2)
                    if mode == "A0":
                        config.A0_CAL_M = float(m)
                        config.A0_CAL_B = float(b)
                    elif mode == "A1":
                        config.A1_CAL_M = float(m)
                        config.A1_CAL_B = float(b)
                    else:
                        config.GAIN_2PT = float(m)
                        config.OFFSET_2PT = float(b)
                        config.A2_CAL_M = float(m)
                        config.A2_CAL_B = float(b)

                    cal = {
                        "A0": {"m": float(config.A0_CAL_M), "b": float(config.A0_CAL_B), "units": "V_in"},
                        "A1": {"m": float(config.A1_CAL_M), "b": float(config.A1_CAL_B), "units": "mA"},
                        "A2": {"m": float(config.GAIN_2PT), "b": float(config.OFFSET_2PT), "units": "kPa"},
                    }
                    save_calibration(cal)

                    var_m.set(f"{m:.6f}")
                    var_b.set(f"{b:.6f}")
                    messagebox.showinfo("Calibracion", "Guardado OK.")
                except Exception as e:
                    messagebox.showerror("Calibracion", f"Error: {e}")

            actions = ttk.Frame(right_col)
            actions.grid(row=2, column=0, sticky="sew")
            actions.grid_columnconfigure(0, weight=1)
            actions.grid_columnconfigure(1, weight=1)

            ttk.Button(actions, text="CALCULAR Y GUARDAR", command=_calc_and_save).grid(
                row=0, column=0, sticky="ew", padx=(0, 6), ipady=6
            )
            ttk.Button(
                actions,
                text="ATRAS",
                command=lambda: _back_to_settings(),
            ).grid(row=0, column=1, sticky="ew", padx=(6, 0), ipady=6)

            def _disable_manual_pwm():
                pwm_enabled["on"] = False
                try:
                    self.pi_worker.freeze()
                except Exception:
                    pass
                try:
                    self.set_pump(config.BOMBA_U_OFF if hasattr(config, "BOMBA_U_OFF") else 1.0)
                    self.set_relay(False)
                except Exception:
                    pass
                btn_pwm_toggle.configure(text="PWM ON")
                var_pwm_state.set("PWM OFF")

            def _parse_pwm_pct() -> float:
                try:
                    pct = float(var_pwm_pct.get().strip().replace(",", "."))
                except Exception:
                    raise ValueError("PWM invalido.")
                if pct < 0.0 or pct > 100.0:
                    raise ValueError("PWM fuera de rango [0, 100].")
                return float(pct)

            def _apply_manual_pwm():
                pct = _parse_pwm_pct()
                u_cmd = pct / 100.0
                self.rt.running = False
                self.rt.target_reached = False
                self.rt.in_band_since_ts = None
                try:
                    self.pi_worker.freeze()
                except Exception:
                    pass
                self.set_relay(True)
                self.set_pump(float(u_cmd))
                pwm_enabled["on"] = True
                btn_pwm_toggle.configure(text="PWM OFF")
                var_pwm_state.set(f"PWM ON | u={u_cmd:.3f}")

            def _edit_pwm_pct():
                self._open_edit_dialog(var_pwm_pct, "PWM (%)", 0.0, 100.0, btn_pwm_pct)
                if pwm_enabled["on"]:
                    try:
                        _apply_manual_pwm()
                    except Exception as e:
                        _disable_manual_pwm()
                        messagebox.showerror("PWM", str(e), parent=win)

            btn_pwm_pct.configure(command=_edit_pwm_pct)

            def _toggle_manual_pwm():
                if pwm_enabled["on"]:
                    _disable_manual_pwm()
                else:
                    try:
                        _apply_manual_pwm()
                    except Exception as e:
                        messagebox.showerror("PWM", str(e), parent=win)

            btn_pwm_toggle.configure(command=_toggle_manual_pwm)

            def _refresh_pwm_status():
                try:
                    p_now = float(self._read_control_pressure_kpa())
                    var_pwm_p.set(f"{p_now:.2f}")
                except Exception as e:
                    var_pwm_p.set("--")
                    if pwm_enabled["on"]:
                        var_pwm_state.set(f"PWM ERR: {e}")
                finally:
                    if win.winfo_exists():
                        pwm_refresh_after["id"] = win.after(120, _refresh_pwm_status)

            def _on_chan_change(*_):
                _update_units()

            var_chan.trace_add("write", _on_chan_change)
            _update_units()

            def _on_close(reopen_settings: bool = False):
                self._calibration_window = None
                try:
                    if pwm_refresh_after["id"] is not None:
                        win.after_cancel(pwm_refresh_after["id"])
                except Exception:
                    pass
                _disable_manual_pwm()
                try:
                    win.grab_release()
                except Exception:
                    pass
                win.destroy()
                if reopen_settings and return_to_settings:
                    self.after_idle(self._open_settings_window)
                elif return_to_settings:
                    self._settings_snapshot = None

            def _back_to_settings():
                _on_close(reopen_settings=True)

            win._manual_close_handler = _on_close
            win.protocol("WM_DELETE_WINDOW", lambda: _on_close(reopen_settings=False))
            self._prepare_popup_same_as_main(win)
            _refresh_pwm_status()
        except Exception as e:
            self._calibration_window = None
            messagebox.showerror("Calibracion", f"No se pudo abrir la ventana: {e}")

    # -------------------------
    # FFT / Ruido
    # -------------------------
    def _open_fft_window(self):
        try:
            win = tk.Toplevel(self)
            win.title("FFT / Ruido")
            win.geometry("800x480")
            win.transient(self.winfo_toplevel())
            win.lift()
            win.focus_force()
            win.grab_set()

            frm = ttk.Frame(win, padding=8)
            frm.pack(fill="both", expand=True)

            top = ttk.Frame(frm)
            top.pack(fill="x", pady=(0, 6))

            var_chan = tk.StringVar(value="A1")
            ttk.Label(top, text="Canal:").pack(side="left", padx=4)
            chan_box = ttk.Frame(top)
            chan_box.pack(side="left", padx=4)
            chan_btns = {}

            def _set_chan(mode_sel: str):
                var_chan.set(mode_sel)

            def _refresh_chan_buttons(*_):
                current = var_chan.get().strip().upper()
                for mode_sel, btn in chan_btns.items():
                    if mode_sel == current:
                        btn.configure(relief="sunken", bg="#d9edf7")
                    else:
                        btn.configure(relief="raised", bg="#f0f0f0")

            chan_labels = {"A0": "Senal V", "A1": "Senal I", "A3": "Senal A3"}
            for mode_sel in ("A0", "A1", "A3"):
                btn = tk.Button(
                    chan_box,
                    text=chan_labels.get(mode_sel, mode_sel),
                    width=4,
                    height=2,
                    font=("Arial", 10, "bold"),
                    command=lambda m=mode_sel: _set_chan(m),
                )
                btn.pack(side="left", padx=2)
                chan_btns[mode_sel] = btn

            var_chan.trace_add("write", _refresh_chan_buttons)
            _refresh_chan_buttons()

            var_n = tk.IntVar(value=int(getattr(config, "FFT_N_SAMPLES", 1024)))
            ttk.Label(top, text="N muestras:").pack(side="left", padx=4)
            var_n_txt = tk.StringVar(value=str(var_n.get()))
            btn_n = ttk.Button(top, text=f"[{var_n_txt.get()}]")
            btn_n.pack(side="left", padx=4)
            btn_n.configure(
                command=lambda: self._open_edit_dialog(
                    var_n_txt,
                    "N muestras FFT",
                    64,
                    16384,
                    btn_n
                )
            )

            lbl_metrics = ttk.Label(top, text="RMS=-- | STD=-- | Pico=-- Hz @ --")
            lbl_metrics.pack(side="left", padx=10)

            actions = ttk.Frame(frm)
            actions.pack(fill="x", pady=(0, 6))

            fig = Figure(figsize=(7.5, 3.0), dpi=100)
            ax = fig.add_subplot(111)
            ax.set_title("FFT Magnitud")
            ax.set_xlabel("Frecuencia (Hz)")
            ax.set_ylabel("Magnitud")
            ax.grid(True, alpha=0.3)

            canvas = FigureCanvasTkAgg(fig, master=frm)
            canvas.get_tk_widget().pack(fill="both", expand=True)

            def _run_fft():
                try:
                    n = max(64, int(var_n.get()))
                    try:
                        n = max(64, int(float(var_n_txt.get().strip().replace(",", "."))))
                        var_n.set(n)
                    except Exception:
                        n = max(64, int(var_n.get()))
                    mode = var_chan.get().strip().upper()
                    if mode == "A0":
                        ch = config.ADS_CH_DUT_V
                    elif mode == "A3":
                        ch = int(getattr(config, "ADS_CH_A3", 3))
                    else:
                        ch = config.ADS_CH_DUT_mA

                    samples = np.zeros(n, dtype=float)
                    for i in range(n):
                        samples[i] = float(self.read_vadc(ch))

                    fs = float(getattr(config, "ADS_SPS", 128))
                    samples = samples - float(np.mean(samples))

                    if bool(getattr(config, "FFT_USE_WINDOW", True)):
                        wnd = np.hanning(n)
                        samples_win = samples * wnd
                    else:
                        samples_win = samples

                    fft_vals = np.fft.rfft(samples_win)
                    mag = np.abs(fft_vals) / max(1, n)
                    freqs = np.fft.rfftfreq(n, d=1.0 / fs)

                    if len(mag) > 1:
                        idx = int(np.argmax(mag[1:])) + 1
                        peak_f = float(freqs[idx])
                        peak_a = float(mag[idx])
                    else:
                        peak_f = 0.0
                        peak_a = 0.0

                    rms = float(np.sqrt(np.mean(samples * samples)))
                    std = float(np.std(samples, ddof=1)) if n > 1 else 0.0

                    ax.clear()
                    ax.plot(freqs, mag, color="blue")
                    ax.set_title("FFT Magnitud")
                    ax.set_xlabel("Frecuencia (Hz)")
                    ax.set_ylabel("Magnitud")
                    ax.grid(True, alpha=0.3)
                    canvas.draw()

                    lbl_metrics.config(text=f"RMS={rms:.6f} | STD={std:.6f} | Pico={peak_f:.2f} Hz @ {peak_a:.6f}")
                except Exception as e:
                    messagebox.showerror("FFT", f"Error: {e}")

            ttk.Button(actions, text="Capturar y Calcular", command=_run_fft).pack(side="left", padx=6)
        except Exception as e:
            messagebox.showerror("FFT", f"No se pudo abrir la ventana: {e}")

    def _open_edit_dialog_sp(self):
        """Abre modal para editar SP con aplicaciÃ³n automÃ¡tica"""
        unit = self.var_sp_unit.get().strip() or "kPa"
        label = f"SP ({unit})"
        min_val = self._pressure_kpa_to_display(self._PRESSURE_MIN_KPA)
        max_val = self._pressure_kpa_to_display(self._PRESSURE_MAX_KPA)

        dialog = tk.Toplevel(self)
        dialog.title(f"Editar: {label}")
        dialog.geometry("320x420")
        dialog.resizable(False, False)

        dialog.attributes("-topmost", True)
        dialog.transient(self.winfo_toplevel())

        dialog.update_idletasks()

        main_window = self.master if self.master else self
        main_x = main_window.winfo_x()
        main_y = main_window.winfo_y()
        main_width = main_window.winfo_width()
        main_height = main_window.winfo_height()

        center_x = main_x + main_width // 2
        center_y = main_y + main_height // 2

        modal_width = 320
        modal_height = 420
        x = max(0, center_x - modal_width // 2)
        y = max(0, center_y - modal_height // 2)

        dialog.geometry(f"{modal_width}x{modal_height}+{x}+{y}")

        dialog.focus_force()
        dialog.grab_set()
        dialog.update_idletasks()
        dialog.update()

        frm = ttk.Frame(dialog, padding=8)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=label, font=("Arial", 11, "bold")).pack(pady=(0, 2))
        ttk.Label(frm, text=f"Rango: {self._fmt_display_pressure(min_val)} - {self._fmt_display_pressure(max_val)} {unit}", font=("Arial", 8)).pack(pady=(0, 8))

        var_edit = tk.StringVar(value=self.var_sp.get())
        entry_font = tkFont.Font(family="Arial", size=14, weight="bold")
        entry = tk.Entry(frm, textvariable=var_edit, justify="center", relief="solid", borderwidth=2)
        entry.config(font=entry_font)
        entry.pack(fill="x", ipady=10, pady=(0, 10))
        entry.select_range(0, len(var_edit.get()))
        entry.focus()
        replace_on_first_input = True

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
        tk.Button(row_frm, text="â†", width=btn_width, height=btn_height, command=delete_last,
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")

        ttk.Button(kbd_frm, text="Borrar todo", command=clear_all).pack(fill="x", padx=2, pady=3)

        action_frm = ttk.Frame(frm)
        action_frm.pack(fill="x", pady=(6, 0))

        def on_save():
            try:
                self.var_sp.set(var_edit.get())
                self._apply_sp()

                dialog.destroy()
            except ValueError as e:
                messagebox.showwarning("Rango invÃ¡lido", str(e))

        def on_cancel():
            dialog.destroy()

        ttk.Button(action_frm, text="✓ Guardar", command=on_save).pack(side="left", padx=2, pady=2, fill="both", expand=True)
        ttk.Button(action_frm, text="✕ Cancelar", command=on_cancel).pack(side="left", padx=2, pady=2, fill="both", expand=True)

        entry.bind("<Return>", lambda e: on_save())
        entry.bind("<Escape>", lambda e: on_cancel())

        dialog.wait_window()

    def _open_edit_dialog_pmin(self):
        self._open_edit_dialog_pressure_value("p_min_kpa", "P mÃ­n")

    def _open_edit_dialog_pmax(self):
        self._open_edit_dialog_pressure_value("p_max_kpa", "P mÃ¡x")

    def _open_edit_dialog_pmaxseg(self):
        self._open_edit_dialog_pressure_value(
            "p_max_seguridad_kpa",
            "P seg",
            max_kpa=self._PRESSURE_SAFETY_MAX_KPA,
        )

    def _open_edit_dialog_pressure_value(self, attr_name: str, field_label: str, max_kpa: Optional[float] = None):
        unit = self.var_sp_unit.get().strip() or "kPa"
        min_val = self._pressure_kpa_to_display(self._PRESSURE_MIN_KPA)
        max_kpa = self._PRESSURE_MAX_KPA if max_kpa is None else float(max_kpa)
        max_val = self._pressure_kpa_to_display(max_kpa)
        current_kpa = float(getattr(self.cfg, attr_name))
        current_disp = self._fmt_display_pressure(self._pressure_kpa_to_display(current_kpa))

        if attr_name == "p_min_kpa":
            button = self.btn_pmin
        elif attr_name == "p_max_kpa":
            button = self.btn_pmax
        elif attr_name == "p_max_seguridad_kpa":
            button = self.btn_pmaxseg
        else:
            raise ValueError("Campo de presiÃ³n invÃ¡lido.")

        def _on_save(raw_value: str):
            value_kpa = self._parse_display_pressure_kpa(
                raw_value,
                field_label,
                min_kpa=self._PRESSURE_MIN_KPA,
                max_kpa=max_kpa,
            )
            setattr(self.cfg, attr_name, value_kpa)
            self._sync_pressure_display_from_kpa()
            if self._widget_exists(button):
                if attr_name == "p_min_kpa":
                    button.configure(text=f"[{self.var_pmin.get()}]")
                elif attr_name == "p_max_kpa":
                    button.configure(text=f"[{self.var_pmax.get()}]")
                else:
                    button.configure(text=f"[{self.var_pmaxseg.get()}]")

        self._open_numeric_keypad_dialog(
            title=f"{field_label} ({unit})",
            range_text=f"Rango: {self._fmt_display_pressure(min_val)} - {self._fmt_display_pressure(max_val)} {unit}",
            initial_value=current_disp,
            on_save=_on_save,
        )

    def _open_numeric_keypad_dialog(self, title: str, range_text: str, initial_value: str, on_save):
        dialog = tk.Toplevel(self)
        dialog.title(f"Editar: {title}")
        dialog.geometry("320x420")
        dialog.resizable(False, False)
        dialog.attributes("-topmost", True)
        dialog.transient(self.winfo_toplevel())
        dialog.update_idletasks()

        main_window = self.master if self.master else self
        main_x = main_window.winfo_x()
        main_y = main_window.winfo_y()
        main_width = main_window.winfo_width()
        main_height = main_window.winfo_height()
        center_x = main_x + main_width // 2
        center_y = main_y + main_height // 2
        modal_width = 320
        modal_height = 420
        x = max(0, center_x - modal_width // 2)
        y = max(0, center_y - modal_height // 2)
        dialog.geometry(f"{modal_width}x{modal_height}+{x}+{y}")

        dialog.focus_force()
        dialog.grab_set()
        dialog.update_idletasks()
        dialog.update()

        frm = ttk.Frame(dialog, padding=8)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=title, font=("Arial", 11, "bold")).pack(pady=(0, 2))
        ttk.Label(frm, text=range_text, font=("Arial", 8)).pack(pady=(0, 8))

        var_edit = tk.StringVar(value=initial_value)
        entry_font = tkFont.Font(family="Arial", size=14, weight="bold")
        entry = tk.Entry(frm, textvariable=var_edit, justify="center", relief="solid", borderwidth=2)
        entry.config(font=entry_font)
        entry.pack(fill="x", ipady=10, pady=(0, 10))
        entry.select_range(0, len(var_edit.get()))
        entry.focus()
        replace_on_first_input = True

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

        btn_font = tkFont.Font(family="Arial", size=10, weight="bold")
        btn_width = 3
        btn_height = 1

        row_frm = ttk.Frame(kbd_frm)
        row_frm.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Button(row_frm, text="7", width=btn_width, height=btn_height, command=lambda: add_digit(7), font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="8", width=btn_width, height=btn_height, command=lambda: add_digit(8), font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="9", width=btn_width, height=btn_height, command=lambda: add_digit(9), font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")

        row_frm = ttk.Frame(kbd_frm)
        row_frm.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Button(row_frm, text="4", width=btn_width, height=btn_height, command=lambda: add_digit(4), font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="5", width=btn_width, height=btn_height, command=lambda: add_digit(5), font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="6", width=btn_width, height=btn_height, command=lambda: add_digit(6), font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")

        row_frm = ttk.Frame(kbd_frm)
        row_frm.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Button(row_frm, text="1", width=btn_width, height=btn_height, command=lambda: add_digit(1), font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="2", width=btn_width, height=btn_height, command=lambda: add_digit(2), font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="3", width=btn_width, height=btn_height, command=lambda: add_digit(3), font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")

        row_frm = ttk.Frame(kbd_frm)
        row_frm.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Button(row_frm, text="0", width=btn_width, height=btn_height, command=lambda: add_digit(0), font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text=".", width=btn_width, height=btn_height, command=add_decimal, font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")
        tk.Button(row_frm, text="â†", width=btn_width, height=btn_height, command=delete_last, font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")

        ttk.Button(kbd_frm, text="Borrar todo", command=clear_all).pack(fill="x", padx=2, pady=3)

        action_frm = ttk.Frame(frm)
        action_frm.pack(fill="x", pady=(6, 0))

        def save_and_close():
            try:
                on_save(var_edit.get())
                dialog.destroy()
            except ValueError as e:
                messagebox.showwarning("Rango invÃ¡lido", str(e))

        def on_cancel():
            dialog.destroy()

        ttk.Button(action_frm, text="✓ Guardar", command=save_and_close).pack(side="left", padx=2, pady=2, fill="both", expand=True)
        ttk.Button(action_frm, text="✕ Cancelar", command=on_cancel).pack(side="left", padx=2, pady=2, fill="both", expand=True)

        entry.bind("<Return>", lambda e: save_and_close())
        entry.bind("<Escape>", lambda e: on_cancel())

        dialog.wait_window()

    def _open_sp_unit_selector(self):
        dialog = tk.Toplevel(self)
        dialog.title("Seleccionar unidad de presion")
        dialog.geometry("280x360")
        dialog.resizable(False, False)
        dialog.transient(self.winfo_toplevel())
        dialog.focus_force()
        dialog.grab_set()

        frm = ttk.Frame(dialog, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Unidad de presion", font=("Arial", 11, "bold")).pack(anchor="w", pady=(0, 6))

        list_frm = ttk.Frame(frm)
        list_frm.pack(fill="both", expand=True)

        yscroll = ttk.Scrollbar(list_frm, orient="vertical")
        yscroll.pack(side="right", fill="y")

        lst_units = tk.Listbox(list_frm, exportselection=False, yscrollcommand=yscroll.set, height=11)
        lst_units.pack(side="left", fill="both", expand=True)
        yscroll.configure(command=lst_units.yview)

        for unit in self._SP_UNITS:
            lst_units.insert("end", unit)

        current = self.var_sp_unit.get().strip() or "kPa"
        try:
            idx = self._SP_UNITS.index(current)
        except ValueError:
            idx = self._SP_UNITS.index("kPa")
        lst_units.selection_set(idx)
        lst_units.activate(idx)
        lst_units.see(idx)

        action_frm = ttk.Frame(frm)
        action_frm.pack(fill="x", pady=(8, 0))

        def on_save():
            sel = lst_units.curselection()
            if not sel:
                return
            self.var_sp_unit.set(self._SP_UNITS[int(sel[0])])
            self._update_sp_unit_ui()
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        ttk.Button(action_frm, text="Guardar", command=on_save).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(action_frm, text="Cancelar", command=on_cancel).pack(side="left", fill="x", expand=True, padx=(4, 0))

        lst_units.bind("<Double-Button-1>", lambda _e: on_save())
        dialog.wait_window()

    # -------------------------
    # Acciones
    # -------------------------
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

        sig_min = _parse_sig(self.var_sigmin, self.cfg.sig_min)
        sig_max = _parse_sig(self.var_sigmax, self.cfg.sig_max)

        if mode == "A0":
            # Si viene del rango tÃ­pico A1 (4-20), conmutar a 0-10 V.
            if _is_close_pair(sig_min, sig_max, self._A1_SIG_MIN_DEFAULT, self._A1_SIG_MAX_DEFAULT):
                sig_min = self._A0_SIG_MIN_DEFAULT
                sig_max = self._A0_SIG_MAX_DEFAULT
                self.var_sigmin.set(f"{sig_min:.3f}")
                self.var_sigmax.set(f"{sig_max:.3f}")
        else:
            # Si viene del rango tÃ­pico A0 (0-10), conmutar a 4-20 mA.
            if _is_close_pair(sig_min, sig_max, self._A0_SIG_MIN_DEFAULT, self._A0_SIG_MAX_DEFAULT):
                sig_min = self._A1_SIG_MIN_DEFAULT
                sig_max = self._A1_SIG_MAX_DEFAULT
                self.var_sigmin.set(f"{sig_min:.3f}")
                self.var_sigmax.set(f"{sig_max:.3f}")

        # Sincronizar cÃ¡lculo live sin esperar START.
        self.cfg.sig_min = float(sig_min)
        self.cfg.sig_max = float(sig_max)
        self._update_settings_signal_ui(mode)

    def _do_tare(self):
        try:
            p_corr = self._read_pressure_corr_kpa()
            self.rt.p_zero_kpa = p_corr
            messagebox.showinfo("TARA", f"Tara aplicada.\nAhora Pâ‰ˆ0 desde Pcorr={p_corr:.2f} kPa")
        except Exception as e:
            messagebox.showerror("TARA", f"No se pudo aplicar tara: {e}")

    # Solo aplica SP con botÃ³n/Enter
    def _apply_sp(self):
        prev_sp_kpa = float(self.cfg.sp_kpa)
        self.cfg.sp_kpa = self._parse_display_pressure_kpa(self.var_sp.get(), "SP")
        self._sync_pressure_display_from_kpa()
        if self.rt.running:
            self.rt.target_reached = False
            self.rt.in_band_since_ts = None
            p_now = float(self._get_runtime_snapshot().get("p_kpa", 0.0))
            sp_changed = abs(float(self.cfg.sp_kpa) - prev_sp_kpa) > 1e-9
            if sp_changed:
                self.pi_worker.retarget(
                    sp_kpa=float(self.cfg.sp_kpa),
                    p_kpa=float(p_now),
                )
            self.pi_worker.unfreeze()
            error_now = float(self.cfg.sp_kpa) - float(p_now)
            self.pi_worker.set_zone_from_sp(
                zone_sp_kpa=float(self.cfg.sp_kpa),
                error_now=error_now,
            )
            with self._runtime_lock:
                self._runtime_snapshot["u_text"] = "u=SYNC"

    def _enter_manual_static_hold(self) -> None:
        self.rt.target_reached = True
        self.rt.in_band_since_ts = None
        self.pi_worker.freeze()
        self.set_pump(1.0)
        self.set_relay(False)
        self.set_valve(False)

    def _update_manual_static_hold(self, now_ts: float, p_kpa: float) -> bool:
        band = float(self._manual_hold_band_kpa)
        err_abs = abs(float(self.cfg.sp_kpa) - float(p_kpa))

        if self.rt.target_reached:
            self.set_pump(1.0)
            self.set_relay(False)
            self.set_valve(False)
            return True

        if err_abs > band:
            self.rt.in_band_since_ts = None
            return False

        if self.rt.in_band_since_ts is None:
            self.rt.in_band_since_ts = float(now_ts)
            return False

        if (float(now_ts) - float(self.rt.in_band_since_ts)) >= float(self._manual_hold_delay_s):
            self._enter_manual_static_hold()
            return True

        return False

    def _start(self):
        try:
            self._pull_config_from_ui()
            self._validate_config()
            pi_u_min, pi_u_max = self._effective_u_bounds(config.PI_CFG.u_min, config.PI_CFG.u_max)
            self.pi.cfg.u_min = pi_u_min
            self.pi.cfg.u_max = pi_u_max
            self.pi.cfg.u_ff = max(pi_u_min, min(float(config.PI_CFG.u_ff), pi_u_max))
            self._apply_state_run()
            self._apply_sp()
        except Exception as e:
            messagebox.showerror("CONFIG", str(e))

    def _stop_to_config(self):
        self._safe_outputs(valve_open=True)
        self._apply_state_config()

    def _stop_and_back(self):
        self._safe_outputs(valve_open=True)
        self._apply_state_config()
        self.request_event("EV_BACK", None)

    def _back_to_idle(self):
        self._safe_outputs(valve_open=True)
        self.request_event("EV_BACK", None)

    # -------------------------
    # Lecturas
    # -------------------------
    def _read_vadc_live(self, ch: int) -> float:
        return float(self.read_vadc_live(ch))

    def _read_pressure_corr_kpa(self) -> float:
        vadc = self._read_vadc_live(config.ADS_CH_REF)
        return mpx_vadc_to_kpa(vadc)

    def _read_control_pressure_kpa(self) -> float:
        p_corr = self._read_pressure_corr_kpa()
        p = p_corr - float(self.rt.p_zero_kpa)
        if p < 0.0:
            p = 0.0
        return float(p)

    def _read_dut_eng(self) -> float:
        ch = config.ADS_CH_DUT_V if self.cfg.dut_mode == "A0" else config.ADS_CH_DUT_mA
        vadc = self._read_vadc_live(ch)
        return dut_vadc_to_eng(vadc, self.cfg.dut_mode)

    def _compute_span_percent(self, dut_eng: float) -> float:
        span = self.cfg.sig_max - self.cfg.sig_min
        if abs(span) < 1e-9:
            return 0.0
        return 100.0 * (dut_eng - self.cfg.sig_min) / span

    def _compute_error_percent_fluke_style(self, p_source_kpa: float, dut_eng: float) -> float:
        p_span = self.cfg.p_max_kpa - self.cfg.p_min_kpa
        sig_span = self.cfg.sig_max - self.cfg.sig_min
        if abs(p_span) < 1e-9 or abs(sig_span) < 1e-9:
            return 0.0
        p_pct = 100.0 * (p_source_kpa - self.cfg.p_min_kpa) / p_span
        sig_pct = 100.0 * (dut_eng - self.cfg.sig_min) / sig_span
        return sig_pct - p_pct

    def _get_live_signal_bounds(self) -> tuple[float, float]:
        return float(self.cfg.sig_min), float(self.cfg.sig_max)

    @staticmethod
    def _dut_est_pressure_kpa(x_meas: float, x_min: float, x_max: float, p_min: float, p_max: float) -> float:
        den = x_max - x_min
        if abs(den) < 1e-9:
            return float(p_min)
        return float(p_min + (x_meas - x_min) * (p_max - p_min) / den)

    def _start_runtime_worker(self):
        if self._runtime_worker is not None and self._runtime_worker.is_alive():
            return
        self._runtime_stop_evt.clear()
        self._runtime_worker = threading.Thread(
            target=self._runtime_worker_loop,
            name="ManualRuntimeWorker",
            daemon=True,
        )
        self._runtime_worker.start()

    def _queue_runtime_event(self, name: str, payload: Optional[Dict[str, Any]] = None):
        self._runtime_event_queue.put((name, payload))

    def _get_runtime_snapshot(self) -> Dict[str, Any]:
        with self._runtime_lock:
            return dict(self._runtime_snapshot)

    def _runtime_worker_loop(self):
        period_s = max(0.02, float(self.update_period_ms) / 1000.0)

        while not self._runtime_stop_evt.is_set():
            loop_started = time.time()
            try:
                now = loop_started
                dt_real = None
                if self.rt.last_update_ts > 0.0:
                    dt_real = now - self.rt.last_update_ts
                    dt_real = max(0.02, min(dt_real, 0.20))
                self.rt.last_update_ts = now

                p = self._read_control_pressure_kpa()
                dut_eng = self._read_dut_eng()
                sig_min_live, sig_max_live = self._get_live_signal_bounds()
                span_pct = self._compute_span_percent(dut_eng)
                err_pct = self._compute_error_percent_fluke_style(p, dut_eng)
                p_dut_est = self._dut_est_pressure_kpa(
                    x_meas=dut_eng,
                    x_min=sig_min_live,
                    x_max=sig_max_live,
                    p_min=self.cfg.p_min_kpa,
                    p_max=self.cfg.p_max_kpa,
                )

                u_text = "u=0.000"
                pmax_seg = float(self.cfg.p_max_seguridad_kpa)
                if p >= pmax_seg:
                    self._safe_outputs(valve_open=False)
                    u_text = "u=SAFE"
                    if not self._runtime_overpressure_latched:
                        self._runtime_overpressure_latched = True
                        self._queue_runtime_event("EV_OVERPRESSURE", {"p_kpa": p, "pmax_kpa": pmax_seg})
                else:
                    self._runtime_overpressure_latched = False
                    if self.rt.running:
                        if self._update_manual_static_hold(now_ts=now, p_kpa=p):
                            u_text = "u=HOLD"
                        else:
                            self.set_valve(True)
                            self.set_relay(True)
                            u_cmd = self.pi_worker.step_now(
                                sp_kpa=float(self.cfg.sp_kpa),
                                p_kpa=float(p),
                                dt=dt_real,
                            )
                            self.set_pump(float(u_cmd))
                            u_text = f"u={float(u_cmd):.3f}"

                        self._update_live_plot(now_ts=now, p_pat_kpa=p, p_dut_est_kpa=p_dut_est)
                    else:
                        u_text = "u=0.000"

                with self._runtime_lock:
                    self._runtime_snapshot = {
                        "p_kpa": float(p),
                        "dut_p_kpa": float(p_dut_est),
                        "dut_eng": float(dut_eng),
                        "span_pct": float(span_pct),
                        "err_pct": float(err_pct),
                        "dut_mode": str(self.cfg.dut_mode),
                        "u_text": u_text,
                    }
                self._runtime_fault_latched = False
            except Exception as e:
                self._safe_outputs(valve_open=True)
                with self._runtime_lock:
                    self._runtime_snapshot["u_text"] = "u=FAIL"
                if not self._runtime_fault_latched:
                    self._runtime_fault_latched = True
                    self._queue_runtime_event("EV_SENSOR_FAIL_CRITICAL", {"error": str(e)})

            sleep_s = period_s - (time.time() - loop_started)
            if sleep_s > 0.0:
                self._runtime_stop_evt.wait(timeout=sleep_s)

    def _schedule_live_plot_poll(self):
        if self._live_plot_after_id is not None:
            return
        poll_ms = max(80, int(round(self._LIVE_PLOT_MIN_REDRAW_S * 1000.0)))
        self._live_plot_after_id = self.after(poll_ms, self._poll_live_plot_queue)

    def _poll_live_plot_queue(self):
        self._live_plot_after_id = None
        latest = None
        try:
            while True:
                latest = self._live_plot_queue.get_nowait()
        except Empty:
            pass

        if latest is not None:
            self._apply_live_plot_data(*latest)

        if self.winfo_exists():
            self._schedule_live_plot_poll()

    def _apply_live_plot_data(self, x, y_pat, y_dut):
        if self._ax_live is None or self._canvas_live is None:
            return

        self._line_live_pat.set_data(x, y_pat)
        self._line_live_dut.set_data(x, y_dut)

        if x:
            x_end = float(x[-1])
            x_start = max(0.0, x_end - float(self._LIVE_PLOT_WINDOW_S))
            if (x_end - x_start) < 1.0:
                x_end = x_start + 1.0
            self._ax_live.set_xlim(x_start, x_end)

            y_all = list(y_pat) + list(y_dut)
            y_min = min(y_all)
            y_max = max(y_all)
            if abs(y_max - y_min) < 1e-6:
                pad = max(1.0, abs(y_max) * 0.05 + 0.5)
            else:
                pad = max(0.5, (y_max - y_min) * 0.10)
            self._ax_live.set_ylim(y_min - pad, y_max + pad)
        else:
            self._ax_live.set_xlim(0.0, self._LIVE_PLOT_WINDOW_S)
            self._ax_live.set_ylim(0.0, 1.0)

        self._canvas_live.draw_idle()

    def _update_live_plot(self, now_ts: float, p_pat_kpa: float, p_dut_est_kpa: float):
        try:
            with self._live_plot_lock:
                if self._live_plot_t0 is None:
                    self._live_plot_t0 = now_ts
                t_rel = now_ts - self._live_plot_t0
                self._live_plot_t.append(float(t_rel))
                self._live_plot_p_pat.append(float(p_pat_kpa))
                self._live_plot_p_dut.append(float(p_dut_est_kpa))

                if (now_ts - self._live_plot_last_draw_ts) < self._LIVE_PLOT_MIN_REDRAW_S:
                    return

                x = list(self._live_plot_t)
                y_pat = list(self._live_plot_p_pat)
                y_dut = list(self._live_plot_p_dut)
                self._live_plot_last_draw_ts = now_ts

            self._live_plot_queue.put((x, y_pat, y_dut))
        except Exception:
            # Fallo de cola/render no debe tumbar el ciclo de adquisicion.
            pass

    def _reset_live_plot(self):
        with self._live_plot_lock:
            self._live_plot_t0 = None
            self._live_plot_last_draw_ts = 0.0
            self._live_plot_t.clear()
            self._live_plot_p_pat.clear()
            self._live_plot_p_dut.clear()
        self._live_plot_queue.put(([], [], []))

    # -------------------------
    # Loop
    # -------------------------
    def _tick(self):
        try:
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

            snapshot = self._get_runtime_snapshot()
            p = float(snapshot.get("p_kpa", 0.0))
            dut_p_kpa = float(snapshot.get("dut_p_kpa", 0.0))
            dut_eng = float(snapshot.get("dut_eng", 0.0))
            dut_mode = str(snapshot.get("dut_mode", self.cfg.dut_mode))
            self.var_p_source.set(f"{p:,.2f} kPa".replace(",", ""))
            self.var_dut_pressure.set(f"{dut_p_kpa:,.2f} kPa".replace(",", ""))
            if dut_mode == "A0":
                self.var_sig.set(f"{dut_eng:,.3f} V".replace(",", ""))
            else:
                self.var_sig.set(f"{dut_eng:,.3f} mA".replace(",", ""))

            span_pct = float(snapshot.get("span_pct", 0.0))
            err_pct = float(snapshot.get("err_pct", 0.0))
            self.var_span.set(f"{span_pct:,.2f} %".replace(",", ""))
            self.var_err.set(f"{err_pct:+,.2f} %".replace(",", ""))
            self.var_pwm.set(str(snapshot.get("u_text", "u=0.000")))

            try:
                while True:
                    name, payload = self._runtime_event_queue.get_nowait()
                    if name in ("EV_OVERPRESSURE", "EV_SENSOR_FAIL_CRITICAL"):
                        self._apply_state_config()
                    self.request_event(name, payload)
            except Empty:
                pass

            self._refresh_local_tx_buttons()

        except Exception as e:
            self._safe_outputs(valve_open=True)
            self.request_event("EV_SENSOR_FAIL_CRITICAL", {"error": str(e)})
            return
        finally:
            self.after(self.update_period_ms, self._tick)

    def _get_active_tx_channel(self) -> Optional[int]:
        try:
            top = self.winfo_toplevel()
            server = getattr(top, "telemetry_server", None)
            getter = getattr(server, "get_active_channel", None)
            if not callable(getter):
                return None
            ch = getter()
            if ch in (0, 1, 2):
                return int(ch)
        except Exception:
            return None
        return None

    def _schedule_tx_refresh(self) -> None:
        def _refresh():
            try:
                ch = self._get_active_tx_channel()
                if ch is not None:
                    # Lectura ligera para mantener fresco snapshot del canal transmitido.
                    # `read_vadc` ya actualiza el snapshot en HW.
                    _ = float(self.read_vadc(int(ch)))
            except Exception:
                pass
            finally:
                try:
                    self._refresh_local_tx_buttons()
                except Exception:
                    pass
                if self.winfo_exists():
                    self._tx_refresh_after_id = self.after(self._tx_refresh_period_ms, _refresh)

        self._tx_refresh_after_id = self.after(self._tx_refresh_period_ms, _refresh)

    # -------------------------
    # Config desde UI
    # -------------------------
    def _pull_config_from_ui(self):
        self.cfg.dut_mode = self.var_mode.get().strip()
        self.cfg.sp_unit = self.var_sp_unit.get().strip() or "kPa"

        def f(var: tk.StringVar, default: float) -> float:
            try:
                return float(var.get().strip().replace(",", "."))
            except:
                return default

        self.cfg.sp_kpa = self._parse_display_pressure_kpa(self.var_sp.get(), "SP")
        self.cfg.p_min_kpa = self._parse_display_pressure_kpa(self.var_pmin.get(), "P mÃ­n")
        self.cfg.p_max_kpa = self._parse_display_pressure_kpa(self.var_pmax.get(), "P mÃ¡x")
        self.cfg.sig_min = f(self.var_sigmin, self.cfg.sig_min)
        self.cfg.sig_max = f(self.var_sigmax, self.cfg.sig_max)
        self.cfg.p_max_seguridad_kpa = self._parse_display_pressure_kpa(
            self.var_pmaxseg.get(),
            "P seg",
            min_kpa=self._PRESSURE_MIN_KPA,
            max_kpa=self._PRESSURE_SAFETY_MAX_KPA,
        )
        self._sync_pressure_display_from_kpa()

    def _validate_config(self):
        if not (self._PRESSURE_MIN_KPA <= self.cfg.sp_kpa <= self._PRESSURE_MAX_KPA):
            raise ValueError("SP fuera de rango fÃ­sico (0-200 kPa).")
        if not (self._PRESSURE_MIN_KPA <= self.cfg.p_min_kpa <= self._PRESSURE_MAX_KPA):
            raise ValueError("P mÃ­n fuera de rango fÃ­sico (0-200 kPa).")
        if not (self._PRESSURE_MIN_KPA <= self.cfg.p_max_kpa <= self._PRESSURE_MAX_KPA):
            raise ValueError("P mÃ¡x fuera de rango fÃ­sico (0-200 kPa).")
        if self.cfg.p_max_kpa <= self.cfg.p_min_kpa:
            raise ValueError("PresiÃ³n mÃ¡x debe ser mayor que presiÃ³n mÃ­n.")
        if self.cfg.sig_max <= self.cfg.sig_min:
            raise ValueError("SeÃ±al mÃ¡x debe ser mayor que seÃ±al mÃ­n.")

    # -------------------------
    # Seguridad actuadores
    # -------------------------
    def _safe_outputs(self, valve_open: bool = True):
        try:
            self.set_pump(config.BOMBA_U_OFF if hasattr(config, "BOMBA_U_OFF") else 1.0)
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

    def _effective_u_bounds(self, u_min: float, u_max: float) -> tuple[float, float]:
        u_min_eff = max(0.0, min(float(u_min), 1.0))
        u_max_eff = max(0.0, min(float(u_max), 1.0))
        if bool(getattr(config, "BOMBA_ACTIVE_LOW", False)):
            pwm_hw_min = max(0.0, min(float(getattr(config, "PWM_HW_MIN_HOLD", 0.20)), 1.0))
            u_max_eff = min(u_max_eff, 1.0 - pwm_hw_min)
        if u_max_eff < u_min_eff:
            u_max_eff = u_min_eff
        return u_min_eff, u_max_eff

    def destroy(self):
        self._close_settings_window()
        self._close_calibration_2pt_window()
        try:
            if self._tx_refresh_after_id is not None:
                self.after_cancel(self._tx_refresh_after_id)
                self._tx_refresh_after_id = None
        except Exception:
            pass
        try:
            if self._live_plot_after_id is not None:
                self.after_cancel(self._live_plot_after_id)
                self._live_plot_after_id = None
        except Exception:
            pass
        try:
            self._runtime_stop_evt.set()
            worker = self._runtime_worker
            if worker is not None:
                worker.join(timeout=1.0)
            self._runtime_worker = None
        except Exception:
            pass
        try:
            self.pi_worker.stop()
        except Exception:
            pass
        super().destroy()




