#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
core/ads1115.py
Funciones de bajo nivel para el ADC ADS1115
"""

import time
from smbus2 import SMBus
import config


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def ads_cfg_word(ch: int, pga_v: float, sps: int) -> int:
    osb = 1 << 15
    mux_map = {0: 0b100, 1: 0b101, 2: 0b110, 3: 0b111}
    mux = mux_map.get(ch, 0b110) << 12

    pga_map = {6.144: 0b000, 4.096: 0b001, 2.048: 0b010,
               1.024: 0b011, 0.512: 0b100, 0.256: 0b101}
    pga = pga_map.get(pga_v, 0b001) << 9

    mode = 1 << 8
    dr_map = {8: 0b000, 16: 0b001, 32: 0b010, 64: 0b011,
              128: 0b100, 250: 0b101, 475: 0b110, 860: 0b111}
    dr = dr_map.get(sps, 0b100) << 5

    return osb | mux | pga | mode | dr | 0b11


def ads_read_v_once(bus: SMBus, ch: int) -> float:
    cfg = ads_cfg_word(ch, config.ADS_PGA_V, config.ADS_SPS)
    bus.write_i2c_block_data(
        config.ADS_ADDR, 0x01,
        [(cfg >> 8) & 0xFF, cfg & 0xFF]
    )
    time.sleep(config.ADS_CONV_DELAY_S)
    d = bus.read_i2c_block_data(config.ADS_ADDR, 0x00, 2)
    raw = (d[0] << 8) | d[1]
    if raw & 0x8000:
        raw -= 1 << 16
    return (raw / 32768.0) * config.ADS_PGA_V

