"""
test_extract_model.py

Tests for parse_lldp_raw._extract_model().

Covers the feature/huawei fix: the System Description TLV is often
multi-line, and the model lives on the first line. sanitize_display_string
strips CR/LF, so the first line must be isolated BEFORE sanitizing —
otherwise the whole description collapses into one unusable string
(the bug seen on the real Huawei S5735-L48T4X-A).

Run:
    python3 test_extract_model.py
"""

from parse_lldp_raw import _extract_model, _TLV_SYS_DESCR


def model_of(sysdescr: bytes) -> str:
    return _extract_model({_TLV_SYS_DESCR: [sysdescr]})


# Real capture from Huawei S5735-L48T4X-A (T283), 2026-07-17. The switch
# leads with a clean "Huawei Switch <model>" line, then the VRP banner.
HUAWEI_S5735 = (
    b"Huawei Switch S5735-L48T4X-A\r\n"
    b"Huawei Versatile Routing Platform Software\r\n"
    b"VRP (R) software, Version 5.170 (S5735 V200R022C00SPC500)\r\n"
    b"Copyright (C) 2000-2022 HUAWEI TECH Co., Ltd."
)

CASES = [
    # (label, sysDescr bytes, expected model)
    ("huawei S5735 (multi-line, model on line 0)",
     HUAWEI_S5735,
     "Huawei Switch S5735-L48T4X-A"),

    # Regression guard: Cisco SG500 sends a single line — must be unchanged.
    ("cisco SG500 (single line)",
     b"SG500-28 28-Port Gigabit Stackable Managed Switch",
     "SG500-28 28-Port Gigabit Stackable Managed Switch"),

    # Leading blank line -> first NON-EMPTY line wins.
    ("leading blank line",
     b"\r\nHuawei Switch S5735-L48T4X-A\r\nVRP ...",
     "Huawei Switch S5735-L48T4X-A"),

    # sanitize still runs on the chosen line: embedded control chars gone,
    # spaces and printable text kept.
    ("control chars stripped from chosen line",
     b"SG500-52\x00\x01 Managed Switch\r\nsecond line",
     "SG500-52 Managed Switch"),

    # Empty / missing TLV.
    ("empty value", b"", ""),
]


def main():
    fails = []
    for label, sysdescr, expected in CASES:
        got = model_of(sysdescr)
        ok = got == expected
        print(f"  [{'ok ' if ok else 'XX '}] {label}\n"
              f"        -> {got!r}"
              f"{'' if ok else chr(10) + '        expected ' + repr(expected)}")
        if not ok:
            fails.append((label, expected, got))

    # Missing TLV entirely (no key at all).
    empty = _extract_model({})
    if empty != "":
        fails.append(("no TLV key", "", empty))
    print(f"  [{'ok ' if empty == '' else 'XX '}] no System Description TLV -> {empty!r}")

    print()
    if fails:
        print(f"FAILED: {len(fails)} case(s).")
        return 1
    print("PASSED: all checks OK.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
