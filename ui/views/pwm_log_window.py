# -*- coding: utf-8 -*-

import os
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox, filedialog
from typing import Callable, List, Optional, Tuple

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from config import hardware as config


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class PwmLogWindow(tk.Toplevel):
    def __init__(
        self,
        master,
        *,
        read_pressure_kpa: Callable[[], float],
        apply_u_cmd: Callable[[float], None],
        get_pwm_freq_hz: Optional[Callable[[], float]] = None,
        set_pwm_freq_hz: Optional[Callable[[float], float]] = None,
        safe_stop: Callable[[], None],
        on_start: Optional[Callable[[], None]] = None,
        on_end: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(master)
        self.title("LOG PWM -> CSV")
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())

        self._read_pressure_kpa = read_pressure_kpa
        self._apply_u_cmd = apply_u_cmd
        self._get_pwm_freq_hz = get_pwm_freq_hz
        self._set_pwm_freq_hz = set_pwm_freq_hz
        self._safe_stop = safe_stop
        self._on_start = on_start
        self._on_end = on_end

        self.var_u_cmd = tk.StringVar(value="0.20")
        self.var_duration = tk.StringVar(value="60")
        init_freq_hz = float(getattr(config, "PWM_FREQ_HZ", 90))
        if self._get_pwm_freq_hz is not None:
            try:
                init_freq_hz = float(self._get_pwm_freq_hz())
            except Exception:
                init_freq_hz = float(getattr(config, "PWM_FREQ_HZ", 90))
        self.var_pwm_freq_hz = tk.StringVar(value=f"{init_freq_hz:.0f}")
        self.var_status = tk.StringVar(value="IDLE")

        self._abort_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._rows: List[Tuple[float, float, float]] = []
        self._running = False
        self._elapsed = 0.0
        self._last_p = 0.0
        self._last_u_cmd = 0.0
        self._done_state: Optional[str] = None
        self._done_error: Optional[str] = None
        self._last_export: Optional[str] = None
        self._cfg_u_cmd = 0.20
        self._live_u_cmd = 0.20
        self._cfg_duration = 60.0
        self._cfg_pwm_freq_hz = init_freq_hz
        self._last_png: Optional[str] = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._poll_ui)

    def _build_ui(self) -> None:
        frm = ttk.Frame(self, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frm, text="u_cmd (0..1)").grid(row=0, column=0, sticky="w", pady=2)
        self.ent_u_cmd = ttk.Entry(frm, textvariable=self.var_u_cmd, width=14)
        self.ent_u_cmd.grid(row=0, column=1, sticky="w", pady=2)
        self.ent_u_cmd.bind("<Return>", lambda _e: self._apply_live_pwm())
        self.btn_apply_pwm = ttk.Button(frm, text="Aplicar PWM", command=self._apply_live_pwm)
        self.btn_apply_pwm.grid(row=0, column=2, sticky="w", pady=2, padx=(6, 0))

        ttk.Label(frm, text="duration_s").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.var_duration, width=14).grid(row=1, column=1, sticky="w", pady=2)

        ttk.Label(frm, text="pwm_freq_hz").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.var_pwm_freq_hz, width=14).grid(row=2, column=1, sticky="w", pady=2)

        self.lbl_status = ttk.Label(frm, textvariable=self.var_status)
        self.lbl_status.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 8))

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, sticky="ew")
        btns.grid_columnconfigure(0, weight=1)
        btns.grid_columnconfigure(1, weight=1)
        btns.grid_columnconfigure(2, weight=1)

        self.btn_start = ttk.Button(btns, text="START", command=self._start)
        self.btn_stop = ttk.Button(btns, text="STOP", command=self._stop)
        self.btn_save_png = ttk.Button(btns, text="Guardar grafica", command=self._save_plot_image)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=4)
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_save_png.grid(row=0, column=2, sticky="ew", padx=4)
        self.btn_stop.state(["disabled"])
        self.btn_save_png.state(["disabled"])

        self._figure = Figure(figsize=(5.6, 2.8), dpi=100)
        self._ax = self._figure.add_subplot(111)
        self._line, = self._ax.plot([], [], color="#007acc", linewidth=2.0)
        self._ax.set_title("Presion en vivo")
        self._ax.set_xlabel("Tiempo [s]")
        self._ax.set_ylabel("Presion [kPa]")
        self._ax.grid(True, alpha=0.30)
        self._canvas = FigureCanvasTkAgg(self._figure, master=frm)
        self._canvas.get_tk_widget().grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(8, 0))

        frm.grid_rowconfigure(5, weight=1)
        frm.grid_columnconfigure(0, weight=1)
        frm.grid_columnconfigure(1, weight=1)

    def _parse_inputs(self) -> Tuple[float, float, float]:
        try:
            u_cmd = float(self.var_u_cmd.get().strip().replace(",", "."))
            duration_s = float(self.var_duration.get().strip().replace(",", "."))
            freq_hz = float(self.var_pwm_freq_hz.get().strip().replace(",", "."))
        except Exception:
            raise ValueError("Valores invalidos en u_cmd/duration_s/pwm_freq_hz.")
        u_cmd = _clamp(u_cmd, 0.0, 1.0)
        duration_s = max(0.1, duration_s)
        freq_hz = max(1.0, freq_hz)
        return u_cmd, duration_s, freq_hz

    def _parse_u_cmd(self) -> float:
        try:
            u_cmd = float(self.var_u_cmd.get().strip().replace(",", "."))
        except Exception:
            raise ValueError("Valor invalido en u_cmd.")
        return _clamp(u_cmd, 0.0, 1.0)

    def _apply_live_pwm(self) -> None:
        try:
            u_cmd = self._parse_u_cmd()
        except Exception as e:
            messagebox.showerror("LOG PWM", str(e), parent=self)
            return

        self.var_u_cmd.set(f"{u_cmd:.3f}")
        with self._lock:
            self._live_u_cmd = u_cmd
            running = self._running

        if running:
            self.var_status.set(f"RUNNING... u={u_cmd:.3f}")
        else:
            self.var_status.set(f"IDLE u={u_cmd:.3f}")

    def _start(self) -> None:
        if self._running:
            return
        try:
            u_cmd, duration_s, freq_hz = self._parse_inputs()
        except Exception as e:
            messagebox.showerror("LOG PWM", str(e), parent=self)
            return

        try:
            if self._set_pwm_freq_hz is not None:
                applied_freq_hz = float(self._set_pwm_freq_hz(freq_hz))
            else:
                config.PWM_FREQ_HZ = int(round(freq_hz))
                applied_freq_hz = float(config.PWM_FREQ_HZ)
        except Exception as e:
            messagebox.showerror("LOG PWM", f"No se pudo aplicar pwm_freq_hz: {e}", parent=self)
            return

        self._cfg_u_cmd = u_cmd
        self._cfg_duration = duration_s
        self._cfg_pwm_freq_hz = applied_freq_hz
        self.var_pwm_freq_hz.set(f"{applied_freq_hz:.0f}")
        self._abort_evt.clear()
        with self._lock:
            self._rows = []
            self._running = True
            self._elapsed = 0.0
            self._last_p = 0.0
            self._last_u_cmd = u_cmd
            self._live_u_cmd = u_cmd
            self._done_state = None
            self._done_error = None
            self._last_export = None
            self._last_png = None

        if self._on_start is not None:
            self._on_start()

        self.btn_start.state(["disabled"])
        self.btn_stop.state(["!disabled"])
        self.btn_save_png.state(["disabled"])
        self.var_status.set(f"RUNNING... f={self._cfg_pwm_freq_hz:.0f}Hz")
        self._refresh_plot([])

        self._thread = threading.Thread(
            target=self._run_worker,
            kwargs={"u_cmd": u_cmd, "duration_s": duration_s},
            name="PwmLogWorker",
            daemon=True,
        )
        self._thread.start()

    def _stop(self) -> None:
        self._abort_evt.set()

    def _run_worker(self, *, u_cmd: float, duration_s: float) -> None:
        state = "DONE"
        error_msg = ""
        t0 = time.perf_counter()
        sleep_s = max(0.05, float(getattr(config, "ADS_CONV_DELAY_S", 0.01)))
        applied_u_cmd: Optional[float] = None

        try:
            while not self._abort_evt.is_set():
                t_s = time.perf_counter() - t0
                if t_s >= duration_s:
                    break

                with self._lock:
                    target_u_cmd = float(self._live_u_cmd)
                if (applied_u_cmd is None) or (abs(target_u_cmd - applied_u_cmd) > 1e-9):
                    self._apply_u_cmd(target_u_cmd)
                    applied_u_cmd = target_u_cmd

                p_kpa = float(self._read_pressure_kpa())
                if p_kpa > float(getattr(config, "P_MAX_SEGURIDAD_KPA", 230.0)):
                    state = "ABORT"
                    error_msg = f"OVERPRESSURE: P={p_kpa:.2f} kPa"
                    break

                with self._lock:
                    if applied_u_cmd is None:
                        applied_u_cmd = float(u_cmd)
                    self._rows.append((float(t_s), float(applied_u_cmd), float(p_kpa)))
                    self._elapsed = float(t_s)
                    self._last_p = float(p_kpa)
                    self._last_u_cmd = float(applied_u_cmd)

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
        return os.path.join(self._csv_dir(), f"pwm_log_{ts}_u-{self._cfg_u_cmd:.3f}.csv")

    def _build_png_path(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(self._csv_dir(), f"pwm_log_{ts}_u-{self._cfg_u_cmd:.3f}.png")

    def _u_cmd_to_pwm_hw(self, u_cmd: float) -> float:
        u = _clamp(float(u_cmd), 0.0, 1.0)
        if bool(getattr(config, "BOMBA_ACTIVE_LOW", False)):
            return _clamp(1.0 - u, 0.0, 1.0)
        return u

    def _export_csv(self) -> str:
        with self._lock:
            rows = list(self._rows)
        out = self._build_csv_path()
        with open(out, "w", encoding="utf-8", newline="") as fh:
            fh.write("t_s;u_cmd;pwm_hw;p_kpa\n")
            for t_s, u_cmd, p_kpa in rows:
                u_cmd = _clamp(float(u_cmd), 0.0, 1.0)
                pwm_hw = self._u_cmd_to_pwm_hw(u_cmd)
                t_txt = f"{t_s:.6f}".replace(".", ",")
                u_txt = f"{u_cmd:.6f}".replace(".", ",")
                pwm_txt = f"{pwm_hw:.6f}".replace(".", ",")
                p_txt = f"{p_kpa:.6f}".replace(".", ",")
                fh.write(f"{t_txt};{u_txt};{pwm_txt};{p_txt}\n")
        return out

    def _refresh_plot(self, rows: List[Tuple[float, float, float]]) -> None:
        if not rows:
            self._line.set_data([], [])
            self._ax.set_xlim(0.0, max(1.0, float(self._cfg_duration)))
            self._ax.set_ylim(0.0, max(10.0, float(getattr(config, "P_MAX_SEGURIDAD_KPA", 230.0)) * 0.20))
            self._canvas.draw_idle()
            return

        xs = [r[0] for r in rows]
        ys = [r[2] for r in rows]
        self._line.set_data(xs, ys)

        x_max = max(xs[-1], 1.0)
        self._ax.set_xlim(0.0, max(float(self._cfg_duration), x_max * 1.05))

        y_min = min(ys)
        y_max = max(ys)
        if abs(y_max - y_min) < 1e-9:
            y_pad = max(0.5, abs(y_max) * 0.05 + 0.5)
        else:
            y_pad = max(0.5, (y_max - y_min) * 0.10)
        self._ax.set_ylim(max(0.0, y_min - y_pad), y_max + y_pad)
        self._canvas.draw_idle()

    def _save_plot_image(self) -> None:
        with self._lock:
            rows = list(self._rows)
            running = self._running

        if running:
            messagebox.showwarning("LOG PWM", "Deten el registro antes de guardar la grafica.", parent=self)
            return
        if not rows:
            messagebox.showwarning("LOG PWM", "No hay datos para guardar.", parent=self)
            return

        default_name = os.path.basename(self._build_png_path())
        out_path = filedialog.asksaveasfilename(
            parent=self,
            title="Guardar grafica PNG",
            defaultextension=".png",
            initialdir=self._csv_dir(),
            initialfile=default_name,
            filetypes=[("PNG", "*.png")],
        )
        if not out_path:
            return

        try:
            self._figure.savefig(out_path, dpi=150, bbox_inches="tight")
            self._last_png = out_path
            messagebox.showinfo("LOG PWM", f"Grafica guardada en:\n{out_path}", parent=self)
        except Exception as e:
            messagebox.showerror("LOG PWM", f"No se pudo guardar la grafica:\n{e}", parent=self)

    def _poll_ui(self) -> None:
        done_state = None
        done_error = None
        done_export = None
        rows: List[Tuple[float, float, float]] = []

        with self._lock:
            elapsed = self._elapsed
            p_kpa = self._last_p
            u_cmd = self._last_u_cmd
            running = self._running
            rows = list(self._rows)
            if self._done_state is not None:
                done_state = self._done_state
                done_error = self._done_error
                done_export = self._last_export
                self._done_state = None
                self._done_error = None

        self._refresh_plot(rows)

        if running:
            self.var_status.set(f"RUNNING t={elapsed:.2f}s p={p_kpa:.2f}kPa u={u_cmd:.3f}")

        if done_state is not None:
            self.btn_start.state(["!disabled"])
            self.btn_stop.state(["disabled"])
            if rows:
                self.btn_save_png.state(["!disabled"])
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
                messagebox.showinfo(
                    "LOG PWM",
                    f"CSV guardado en:\n{done_export}\n\nPuedes guardar la grafica con 'Guardar grafica'.",
                    parent=self
                )

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
