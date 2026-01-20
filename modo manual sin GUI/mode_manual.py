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
    # Polinomio
    p_raw = config.MPX_A2 * vadc * vadc + config.MPX_B2 * vadc + config.MPX_C2
    if p_raw < 0:
        p_raw = 0.0
    # 2PT
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
    dut_mode: str = "A1"  # "A0" o "A1"
    p_min_kpa: float = 0.0
    p_max_kpa: float = 200.0
    sig_min: float = 4.0   # Vmin o mAmin según modo
    sig_max: float = 20.0  # Vmax o mAmax según modo
    p_max_seguridad_kpa: float = config.P_MAX_SEGURIDAD_KPA


@dataclass
class ManualRuntime:
    running: bool = False
    p_zero_kpa: float = 0.0  # tara aplicada sobre Pcorr
    last_update_ts: float = 0.0


# =========================
# Frame GUI del modo manual
# =========================
class ManualView(ttk.Frame):
    """
    Vista/Control del modo manual (S2 y S3).
    Recibe callbacks de hardware desde main.py:

    - read_vadc(channel:int)->float
    - set_pump(u_cmd:float)        (u_cmd 0..1 lógico; internamente main aplica BOMBA_ACTIVE_LOW)
    - set_relay(on:bool)
    - set_valve(open_:bool)        (open_=True => energizada y abierta)
    - request_event(name:str)      (para volver a S1, etc)
    """

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

        self.var_mode = tk.StringVar(value=self.cfg.dut_mode)  # "A0"/"A1"

        # Lecturas en vivo
        self.var_p_source = tk.StringVar(value="0.00 kPa")
        self.var_sig = tk.StringVar(value="0.000 mA")
        self.var_span = tk.StringVar(value="0.0 %")
        self.var_err = tk.StringVar(value="0.0 %")
        self.var_pwm = tk.StringVar(value="u=0.000")

        self.var_temp = tk.StringVar(value="T: --.- °C")  # si luego lo pasas desde main

        self._build_ui()
        self._apply_state_config()

        # Estado seguro al entrar:
        self._safe_outputs()
        self.after(self.update_period_ms, self._tick)

    # -------------------------
    # UI
    # -------------------------
    def _build_ui(self):
        self.columnconfigure(0, weight=1)

        title = ttk.Label(self, text="MODO MANUAL", font=("Arial", 16, "bold"))
        title.grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))

        # Panel configuración (S2)
        frm_cfg = ttk.LabelFrame(self, text="Configuración")
        frm_cfg.grid(row=1, column=0, sticky="ew", padx=10, pady=8)
        frm_cfg.columnconfigure(0, weight=1)
        frm_cfg.columnconfigure(1, weight=1)
        self.frm_cfg = frm_cfg

        # Modo DUT
        mode_box = ttk.LabelFrame(frm_cfg, text="DUT: Señal")
        mode_box.grid(row=0, column=0, sticky="ew", padx=8, pady=6)

        rb_a1 = ttk.Radiobutton(mode_box, text="Corriente (A1) 4–20 mA", value="A1", variable=self.var_mode, command=self._on_mode_changed)
        rb_a0 = ttk.Radiobutton(mode_box, text="Voltaje (A0) 0–10 V", value="A0", variable=self.var_mode, command=self._on_mode_changed)
        rb_a1.grid(row=0, column=0, sticky="w", padx=8, pady=3)
        rb_a0.grid(row=1, column=0, sticky="w", padx=8, pady=3)

        # Rangos
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

        # Setpoint
        sp_box = ttk.LabelFrame(frm_cfg, text="Control")
        sp_box.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=6)
        sp_box.columnconfigure(1, weight=1)

        ttk.Label(sp_box, text="Setpoint (kPa):").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(sp_box, textvariable=self.var_sp, width=12).grid(row=0, column=1, sticky="w", padx=6, pady=4)

        # Botones config
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

        # Panel lectura (S3)
        frm_live = ttk.LabelFrame(self, text="Lecturas en vivo (tipo calibrador)")
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

        # Botones RUN
        frm_run_btn = ttk.Frame(frm_live)
        frm_run_btn.grid(row=5, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        frm_run_btn.columnconfigure(0, weight=1)
        frm_run_btn.columnconfigure(1, weight=1)

        self.btn_stop = ttk.Button(frm_run_btn, text="STOP (volver config)", command=self._stop_to_config)
        self.btn_back2 = ttk.Button(frm_run_btn, text="BACK (Idle)", command=self._back_to_idle)

        self.btn_stop.grid(row=0, column=0, sticky="ew", padx=5)
        self.btn_back2.grid(row=0, column=1, sticky="ew", padx=5)

        # Ajuste inicial de labels según modo
        self._on_mode_changed()

    # -------------------------
    # Estados internos (S2/S3)
    # -------------------------
    def _apply_state_config(self):
        """S2_CONFIG_MANUAL (config editable; bomba OFF)."""
        self.rt.running = False
        self.pi.reset()
        self.pi.freeze()  # en config no se usa
        self._safe_outputs(valve_open=True)

        # Habilitar config, deshabilitar stop
        self._set_config_widgets_state(enabled=True)
        self.btn_stop.state(["disabled"])

    def _apply_state_run(self):
        """S3_PI_CONTROL (corriendo; SP editable; el resto bloqueado)."""
        self.rt.running = True
        self.pi.reset()
        self.pi.unfreeze()

        # Actuadores: válvula abierta + relé ON (alimenta bomba)
        self.set_valve(True)
        self.set_relay(True)

        self._set_config_widgets_state(enabled=False)
        # pero SP sí editable en RUN:
        # (lo dejamos editable siempre)
        self.btn_stop.state(["!disabled"])

    def _set_config_widgets_state(self, enabled: bool):
        state = "normal" if enabled else "disabled"

        # En config: puedes tocar modo y rangos
        for child in self.frm_cfg.winfo_children():
            # respetar botones start/back/zero según estado
            pass

        # Manejo fino:
        # Radiobuttons están dentro de frm_cfg -> buscar recursivo
        def set_state_recursive(w):
            for c in w.winfo_children():
                # Entry / Radiobutton / etc.
                cls = c.winfo_class()
                if cls in ("TEntry", "Entry"):
                    c.configure(state=state)
                elif cls in ("TRadiobutton", "Radiobutton"):
                    c.configure(state=state)
                elif cls in ("TCombobox", "Combobox"):
                    c.configure(state=state)
                set_state_recursive(c)

        set_state_recursive(self.frm_cfg)

        # SP siempre editable (manual en vivo), incluso en RUN
        # así que lo re-habilitamos:
        # (buscamos la entry por variable no es trivial; lo dejamos simple: SP se vuelve a normal)
        # Mejor: no lo tocamos si está corriendo:
        # Si está corriendo, el state anterior lo puso disabled; lo reactivamos:
        if not enabled:
            # Reactivar solo SP
            # (hack: recorremos entries y reactivamos el primero del sp_box)
            try:
                # sp_box es el 3er child del frm_cfg (no garantizado), así que lo buscamos por texto:
                for lf in self.frm_cfg.winfo_children():
                    if isinstance(lf, ttk.LabelFrame) and lf.cget("text") == "Control":
                        for e in lf.winfo_children():
                            if isinstance(e, ttk.Entry):
                                e.configure(state="normal")
            except Exception:
                pass

        # Botones:
        if enabled:
            self.btn_start.state(["!disabled"])
            self.btn_zero.state(["!disabled"])
        else:
            self.btn_start.state(["disabled"])
            self.btn_zero.state(["!disabled"])  # tara la dejamos disponible si quieres; si no, pon disabled

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
            # valores por defecto típicos
            if self._is_default_sig_for_other_mode():
                self.var_sigmin.set("0.000")
                self.var_sigmax.set("10.000")
        else:
            self.lbl_sigmin.configure(text="Corriente mín (mA):")
            self.lbl_sigmax.configure(text="Corriente máx (mA):")
            if self._is_default_sig_for_other_mode():
                self.var_sigmin.set("4.000")
                self.var_sigmax.set("20.000")

    def _is_default_sig_for_other_mode(self) -> bool:
        # ayuda: si usuario no tocó aún, permitimos autodefault.
        # No es crítico.
        return True

    def _do_tare(self):
        """Tara: P=0 en la condición actual (Pcorr)."""
        try:
            p_corr = self._read_pressure_corr_kpa()
            self.rt.p_zero_kpa = p_corr
            messagebox.showinfo("TARA", f"Tara aplicada.\nAhora P≈0 desde Pcorr={p_corr:.2f} kPa")
        except Exception as e:
            messagebox.showerror("TARA", f"No se pudo aplicar tara: {e}")

    def _start(self):
        """Valida config y pasa a RUN."""
        try:
            self._pull_config_from_ui()
            self._validate_config()
            self._apply_state_run()
        except Exception as e:
            messagebox.showerror("CONFIG", str(e))

    def _stop_to_config(self):
        """Detiene PI y vuelve a config."""
        self._safe_outputs(valve_open=True)
        self._apply_state_config()

    def _back_to_idle(self):
        """Vuelve a S1 (main decide)."""
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
        # MPX está en ADS_CH_REF (A2)
        vadc = self._read_vadc_avg(config.ADS_CH_REF, n=5, dt_s=0.0)
        return mpx_vadc_to_kpa(vadc)

    def _read_dut_eng(self) -> float:
        if self.cfg.dut_mode == "A0":
            ch = config.ADS_CH_DUT_V
        else:
            ch = config.ADS_CH_DUT_mA

        vadc = self._read_vadc_avg(ch, n=5, dt_s=0.0)

        # sanity check opcional
        if vadc < config.VADC_MIN_OK - 0.1 or vadc > config.VADC_MAX_OK + 0.1:
            # no hacemos error crítico aquí: solo mostramos.
            pass

        return dut_vadc_to_eng(vadc, self.cfg.dut_mode)

    def _compute_span_percent(self, dut_eng: float) -> float:
        span = self.cfg.sig_max - self.cfg.sig_min
        if abs(span) < 1e-9:
            return 0.0
        return 100.0 * (dut_eng - self.cfg.sig_min) / span

    def _compute_error_percent_fluke_style(self, p_source_kpa: float, dut_eng: float) -> float:
        """
        Error tipo calibrador:
        - Referencia: presión aplicada (FUENTE), normalizada por rango de presión.
        - DUT: señal medida, normalizada por rango de señal.
        %Error = (%SPAN_DUT - %SPAN_PRESION)
        """
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
            # 1) Presión (Pcorr - tara)
            p_corr = self._read_pressure_corr_kpa()
            p = p_corr - self.rt.p_zero_kpa
            if p < 0:
                p = 0.0

            # 2) DUT eng (V o mA)
            dut_eng = self._read_dut_eng()

            # 3) Mostrar lecturas tipo calibrador
            self.var_p_source.set(f"{p:,.2f} kPa".replace(",", ""))
            if self.cfg.dut_mode == "A0":
                self.var_sig.set(f"{dut_eng:,.3f} V".replace(",", ""))
            else:
                self.var_sig.set(f"{dut_eng:,.3f} mA".replace(",", ""))

            span_pct = self._compute_span_percent(dut_eng)
            err_pct = self._compute_error_percent_fluke_style(p, dut_eng)

            self.var_span.set(f"{span_pct:,.2f} %".replace(",", ""))
            self.var_err.set(f"{err_pct:+,.2f} %".replace(",", ""))

            # 4) Seguridad: overpressure
            pmax_seg = self.cfg.p_max_seguridad_kpa
            if p >= pmax_seg:
                self._safe_outputs(valve_open=False)
                self.request_event("EV_OVERPRESSURE", {"p_kpa": p, "pmax_kpa": pmax_seg})
                return

            # 5) Si RUN: PI y PWM
            if self.rt.running:
                sp = self._get_sp_kpa()
                # PI produce u_cmd lógico 0..1
                u_cmd = self.pi.step(sp_kpa=sp, p_kpa=p, dt=None)
                self.set_pump(u_cmd)
                self.var_pwm.set(f"u={u_cmd:.3f}")
            else:
                self.var_pwm.set("u=0.000")

        except Exception as e:
            # En manual: si falla lectura repetida, main decidirá si es crítico
            self._safe_outputs(valve_open=True)
            self.request_event("EV_SENSOR_FAIL_CRITICAL", {"error": str(e)})
            return
        finally:
            self.after(self.update_period_ms, self._tick)

    # -------------------------
    # Config desde UI
    # -------------------------
    def _get_sp_kpa(self) -> float:
        s = self.var_sp.get().strip().replace(",", ".")
        try:
            return max(0.0, float(s))
        except:
            return self.cfg.sp_kpa

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
        if self.cfg.p_max_seguridad_kpa <= self.cfg.p_max_kpa:
            # No lo forzamos, pero es recomendable:
            # seguridad >= rango
            pass

    # -------------------------
    # Seguridad actuadores
    # -------------------------
    def _safe_outputs(self, valve_open: bool = True):
        """Apaga bomba (PWM OFF real) + relé OFF. Válvula abierta por defecto en manual idle."""
        try:
            self.set_pump(config.BOMBA_U_OFF if hasattr(config, "BOMBA_U_OFF") else 1.0)
        except:
            pass
        try:
            self.set_relay(False)
        except:
            pass
        try:
            if config.USE_VALVULA:
                self.set_valve(bool(valve_open))
        except:
            pass
        try:
            self.pi.reset()
            self.pi.freeze()
        except:
            pass
