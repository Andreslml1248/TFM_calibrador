# -*- coding: utf-8 -*-

import csv
import os
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox
from typing import Callable, Dict, List, Optional

from config import hardware as config


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class StepTestWindow(tk.Toplevel):
    def __init__(
        self,
        master,
        *,
        read_pressure_kpa: Callable[[], float],
        set_pump: Callable[[float], None],
        set_relay: Callable[[bool], None],
        set_valve: Callable[[bool], None],
        get_sp_kpa: Callable[[], Optional[float]],
        get_pwm_hw: Optional[Callable[[], Optional[float]]] = None,
        on_test_start: Optional[Callable[[], None]] = None,
        on_test_end: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(master)
        self.title("STEP TEST (K,L,tau)")
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())

        self._read_pressure_kpa = read_pressure_kpa
        self._set_pump = set_pump
        self._set_relay = set_relay
        self._set_valve = set_valve
        self._get_sp_kpa = get_sp_kpa
        self._get_pwm_hw = get_pwm_hw
        self._on_test_start = on_test_start
        self._on_test_end = on_test_end

        self._abort_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._rows: List[Dict[str, object]] = []
        self._running = False
        self._state = "IDLE"
        self._elapsed_s = 0.0
        self._p_kpa = 0.0
        self._u_cmd = 1.0 if bool(getattr(config, "BOMBA_ACTIVE_LOW", True)) else 0.0
        self._last_dt_measured_s = 0.0
        self._thread_done_state: Optional[str] = None
        self._thread_done_error: Optional[str] = None
        self._last_export_path: Optional[str] = None
        self._last_export_cfg: Optional[Dict[str, float]] = None

        self.var_u0 = tk.StringVar(value="0.20")
        self.var_u1 = tk.StringVar(value="0.40")
        self.var_t_pre = tk.StringVar(value="5.0")
        self.var_t_total = tk.StringVar(value="60.0")
        self.var_sample = tk.StringVar(value="0.08")
        self.var_pwm_hw_real = tk.BooleanVar(value=False)

        self.var_elapsed = tk.StringVar(value="0.00 s")
        self.var_pressure = tk.StringVar(value="0.00 kPa")
        self.var_state = tk.StringVar(value="IDLE")
        self.var_u_applied = tk.StringVar(value="u=1.000")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._poll_ui)

    def _build_ui(self) -> None:
        frm = ttk.Frame(self, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frm, text="u0 (0..1)").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.var_u0, width=12).grid(row=0, column=1, sticky="w", pady=2)

        ttk.Label(frm, text="u1 (0..1)").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.var_u1, width=12).grid(row=1, column=1, sticky="w", pady=2)

        ttk.Label(frm, text="t_pre_s").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.var_t_pre, width=12).grid(row=2, column=1, sticky="w", pady=2)

        ttk.Label(frm, text="t_total_s").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.var_t_total, width=12).grid(row=3, column=1, sticky="w", pady=2)

        ttk.Label(frm, text="sample_period_s").grid(row=4, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.var_sample, width=12).grid(row=4, column=1, sticky="w", pady=2)

        ttk.Checkbutton(
            frm,
            text="Registrar pwm_hw real",
            variable=self.var_pwm_hw_real,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 6))

        status_box = ttk.LabelFrame(frm, text="Live")
        status_box.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        status_box.grid_columnconfigure(1, weight=1)

        ttk.Label(status_box, text="Tiempo").grid(row=0, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(status_box, textvariable=self.var_elapsed).grid(row=0, column=1, sticky="w", padx=6, pady=2)

        ttk.Label(status_box, text="Presion").grid(row=1, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(status_box, textvariable=self.var_pressure).grid(row=1, column=1, sticky="w", padx=6, pady=2)

        ttk.Label(status_box, text="Estado").grid(row=2, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(status_box, textvariable=self.var_state).grid(row=2, column=1, sticky="w", padx=6, pady=2)

        ttk.Label(status_box, text="u aplicado").grid(row=3, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(status_box, textvariable=self.var_u_applied).grid(row=3, column=1, sticky="w", padx=6, pady=2)

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=2, sticky="ew")
        btns.grid_columnconfigure(0, weight=1)
        btns.grid_columnconfigure(1, weight=1)
        btns.grid_columnconfigure(2, weight=1)

        self.btn_start = ttk.Button(btns, text="Start", command=self._start_test)
        self.btn_abort = ttk.Button(btns, text="Abort", command=self._abort_test)
        self.btn_export = ttk.Button(btns, text="Export CSV", command=self._export_csv_button)

        self.btn_start.grid(row=0, column=0, sticky="ew", padx=4)
        self.btn_abort.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_export.grid(row=0, column=2, sticky="ew", padx=4)
        self.btn_abort.state(["disabled"])

    def _parse_cfg(self) -> Dict[str, float]:
        def f(var: tk.StringVar, name: str) -> float:
            try:
                return float(var.get().strip().replace(",", "."))
            except Exception:
                raise ValueError(f"{name}: valor invalido.")

        u0 = _clamp(f(self.var_u0, "u0"), 0.0, 1.0)
        u1 = _clamp(f(self.var_u1, "u1"), 0.0, 1.0)
        t_pre = max(0.0, f(self.var_t_pre, "t_pre_s"))
        t_total = max(0.1, f(self.var_t_total, "t_total_s"))
        sample = max(0.01, f(self.var_sample, "sample_period_s"))
        if t_pre > t_total:
            raise ValueError("t_pre_s no puede ser mayor que t_total_s.")
        return {
            "u0": u0,
            "u1": u1,
            "t_pre_s": t_pre,
            "t_total_s": t_total,
            "sample_period_s": sample,
        }

    def _start_test(self) -> None:
        if self._running:
            return
        try:
            cfg = self._parse_cfg()
        except Exception as e:
            messagebox.showerror("STEP TEST", str(e), parent=self)
            return

        self._abort_evt.clear()
        with self._lock:
            self._rows = []
            self._running = True
            self._state = "PRE"
            self._elapsed_s = 0.0
            self._p_kpa = 0.0
            self._u_cmd = cfg["u0"]
            self._last_dt_measured_s = 0.0
            self._thread_done_state = None
            self._thread_done_error = None
            self._last_export_cfg = dict(cfg)

        if self._on_test_start is not None:
            self._on_test_start()

        self.btn_start.state(["disabled"])
        self.btn_abort.state(["!disabled"])
        self.btn_export.state(["disabled"])

        register_pwm_hw_real = bool(self.var_pwm_hw_real.get())

        self._thread = threading.Thread(
            target=self._run_test,
            kwargs={"cfg": cfg, "register_pwm_hw_real": register_pwm_hw_real},
            name="StepTestWorker",
            daemon=True,
        )
        self._thread.start()

    def _abort_test(self) -> None:
        self._abort_evt.set()

    def _safe_outputs(self) -> None:
        u_off = 1.0 if bool(getattr(config, "BOMBA_ACTIVE_LOW", True)) else 0.0
        try:
            self._set_pump(u_off)
        except Exception:
            pass
        try:
            self._set_relay(False)
        except Exception:
            pass
        try:
            self._set_valve(True)
        except Exception:
            pass

    def _read_pwm_hw(self) -> Optional[float]:
        if self._get_pwm_hw is None:
            return None
        try:
            v = self._get_pwm_hw()
            return None if v is None else float(v)
        except Exception:
            return None

    def _row_from_sample(
        self,
        *,
        t_s: float,
        mode: str,
        p_kpa: float,
        u_cmd: float,
        dt_measured_s: float,
        register_pwm_hw_real: bool,
    ) -> Dict[str, object]:
        sp = self._get_sp_kpa()
        pwm_hw = self._read_pwm_hw() if register_pwm_hw_real else u_cmd
        if pwm_hw is None:
            pwm_hw = u_cmd
        return {
            "t_s": float(t_s),
            "ts_iso": datetime.now().isoformat(timespec="milliseconds"),
            "sp_kpa": "" if sp is None else float(sp),
            "p_kpa": float(p_kpa),
            "u_cmd": float(u_cmd),
            "pwm_hw": float(pwm_hw),
            "mode": mode,
            "dt_measured_s": float(dt_measured_s),
            "notes": "",
        }

    def _run_test(self, *, cfg: Dict[str, float], register_pwm_hw_real: bool) -> None:
        t_pre_s = float(cfg["t_pre_s"])
        t_total_s = float(cfg["t_total_s"])
        sample_period_s = float(cfg["sample_period_s"])
        u0 = float(cfg["u0"])
        u1 = float(cfg["u1"])

        start_mono = time.monotonic()
        last_sample_ts: Optional[float] = None
        sample_idx = 0
        end_state = "DONE"
        end_error: Optional[str] = None

        try:
            self._set_valve(False)
            self._set_relay(True)

            while not self._abort_evt.is_set():
                now_mono = time.monotonic()
                elapsed = now_mono - start_mono
                if elapsed >= t_total_s:
                    break

                mode = "PRE" if elapsed < t_pre_s else "STEP"
                u_cmd = u0 if mode == "PRE" else u1
                self._set_pump(u_cmd)

                p_kpa = float(self._read_pressure_kpa())
                if p_kpa > float(getattr(config, "P_MAX_SEGURIDAD_KPA", 230.0)):
                    end_state = "ABORT"
                    end_error = f"OVERPRESSURE: P={p_kpa:.2f} kPa"
                    break

                dt_measured_s = 0.0 if last_sample_ts is None else (now_mono - last_sample_ts)
                last_sample_ts = now_mono

                row = self._row_from_sample(
                    t_s=elapsed,
                    mode=mode,
                    p_kpa=p_kpa,
                    u_cmd=u_cmd,
                    dt_measured_s=dt_measured_s,
                    register_pwm_hw_real=register_pwm_hw_real,
                )

                with self._lock:
                    self._rows.append(row)
                    self._state = mode
                    self._elapsed_s = elapsed
                    self._p_kpa = p_kpa
                    self._u_cmd = u_cmd
                    self._last_dt_measured_s = dt_measured_s

                sample_idx += 1
                target_ts = start_mono + sample_idx * sample_period_s
                wait_s = max(0.0, target_ts - time.monotonic())
                if self._abort_evt.wait(wait_s):
                    break

            if self._abort_evt.is_set():
                end_state = "ABORT"
        except Exception as e:
            end_state = "ABORT"
            end_error = str(e)
        finally:
            self._safe_outputs()

            auto_export_path = None
            try:
                auto_export_path = self._export_csv()
            except Exception:
                auto_export_path = None

            with self._lock:
                self._running = False
                self._state = end_state
                self._thread_done_state = end_state
                self._thread_done_error = end_error
                if auto_export_path:
                    self._last_export_path = auto_export_path

    def _csv_dir(self) -> str:
        root = os.path.abspath(getattr(config, "DATA_DIR", "data"))
        out_dir = os.path.join(root, "step_tests")
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def _build_default_csv_path(self) -> str:
        cfg = self._last_export_cfg or self._parse_cfg()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        u0_txt = f"{float(cfg['u0']):.3f}"
        u1_txt = f"{float(cfg['u1']):.3f}"
        name = f"step_test_{ts}_u0-{u0_txt}_u1-{u1_txt}.csv"
        return os.path.join(self._csv_dir(), name)

    def _export_csv(self, path: Optional[str] = None) -> str:
        with self._lock:
            rows = list(self._rows)
            cfg_local = dict(self._last_export_cfg or {})

        if not rows:
            raise RuntimeError("No hay datos para exportar.")

        if not cfg_local:
            cfg_local = self._parse_cfg()

        out_path = path or self._build_default_csv_path()
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        header_cfg = {
            "u0": cfg_local.get("u0", ""),
            "u1": cfg_local.get("u1", ""),
            "t_pre_s": cfg_local.get("t_pre_s", ""),
            "t_total_s": cfg_local.get("t_total_s", ""),
            "sample_period_s": cfg_local.get("sample_period_s", ""),
            "ADS_SPS": getattr(config, "ADS_SPS", ""),
            "ADS_CONV_DELAY_S": getattr(config, "ADS_CONV_DELAY_S", ""),
            "BOMBA_ACTIVE_LOW": getattr(config, "BOMBA_ACTIVE_LOW", ""),
            "VALV_ACTIVE_HIGH": getattr(config, "VALV_ACTIVE_HIGH", ""),
        }

        cols = [
            "t_s",
            "ts_iso",
            "sp_kpa",
            "p_kpa",
            "u_cmd",
            "pwm_hw",
            "mode",
            "dt_measured_s",
            "notes",
        ]

        with open(out_path, "w", encoding="utf-8", newline="") as fh:
            for k, v in header_cfg.items():
                fh.write(f"# {k}={v}\n")
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for row in rows:
                w.writerow(row)

        self._last_export_path = out_path
        return out_path

    def _export_csv_button(self) -> None:
        try:
            out_path = self._export_csv()
        except Exception as e:
            messagebox.showerror("STEP TEST", str(e), parent=self)
            return
        messagebox.showinfo("STEP TEST", f"CSV guardado en:\n{out_path}", parent=self)

    def _poll_ui(self) -> None:
        done_state = None
        done_error = None
        with self._lock:
            self.var_elapsed.set(f"{self._elapsed_s:.2f} s")
            self.var_pressure.set(f"{self._p_kpa:.2f} kPa")
            self.var_state.set(self._state)
            self.var_u_applied.set(f"u={self._u_cmd:.3f}")
            if self._thread_done_state is not None:
                done_state = self._thread_done_state
                done_error = self._thread_done_error
                self._thread_done_state = None
                self._thread_done_error = None

        if done_state is not None:
            self.btn_start.state(["!disabled"])
            self.btn_abort.state(["disabled"])
            self.btn_export.state(["!disabled"])
            if self._on_test_end is not None:
                self._on_test_end(done_state)

            if done_state == "ABORT":
                msg = "Ensayo abortado."
                if done_error:
                    msg += f"\n{done_error}"
                messagebox.showwarning("STEP TEST", msg, parent=self)
            else:
                if self._last_export_path:
                    messagebox.showinfo(
                        "STEP TEST",
                        f"Ensayo finalizado.\nCSV: {self._last_export_path}",
                        parent=self,
                    )

        self.after(120, self._poll_ui)

    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno(
                "STEP TEST",
                "El ensayo esta corriendo. Deseas abortar y cerrar?",
                parent=self,
            ):
                return
            self._abort_evt.set()
        self.destroy()
