# control_pi.py
# -*- coding: utf-8 -*-

from dataclasses import dataclass
from typing import Optional

def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


@dataclass
class PIConfig:
    kp: float
    ki: float
    dt: float
    u_min: float
    u_max: float
    deadband_kpa: float
    u_ff: float
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
        self.last_u: float = clamp(self.cfg.u_ff, self.cfg.u_min, self.cfg.u_max)
        self.frozen: bool = False
        self.last_sp: Optional[float] = None
        self.last_p: Optional[float] = None

        # filtro opcional de P (si lo quieres aquí en vez de en otro lado)
        self._p_filt: Optional[float] = None

    def freeze(self) -> None:
        self.frozen = True

    def unfreeze(self) -> None:
        self.frozen = False

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

        # --- Zona muerta (idéntico a tu script) ---
        if abs(e) <= self.cfg.deadband_kpa:
            self.I *= self.cfg.i_decay_in_deadband
            u = clamp(self.cfg.u_ff + self.I, self.cfg.u_min, self.cfg.u_max)
            self.last_u = u
            self.last_sp = sp
            self.last_p = p_use
            return u

        # --- PI con anti-windup por "pushing" (idéntico a tu script) ---
        u_unsat = self.cfg.u_ff + self.cfg.kp * e + self.I

        pushing_high = (u_unsat > self.cfg.u_max and e > 0.0)
        pushing_low  = (u_unsat < self.cfg.u_min and e < 0.0)

        if not (pushing_high or pushing_low):
            self.I += self.cfg.ki * e * dt

        u = clamp(self.cfg.u_ff + self.cfg.kp * e + self.I, self.cfg.u_min, self.cfg.u_max)

        self.last_u = u
        self.last_sp = sp
        self.last_p = p_use
        return u
