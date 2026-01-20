
# mode_manual.py
# -*- coding: utf-8 -*-

import time
import tkinter as tk
from tkinter import ttk, messagebox
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any

import config
from control_pi import PIController, PIConfig


# =========================
# Utilidades de conversión
# =========================
def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def mpx_vadc_to_kpa(vadc: float) -> float:
    """Convierte VADC (ADS) -> presión kPa usando polinomio + 2PT si aplica."""
    p_raw = config.MPX_A2 * vadc * vadc + config.MPX_B2 * vadc + config.MPX_C2
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
    Convierte VADC (ADS) a ingeniería:
      - A0 -> Vin (V): Vin = gain*VADC + offset
      - A1 -> ImA (mA): ImA = gain*VADC + offset
    """
    if dut_mode == "A0":
        if config.USE_A0_CAL:
            return config.A0_VIN_GAIN * vadc + config.A0_VIN_OFFSET
        return vadc
    else:
        if config.USE_A1_CAL:
            return config.A1_IMA_GAIN * vadc + config.A1_IMA_OFFSET
        return vadc


# =========================
# Contexto manual (simple)
# =========================
@dataclass
class ManualConfig:
    sp_kpa: float = 60.0
    dut_mode: str = "A1"
    p_min_kpa: float = 0.0
    p_max_kpa: float = 200.0
    sig_min: float = 4.0
    sig_max: float = 20.0
    p_max_seguridad_kpa: float = config.P_MAX_SEGURIDAD_KPA


@dataclass
class ManualRuntime:
    running: bool = False
    p_zero_kpa: float = 0.0
    last_update_ts: float = 0.0


# =========================
# Frame GUI del modo manual
# =========================
class ManualView(ttk.Frame):
    def __init__(
        self,
        master,
        *,
        read_vadc: Callable[[int], float],
        set_pump: Callable[[float], None],
        set_relay: Callable[[bool], None],
        set_valve: Callable[[bool], None],
        request_event: Callable[[str, Optional[Dict[str, Any]]], None],
        update_period_ms: int = 100,
    ):
        super().__init__(master)

        self.read_vadc = read_vadc
        self.set_pump = set_pump
        self.set_relay = set_relay
        self.set_valve = set_valve
        self.request_event = request_event
        self.update_period_ms = update_period_ms

        # ========= SCROLL WRAPPER (SOLO UI) =========
        # Canvas + Scrollbar + inner frame (self.body)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")

        self.body = ttk.Frame(self.canvas)
        self._body_window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        # Ajustar scrollregion cuando cambie el tamaño del contenido
        self.body.bind("<Configure>", self._on_body_configure)
        # Ajustar ancho del frame interno al ancho del canvas (para que no “corte”)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # Scroll con rueda del mouse (y touchpad)
        self._bind_mousewheel(self.canvas)

        # ========= FIN SCROLL WRAPPER =========

        # PI único (sirve manual y auto)
        self.pi = PIController(PIConfig(
            kp=config.PI_CFG.kp,
            ki=config.PI_CFG.ki,
            dt=config.PI_CFG.dt,
            u_min=config.PI_CFG.u_min,
            u_max=config.PI_CFG.u_max,
            deadband_kpa=config.PI_CFG.deadband_kpa,
            u_ff=config.PI_CFG.u_ff,
            i_decay_in_deadband=0.97
        ))

        self.cfg = ManualConfig()
        self.rt = ManualRuntime()

        # Variables Tk
        self.var_sp = tk.StringVar(value=f"{self.cfg.sp_kpa:.2f}")
        self.var_pmin = tk.StringVar(value=f"{self.cfg.p_min_kpa:.2f}")
        self.var_pmax = tk.StringVar(value=f"{self.cfg.p_max_kpa:.2f}")
        self.var_sigmin = tk.StringVar(value=f"{self.cfg.sig_min:.3f}")
        self.var_sigmax = tk.StringVar(value=f"{self.cfg.sig_max:.3f}")
        self.var_pmaxseg = tk.StringVar(value=f"{self.cfg.p_max_seguridad_kpa:.1f}")
        self.var_mode = tk.StringVar(value=self.cfg.dut_mode)

        self.var_p_source = tk.StringVar(value="0.00 kPa")
        self.var_sig = tk.StringVar(value="0.000 mA")
        self.var_span = tk.StringVar(value="0.0 %")
        self.var_err = tk.StringVar(value="0.0 %")
        self.var_pwm = tk.StringVar(value="u=0.000")
        self.var_temp = tk.StringVar(value="T: --.- °C")

        # Construir UI dentro de self.body (no en self)
        self._build_ui(self.body)
        self._apply_state_config()

        self._safe_outputs()
        self.after(self.update_period_ms, self._tick)

    # =========================
    # SCROLL helpers (solo UI)
    # =========================
    def _on_body_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        # Mantener el frame interno al mismo ancho del canvas
        self.canvas.itemconfigure(self._body_window, width=event.width)

    def _bind_mousewheel(self, widget):
        # Windows / Mac / Linux (tkinter varía)
        widget.bind_all("<MouseWheel>", self._on_mousewheel)       # Windows/Mac
        widget.bind_all("<Button-4>", self._on_mousewheel_linux)   # Linux up
        widget.bind_all("<Button-5>", self._on_mousewheel_linux)   # Linux down

    def _on_mousewheel(self, event):
        # En Windows, event.delta = 120/-120
        if event.delta:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_mousewheel_linux(self, event):
        if event.num == 4:
            self.canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(1, "units")

    # -------------------------
    # UI (igual que antes, solo que vive en self.body)
    # -------------------------
    def _build_ui(self, root):
        root.columnconfigure(0, weight=1)

        title = ttk.Label(root, text="MODO MANUAL", font=("Arial", 16, "bold"))
        title.grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))

        frm_cfg = ttk.LabelFrame(root, text="Configuración")
        frm_cfg.grid(row=1, column=0, sticky="ew", padx=10, pady=8)
        frm_cfg.columnconfigure(0, weight=1)
        frm_cfg.columnconfigure(1, weight=1)
        self.frm_cfg = frm_cfg

        mode_box = ttk.LabelFrame(frm_cfg, text="DUT: Señal")
        mode_box.grid(row=0, column=0, sticky="ew", padx=8, pady=6)

        rb_a1 = ttk.Radiobutton(mode_box, text="Corriente (A1) 4–20 mA", value="A1",
                                variable=self.var_mode, command=self._on_mode_changed)
        rb_a0 = ttk.Radiobutton(mode_box, text="Voltaje (A0) 0–10 V", value="A0",
                                variable=self.var_mode, command=self._on_mode_changed)
        rb_a1.grid(row=0, column=0, sticky="w", padx=8, pady=3)
        rb_a0.grid(row=1, column=0, sticky="w", padx=8, pady=3)

        rng_box = ttk.LabelFrame(frm_cfg, text="Rangos")
        rng_box.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        rng_box.columnconfigure(1, weight=1)

        ttk.Label(rng_box, text="Presión mín (kPa):").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(rng_box, textvariable=self.var_pmin, width=12).grid(row=0, column=1, sticky="w", padx=6, pady=3)

        ttk.Label(rng_box, text="Presión máx (kPa):").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(rng_box, textvariable=self.var_pmax, width=12).grid(row=1, column=1, sticky="w", padx=6, pady=3)

        self.lbl_sigmin = ttk.Label(rng_box, text="Corriente mín (mA):")
        self.lbl_sigmin.grid(row=2, column=0, sticky="w", padx=6, pady=3)
        self.ent_sigmin = ttk.Entry(rng_box, textvariable=self.var_sigmin, width=12)
        self.ent_sigmin.grid(row=2, column=1, sticky="w", padx=6, pady=3)

        self.lbl_sigmax = ttk.Label(rng_box, text="Corriente máx (mA):")
        self.lbl_sigmax.grid(row=3, column=0, sticky="w", padx=6, pady=3)
        self.ent_sigmax = ttk.Entry(rng_box, textvariable=self.var_sigmax, width=12)
        self.ent_sigmax.grid(row=3, column=1, sticky="w", padx=6, pady=3)

        ttk.Label(rng_box, text="P. máx seguridad (kPa):").grid(row=4, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(rng_box, textvariable=self.var_pmaxseg, width=12).grid(row=4, column=1, sticky="w", padx=6, pady=3)

        sp_box = ttk.LabelFrame(frm_cfg, text="Control")
        sp_box.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=6)
        sp_box.columnconfigure(1, weight=1)

        ttk.Label(sp_box, text="Setpoint (kPa):").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ent_sp = ttk.Entry(sp_box, textvariable=self.var_sp, width=12)
        ent_sp.grid(row=0, column=1, sticky="w", padx=6, pady=4)

        self.btn_apply_sp = ttk.Button(sp_box, text="APLICAR SP", command=self._apply_sp)
        self.btn_apply_sp.grid(row=0, column=2, sticky="w", padx=6, pady=4)
        ent_sp.bind("<Return>", lambda e: self._apply_sp())

        btns = ttk.Frame(frm_cfg)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        btns.columnconfigure(2, weight=1)

        self.btn_zero = ttk.Button(btns, text="TARA (P=0)", command=self._do_tare)
        self.btn_start = ttk.Button(btns, text="START", command=self._start)
        self.btn_back = ttk.Button(btns, text="BACK", command=self._back_to_idle)

        self.btn_zero.grid(row=0, column=0, sticky="ew", padx=5)
        self.btn_start.grid(row=0, column=1, sticky="ew", padx=5)
        self.btn_back.grid(row=0, column=2, sticky="ew", padx=5)

        frm_live = ttk.LabelFrame(root, text="Lecturas en vivo (tipo calibrador)")
        frm_live.grid(row=2, column=0, sticky="ew", padx=10, pady=8)
        frm_live.columnconfigure(1, weight=1)
        self.frm_live = frm_live

        ttk.Label(frm_live, text="FUENTE (Presión):").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Label(frm_live, textvariable=self.var_p_source, font=("Arial", 14, "bold")).grid(row=0, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(frm_live, text="DUT (Medición):").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Label(frm_live, textvariable=self.var_sig, font=("Arial", 14, "bold")).grid(row=1, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(frm_live, text="%SPAN:").grid(row=2, column=0, sticky="w", padx=8, pady=2)
        ttk.Label(frm_live, textvariable=self.var_span).grid(row=2, column=1, sticky="w", padx=8, pady=2)

        ttk.Label(frm_live, text="%ERROR:").grid(row=3, column=0, sticky="w", padx=8, pady=2)
        ttk.Label(frm_live, textvariable=self.var_err).grid(row=3, column=1, sticky="w", padx=8, pady=2)

        ttk.Label(frm_live, text="Control:").grid(row=4, column=0, sticky="w", padx=8, pady=2)
        ttk.Label(frm_live, textvariable=self.var_pwm).grid(row=4, column=1, sticky="w", padx=8, pady=2)

        frm_run_btn = ttk.Frame(frm_live)
        frm_run_btn.grid(row=5, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        frm_run_btn.columnconfigure(0, weight=1)
        frm_run_btn.columnconfigure(1, weight=1)

        self.btn_stop = ttk.Button(frm_run_btn, text="STOP (volver config)", command=self._stop_to_config)
        self.btn_back2 = ttk.Button(frm_run_btn, text="BACK (Idle)", command=self._back_to_idle)

        self.btn_stop.grid(row=0, column=0, sticky="ew", padx=5)
        self.btn_back2.grid(row=0, column=1, sticky="ew", padx=5)

        self._on_mode_changed()

    # -------------------------
    # Estados internos (S2/S3)
    # -------------------------
    def _apply_state_config(self):
        self.rt.running = False
        self.pi.reset()
        self.pi.freeze()
        self.rt.last_update_ts = 0.0
        self._safe_outputs(valve_open=True)
        self._set_config_widgets_state(enabled=True)
        self.btn_stop.state(["disabled"])

    def _apply_state_run(self):
        self.rt.running = True
        self.pi.reset()
        self.pi.unfreeze()
        self.rt.last_update_ts = 0.0
        self.set_valve(True)
        self.set_relay(True)
        self._set_config_widgets_state(enabled=False)
        self.btn_stop.state(["!disabled"])

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
    # Acciones botones
    # -------------------------
    def _on_mode_changed(self):
        mode = self.var_mode.get().strip()
        if mode not in ("A0", "A1"):
            mode = "A1"
            self.var_mode.set(mode)
        self.cfg.dut_mode = mode

        if mode == "A0":
            self.lbl_sigmin.configure(text="Voltaje mín (V):")
            self.lbl_sigmax.configure(text="Voltaje máx (V):")
            self.var_sig.set("0.000 V")
        else:
            self.lbl_sigmin.configure(text="Corriente mín (mA):")
            self.lbl_sigmax.configure(text="Corriente máx (mA):")
            self.var_sig.set("0.000 mA")

    def _do_tare(self):
        try:
            p_corr = self._read_pressure_corr_kpa()
            self.rt.p_zero_kpa = p_corr
            messagebox.showinfo("TARA", f"Tara aplicada.\nAhora P≈0 desde Pcorr={p_corr:.2f} kPa")
        except Exception as e:
            messagebox.showerror("TARA", f"No se pudo aplicar tara: {e}")

    # ✅ SOLO SP APLICADO CON BOTON / ENTER
    def _apply_sp(self):
        try:
            s = self.var_sp.get().strip().replace(",", ".")
            sp = float(s)
            if sp < 0:
                sp = 0.0
            self.cfg.sp_kpa = float(sp)
        except Exception:
            pass

    def _start(self):
        try:
            self._pull_config_from_ui()
            self._validate_config()
            self._apply_sp()
            self._apply_state_run()
        except Exception as e:
            messagebox.showerror("CONFIG", str(e))

    def _stop_to_config(self):
        self._safe_outputs(valve_open=True)
        self._apply_state_config()

    def _back_to_idle(self):
        self._safe_outputs(valve_open=True)
        self.request_event("EV_BACK", None)

    # -------------------------
    # Lecturas y cálculo live
    # -------------------------
    def _read_vadc_avg(self, ch: int, n: int, dt_s: float) -> float:
        s = 0.0
        for _ in range(max(1, n)):
            v = float(self.read_vadc(ch))
            s += v
            if dt_s > 0:
                time.sleep(dt_s)
        return s / max(1, n)

    def _read_pressure_corr_kpa(self) -> float:
        vadc = self._read_vadc_avg(config.ADS_CH_REF, n=5, dt_s=0.0)
        return mpx_vadc_to_kpa(vadc)

    def _read_dut_eng(self) -> float:
        ch = config.ADS_CH_DUT_V if self.cfg.dut_mode == "A0" else config.ADS_CH_DUT_mA
        vadc = self._read_vadc_avg(ch, n=5, dt_s=0.0)
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

    # -------------------------
    # Loop periódico
    # -------------------------
    def _tick(self):
        try:
            now = time.time()
            dt_real = None
            if self.rt.last_update_ts > 0.0:
                dt_real = now - self.rt.last_update_ts
                dt_real = max(0.01, min(dt_real, 0.5))
            self.rt.last_update_ts = now

            p_corr = self._read_pressure_corr_kpa()
            p = p_corr - self.rt.p_zero_kpa
            if p < 0:
                p = 0.0

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

            if self.rt.running:
                sp = float(self.cfg.sp_kpa)
                u_cmd = self.pi.step(sp_kpa=sp, p_kpa=p, dt=dt_real)
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

    # -------------------------
    # Config desde UI
    # -------------------------
    def _pull_config_from_ui(self):
        self.cfg.dut_mode = self.var_mode.get().strip()

        def f(var: tk.StringVar, default: float) -> float:
            try:
                return float(var.get().strip().replace(",", "."))
            except:
                return default

        self.cfg.sp_kpa = f(self.var_sp, self.cfg.sp_kpa)
        self.cfg.p_min_kpa = f(self.var_pmin, self.cfg.p_min_kpa)
        self.cfg.p_max_kpa = f(self.var_pmax, self.cfg.p_max_kpa)
        self.cfg.sig_min = f(self.var_sigmin, self.cfg.sig_min)
        self.cfg.sig_max = f(self.var_sigmax, self.cfg.sig_max)
        self.cfg.p_max_seguridad_kpa = f(self.var_pmaxseg, self.cfg.p_max_seguridad_kpa)

    def _validate_config(self):
        if self.cfg.p_max_kpa <= self.cfg.p_min_kpa:
            raise ValueError("Presión máx debe ser mayor que presión mín.")
        if self.cfg.sig_max <= self.cfg.sig_min:
            raise ValueError("Señal máx debe ser mayor que señal mín.")

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
            self.pi.reset()
            self.pi.freeze()
        except Exception:
            pass