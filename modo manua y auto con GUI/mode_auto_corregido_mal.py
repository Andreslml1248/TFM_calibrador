# mode_auto.py
# -*- coding: utf-8 -*-

import time
import tkinter as tk
from tkinter import ttk, messagebox
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any, List

import config
from control_pi import PIController, PIConfig


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

    # ✅ Editables:
    deadband_kpa: float = float(getattr(config.PI_CFG, "deadband_kpa", 0.5))

    inband_wait_s: float = 2.0              # 2 s dentro del deadband
    valve_to_pump_delay_s: float = 0.5      # 0.5 s entre cerrar EV y apagar bomba

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

ZERO_VENT = "ZERO_VENT"           # EV abierta, bomba off, relé off, llegar a 0 real
ZERO_HOLD = "ZERO_HOLD"           # EV cerrada, todo off, mantener 0 y medir punto 0

GOTO_SP = "GOTO_SP"               # PI activo hacia setpoint
IN_BAND_WAIT = "IN_BAND_WAIT"     # dentro de deadband, contar 2 s
COAST_CLOSE_VALVE = "COAST_CLOSE_VALVE"  # cerrar EV
COAST_PUMP_OFF = "COAST_PUMP_OFF"         # 0.5 s después apagar bomba+relé
HOLD_MEASURE = "HOLD_MEASURE"     # todo off, EV cerrada, medir (SIN reenganche)


# ============================================================
# VIEW
# ============================================================
class AutoView(ttk.Frame):

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

        self.cfg = AutoConfig()
        self.rt = AutoRuntime(points=[])

        # PI igual al manual (NO tocamos naturaleza de u, ni LOW/HIGH de bomba)
        self.pi = PIController(PIConfig(
            kp=config.PI_CFG.kp,
            ki=config.PI_CFG.ki,
            dt=config.PI_CFG.dt,          # aquí vive el dt nominal (0.05)
            u_min=config.PI_CFG.u_min,
            u_max=config.PI_CFG.u_max,
            deadband_kpa=float(getattr(config.PI_CFG, "deadband_kpa", 0.5)),
            u_ff=config.PI_CFG.u_ff,
            i_decay_in_deadband=0.97
        ))

        self._build_ui()
        self._safe_outputs(valve_open=True)
        self.after(self.update_period_ms, self._tick)

    # ========================================================
    # UI
    # ========================================================
    def _build_ui(self):
        ttk.Label(self, text="MODO AUTOMÁTICO", font=("Arial", 16, "bold")).pack(pady=8)

        self.lbl_status = ttk.Label(self, text="IDLE", font=("Arial", 12, "bold"))
        self.lbl_status.pack(pady=4)

        self.lbl_live = ttk.Label(self, text="P=--.- kPa | SP=--.- | u=--", font=("Arial", 11))
        self.lbl_live.pack(pady=2)

        frm = ttk.LabelFrame(self, text="Configuración")
        frm.pack(fill="x", padx=10, pady=8)

        # DUT
        self.var_mode = tk.StringVar(value="A1")
        ttk.Radiobutton(frm, text="A1 (4–20 mA)", variable=self.var_mode, value="A1").grid(row=0, column=0, sticky="w", padx=6)
        ttk.Radiobutton(frm, text="A0 (0–10 V)", variable=self.var_mode, value="A0").grid(row=0, column=1, sticky="w", padx=6)

        # Señal / Presión
        self.var_sig_min = tk.StringVar(value="4.0")
        self.var_sig_max = tk.StringVar(value="20.0")
        self.var_pmin = tk.StringVar(value="0.0")
        self.var_pmax = tk.StringVar(value="200.0")

        ttk.Label(frm, text="Señal min").grid(row=1, column=0, padx=6, pady=2, sticky="e")
        ttk.Entry(frm, textvariable=self.var_sig_min, width=8).grid(row=1, column=1, padx=6, pady=2, sticky="w")
        ttk.Label(frm, text="Señal max").grid(row=1, column=2, padx=6, pady=2, sticky="e")
        ttk.Entry(frm, textvariable=self.var_sig_max, width=8).grid(row=1, column=3, padx=6, pady=2, sticky="w")

        ttk.Label(frm, text="P min (kPa)").grid(row=2, column=0, padx=6, pady=2, sticky="e")
        ttk.Entry(frm, textvariable=self.var_pmin, width=8).grid(row=2, column=1, padx=6, pady=2, sticky="w")
        ttk.Label(frm, text="P max (kPa)").grid(row=2, column=2, padx=6, pady=2, sticky="e")
        ttk.Entry(frm, textvariable=self.var_pmax, width=8).grid(row=2, column=3, padx=6, pady=2, sticky="w")

        # Secuencia
        self.var_npts = tk.StringVar(value="5")
        self.var_dir = tk.StringVar(value="BOTH")
        ttk.Label(frm, text="Puntos").grid(row=3, column=0, padx=6, pady=2, sticky="e")
        ttk.Combobox(frm, textvariable=self.var_npts, values=["2", "3", "5"], width=6, state="readonly").grid(row=3, column=1, padx=6, pady=2, sticky="w")
        ttk.Label(frm, text="Dirección").grid(row=3, column=2, padx=6, pady=2, sticky="e")
        ttk.Combobox(frm, textvariable=self.var_dir, values=["UP", "DOWN", "BOTH"], width=8, state="readonly").grid(row=3, column=3, padx=6, pady=2, sticky="w")

        # Tiempos + deadband editable
        self.var_tsettle = tk.StringVar(value="5")
        self.var_tmax = tk.StringVar(value="10")
        self.var_inband = tk.StringVar(value="2.0")
        self.var_deadband = tk.StringVar(value=f"{self.cfg.deadband_kpa:.3f}")

        ttk.Label(frm, text="Asentamiento (s)").grid(row=4, column=0, padx=6, pady=2, sticky="e")
        ttk.Entry(frm, textvariable=self.var_tsettle, width=8).grid(row=4, column=1, padx=6, pady=2, sticky="w")
        ttk.Label(frm, text="P máx (s)").grid(row=4, column=2, padx=6, pady=2, sticky="e")
        ttk.Entry(frm, textvariable=self.var_tmax, width=8).grid(row=4, column=3, padx=6, pady=2, sticky="w")

        ttk.Label(frm, text="Deadband (kPa)").grid(row=5, column=0, padx=6, pady=2, sticky="e")
        ttk.Entry(frm, textvariable=self.var_deadband, width=8).grid(row=5, column=1, padx=6, pady=2, sticky="w")

        ttk.Label(frm, text="In-band (s)").grid(row=5, column=2, padx=6, pady=2, sticky="e")
        ttk.Entry(frm, textvariable=self.var_inband, width=8).grid(row=5, column=3, padx=6, pady=2, sticky="w")

        ttk.Label(frm, text="EV→Bomba (s)").grid(row=6, column=0, padx=6, pady=2, sticky="e")
        ttk.Label(frm, text="0.5 fijo").grid(row=6, column=1, padx=6, pady=2, sticky="w")

        # Botones
        btns = ttk.Frame(self)
        btns.pack(pady=10)

        ttk.Button(btns, text="TARA (0 kPa)", command=self._do_tare).grid(row=0, column=0, padx=10)
        ttk.Button(btns, text="START", command=self._start).grid(row=0, column=1, padx=10)
        ttk.Button(btns, text="STOP", command=self._stop).grid(row=0, column=2, padx=10)

    # ========================================================
    # CONFIG / POINTS
    # ========================================================
    def _pull_cfg(self):
        self.cfg.dut_mode = self.var_mode.get().strip().upper()
        self.cfg.sig_min = float(self.var_sig_min.get().strip().replace(",", "."))
        self.cfg.sig_max = float(self.var_sig_max.get().strip().replace(",", "."))
        self.cfg.p_min_kpa = float(self.var_pmin.get().strip().replace(",", "."))
        self.cfg.p_max_kpa = float(self.var_pmax.get().strip().replace(",", "."))
        self.cfg.n_points = int(self.var_npts.get().strip())
        self.cfg.direction = self.var_dir.get().strip().upper()
        self.cfg.settle_time_s = float(self.var_tsettle.get().strip().replace(",", "."))
        self.cfg.settle_time_max_s = float(self.var_tmax.get().strip().replace(",", "."))
        self.cfg.inband_wait_s = float(self.var_inband.get().strip().replace(",", "."))
        self.cfg.deadband_kpa = float(self.var_deadband.get().strip().replace(",", "."))

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
        if self.cfg.inband_wait_s < 0:
            raise ValueError("In-band debe ser >= 0.")
        if self.cfg.deadband_kpa <= 0:
            raise ValueError("Deadband debe ser > 0.")

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

            # aplicar deadband al PI (sin tocar config global)
            self.pi.cfg.deadband_kpa = float(self.cfg.deadband_kpa)

            if not self.rt.tare_done:
                p_corr = self._read_pressure_corr_kpa()
                self.rt.p_zero_kpa = p_corr
                self.rt.tare_done = True

            self.rt.points = self._build_points()
            self.rt.step_index = 0
            self.rt.running = True
            self.pi.reset()

            self._goto_state(ZERO_VENT)
            self.lbl_status.config(text=f"RUNNING | {self.rt.state}")

        except Exception as e:
            messagebox.showerror("AUTO", str(e))

    def _stop(self):
        self.rt.running = False
        self._safe_outputs(valve_open=True)
        self._goto_state(IDLE)
        self.lbl_status.config(text="STOPPED")

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

    def _is_max_point(self, sp: float) -> bool:
        return abs(sp - max(self.rt.points)) < 1e-9 if self.rt.points else False

    def _advance_point(self):
        self.rt.step_index += 1
        self.pi.reset()
        self.rt.t_state = time.time()

        if self.rt.step_index >= len(self.rt.points):
            self.rt.running = False
            self._safe_outputs(valve_open=True)
            self._goto_state(IDLE)
            self.lbl_status.config(text="FINISHED")
            return

        self._goto_state(GOTO_SP)

    # ========================================================
    # LOOP
    # ========================================================
    def _tick(self):
        try:
            if not self.rt.running:
                return

            p_corr = self._read_pressure_corr_kpa()
            p = max(0.0, p_corr - self.rt.p_zero_kpa)
            self.rt.last_p = p

            sp = self._current_sp()
            dead = float(self.cfg.deadband_kpa)

            # Seguridad
            if p >= float(self.cfg.p_max_seguridad_kpa):
                raise RuntimeError(f"OVERPRESSURE: P={p:.2f} kPa")

            self.lbl_live.config(text=f"P={p:6.2f} kPa | SP={sp:6.2f} | u={self.rt.last_u:5.3f}")

            st = self.rt.state
            now = time.time()
            t0 = self.rt.t_state or now
            dt_st = now - t0

            # 1) ZERO_VENT
            if st == ZERO_VENT:
                self.set_pump(1.0)
                self.set_relay(False)
                self.set_valve(True)

                if abs(p - 0.0) <= dead:
                    self._goto_state(ZERO_HOLD)

            # 2) ZERO_HOLD
            elif st == ZERO_HOLD:
                self.set_pump(1.0)
                self.set_relay(False)
                self.set_valve(False)

                if dt_st >= float(self.cfg.settle_time_s):
                    self._advance_point()

            # 3) GOTO_SP
            elif st == GOTO_SP:
                self.set_valve(True)
                self.set_relay(True)

                # dt=None => usa dt nominal interno (config.PI_CFG.dt)
                u = self.pi.step(sp_kpa=sp, p_kpa=p, dt=None)
                self.rt.last_u = float(u)
                self.set_pump(u)

                if abs(sp - p) <= dead:
                    self._goto_state(IN_BAND_WAIT)

            # 4) IN_BAND_WAIT (mantiene PI mientras cuenta)
            elif st == IN_BAND_WAIT:
                self.set_valve(True)
                self.set_relay(True)

                u = self.pi.step(sp_kpa=sp, p_kpa=p, dt=None)
                self.rt.last_u = float(u)
                self.set_pump(u)

                if abs(sp - p) > dead:
                    self._goto_state(GOTO_SP)
                else:
                    if dt_st >= float(self.cfg.inband_wait_s):
                        self._goto_state(COAST_CLOSE_VALVE)

            # 5) COAST_CLOSE_VALVE: cierra EV, mantiene u un ratito
            elif st == COAST_CLOSE_VALVE:
                self.set_valve(False)
                self.set_relay(True)
                self.set_pump(self.rt.last_u)

                if dt_st >= float(self.cfg.valve_to_pump_delay_s):
                    self._goto_state(COAST_PUMP_OFF)

            # 6) COAST_PUMP_OFF: apaga todo
            elif st == COAST_PUMP_OFF:
                self.set_valve(False)
                self.set_pump(1.0)
                self.set_relay(False)
                self.pi.freeze()
                self._goto_state(HOLD_MEASURE)

            # 7) HOLD_MEASURE (A): NO reengancha aunque se salga
            elif st == HOLD_MEASURE:
                self.set_valve(False)
                self.set_pump(1.0)
                self.set_relay(False)

                wait = float(self.cfg.settle_time_max_s) if self._is_max_point(sp) else float(self.cfg.settle_time_s)

                if dt_st >= wait:
                    self.pi.unfreeze()
                    self._advance_point()

            else:
                self._safe_outputs(valve_open=True)
                self._goto_state(IDLE)

            self.lbl_status.config(text=f"RUNNING | {self.rt.state} | i={self.rt.step_index+1}/{len(self.rt.points)}")

        except Exception as e:
            self._safe_outputs(valve_open=True)
            self.rt.running = False
            self._goto_state(IDLE)
            self.request_event("EV_AUTO_FAIL", {"error": str(e)})

        finally:
            self.after(self.update_period_ms, self._tick)

    # ========================================================
    # PRESSURE UTILS
    # ========================================================
    def _read_pressure_corr_kpa(self) -> float:
        vadc = float(self.read_vadc(config.ADS_CH_REF))
        return float(self._mpx_vadc_to_kpa(vadc))

    def _mpx_vadc_to_kpa(self, vadc: float) -> float:
        p = config.MPX_A2 * vadc * vadc + config.MPX_B2 * vadc + config.MPX_C2
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
            self.pi.reset()
            self.pi.freeze()
        except Exception:
            pass