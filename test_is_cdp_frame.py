"""
test_is_cdp_frame.py

Tests for capture_raw._is_cdp_frame().

Covers the fix for misclassified Cisco control frames: UDLD, DTP, VTP
and PAgP all share the CDP multicast MAC 01:00:0C:CC:CC:CC and differ
only in the SNAP protocol ID. UDLD's header layout is byte-for-byte
compatible with CDP (version + flags + checksum + type/length TLVs),
so before the fix a UDLD frame parsed as CDP produced a plausible
bogus neighbor result that could win the discovery race over genuine
LLDP data.

Run:
    python3 test_is_cdp_frame.py
Exit code 0 = all checks passed.
"""

import struct

from capture_raw import _is_cdp_frame, _is_lldp_frame
from trigger import _build_cdp_frame, _build_lldp_frame


_DST_CISCO = bytes.fromhex("01000ccccccc")
_SRC_MAC   = bytes.fromhex("c472953c455d")
_LLC       = b"\xaa\xaa\x03"
_OUI_CISCO = b"\x00\x00\x0c"


def _cisco_multicast_frame(snap_pid: bytes, pdu: bytes) -> bytes:
    """Build an 802.3/LLC/SNAP frame to the Cisco multicast MAC."""
    payload = _LLC + _OUI_CISCO + snap_pid + pdu
    return _DST_CISCO + _SRC_MAC + struct.pack("!H", len(payload)) + payload


def _udld_frame() -> bytes:
    """UDLD probe: ver/opcode + flags + checksum + CDP-compatible TLVs."""
    tlv_device = struct.pack("!HH", 0x0001, 4 + 10) + b"SW-CORE-01"
    tlv_echo   = struct.pack("!HH", 0x0003, 4 + 8) + b"\x01\x02Gi1/5\x00"
    pdu = b"\x21\x00" + b"\x00\x00" + tlv_device + tlv_echo
    return _cisco_multicast_frame(b"\x01\x11", pdu)


def _dtp_frame() -> bytes:
    """DTP: version byte + DTP TLVs (domain, status)."""
    tlv_domain = struct.pack("!HH", 0x0001, 4 + 9) + b"testdoma\x00"
    tlv_status = struct.pack("!HH", 0x0002, 5) + b"\x03"
    return _cisco_multicast_frame(b"\x20\x04", b"\x01" + tlv_domain + tlv_status)


def _vtp_frame() -> bytes:
    """VTP summary advertisement stub."""
    return _cisco_multicast_frame(b"\x20\x03", b"\x02\x01\x00\x08" + b"\x00" * 16)


def _pagp_frame() -> bytes:
    """PAgP stub."""
    return _cisco_multicast_frame(b"\x01\x04", b"\x01\x01" + b"\x00" * 16)


CASES = [
    # (name, frame, expected _is_cdp_frame result)
    ("real CDP trigger frame",        _build_cdp_frame(_SRC_MAC, "eth0"), True),
    ("UDLD (CDP-compatible layout)",  _udld_frame(),                      False),
    ("DTP",                           _dtp_frame(),                       False),
    ("VTP",                           _vtp_frame(),                       False),
    ("PAgP",                          _pagp_frame(),                      False),
    ("truncated (dst MAC only)",      _DST_CISCO + _SRC_MAC,              False),
    ("truncated mid-SNAP",            (_DST_CISCO + _SRC_MAC
                                       + b"\x00\x10" + _LLC + _OUI_CISCO), False),
    ("empty frame",                   b"",                                False),
]


def main() -> int:
    fails = []

    for name, frame, expected in CASES:
        got = _is_cdp_frame(frame)
        ok = got is expected
        print(f"  [{'ok ' if ok else 'FAIL'}] {name}: _is_cdp_frame -> {got}")
        if not ok:
            fails.append(name)

    # Sanity: the LLDP classifier must be unaffected — a genuine LLDP
    # trigger frame is still LLDP and never CDP.
    lldp = _build_lldp_frame(_SRC_MAC, "eth0")
    if not _is_lldp_frame(lldp) or _is_cdp_frame(lldp):
        print("  [FAIL] LLDP trigger frame classification changed")
        fails.append("lldp sanity")
    else:
        print("  [ok ] LLDP trigger frame still classified as LLDP only")

    if fails:
        print(f"\nFAILED: {len(fails)} case(s): {', '.join(fails)}")
        return 1

    print("\nPASSED: CDP classifier accepts only SNAP PID 0x2000 frames.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
