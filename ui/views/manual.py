# mode_manual.py
# -*- coding: utf-8 -*-

import time
import tkinter as tk
from tkinter import ttk, messagebox, font as tkFont
from dataclasses import dataclass
from collections import deque
from typing import Callable, Optional, Dict, Any, TYPE_CHECKING

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from config import hardware as config
from core.control import PIController, PIConfig, PIWorker
from core.calibration import two_point_cal, save_calibration
if TYPE_CHECKING:
    from ui.views.pwm_log_window import PwmLogWindow


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
    _LIVE_PLOT_MAX_POINTS = 1200
    _LIVE_PLOT_MIN_REDRAW_S = 0.10

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
        get_pump_freq_hz: Optional[Callable[[], float]] = None,
        set_pump_freq_hz: Optional[Callable[[float], float]] = None,
        update_period_ms: int = 100,
    ):
        super().__init__(master)
        self.read_vadc = read_vadc
        self.read_vadc_live = read_vadc_live
        self.set_pump = set_pump
        self.get_pump_freq_hz = get_pump_freq_hz
        self.set_pump_freq_hz = set_pump_freq_hz
        self.set_relay = set_relay
        self.set_valve = set_valve
        self.request_event = request_event
        self.update_period_ms = update_period_ms
        self._pwm_log_active = False
        self._pwm_log_win: Optional[Any] = None
        self._tx_refresh_after_id: Optional[str] = None
        self._tx_refresh_period_ms = max(
            20,
            int(round(float(getattr(config, "TELEMETRY_FORCE_REFRESH_S", 0.05)) * 1000.0)),
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
        self._live_plot_t0: Optional[float] = None
        self._live_plot_last_draw_ts: float = 0.0
        self._live_plot_t = deque(maxlen=self._LIVE_PLOT_MAX_POINTS)
        self._live_plot_p_pat = deque(maxlen=self._LIVE_PLOT_MAX_POINTS)
        self._live_plot_p_dut = deque(maxlen=self._LIVE_PLOT_MAX_POINTS)

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

        # Lecturas en vivo
        self.var_p_source = tk.StringVar(value="0.00 kPa")
        self.var_sig = tk.StringVar(value="0.000 mA")
        self.var_span = tk.StringVar(value="0.0 %")
        self.var_err = tk.StringVar(value="0.0 %")
        self.var_pwm = tk.StringVar(value="u=0.000")
        self.var_temp = tk.StringVar(value="Temp: --.- C")

        self._build_ui_compact()
        self._apply_state_config()

        self._safe_outputs()
        self.after(self.update_period_ms, self._tick)
        self._schedule_tx_refresh()

    # -------------------------
    # UI compacta (SIN scroll)
    # -------------------------
    def _build_ui_compact(self):
        # Grid principal: 2 columnas
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1, uniform="col")
        self.grid_columnconfigure(1, weight=1, uniform="col")

        # TÃ­tulo arriba (ocupa 2 columnas)
        header = ttk.Frame(self)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 4))
        header.grid_columnconfigure(0, weight=1)

        title = ttk.Label(header, text="MODO MANUAL", font=("Arial", 15, "bold"))
        title.grid(row=0, column=0, sticky="w")

        ttk.Label(header, textvariable=self.var_temp, font=("Arial", 11, "bold")).grid(row=0, column=1, sticky="e")

        # ===== Columna izquierda: CONFIG =====
        frm_cfg = ttk.LabelFrame(self, text="ConfiguraciÃ³n")
        frm_cfg.grid(row=1, column=0, sticky="nsew", padx=(10, 6), pady=(4, 8))
        frm_cfg.grid_columnconfigure(0, weight=1)
        self.frm_cfg = frm_cfg

        # DUT + Rangos en una fila (2 subcolumnas dentro)
        top_cfg = ttk.Frame(frm_cfg)
        top_cfg.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 4))
        top_cfg.grid_columnconfigure(0, weight=1)
        top_cfg.grid_columnconfigure(1, weight=1)

        # DUT
        mode_box = ttk.LabelFrame(top_cfg, text="DUT")
        mode_box.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=0)

        rb_a1 = ttk.Radiobutton(
            mode_box, text="Transmisor de presion P/I", value="A1",
            variable=self.var_mode, command=self._on_mode_changed
        )
        rb_a0 = ttk.Radiobutton(
            mode_box, text="Transmisor de presion P/V", value="A0",
            variable=self.var_mode, command=self._on_mode_changed
        )
        rb_a1.grid(row=0, column=0, sticky="w", padx=8, pady=(3, 1))
        rb_a0.grid(row=1, column=0, sticky="w", padx=8, pady=(2, 6))

        # Rangos
        rng_box = ttk.LabelFrame(top_cfg, text="Rangos")
        rng_box.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=0)
        rng_box.grid_columnconfigure(1, weight=1)

        # Hacemos 2 columnas compactas
        ttk.Label(rng_box, text="P mÃ­n").grid(row=0, column=0, sticky="w", padx=6, pady=(4, 2))
        self.btn_pmin = ttk.Button(rng_box, text=f"[{self.var_pmin.get()}]", command=self._open_edit_dialog_pmin)
        self.btn_pmin.grid(row=0, column=1, sticky="w", padx=6, pady=(4, 2))

        ttk.Label(rng_box, text="P mÃ¡x").grid(row=1, column=0, sticky="w", padx=6, pady=2)
        self.btn_pmax = ttk.Button(rng_box, text=f"[{self.var_pmax.get()}]", command=self._open_edit_dialog_pmax)
        self.btn_pmax.grid(row=1, column=1, sticky="w", padx=6, pady=2)

        self.lbl_sigmin = ttk.Label(rng_box, text="I mÃ­n")
        self.lbl_sigmin.grid(row=2, column=0, sticky="w", padx=6, pady=2)
        self.btn_sigmin = ttk.Button(rng_box, text=f"[{self.var_sigmin.get()}]", command=lambda: self._open_edit_dialog(self.var_sigmin, "SeÃ±al mÃ­n", 0, 100, self.btn_sigmin))
        self.btn_sigmin.grid(row=2, column=1, sticky="w", padx=6, pady=2)

        self.lbl_sigmax = ttk.Label(rng_box, text="I mÃ¡x")
        self.lbl_sigmax.grid(row=3, column=0, sticky="w", padx=6, pady=2)
        self.btn_sigmax = ttk.Button(rng_box, text=f"[{self.var_sigmax.get()}]", command=lambda: self._open_edit_dialog(self.var_sigmax, "SeÃ±al mÃ¡x", 0, 100, self.btn_sigmax))
        self.btn_sigmax.grid(row=3, column=1, sticky="w", padx=6, pady=2)

        ttk.Label(rng_box, text="P seg").grid(row=4, column=0, sticky="w", padx=6, pady=(2, 6))
        self.btn_pmaxseg = ttk.Button(rng_box, text=f"[{self.var_pmaxseg.get()}]", command=lambda: self._open_edit_dialog(self.var_pmaxseg, "P seguridad (kPa)", 0, 500, self.btn_pmaxseg))
        self.btn_pmaxseg.grid(row=4, column=1, sticky="w", padx=6, pady=(2, 6))

        # Control (SP + botÃ³n aplicar) compacto
        sp_box = ttk.LabelFrame(frm_cfg, text="Control")
        sp_box.grid(row=1, column=0, sticky="ew", padx=8, pady=6)
        sp_box.grid_columnconfigure(1, weight=1)

        ttk.Label(sp_box, textvariable=self.var_sp_label).grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.btn_sp = ttk.Button(sp_box, text=f"[{self.var_sp.get()}]", command=lambda: self._open_edit_dialog_sp())
        self.btn_sp.grid(row=0, column=1, sticky="w", padx=6, pady=6)
        self.btn_sp_unit = ttk.Button(sp_box, text=self.var_sp_unit.get(), width=10, command=self._open_sp_unit_selector)
        self.btn_sp_unit.grid(row=0, column=2, sticky="w", padx=(0, 6), pady=6)

        # Botones config (fila compacta)
        btns = ttk.Frame(frm_cfg)
        btns.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 8))
        btns.grid_columnconfigure(0, weight=1)
        btns.grid_columnconfigure(1, weight=1)
        btns.grid_columnconfigure(2, weight=1)

        self.btn_zero = ttk.Button(btns, text="P=0", command=self._do_tare)
        self.btn_start = ttk.Button(btns, text="START", command=self._start)
        self.btn_stop_cfg = ttk.Button(btns, text="STOP", command=self._stop_and_back)

        self.btn_zero.grid(row=0, column=0, sticky="ew", padx=4)
        self.btn_start.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_stop_cfg.grid(row=0, column=2, sticky="ew", padx=4)

        # Herramientas
        tools = ttk.Frame(frm_cfg)
        tools.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))
        tools.grid_columnconfigure(0, weight=1)
        tools.grid_columnconfigure(1, weight=1)
        tools.grid_columnconfigure(2, weight=1)

        self.btn_cal_2pt = ttk.Button(
            tools, text="Calibracion 2 puntos (A0/A1/A2)", command=self._open_calibration_2pt
        )
        self.btn_fft = ttk.Button(
            tools, text="FFT / Ruido", command=self._open_fft_window
        )
        self.btn_pwm_log = ttk.Button(
            tools, text="LOG PWM -> CSV", command=self._open_pwm_log_window
        )

        self.btn_cal_2pt.grid(row=0, column=0, sticky="ew", padx=4)
        self.btn_fft.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_pwm_log.grid(row=0, column=2, sticky="ew", padx=4)

        # ===== Columna derecha: LIVE =====
        frm_live = ttk.LabelFrame(self, text="Lecturas en vivo")
        frm_live.grid(row=1, column=1, sticky="nsew", padx=(6, 10), pady=(4, 8))
        frm_live.grid_columnconfigure(1, weight=1)
        frm_live.grid_rowconfigure(5, weight=1)
        self.frm_live = frm_live

        # Letras un pelÃ­n mÃ¡s pequeÃ±as para que quepa
        big = ("Arial", 10, "bold")
        normal = ("Arial", 9)

        ttk.Label(frm_live, text="PRESIÃ“N:", font=normal).grid(row=0, column=0, sticky="w", padx=8, pady=(3, 1))
        ttk.Label(frm_live, textvariable=self.var_p_source, font=big).grid(row=0, column=1, sticky="w", padx=8, pady=(3, 1))

        ttk.Label(frm_live, text="DUT:", font=normal).grid(row=1, column=0, sticky="w", padx=8, pady=1)
        ttk.Label(frm_live, textvariable=self.var_sig, font=big).grid(row=1, column=1, sticky="w", padx=8, pady=1)

        ttk.Label(frm_live, text="%SPAN:", font=normal).grid(row=2, column=0, sticky="w", padx=8, pady=0)
        ttk.Label(frm_live, textvariable=self.var_span, font=normal).grid(row=2, column=1, sticky="w", padx=8, pady=0)

        ttk.Label(frm_live, text="%ERROR:", font=normal).grid(row=3, column=0, sticky="w", padx=8, pady=0)
        ttk.Label(frm_live, textvariable=self.var_err, font=normal).grid(row=3, column=1, sticky="w", padx=8, pady=0)

        ttk.Label(frm_live, text="PWM:", font=normal).grid(row=4, column=0, sticky="w", padx=8, pady=(0, 2))
        ttk.Label(frm_live, textvariable=self.var_pwm, font=normal).grid(row=4, column=1, sticky="w", padx=8, pady=(0, 2))

        self._on_mode_changed()
        self._update_sp_unit_ui()

    def _update_sp_unit_ui(self):
        unit = self.var_sp_unit.get().strip() or "kPa"
        if unit not in self._UNIT_TO_KPA:
            unit = "kPa"
        self.var_sp_unit.set(unit)
        self.cfg.sp_unit = unit
        self.var_sp_label.set(f"SP ({unit}):")
        if hasattr(self, "btn_sp_unit"):
            self.btn_sp_unit.configure(text=unit)
        self._sync_pressure_display_from_kpa()

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

    def _parse_display_pressure_kpa(self, raw: str, field_name: str) -> float:
        try:
            display_value = float(raw.strip().replace(",", "."))
        except Exception:
            raise ValueError(f"{field_name}: valor invÃ¡lido.")

        value_kpa = self._pressure_display_to_kpa(display_value)
        if value_kpa < self._PRESSURE_MIN_KPA or value_kpa > self._PRESSURE_MAX_KPA:
            unit = self.var_sp_unit.get().strip() or "kPa"
            raise ValueError(
                f"{field_name}: fuera de rango fÃ­sico 0-200 kPa. "
                f"({display_value:.4f} {unit} = {value_kpa:.4f} kPa)"
            )
        return float(value_kpa)

    def _sync_pressure_display_from_kpa(self):
        self.var_sp.set(self._fmt_display_pressure(self._pressure_kpa_to_display(self.cfg.sp_kpa)))
        self.var_pmin.set(self._fmt_display_pressure(self._pressure_kpa_to_display(self.cfg.p_min_kpa)))
        self.var_pmax.set(self._fmt_display_pressure(self._pressure_kpa_to_display(self.cfg.p_max_kpa)))

        if hasattr(self, "btn_sp"):
            self.btn_sp.configure(text=f"[{self.var_sp.get()}]")
        if hasattr(self, "btn_pmin"):
            self.btn_pmin.configure(text=f"[{self.var_pmin.get()}]")
        if hasattr(self, "btn_pmax"):
            self.btn_pmax.configure(text=f"[{self.var_pmax.get()}]")

    # -------------------------
    # Estados internos
    # -------------------------
    def _apply_state_config(self):
        self.rt.running = False
        self.rt.target_reached = False
        self.pi_worker.reset()
        self.pi_worker.freeze()
        self.rt.last_update_ts = 0.0
        self._safe_outputs(valve_open=True)
        self._set_config_widgets_state(enabled=True)
        self.btn_stop_cfg.state(["disabled"])

    def _apply_state_run(self):
        self.rt.running = True
        self.rt.target_reached = False
        self._reset_live_plot()
        self.pi_worker.reset()
        self.pi_worker.unfreeze()
        self.rt.last_update_ts = 0.0
        self.set_valve(True)
        self.set_relay(True)
        self._set_config_widgets_state(enabled=False)
        self.btn_stop_cfg.state(["!disabled"])

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
            self.btn_start.state(["!disabled"])
            self.btn_zero.state(["!disabled"])
        else:
            self.btn_start.state(["disabled"])
            self.btn_zero.state(["!disabled"])

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

        ttk.Button(action_frm, text="âœ“ Guardar", command=on_save).pack(side="left", padx=2, pady=2, fill="both", expand=True)
        ttk.Button(action_frm, text="âœ• Cancelar", command=on_cancel).pack(side="left", padx=2, pady=2, fill="both", expand=True)

        entry.bind("<Return>", lambda e: on_save())
        entry.bind("<Escape>", lambda e: on_cancel())

        dialog.wait_window()

    # -------------------------
    # Calibracion 2 puntos (A0/A1)
    # -------------------------
    def _open_calibration_2pt(self):
        try:
            win = tk.Toplevel(self)
            win.title("Calibracion 2 puntos (A0/A1/A2)")
            win.resizable(False, False)
            try:
                win.attributes("-topmost", True)
            except tk.TclError:
                pass
            win.transient(self.winfo_toplevel())
            win.lift()
            win.focus_force()
            win.grab_set()

            container = ttk.Frame(win)
            container.grid(row=0, column=0, sticky="nsew")
            win.grid_rowconfigure(0, weight=1)
            win.grid_columnconfigure(0, weight=1)

            canvas = tk.Canvas(container, highlightthickness=0)
            vscroll = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=vscroll.set)
            canvas.grid(row=0, column=0, sticky="nsew")
            vscroll.grid(row=0, column=1, sticky="ns")
            container.grid_rowconfigure(0, weight=1)
            container.grid_columnconfigure(0, weight=1)

            frm = ttk.Frame(canvas, padding=12)
            canvas_window = canvas.create_window((0, 0), window=frm, anchor="nw")

            def _on_frame_configure(_event=None):
                canvas.configure(scrollregion=canvas.bbox("all"))

            def _on_canvas_configure(event):
                canvas.itemconfigure(canvas_window, width=event.width)

            frm.bind("<Configure>", _on_frame_configure)
            canvas.bind("<Configure>", _on_canvas_configure)

            var_chan = tk.StringVar(value="A0")
            var_x1 = tk.StringVar(value="--")
            var_x2 = tk.StringVar(value="--")
            var_y1 = tk.StringVar(value="0.000")
            var_y2 = tk.StringVar(value="0.000")
            var_m = tk.StringVar(value="--")
            var_b = tk.StringVar(value="--")
            var_units = tk.StringVar(value="V")
            var_a2_sp = tk.StringVar(value=f"{self.cfg.sp_kpa:.2f}")
            var_a2_p = tk.StringVar(value="0.00")
            var_a2_state = tk.StringVar(value="PI OFF")
            a2_pi_enabled = {"on": False}
            a2_last_sp = {"value": None}
            a2_pi_after = {"id": None}

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

            ttk.Label(frm, text="Canal", font=("Arial", 11, "bold")).grid(row=0, column=0, sticky="w", padx=6, pady=4)
            chan_box = ttk.Frame(frm)
            chan_box.grid(row=0, column=1, columnspan=2, sticky="w", padx=6, pady=4)
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
                    width=5,
                    height=2,
                    font=("Arial", 11, "bold"),
                    command=lambda m=mode_sel: _set_chan(m),
                )
                btn.pack(side="left", padx=3)
                chan_btns[mode_sel] = btn

            var_chan.trace_add("write", _refresh_chan_buttons)
            _refresh_chan_buttons()

            ttk.Label(frm, text="Punto 1 (y_real)", font=("Arial", 10, "bold")).grid(row=1, column=0, sticky="w", padx=6, pady=4)
            btn_y1 = ttk.Button(frm, text=f"[{var_y1.get()}]")
            btn_y1.grid(row=1, column=1, sticky="w", padx=6, pady=4)
            btn_y1.configure(
                command=lambda: self._open_edit_dialog(
                    var_y1,
                    f"Punto 1 (y_real) [{var_units.get()}]",
                    -1000.0,
                    1000.0,
                    btn_y1
                )
            )
            ttk.Label(frm, textvariable=var_units).grid(row=1, column=2, sticky="w")

            ttk.Button(frm, text="Capturar Punto 1 (x1=Vadc)", command=lambda: _capture_point(1)).grid(
                row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=4
            )
            ttk.Label(frm, text="x1 (Vadc):").grid(row=3, column=0, sticky="w", padx=6, pady=2)
            ttk.Label(frm, textvariable=var_x1).grid(row=3, column=1, sticky="w", padx=6, pady=2)

            ttk.Label(frm, text="Punto 2 (y_real)", font=("Arial", 10, "bold")).grid(row=4, column=0, sticky="w", padx=6, pady=4)
            btn_y2 = ttk.Button(frm, text=f"[{var_y2.get()}]")
            btn_y2.grid(row=4, column=1, sticky="w", padx=6, pady=4)
            btn_y2.configure(
                command=lambda: self._open_edit_dialog(
                    var_y2,
                    f"Punto 2 (y_real) [{var_units.get()}]",
                    -1000.0,
                    1000.0,
                    btn_y2
                )
            )
            ttk.Label(frm, textvariable=var_units).grid(row=4, column=2, sticky="w")

            ttk.Button(frm, text="Capturar Punto 2 (x2=Vadc)", command=lambda: _capture_point(2)).grid(
                row=5, column=0, columnspan=3, sticky="ew", padx=6, pady=4
            )
            ttk.Label(frm, text="x2 (Vadc):").grid(row=6, column=0, sticky="w", padx=6, pady=2)
            ttk.Label(frm, textvariable=var_x2).grid(row=6, column=1, sticky="w", padx=6, pady=2)

            # Control PI auxiliar para A2
            a2_box = ttk.LabelFrame(frm, text="Control PI (solo A2)")
            a2_box.grid(row=7, column=0, columnspan=3, sticky="ew", padx=6, pady=4)
            a2_box.grid_columnconfigure(1, weight=1)

            ttk.Label(a2_box, text="SP (kPa):").grid(row=0, column=0, sticky="w", padx=6, pady=3)
            ttk.Entry(a2_box, textvariable=var_a2_sp, width=10).grid(row=0, column=1, sticky="w", padx=6, pady=3)
            btn_a2_pi = ttk.Button(a2_box, text="PI ON")
            btn_a2_pi.grid(row=0, column=2, sticky="ew", padx=6, pady=3)
            ttk.Label(a2_box, text="P actual (kPa):").grid(row=1, column=0, sticky="w", padx=6, pady=3)
            ttk.Label(a2_box, textvariable=var_a2_p).grid(row=1, column=1, sticky="w", padx=6, pady=3)
            ttk.Label(a2_box, textvariable=var_a2_state).grid(row=1, column=2, sticky="w", padx=6, pady=3)

            ttk.Separator(frm).grid(row=8, column=0, columnspan=3, sticky="ew", pady=6)

            ttk.Label(frm, text="m:").grid(row=9, column=0, sticky="w", padx=6, pady=2)
            ttk.Label(frm, textvariable=var_m).grid(row=9, column=1, sticky="w", padx=6, pady=2)
            ttk.Label(frm, text="b:").grid(row=10, column=0, sticky="w", padx=6, pady=2)
            ttk.Label(frm, textvariable=var_b).grid(row=10, column=1, sticky="w", padx=6, pady=2)

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

            ttk.Button(frm, text="Calcular y Guardar", command=_calc_and_save).grid(
                row=11, column=0, columnspan=3, sticky="ew", padx=6, pady=6
            )

            def _a2_pi_disable():
                a2_pi_enabled["on"] = False
                a2_last_sp["value"] = None
                try:
                    self.pi_worker.freeze()
                except Exception:
                    pass
                try:
                    self.set_pump(config.BOMBA_U_OFF if hasattr(config, "BOMBA_U_OFF") else 1.0)
                    self.set_relay(False)
                    self.set_valve(False)
                except Exception:
                    pass
                btn_a2_pi.configure(text="PI ON")
                var_a2_state.set("PI OFF")

            def _a2_pi_enable():
                if var_chan.get().strip().upper() != "A2":
                    messagebox.showwarning("Calibracion", "El PI auxiliar solo se habilita en A2.", parent=win)
                    return
                try:
                    sp = float(var_a2_sp.get().strip().replace(",", "."))
                except Exception:
                    messagebox.showerror("Calibracion", "SP invalido para PI.", parent=win)
                    return
                try:
                    p_now = float(self._read_control_pressure_kpa())
                except Exception:
                    p_now = 0.0
                self.rt.running = False
                self.pi_worker.reset()
                self.pi_worker.retarget(sp_kpa=float(sp), p_kpa=float(p_now))
                self.pi_worker.unfreeze()
                a2_pi_enabled["on"] = True
                a2_last_sp["value"] = float(sp)
                btn_a2_pi.configure(text="PI OFF")
                var_a2_state.set(f"PI ON | SP={sp:.2f} kPa")

            def _toggle_a2_pi():
                if a2_pi_enabled["on"]:
                    _a2_pi_disable()
                else:
                    _a2_pi_enable()

            btn_a2_pi.configure(command=_toggle_a2_pi)

            def _a2_pi_tick():
                try:
                    p_now = float(self._read_control_pressure_kpa())
                    var_a2_p.set(f"{p_now:.2f}")
                    if a2_pi_enabled["on"]:
                        if var_chan.get().strip().upper() != "A2":
                            _a2_pi_disable()
                        else:
                            try:
                                sp = float(var_a2_sp.get().strip().replace(",", "."))
                            except Exception:
                                sp = float(self.cfg.sp_kpa)
                            if a2_last_sp["value"] is None or abs(float(sp) - float(a2_last_sp["value"])) > 1e-9:
                                self.pi_worker.retarget(sp_kpa=float(sp), p_kpa=float(p_now))
                                a2_last_sp["value"] = float(sp)
                            self.set_valve(False)
                            self.set_relay(True)
                            self.pi_worker.set_inputs(sp_kpa=sp, p_kpa=p_now, dt=float(config.PI_CFG.dt))
                            u_cmd = self.pi_worker.get_output()
                            self.set_pump(u_cmd)
                            var_a2_state.set(f"PI ON | u={u_cmd:.3f}")
                except Exception as e:
                    var_a2_state.set(f"PI ERR: {e}")
                finally:
                    if win.winfo_exists():
                        a2_pi_after["id"] = win.after(120, _a2_pi_tick)

            def _on_chan_change(*_):
                _update_units()
                if var_chan.get().strip().upper() != "A2" and a2_pi_enabled["on"]:
                    _a2_pi_disable()

            var_chan.trace_add("write", _on_chan_change)
            _update_units()

            def _on_close():
                try:
                    if a2_pi_after["id"] is not None:
                        win.after_cancel(a2_pi_after["id"])
                except Exception:
                    pass
                try:
                    canvas.unbind_all("<MouseWheel>")
                except Exception:
                    pass
                _a2_pi_disable()
                win.destroy()

            def _on_mousewheel(event):
                try:
                    delta = -1 * int(event.delta / 120)
                    canvas.yview_scroll(delta, "units")
                except Exception:
                    pass

            canvas.bind_all("<MouseWheel>", _on_mousewheel)

            win.protocol("WM_DELETE_WINDOW", _on_close)
            _a2_pi_tick()
        except Exception as e:
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

        ttk.Button(action_frm, text="âœ“ Guardar", command=on_save).pack(side="left", padx=2, pady=2, fill="both", expand=True)
        ttk.Button(action_frm, text="âœ• Cancelar", command=on_cancel).pack(side="left", padx=2, pady=2, fill="both", expand=True)

        entry.bind("<Return>", lambda e: on_save())
        entry.bind("<Escape>", lambda e: on_cancel())

        dialog.wait_window()

    def _open_edit_dialog_pmin(self):
        self._open_edit_dialog_pressure_bound("p_min_kpa", "P mÃ­n")

    def _open_edit_dialog_pmax(self):
        self._open_edit_dialog_pressure_bound("p_max_kpa", "P mÃ¡x")

    def _open_edit_dialog_pressure_bound(self, attr_name: str, field_label: str):
        unit = self.var_sp_unit.get().strip() or "kPa"
        min_val = self._pressure_kpa_to_display(self._PRESSURE_MIN_KPA)
        max_val = self._pressure_kpa_to_display(self._PRESSURE_MAX_KPA)
        current_kpa = float(getattr(self.cfg, attr_name))
        current_disp = self._fmt_display_pressure(self._pressure_kpa_to_display(current_kpa))

        if attr_name == "p_min_kpa":
            button = self.btn_pmin
        elif attr_name == "p_max_kpa":
            button = self.btn_pmax
        else:
            raise ValueError("Campo de presiÃ³n invÃ¡lido.")

        def _on_save(raw_value: str):
            value_kpa = self._parse_display_pressure_kpa(raw_value, field_label)
            setattr(self.cfg, attr_name, value_kpa)
            self._sync_pressure_display_from_kpa()
            button.configure(text=f"[{self.var_pmin.get() if attr_name == 'p_min_kpa' else self.var_pmax.get()}]")

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

        ttk.Button(action_frm, text="âœ“ Guardar", command=save_and_close).pack(side="left", padx=2, pady=2, fill="both", expand=True)
        ttk.Button(action_frm, text="âœ• Cancelar", command=on_cancel).pack(side="left", padx=2, pady=2, fill="both", expand=True)

        entry.bind("<Return>", lambda e: save_and_close())
        entry.bind("<Escape>", lambda e: on_cancel())

        dialog.wait_window()

    def _open_sp_unit_selector(self):
        dialog = tk.Toplevel(self)
        dialog.title("Seleccionar unidad SP")
        dialog.geometry("280x360")
        dialog.resizable(False, False)
        dialog.transient(self.winfo_toplevel())
        dialog.focus_force()
        dialog.grab_set()

        frm = ttk.Frame(dialog, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Unidad de SP", font=("Arial", 11, "bold")).pack(anchor="w", pady=(0, 6))

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
            self.lbl_sigmin.configure(text="V mÃ­n")
            self.lbl_sigmax.configure(text="V mÃ¡x")
            # Si viene del rango tÃ­pico A1 (4-20), conmutar a 0-10 V.
            if _is_close_pair(sig_min, sig_max, self._A1_SIG_MIN_DEFAULT, self._A1_SIG_MAX_DEFAULT):
                sig_min = self._A0_SIG_MIN_DEFAULT
                sig_max = self._A0_SIG_MAX_DEFAULT
                self.var_sigmin.set(f"{sig_min:.3f}")
                self.var_sigmax.set(f"{sig_max:.3f}")
        else:
            self.lbl_sigmin.configure(text="I mÃ­n")
            self.lbl_sigmax.configure(text="I mÃ¡x")
            # Si viene del rango tÃ­pico A0 (0-10), conmutar a 4-20 mA.
            if _is_close_pair(sig_min, sig_max, self._A0_SIG_MIN_DEFAULT, self._A0_SIG_MAX_DEFAULT):
                sig_min = self._A1_SIG_MIN_DEFAULT
                sig_max = self._A1_SIG_MAX_DEFAULT
                self.var_sigmin.set(f"{sig_min:.3f}")
                self.var_sigmax.set(f"{sig_max:.3f}")

        # Sincronizar cÃ¡lculo live sin esperar START.
        self.cfg.sig_min = float(sig_min)
        self.cfg.sig_max = float(sig_max)

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
            try:
                p_now = self._read_control_pressure_kpa()
            except Exception:
                p_now = 0.0
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
            u_cmd = self.pi_worker.step_now(
                sp_kpa=float(self.cfg.sp_kpa),
                p_kpa=float(p_now),
                dt=float(config.PI_CFG.dt),
            )
            self.set_pump(float(u_cmd))
            self.var_pwm.set(f"u={float(u_cmd):.3f}")

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
        def _parse_sig(var: tk.StringVar, default: float) -> float:
            try:
                return float(var.get().strip().replace(",", "."))
            except Exception:
                return float(default)
        return _parse_sig(self.var_sigmin, self.cfg.sig_min), _parse_sig(self.var_sigmax, self.cfg.sig_max)

    @staticmethod
    def _dut_est_pressure_kpa(x_meas: float, x_min: float, x_max: float, p_min: float, p_max: float) -> float:
        den = x_max - x_min
        if abs(den) < 1e-9:
            return float(p_min)
        return float(p_min + (x_meas - x_min) * (p_max - p_min) / den)

    def _update_live_plot(self, now_ts: float, p_pat_kpa: float, p_dut_est_kpa: float):
        try:
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
            if not x:
                return

            self._live_plot_last_draw_ts = now_ts
        except Exception:
            # Fallo de render no debe tumbar el ciclo de adquisicion.
            pass

    def _reset_live_plot(self):
        self._live_plot_t0 = None
        self._live_plot_last_draw_ts = 0.0
        self._live_plot_t.clear()
        self._live_plot_p_pat.clear()
        self._live_plot_p_dut.clear()

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
                self.var_temp.set(f"Temp: {temp_c:.1f} C")
            except Exception:
                self.var_temp.set("Temp: --.- C")

            now = time.time()
            dt_real = None
            if self.rt.last_update_ts > 0.0:
                dt_real = now - self.rt.last_update_ts
                dt_real = max(0.02, min(dt_real, 0.20))
            self.rt.last_update_ts = now

            p = self._read_control_pressure_kpa()

            dut_eng = self._read_dut_eng()

            self.var_p_source.set(f"{p:,.2f} kPa".replace(",", ""))
            if self.cfg.dut_mode == "A0":
                self.var_sig.set(f"{dut_eng:,.3f} V".replace(",", ""))
            else:
                self.var_sig.set(f"{dut_eng:,.3f} mA".replace(",", ""))

            span_pct = self._compute_span_percent(dut_eng)
            err_pct = self._compute_error_percent_fluke_style(p, dut_eng)

            self.var_span.set(f"{span_pct:,.2f} %".replace(",", ""))
            self.var_err.set(f"{err_pct:+,.2f} %".replace(",", ""))

            pmax_seg = self.cfg.p_max_seguridad_kpa
            if p >= pmax_seg:
                self._safe_outputs(valve_open=False)
                self.request_event("EV_OVERPRESSURE", {"p_kpa": p, "pmax_kpa": pmax_seg})
                return

            if self._pwm_log_active:
                self.var_pwm.set("u=LOG")
            elif self.rt.running:
                sig_min_live, sig_max_live = self._get_live_signal_bounds()
                p_dut_est = self._dut_est_pressure_kpa(
                    x_meas=dut_eng,
                    x_min=sig_min_live,
                    x_max=sig_max_live,
                    p_min=self.cfg.p_min_kpa,
                    p_max=self.cfg.p_max_kpa,
                )
                self._update_live_plot(now_ts=now, p_pat_kpa=p, p_dut_est_kpa=p_dut_est)

                sp = float(self.cfg.sp_kpa)
                sp_ctrl = sp

                self.set_valve(True)
                self.set_relay(True)
                u_cmd = self.pi_worker.step_now(sp_kpa=sp_ctrl, p_kpa=p, dt=dt_real)
                self.set_pump(u_cmd)
                self.var_pwm.set(f"u={u_cmd:.3f}")
            else:
                self.var_pwm.set("u=0.000")

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
        self.cfg.p_max_seguridad_kpa = f(self.var_pmaxseg, self.cfg.p_max_seguridad_kpa)
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

    def _apply_u_cmd_for_log(self, u_cmd: float) -> None:
        u_cmd = max(0.0, min(float(u_cmd), 1.0))
        self.set_valve(True)
        self.set_relay(True)
        self.set_pump(u_cmd)

    def _safe_stop_for_log(self) -> None:
        self._safe_outputs(valve_open=True)

    def _on_pwm_log_start(self) -> None:
        self._pwm_log_active = True
        self.rt.running = False
        self.pi_worker.reset()
        self.pi_worker.freeze()
        self.btn_start.state(["disabled"])
        self.btn_pwm_log.state(["disabled"])

    def _on_pwm_log_end(self, _state: str) -> None:
        self._pwm_log_active = False
        self.pi_worker.unfreeze()
        self.btn_start.state(["!disabled"])
        self.btn_pwm_log.state(["!disabled"])

    def _open_pwm_log_window(self) -> None:
        if self._pwm_log_win is not None and self._pwm_log_win.winfo_exists():
            self._pwm_log_win.lift()
            self._pwm_log_win.focus_force()
            return

        try:
            from .pwm_log_window import PwmLogWindow
        except Exception as e:
            messagebox.showerror("LOG PWM", f"No se pudo abrir LOG PWM: {e}")
            return

        self._pwm_log_win = PwmLogWindow(
            self,
            read_pressure_kpa=self._read_control_pressure_kpa,
            apply_u_cmd=self._apply_u_cmd_for_log,
            get_pwm_freq_hz=self.get_pump_freq_hz,
            set_pwm_freq_hz=self.set_pump_freq_hz,
            safe_stop=self._safe_stop_for_log,
            on_start=self._on_pwm_log_start,
            on_end=self._on_pwm_log_end,
        )
        self._pwm_log_win.bind("<Destroy>", self._on_pwm_log_window_destroy, add="+")

    def _on_pwm_log_window_destroy(self, event) -> None:
        if self._pwm_log_win is None:
            return
        if event.widget is not self._pwm_log_win:
            return
        self._pwm_log_win = None
        if self._pwm_log_active:
            self._on_pwm_log_end("ABORT")

    def destroy(self):
        try:
            if self._tx_refresh_after_id is not None:
                self.after_cancel(self._tx_refresh_after_id)
                self._tx_refresh_after_id = None
        except Exception:
            pass
        try:
            if self._pwm_log_win is not None and self._pwm_log_win.winfo_exists():
                self._pwm_log_win.destroy()
        except Exception:
            pass
        try:
            self.pi_worker.stop()
        except Exception:
            pass
        super().destroy()




