# core/control.py
# -*- coding: utf-8 -*-

from dataclasses import dataclass
import threading
from typing import Optional
from config import hardware as hw_config

def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def pwm_real_to_u_cmd(pwm_real: float) -> float:
    pwm = clamp(float(pwm_real), 0.0, 1.0)
    return 1.0 - pwm if bool(getattr(hw_config, "BOMBA_ACTIVE_LOW", False)) else pwm


@dataclass
class PIConfig:
    kp: float
    ki: float
    dt: float
    u_min: float
    u_max: float
    deadband_kpa: float
    u_ff: float
    kp_low: float = float(getattr(hw_config, "KP_LOW", 0.0006))
    ki_low: float = float(getattr(hw_config, "KI_LOW", 0.00004))
    kp_mid: float = float(getattr(hw_config, "KP_MID", 0.0009))
    ki_mid: float = float(getattr(hw_config, "KI_MID", 0.00007))
    kp_high: float = float(getattr(hw_config, "KP_HIGH", 0.0012))
    ki_high: float = float(getattr(hw_config, "KI_HIGH", 0.00010))
    hold_band_kpa: float = 0.0
    kp_hold: float = 0.0
    ki_hold: float = 0.0
    p_filt_alpha: float = 1.0          # por si luego filtras P aquí (opcional)
    i_decay_in_deadband: float = 0.97  # igual que tu script: I *= 0.97


class PIController:
    """
    PI para presión (kPa).
    - reset(): borra integrador y estado.
    - freeze(): congela (no actualiza integrador ni u).
    - unfreeze(): reanuda.
    - step(sp, p, dt): calcula u_cmd en [u_min, u_max].
      (La inversión BOMBA_ACTIVE_LOW se aplica fuera, al generar PWM_hw.)
    """

    def __init__(self, cfg: PIConfig):
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        self.I: float = 0.0
        self.last_u: float = pwm_real_to_u_cmd(0.0)
        self.frozen: bool = False
        self.last_sp: Optional[float] = None
        self.last_p: Optional[float] = None

        # filtro opcional de P (si lo quieres aquí en vez de en otro lado)
        self._p_filt: Optional[float] = None

    def freeze(self) -> None:
        self.frozen = True

    def unfreeze(self) -> None:
        self.frozen = False

    def _select_zone_gains(self, sp_kpa: float) -> tuple[float, float]:
        sp = float(sp_kpa)
        if sp < 30.0:
            return float(self.cfg.kp_low), float(self.cfg.ki_low)
        if sp < 100.0:
            return float(self.cfg.kp_mid), float(self.cfg.ki_mid)
        return float(self.cfg.kp_high), float(self.cfg.ki_high)

    def step(self, sp_kpa: float, p_kpa: float, dt: Optional[float] = None) -> float:
        if self.frozen:
            return self.last_u

        if dt is None or dt <= 0.0:
            dt = self.cfg.dt

        sp = float(sp_kpa)
        p = float(p_kpa)

        # (Opcional) filtro 1er orden sobre presión
        a = float(self.cfg.p_filt_alpha)
        if a >= 1.0:
            p_use = p
        else:
            if self._p_filt is None:
                self._p_filt = p
            else:
                self._p_filt = a * p + (1.0 - a) * self._p_filt
            p_use = self._p_filt

        e = sp - p_use
        kp_use, ki_use = self._select_zone_gains(sp)
        u_unsat = kp_use * e + self.I

        pushing_high = (u_unsat > self.cfg.u_max and e > 0.0)
        pushing_low  = (u_unsat < self.cfg.u_min and e < 0.0)

        if not (pushing_high or pushing_low):
            self.I += ki_use * e * dt

        u = clamp(kp_use * e + self.I, self.cfg.u_min, self.cfg.u_max)

        self.last_u = u
        self.last_sp = sp
        self.last_p = p_use
        return u


class PIWorker:
    """
    Ejecuta PIController en un hilo dedicado.
    - set_inputs(sp, p, dt): publica la ultima entrada para calcular.
    - get_output(): devuelve la ultima salida calculada.
    """

    def __init__(self, controller: PIController, period_s: float):
        self.controller = controller
        self.period_s = max(0.001, float(period_s))

        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._new_input_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._has_input = False
        self._sp = 0.0
        self._p = 0.0
        self._dt: Optional[float] = None
        self._last_u = self.controller.last_u

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="PIWorker", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 1.0) -> None:
        self._stop_evt.set()
        self._new_input_evt.set()
        t = self._thread
        if t is not None:
            t.join(timeout=max(0.0, float(timeout_s)))
        self._thread = None

    def set_inputs(self, sp_kpa: float, p_kpa: float, dt: Optional[float] = None) -> None:
        with self._lock:
            self._sp = float(sp_kpa)
            self._p = float(p_kpa)
            self._dt = dt
            self._has_input = True
        self._new_input_evt.set()

    def get_output(self) -> float:
        with self._lock:
            return float(self._last_u)

    def step_now(self, sp_kpa: float, p_kpa: float, dt: Optional[float] = None) -> float:
        """Calcula una iteracion de PI de forma sincrona y actualiza la ultima salida."""
        with self._lock:
            u = self.controller.step(sp_kpa=float(sp_kpa), p_kpa=float(p_kpa), dt=dt)
            self._last_u = float(u)
            return float(u)

    def reset(self) -> None:
        with self._lock:
            self.controller.reset()
            self._last_u = self.controller.last_u

    def freeze(self) -> None:
        with self._lock:
            self.controller.freeze()

    def unfreeze(self) -> None:
        with self._lock:
            self.controller.unfreeze()

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            signaled = self._new_input_evt.wait(timeout=self.period_s)

            if self._stop_evt.is_set():
                break

            if not signaled:
                # No llego input nuevo: NO recalcular
                continue

            self._new_input_evt.clear()

            with self._lock:
                if not self._has_input:
                    continue
                sp = self._sp
                p = self._p
                dt = self._dt
                u = self.controller.step(sp_kpa=sp, p_kpa=p, dt=dt)
                self._last_u = float(u)

