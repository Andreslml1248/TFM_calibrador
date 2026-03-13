# Calibrador TFM - Notas rapidas

## Filtros LIVE (A0/A1/A2)
- Config en `config/hardware.py`:
  - `FILTER_LIVE_ENABLE`
  - `A0_MEDIAN_N`, `A0_MEAN_N`
  - `A1_MEDIAN_N`, `A1_MEAN_N`
  - `A2_MEDIAN_N`, `A2_MEAN_N`
- Cadena: Median PtByPt -> Mean PtByPt (por canal, con estado).

## Medicion oficial por punto (AUTO)
- `N_SAMPLES_MEASURE`
- Anti-picos (solo estadistica, sin memoria):
  - `MEASURE_MEDIAN_ENABLE`
  - `MEASURE_MEDIAN_N`
- La medicion no usa el filtro LIVE y no introduce retardo.

## Calibracion 2 puntos (A0/A1)
- Boton en modo manual: **Calibracion 2 puntos (A0/A1)**.
- Captura x1/x2 desde VADC crudo, ingresa y1/y2 reales.
- Calcula `m, b` y guarda en `data/calibration.json`.
- Se carga al iniciar la app; si no existe, usa defaults y avisa.

## FFT / Ruido (Manual)
- Boton en modo manual: **FFT / Ruido**.
- Captura N muestras crudas y grafica espectro.
- Reporta RMS/STD y pico dominante (Hz).
- Parametros: `FFT_N_SAMPLES`, `FFT_USE_WINDOW`, `ADS_SPS`.

## Ethernet directo con LabVIEW
- Para conexion directa por cable con LabVIEW, el PC debe dejar su adaptador Ethernet en obtener IP automaticamente (DHCP).
- LabVIEW debe conectarse siempre a `192.168.50.2`.
- Puertos TCP sin cambios: `5000`, `5001`, `5002`.

