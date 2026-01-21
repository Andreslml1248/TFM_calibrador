# mode_manual.py
# -*- coding: utf-8 -*-

import time
import tkinter as tk
from tkinter import ttk, messagebox, font as tkFont
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any

from config import hardware as config
from core.control import PIController, PIConfig


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
    dut_mode: str = "A1"  # "A0" o "A1"
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

        # Lecturas en vivo
        self.var_p_source = tk.StringVar(value="0.00 kPa")
        self.var_sig = tk.StringVar(value="0.000 mA")
        self.var_span = tk.StringVar(value="0.0 %")
        self.var_err = tk.StringVar(value="0.0 %")
        self.var_pwm = tk.StringVar(value="u=0.000")

        self._build_ui_compact()
        self._apply_state_config()

        self._safe_outputs()
        self.after(self.update_period_ms, self._tick)

    # -------------------------
    # UI compacta (SIN scroll)
    # -------------------------
    def _build_ui_compact(self):
        # Grid principal: 2 columnas
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1, uniform="col")
        self.grid_columnconfigure(1, weight=1, uniform="col")

        # Título arriba (ocupa 2 columnas)
        title = ttk.Label(self, text="MODO MANUAL", font=("Arial", 15, "bold"))
        title.grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4))

        # ===== Columna izquierda: CONFIG =====
        frm_cfg = ttk.LabelFrame(self, text="Configuración")
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
            mode_box, text="A1 (4–20 mA)", value="A1",
            variable=self.var_mode, command=self._on_mode_changed
        )
        rb_a0 = ttk.Radiobutton(
            mode_box, text="A0 (0–10 V)", value="A0",
            variable=self.var_mode, command=self._on_mode_changed
        )
        rb_a1.grid(row=0, column=0, sticky="w", padx=8, pady=(4, 2))
        rb_a0.grid(row=1, column=0, sticky="w", padx=8, pady=(2, 6))

        # Rangos
        rng_box = ttk.LabelFrame(top_cfg, text="Rangos")
        rng_box.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=0)
        rng_box.grid_columnconfigure(1, weight=1)

        # Hacemos 2 columnas compactas
        ttk.Label(rng_box, text="P mín").grid(row=0, column=0, sticky="w", padx=6, pady=(4, 2))
        self.btn_pmin = ttk.Button(rng_box, text=f"[{self.var_pmin.get()}]", command=lambda: self._open_edit_dialog(self.var_pmin, "P mín (kPa)", 0, 500, self.btn_pmin))
        self.btn_pmin.grid(row=0, column=1, sticky="w", padx=6, pady=(4, 2))

        ttk.Label(rng_box, text="P máx").grid(row=1, column=0, sticky="w", padx=6, pady=2)
        self.btn_pmax = ttk.Button(rng_box, text=f"[{self.var_pmax.get()}]", command=lambda: self._open_edit_dialog(self.var_pmax, "P máx (kPa)", 0, 500, self.btn_pmax))
        self.btn_pmax.grid(row=1, column=1, sticky="w", padx=6, pady=2)

        self.lbl_sigmin = ttk.Label(rng_box, text="I mín")
        self.lbl_sigmin.grid(row=2, column=0, sticky="w", padx=6, pady=2)
        self.btn_sigmin = ttk.Button(rng_box, text=f"[{self.var_sigmin.get()}]", command=lambda: self._open_edit_dialog(self.var_sigmin, "Señal mín", 0, 100, self.btn_sigmin))
        self.btn_sigmin.grid(row=2, column=1, sticky="w", padx=6, pady=2)

        self.lbl_sigmax = ttk.Label(rng_box, text="I máx")
        self.lbl_sigmax.grid(row=3, column=0, sticky="w", padx=6, pady=2)
        self.btn_sigmax = ttk.Button(rng_box, text=f"[{self.var_sigmax.get()}]", command=lambda: self._open_edit_dialog(self.var_sigmax, "Señal máx", 0, 100, self.btn_sigmax))
        self.btn_sigmax.grid(row=3, column=1, sticky="w", padx=6, pady=2)

        ttk.Label(rng_box, text="P seg").grid(row=4, column=0, sticky="w", padx=6, pady=(2, 6))
        self.btn_pmaxseg = ttk.Button(rng_box, text=f"[{self.var_pmaxseg.get()}]", command=lambda: self._open_edit_dialog(self.var_pmaxseg, "P seguridad (kPa)", 0, 500, self.btn_pmaxseg))
        self.btn_pmaxseg.grid(row=4, column=1, sticky="w", padx=6, pady=(2, 6))

        # Control (SP + botón aplicar) compacto
        sp_box = ttk.LabelFrame(frm_cfg, text="Control")
        sp_box.grid(row=1, column=0, sticky="ew", padx=8, pady=6)
        sp_box.grid_columnconfigure(1, weight=1)

        ttk.Label(sp_box, text="SP (kPa):").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.btn_sp = ttk.Button(sp_box, text=f"[{self.var_sp.get()}]", command=lambda: self._open_edit_dialog_sp())
        self.btn_sp.grid(row=0, column=1, sticky="w", padx=6, pady=6)

        # Botones config (fila compacta)
        btns = ttk.Frame(frm_cfg)
        btns.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 8))
        btns.grid_columnconfigure(0, weight=1)
        btns.grid_columnconfigure(1, weight=1)
        btns.grid_columnconfigure(2, weight=1)

        self.btn_zero = ttk.Button(btns, text="TARA", command=self._do_tare)
        self.btn_start = ttk.Button(btns, text="START", command=self._start)
        self.btn_back = ttk.Button(btns, text="BACK", command=self._back_to_idle)

        self.btn_zero.grid(row=0, column=0, sticky="ew", padx=4)
        self.btn_start.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_back.grid(row=0, column=2, sticky="ew", padx=4)

        # ===== Columna derecha: LIVE =====
        frm_live = ttk.LabelFrame(self, text="Lecturas en vivo")
        frm_live.grid(row=1, column=1, sticky="nsew", padx=(6, 10), pady=(4, 8))
        frm_live.grid_columnconfigure(1, weight=1)
        self.frm_live = frm_live

        # Letras un pelín más pequeñas para que quepa
        big = ("Arial", 13, "bold")
        normal = ("Arial", 11)

        ttk.Label(frm_live, text="PRESIÓN:", font=normal).grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        ttk.Label(frm_live, textvariable=self.var_p_source, font=big).grid(row=0, column=1, sticky="w", padx=8, pady=(8, 4))

        ttk.Label(frm_live, text="DUT:", font=normal).grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Label(frm_live, textvariable=self.var_sig, font=big).grid(row=1, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(frm_live, text="%SPAN:", font=normal).grid(row=2, column=0, sticky="w", padx=8, pady=2)
        ttk.Label(frm_live, textvariable=self.var_span, font=normal).grid(row=2, column=1, sticky="w", padx=8, pady=2)

        ttk.Label(frm_live, text="%ERROR:", font=normal).grid(row=3, column=0, sticky="w", padx=8, pady=2)
        ttk.Label(frm_live, textvariable=self.var_err, font=normal).grid(row=3, column=1, sticky="w", padx=8, pady=2)

        ttk.Label(frm_live, text="CONTROL:", font=normal).grid(row=4, column=0, sticky="w", padx=8, pady=(2, 8))
        ttk.Label(frm_live, textvariable=self.var_pwm, font=normal).grid(row=4, column=1, sticky="w", padx=8, pady=(2, 8))

        # Botones RUN abajo en la columna derecha
        frm_run_btn = ttk.Frame(frm_live)
        frm_run_btn.grid(row=5, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 10))
        frm_run_btn.grid_columnconfigure(0, weight=1)
        frm_run_btn.grid_columnconfigure(1, weight=1)

        self.btn_stop = ttk.Button(frm_run_btn, text="STOP", command=self._stop_to_config)
        self.btn_back2 = ttk.Button(frm_run_btn, text="BACK", command=self._back_to_idle)

        self.btn_stop.grid(row=0, column=0, sticky="ew", padx=4)
        self.btn_back2.grid(row=0, column=1, sticky="ew", padx=4)

        self._on_mode_changed()

    # -------------------------
    # Estados internos
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
    # Modal Edit Dialog
    # -------------------------
    def _open_edit_dialog(self, var: tk.StringVar, label: str, min_val: float, max_val: float, button: ttk.Button):
        """
        Abre un diálogo modal para editar un valor numérico con teclado integrado.
        Optimizado para pantalla táctil en Raspberry Pi.
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

        kbd_frm = ttk.LabelFrame(frm, text="Teclado", padding=6)
        kbd_frm.pack(fill="both", expand=True, pady=(0, 8))

        def add_digit(digit):
            current = var_edit.get()
            var_edit.set(current + str(digit))
            entry.focus()
            entry.update()

        def add_decimal():
            current = var_edit.get()
            if "." not in current:
                var_edit.set(current + ".")
            entry.focus()
            entry.update()

        def delete_last():
            current = var_edit.get()
            var_edit.set(current[:-1] if current else "")
            entry.focus()
            entry.update()

        def clear_all():
            var_edit.set("")
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

                if valor < min_val or valor > max_val:
                    raise ValueError(f"Valor fuera de rango [{min_val}, {max_val}]")

                var.set(str(valor))
                button.config(text=f"[{valor}]")

                dialog.destroy()
            except ValueError as e:
                messagebox.showerror("Error", f"Valor inválido: {str(e)}")

        def on_cancel():
            dialog.destroy()

        ttk.Button(action_frm, text="✓ Guardar", command=on_save).pack(side="left", padx=2, pady=2, fill="both", expand=True)
        ttk.Button(action_frm, text="✕ Cancelar", command=on_cancel).pack(side="left", padx=2, pady=2, fill="both", expand=True)

        entry.bind("<Return>", lambda e: on_save())
        entry.bind("<Escape>", lambda e: on_cancel())

        dialog.wait_window()

    def _open_edit_dialog_sp(self):
        """Abre modal para editar SP con aplicación automática"""
        button = self.btn_sp
        var = self.var_sp
        label = "SP (kPa)"
        min_val = 0
        max_val = 500

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

        kbd_frm = ttk.LabelFrame(frm, text="Teclado", padding=6)
        kbd_frm.pack(fill="both", expand=True, pady=(0, 8))

        def add_digit(digit):
            current = var_edit.get()
            var_edit.set(current + str(digit))
            entry.focus()
            entry.update()

        def add_decimal():
            current = var_edit.get()
            if "." not in current:
                var_edit.set(current + ".")
            entry.focus()
            entry.update()

        def delete_last():
            current = var_edit.get()
            var_edit.set(current[:-1] if current else "")
            entry.focus()
            entry.update()

        def clear_all():
            var_edit.set("")
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
        tk.Button(row_frm, text="←", width=btn_width, height=btn_height, command=delete_last,
                  font=btn_font, relief="raised", bd=1).pack(side="left", padx=1, pady=1, expand=True, fill="both")

        ttk.Button(kbd_frm, text="Borrar todo", command=clear_all).pack(fill="x", padx=2, pady=3)

        action_frm = ttk.Frame(frm)
        action_frm.pack(fill="x", pady=(6, 0))

        def on_save():
            try:
                valor = float(var_edit.get().strip().replace(",", "."))

                if valor < min_val or valor > max_val:
                    raise ValueError(f"Valor fuera de rango [{min_val}, {max_val}]")

                var.set(str(valor))
                button.config(text=f"[{valor}]")
                self._apply_sp()

                dialog.destroy()
            except ValueError as e:
                messagebox.showerror("Error", f"Valor inválido: {str(e)}")

        def on_cancel():
            dialog.destroy()

        ttk.Button(action_frm, text="✓ Guardar", command=on_save).pack(side="left", padx=2, pady=2, fill="both", expand=True)
        ttk.Button(action_frm, text="✕ Cancelar", command=on_cancel).pack(side="left", padx=2, pady=2, fill="both", expand=True)

        entry.bind("<Return>", lambda e: on_save())
        entry.bind("<Escape>", lambda e: on_cancel())

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

        if mode == "A0":
            self.lbl_sigmin.configure(text="V mín")
            self.lbl_sigmax.configure(text="V máx")
            # defaults típicos (solo si quieres):
            # self.var_sigmin.set("0.000"); self.var_sigmax.set("10.000")
        else:
            self.lbl_sigmin.configure(text="I mín")
            self.lbl_sigmax.configure(text="I máx")
            # self.var_sigmin.set("4.000"); self.var_sigmax.set("20.000")

    def _do_tare(self):
        try:
            p_corr = self._read_pressure_corr_kpa()
            self.rt.p_zero_kpa = p_corr
            messagebox.showinfo("TARA", f"Tara aplicada.\nAhora P≈0 desde Pcorr={p_corr:.2f} kPa")
        except Exception as e:
            messagebox.showerror("TARA", f"No se pudo aplicar tara: {e}")

    # Solo aplica SP con botón/Enter
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
    # Lecturas
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
    # Loop
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
                sp = float(self.cfg.sp_kpa)  # SP aplicado
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