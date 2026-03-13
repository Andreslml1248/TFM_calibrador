#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
core/network.py
Utilidades para identificar IPs IPv4 locales por tipo de interfaz.
"""

from __future__ import annotations

from dataclasses import dataclass
import platform
import re
import socket
import subprocess
from typing import List, Optional


@dataclass(frozen=True)
class NetworkInterfaceInfo:
    name: str
    ipv4: str
    kind: str  # "ethernet", "wifi", "unknown"


def _classify_interface(name: str) -> str:
    raw = (name or "").strip().lower()
    if any(token in raw for token in ("wi-fi", "wifi", "wireless", "wlan", "wlp", "wl")):
        return "wifi"
    if any(token in raw for token in ("ethernet", "eth", "enp", "eno", "enx", "lan")):
        return "ethernet"
    return "unknown"


def _interface_priority(item: NetworkInterfaceInfo) -> tuple[int, str, str]:
    raw = (item.name or "").strip().lower()
    if item.kind == "ethernet":
        if raw == "eth0":
            return (0, raw, item.ipv4)
        return (1, raw, item.ipv4)
    if item.kind == "wifi":
        if raw == "wlan0":
            return (2, raw, item.ipv4)
        return (3, raw, item.ipv4)
    return (4, raw, item.ipv4)


def _parse_windows_ipconfig(stdout: str) -> List[NetworkInterfaceInfo]:
    interfaces: List[NetworkInterfaceInfo] = []
    current_name: Optional[str] = None
    current_kind = "unknown"

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if not line.startswith(" ") and stripped.endswith(":"):
            current_name = stripped[:-1]
            current_kind = _classify_interface(current_name)
            continue

        if current_name is None or "IPv4" not in stripped or ":" not in stripped:
            continue

        ip_match = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", stripped)
        if not ip_match:
            continue

        ipv4 = ip_match.group(1)
        if ipv4.startswith("127."):
            continue
        interfaces.append(NetworkInterfaceInfo(name=current_name, ipv4=ipv4, kind=current_kind))

    return interfaces


def _parse_linux_ip(stdout: str) -> List[NetworkInterfaceInfo]:
    interfaces: List[NetworkInterfaceInfo] = []
    current_name: Optional[str] = None

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if re.match(r"^\d+:\s", line):
            match = re.match(r"^\d+:\s+([^:]+):", line)
            current_name = match.group(1) if match else None
            continue

        if current_name is None:
            continue

        stripped = line.strip()
        if not stripped.startswith("inet "):
            continue

        parts = stripped.split()
        if len(parts) < 2 or "/" not in parts[1]:
            continue
        ipv4 = parts[1].split("/", 1)[0]
        if ipv4.startswith("127."):
            continue
        interfaces.append(
            NetworkInterfaceInfo(
                name=current_name,
                ipv4=ipv4,
                kind=_classify_interface(current_name),
            )
        )

    return interfaces


def _fallback_hostname_addresses() -> List[NetworkInterfaceInfo]:
    ips = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, family=socket.AF_INET, type=socket.SOCK_STREAM):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return [NetworkInterfaceInfo(name="host", ipv4=ip, kind="unknown") for ip in sorted(ips)]


def get_network_interfaces() -> List[NetworkInterfaceInfo]:
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["ipconfig"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            interfaces = _parse_windows_ipconfig(result.stdout)
        else:
            result = subprocess.run(
                ["ip", "-4", "addr", "show"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            interfaces = _parse_linux_ip(result.stdout)
    except Exception:
        interfaces = []

    if not interfaces:
        interfaces = _fallback_hostname_addresses()

    seen = set()
    unique: List[NetworkInterfaceInfo] = []
    for item in interfaces:
        key = (item.name, item.ipv4, item.kind)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def pick_preferred_interface(preferred: str) -> Optional[NetworkInterfaceInfo]:
    pref = (preferred or "auto").strip().lower()
    interfaces = sorted(get_network_interfaces(), key=_interface_priority)
    if not interfaces:
        return None

    if pref == "ethernet":
        for item in interfaces:
            if item.kind == "ethernet":
                return item
        return None

    if pref == "wifi":
        for item in interfaces:
            if item.kind == "wifi":
                return item
        return None

    for wanted_kind in ("ethernet", "wifi", "unknown"):
        for item in interfaces:
            if item.kind == wanted_kind:
                return item
    return interfaces[0]


def get_primary_interface_by_kind(kind: str) -> Optional[NetworkInterfaceInfo]:
    wanted = (kind or "").strip().lower()
    if wanted not in {"ethernet", "wifi", "unknown"}:
        return None

    interfaces = sorted(get_network_interfaces(), key=_interface_priority)
    for item in interfaces:
        if item.kind == wanted:
            return item
    return None
