# TFM Calibrador - Estructura del Proyecto y Fórmulas

## Descripción General

Sistema automático de calibración de transductores de presión (0-230 kPa) en Raspberry Pi con interfaz gráfica. El proyecto controla una bomba hidráulica mediante un controlador PI de presión, aplica puntos de calibración automáticamente y registra los resultados en PDF.

---

## Estructura del Proyecto

```
TFM_calibrador/
├── main.py                          # Punto de entrada
├── config/
│   └── hardware.py                  # Configuración centralizada (parámetros, límites, fórmulas)
├── core/
│   ├── control.py                   # Controlador PI de presión
│   ├── ads1115.py                   # Driver ADC (ADS1115 por I2C)
│   ├── hw.py                        # Wrapper GPIO/PWM/relé/ADC
│   └── mocks.py                     # Mocks para desarrollo en Windows
├── ui/
│   ├── app.py                       # Punto entrada GUI (Tkinter)
│   ├── event_handler.py             # Gestor de eventos
│   └── views/
│       ├── manual.py                # Modo manual (control presión en tiempo real)
│       └── auto.py                  # Modo automático (calibración multi-punto)
└── test_*.py                        # Tests varios
```

---

## 1. CONFIGURACIÓN (config/hardware.py)

### 1.1 Hardware: GPIO, PWM, Relé, Válvula

| Parámetro | Valor | Descripción |
|-----------|-------|-------------|
| `PWM_PIN` | 12 | GPIO para PWM bomba |
| `PWM_FREQ_HZ` | 200 | Frecuencia PWM (Hz) |
| `RELE_BOMBA_PIN` | 17 | GPIO relé de potencia |
| `VALV_PIN` | 27 | GPIO electroválvula (NC normalmente cerrada) |
| `BOMBA_ACTIVE_LOW` | True | Inversión PWM: `pwm_hw = 1 - u_cmd` |

### 1.2 Conversión de Entradas (Calibración Lineal)

**Canal A0 (DUT Voltaje 0-10V):**
```
V_DUT = A0_VIN_GAIN × VADC + A0_VIN_OFFSET
V_DUT = 3.235548 × VADC + 0.003870
```

**Canal A1 (DUT Corriente 4-20mA):**
```
I_DUT = A1_IMA_GAIN × VADC + A1_IMA_OFFSET
I_DUT = 4.945630 × VADC + 4.038358
```

### 1.3 Sensor de Presión (MPX5500DP: V → kPa)

**Polinomio cuadrático:**
```
P_raw = A2 × VADC² + B2 × VADC + C2

donde:
  A2 = 0.27334322
  B2 = 106.390322
  C2 = -22.167571
```

**Corrección 2PT (ajuste de dos puntos):**
```
Si USE_2PT = True:
  P_corregida = GAIN_2PT × P_raw + OFFSET_2PT
  
  GAIN_2PT = 1.0150102699300843
  OFFSET_2PT = 0.0
```

### 1.4 Parámetros del Controlador PI

| Parámetro | Valor | Significado |
|-----------|-------|------------|
| `KP_DEFAULT` | 0.010 | Ganancia proporcional |
| `KI_DEFAULT` | 0.00071 | Ganancia integral |
| `DT_PI` | 0.05 | Período de control (50 ms) |
| `U_MIN` | 0.0 | Límite mínimo salida control |
| `U_MAX` | 1.0 | Límite máximo salida control |
| `U_FF` | 0.38 | Feedforward nominal (estimado) |
| `DEADBAND_KPA` | 1.0 | Banda muerta (±1 kPa) |
| `P_FILT_ALPHA` | 1.0 | Factor filtro 1er orden (1=sin filtro) |

### 1.5 Seguridad y Límites

| Parámetro | Valor |
|-----------|-------|
| `P_MAX_SEGURIDAD_KPA` | 230.0 |
| `P_MIN_KPA` | -5.0 (opcional) |

---

## 2. CONTROLADOR PI (core/control.py)

### 2.1 Ecuaciones del PI

**Error:**
```
e(t) = SP - P(t)
  donde: SP = setpoint (presión deseada)
         P(t) = presión actual (kPa)
```

**Zona Muerta:**
```
Si |e| ≤ deadband_kpa:
  I *= i_decay_in_deadband (típico: 0.97)
  u = clamp(u_ff + I, u_min, u_max)
  (No se integra nuevo error)
```

**PI Standard con Anti-windup:**
```
u_unsat = u_ff + Kp × e + I

Detección de saturación (pushing):
  pushing_high = (u_unsat > u_max AND e > 0)
  pushing_low  = (u_unsat < u_min AND e < 0)

Si NO hay pushing:
  I += Ki × e × dt

u = clamp(u_ff + Kp × e + I, u_min, u_max)
```

### 2.2 Filtro de Presión (Opcional)

```
Si p_filt_alpha < 1.0:
  P_filtrada = alpha × P_actual + (1 - alpha) × P_anterior
```

### 2.3 Estados del Controlador

- `reset()` → I=0, frozen=False
- `freeze()` → No actualiza u, retorna último valor
- `unfreeze()` → Reanuda control
- `step(sp, p, dt)` → Calcula u_cmd

---

## 3. MUESTREO ADC (core/ads1115.py)

### 3.1 Lectura ADC

**Configuración para ADS1115:**
- Bus I2C: 1 (dirección 0x48)
- Canales:
  - `ADS_CH_REF = 2` → Sensor MPX (presión)
  - `ADS_CH_DUT_V = 0` → DUT voltaje (A0)
  - `ADS_CH_DUT_mA = 1` → DUT corriente (A1)

**Parámetros ADC:**
```
PGA_V = 4.096 V    (rango de entrada ADC)
SPS = 128           (samples/segundo)
CONV_DELAY_S = 0.01 (tiempo conversión)
```

**Conversión ADC → Voltaje:**
```
VADC = (valor_bruto_16bit / 32768) × PGA_V
```

### 3.2 Muestreo Promediado

| Config | Valor | Uso |
|--------|-------|-----|
| `N_SAMPLES_AVG` | 20 | Control en tiempo real |
| `SAMPLE_DT_AVG_S` | 0.01 | Intervalo entre muestras (10 ms) |
| `N_SAMPLES_MEASURE` | 50 | Medición oficial de punto |
| `SAMPLE_DT_MEASURE_S` | 0.01 | Intervalo (10 ms) |

---

## 4. MODO MANUAL (ui/views/manual.py)

### 4.1 Propósito

Control manual de la presión en tiempo real. El usuario establece un setpoint (SP) y el PI mantiene la presión en ese nivel. Se registran las lecturas de presión y DUT en vivo.

### 4.2 Lecturas Mostradas

**%SPAN (Porcentaje de Rango de Señal):**
```
%SPAN = 100 × (DUT_eng - sig_min) / (sig_max - sig_min)
  donde:
    sig_min = 4.0 mA (o 0V para A0)
    sig_max = 20.0 mA (o 10V para A0)
```

**%ERROR (Estilo Fluke - Error porcentual relativo al span):**
```
P_pct = 100 × (P_medida - P_min) / (P_max - P_min)
sig_pct = 100 × (DUT - sig_min) / (sig_max - sig_min)

%ERROR = sig_pct - P_pct
```

### 4.3 Bucle Control (tick)

1. Leer presión: `P_corr = MPX_VADC_to_kPA(vadc_ref) - p_zero_tara`
2. Leer DUT: `DUT = calibración_lineal(vadc_dut)`
3. Si P > P_max_seguridad → ERROR
4. Si running: `u = PI.step(sp, p)` → `set_pump(u)`
5. Calcular %SPAN, %ERROR
6. Mostrar valores en vivo

### 4.4 Tara (Cero)

Almacena la presión actual como referencia:
```
p_zero_kpa = lectura_actual
P_resultante = P_corregida - p_zero_kpa
```

---

## 5. MODO AUTOMÁTICO (ui/views/auto.py)

### 5.1 Propósito

Calibración automática multi-punto. Programa una serie de puntos de presión, sube/baja automáticamente manteniendo cada punto estable y registra los datos de DUT para post-procesamiento.

### 5.2 Generación de Puntos

**Para N puntos entre P_min y P_max:**

```python
# Caso n=5 (más común):
puntos_base = [0.0, 0.25×P_max, 0.5×P_max, 0.75×P_max, P_max]

# Según dirección:
direction == "UP"   → puntos_base (ascendente)
direction == "DOWN" → reversed(puntos_base) (descendente)
direction == "BOTH" → puntos_base + reversed(puntos_base[:-1])
                      (sube y baja, sin repetir máximo)
```

### 5.3 Máquina de Estados

```
IDLE
  ↓
ZERO_VENT (despresurizamos, abrimos EV)
  ↓
ZERO_HOLD (esperamos settle_time para que presión sea estable)
  ↓
GOTO_SP (control PI hacia el punto de calibración)
  ├─ Subida (SP > P): bomba activada, EV cerrada
  └─ Bajada (SP < P): bomba OFF, EV abierta
  ↓
IN_BAND_WAIT_UP o IN_BAND_WAIT_DOWN (dentro de banda muerta)
  ├─ UP: espera inband_up_s sin salir de la banda
  └─ DOWN: espera inband_down_s
  ↓
DOWN_CLOSE_DELAY (bajada específica: retardo de 0.5s antes de cerrar EV)
  ↓
HOLD_MEASURE (esperamos settle_time_max_s, luego medimos)
  ↓
Siguiente punto o FINISHED
```

### 5.4 Condiciones de Control (editable en ventana)

| Parámetro | Rango | Descripción |
|-----------|-------|------------|
| `deadband_kpa` | 0-20 | Banda muerta ajustable (por defecto 3 kPa) |
| `inband_up_s` | 0-30 | Tiempo en banda durante subida |
| `inband_down_s` | 0-30 | Tiempo en banda durante bajada |
| `valve_close_delay_s` | FIJO 0.5 | Retardo cierre EV en bajada |

### 5.5 Medición de Punto

Cuando se alcanza estabilidad, se ejecuta `_record_point_result(sp_kpa)`:

```python
# Tomar N_SAMPLES_MEASURE muestras (50 por defecto)
para cada muestra:
  vadc_ref = lectura_adc(canal_presión)
  p = MPX_VADC_to_kPA(vadc_ref) - p_zero_kpa
  
  vadc_dut = lectura_adc(canal_dut)
  dut_eng = calibración_lineal(vadc_dut)
  
  almacenar (p, dut_eng)
  dormir 10ms

# Calcular estadísticas:
p_media = media(lista_p)
p_desv = std(lista_p)
dut_media = media(lista_dut)
dut_desv = std(lista_dut)

span_pct = 100 × (dut_media - sig_min) / (sig_max - sig_min)
err_pct = %ERROR_fluke(p_media, dut_media)
```

### 5.6 Resultados: Ajuste Lineal y R²

Después de tomar todas las mediciones:

```
Datos: (P_medida, DUT_medido) para cada punto

Ajuste lineal: y = m × x + b
  m, b = polyfit(P, DUT, grado=1)

Residuos: y_pred = m × x + b
  SS_res = Σ(y - y_pred)²
  SS_tot = Σ(y - media(y))²

Coeficiente de determinación:
  R² = 1 - (SS_res / SS_tot)
```

**Ecuación mostrada en gráfica:**
```
y = {m:.4f} × x + {b:.4f} | R² = {r2:.4f}
```

### 5.7 Exportación PDF

Los resultados se guardan automáticamente en `resultados_calibracion/calibracion_{timestamp}.pdf` con:
- Tabla de resultados (i, SP, P, σP, DUT, σDUT, %SPAN, %ERROR, u)
- Gráfica lineal con datos y ajuste
- Ecuación lineal y R²

---

## 6. FÓRMULAS DE CÁLCULO - RESUMEN CRÍTICO

### 6.1 Cadena de Conversión Presión

```
VADC_raw (0-4.096V)
    ↓ [ads1115.py]
VADC (voltaje ADC)
    ↓ [manual.py / auto.py → _mpx_vadc_to_kpa]
P_raw = A2×VADC² + B2×VADC + C2
    ↓
P_corregida = GAIN_2PT × P_raw (si USE_2PT=True)
    ↓
P_calibracion = P_corregida - p_zero_kpa (tara)
```

### 6.2 Cadena de Conversión DUT

```
VADC (0-3.3V desde ADS)
    ↓ [manual.py / auto.py → _dut_vadc_to_eng]
DUT = GAIN × VADC + OFFSET
    │
    ├─ A0: V_DUT = 3.235548 × VADC + 0.003870
    └─ A1: I_DUT = 4.945630 × VADC + 4.038358
```

### 6.3 Métricas de Rendimiento

```
%SPAN = 100 × (DUT - sig_min) / (sig_max - sig_min)

P_pct = 100 × (P - P_min) / (P_max - P_min)
sig_pct = 100 × (DUT - sig_min) / (sig_max - sig_min)

%ERROR = sig_pct - P_pct
```

### 6.4 Control PI

```
e = SP - P

SI |e| ≤ deadband:
  I *= 0.97
  u = clamp(u_ff + I, u_min, u_max)
SINO:
  Si NO pushing:
    I += Ki × e × dt
  u_unsat = u_ff + Kp×e + I
  u = clamp(u_unsat, u_min, u_max)
```

---

## 7. ARCHIVOS CLAVE Y RESPONSABILIDADES

| Archivo | Función Principal |
|---------|------------------|
| `main.py` | Inicializa App(Tkinter) |
| `config/hardware.py` | Parámetros, coeficientes, límites |
| `core/control.py` | Controlador PI puro (sin I/O) |
| `core/ads1115.py` | Lectura ADC bruta (sin conversión) |
| `core/hw.py` | Abstracción GPIO/PWM/relé/ADC |
| `ui/app.py` | Navegación entre vistas (manual/auto) |
| `ui/views/manual.py` | Control en tiempo real con PI |
| `ui/views/auto.py` | Máquina de estados para calibración multi-punto |

---

## 8. FLUJO TÍPICO DE CALIBRACIÓN

1. **Inicio:** Usuario selecciona MODO AUTOMÁTICO
2. **Config:** Define rangos de presión, número de puntos, dirección
3. **TARA:** Se almacena P_zero (presión en reposo)
4. **START:** Se genera lista de puntos y comienza máquina de estados
5. **ZERO_VENT → GOTO_SP:** Se sube/baja presión hacia cada punto
6. **IN_BAND_WAIT:** Se aguarda estabilidad dentro de banda muerta
7. **HOLD_MEASURE:** Se toman 50 muestras, se promedian y se registran
8. **Siguiente Punto:** Se repite para cada punto
9. **FINISHED:** Se muestra gráfica con ajuste lineal, R² y tabla de resultados
10. **PDF:** Se exporta automáticamente a `resultados_calibracion/`

---

## 9. CONSIDERACIONES ESPECIALES

### 9.1 Inversión PWM (Bomba Activa en Bajo)

```
BOMBA_ACTIVE_LOW = True →
pwm_hw = 1 - u_cmd

Ejemplo: Si u_cmd = 0.38 (feedforward)
        → pwm_hw = 0.62 (bomba activada al 62%)
```

### 9.2 Zona Muerta e Integrador

En la banda muerta, el integrador **decae** (multiplicado por 0.97) para evitar windup cuando el error es muy pequeño.

### 9.3 Detección de Pushing (Anti-windup Automático)

Si el sistema quiere salir de los límites [u_min, u_max] en la dirección del error, no se integra:
- **Pushing alto:** u_unsat > u_max y e > 0 → No integrar
- **Pushing bajo:** u_unsat < u_min y e < 0 → No integrar

### 9.4 Cierre Retardado en Bajada

Cuando la presión desciende y entra en banda, hay un retardo de **0.5 segundos** antes de cerrar la electroválvula. Esto permite amortiguación y estabilidad.

### 9.5 Filtrado de Presión (Opcional)

Si `p_filt_alpha < 1.0`, se aplica filtro exponencial de primer orden:
```
P_filt = alpha × P_actual + (1 - alpha) × P_anterior
```
(Por defecto desactivado: alpha = 1.0)

---

## 10. RESUMEN DE FÓRMULAS MATEMÁTICAS

### Conversión Analógica
- **MPX5500DP (Presión):** P = 0.273×VADC² + 106.39×VADC - 22.17
- **Calibración Lineal DUT-A0:** V = 3.236×VADC + 0.00387
- **Calibración Lineal DUT-A1:** I = 4.946×VADC + 4.038

### Control PI
- **Error:** e = SP - P
- **Integrador:** I(n+1) = I(n) + Ki×e×dt (si no hay pushing)
- **Comando:** u = clamp(u_ff + Kp×e + I, 0, 1)

### Métricas
- **%SPAN:** (DUT - sig_min) / (sig_max - sig_min) × 100
- **%ERROR:** [(DUT-sig_min)/(sig_max-sig_min) - (P-P_min)/(P_max-P_min)] × 100
- **R²:** 1 - Σ(y-ŷ)² / Σ(y-μ)²

---

## Conclusión

Este proyecto implementa un **calibrador automático de transductores de presión** basado en control PI retroalimentado. El énfasis está en:

1. **Precisión de conversiones:** Fórmulas polinomiales + lineales para presión y DUT
2. **Control robusto:** PI con anti-windup, zona muerta y feedforward
3. **Automatización:** Máquina de estados que ejecuta secuencias complejas
4. **Análisis estadístico:** Ajuste lineal y cálculo de R² para validar transductores

Las fórmulas críticas están en `config/hardware.py` y las implementaciones en `core/control.py` y `ui/views/*.py`.

