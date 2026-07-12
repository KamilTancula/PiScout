"""
config.py

PiScout configuration.

Edit this file to adjust behavior. All values have safe defaults
that work without any changes on most networks.

Most settings can also be overridden via environment variables using
the PS_ prefix (e.g. PS_NETWORK_INTERFACE=eth1). Environment variables
take precedence over the values in this file.

What this file does:
- Define which display type to use
- Configure the network interface
- Set SNMP community strings to try
- Tune timing values

What this file does NOT do:
- Initialize hardware
- Capture packets
- Parse data
- Draw anything on screen
"""

import os


# ============================================================
# ==================== SNMP SETTINGS =========================
# ============================================================
# This fork uses SNMPv3 with a dedicated read-only user instead of
# community strings. Switches are configured so that ONLY this user
# can read data; no community brute-forcing is performed in v3 mode.
#
# SNMP is DISABLED by default: the device works fully passively
# (LLDP/CDP capture + local inventory map) and never sends a single
# SNMP packet. Enable it only on networks where a dedicated read-only
# SNMPv3 user (e.g. "ITTools") has been provisioned on the switches.
#
# SNMP_ENABLED     : master switch for the SNMP discovery thread.
#                    DISABLED by default — the device works passively
#                    (LLDP/CDP listen + DHCP + local inventory map) and
#                    never sends SNMP/SSH/NETCONF/API traffic to any
#                    switch. Enable only on networks where querying
#                    switches as the dedicated user is acceptable.
# SNMP_VERSION     : "3" (default) or "2c" (legacy community mode).
#
# SNMPv3 credentials (used when SNMP_VERSION = "3"):
#   SNMP_V3_USER          : security name (default "ITTools")
#   SNMP_V3_AUTH_PROTOCOL : "SHA" or "MD5"
#   SNMP_V3_AUTH_PASSWORD : min 8 characters; empty = noAuthNoPriv
#   SNMP_V3_PRIV_PROTOCOL : "DES" or "AES"
#                           (Cisco SG500 CLI provisions DES for priv)
#   SNMP_V3_PRIV_PASSWORD : min 8 characters; empty = no privacy
#
# The security level is derived automatically:
#   auth + priv password -> authPriv
#   auth password only   -> authNoPriv
#   no passwords         -> noAuthNoPriv
#
# Override examples:
#   PS_SNMP_ENABLED=0
#   PS_SNMP_V3_USER=ITTools PS_SNMP_V3_AUTH_PASS=... PS_SNMP_V3_PRIV_PASS=...
# ============================================================
SNMP_ENABLED = os.environ.get("PS_SNMP_ENABLED", "0").lower() in (
    "1", "true", "yes"
)

SNMP_VERSION = os.environ.get("PS_SNMP_VERSION", "3").strip()

SNMP_V3_USER          = os.environ.get("PS_SNMP_V3_USER", "ITTools")
SNMP_V3_AUTH_PROTOCOL = os.environ.get("PS_SNMP_V3_AUTH_PROTO", "SHA")
SNMP_V3_AUTH_PASSWORD = os.environ.get("PS_SNMP_V3_AUTH_PASS", "")
SNMP_V3_PRIV_PROTOCOL = os.environ.get("PS_SNMP_V3_PRIV_PROTO", "DES")
SNMP_V3_PRIV_PASSWORD = os.environ.get("PS_SNMP_V3_PRIV_PASS", "")


# ============================================================
# ================= USER DISPLAY SELECTION ===================
# ============================================================
# Valid values:
#   "epaper"  = Waveshare 3.7" e-paper HAT, 480x280 (default)
#   "lcd"     = Waveshare 1.44" LCD HAT display
#
# Override without editing this file:
#   PS_DISPLAY_TYPE=lcd python3 main.py
# ============================================================

DISPLAY_TYPE = os.environ.get("PS_DISPLAY_TYPE", "epaper")


# ============================================================
# -------------------- NETWORK SETTINGS ----------------------
# ============================================================

# Network interface to monitor.
# Override: PS_NETWORK_INTERFACE=eth1
NETWORK_INTERFACE = os.environ.get("PS_NETWORK_INTERFACE", "eth0")

# How long to wait for any discovery method before giving up.
# 120 seconds allows CDP's 60-second cycle to be caught up to 2 times.
# Override: PS_DISCOVERY_TIMEOUT=60
DISCOVERY_TIMEOUT = float(os.environ.get("PS_DISCOVERY_TIMEOUT", "120.0"))

# How long after the "Scanning..." screen appears before port data is
# allowed to replace it. This ensures the user sees the screen for at
# least this many seconds even if SNMP responds almost instantly.
# The e-paper draw time (~3s) is additional buffer on top of this.
RESULT_REVEAL_DELAY = 1.5

# How long to wait before displaying a partial result (one where switch_name
# is present but port is missing, or vice versa). Some switches such as
# FortiSwitch omit optional LLDP TLVs like the port VLAN ID. Showing partial
# data after this delay gives the user something useful rather than a blank
# screen while the device continues trying to get complete information.
# The background passive listener will upgrade the display to full data
# the moment a complete advertisement is received.
# Set to 0 to disable partial display entirely (wait for complete data only).
PARTIAL_DISPLAY_DELAY = 30.0

# How long to block waiting for a raw LLDP/CDP frame on each receive call.
# 2.0 seconds keeps the passive listener responsive to link-down events.
RAW_RECEIVE_TIMEOUT = 2.0


# ============================================================
# -------------------- SNMP SETTINGS -------------------------
# ============================================================

# User-defined SNMP community string (LEGACY — used only when
# SNMP_VERSION = "2c"; ignored entirely in the default v3 mode).
# If set, this is tried FIRST before the built-in list below.
# Override: PS_SNMP_COMMUNITY=mystring
SNMP_USER_COMMUNITY = os.environ.get("PS_SNMP_COMMUNITY", "")

# Built-in community strings tried in order after SNMP_USER_COMMUNITY.
# Covers the vast majority of network environments without configuration.
SNMP_COMMUNITY_STRINGS = [
    "public",
    "cisco",
    "community",
    "private",
    "manager",
    "snmp",
    "monitor",
    "readonly",
]

# Seconds to wait for each individual SNMP response.
# 1.0 second is sufficient for most switches while keeping the race
# responsive — if passive wins, the SNMP thread stops within 1 second.
SNMP_TIMEOUT = 1.0

# Number of SNMP retries per query before moving to the next community string.
SNMP_RETRIES = 1

# Seconds to wait for a DHCP lease before falling back to ARP observation.
SNMP_DHCP_WAIT = 8.0

# Seconds to listen for ARP traffic when DHCP is unavailable.
SNMP_ARP_WAIT = 3.0


# ============================================================
# -------------------- DISPLAY SETTINGS ----------------------
# ============================================================

# Optional font path used by both display types.
DISPLAY_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


# ============================================================
# ------------------- E-PAPER SETTINGS -----------------------
# ============================================================

# Minimum seconds between normal e-paper refreshes.
EPAPER_MIN_REFRESH_INTERVAL = 10

# If True, the panel sleeps after each update (image stays visible).
EPAPER_AUTO_SLEEP = True

# Full refresh after this many partial refreshes to clear ghosting.
EPAPER_PARTIAL_REFRESH_LIMIT = 8


# ============================================================
# --------------------- LCD SETTINGS -------------------------
# ============================================================

LCD_ROTATE_180           = True
LCD_CLEAR_ON_START       = True
LCD_BACKGROUND_COLOR     = (0, 0, 0)
LCD_TEXT_COLOR           = (255, 255, 255)
LCD_BACKLIGHT_BRIGHTNESS = 100


# ============================================================
# ---------------------- LOG LEVEL ---------------------------
# ============================================================
# "WARNING" for normal appliance use (minimizes SD card writes).
# "DEBUG"   for troubleshooting.
#
# Override without editing this file:
#   PS_LOG_LEVEL=DEBUG python3 main.py
# ============================================================

LOG_LEVEL = os.environ.get("PS_LOG_LEVEL", "WARNING")


# ============================================================
# ------------------- HISTORY SETTINGS -----------------------
# ============================================================
# Controls whether and how port discovery results are saved to disk.
#
# PORT_HISTORY_MODE:
#   0 = Off (default) — nothing written, fully compatible with read-only
#   1 = Port History  — saves last PORT_HISTORY_LIMIT results to
#                       PORT_HISTORY_PATH/history.jsonl
#   2 = Debug Log     — writes verbose rotating log to
#                       PORT_HISTORY_PATH/debug.log
#
# Notes:
#   - Modes 1 and 2 require the writable /data partition to be mounted.
#     Run make_readonly.sh to set this up.
#   - Mode 0 is safe on a read-only filesystem with no writable partition.
#   - Each history entry is ~180 bytes. 50 entries = ~9KB total.
#   - The debug log rotates at 5MB and keeps 3 backup files.
#
# Override: PS_HISTORY_MODE=1
# Override: PS_HISTORY_PATH=/data/piscout
# ============================================================

PORT_HISTORY_MODE  = int(os.environ.get("PS_HISTORY_MODE",  "0"))
PORT_HISTORY_LIMIT = int(os.environ.get("PS_HISTORY_LIMIT", "50"))
PORT_HISTORY_PATH  = os.environ.get("PS_HISTORY_PATH", "/data/piscout")
