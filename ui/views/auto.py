# mode_auto.py
# -*- coding: utf-8 -*-

import time
import tkinter as tk
from tkinter import ttk, messagebox, font as tkFont
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any, List
import os
from datetime import datetime

from config import hardware as config
from core.control import PIController, PIConfig

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
    deadband_kpa: float = 3.0
    inband_up_s: float = 1.5
    inband_down_s: float = 0.5

    # Cierre retardado de EV cuando llega al deadband en bajada:
    valve_close_delay_s: float = 0.5  # FIJO por requisito

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

        # RESULTADOS (solo se añade esto, no cambia control)
        self.results: List[Dict[str, Any]] = []
        self._results_win: Optional[tk.Toplevel] = None

        # PI (base IGUAL a config; en START aplicamos overrides desde cfg)
        self.pi = PIController(PIConfig(
            kp=config.PI_CFG.kp,
            ki=config.PI_CFG.ki,
            dt=config.PI_CFG.dt,
            u_min=config.PI_CFG.u_min,
            u_max=config.PI_CFG.u_max,
            deadband_kpa=float(getattr(config.PI_CFG, "deadband_kpa", 0.5)),
            u_ff=config.PI_CFG.u_ff,
            i_decay_in_deadband=0.97
        ))

        # dt real del PI
        self._last_tick_ts: Optional[float] = None

        # Ventana control (si está abierta)
        self._control_win: Optional[tk.Toplevel] = None

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

        # LIVE con DUT
        self.lbl_live = ttk.Label(self, text="P=--.- kPa | SP=--.- | u=-- | DUT=--", font=("Arial", 11))
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
        self.btn_sig_min = ttk.Button(frm, text=f"[{self.var_sig_min.get()}]", command=lambda: self._open_edit_dialog(self.var_sig_min, "Señal min", 0, 100, self.btn_sig_min))
        self.btn_sig_min.grid(row=1, column=1, padx=6, pady=2, sticky="w")

        ttk.Label(frm, text="Señal max").grid(row=1, column=2, padx=6, pady=2, sticky="e")
        self.btn_sig_max = ttk.Button(frm, text=f"[{self.var_sig_max.get()}]", command=lambda: self._open_edit_dialog(self.var_sig_max, "Señal max", 0, 100, self.btn_sig_max))
        self.btn_sig_max.grid(row=1, column=3, padx=6, pady=2, sticky="w")

        ttk.Label(frm, text="P min (kPa)").grid(row=2, column=0, padx=6, pady=2, sticky="e")
        self.btn_pmin = ttk.Button(frm, text=f"[{self.var_pmin.get()}]", command=lambda: self._open_edit_dialog(self.var_pmin, "P min (kPa)", 0, 500, self.btn_pmin))
        self.btn_pmin.grid(row=2, column=1, padx=6, pady=2, sticky="w")

        ttk.Label(frm, text="P max (kPa)").grid(row=2, column=2, padx=6, pady=2, sticky="e")
        self.btn_pmax = ttk.Button(frm, text=f"[{self.var_pmax.get()}]", command=lambda: self._open_edit_dialog(self.var_pmax, "P max (kPa)", 0, 500, self.btn_pmax))
        self.btn_pmax.grid(row=2, column=3, padx=6, pady=2, sticky="w")

        # Secuencia
        self.var_npts = tk.StringVar(value="5")
        self.var_dir = tk.StringVar(value="BOTH")
        ttk.Label(frm, text="Puntos").grid(row=3, column=0, padx=6, pady=2, sticky="e")
        ttk.Combobox(frm, textvariable=self.var_npts, values=["2", "3", "5"], width=6, state="readonly").grid(row=3, column=1, padx=6, pady=2, sticky="w")
        ttk.Label(frm, text="Dirección").grid(row=3, column=2, padx=6, pady=2, sticky="e")
        ttk.Combobox(frm, textvariable=self.var_dir, values=["UP", "DOWN", "BOTH"], width=8, state="readonly").grid(row=3, column=3, padx=6, pady=2, sticky="w")

        # Tiempos (NO control)
        self.var_tsettle = tk.StringVar(value="5")
        self.var_tmax = tk.StringVar(value="10")

        ttk.Label(frm, text="Asentamiento (s)").grid(row=4, column=0, padx=6, pady=2, sticky="e")
        self.btn_tsettle = ttk.Button(frm, text=f"[{self.var_tsettle.get()}]", command=lambda: self._open_edit_dialog(self.var_tsettle, "Asentamiento (s)", 0, 60, self.btn_tsettle))
        self.btn_tsettle.grid(row=4, column=1, padx=6, pady=2, sticky="w")

        ttk.Label(frm, text="P máx (s)").grid(row=4, column=2, padx=6, pady=2, sticky="e")
        self.btn_tmax = ttk.Button(frm, text=f"[{self.var_tmax.get()}]", command=lambda: self._open_edit_dialog(self.var_tmax, "P máx (s)", 0, 60, self.btn_tmax))
        self.btn_tmax.grid(row=4, column=3, padx=6, pady=2, sticky="w")

        ttk.Button(frm, text="Editar condiciones de control", command=self._open_control_window)\
            .grid(row=5, column=0, columnspan=4, padx=6, pady=8, sticky="we")

        btns = ttk.Frame(self)
        btns.pack(pady=10)

        ttk.Button(btns, text="TARA (0 kPa)", command=self._do_tare).grid(row=0, column=0, padx=10)
        ttk.Button(btns, text="START", command=self._start).grid(row=0, column=1, padx=10)
        ttk.Button(btns, text="STOP", command=self._stop).grid(row=0, column=2, padx=10)

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

        # Frame para teclado numérico
        kbd_frm = ttk.LabelFrame(frm, text="Teclado", padding=6)
        kbd_frm.pack(fill="both", expand=True, pady=(0, 8))

        def add_digit(digit):
            """Agrega un dígito al campo"""
            current = var_edit.get()
            var_edit.set(current + str(digit))
            entry.focus()
            entry.update()

        def add_decimal():
            """Agrega un punto decimal"""
            current = var_edit.get()
            if "." not in current:
                var_edit.set(current + ".")
            entry.focus()
            entry.update()

        def delete_last():
            """Borra el último carácter"""
            current = var_edit.get()
            var_edit.set(current[:-1] if current else "")
            entry.focus()
            entry.update()

        def clear_all():
            """Borra todo"""
            var_edit.set("")
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

    def _update_button_display(self):
        """
        Actualiza el texto de los botones para mostrar el valor actual.
        Esto es un placeholder que se puede mejorar si es necesario.
        """
        pass

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

        self.var_deadband = tk.DoubleVar(value=self.cfg.deadband_kpa)
        self.var_inband_up = tk.DoubleVar(value=self.cfg.inband_up_s)
        self.var_inband_down = tk.DoubleVar(value=self.cfg.inband_down_s)
        self.var_u_min = tk.DoubleVar(value=self.cfg.u_min)
        self.var_u_max = tk.DoubleVar(value=self.cfg.u_max)
        self.var_u_ff = tk.DoubleVar(value=self.cfg.u_ff)

        frm = ttk.Frame(win, padding=20)
        frm.grid(row=0, column=0)

        # Fuente más grande
        lbl_font = ("Arial", 13)
        spinbox_font = ("Arial", 14, "bold")

        r = 0
        ttk.Label(frm, text="Banda muerta (kPa)", font=lbl_font).grid(row=r, column=0, sticky="e", padx=12, pady=10)
        sb_deadband = tk.Spinbox(frm, from_=0, to=20, increment=0.1, textvariable=self.var_deadband, width=18, format="%.3f", font=spinbox_font)
        sb_deadband.grid(row=r, column=1, sticky="ew", padx=12, pady=10)
        r += 1

        ttk.Label(frm, text="Tiempo en banda SUBIDA (s)", font=lbl_font).grid(row=r, column=0, sticky="e", padx=12, pady=10)
        sb_inband_up = tk.Spinbox(frm, from_=0, to=30, increment=0.1, textvariable=self.var_inband_up, width=18, format="%.3f", font=spinbox_font)
        sb_inband_up.grid(row=r, column=1, sticky="ew", padx=12, pady=10)
        r += 1

        ttk.Label(frm, text="Tiempo en banda BAJADA (s)", font=lbl_font).grid(row=r, column=0, sticky="e", padx=12, pady=10)
        sb_inband_down = tk.Spinbox(frm, from_=0, to=30, increment=0.1, textvariable=self.var_inband_down, width=18, format="%.3f", font=spinbox_font)
        sb_inband_down.grid(row=r, column=1, sticky="ew", padx=12, pady=10)
        r += 1

        ttk.Separator(frm).grid(row=r, column=0, columnspan=2, sticky="we", pady=12)
        r += 1

        ttk.Label(frm, text="U mínima", font=lbl_font).grid(row=r, column=0, sticky="e", padx=12, pady=10)
        sb_u_min = tk.Spinbox(frm, from_=0, to=1, increment=0.01, textvariable=self.var_u_min, width=18, format="%.3f", font=spinbox_font)
        sb_u_min.grid(row=r, column=1, sticky="ew", padx=12, pady=10)
        r += 1

        ttk.Label(frm, text="U máxima", font=lbl_font).grid(row=r, column=0, sticky="e", padx=12, pady=10)
        sb_u_max = tk.Spinbox(frm, from_=0, to=1, increment=0.01, textvariable=self.var_u_max, width=18, format="%.3f", font=spinbox_font)
        sb_u_max.grid(row=r, column=1, sticky="ew", padx=12, pady=10)
        r += 1

        ttk.Label(frm, text="U feedforward (Uff)", font=lbl_font).grid(row=r, column=0, sticky="e", padx=12, pady=10)
        sb_u_ff = tk.Spinbox(frm, from_=0, to=1, increment=0.01, textvariable=self.var_u_ff, width=18, format="%.3f", font=spinbox_font)
        sb_u_ff.grid(row=r, column=1, sticky="ew", padx=12, pady=10)
        r += 1

        ttk.Label(frm, text="Nota: en BAJADA la electroválvula se cierra 0.5 s después de llegar al deadband.", font=("Arial", 10))\
            .grid(row=r, column=0, columnspan=2, sticky="w", padx=6, pady=(15, 5))
        r += 1

        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=2, pady=(15, 0))

        ttk.Button(btns, text="Guardar", command=self._save_control_window).grid(row=0, column=0, padx=12, ipady=8)
        ttk.Button(btns, text="Cerrar", command=win.destroy).grid(row=0, column=1, padx=12, ipady=8)

        def _on_close():
            try:
                win.destroy()
            finally:
                self._control_win = None

        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _save_control_window(self):
        try:
            dead = float(self.var_deadband.get())
            inu = float(self.var_inband_up.get())
            ind = float(self.var_inband_down.get())
            umin = float(self.var_u_min.get())
            umax = float(self.var_u_max.get())
            uff = float(self.var_u_ff.get())

            if dead <= 0:
                raise ValueError("La banda muerta debe ser > 0.")
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
        self.cfg.p_min_kpa = float(self.var_pmin.get().strip().replace(",", "."))
        self.cfg.p_max_kpa = float(self.var_pmax.get().strip().replace(",", "."))
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

        if self.cfg.deadband_kpa <= 0:
            raise ValueError("Banda muerta debe ser > 0.")
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

            # (control igual)
            self.pi.cfg.deadband_kpa = float(self.cfg.deadband_kpa)
            self.pi.cfg.u_min = float(self.cfg.u_min)
            self.pi.cfg.u_max = float(self.cfg.u_max)
            self.pi.cfg.u_ff  = float(self.cfg.u_ff)

            if not self.rt.tare_done:
                p_corr = self._read_pressure_corr_kpa()
                self.rt.p_zero_kpa = p_corr
                self.rt.tare_done = True

            self.rt.points = self._build_points()
            self.rt.step_index = 0
            self.rt.running = True
            self.pi.reset()

            # reset resultados
            self.results = []

            self._last_tick_ts = None
            self._goto_state(ZERO_VENT)
            self.lbl_status.config(text=f"RUNNING | {self.rt.state}")

        except Exception as e:
            messagebox.showerror("AUTO", str(e))

    def _stop(self):
        self.rt.running = False
        self._safe_outputs(valve_open=True)
        self._goto_state(IDLE)
        self.lbl_status.config(text="STOPPED")
        self._last_tick_ts = None

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

            self._show_results_window()

            return

        self._goto_state(GOTO_SP)

    def _is_down_step(self, sp: float, p: float) -> bool:
        return sp < p

    # ========================================================
    # LOOP
    # ========================================================
    def _tick(self):
        try:
            if not self.rt.running:
                return

            now = time.time()
            if self._last_tick_ts is None:
                dt_pi = None
            else:
                dt_pi = max(0.001, now - self._last_tick_ts)
            self._last_tick_ts = now

            p_corr = self._read_pressure_corr_kpa()
            p = max(0.0, p_corr - self.rt.p_zero_kpa)
            self.rt.last_p = p

            sp = self._current_sp()
            dead = float(self.cfg.deadband_kpa)

            if p >= float(self.cfg.p_max_seguridad_kpa):
                raise RuntimeError(f"OVERPRESSURE: P={p:.2f} kPa")

            # LIVE: DUT
            try:
                dut_txt = self._dut_text_live()
            except Exception:
                dut_txt = "DUT=ERR"

            self.lbl_live.config(
                text=f"P={p:6.2f} kPa | SP={sp:6.2f} | u={self.rt.last_u:5.3f} | {dut_txt}"
            )

            st = self.rt.state
            t = self.rt.t_state or now
            dt_st = now - t

            if st == ZERO_VENT:
                self.set_pump(1.0)
                self.set_relay(False)
                self.set_valve(True)

                if abs(p - 0.0) <= dead:
                    self._goto_state(ZERO_HOLD)

            elif st == ZERO_HOLD:
                self.set_pump(1.0)
                self.set_relay(False)
                self.set_valve(False)

                if dt_st >= float(self.cfg.settle_time_s):
                    self._advance_point()

            elif st == GOTO_SP:
                is_down = self._is_down_step(sp, p)

                if not is_down:
                    self.set_valve(False)
                    self.set_relay(True)

                    u = self.pi.step(sp_kpa=sp, p_kpa=p, dt=dt_pi)
                    self.rt.last_u = float(u)
                    self.set_pump(u)

                    if abs(sp - p) <= dead:
                        self._goto_state(IN_BAND_WAIT_UP)

                else:
                    self.set_pump(1.0)
                    self.set_relay(False)
                    self.pi.freeze()

                    self.set_valve(True)
                    self.rt.last_u = 1.0

                    if abs(sp - p) <= dead:
                        self._goto_state(IN_BAND_WAIT_DOWN)

            elif st == IN_BAND_WAIT_UP:
                self.set_valve(False)
                self.set_relay(True)

                u = self.pi.step(sp_kpa=sp, p_kpa=p, dt=dt_pi)
                self.rt.last_u = float(u)
                self.set_pump(u)

                if abs(sp - p) > dead:
                    self._goto_state(GOTO_SP)
                else:
                    if dt_st >= float(self.cfg.inband_up_s):
                        self.set_pump(1.0)
                        self.set_relay(False)
                        self.pi.freeze()
                        self._goto_state(HOLD_MEASURE)

            elif st == IN_BAND_WAIT_DOWN:
                self.set_pump(1.0)
                self.set_relay(False)
                self.pi.freeze()
                self.set_valve(True)

                if abs(sp - p) > dead:
                    self._goto_state(GOTO_SP)
                else:
                    if dt_st >= float(self.cfg.inband_down_s):
                        self._goto_state(DOWN_CLOSE_DELAY)

            elif st == DOWN_CLOSE_DELAY:
                self.set_pump(1.0)
                self.set_relay(False)
                self.pi.freeze()
                self.set_valve(True)

                if dt_st >= float(self.cfg.valve_close_delay_s):
                    self.set_valve(False)
                    self._goto_state(HOLD_MEASURE)

            elif st == HOLD_MEASURE:
                self.set_valve(False)
                self.set_pump(1.0)
                self.set_relay(False)

                wait = float(self.cfg.settle_time_max_s) if self._is_max_point(sp) else float(self.cfg.settle_time_s)
                if dt_st >= wait:
                    # ✅ AQUÍ SOLO AÑADIMOS MEDICIÓN Y REGISTRO (no cambia control)
                    try:
                        self._record_point_result(sp_kpa=float(sp))
                    except Exception as e:
                        # si falla medición, aborta con error claro
                        raise RuntimeError(f"Fallo medición punto (SP={sp:.2f}): {e}")

                    self.pi.unfreeze()
                    self._advance_point()

            else:
                self._safe_outputs(valve_open=True)
                self._goto_state(IDLE)

            self.lbl_status.config(text=f"RUNNING | {self.rt.state} | i={self.rt.step_index}/{len(self.rt.points)-1}")

        except Exception as e:
            self._safe_outputs(valve_open=True)
            self.rt.running = False
            self._goto_state(IDLE)
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
        dt_s = float(getattr(config, "SAMPLE_DT_MEASURE_S", 0.01))

        p_list: List[float] = []
        dut_list: List[float] = []
        vadc_ref_list: List[float] = []
        vadc_dut_list: List[float] = []

        mode = (self.cfg.dut_mode or "A1").upper()
        ch_dut = config.ADS_CH_DUT_V if mode == "A0" else config.ADS_CH_DUT_mA

        for _ in range(max(1, n)):
            vadc_ref = float(self.read_vadc(config.ADS_CH_REF))
            p_corr = float(self._mpx_vadc_to_kpa(vadc_ref))
            p = max(0.0, p_corr - float(self.rt.p_zero_kpa))

            vadc_dut = float(self.read_vadc(ch_dut))
            dut_eng = float(self._dut_vadc_to_eng(vadc_dut, mode))

            vadc_ref_list.append(vadc_ref)
            vadc_dut_list.append(vadc_dut)
            p_list.append(p)
            dut_list.append(dut_eng)

            if dt_s > 0:
                time.sleep(dt_s)

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
            "dut_mode": mode,
            "dut_eng": float(dut_mean),
            "dut_std": float(dut_std),
            "span_pct": float(span_pct),
            "err_pct": float(err_pct),
            "u_last": float(self.rt.last_u),
        }
        self.results.append(row)

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

    def _show_results_window(self):
        """
        Ventana final con:
        - Tabla de resultados (compacta)
        - Gráfica lineal con ecuación y R²
        - Botones para exportar a PDF y cerrar
        Optimizada para pantalla 7"
        """
        if not self.results:
            return

        # si ya existe, solo levantar
        if self._results_win is not None and self._results_win.winfo_exists():
            self._results_win.lift()
            self._results_win.focus_force()
            return

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
        ax.scatter(x, y, s=40, alpha=0.7, color="blue")
        ax.plot(x, y_hat, "r-", linewidth=1.5)
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
                ax_plot.scatter(x, y, s=50, alpha=0.7, color="blue", label="Datos medidos")
                ax_plot.plot(x, y_hat, "r-", linewidth=2, label="Ajuste lineal")
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
        ttk.Button(frm_btns, text="Cerrar", command=win.destroy).pack(side="left", padx=2)

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

        win.protocol("WM_DELETE_WINDOW", _on_close)

    # ========================================================
    # DUT UTILS (lectura en vivo con calibración lineal config.py)
    # ========================================================
    def _read_dut_vadc(self) -> float:
        mode = (self.cfg.dut_mode or "A1").upper()
        ch = config.ADS_CH_DUT_V if mode == "A0" else config.ADS_CH_DUT_mA
        return float(self.read_vadc(ch))

    def _dut_vadc_to_eng(self, vadc: float, mode: str) -> float:
        mode = (mode or "A1").upper()
        if mode == "A0":
            if bool(getattr(config, "USE_A0_CAL", True)):
                return float(getattr(config, "A0_VIN_GAIN", 1.0)) * float(vadc) + float(getattr(config, "A0_VIN_OFFSET", 0.0))
            return float(vadc)

        if bool(getattr(config, "USE_A1_CAL", True)):
            return float(getattr(config, "A1_IMA_GAIN", 1.0)) * float(vadc) + float(getattr(config, "A1_IMA_OFFSET", 0.0))
        return float(vadc)

    def _dut_text_live(self) -> str:
        mode = (self.cfg.dut_mode or "A1").upper()
        vadc = self._read_dut_vadc()

        if mode == "A0":
            vin = self._dut_vadc_to_eng(vadc, mode)
            return f"DUT(A0)= {vin:5.3f} V | Vadc={vadc:5.3f} V"

        ima = self._dut_vadc_to_eng(vadc, mode)
        return f"DUT(A1)= {ima:6.2f} mA | Vadc={vadc:5.3f} V"

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

