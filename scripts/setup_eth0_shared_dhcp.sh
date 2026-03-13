#!/usr/bin/env bash
set -euo pipefail

IFACE="eth0"
CON_NAME="TFM-eth0-shared"
IPV4_ADDR="192.168.50.2/24"
DHCP_RANGE="192.168.50.10,192.168.50.100"

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
    ipv4.method shared \
    ipv4.addresses "${IPV4_ADDR}" \
    ipv4.shared-dhcp-range "${DHCP_RANGE}" \
    ipv4.never-default yes \
    ipv6.method ignore
else
  nmcli connection add type ethernet ifname "${IFACE}" con-name "${CON_NAME}" \
    ipv4.method shared \
    ipv4.addresses "${IPV4_ADDR}" \
    ipv4.shared-dhcp-range "${DHCP_RANGE}" \
    ipv4.never-default yes \
    ipv6.method ignore \
    connection.autoconnect yes
fi

while IFS= read -r active_con; do
  if [[ -n "${active_con}" ]]; then
    nmcli connection down "${active_con}" || true
  fi
done < <(
  nmcli -t -f NAME,DEVICE connection show --active |
  awk -F: -v iface="${IFACE}" -v wanted="${CON_NAME}" '$2 == iface && $1 != wanted { print $1 }'
)

nmcli connection up "${CON_NAME}"

echo
echo "Configuracion aplicada en ${IFACE}:"
nmcli connection show "${CON_NAME}"
echo
ip -4 addr show "${IFACE}"
