#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
main_v1.py (MANUAL estable, por terminal)
- PI EXACTO al script que te funciona (KP/KI/U_FF + deadband + anti-windup).
- Bomba invertida soportada (BOMBA_ACTIVE_LOW=True).
- TARA (ZERO) obligatoria antes de arrancar el control.
- Comandos en vivo NO bloqueantes:
    sp 60   -> cambia SP
    60      -> cambia SP (también acepta número solo)
    stop    -> vuelve a configuración
    back    -> vuelve a idle
- DUT display:
    A0 = voltaje (0..10V)   (min/max por defecto 0/10)
    A1 = corriente (4..20mA)(min/max por defecto 4/20)
- NUEVO:
    * En setup pide rango de presión del DUT (Pmin/Pmax) y modo (lineal/polinómico)
    * Calcula "P_dut_expected" a partir de la señal (mA/V) y calcula ERROR%FS en vivo:
        err%FS = 100*(P_dut_expected - P_ref)/FS
    * Se ELIMINA el porcentaje viejo (que era % de span eléctrico)
- Ventilador + DS18B20 (hilo global siempre activo mientras corre el programa)
"""

import os
import glob
import time
import threading
from collections import deque

from smbus2 import SMBus
from gpiozero import DigitalOutputDevice, PWMOutputDevice
from gpiozero.pins.lgpio import LGPIOFactory


# ===================== GPIO =====================
PWM_PIN        = 12
RELE_BOMBA_PIN = 17
VALV_PIN       = 27

PWM_FREQ_HZ = 200
BOMBA_ACTIVE_LOW = True   # True: pwm_hw = 1-u (0=ON fuerte, 1=OFF)

USE_VALVULA = True
VALV_ACTIVE_HIGH = True   # True: valvula.on() = energizada = ABIERTA (NC)

# ---- FAN (PWM) ----
FAN_PWM_PIN     = 18      # <-- cambia si tu pin real es otro
FAN_PWM_FREQ_HZ = 200     # <-- lgpio: usa 200/500 Hz típicos
# Reglas temperatura:
TEMP_TARGET_C     = 25.0
TEMP_FULLSPEED_C  = 35.0
TEMP_HYST_C       = 1.0
FAN_FAILSAFE_FULLSPEED = True
FAN_FAILSAFE_PWM = 1.0


# ===================== ADS1115 =====================
ADS_I2C_BUS = 1
ADS_ADDR = 0x48
PGA_V    = 4.096
SPS      = 128

ADS_CH_REF   = 2   # MPX en A2 (tu caso)
ADS_CH_DUT_V = 0   # A0
ADS_CH_DUT_mA= 1   # A1

N_SAMPLES_AVG = 20
SAMPLE_DT_AVG_S = 0.01
ADS_CONV_DELAY_S = 0.01


# ===================== MPX polinomio + 2PT =====================
A2 = 0.27334322
B2 = 106.390322
C2 = -22.167571

USE_2PT   = True
GAIN_2PT  = 1.0150102699300843
OFFSET_2PT= -0.0

P_MAX_SEGURIDAD_KPA = 230.0
VADC_MIN_OK = 0.0
VADC_MAX_OK = 3.35


def p_cal(v: float) -> float:
    return A2*v*v + B2*v + C2


def mpx_v_to_kpa(vadc: float) -> float:
    p_raw = p_cal(vadc)
    if p_raw < 0:
        p_raw = 0.0
    if USE_2PT:
        return GAIN_2PT * p_raw + OFFSET_2PT
    return p_raw


# ===================== DUT calibraciones (las tuyas) =====================
# A0: Vin = 3.235548 * VADC + 0.003870
A0_VIN_GAIN   = 3.235548
A0_VIN_OFFSET = 0.003870

# A1: ImA = 4.945630 * VADC + 4.038358
A1_IMA_GAIN   = 4.945630
A1_IMA_OFFSET = 4.038358


def dut_eng_from_vadc(mode: str, vadc: float) -> float:
    mode = mode.strip().upper()
    if mode == "A0":
        return A0_VIN_GAIN * vadc + A0_VIN_OFFSET
    if mode == "A1":
        return A1_IMA_GAIN * vadc + A1_IMA_OFFSET
    return vadc


# ===================== CONTROL PI (idéntico al tuyo) =====================
KP = 0.010
KI = 0.00071

DT = 0.05
U_MIN = 0.0
U_MAX = 1.0

DEADBAND_KPA = 0.5
U_FF = 0.38
P_FILT_ALPHA = 1.0


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


# ===================== ADS1115 low-level =====================
def ads_cfg_word(ch: int, pga_v: float, sps: int) -> int:
    osb = 1 << 15
    mux_map = {0:0b100, 1:0b101, 2:0b110, 3:0b111}
    mux = mux_map.get(ch, 0b110) << 12

    pga_map = {6.144:0b000, 4.096:0b001, 2.048:0b010, 1.024:0b011, 0.512:0b100, 0.256:0b101}
    pga = pga_map.get(pga_v, 0b001) << 9

    mode = 1 << 8
    dr_map = {8:0b000,16:0b001,32:0b010,64:0b011,128:0b100,250:0b101,475:0b110,860:0b111}
    dr = dr_map.get(sps, 0b100) << 5

    return osb | mux | pga | mode | dr | 0b11


def ads_read_v_once(bus: SMBus, ch: int) -> float:
    cfg = ads_cfg_word(ch, PGA_V, SPS)
    bus.write_i2c_block_data(ADS_ADDR, 0x01, [(cfg>>8)&0xFF, cfg&0xFF])
    time.sleep(ADS_CONV_DELAY_S)
    d = bus.read_i2c_block_data(ADS_ADDR, 0x00, 2)
    raw = (d[0] << 8) | d[1]
    if raw & 0x8000:
        raw -= 1 << 16
    return (raw / 32768.0) * PGA_V


def ads_read_v_avg(bus: SMBus, ch: int, n: int, dt_s: float) -> float:
    s = 0.0
    for _ in range(max(1, n)):
        s += ads_read_v_once(bus, ch)
        if dt_s > 0:
            time.sleep(dt_s)
    return s / max(1, n)


# ===================== DS18B20 + Fan PWM =====================
def find_ds18b20_device() -> str:
    base = "/sys/bus/w1/devices"
    cand = glob.glob(os.path.join(base, "28-*"))
    return cand[0] if cand else ""


def read_ds18b20_c(device_dir: str) -> float:
    path = os.path.join(device_dir, "w1_slave")
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    if len(lines) < 2 or "YES" not in lines[0]:
        raise RuntimeError("DS18B20 CRC FAIL")
    pos = lines[1].find("t=")
    if pos < 0:
        raise RuntimeError("DS18B20 format")
    t_milli = int(lines[1][pos + 2 :])
    return t_milli / 1000.0


def fan_pwm_from_temp(t_c: float) -> float:
    if t_c <= TEMP_TARGET_C:
        return 0.0
    if t_c >= TEMP_FULLSPEED_C:
        return 1.0
    return (t_c - TEMP_TARGET_C) / (TEMP_FULLSPEED_C - TEMP_TARGET_C)


class FanMonitor(threading.Thread):
    def __init__(self, fan_pwm_dev: PWMOutputDevice):
        super().__init__(daemon=True)
        self.fan = fan_pwm_dev
        self.devdir = find_ds18b20_device()
        self.last_temp = None
        self.last_pwm = 0.0
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        # Si no hay sensor: failsafe
        if not self.devdir:
            if FAN_FAILSAFE_FULLSPEED:
                self.last_pwm = clamp(FAN_FAILSAFE_PWM, 0.0, 1.0)
                self.fan.value = self.last_pwm
            return

        fan_on = False

        while not self._stop:
            try:
                t_c = read_ds18b20_c(self.devdir)
                self.last_temp = t_c

                # histéresis simple para encender/apagar
                if not fan_on and t_c >= TEMP_TARGET_C:
                    fan_on = True
                if fan_on and t_c <= (TEMP_TARGET_C - TEMP_HYST_C):
                    fan_on = False

                pwm = 0.0 if not fan_on else fan_pwm_from_temp(t_c)
                self.last_pwm = clamp(pwm, 0.0, 1.0)
                self.fan.value = self.last_pwm

            except Exception:
                if FAN_FAILSAFE_FULLSPEED:
                    self.last_pwm = clamp(FAN_FAILSAFE_PWM, 0.0, 1.0)
                    self.fan.value = self.last_pwm

            time.sleep(1.0)


# ===================== Hilo de comandos (NO bloquea) =====================
class CommandInput(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.q = deque()
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        while not self._stop:
            try:
                s = input().strip()
                if s:
                    self.q.append(s)
            except Exception:
                pass


# ===================== Hardware wrapper =====================
class HW:
    def __init__(self):
        self.factory = LGPIOFactory()

        self.rele_bomba = DigitalOutputDevice(RELE_BOMBA_PIN, pin_factory=self.factory)
        self.pwm_bomba  = PWMOutputDevice(PWM_PIN, frequency=PWM_FREQ_HZ, pin_factory=self.factory)

        self.valvula = None
        if USE_VALVULA:
            self.valvula = DigitalOutputDevice(VALV_PIN, pin_factory=self.factory)

        # FAN
        self.fan_pwm = PWMOutputDevice(FAN_PWM_PIN, frequency=FAN_PWM_FREQ_HZ, pin_factory=self.factory)
        self.fan_mon = FanMonitor(self.fan_pwm)

        self.bus = SMBus(ADS_I2C_BUS)

        # Estado seguro al arrancar
        self.bomba_off()
        self.set_ev_open()

        # Arranca ventilador global
        self.fan_mon.start()

    def set_ev_open(self):
        if self.valvula is None:
            return
        if VALV_ACTIVE_HIGH:
            self.valvula.on()
        else:
            self.valvula.off()

    def set_ev_closed(self):
        if self.valvula is None:
            return
        if VALV_ACTIVE_HIGH:
            self.valvula.off()
        else:
            self.valvula.on()

    def bomba_power(self, on: bool):
        if on:
            self.rele_bomba.on()
        else:
            self.rele_bomba.off()

    def bomba_set_u(self, u_cmd: float):
        u = clamp(u_cmd, 0.0, 1.0)
        pwm_hw = (1.0 - u) if BOMBA_ACTIVE_LOW else u
        self.pwm_bomba.value = clamp(pwm_hw, 0.0, 1.0)

    def bomba_off(self):
        # OFF real: PWM + relé
        try:
            self.pwm_bomba.value = 1.0 if BOMBA_ACTIVE_LOW else 0.0
        except Exception:
            pass
        try:
            self.rele_bomba.off()
        except Exception:
            pass

    def close(self):
        try:
            self.bomba_off()
        except Exception:
            pass
        try:
            self.set_ev_closed()
        except Exception:
            pass
        try:
            self.fan_mon.stop()
        except Exception:
            pass
        try:
            self.fan_pwm.value = 0.0
        except Exception:
            pass
        try:
            self.bus.close()
        except Exception:
            pass


# ===================== DUT model: señal -> presión esperada =====================
def dut_expected_pressure_kpa(
    eng_value: float,
    eng_min: float,
    eng_max: float,
    p_min_kpa: float,
    p_max_kpa: float,
    mode_curve: str,
    poly_a: float,
    poly_b: float,
    poly_c: float
) -> float:
    """
    Devuelve la presión "que debería marcar" el DUT según la señal.
    - Lineal: mapea eng_min..eng_max a p_min..p_max
    - Polinómico: P = a*x^2 + b*x + c (x = señal en V o mA)
      (OJO: este polinomio es del DUT, NO del MPX)
    """
    if eng_max == eng_min:
        eng_max = eng_min + 1e-9

    mode_curve = (mode_curve or "L").strip().upper()
    if mode_curve.startswith("P"):
        # polinómico
        p = poly_a*(eng_value**2) + poly_b*eng_value + poly_c
        return p

    # lineal (default)
    frac = (eng_value - eng_min) / (eng_max - eng_min)
    p = p_min_kpa + frac * (p_max_kpa - p_min_kpa)
    return p


# ===================== Estados =====================
S1_SAFE_IDLE     = "S1_SAFE_IDLE"
S2_CONFIG_MANUAL = "S2_CONFIG_MANUAL"
S3_PI_CONTROL    = "S3_PI_CONTROL"


def main():
    hw = HW()

    ctx = {
        "dut_mode": "A1",   # A0 o A1
        "sp_kpa": 60.0,

        # señal eléctrica min/max
        "min_eng": 4.0,
        "max_eng": 20.0,

        # rango presión DUT
        "p_min_kpa": 0.0,
        "p_max_kpa": 200.0,

        # curva DUT
        "curve": "L",       # L=Lineal, P=Polinómico
        "poly_a": 0.0,      # si curva=P
        "poly_b": 0.0,
        "poly_c": 0.0,

        # tara
        "p_zero": 0.0,
    }

    state = S1_SAFE_IDLE

    # Variables PI (idéntico al tuyo)
    I = 0.0
    p_f = None

    try:
        while True:

            # ===================== IDLE =====================
            if state == S1_SAFE_IDLE:
                hw.bomba_off()
                hw.set_ev_open()

                v_ref = ads_read_v_avg(hw.bus, ADS_CH_REF, N_SAMPLES_AVG, SAMPLE_DT_AVG_S)
                p_ref = mpx_v_to_kpa(v_ref)

                t_txt = ""
                if hw.fan_mon.last_temp is not None:
                    t_txt = f" | T={hw.fan_mon.last_temp:.1f}C fan={hw.fan_mon.last_pwm:.2f}"

                print(f"\n[m]=Manual  [q]=Salir  (P_ref={p_ref:.1f} kPa{t_txt})")
                cmd = input("> ").strip().lower()
                if cmd == "m":
                    state = S2_CONFIG_MANUAL
                elif cmd == "q":
                    break
                else:
                    continue

            # ===================== CONFIG =====================
            elif state == S2_CONFIG_MANUAL:
                hw.bomba_off()
                hw.set_ev_open()

                dm = input("DUT A0=V / A1=mA [A1]: ").strip().upper()
                if dm not in ("A0", "A1", ""):
                    dm = "A1"
                ctx["dut_mode"] = dm if dm else "A1"

                s = input(f"SP kPa [{ctx['sp_kpa']:.0f}]: ").strip().replace(",", ".")
                if s:
                    ctx["sp_kpa"] = max(0.0, float(s))

                # ---- rango presión DUT ----
                s = input(f"Presión mínima DUT (kPa) [{ctx['p_min_kpa']:.1f}]: ").strip().replace(",", ".")
                if s:
                    ctx["p_min_kpa"] = float(s)

                s = input(f"Presión máxima DUT (kPa) [{ctx['p_max_kpa']:.1f}]: ").strip().replace(",", ".")
                if s:
                    ctx["p_max_kpa"] = float(s)

                if ctx["p_max_kpa"] == ctx["p_min_kpa"]:
                    ctx["p_max_kpa"] = ctx["p_min_kpa"] + 1.0
                if ctx["p_max_kpa"] < ctx["p_min_kpa"]:
                    ctx["p_min_kpa"], ctx["p_max_kpa"] = ctx["p_max_kpa"], ctx["p_min_kpa"]

                # ---- señal eléctrica min/max (siempre pregunta) ----
                if ctx["dut_mode"] == "A0":
                    default_min, default_max = 0.0, 10.0
                    unit = "V"
                else:
                    default_min, default_max = 4.0, 20.0
                    unit = "mA"

                mn = input(f"Señal mínima ({unit}) [{default_min:.1f}]: ").strip().replace(",", ".")
                mx = input(f"Señal máxima ({unit}) [{default_max:.1f}]: ").strip().replace(",", ".")

                ctx["min_eng"] = float(mn) if mn else default_min
                ctx["max_eng"] = float(mx) if mx else default_max

                if ctx["max_eng"] == ctx["min_eng"]:
                    ctx["max_eng"] = ctx["min_eng"] + 1.0

                # ---- curva ----
                c = input("Curva DUT: [L]=Lineal / [P]=Polinómico [L]: ").strip().upper()
                ctx["curve"] = c if c in ("L", "P") else "L"

                if ctx["curve"] == "P":
                    s = input(f"Polinomio DUT: a (P=a*x^2+b*x+c) [a={ctx['poly_a']}]: ").strip().replace(",", ".")
                    if s:
                        ctx["poly_a"] = float(s)
                    s = input(f"Polinomio DUT: b [b={ctx['poly_b']}]: ").strip().replace(",", ".")
                    if s:
                        ctx["poly_b"] = float(s)
                    s = input(f"Polinomio DUT: c [c={ctx['poly_c']}]: ").strip().replace(",", ".")
                    if s:
                        ctx["poly_c"] = float(s)
                else:
                    ctx["poly_a"] = 0.0
                    ctx["poly_b"] = 0.0
                    ctx["poly_c"] = 0.0

                print("\n[s]=START  [b]=BACK")
                cmd = input("> ").strip().lower()
                if cmd == "s":
                    state = S3_PI_CONTROL
                else:
                    state = S1_SAFE_IDLE

            # ===================== CONTROL =====================
            elif state == S3_PI_CONTROL:
                hw.bomba_off()
                hw.set_ev_open()

                input("\nAsegura 0 kPa. ENTER para TARA.\n")

                v0 = ads_read_v_avg(hw.bus, ADS_CH_REF, 10, 0.01)
                p0 = mpx_v_to_kpa(v0)
                ctx["p_zero"] = p0

                I = 0.0
                p_f = None

                hw.bomba_power(True)

                print("\nCONTROL ACTIVO → sp 60 | 60 | stop | back")
                cmd_thread = CommandInput()
                cmd_thread.start()

                last = time.time()
                last_print = 0.0

                while True:
                    now = time.time()
                    dt = now - last
                    if dt <= 0:
                        dt = DT
                    last = now

                    # ---- presión referencia (MPX) con tara ----
                    v = ads_read_v_avg(hw.bus, ADS_CH_REF, 5, 0.0)
                    if v < VADC_MIN_OK or v > VADC_MAX_OK:
                        raise RuntimeError(f"MPX VADC fuera de rango: {v:.3f} V")

                    p_corr = mpx_v_to_kpa(v)
                    if p_corr >= P_MAX_SEGURIDAD_KPA:
                        raise RuntimeError(f"OVERPRESSURE: Pcorr={p_corr:.2f} kPa")

                    p_meas = p_corr - ctx["p_zero"]
                    if p_meas < 0:
                        p_meas = 0.0

                    # filtro
                    if p_f is None:
                        p_f = p_meas
                    else:
                        p_f = P_FILT_ALPHA * p_meas + (1.0 - P_FILT_ALPHA) * p_f
                    p = p_f

                    # ---- PI idéntico ----
                    sp = float(ctx["sp_kpa"])
                    e = sp - p

                    if abs(e) <= DEADBAND_KPA:
                        I *= 0.97
                        u = clamp(U_FF + I, U_MIN, U_MAX)
                    else:
                        u_unsat = U_FF + KP*e + I
                        pushing_high = (u_unsat > U_MAX and e > 0)
                        pushing_low  = (u_unsat < U_MIN and e < 0)

                        if not (pushing_high or pushing_low):
                            I += KI * e * dt

                        u = clamp(U_FF + KP*e + I, U_MIN, U_MAX)

                    hw.bomba_set_u(u)

                    # ---- DUT señal -> presión esperada -> error%FS ----
                    ch = ADS_CH_DUT_V if ctx["dut_mode"] == "A0" else ADS_CH_DUT_mA
                    v_dut = ads_read_v_avg(hw.bus, ch, 5, 0.0)
                    eng = dut_eng_from_vadc(ctx["dut_mode"], v_dut)  # V o mA

                    p_dut = dut_expected_pressure_kpa(
                        eng_value=eng,
                        eng_min=float(ctx["min_eng"]),
                        eng_max=float(ctx["max_eng"]),
                        p_min_kpa=float(ctx["p_min_kpa"]),
                        p_max_kpa=float(ctx["p_max_kpa"]),
                        mode_curve=str(ctx["curve"]),
                        poly_a=float(ctx["poly_a"]),
                        poly_b=float(ctx["poly_b"]),
                        poly_c=float(ctx["poly_c"]),
                    )

                    fs = float(ctx["p_max_kpa"] - ctx["p_min_kpa"])
                    if fs <= 0:
                        fs = 1.0

                    err_kpa = p_dut - p
                    err_pct_fs = 100.0 * err_kpa / fs

                    # texto temperatura/ventilador
                    t_txt = ""
                    if hw.fan_mon.last_temp is not None:
                        t_txt = f" | T={hw.fan_mon.last_temp:.1f}C fan={hw.fan_mon.last_pwm:.2f}"

                    if (now - last_print) >= 1.0:
                        last_print = now
                        tag = "V" if ctx["dut_mode"] == "A0" else "mA"
                        curve_tag = "LIN" if ctx["curve"] == "L" else "POLY"
                        print(
                            f"P={p:6.2f} | SP={sp:6.2f} | u={u:5.3f} | "
                            f"DUT={eng:6.3f} {tag} | Pdut={p_dut:6.2f} | "
                            f"Err={err_kpa:+6.2f} kPa | Err%FS={err_pct_fs:+6.2f}% ({curve_tag}){t_txt}"
                        )

                    # ---- comandos ----
                    while cmd_thread.q:
                        cmd = cmd_thread.q.popleft().strip().lower()

                        if cmd == "stop":
                            cmd_thread.stop()
                            hw.bomba_off()
                            state = S2_CONFIG_MANUAL
                            break

                        if cmd == "back":
                            cmd_thread.stop()
                            hw.bomba_off()
                            state = S1_SAFE_IDLE
                            break

                        if cmd.startswith("sp"):
                            try:
                                val = float(cmd.split()[1].replace(",", "."))
                                ctx["sp_kpa"] = max(0.0, val)
                                print(f"[SP] {ctx['sp_kpa']:.2f} kPa")
                            except Exception:
                                print("Formato: sp 60")
                            continue

                        try:
                            val = float(cmd.replace(",", "."))
                            ctx["sp_kpa"] = max(0.0, val)
                            print(f"[SP] {ctx['sp_kpa']:.2f} kPa")
                        except Exception:
                            print("Comandos: sp 60 | 60 | stop | back")

                    if state != S3_PI_CONTROL:
                        break

                    spent = time.time() - now
                    sleep_t = DT - spent
                    if sleep_t > 0:
                        time.sleep(sleep_t)

    except KeyboardInterrupt:
        print("\n[STOP] Ctrl+C")
    finally:
        hw.close()
        print("[DONE]")


if __name__ == "__main__":
    main()
