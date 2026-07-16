#!/usr/bin/env bash
# ============================================================
# setup_wifi_ap.sh
#
# Configures wlan0 as a PERMANENT Wi-Fi Access Point so PiScout
# can always be reached over SSH in the field, independently of
# whatever eth0 is plugged into.
#
#   wlan0  ->  always-on AP   (SSID + password prompted here)
#   eth0   ->  DHCP client    (switch discovery + repo updates)
#
# The two paths run in parallel and never fight each other:
#   - Unplugging / replugging the switch cable on eth0 does NOT
#     drop the SSH session, because that session lives on the AP.
#   - Updates (git pull) go over eth0 whenever it lands on a
#     network that has DHCP *and* Internet.
#
# Idempotent: safe to re-run at any time to change SSID / password.
#
# For unattended imaging you may skip the prompts by exporting
# PISCOUT_AP_SSID and PISCOUT_AP_PSK before running the script.
#
# IMPORTANT: run this over eth0 or the local console — NOT over a
# wlan0 client link you are about to replace, or you will cut your
# own connection the moment the AP takes over the radio.
# ============================================================

set -euo pipefail

# ---- Tunables ---------------------------------------------
AP_PROFILE="piscout-ap"
AP_IFACE="wlan0"
WIFI_COUNTRY="PL"
AP_BAND="bg"                 # bg = 2.4 GHz: best range in the field
AP_CHANNEL="6"
DEFAULT_SSID="Pi_IT-Tools_AP"
# Leave AP_GATEWAY empty to use NetworkManager's default 10.42.0.1/24.
# Set e.g. "10.99.13.1/24" to move the AP subnet away from 10.42.0.0/24
# (useful at client sites that already use a 10.x network).
AP_GATEWAY=""
# -----------------------------------------------------------

RED="\033[0;31m"; GREEN="\033[0;32m"; YELLOW="\033[1;33m"; RESET="\033[0m"
info()  { echo -e "${GREEN}[INFO]${RESET}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error() { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
die()   { error "$*"; exit 1; }

# ---- Prerequisites ----------------------------------------
[[ $EUID -eq 0 ]] || die "This script must be run as root. Use: sudo bash setup_wifi_ap.sh"
command -v nmcli >/dev/null 2>&1 || die "nmcli (NetworkManager) not found."
[[ -e "/sys/class/net/$AP_IFACE" ]] || die "Interface $AP_IFACE not present on this device."

# ---- SSID -------------------------------------------------
AP_SSID="${PISCOUT_AP_SSID:-}"
if [[ -z "$AP_SSID" ]]; then
    read -rp "Wi-Fi SSID [${DEFAULT_SSID}]: " AP_SSID
    AP_SSID="${AP_SSID:-$DEFAULT_SSID}"
fi
[[ -n "$AP_SSID" ]]        || die "SSID cannot be empty."
(( ${#AP_SSID} <= 32 ))   || die "SSID too long (max 32 characters)."

# ---- Password (hidden, confirmed, 8-63 chars) -------------
AP_PSK="${PISCOUT_AP_PSK:-}"
if [[ -n "$AP_PSK" ]]; then
    (( ${#AP_PSK} >= 8 && ${#AP_PSK} <= 63 )) || \
        die "PISCOUT_AP_PSK must be 8-63 characters."
else
    while :; do
        read -rsp "Wi-Fi password (8-63 chars): " AP_PSK;  echo
        read -rsp "Repeat password: "            AP_PSK2; echo
        if [[ "$AP_PSK" != "$AP_PSK2" ]]; then
            warn "Passwords do not match — try again."; continue
        fi
        if (( ${#AP_PSK} < 8 || ${#AP_PSK} > 63 )); then
            warn "Password must be 8-63 characters — try again."; continue
        fi
        break
    done
fi

# ---- Radio: country + unblock -----------------------------
# Without a regulatory country the driver refuses to start an AP.
info "Enabling Wi-Fi radio and setting country ($WIFI_COUNTRY)..."
rfkill unblock wlan 2>/dev/null || true
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_wifi_country "$WIFI_COUNTRY" 2>/dev/null || \
        warn "Could not set country via raspi-config — verify with 'iw reg get'."
fi

# ---- Neutralize conflicting wlan0 client profiles ---------
# wlan0 can be an AP *or* a client, never both at once. Any leftover
# Wi-Fi client profile (a keyfile one, or a netplan-generated one that
# regenerates on every boot) would grab the radio and stop the AP.
info "Looking for conflicting Wi-Fi client profiles on $AP_IFACE..."
NETPLAN_CHANGED=0
while IFS=: read -r c_name c_type c_uuid; do
    [[ "$c_type" == "802-11-wireless" ]] || continue
    [[ "$c_name" == "$AP_PROFILE" ]]     && continue
    info "  Found client profile: $c_name"
    npfile="$(grep -rl "$c_uuid" /etc/netplan/ 2>/dev/null | head -n1 || true)"
    if [[ -n "$npfile" ]]; then
        # netplan-generated: must be removed at the source, or it
        # comes back on the next boot. Back up first (it holds the
        # Wi-Fi password) — keep that backup OUT of the git repo.
        cp "$npfile" "/root/$(basename "$npfile").bak"
        rm -f "$npfile"
        NETPLAN_CHANGED=1
        info "  Removed netplan client definition (backup in /root/)."
    else
        nmcli connection delete "$c_name" >/dev/null 2>&1 || true
        info "  Deleted NetworkManager client profile."
    fi
done < <(nmcli -t -f NAME,TYPE,UUID connection show)

if (( NETPLAN_CHANGED )); then
    netplan generate 2>/dev/null || true
    netplan apply   2>/dev/null || true
fi
nmcli connection reload 2>/dev/null || true

# ---- (Re)create the AP profile ----------------------------
info "Creating AP profile '$AP_PROFILE' (SSID: $AP_SSID)..."
nmcli connection delete "$AP_PROFILE" >/dev/null 2>&1 || true

nmcli connection add type wifi ifname "$AP_IFACE" con-name "$AP_PROFILE" \
    autoconnect yes ssid "$AP_SSID" >/dev/null

# mode ap          : run as access point
# band bg / chan 6 : fixed 2.4 GHz channel (reliable AP start-up)
# ipv4 shared      : NM assigns 10.42.0.1/24 and runs its own DHCP server
# wpa-psk          : WPA2 with the password entered above
# wps-method       : disabled, so Windows offers the passphrase field
#                    directly instead of a WPS PIN prompt (and removes
#                    the brute-forceable WPS attack surface)
nmcli connection modify "$AP_PROFILE" \
    802-11-wireless.mode ap \
    802-11-wireless.band "$AP_BAND" \
    802-11-wireless.channel "$AP_CHANNEL" \
    ipv4.method shared \
    802-11-wireless-security.key-mgmt wpa-psk \
    802-11-wireless-security.psk "$AP_PSK" \
    802-11-wireless-security.wps-method disabled

if [[ -n "$AP_GATEWAY" ]]; then
    nmcli connection modify "$AP_PROFILE" ipv4.addresses "$AP_GATEWAY"
    info "AP subnet overridden -> $AP_GATEWAY"
fi

# ---- Normalize eth0 for DHCP + DNS (repo updates) ---------
# The original piscout-eth0 profile deferred the default route and DNS
# to wlan0, from the days when wlan0 was a *client* providing Internet.
# With wlan0 now an AP (no uplink), eth0 must supply DNS itself, or a
# 'git pull' over eth0 cannot resolve github.com.
if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "piscout-eth0"; then
    info "Normalizing eth0 profile so DNS/updates work over the cable..."
    nmcli connection modify piscout-eth0 ipv4.ignore-auto-dns no 2>/dev/null || \
        warn "Could not clear ignore-auto-dns on piscout-eth0 — check DNS over eth0."
    nmcli connection modify piscout-eth0 ipv4.route-metric -1 2>/dev/null || true
fi

# ---- Activate ---------------------------------------------
info "Activating the Access Point..."
nmcli connection up "$AP_PROFILE" >/dev/null 2>&1 || \
    warn "AP did not activate now — it will come up on next boot (autoconnect=yes)."

# ---- Summary ----------------------------------------------
AP_CIDR="$(ip -4 -o addr show "$AP_IFACE" 2>/dev/null | awk '{print $4}' | head -n1)"
AP_IP="${AP_CIDR%%/*}"
echo ""
info "================================================"
info "  Wi-Fi Access Point configured."
info "================================================"
info "  SSID      : $AP_SSID"
info "  Interface : $AP_IFACE (${AP_CIDR:-no address yet — check after reboot})"
if [[ -n "$AP_IP" ]]; then
    info "  Reach it  : ssh <user>@${AP_IP}"
fi
echo ""
warn "Reboot once and confirm the AP comes up on its own before taking"
warn "the device into the field:  sudo reboot"
