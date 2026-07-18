"""
test_shorten_interface_name.py

Regression + staging tests for parse_utils.shorten_interface_name().

Purpose (feature/huawei):
  1. LOCK the current Cisco-oriented behavior so that adding Huawei
     interface forms cannot silently regress Cisco display output
     (e.g. GigabitEthernet -> Gi must stay Gi, not become GE).
  2. STAGE the Huawei cases: the desired Huawei output is unknown until
     we capture a real LLDP frame from the S5735 (KROK 1). Those
     assertions are marked TODO and skipped for now. Fill them in from
     the real Port ID TLV, then implement the tokens and flip RUN_HUAWEI.

Run:
    python3 test_shorten_interface_name.py
Exit code 0 = all active checks passed.
"""

from parse_utils import shorten_interface_name as short


# --- Baseline Cisco behavior (MUST NOT CHANGE) -------------------------
CISCO_CASES = [
    ("GigabitEthernet1/0/24",         "Gi1/0/24"),
    ("TenGigabitEthernet1/1/1",       "Te1/1/1"),
    ("TwentyFiveGigabitEthernet1/1",  "Twe1/1"),
    ("TwentyFiveGigE1/1",             "Twe1/1"),
    ("FortyGigabitEthernet1/1",       "Fo1/1"),
    ("HundredGigabitEthernet1/0/1",   "Hu1/0/1"),
    ("HundredGigE1/0/1",              "Hu1/0/1"),
    ("FastEthernet0/1",               "Fa0/1"),
    ("Port-channel10",                "Po10"),
    ("Port-Channel10",                "Po10"),
    ("Ethernet1/1",                   "Eth1/1"),
]

# Case-insensitive: SG500 advertises lowercase interface names.
CASE_CASES = [
    ("gigabitethernet1/1/3",          "Gi1/1/3"),
    ("TENGIGABITETHERNET1/1/1",       "Te1/1/1"),
]

# Ordering guard: a longer prefix must win over a shorter one that
# shares its start. If Ethernet were matched first, TenGigabitEthernet
# would be mangled. Locks the required precedence for when Huawei
# tokens get inserted into the list.
ORDERING_CASES = [
    ("TenGigabitEthernet2/0/1",       "Te2/0/1"),   # not "Eth..."
    ("GigabitEthernet0/0/1",          "Gi0/0/1"),   # not "Eth..."
]

# Safe pass-through: unknown prefixes are returned unchanged.
PASSTHROUGH_CASES = [
    ("",                              ""),
    ("Vlanif100",                     "Vlanif100"),
    ("some-random-name",              "some-random-name"),
]

# Confirmed on real Huawei S5735-L48T4X-A hardware (T283, 2026-07-17):
# plain copper GE ports use the SHARED GigabitEthernet->Gi mapping and
# shorten correctly with no Huawei-specific change. Locks that in.
HUAWEI_CONFIRMED = [
    ("GigabitEthernet0/0/37",         "Gi0/0/37"),
    ("GigabitEthernet0/0/39",         "Gi0/0/39"),
]

# Current (pre-change) behavior for Huawei-style names. Documents the
# BASELINE today: the tokens are unknown to shorten_interface_name, so
# it returns them unchanged. When the Huawei tokens are implemented,
# these move into HUAWEI_CASES below with their new expected values.
HUAWEI_BASELINE_TODAY = [
    ("XGigabitEthernet0/0/1",         "XGigabitEthernet0/0/1"),
    ("10GE1/0/1",                     "10GE1/0/1"),
    ("Eth-Trunk1",                    "Eth-Trunk1"),
    ("MEth0/0/1",                     "MEth0/0/1"),
]

# TODO(feature/huawei): fill from the real S5735 LLDP dump (KROK 1),
# implement the tokens in parse_utils.shorten_interface_name, then set
# RUN_HUAWEI = True. Example expected values (per proposed mapping) are
# commented out — replace with whatever the switch actually sends.
RUN_HUAWEI = False
HUAWEI_CASES = [
    # ("XGigabitEthernet0/0/1",       "XGE0/0/1"),
    # ("10GE1/0/1",                    "10GE1/0/1"),
    # ("40GE1/0/1",                    "40GE1/0/1"),
    # ("100GE1/0/1",                   "100GE1/0/1"),
    # ("Eth-Trunk1",                   "Eth-Trunk1"),
    # ("MEth0/0/1",                    "MEth0/0/1"),
]


def _run(name, cases):
    fails = []
    for src, expected in cases:
        got = short(src)
        ok = got == expected
        print(f"  [{'ok ' if ok else 'XX '}] {name}: {src!r} -> {got!r}"
              f"{'' if ok else f' (expected {expected!r})'}")
        if not ok:
            fails.append((src, expected, got))
    return fails


def main():
    fails = []
    print("Cisco baseline (must not change):");   fails += _run("cisco", CISCO_CASES)
    print("Case-insensitive:");                   fails += _run("case", CASE_CASES)
    print("Ordering guard:");                     fails += _run("order", ORDERING_CASES)
    print("Safe pass-through:");                  fails += _run("passthru", PASSTHROUGH_CASES)
    print("Huawei GE (confirmed on S5735):");     fails += _run("hw-confirmed", HUAWEI_CONFIRMED)
    print("Huawei baseline TODAY (unchanged):");  fails += _run("hw-baseline", HUAWEI_BASELINE_TODAY)

    if RUN_HUAWEI:
        print("Huawei implemented cases:");        fails += _run("huawei", HUAWEI_CASES)
    else:
        print("Huawei implemented cases: SKIPPED (RUN_HUAWEI=False, awaiting S5735 dump)")

    print()
    if fails:
        print(f"FAILED: {len(fails)} case(s) did not match.")
        return 1
    print("PASSED: all active checks OK.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
