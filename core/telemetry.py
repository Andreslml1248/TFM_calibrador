#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
core/telemetry.py
Telemetria TCP de solo lectura para ADS1115.
"""

import socket
import threading
from typing import Dict, List, Optional


CHANNEL_TO_PORT = {
    0: 5000,  # A0
    1: 5001,  # A1
    2: 5002,  # A2
}


class TelemetrySnapshot:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_channel = {0: None, 1: None, 2: None}

    def update(self, channel: int, value_v: float) -> None:
        ch = int(channel)
        if ch not in self._by_channel:
            return
        with self._lock:
            self._by_channel[ch] = float(value_v)

    def get(self, channel: int) -> Optional[float]:
        ch = int(channel)
        with self._lock:
            val = self._by_channel.get(ch)
            return None if val is None else float(val)


_GLOBAL_SNAPSHOT = TelemetrySnapshot()


def get_global_telemetry_snapshot() -> TelemetrySnapshot:
    return _GLOBAL_SNAPSHOT


class ADSTelemetryServer:
    """
    Servidor TCP para enviar el ultimo valor disponible del canal activo.
    - Un listener por canal/puerto.
    - Solo transmite el canal seleccionado.
    - No realiza lecturas de ADS; solo consume snapshot.
    """

    def __init__(self, snapshot: TelemetrySnapshot, host: str = "0.0.0.0"):
        self.snapshot = snapshot
        self.host = host
        self._active_channel: Optional[int] = None

        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._listeners: Dict[int, socket.socket] = {}
        self._clients: Dict[int, List[socket.socket]] = {0: [], 1: [], 2: []}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="ADSTelemetryServer", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 1.0) -> None:
        self._stop_evt.set()
        t = self._thread
        if t is not None:
            t.join(timeout=max(0.0, float(timeout_s)))
        self._thread = None

    def set_active_channel(self, channel: Optional[int]) -> None:
        ch = None if channel is None else int(channel)
        if ch not in (None, 0, 1, 2):
            return
        with self._lock:
            self._active_channel = ch

    def get_active_channel(self) -> Optional[int]:
        with self._lock:
            return self._active_channel

    def _open_listeners(self) -> None:
        for ch, port in CHANNEL_TO_PORT.items():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, int(port)))
            sock.listen(4)
            sock.setblocking(False)
            self._listeners[ch] = sock

    def _accept_new_clients(self) -> None:
        for ch, listener in self._listeners.items():
            while True:
                try:
                    conn, _addr = listener.accept()
                except BlockingIOError:
                    break
                except OSError:
                    break
                conn.setblocking(True)
                conn.settimeout(0.01)
                self._clients[ch].append(conn)

    def _broadcast_channel_value(self, ch: int) -> None:
        value = self.snapshot.get(ch)
        if value is None:
            return
        msg = f"{value:.6f}\r\n"
        print("TX:", repr(msg))
        payload = msg.encode("ascii")

        alive_clients: List[socket.socket] = []
        for conn in self._clients[ch]:
            try:
                conn.sendall(payload)
                alive_clients.append(conn)
            except OSError:
                try:
                    conn.close()
                except OSError:
                    pass
        self._clients[ch] = alive_clients

    def _run(self) -> None:
        try:
            self._open_listeners()
            while not self._stop_evt.is_set():
                self._accept_new_clients()
                active = self.get_active_channel()

                if active in (0, 1, 2):
                    self._broadcast_channel_value(int(active))
                    wait_s = 0.05  # ~20 Hz
                else:
                    wait_s = 0.20  # sin telemetria activa, carga minima

                self._stop_evt.wait(wait_s)
        finally:
            self._close_all()

    def _close_all(self) -> None:
        for ch in (0, 1, 2):
            for conn in self._clients[ch]:
                try:
                    conn.close()
                except OSError:
                    pass
            self._clients[ch].clear()

        for sock in self._listeners.values():
            try:
                sock.close()
            except OSError:
                pass
        self._listeners.clear()
