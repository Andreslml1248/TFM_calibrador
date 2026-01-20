# config.py
# -*- coding: utf-8 -*-

from dataclasses import dataclass
from typing import List

# ============================
# GPIO / ACTUADORES
# ============================
# Bomba: PWM + relé de potencia (24V)
PWM_PIN: int = 12
PWM_FREQ_HZ: int = 200
RELE_BOMBA_PIN: int = 17

# Electroválvula NC: energizar = abrir ✅
VALV_PIN: int = 27
USE_VALVULA: bool = True
VALV_ACTIVE_HIGH: bool = True  # True => ON = energizada = ABIERTA

# Bomba invertida (como tus pruebas)
# True: PWM_hw = 1-u  (u=0 -> ON fuerte, u=1 -> OFF)
BOMBA_ACTIVE_LOW: bool = True

# ============================
# VENTILACIÓN + TEMPERATURA (DS18B20)
# ============================
FAN_PWM_PIN: int = 18
FAN_PWM_FREQ_HZ: int = 200

# DS18B20 se lee por 1-Wire (w1). GPIO donde está cableado/habilitado.
DS18B20_GPIO: int = 4

# Objetivo térmico (tu regla)
TEMP_TARGET_C: float = 25.0       # desde aquí empieza a subir el PWM
TEMP_FULLSPEED_C: float = 35.0    # a esta temp: ventilador al 100%
TEMP_HYST_C: float = 0.5          # histéresis para evitar parpadeo

# Antes de 25°C: apagado
FAN_PWM_MIN: float = 0.0
FAN_PWM_MAX: float = 1.0

# Si el sensor de temperatura falla:
FAN_FAILSAFE_FULLSPEED: bool = True
FAN_FAILSAFE_PWM: float = 1.0

# ============================
# SEGURIDAD / LÍMITES
# ============================
P_MAX_SEGURIDAD_KPA: float = 230.0
P_MIN_KPA: float = -5.0  # opcional (detectar sensor raro)

# ============================
# ADS1115
# ============================
ADS_I2C_BUS: int = 1
ADS_ADDR: int = 0x48
ADS_PGA_V: float = 4.096
ADS_SPS: int = 128
ADS_CONV_DELAY_S: float = 0.01

# Rango “realista” para detectar fallos en VADC (single-ended en Pi suele ser 0..3.3V)
VADC_MIN_OK: float = 0.0
VADC_MAX_OK: float = 3.35

# Mapeo de canales ADS1115
# A0 = DUT voltaje (0–10V escalado) -> VADC (V)
# A1 = DUT corriente (4–20mA por shunt) -> VADC (V)
# A2 = MPX referencia -> VADC (V)
ADS_CH_DUT_V: int = 0
ADS_CH_DUT_mA: int = 1
ADS_CH_REF: int = 2

# Sí: MPX se lee desde ADS
USE_ADS_REF: bool = True

# ============================
# CALIBRACIÓN ENTRADAS DUT (ECUACIONES LINEALES)
# ============================
# A0: Vin = 3.235548 * VADC + 0.003870
A0_VIN_GAIN: float = 3.235548
A0_VIN_OFFSET: float = 0.003870
USE_A0_CAL: bool = True

# A1: ImA = 4.945630 * VADC + 4.038358
A1_IMA_GAIN: float = 4.945630
A1_IMA_OFFSET: float = 4.038358
USE_A1_CAL: bool = True

# ============================
# MUESTREO
# ============================
# Promedio rápido para control/UI (S1–S7)
N_SAMPLES_AVG: int = 20
SAMPLE_DT_AVG_S: float = 0.01

# Medición oficial por punto (S8)
N_SAMPLES_MEASURE: int = 50
SAMPLE_DT_MEASURE_S: float = 0.01

# ============================
# MPX5500DP (V -> kPa)
# ============================
# Polinomio que vienes usando:
MPX_A2: float = 0.27334322
MPX_B2: float = 106.390322
MPX_C2: float = -22.167571

# Corrección 2PT (si está activada)
USE_2PT: bool = True
GAIN_2PT: float = 1.0150102699300843
OFFSET_2PT: float = 0.0

# ============================
# PI CONTROL (un solo PI para manual y auto)
# ============================
KP_DEFAULT: float = 0.010
KI_DEFAULT: float = 0.00071

DT_PI: float = 0.05
U_MIN: float = 0.0
U_MAX: float = 1.0

DEADBAND_KPA: float = 1
U_FF: float = 0.38
P_FILT_ALPHA: float = 1.0  # 1.0 = sin filtro

@dataclass
class PIConfig:
    kp: float = KP_DEFAULT
    ki: float = KI_DEFAULT
    dt: float = DT_PI
    u_min: float = U_MIN
    u_max: float = U_MAX
    deadband_kpa: float = DEADBAND_KPA
    u_ff: float = U_FF
    p_filt_alpha: float = P_FILT_ALPHA

PI_CFG = PIConfig()

# ============================
# AUTO (S5/S6) - Ventanas / tiempos por defecto
# ============================
TOL_KPA_DEFAULT: float = 1.0
T_GOTO_MAX_S_DEFAULT: int = 30
T_IN_WINDOW_S_DEFAULT: int = 2
T_STABLE_OK_S_DEFAULT: int = 5
T_RETRY_S_DEFAULT: int = 5

T_STABLE_OPTIONS_S: List[int] = [2, 3, 5, 15, 30, 120]

# ============================
# UI / PLOT / PATHS
# ============================
PLOT_WINDOW_SEC: int = 60

DATA_DIR: str = "data"
CERT_DIR: str = "certs"
LOG_DIR: str = "logs"
