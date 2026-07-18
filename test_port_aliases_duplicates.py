"""
test_port_aliases_duplicates.py

Tests for the duplicate-alias warning in port_aliases._load()
(feature/adaptation-for-huawei, correction #13).

port_aliases merges several device JSON files. When two files define the
same (device, port) with DIFFERENT descriptions, the later one silently
won. This adds a WARNING on real overrides while keeping last-write-wins.
Verified here: override across files warns once (with both files + old/new
desc), different keys don't warn, name-space vs MAC-space is not a
conflict, bad JSON is skipped, and identical redefinitions stay silent.

Run:
    python3 test_port_aliases_duplicates.py
"""

import json
import logging
import tempfile
from pathlib import Path

import config
import port_aliases

# Capture only the duplicate-conflict warnings (parse warnings are separate).
_records: list[logging.LogRecord] = []


class _Cap(logging.Handler):
    def emit(self, rec):
        _records.append(rec)


def _dup_warnings() -> list[str]:
    return [r.getMessage() for r in _records
            if r.getMessage().startswith("Duplicate port alias")]


def _setup(files: dict) -> Path:
    """Write device JSON files into a fresh dir and point the module at it."""
    base = Path(tempfile.mkdtemp(prefix="piscout-alias-"))
    dev = base / "devices"
    dev.mkdir()
    for name, content in files.items():
        path = dev / name
        if isinstance(content, str):        # raw text (for bad-JSON case)
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(json.dumps(content), encoding="utf-8")
    config.PORT_HISTORY_PATH = str(base)
    port_aliases._cache["stamp"] = None     # force reload
    _records.clear()
    return base


def main() -> int:
    logging.getLogger("port_aliases").addHandler(_Cap())
    logging.getLogger("port_aliases").setLevel(logging.WARNING)
    fails: list[str] = []

    def check(cond, msg):
        print(f"  [{'ok ' if cond else 'XX '}] {msg}")
        if not cond:
            fails.append(msg)

    # A) Override across files, ports written differently but normalizing
    #    to the same key -> exactly one warning naming both files + descs.
    _setup({
        "01-base.json":     {"T283": {"GigabitEthernet0/0/37": "A2.C07"}},
        "02-override.json": {"T283": {"Gi0/0/37": "A2.C07-NEW"}},
    })
    got = port_aliases.lookup("T283", "", "Gi0/0/37")
    warns = _dup_warnings()
    check(got == "A2.C07-NEW", f"A: last file wins (got {got!r})")
    check(len(warns) == 1, f"A: exactly one duplicate warning (got {len(warns)})")
    if warns:
        w = warns[0]
        check(all(s in w for s in ("device=t283", "port=gi0/0/37",
                                   "old='A2.C07'", "new='A2.C07-NEW'",
                                   "old_file=01-base.json",
                                   "new_file=02-override.json")),
              f"A: warning has device/port/old/new/both files")

    # B) Same device, different ports -> no warning.
    _setup({
        "01-base.json":     {"T283": {"Gi0/0/37": "A"}},
        "02-more.json":     {"T283": {"Gi0/0/38": "B"}},
    })
    port_aliases.lookup("T283", "", "Gi0/0/37")
    check(len(_dup_warnings()) == 0, "B: different ports -> no warning")

    # C) Different devices, same port -> no warning.
    _setup({
        "01-a.json": {"T283": {"Gi0/0/37": "A"}},
        "02-b.json": {"T999": {"Gi0/0/37": "B"}},
    })
    port_aliases.lookup("T283", "", "Gi0/0/37")
    check(len(_dup_warnings()) == 0, "C: different devices -> no warning")

    # D) Same port in name-space AND mac-space -> not a conflict; priority
    #    (name first, MAC fallback) preserved.
    _setup({
        "01.json": {
            "T283": {"Gi0/0/37": "byname"},
            "58:25:75:E6:0E:1D": {"Gi0/0/37": "bymac"},
        },
    })
    by_name = port_aliases.lookup("T283", "58:25:75:E6:0E:1D", "Gi0/0/37")
    by_mac  = port_aliases.lookup("", "58:25:75:E6:0E:1D", "Gi0/0/37")
    check(len(_dup_warnings()) == 0, "D: name vs MAC space -> no warning")
    check(by_name == "byname", f"D: name match wins (got {by_name!r})")
    check(by_mac == "bymac", f"D: MAC fallback works (got {by_mac!r})")

    # E) Bad JSON file is skipped; discovery continues from the good file.
    _setup({
        "01-base.json": {"T283": {"Gi0/0/37": "A"}},
        "02-bad.json":  "{ this is not valid json",
    })
    got = port_aliases.lookup("T283", "", "Gi0/0/37")
    check(got == "A", f"E: good file still works despite bad JSON (got {got!r})")

    # F) Identical redefinition across files -> silent (no data lost).
    _setup({
        "01.json": {"T283": {"Gi0/0/37": "SAME"}},
        "02.json": {"T283": {"Gi0/0/37": "SAME"}},
    })
    got = port_aliases.lookup("T283", "", "Gi0/0/37")
    check(got == "SAME", f"F: value preserved (got {got!r})")
    check(len(_dup_warnings()) == 0, "F: identical redefinition -> no warning")

    print()
    if fails:
        print(f"FAILED: {len(fails)} issue(s).")
        return 1
    print("PASSED: duplicate-alias detection behaves per acceptance criteria.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
