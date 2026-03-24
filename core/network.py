#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
core/network.py
Helpers para exponer IPs de WiFi/Ethernet en la UI.
"""

from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess
from typing import Optional

from core.telemetry import CHANNEL_TO_PORT


@dataclass(frozen=True)
class NetworkAddresses:
    wifi_ip: Optional[str] = None
    eth_ip: Optional[str] = None


def _is_wifi_interface(name: str) -> bool:
    ifname = (name or "").strip().lower()
    return ifname.startswith(("wlan", "wl"))


def _is_ethernet_interface(name: str) -> bool:
    ifname = (name or "").strip().lower()
    return ifname.startswith(("eth", "en"))


def _parse_ip_addr_output(output: str) -> NetworkAddresses:
    wifi_ip: Optional[str] = None
    eth_ip: Optional[str] = None

    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 4:
            continue

        if_name = parts[1]
        try:
            inet_idx = parts.index("inet")
            ip_value = parts[inet_idx + 1].split("/", 1)[0].strip()
        except (ValueError, IndexError):
            continue

        if not ip_value or ip_value.startswith("127."):
            continue

        if wifi_ip is None and _is_wifi_interface(if_name):
            wifi_ip = ip_value
            continue

        if eth_ip is None and _is_ethernet_interface(if_name):
            eth_ip = ip_value

    return NetworkAddresses(wifi_ip=wifi_ip, eth_ip=eth_ip)


def get_network_addresses() -> NetworkAddresses:
    ip_cmd = shutil.which("ip")
    if not ip_cmd:
        return NetworkAddresses()

    try:
        proc = subprocess.run(
            [ip_cmd, "-4", "-o", "addr", "show", "up"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=1.0,
        )
    except Exception:
        return NetworkAddresses()

    return _parse_ip_addr_output(proc.stdout)


def format_labview_status(active_channel: Optional[int], addresses: NetworkAddresses) -> str:
    tx_txt = "OFF"
    if active_channel in CHANNEL_TO_PORT:
        tx_txt = f"A{int(active_channel)}:{int(CHANNEL_TO_PORT[int(active_channel)])}"

    wifi_txt = addresses.wifi_ip or "--"
    eth_txt = addresses.eth_ip or "--"
    return f"LAB {tx_txt} | W {wifi_txt} | E {eth_txt}"
