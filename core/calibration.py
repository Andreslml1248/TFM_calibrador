#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
core/calibration.py
Carga/guarda calibracion 2 puntos y aplica coeficientes en config.
"""

import json
import os
from typing import Dict, Tuple

from config import hardware as config


def two_point_cal(x1: float, y1: float, x2: float, y2: float) -> Tuple[float, float]:
    if float(x2) == float(x1):
        raise ValueError("x1 y x2 no pueden ser iguales.")
    m = (float(y2) - float(y1)) / (float(x2) - float(x1))
    b = float(y1) - m * float(x1)
    return float(m), float(b)


def _default_calibration() -> Dict[str, Dict[str, float]]:
    return {
        "A0": {"m": float(config.A0_CAL_M), "b": float(config.A0_CAL_B), "units": "V_in"},
        "A1": {"m": float(config.A1_CAL_M), "b": float(config.A1_CAL_B), "units": "mA"},
        "A2": {"m": float(config.GAIN_2PT), "b": float(config.OFFSET_2PT), "units": "kPa"},
    }


def _calibration_path() -> str:
    return os.path.join(str(config.DATA_DIR), "calibration.json")


def save_calibration(cal: Dict[str, Dict[str, float]]) -> None:
    path = _calibration_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cal, f, indent=2, sort_keys=True)


def load_calibration() -> Tuple[Dict[str, Dict[str, float]], bool]:
    """
    Devuelve (cal, loaded_from_file).
    Si no existe, usa defaults de config.
    """
    path = _calibration_path()
    if not os.path.isfile(path):
        cal = _default_calibration()
        _apply_calibration(cal)
        return cal, False

    with open(path, "r", encoding="utf-8") as f:
        cal = json.load(f)
    _apply_calibration(cal)
    return cal, True


def _apply_calibration(cal: Dict[str, Dict[str, float]]) -> None:
    try:
        if "A0" in cal:
            config.A0_CAL_M = float(cal["A0"].get("m", config.A0_CAL_M))
            config.A0_CAL_B = float(cal["A0"].get("b", config.A0_CAL_B))
        if "A1" in cal:
            config.A1_CAL_M = float(cal["A1"].get("m", config.A1_CAL_M))
            config.A1_CAL_B = float(cal["A1"].get("b", config.A1_CAL_B))
        if "A2" in cal:
            config.A2_CAL_M = float(cal["A2"].get("m", config.A2_CAL_M))
            config.A2_CAL_B = float(cal["A2"].get("b", config.A2_CAL_B))
            config.GAIN_2PT = float(cal["A2"].get("m", config.GAIN_2PT))
            config.OFFSET_2PT = float(cal["A2"].get("b", config.OFFSET_2PT))
    except Exception:
        pass
