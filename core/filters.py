#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
core/filters.py
Filtros PtByPt con estado para uso en vivo.
"""

from collections import deque
from typing import Deque, List


class MedianPtByPt:
    def __init__(self, window: int):
        self.window = max(1, int(window))
        self._buf: Deque[float] = deque(maxlen=self.window)

    def update(self, x: float) -> float:
        self._buf.append(float(x))
        if len(self._buf) == 1:
            return float(self._buf[0])
        data: List[float] = sorted(self._buf)
        mid = len(data) // 2
        if len(data) % 2 == 1:
            return float(data[mid])
        return 0.5 * (data[mid - 1] + data[mid])


class MeanPtByPt:
    def __init__(self, window: int):
        self.window = max(1, int(window))
        self._buf: Deque[float] = deque(maxlen=self.window)

    def update(self, x: float) -> float:
        self._buf.append(float(x))
        return float(sum(self._buf) / max(1, len(self._buf)))


class ChannelFilterChain:
    def __init__(self, median_n: int, mean_n: int):
        self.median = MedianPtByPt(median_n)
        self.mean = MeanPtByPt(mean_n)

    def update(self, x: float) -> float:
        return self.mean.update(self.median.update(x))
