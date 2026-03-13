#!/usr/bin/env bash
set -euo pipefail

IFACE="eth0"
CON_NAME="TFM-eth0-static"
IPV4_ADDR="192.168.50.2/24"

if ! command -v nmcli >/dev/null 2>&1; then
  echo "Error: nmcli no esta disponible. Este script requiere NetworkManager." >&2
  exit 1
fi

if ! nmcli -t -f DEVICE device status | grep -qx "${IFACE}"; then
  echo "Error: la interfaz ${IFACE} no existe en este sistema." >&2
  exit 1
fi

if nmcli -t -f NAME connection show | grep -qx "${CON_NAME}"; then
  nmcli connection modify "${CON_NAME}" \
    connection.interface-name "${IFACE}" \
    connection.autoconnect yes \
    ipv4.method manual \
    ipv4.addresses "${IPV4_ADDR}" \
    ipv4.gateway "" \
    ipv4.dns "" \
    ipv4.never-default yes \
    ipv6.method ignore
else
  nmcli connection add type ethernet ifname "${IFACE}" con-name "${CON_NAME}" \
    ipv4.method manual \
    ipv4.addresses "${IPV4_ADDR}" \
    ipv4.gateway "" \
    ipv4.dns "" \
    ipv4.never-default yes \
    ipv6.method ignore \
    connection.autoconnect yes
fi

ACTIVE_CON="$(nmcli -g GENERAL.CONNECTION device show "${IFACE}" 2>/dev/null || true)"
if [[ -n "${ACTIVE_CON}" && "${ACTIVE_CON}" != "--" && "${ACTIVE_CON}" != "${CON_NAME}" ]]; then
  nmcli connection down "${ACTIVE_CON}" || true
fi

nmcli connection up "${CON_NAME}"

echo
echo "Configuracion aplicada en ${IFACE}:"
nmcli connection show "${CON_NAME}"
echo
ip -4 addr show "${IFACE}"
