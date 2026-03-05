# -*- coding: utf-8 -*-

import os
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox
from typing import Callable, List, Optional, Tuple

from config import hardware as config


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class PwmLogWindow(tk.Toplevel):
    def __init__(
        self,
        master,
        *,
        read_pressure_kpa: Callable[[], float],
        apply_real_pwm: Callable[[float], None],
        safe_stop: Callable[[], None],
        on_start: Optional[Callable[[], None]] = None,
        on_end: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(master)
        self.title("LOG PWM -> CSV")
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())

        self._read_pressure_kpa = read_pressure_kpa
        self._apply_real_pwm = apply_real_pwm
        self._safe_stop = safe_stop
        self._on_start = on_start
        self._on_end = on_end

        self.var_pwm = tk.StringVar(value="0.20")
        self.var_duration = tk.StringVar(value="60")
        self.var_status = tk.StringVar(value="IDLE")

        self._abort_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._rows: List[Tuple[float, float]] = []
        self._running = False
        self._elapsed = 0.0
        self._last_p = 0.0
        self._done_state: Optional[str] = None
        self._done_error: Optional[str] = None
        self._last_export: Optional[str] = None
        self._cfg_pwm = 0.20
        self._cfg_duration = 60.0

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._poll_ui)

    def _build_ui(self) -> None:
        frm = ttk.Frame(self, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frm, text="pwm (0..1)").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.var_pwm, width=14).grid(row=0, column=1, sticky="w", pady=2)

        ttk.Label(frm, text="duration_s").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.var_duration, width=14).grid(row=1, column=1, sticky="w", pady=2)

        self.lbl_status = ttk.Label(frm, textvariable=self.var_status)
        self.lbl_status.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 8))

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=2, sticky="ew")
        btns.grid_columnconfigure(0, weight=1)
        btns.grid_columnconfigure(1, weight=1)

        self.btn_start = ttk.Button(btns, text="START", command=self._start)
        self.btn_stop = ttk.Button(btns, text="STOP", command=self._stop)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=4)
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_stop.state(["disabled"])

    def _parse_inputs(self) -> Tuple[float, float]:
        try:
            pwm = float(self.var_pwm.get().strip().replace(",", "."))
            duration_s = float(self.var_duration.get().strip().replace(",", "."))
        except Exception:
            raise ValueError("Valores invalidos en pwm/duration_s.")
        pwm = _clamp(pwm, 0.0, 1.0)
        duration_s = max(0.1, duration_s)
        return pwm, duration_s

    def _start(self) -> None:
        if self._running:
            return
        try:
            pwm, duration_s = self._parse_inputs()
        except Exception as e:
            messagebox.showerror("LOG PWM", str(e), parent=self)
            return

        self._cfg_pwm = pwm
        self._cfg_duration = duration_s
        self._abort_evt.clear()
        with self._lock:
            self._rows = []
            self._running = True
            self._elapsed = 0.0
            self._last_p = 0.0
            self._done_state = None
            self._done_error = None
            self._last_export = None

        if self._on_start is not None:
            self._on_start()

        self.btn_start.state(["disabled"])
        self.btn_stop.state(["!disabled"])
        self.var_status.set("RUNNING...")

        self._thread = threading.Thread(
            target=self._run_worker,
            kwargs={"pwm": pwm, "duration_s": duration_s},
            name="PwmLogWorker",
            daemon=True,
        )
        self._thread.start()

    def _stop(self) -> None:
        self._abort_evt.set()

    def _run_worker(self, *, pwm: float, duration_s: float) -> None:
        state = "DONE"
        error_msg = ""
        t0 = time.perf_counter()
        sleep_s = max(0.05, float(getattr(config, "ADS_CONV_DELAY_S", 0.01)))

        try:
            self._apply_real_pwm(pwm)
            while not self._abort_evt.is_set():
                t_s = time.perf_counter() - t0
                if t_s >= duration_s:
                    break

                p_kpa = float(self._read_pressure_kpa())
                if p_kpa > float(getattr(config, "P_MAX_SEGURIDAD_KPA", 230.0)):
                    state = "ABORT"
                    error_msg = f"OVERPRESSURE: P={p_kpa:.2f} kPa"
                    break

                with self._lock:
                    self._rows.append((float(t_s), float(p_kpa)))
                    self._elapsed = float(t_s)
                    self._last_p = float(p_kpa)

                time.sleep(sleep_s)

            if self._abort_evt.is_set():
                state = "ABORT"
        except Exception as e:
            state = "ABORT"
            error_msg = str(e)
        finally:
            try:
                self._safe_stop()
            except Exception:
                pass

            export_path = None
            try:
                export_path = self._export_csv()
            except Exception:
                pass

            with self._lock:
                self._running = False
                self._done_state = state
                self._done_error = error_msg
                self._last_export = export_path

    def _csv_dir(self) -> str:
        base = os.path.abspath(getattr(config, "DATA_DIR", "data"))
        out_dir = os.path.join(base, "pwm_logs")
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def _build_csv_path(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(self._csv_dir(), f"pwm_log_{ts}_pwm-{self._cfg_pwm:.3f}.csv")

    def _export_csv(self) -> str:
        with self._lock:
            rows = list(self._rows)
        out = self._build_csv_path()
        with open(out, "w", encoding="utf-8", newline="") as fh:
            fh.write("t_s;p_kpa\n")
            for t_s, p_kpa in rows:
                fh.write(f"{t_s:.6f};{p_kpa:.6f}\n")
        return out

    def _poll_ui(self) -> None:
        done_state = None
        done_error = None
        done_export = None

        with self._lock:
            elapsed = self._elapsed
            p_kpa = self._last_p
            running = self._running
            if self._done_state is not None:
                done_state = self._done_state
                done_error = self._done_error
                done_export = self._last_export
                self._done_state = None
                self._done_error = None

        if running:
            self.var_status.set(f"RUNNING t={elapsed:.2f}s p={p_kpa:.2f}kPa")

        if done_state is not None:
            self.btn_start.state(["!disabled"])
            self.btn_stop.state(["disabled"])
            if self._on_end is not None:
                self._on_end(done_state)
            if done_state == "ABORT":
                msg = "ABORT"
                if done_error:
                    msg += f" | {done_error}"
                self.var_status.set(msg)
            else:
                self.var_status.set("DONE")
            if done_export:
                messagebox.showinfo("LOG PWM", f"CSV guardado en:\n{done_export}", parent=self)

        self.after(120, self._poll_ui)

    def _on_close(self) -> None:
        self._abort_evt.set()
        try:
            self._safe_stop()
        except Exception:
            pass
        if self._on_end is not None:
            self._on_end("ABORT")
        self.destroy()
